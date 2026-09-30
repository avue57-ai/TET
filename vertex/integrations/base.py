"""Backend selection and key-based executors.

Every connector call is a bridge job. `bridge` mode prints the MCP call for Claude to execute; `key` mode
executes the same job directly (httpx REST for Lemlist/Apollo, an MCP JSON-RPC client for Inven).
The key backends are only usable in an environment whose network policy allows the provider hosts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from vertex.bridge.jobs import BridgeUnavailable
from vertex.settings import get_settings
from vertex.utils.errors import FatalError, RetryableError
from vertex.utils.logging import get_logger

log = get_logger("backend")


def backend_mode(provider: str) -> str:
    return get_settings().backend_for(provider)


def probe_hosts() -> dict[str, str]:
    """Check whether the provider hosts are reachable (policy allows CONNECT). Cheap, no auth needed."""
    out: dict[str, str] = {}
    for provider, url in (("lemlist", "https://api.lemlist.com/"), ("apollo", "https://api.apollo.io/"),
                          ("inven", "https://api.inven.ai/")):
        try:
            r = httpx.get(url, timeout=10.0, follow_redirects=False)
            out[provider] = f"reachable (HTTP {r.status_code})"
        except httpx.HTTPError as ex:
            out[provider] = f"blocked or unreachable ({type(ex).__name__})"
    return out


# ---------------------------------------------------------------- Lemlist REST

_LEMLIST_TOOL_MAP: dict[str, tuple[str, str]] = {
    # tool name used in bridge jobs -> (method, path template)
    "get_campaigns": ("GET", "/api/campaigns"),
    "get_campaign_details": ("GET", "/api/campaigns/{campaignId}"),
    "get_campaign_sequences": ("GET", "/api/campaigns/{campaignId}/sequences"),
    "get_unsubscribes": ("GET", "/api/unsubscribes"),
    "search_campaign_leads": ("GET", "/api/campaigns/{campaignId}/leads"),
    "search_contacts": ("GET", "/api/contacts"),
    "get_lead_by_email": ("GET", "/api/leads/{email}"),
    "get_activities": ("GET", "/api/activities"),
    "get_inbox_conversations": ("GET", "/api/inbox"),
    "get_inbox_conversation": ("GET", "/api/inbox/{contactId}"),
    "create_campaign": ("POST", "/api/campaigns"),
    "add_sequence_step": ("POST", "/api/sequences/{sequenceId}/steps"),
    "update_sequence_step": ("PATCH", "/api/sequences/{sequenceId}/steps/{stepId}"),
    "add_leads_to_campaign": ("POST", "/api/campaigns/{campaignId}/leads"),
    "update_lead_variables": ("PATCH", "/api/leads/{leadId}/variables"),
    "pause_campaign": ("POST", "/api/campaigns/{campaignId}/pause"),
    "add_unsubscribe": ("POST", "/api/unsubscribes/{email}"),
    "get_team_senders": ("GET", "/api/team/senders"),
}


def _lemlist_call(tool: str, args: dict[str, Any]) -> Any:
    key = get_settings().lemlist_api_key
    if not key:
        raise BridgeUnavailable("LEMLIST_API_KEY not set")
    if tool not in _LEMLIST_TOOL_MAP:
        raise FatalError(f"no REST mapping for lemlist tool {tool}")
    if tool == "search_campaign_leads" and "email" in args and "campaignId" not in args:
        tool, args = "get_lead_by_email", {"email": args["email"], "version": "v2"}
    method, path = _LEMLIST_TOOL_MAP[tool]
    path = path.format(**{k: v for k, v in args.items() if isinstance(v, str)})
    params = {k: v for k, v in args.items() if f"{{{k}}}" not in _LEMLIST_TOOL_MAP[tool][1]} if method == "GET" else None
    body = {k: v for k, v in args.items() if f"{{{k}}}" not in _LEMLIST_TOOL_MAP[tool][1]} if method != "GET" else None
    try:
        r = httpx.request(method, f"https://api.lemlist.com{path}", params=params, json=body,
                          auth=("", key), timeout=60.0)
    except httpx.HTTPError as ex:
        raise BridgeUnavailable(f"lemlist unreachable: {ex}") from ex
    if r.status_code == 429 or r.status_code >= 500:
        raise RetryableError(f"lemlist {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400:
        raise FatalError(f"lemlist {r.status_code}: {r.text[:300]}")
    return r.json()


# ---------------------------------------------------------------- Apollo REST

_APOLLO_TOOL_MAP: dict[str, tuple[str, str]] = {
    "apollo_mixed_people_api_search": ("POST", "/api/v1/mixed_people/api_search"),
    "apollo_people_bulk_match": ("POST", "/api/v1/people/bulk_match"),
    "apollo_organizations_bulk_enrich": ("POST", "/api/v1/organizations/bulk_enrich"),
    "apollo_organizations_enrich": ("GET", "/api/v1/organizations/enrich"),
    "apollo_contacts_search": ("POST", "/api/v1/contacts/search"),
    "apollo_labels_index": ("GET", "/api/v1/labels"),
}


def _apollo_call(tool: str, args: dict[str, Any]) -> Any:
    key = get_settings().apollo_api_key
    if not key:
        raise BridgeUnavailable("APOLLO_API_KEY not set")
    if tool not in _APOLLO_TOOL_MAP:
        raise FatalError(f"no REST mapping for apollo tool {tool}")
    method, path = _APOLLO_TOOL_MAP[tool]
    clean = {k: v for k, v in args.items() if not k.startswith("_")}
    try:
        r = httpx.request(method, f"https://api.apollo.io{path}", params=clean if method == "GET" else None,
                          json=clean if method != "GET" else None,
                          headers={"x-api-key": key, "Content-Type": "application/json", "Cache-Control": "no-cache"},
                          timeout=60.0)
    except httpx.HTTPError as ex:
        raise BridgeUnavailable(f"apollo unreachable: {ex}") from ex
    if r.status_code == 429 or r.status_code >= 500:
        raise RetryableError(f"apollo {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400:
        raise FatalError(f"apollo {r.status_code}: {r.text[:300]}")
    return r.json()


# ---------------------------------------------------------------- Inven MCP-key client

def _inven_call(tool: str, args: dict[str, Any]) -> Any:
    key = get_settings().inven_api_key
    if not key:
        raise BridgeUnavailable("INVEN_API_KEY (MCP bearer key) not set")
    try:
        import anyio
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as ex:
        raise FatalError("mcp package not installed (pip install mcp)") from ex

    async def _run() -> Any:
        async with streamablehttp_client("https://api.inven.ai/mcp/v1", headers={"Authorization": f"Bearer {key}"}) as (
            read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments=args)
                texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
                joined = "\n".join(texts)
                try:
                    return json.loads(joined)
                except json.JSONDecodeError:
                    return {"text": joined}

    try:
        return anyio.run(_run)
    except Exception as ex:  # noqa: BLE001
        raise BridgeUnavailable(f"inven mcp call failed: {ex}") from ex


def execute_with_key(job: dict[str, Any]) -> Any:
    connector = job["connector"]
    if connector == "lemlist":
        return _lemlist_call(job["tool"], job["args"])
    if connector == "apollo":
        return _apollo_call(job["tool"], job["args"])
    if connector == "inven":
        return _inven_call(job["tool"], job["args"])
    raise BridgeUnavailable(f"connector {connector} has no key backend (Claude-only: websearch/granola)")


def save_result(job_id: int, payload: Any, inbox_dir: Path) -> Path:
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / f"{job_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    return path

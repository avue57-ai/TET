"""Lemlist integration.

Bridge purposes (Claude executes the MCP tool, or `bridge run` uses the REST key):
  lemlist.unsubscribes    get_unsubscribes            -> suppression (unsubscribed / bounced)
  lemlist.lead_lookup     search_campaign_leads(email) -> prior-outreach cooldown for that email + proof of lookup
  lemlist.contact_lookup  search_contacts(companyDomain) -> company held for a human when it already exists in Lemlist
Key-only helper: import_campaign_leads_rest() bulk-imports every campaign's leads via the CSV export endpoint.
Campaign build / enrollment / sync live in vertex.workflows.campaign and vertex.workflows.sync.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from vertex.bridge.jobs import enqueue
from vertex.bridge.router import register
from vertex.core.normalize import normalize_domain, normalize_email, normalize_linkedin
from vertex.db.connection import utcnow
from vertex.db.repo import record_source
from vertex.settings import get_settings
from vertex.utils.logging import get_logger
from vertex.workflows.suppression import add_suppression, classify_value, cooldown

log = get_logger("lemlist")
NEGATIVE_STATES = {"notInterested", "unsubscribed", "interested_no", "notinterested"}


# ---------------------------------------------------------------- planners
def plan_unsubscribes(conn: sqlite3.Connection, offset: int = 0, limit: int = 100) -> int:
    return enqueue(conn, "lemlist", "get_unsubscribes", {"limit": limit, "offset": offset}, "lemlist.unsubscribes",
                   context={"offset": offset}, est_credits=0, credit_type="lemlist")


def plan_lookups(conn: sqlite3.Connection, company_ids: list[int]) -> list[int]:
    jobs: list[int] = []
    for cid in company_ids:
        co = conn.execute("SELECT id, domain FROM companies WHERE id = ?", (cid,)).fetchone()
        if not co:
            continue
        jobs.append(enqueue(conn, "lemlist", "search_contacts", {"companyDomain": co["domain"], "limit": 50}, "lemlist.contact_lookup",
                            context={"company_id": cid}, est_credits=0, credit_type="lemlist"))
        for ct in conn.execute("SELECT id, email FROM contacts WHERE company_id = ? AND is_primary = 1 AND email IS NOT NULL", (cid,)):
            jobs.append(enqueue(conn, "lemlist", "search_campaign_leads", {"email": ct["email"]}, "lemlist.lead_lookup",
                                context={"company_id": cid, "contact_id": ct["id"]}, est_credits=0, credit_type="lemlist"))
    return jobs


# ---------------------------------------------------------------- handlers
def _cooldown_from(latest_iso: str | None, days: int) -> str | None:
    """Cooldown expiry measured from the last contact date; None when the cooldown has already elapsed."""
    if not latest_iso:
        return cooldown(days)
    try:
        t = datetime.fromisoformat(latest_iso.replace("Z", "+00:00"))
    except ValueError:
        return cooldown(days)
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    exp = t + timedelta(days=days)
    return None if exp <= datetime.now(UTC) else exp.strftime("%Y-%m-%dT%H:%M:%SZ")


@register("lemlist.unsubscribes")
def ingest_unsubscribes(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    rows = payload.get("unsubscribes", []) if isinstance(payload, dict) else payload
    inserted = 0
    for r in rows:
        if isinstance(r, (list, tuple)):
            value, source, created = (list(r) + [None, None, None])[:3]
        else:
            value, source, created = r.get("value"), r.get("source"), r.get("createdAt")
        cv = classify_value(value or "")
        if not cv:
            continue
        reason = "bounced" if source == "bounced" else "unsubscribed"
        inserted += int(add_suppression(conn, cv[0], cv[1], reason, "lemlist_unsubscribes"))
        if reason == "bounced" and cv[0] == "email":
            conn.execute("UPDATE contacts SET email_status = 'bounced', updated_at = ? WHERE email = ?", (utcnow(), cv[1]))
    # REST auto-paging: a full page means there may be more
    limit = int(job["args"].get("limit", 100))
    if isinstance(payload, dict) and len(rows) >= limit and job.get("backend") == "key":
        plan_unsubscribes(conn, offset=int(job["args"].get("offset", 0)) + limit, limit=limit)
    return {"rows": len(rows), "inserted": inserted, "skipped": len(rows) - inserted}


@register("lemlist.lead_lookup")
def ingest_lead_lookup(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    email = normalize_email(job["args"]["email"]) or job["args"]["email"]
    ctx = job.get("context") or {}
    if isinstance(payload, dict):
        leads = payload.get("leads") or ([payload["lead"]] if payload.get("lead") else []) or ([payload] if payload.get("_id") else [])
    else:
        leads = list(payload or [])
    record_source(conn, "lemlist", f"lead_lookup:{email}", ctx.get("company_id"),
                  {"email": email, "leads": leads, "checked_at": utcnow()}, batch_id=batch_id)
    days = int(get_settings().campaign.get("cooldown_days_prior_outreach", 180))
    n = 0
    if leads:
        latest = max((l.get("createdAt") or "") for l in leads) or None
        negative = any((l.get("lastState") or l.get("state") or "") in NEGATIVE_STATES for l in leads)
        exp = None if negative else _cooldown_from(latest, days)
        if negative or exp:
            n += int(add_suppression(conn, "email", email, "not_interested" if negative else "prior_outreach", "lemlist_leads", expires_at=exp))
        for l in leads:
            li = normalize_linkedin(l.get("linkedinUrl") or "")
            if li and (negative or exp):
                n += int(add_suppression(conn, "linkedin", li, "not_interested" if negative else "prior_outreach", "lemlist_leads", expires_at=exp))
    return {"rows": len(leads), "inserted": n, "skipped": 0}


@register("lemlist.contact_lookup")
def ingest_contact_lookup(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    domain = normalize_domain(job["args"]["companyDomain"]) or job["args"]["companyDomain"]
    ctx = job.get("context") or {}
    contacts = (payload.get("contacts") if isinstance(payload, dict) else payload) or []
    record_source(conn, "lemlist", f"contact_lookup:{domain}", ctx.get("company_id"),
                  {"domain": domain, "contacts": contacts, "checked_at": utcnow()}, batch_id=batch_id)
    n = 0
    if contacts:
        # The company already exists in the Lemlist CRM: a human decides whether it was worked before.
        n += int(add_suppression(conn, "domain", domain, "prior_outreach", "lemlist_contacts", expires_at=cooldown(180), hold_for_human=True))
        for c in contacts:
            e = normalize_email(c.get("email") or "")
            if e and (c.get("campaignCount") or c.get("campaignsCount") or 0):
                n += int(add_suppression(conn, "email", e, "prior_outreach", "lemlist_contacts", expires_at=cooldown(180)))
    return {"rows": len(contacts), "inserted": n, "skipped": 0}


# ---------------------------------------------------------------- key-only bulk import
def import_campaign_leads_rest(conn: sqlite3.Connection) -> dict[str, Any]:
    """Needs LEMLIST_API_KEY and network access to api.lemlist.com. Imports every campaign's leads as prior outreach."""
    import httpx

    key = get_settings().lemlist_api_key
    if not key:
        raise RuntimeError("LEMLIST_API_KEY not set")
    days = int(get_settings().campaign.get("cooldown_days_prior_outreach", 180))
    auth = ("", key)
    camps: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = httpx.get("https://api.lemlist.com/api/campaigns", params={"version": "v2", "limit": 100, "offset": offset}, auth=auth, timeout=60)
        r.raise_for_status()
        page = r.json().get("campaigns", r.json()) if isinstance(r.json(), dict) else r.json()
        camps += page
        if len(page) < 100:
            break
        offset += 100
    total = inserted = 0
    for c in camps:
        r = httpx.get(f"https://api.lemlist.com/api/v2/campaigns/{c['_id']}/export/leads", params={"state": "all", "format": "csv"}, auth=auth, timeout=120)
        if r.status_code != 200:
            log.warning("export failed", campaign=c.get("_id"), status=r.status_code)
            continue
        for row in csv.DictReader(io.StringIO(r.text)):
            total += 1
            e = normalize_email(row.get("email") or "")
            state = row.get("lastState") or ""
            negative = state in NEGATIVE_STATES
            exp = None if negative else _cooldown_from(row.get("createdAt") or row.get("addedAt"), days)
            if e and (negative or exp):
                inserted += int(add_suppression(conn, "email", e, "not_interested" if negative else "prior_outreach", f"lemlist_export:{c.get('name', '')[:40]}", expires_at=exp))
            li = normalize_linkedin(row.get("linkedinUrl") or "")
            if li and (negative or exp):
                inserted += int(add_suppression(conn, "linkedin", li, "not_interested" if negative else "prior_outreach", "lemlist_export", expires_at=exp))
    conn.execute("INSERT INTO ingest_batches(source, kind, file_path, file_sha256, rows, inserted, status, started_at, finished_at) VALUES ('lemlist','campaign_leads','rest',?,?,?,'done',?,?)",
                 (f"campaign_leads:{utcnow()}", total, inserted, utcnow(), utcnow()))
    return {"campaigns": len(camps), "leads": total, "inserted": inserted}

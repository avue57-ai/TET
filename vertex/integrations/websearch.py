"""WebSearch (Claude-only) research jobs: query templates and summary ingest into source_records / signals.

Claude executes the WebSearch tool for each job and saves {"query": ..., "summary": "<text>", "links": [...]}
to data/inbox/<job>.json. Scripts cannot call WebSearch, so these jobs never have a key backend.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from vertex.bridge.jobs import enqueue
from vertex.bridge.router import register
from vertex.db.repo import record_source


def signal_queries(name: str, city: str | None, state: str | None) -> list[tuple[str, str]]:
    loc = " ".join(x for x in (city, state) if x)
    return [
        ("overview", f'"{name}" {loc} software'),
        ("credentials", f'"{name}" award OR anniversary OR accredited OR certified OR "years in business" OR "founded in"'),
        ("customers", f'"{name}" customers OR "case study" OR project OR expansion OR "new office" OR partnership'),
        ("people", f'"{name}" founder OR owner OR president OR CEO'),
    ]


def plan_signal_searches(conn: sqlite3.Connection, company_id: int, kinds: list[str] | None = None) -> list[int]:
    row = conn.execute("SELECT name, hq_city, hq_state FROM companies WHERE id = ?", (company_id,)).fetchone()
    jobs = []
    for kind, q in signal_queries(row["name"], row["hq_city"], row["hq_state"]):
        if kinds and kind not in kinds:
            continue
        jobs.append(enqueue(conn, "websearch", "WebSearch", {"query": q}, "websearch.signals",
                            context={"company_id": company_id, "kind": kind}, est_credits=1, credit_type="websearch"))
    return jobs


def _store(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None) -> dict[str, Any]:
    cid = job["context"]["company_id"]
    kind = job["context"].get("kind", "search")
    if isinstance(payload, str):
        payload = {"summary": payload}
    payload = dict(payload)
    payload.setdefault("query", job["args"].get("query"))
    payload["kind"] = kind
    _, new = record_source(conn, "websearch", f"{cid}:{kind}:{payload.get('query')}", cid, payload, batch_id=batch_id)
    return {"rows": 1, "inserted": int(new), "skipped": int(not new)}


@register("websearch.prescreen")
def ingest_prescreen(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    return _store(conn, job, payload, batch_id)


@register("websearch.signals")
def ingest_signals(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    return _store(conn, job, payload, batch_id)

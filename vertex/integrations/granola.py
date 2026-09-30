"""Granola: meeting list ingest → meetings table + existing-relationship holds by company-name match in titles.

Granola's list_meetings exposes titles and the note owner only (no external attendee emails on this plan), so the
match is name-based and always held for a human rather than auto-suppressed. Meeting outcomes for the feedback loop
are recorded by the human (or by get_meetings on a matched meeting) in vertex.workflows.pipeline.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from vertex.bridge.jobs import enqueue
from vertex.bridge.router import register
from vertex.core.normalize import normalize_name
from vertex.db.connection import utcnow
from vertex.workflows.suppression import add_suppression

GENERIC = {"software", "systems", "solutions", "technologies", "group", "services", "labs", "data", "health", "medical", "analytics", "networks", "partners", "capital"}


def plan_meetings(conn: sqlite3.Connection, time_range: str = "last_30_days") -> int:
    return enqueue(conn, "granola", "list_meetings", {"time_range": time_range}, "granola.meetings",
                   context={"time_range": time_range}, est_credits=0, credit_type="granola")


def _match_companies(conn: sqlite3.Connection, title: str) -> list[sqlite3.Row]:
    t = " " + re.sub(r"[^a-z0-9 ]+", " ", title.lower()) + " "
    hits = []
    for r in conn.execute("SELECT id, name, name_norm, domain FROM companies WHERE name_norm IS NOT NULL AND length(name_norm) >= 4"):
        n = r["name_norm"]
        if n in GENERIC or len(n) < 4:
            continue
        if f" {n} " in t:
            hits.append(r)
    return hits


@register("granola.meetings")
def ingest_meetings(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    rows = payload.get("meetings", []) if isinstance(payload, dict) else payload
    inserted = matched = 0
    for m in rows:
        mid, title, date = m.get("id"), (m.get("title") or "").strip(), m.get("date")
        if not mid:
            continue
        exists = conn.execute("SELECT id, company_id FROM meetings WHERE granola_meeting_id = ?", (mid,)).fetchone()
        if not exists:
            conn.execute("INSERT INTO meetings(granola_meeting_id, held_at, summary, created_at) VALUES (?,?,?,?)", (mid, date, title[:300], utcnow()))
            inserted += 1
        for co in _match_companies(conn, title):
            matched += 1
            conn.execute("UPDATE meetings SET company_id = COALESCE(company_id, ?), matched_by = COALESCE(matched_by, 'title') WHERE granola_meeting_id = ?", (co["id"], mid))
            add_suppression(conn, "domain", co["domain"], "existing_relationship", f"granola:{mid[:8]}", expires_at=None, hold_for_human=True)
    return {"rows": len(rows), "inserted": inserted, "skipped": len(rows) - inserted, "matched": matched}

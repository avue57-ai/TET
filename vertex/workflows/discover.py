"""Discovery workflow: thesis -> source queries -> Inven bridge jobs (build_search -> build_columns -> pages)."""

from __future__ import annotations

import sqlite3
from typing import Any

from vertex.core.thesis import Thesis, compile_queries, ensure_source_queries, register_thesis
from vertex.integrations.inven import plan_build_search
from vertex.utils.logging import get_logger

log = get_logger("discover")


def exclusion_domains(conn: sqlite3.Connection) -> list[str]:
    known = [r[0] for r in conn.execute("SELECT domain FROM companies")]
    supp = [r[0] for r in conn.execute("SELECT value FROM suppression WHERE kind = 'domain' AND (expires_at IS NULL OR expires_at > datetime('now'))")]
    return sorted(set(known) | set(supp))


def plan_discovery(conn: sqlite3.Connection, thesis: Thesis, verticals: list[str] | None = None,
                   families: list[str] | None = None) -> dict[str, Any]:
    thesis_id = register_thesis(conn, thesis)
    excl = exclusion_domains(conn)
    planned: list[dict[str, Any]] = []
    for v in thesis.verticals:
        if verticals and v.key not in verticals:
            continue
        queries = compile_queries(thesis, v, families)
        ids = ensure_source_queries(conn, thesis_id, v.key, queries)
        for (kind, desc), sq_id in zip(queries, ids, strict=True):
            status = conn.execute("SELECT status FROM source_queries WHERE id = ?", (sq_id,)).fetchone()[0]
            if status not in ("planned",):
                continue
            job_id = plan_build_search(conn, thesis_id, sq_id, desc, excl)
            planned.append({"source_query_id": sq_id, "vertical": v.key, "kind": kind, "job_id": job_id})
    log.info(f"planned {len(planned)} build_search job(s) for thesis {thesis.slug}; excluding {len(excl)} known/suppressed domains")
    return {"thesis_id": thesis_id, "planned": planned, "excluded_domains": len(excl)}


def discovery_status(conn: sqlite3.Connection, thesis_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, vertical, query_kind, status, estimated_total, rows_returned, new_companies, prescreen_keeper_rate, halted_reason "
        "FROM source_queries WHERE thesis_id = ? ORDER BY vertical, id", (thesis_id,)
    ).fetchall()
    return [dict(r) for r in rows]

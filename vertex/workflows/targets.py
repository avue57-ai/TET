"""Target acceptance: propose the top-scored keepers, record accept/pass decisions, move stages.

Acceptance is a decision row on an `accept_target` review item. A human decides on the review page; the engine may
pre-accept a wave on explicit instruction (actor="system", reason recorded) so enrichment can start, and every
pre-accepted target still appears in Section C where the human can reverse it before any enrollment.
"""

from __future__ import annotations

import json
import random
import sqlite3
from typing import Any

from vertex.core.pipeline import advance_if_behind, move
from vertex.db.connection import utcnow
from vertex.db.repo import add_review_item

ACCEPTED_STAGES = ("Qualified", "Contact Found", "Ready for Outreach", "Contacted", "Engaged", "Conversation", "NDA", "Diligence", "LOI")


def candidate_rows(conn: sqlite3.Connection, thesis_id: int, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.id AS company_id, c.name, c.domain, c.vertical, c.hq_city, c.hq_state, c.employee_count, c.ownership_type, c.ownership_confidence, "
        "s.vertex_score, s.attractiveness, s.transactability, s.vertex_conf, s.coverage_pct, s.provisional, s.tier, s.id AS score_id, tc.stage, tc.prescreen_evidence_json "
        "FROM thesis_companies tc JOIN companies c ON c.id = tc.company_id "
        "JOIN v_latest_score s ON s.company_id = tc.company_id AND s.thesis_id = tc.thesis_id "
        "WHERE tc.thesis_id = ? AND tc.prescreen_status = 'keep' AND tc.stage NOT IN ('Excluded','Passed') AND s.tier IN ('T1','T2','T3') "
        "AND c.domain NOT IN (SELECT value FROM suppression WHERE kind = 'domain' AND (expires_at IS NULL OR expires_at > ?)) "
        "ORDER BY s.vertex_score DESC, s.vertex_conf DESC LIMIT ?",
        (thesis_id, utcnow(), limit),
    ).fetchall()


def propose_targets(conn: sqlite3.Connection, thesis_id: int, top_n: int = 50, exploration_pct: float = 0.10,
                    per_vertical_cap: int | None = None, seed: int = 7) -> list[int]:
    """Create pending accept_target review items for the top N by score plus a small exploration cohort of T3/low-confidence."""
    rows = candidate_rows(conn, thesis_id, limit=400)
    n_explore = max(0, int(round(top_n * exploration_pct)))
    n_core = top_n - n_explore
    core: list[sqlite3.Row] = []
    per_v: dict[str, int] = {}
    for r in rows:
        if len(core) >= n_core:
            break
        if per_vertical_cap and per_v.get(r["vertical"], 0) >= per_vertical_cap:
            continue
        core.append(r)
        per_v[r["vertical"]] = per_v.get(r["vertical"], 0) + 1
    chosen = {r["company_id"] for r in core}
    pool = [r for r in rows if r["company_id"] not in chosen and (r["tier"] == "T3" or (r["vertex_conf"] or 0) < 0.5)]
    rnd = random.Random(seed)
    explore = rnd.sample(pool, min(n_explore, len(pool))) if pool else []
    items: list[int] = []
    for cohort, group in (("core", core), ("exploration", explore)):
        for r in group:
            ev = json.loads(r["prescreen_evidence_json"] or "{}")
            payload = {"cohort": cohort, "score": r["vertex_score"], "tier": r["tier"], "confidence": r["vertex_conf"], "coverage": r["coverage_pct"],
                       "provisional": r["provisional"], "vertical": r["vertical"], "ownership": r["ownership_type"], "ownership_confidence": r["ownership_confidence"],
                       "employees": r["employee_count"], "fit_reason": ev.get("fit_reason"), "ownership_evidence": ev.get("ownership_evidence"), "score_id": r["score_id"]}
            items.append(add_review_item(conn, "accept_target", "companies", r["company_id"], r["company_id"], payload, priority=2 if cohort == "core" else 3))
    return items


def decide_target(conn: sqlite3.Connection, thesis_id: int, company_id: int, decision: str, decided_by: str, source: str,
                  reason_code: str | None = None, note: str | None = None) -> int:
    item = conn.execute("SELECT id FROM review_items WHERE item_type = 'accept_target' AND ref_table = 'companies' AND ref_id = ?", (company_id,)).fetchone()
    item_id = item["id"] if item else add_review_item(conn, "accept_target", "companies", company_id, company_id, {"cohort": "manual"}, priority=2)
    cur = conn.execute(
        "INSERT INTO review_decisions(review_item_id, decision, reason_code, payload_json, decided_by, source, decided_at) VALUES (?,?,?,?,?,?,?)",
        (item_id, decision, reason_code, json.dumps({"note": note} if note else {}), decided_by, source, utcnow()))
    conn.execute("UPDATE review_items SET status = 'decided' WHERE id = ?", (item_id,))
    if decision == "approve":
        advance_if_behind(conn, thesis_id, company_id, "Qualified", note or f"accept_target by {decided_by}", "human" if decided_by != "system" else "system")
    elif decision == "reject":
        move(conn, thesis_id, company_id, "Passed", f"{reason_code or 'rejected'}: {note or ''}".strip(), "human" if decided_by != "system" else "system", force=True)
    return int(cur.lastrowid)


def accept_pending(conn: sqlite3.Connection, thesis_id: int, decided_by: str, source: str, note: str, cohorts: tuple[str, ...] = ("core", "exploration")) -> list[int]:
    ids = []
    for it in conn.execute("SELECT id, ref_id, payload_json FROM review_items WHERE item_type = 'accept_target' AND status = 'pending'").fetchall():
        if json.loads(it["payload_json"] or "{}").get("cohort", "core") not in cohorts:
            continue
        ids.append(decide_target(conn, thesis_id, it["ref_id"], "approve", decided_by, source, note=note))
    return ids


def accepted_company_ids(conn: sqlite3.Connection, thesis_id: int, limit: int = 500) -> list[int]:
    q = ("SELECT tc.company_id FROM thesis_companies tc LEFT JOIN v_latest_score s ON s.company_id = tc.company_id AND s.thesis_id = tc.thesis_id "
         f"WHERE tc.thesis_id = ? AND tc.stage IN ({','.join('?' * len(ACCEPTED_STAGES))}) ORDER BY COALESCE(s.vertex_score, 0) DESC LIMIT ?")
    return [r[0] for r in conn.execute(q, (thesis_id, *ACCEPTED_STAGES, limit))]


def target_table(conn: sqlite3.Connection, thesis_id: int, limit: int = 60) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute(
        "SELECT c.id, c.name, c.domain, c.vertical, c.hq_state, c.employee_count, c.ownership_type, c.ownership_confidence, s.vertex_score, s.tier, s.vertex_conf, s.coverage_pct, s.provisional, tc.stage, "
        "(SELECT COUNT(*) FROM contacts ct WHERE ct.company_id = c.id AND ct.is_primary = 1) AS has_primary, "
        "(SELECT COUNT(*) FROM signals sg WHERE sg.company_id = c.id AND sg.safe_to_cite = 1) AS hooks, "
        "(SELECT COUNT(*) FROM messages m WHERE m.company_id = c.id AND m.status IN ('draft','approved','edited')) AS msgs "
        "FROM thesis_companies tc JOIN companies c ON c.id = tc.company_id LEFT JOIN v_latest_score s ON s.company_id = c.id AND s.thesis_id = tc.thesis_id "
        f"WHERE tc.thesis_id = ? AND tc.stage IN ({','.join('?' * len(ACCEPTED_STAGES))}) ORDER BY s.vertex_score DESC LIMIT ?",
        (thesis_id, *ACCEPTED_STAGES, limit)):
        out.append(dict(r))
    return out

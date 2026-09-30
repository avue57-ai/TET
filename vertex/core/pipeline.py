"""Pipeline stage machine. Stage lives per thesis x company; companies.current_stage is a denormalized mirror."""

from __future__ import annotations

import sqlite3

from vertex.db.connection import utcnow

STAGES = [
    "Identified", "Screened", "Qualified", "Contact Found", "Ready for Outreach", "Contacted", "Engaged",
    "Conversation", "NDA", "Diligence", "LOI", "Closed",
]
TERMINAL = {"Passed", "Excluded", "Closed"}
HUMAN_ONLY = {"NDA", "Diligence", "LOI", "Closed"}


def stage_index(stage: str) -> int:
    return STAGES.index(stage) if stage in STAGES else -1


def can_transition(from_stage: str | None, to_stage: str, actor: str) -> tuple[bool, str]:
    if to_stage not in STAGES and to_stage not in TERMINAL:
        return False, f"unknown stage {to_stage}"
    if from_stage in TERMINAL and actor != "human":
        return False, f"{from_stage} is terminal; only a human can reopen"
    if to_stage in HUMAN_ONLY and actor != "human":
        return False, f"{to_stage} can only be set by a human"
    if to_stage in ("Passed", "Excluded"):
        return True, "ok"
    if from_stage is None or from_stage in TERMINAL:
        return True, "ok"
    if stage_index(to_stage) < stage_index(from_stage) and actor != "human":
        return False, "backward moves require a human"
    return True, "ok"


def current_stage(conn: sqlite3.Connection, thesis_id: int, company_id: int) -> str | None:
    row = conn.execute("SELECT stage FROM thesis_companies WHERE thesis_id = ? AND company_id = ?",
                       (thesis_id, company_id)).fetchone()
    return row["stage"] if row else None


def move(conn: sqlite3.Connection, thesis_id: int, company_id: int, to_stage: str, reason: str,
         actor: str = "system", force: bool = False) -> bool:
    """Move a company to a stage. Returns True if a transition was recorded (no-op if already there)."""
    frm = current_stage(conn, thesis_id, company_id)
    if frm == to_stage:
        return False
    ok, why = can_transition(frm, to_stage, actor)
    if not ok and not force:
        raise ValueError(f"cannot move {company_id} from {frm} to {to_stage}: {why}")
    now = utcnow()
    conn.execute(
        "INSERT INTO stage_transitions(company_id, thesis_id, from_stage, to_stage, reason, actor, at) VALUES (?,?,?,?,?,?,?)",
        (company_id, thesis_id, frm, to_stage, reason, actor, now),
    )
    conn.execute(
        "UPDATE thesis_companies SET stage = ?, stage_updated_at = ?, exclusion_reason = CASE WHEN ? IN ('Excluded','Passed') THEN ? ELSE exclusion_reason END"
        " WHERE thesis_id = ? AND company_id = ?",
        (to_stage, now, to_stage, reason, thesis_id, company_id),
    )
    conn.execute("UPDATE companies SET current_stage = ?, updated_at = ? WHERE id = ?", (to_stage, now, company_id))
    return True


def advance_if_behind(conn: sqlite3.Connection, thesis_id: int, company_id: int, to_stage: str, reason: str,
                      actor: str = "system") -> bool:
    """Move forward only; never regress a company that is already further along."""
    frm = current_stage(conn, thesis_id, company_id)
    if frm in TERMINAL:
        return False
    if frm is not None and stage_index(frm) >= stage_index(to_stage):
        return False
    return move(conn, thesis_id, company_id, to_stage, reason, actor)

"""Prescreen: sample companies per vertical, judge size + ownership from Inven fields and WebSearch summaries."""

from __future__ import annotations

import json
import random
import sqlite3
from typing import Any

from pydantic import BaseModel, Field

from vertex.ai.claude import run_prompt
from vertex.bridge.jobs import enqueue
from vertex.core.thesis import Thesis
from vertex.integrations.inven import latest_inven_extras
from vertex.utils.logging import get_logger

log = get_logger("prescreen")


class PrescreenVerdict(BaseModel):
    thesis_fit: bool | None = None
    fit_reason: str = ""
    size_band_ok: bool | None = None
    ownership: str = "unknown"
    ownership_evidence: str | None = None
    institutional_capital_evidence: str | None = None
    keep: bool = False
    confidence: float = Field(default=0.4, ge=0, le=1)
    reason: str = ""


def sample_companies(conn: sqlite3.Connection, thesis_id: int, vertical: str, n: int = 25, seed: int = 7) -> list[int]:
    rows = conn.execute(
        "SELECT tc.company_id, tc.source_query_id FROM thesis_companies tc JOIN companies c ON c.id = tc.company_id "
        "WHERE tc.thesis_id = ? AND c.vertical = ? AND tc.prescreen_status = 'pending' AND tc.stage IN ('Identified','Screened')",
        (thesis_id, vertical),
    ).fetchall()
    by_query: dict[int | None, list[int]] = {}
    for r in rows:
        by_query.setdefault(r["source_query_id"], []).append(r["company_id"])
    rnd = random.Random(seed)
    picked: list[int] = []
    # stratified across query families
    while len(picked) < n and any(by_query.values()):
        for q, ids in list(by_query.items()):
            if ids and len(picked) < n:
                picked.append(ids.pop(rnd.randrange(len(ids))))
    return picked


def plan_websearch_for(conn: sqlite3.Connection, company_id: int) -> list[int]:
    row = conn.execute("SELECT name, hq_city, hq_state, domain FROM companies WHERE id = ?", (company_id,)).fetchone()
    loc = " ".join(x for x in (row["hq_city"], row["hq_state"]) if x)
    q1 = f'"{row["name"]}" {loc} owner OR founder OR "founded by" OR acquired OR "portfolio company" OR "backed by" OR ESOP'
    jobs = [enqueue(conn, "websearch", "WebSearch", {"query": q1}, "websearch.prescreen",
                    context={"company_id": company_id, "kind": "ownership"}, est_credits=1, credit_type="websearch")]
    return jobs


def judge(conn: sqlite3.Connection, company_id: int, thesis_id: int, thesis: Thesis) -> PrescreenVerdict:
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    extras = latest_inven_extras(conn, company_id)
    summaries = conn.execute(
        "SELECT payload_json FROM source_records WHERE company_id = ? AND source = 'websearch' ORDER BY id DESC LIMIT 3",
        (company_id,),
    ).fetchall()
    material_parts = []
    if row["description"]:
        material_parts.append(f"DESCRIPTION: {row['description']}")
    if row["ownership_detail"]:
        material_parts.append(f"INVEN OWNERSHIP DETAIL: {row['ownership_detail']}")
    for s in summaries:
        p = json.loads(s[0])
        material_parts.append(f"WEB SEARCH SUMMARY: {p.get('summary') or p.get('text') or json.dumps(p)[:2000]}")
    material = "\n".join(material_parts)
    emp = thesis.size.get("employees") or [25, 250]
    rev = thesis.size.get("revenue_usd") or [5e6, 50e6]
    payload = {
        "thesis_name": thesis.name, "vertical_description": next((v.description for v in thesis.verticals if v.key == row["vertical"]), thesis.verticals[0].description),
        "employees_lo": emp[0], "employees_hi": emp[1],
        "revenue_lo_m": int(rev[0] / 1e6), "revenue_hi_m": int(rev[1] / 1e6),
        "company_name": row["name"], "domain": row["domain"],
        "hq": ", ".join(x for x in (row["hq_city"], row["hq_state"]) if x),
        "employees": row["employee_count"], "ownership_type": row["ownership_type"],
        "investors": row["investors_json"] or "[]", "founded": row["year_founded"], "material": material or "(none)",
    }
    v = run_prompt(conn, "prescreen", payload, PrescreenVerdict)
    # substring guard on quotes
    norm = " ".join(material.lower().split())
    for attr in ("ownership_evidence", "institutional_capital_evidence"):
        q = getattr(v, attr)
        if q and " ".join(q.lower().split()) not in norm:
            setattr(v, attr, None)
    if v.ownership in ("pe_backed", "vc_backed", "public", "corporate") and not v.institutional_capital_evidence and not v.ownership_evidence:
        v.ownership, v.keep = "unknown", v.keep  # cannot exclude on an unquoted claim
    if v.thesis_fit is False:
        v.keep = False
    status = "keep" if v.keep else ("drop" if v.thesis_fit is False or v.ownership in ("pe_backed", "vc_backed", "public", "corporate") or v.size_band_ok is False else "unclear")
    conn.execute(
        "UPDATE thesis_companies SET prescreen_status = ?, prescreen_evidence_json = ? WHERE thesis_id = ? AND company_id = ?",
        (status, v.model_dump_json(), thesis_id, company_id),
    )
    if v.ownership != "unknown" and v.confidence >= 0.5 and (v.ownership_evidence or v.institutional_capital_evidence):
        conf = "med" if v.confidence >= 0.5 else "low"
        conn.execute("UPDATE companies SET ownership_type = ?, ownership_confidence = ?, ownership_detail = COALESCE(?, ownership_detail) WHERE id = ?",
                     (v.ownership, conf, v.ownership_evidence or v.institutional_capital_evidence, company_id))
    return v


def keeper_rates(conn: sqlite3.Connection, thesis_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT sq.id, sq.vertical, sq.query_kind, "
        "SUM(CASE WHEN tc.prescreen_status='keep' THEN 1 ELSE 0 END) AS keep, "
        "SUM(CASE WHEN tc.prescreen_status='drop' THEN 1 ELSE 0 END) AS drop, "
        "SUM(CASE WHEN tc.prescreen_status='unclear' THEN 1 ELSE 0 END) AS unclear "
        "FROM source_queries sq LEFT JOIN thesis_companies tc ON tc.source_query_id = sq.id AND tc.prescreen_status != 'pending' "
        "WHERE sq.thesis_id = ? GROUP BY sq.id", (thesis_id,),
    ).fetchall()
    out = []
    for r in rows:
        judged = (r["keep"] or 0) + (r["drop"] or 0) + (r["unclear"] or 0)
        rate = (r["keep"] or 0) / judged if judged else None
        if rate is not None:
            conn.execute("UPDATE source_queries SET prescreen_keeper_rate = ? WHERE id = ?", (rate, r["id"]))
        out.append({"source_query_id": r["id"], "vertical": r["vertical"], "kind": r["query_kind"], "judged": judged,
                    "keep": r["keep"] or 0, "drop": r["drop"] or 0, "unclear": r["unclear"] or 0, "keeper_rate": rate})
    return out

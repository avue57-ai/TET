"""LLM-scored components with the evidence-substring guard, and the score-company workflow."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, Field

from vertex.ai.claude import prompt_version, run_prompt
from vertex.core import scoring
from vertex.core.thesis import Thesis
from vertex.integrations.inven import latest_inven_extras
from vertex.utils.logging import get_logger

log = get_logger("score")


class Judgement(BaseModel):
    value: int | None = Field(default=None, ge=0, le=5)
    confidence: float = Field(default=0.4, ge=0, le=1)
    evidence_quote: str | None = None


class LLMComponents(BaseModel):
    recurring_revenue: Judgement = Judgement()
    mission_critical: Judgement = Judgement()
    retention_evidence: Judgement = Judgement()
    moat: Judgement = Judgement()
    capital_intensity: Judgement = Judgement()
    scalability: Judgement = Judgement()
    succession_indicators: Judgement = Judgement()
    management_changes: Judgement = Judgement()
    liquidity_signals: Judgement = Judgement()
    bootstrapped_evidence: str | None = None
    founder_led_evidence: str | None = None
    banned_theme_notes: str | None = None


def build_material(conn: sqlite3.Connection, row: sqlite3.Row, extras: dict[str, Any]) -> str:
    parts: list[str] = []
    if row["description"]:
        parts.append(f"DESCRIPTION: {row['description']}")
    kws = extras.get("keywords") or []
    if kws:
        parts.append("KEYWORDS: " + ", ".join(str(k) for k in kws[:40]))
    news = extras.get("news")
    if news:
        parts.append(f"NEWS: {json.dumps(news, ensure_ascii=False, default=str)[:3000]}")
    apollo = conn.execute(
        "SELECT payload_json FROM source_records WHERE company_id = ? AND source = 'apollo_org' ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    if apollo:
        p = json.loads(apollo[0])
        for k in ("short_description", "seo_description", "keywords", "industry"):
            v = p.get(k)
            if v:
                parts.append(f"APOLLO {k.upper()}: {v if isinstance(v, str) else json.dumps(v)[:800]}")
    for s in conn.execute("SELECT hook_type, text, evidence_quote FROM signals WHERE company_id = ? ORDER BY confidence DESC LIMIT 12",
                          (row["id"],)).fetchall():
        parts.append(f"SIGNAL [{s['hook_type']}]: {s['text']} | quote: {s['evidence_quote'] or ''}")
    return "\n".join(parts)


def _quote_in(quote: str | None, material: str) -> str | None:
    if not quote:
        return None
    q = " ".join(quote.lower().split())
    return quote if q in " ".join(material.lower().split()) else None


def score_company(conn: sqlite3.Connection, company_id: int, thesis_id: int, thesis: Thesis, use_llm: bool = True,
                  weights: dict[str, Any] | None = None) -> scoring.ScoreResult:
    weights = weights or scoring.load_weights()
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    extras = latest_inven_extras(conn, company_id)
    vkey = row["vertical"] or thesis.verticals[0].key
    try:
        vertical = thesis.vertical(vkey)
    except KeyError:
        vertical = thesis.verticals[0]
    material = build_material(conn, row, extras)
    llm_dict: dict[str, dict[str, Any]] = {}
    boot = None
    phash = None
    if use_llm and material.strip():
        payload = {
            "thesis_name": thesis.name, "vertical_description": vertical.description,
            "recurring_forms": ", ".join(thesis.recurring_revenue.get("forms", [])),
            "mission_critical_rationale": thesis.mission_critical.get("rationale", ""),
            "company_name": row["name"], "domain": row["domain"],
            "hq": ", ".join(x for x in (row["hq_city"], row["hq_state"]) if x), "employees": row["employee_count"],
            "founded": row["year_founded"], "material": material,
        }
        out = run_prompt(conn, "score_components", payload, LLMComponents)
        phash = prompt_version("score_components")
        llm_dict = {
            k: getattr(out, k).model_dump() for k in LLMComponents.model_fields if isinstance(getattr(out, k), Judgement)
        }
        boot = _quote_in(out.bootstrapped_evidence, material)
        founder_led = _quote_in(out.founder_led_evidence, material)
        if founder_led and not row["founder_active"]:
            conn.execute("UPDATE companies SET founder_active = 1 WHERE id = ?", (company_id,))
            row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        if out.banned_theme_notes:
            conn.execute(
                "INSERT INTO signals(company_id, hook_type, text, evidence_quote, evidence_source, confidence, safe_to_cite, banned_theme, freshness, captured_at)"
                " VALUES (?, 'internal_note', ?, NULL, 'llm', 0.4, 0, 1, 'undated', datetime('now'))",
                (company_id, out.banned_theme_notes[:500]),
            )
    comps, hard = scoring.deterministic_components(conn, row, extras, thesis, vertical, weights, llm_boot=boot)
    if llm_dict:
        comps = scoring.merge_llm(comps, llm_dict, material)
    res = scoring.aggregate(comps, hard, weights, row["ownership_confidence"])
    scoring.persist(conn, company_id, thesis_id, res, weights["version"], phash)
    return res

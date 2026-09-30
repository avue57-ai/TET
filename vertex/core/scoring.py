"""Scoring engine: deterministic components + LLM components -> confidence-weighted axes -> Vertex Score.

Rules of the rubric (see docs/scoring.md):
- component value 0-5 or None; None means missing (no data) or not applicable; never imputed.
- axis = 100 * sum(w*v/5*c) / sum(w*c) over scored components (thesis constants included in the score,
  excluded from coverage).
- coverage = weight of scored company-specific components / weight of applicable company-specific components.
- vertex = 100*sqrt(A/100 * T/100) (geometric) so an attractive-but-untransactable company cannot rank T1.
- band = +/- round(band_k * (1 - vertex_conf)), labelled heuristic.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import yaml

from vertex.core.thesis import Thesis, Vertical
from vertex.db.connection import utcnow
from vertex.settings import get_settings


@dataclass
class Component:
    axis: str
    name: str
    weight: float
    value: float | None = None
    confidence: float | None = None
    evidence_text: str | None = None
    evidence_ref: str | None = None
    status: str = "missing_no_data"          # scored | missing_no_data | missing_not_applicable | thesis_constant
    next_source: str | None = None
    is_estimate: bool = False

    def scored(self) -> bool:
        return self.status in ("scored", "thesis_constant") and self.value is not None and self.confidence is not None


@dataclass
class ScoreResult:
    attractiveness: float | None
    attractiveness_conf: float
    transactability: float | None
    transactability_conf: float
    vertex_score: float | None
    vertex_conf: float
    band_low: float | None
    band_high: float | None
    coverage_pct: float
    provisional: bool
    tier: str
    hard_exclusion_reason: str | None
    data_gaps: dict[str, str]
    components: list[Component] = field(default_factory=list)


def load_weights(path: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    p = settings.config_dir.parent / (path or settings.scoring.get("weights_file", "config/scoring/weights.yaml"))
    with open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------- helpers

def _years_since(year: int | None) -> float | None:
    if not year:
        return None
    return datetime.now(UTC).year - int(year)


def _pct(v: float | None) -> float | None:
    """Inven growth fields are already percents (verified 2026-09-30: 6 = 6%)."""
    return None if v is None else float(v)


def _growth_component(row: sqlite3.Row, w: float) -> Component:
    c = Component("attractiveness", "growth", w)
    g = json.loads(row["growth_json"]) if row["growth_json"] else {}
    h12 = _pct(g.get("headcount_growth_12month"))
    h24 = _pct(g.get("headcount_growth_24month"))
    tr = _pct(g.get("traffic_growth_12month"))
    signals = [x for x in (h12, h24, tr) if x is not None]
    if not signals:
        c.next_source = "Apollo org enrich (headcount growth) or job postings"
        return c
    primary = h12 if h12 is not None else (h24 / 2 if h24 is not None else tr)
    if primary >= 15:
        val = 5
    elif primary >= 5:
        val = 4
    elif primary >= 0:
        val = 3
    elif primary >= -5:
        val = 2
    else:
        val = 1
    emp = row["employee_count"] or 0
    conf = 0.8 if emp >= 100 else 0.65 if emp >= 50 else 0.5 if emp >= 25 else 0.4
    if val >= 4 and len(signals) < 2:
        val, c.is_estimate = 3, True
    c.value, c.confidence, c.status = float(val), conf, "scored"
    c.evidence_text = f"headcount_12m={h12}, headcount_24m={h24}, traffic_12m={tr} (percent)"
    c.evidence_ref = "inven:growth"
    return c


def _margins_component(row: sqlite3.Row, extras: dict[str, Any], w: float) -> Component:
    c = Component("attractiveness", "margins", w)
    margin = extras.get("row", {}).get("latest_ebitda_margin") if extras else None
    basis = extras.get("row", {}).get("latest_ebitda_margin_basis", "") if extras else ""
    if margin is not None and str(basis).startswith("registry"):
        m = float(margin) * (100 if abs(float(margin)) <= 1.5 else 1)
        val = 5 if m >= 30 else 4 if m >= 20 else 3 if m >= 12 else 2 if m >= 5 else 1
        c.value, c.confidence, c.status = float(val), 0.9, "scored"
        c.evidence_text, c.evidence_ref = f"registry EBITDA margin {m:.1f}%", "inven:registry"
    else:
        c.next_source = "no registry data; ask on the call (never estimated)"
    return c


def _founder_ownership(row: sqlite3.Row, w: float) -> tuple[Component, str | None]:
    c = Component("transactability", "founder_ownership", w)
    own, conf = row["ownership_type"] or "unknown", row["ownership_confidence"]
    hard = None
    if own in ("pe_backed", "vc_backed", "public", "corporate"):
        c.value, c.confidence, c.status = 0.0, 0.8, "scored"
        c.evidence_text = row["ownership_detail"]
        hard = f"ownership {own}"
    elif own in ("founder", "family"):
        c.value, c.status = 5.0, "scored"
        c.confidence = {"high": 0.85, "med": 0.6, "low": 0.45}.get(conf or "low", 0.45)
        c.evidence_text = row["ownership_detail"] or f"ownership_type={own}"
        c.evidence_ref = "inven:ownership"
    elif own == "management":
        c.value, c.confidence, c.status = 3.0, 0.6, "scored"
        c.evidence_text = row["ownership_detail"]
    elif own == "esop":
        c.value, c.confidence, c.status = 2.0, 0.6, "scored"
        c.evidence_text = "employee-owned (separate track)"
    else:
        c.next_source = "WebSearch owner query / Apollo org enrich / founder-titled executive"
    return c, hard


def _no_institutional_capital(row: sqlite3.Row, extras: dict[str, Any], llm_boot: str | None, w: float) -> tuple[Component, str | None]:
    c = Component("transactability", "no_institutional_capital", w)
    inv = json.loads(row["investors_json"]) if row["investors_json"] else []
    raised = row["capital_raised_total"] or 0
    rounds = row["funding_rounds"] or 0
    hard = None
    if inv or raised > 0 or rounds > 0 or (row["ownership_type"] in ("pe_backed", "vc_backed")):
        c.value, c.confidence, c.status = 0.0, 0.8, "scored"
        c.evidence_text = f"investors={inv[:3]} raised={raised} rounds={rounds}"
        hard = "institutional capital on record"
        return c, hard
    if llm_boot:
        c.value, c.confidence, c.status = 5.0, 0.7, "scored"
        c.evidence_text, c.evidence_ref = llm_boot, "llm:evidence"
        return c, None
    inven_row = (extras or {}).get("row", {})
    if inven_row and any(k in inven_row for k in ("investors", "total_funding", "num_funding_rounds")):
        c.value, c.confidence, c.status = 4.0, 0.4, "scored"
        c.evidence_text = "no funding rows or investors returned by Inven (absence of evidence)"
        c.evidence_ref = "inven:funding"
        c.is_estimate = True
    else:
        c.next_source = "Inven funding columns not returned; Apollo org enrich (funding events)"
    return c, None


def _founder_tenure(row: sqlite3.Row, w: float) -> Component:
    c = Component("transactability", "founder_tenure", w)
    yrs = _years_since(row["year_founded"])
    founder_led = row["ownership_type"] in ("founder", "family") or bool(row["founder_active"])
    tenure = row["founder_tenure_years"] or (yrs if founder_led else None)
    if tenure is None:
        c.next_source = "Apollo people search (founder title tenure) / WebSearch founder query"
        return c
    val = 5 if tenure >= 15 else 4 if tenure >= 10 else 3 if tenure >= 5 else 2
    c.value, c.status = float(val), "scored"
    c.confidence = 0.8 if row["founder_tenure_years"] else 0.55
    c.evidence_text = f"founder tenure ~{tenure:.0f} years ({'title tenure' if row['founder_tenure_years'] else 'from founded year'})"
    return c


def _ownership_duration(row: sqlite3.Row, w: float) -> Component:
    c = Component("transactability", "ownership_duration", w)
    yrs = _years_since(row["year_founded"])
    changes = json.loads(row["ownership_changes_json"]) if row["ownership_changes_json"] else []
    if changes:
        try:
            last = max(int(str(ch.get("year") or ch)[:4]) for ch in changes)
            yrs = datetime.now(UTC).year - last
        except (ValueError, TypeError, AttributeError):
            pass
    if yrs is None:
        c.next_source = "founded year (Apollo org enrich)"
        return c
    val = 5 if yrs >= 20 else 4 if yrs >= 15 else 3 if yrs >= 10 else 2 if yrs >= 5 else 1
    c.value, c.confidence, c.status = float(val), 0.7, "scored"
    c.evidence_text = f"{yrs:.0f} years since founding or last ownership change"
    return c


def _size_fit(row: sqlite3.Row, thesis: Thesis, vertical: Vertical, w: float) -> Component:
    c = Component("transactability", "size_fit", w)
    emp_band = thesis.size.get("employees") or [25, 250]
    rev_band = thesis.size.get("revenue_usd") or [5e6, 50e6]
    emp = row["employee_count"]
    rev = row["est_revenue_low"]
    basis = row["revenue_basis"] or "unknown"
    if rev is None and emp is not None:
        rpe = thesis.prior(vertical, "rev_per_employee_usd", 180000)
        rev, basis, c.is_estimate = emp * rpe, "inferred", True
    if emp is None and rev is None:
        c.next_source = "Apollo org enrich (employee count)"
        return c
    scores = []
    if emp is not None:
        lo, hi = emp_band
        mid_lo, mid_hi = lo + (hi - lo) * 0.2, hi - (hi - lo) * 0.2
        scores.append(5 if mid_lo <= emp <= mid_hi else 4 if lo <= emp <= hi else 0)
    if rev is not None:
        lo, hi = rev_band
        scores.append(5 if lo * 1.5 <= rev <= hi * 0.8 else 4 if lo <= rev <= hi else 1)
    c.value, c.status = float(min(scores)), "scored"
    c.confidence = {"registry": 0.9, "public_reported": 0.9, "estimate": 0.6, "inferred": 0.4}.get(basis, 0.5)
    c.evidence_text = f"employees={emp}, revenue≈{rev:,.0f} ({basis})" if rev is not None else f"employees={emp}"
    return c


def _acquisition_history(row: sqlite3.Row, w: float) -> Component:
    c = Component("transactability", "limited_acquisition_history", w)
    acq = json.loads(row["acquisition_history_json"]) if row["acquisition_history_json"] else {}
    n = acq.get("acquisitions_count")
    if n is None:
        c.next_source = "Inven transactions columns"
        return c
    val = 5 if n == 0 else 3 if n <= 2 else 1
    c.value, c.confidence, c.status = float(val), 0.7, "scored"
    c.evidence_text = f"acquisitions_count={n}"
    return c


def _ownership_complexity(extras: dict[str, Any], w: float) -> Component:
    c = Component("transactability", "ownership_complexity", w)
    owners = (extras or {}).get("current_owners") or []
    if not owners:
        c.next_source = "registry ownership / WebSearch owner query"
        return c
    n = len(owners)
    val = 5 if n == 1 else 4 if n == 2 else 2
    c.value, c.confidence, c.status = float(val), 0.6, "scored"
    c.evidence_text = f"{n} named owner(s)"
    return c


def _openness(conn: sqlite3.Connection, company_id: int, w: float) -> Component:
    c = Component("transactability", "openness_evidence", w)
    enrolled = conn.execute("SELECT COUNT(*) FROM enrollments WHERE company_id = ?", (company_id,)).fetchone()[0]
    if not enrolled:
        c.status = "missing_not_applicable"
        return c
    pos = conn.execute(
        "SELECT COUNT(*) FROM replies r JOIN enrollments e ON e.id = r.enrollment_id WHERE e.company_id = ? AND r.final_class IN ('Interested','Open to conversation','Referral','Follow up in X months')",
        (company_id,),
    ).fetchone()[0]
    accepted = conn.execute(
        "SELECT COUNT(*) FROM engagement_events ev JOIN enrollments e ON e.id = ev.enrollment_id WHERE e.company_id = ? AND ev.event_type = 'linkedinInviteAccepted'",
        (company_id,),
    ).fetchone()[0]
    val = 5 if pos else 3 if accepted else 2
    c.value, c.confidence, c.status = float(val), 0.8, "scored"
    c.evidence_text = f"positive replies={pos}, invites accepted={accepted}"
    return c


def _constant(axis: str, name: str, value: float, w: float) -> Component:
    return Component(axis, name, w, value=float(value), confidence=0.7, status="thesis_constant",
                     evidence_text="thesis-level prior", evidence_ref="thesis")


# ---------------------------------------------------------------- assembly

def deterministic_components(conn: sqlite3.Connection, row: sqlite3.Row, extras: dict[str, Any], thesis: Thesis,
                             vertical: Vertical, weights: dict[str, Any], llm_boot: str | None = None
                             ) -> tuple[list[Component], list[str]]:
    A, T = weights["attractiveness"], weights["transactability"]
    hard: list[str] = []
    comps: list[Component] = [
        _constant("attractiveness", "industry_fit", thesis.prior(vertical, "industry", 4), A["industry_fit"]["weight"]),
        _growth_component(row, A["growth"]["weight"]),
        _constant("attractiveness", "fragmentation", thesis.prior(vertical, "fragmentation", 4), A["fragmentation"]["weight"]),
        _margins_component(row, extras, A["margins"]["weight"]),
        _constant("attractiveness", "tailwinds", thesis.prior(vertical, "tailwinds", 4), A["tailwinds"]["weight"]),
    ]
    fo, h1 = _founder_ownership(row, T["founder_ownership"]["weight"])
    nic, h2 = _no_institutional_capital(row, extras, llm_boot, T["no_institutional_capital"]["weight"])
    hard += [h for h in (h1, h2) if h]
    comps += [fo, _founder_tenure(row, T["founder_tenure"]["weight"]), nic,
              _ownership_duration(row, T["ownership_duration"]["weight"]),
              _size_fit(row, thesis, vertical, T["size_fit"]["weight"]),
              _acquisition_history(row, T["limited_acquisition_history"]["weight"]),
              _ownership_complexity(extras, T["ownership_complexity"]["weight"]),
              _openness(conn, row["id"], T["openness_evidence"]["weight"])]
    # LLM components start as missing; merge_llm fills them.
    for name, spec in A.items():
        if spec["kind"] == "llm":
            comps.append(Component("attractiveness", name, spec["weight"], next_source="Claude read of description/signals"))
    for name, spec in T.items():
        if spec["kind"] == "llm":
            comps.append(Component("transactability", name, spec["weight"], next_source="Claude read of description/signals/news"))
    return comps, hard


def merge_llm(comps: list[Component], llm: dict[str, dict[str, Any]], material: str) -> list[Component]:
    """Apply LLM judgements with the evidence-substring guard. A quote not in the material forces null."""
    norm_material = " ".join(material.lower().split())
    out = []
    for c in comps:
        j = llm.get(c.name)
        if j is None or c.status == "thesis_constant":
            out.append(c)
            continue
        val, conf, quote = j.get("value"), j.get("confidence"), j.get("evidence_quote")
        if val is None or not quote:
            c.status, c.value, c.confidence = "missing_no_data", None, None
            c.next_source = c.next_source or "no evidence in available material"
            out.append(c)
            continue
        q = " ".join(str(quote).lower().split())
        if q not in norm_material:
            c.status, c.value, c.confidence = "missing_no_data", None, None
            c.next_source = "LLM quote not found verbatim in material (rejected)"
            out.append(c)
            continue
        c.value = float(max(0, min(5, val)))
        c.confidence = float(min(0.6, conf if conf is not None else 0.5))
        c.evidence_text, c.evidence_ref, c.status = str(quote)[:400], "llm:material", "scored"
        out.append(c)
    return out


def _axis(comps: list[Component], axis: str) -> tuple[float | None, float, float]:
    """Return (score, confidence, coverage_pct) for one axis."""
    ax = [c for c in comps if c.axis == axis]
    scored = [c for c in ax if c.scored()]
    num = sum(c.weight * (c.value / 5.0) * c.confidence for c in scored)
    den = sum(c.weight * c.confidence for c in scored)
    score = 100.0 * num / den if den > 0 else None
    specific = [c for c in ax if c.status != "thesis_constant"]
    applicable = [c for c in specific if c.status != "missing_not_applicable"]
    covered = [c for c in applicable if c.status == "scored"]
    cov_w = sum(c.weight for c in applicable)
    coverage = 100.0 * sum(c.weight for c in covered) / cov_w if cov_w else 0.0
    conf_w = sum(c.weight for c in covered)
    mean_conf = sum(c.weight * c.confidence for c in covered) / conf_w if conf_w else 0.0
    return (round(score, 1) if score is not None else None, round((coverage / 100.0) * mean_conf, 3), round(coverage, 1))


def aggregate(comps: list[Component], hard: list[str], weights: dict[str, Any], ownership_conf: str | None) -> ScoreResult:
    tiers = weights["tiers"]
    A, A_conf, A_cov = _axis(comps, "attractiveness")
    T, T_conf, T_cov = _axis(comps, "transactability")
    a_specific_w = sum(c.weight for c in comps if c.axis == "attractiveness" and c.status not in ("thesis_constant", "missing_not_applicable"))
    t_specific_w = sum(c.weight for c in comps if c.axis == "transactability" and c.status not in ("thesis_constant", "missing_not_applicable"))
    coverage = (A_cov * a_specific_w + T_cov * t_specific_w) / (a_specific_w + t_specific_w) if (a_specific_w + t_specific_w) else 0.0
    vertex = None
    if A is not None and T is not None:
        if weights.get("composite", "geometric") == "geometric":
            vertex = 100.0 * math.sqrt(max(A, 0) / 100.0 * max(T, 0) / 100.0)
        else:
            s = float(weights.get("attractiveness_share", 0.45))
            vertex = s * A + (1 - s) * T
    vconf = min(A_conf, T_conf)
    band = round(float(tiers.get("band_k", 25)) * (1 - vconf)) if vertex is not None else None
    provisional = coverage < tiers["provisional_coverage"] or vconf < tiers["provisional_conf"]
    gaps = {c.name: c.next_source or "unknown" for c in comps if c.status == "missing_no_data"}
    if hard:
        tier = "Exclude"
    elif vertex is None:
        tier = "Hold"
    elif (vertex >= tiers["t1_min"] and vconf >= tiers["t1_min_conf"] and coverage >= tiers["t1_min_coverage"]
          and (ownership_conf in ("high", "med"))):
        tier = "T1"
    elif vertex >= tiers["t2_min"]:
        tier = "T2"
    elif vertex >= tiers["t3_min"]:
        tier = "T3"
    else:
        tier = "Hold"
    return ScoreResult(
        attractiveness=A, attractiveness_conf=A_conf, transactability=T, transactability_conf=T_conf,
        vertex_score=round(vertex, 1) if vertex is not None else None, vertex_conf=round(vconf, 3),
        band_low=round(max(0.0, vertex - band), 1) if vertex is not None else None,
        band_high=round(min(100.0, vertex + band), 1) if vertex is not None else None,
        coverage_pct=round(coverage, 1), provisional=provisional, tier=tier,
        hard_exclusion_reason="; ".join(hard) or None, data_gaps=gaps, components=comps,
    )


def persist(conn: sqlite3.Connection, company_id: int, thesis_id: int, res: ScoreResult, weights_version: str,
            prompt_hash: str | None, scored_by: str = "engine") -> int:
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        sid = _persist(conn, company_id, thesis_id, res, weights_version, prompt_hash, scored_by)
        if own_txn:
            conn.execute("COMMIT")
        return sid
    except Exception:
        if own_txn:
            conn.execute("ROLLBACK")
        raise


def _persist(conn: sqlite3.Connection, company_id: int, thesis_id: int, res: ScoreResult, weights_version: str,
             prompt_hash: str | None, scored_by: str) -> int:
    cur = conn.execute(
        "INSERT INTO scores(company_id, thesis_id, weights_version, prompt_hash, attractiveness, attractiveness_conf, transactability, "
        "transactability_conf, vertex_score, vertex_conf, band_low, band_high, coverage_pct, provisional, tier, hard_exclusion_reason, "
        "data_gaps_json, scored_at, scored_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (company_id, thesis_id, weights_version, prompt_hash, res.attractiveness, res.attractiveness_conf, res.transactability,
         res.transactability_conf, res.vertex_score, res.vertex_conf, res.band_low, res.band_high, res.coverage_pct,
         int(res.provisional), res.tier, res.hard_exclusion_reason, json.dumps(res.data_gaps), utcnow(), scored_by),
    )
    sid = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO score_components(score_id, axis, component, weight, value, confidence, evidence_text, evidence_source_ref, status, next_source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(sid, c.axis, c.name, c.weight, c.value, c.confidence, c.evidence_text, c.evidence_ref, c.status, c.next_source)
         for c in res.components],
    )
    return sid


def as_dict(res: ScoreResult) -> dict[str, Any]:
    d = asdict(res)
    d["components"] = [asdict(c) for c in res.components]
    return d

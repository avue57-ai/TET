"""Hard screening rules. A hard rule always wins over a score. Unknown never triggers an exclusion by itself."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from vertex.core import pipeline
from vertex.core.thesis import Thesis

CORPORATE_PATTERNS = re.compile(
    r"\b(a|an) (division|subsidiary|unit|brand|business) of\b|\bwholly[- ]owned\b|\bportfolio company\b|"
    r"\bbacked by\b|\bacquired by\b|\bpart of the .* (group|family of companies)\b|\bmerged with\b",
    re.IGNORECASE,
)
ESOP_PATTERNS = re.compile(r"\besop\b|employee[- ]owned", re.IGNORECASE)


@dataclass
class ScreenResult:
    company_id: int
    outcome: str                       # screened | excluded | held
    reasons: list[str] = field(default_factory=list)
    track: str = "founder"


NON_INSTITUTIONAL = re.compile(
    r"small business administration|\bsba\b|grant|national science foundation|\bnsf\b|\bnih\b|department of|"
    r"economic development|state of |county|kickstarter|crowdfund|friends and family|angel", re.IGNORECASE)


def _investors(row: sqlite3.Row) -> list[str]:
    """Investor names that indicate institutional equity (SBA loans, grants, angels are not institutional equity)."""
    try:
        v = json.loads(row["investors_json"]) if row["investors_json"] else []
    except json.JSONDecodeError:
        v = []
    return [str(x) for x in v if x and not NON_INSTITUTIONAL.search(str(x))]


def screen_company(row: sqlite3.Row, thesis: Thesis, consolidators: list[str]) -> ScreenResult:
    reasons: list[str] = []
    track = "founder"
    own = row["ownership_type"] or "unknown"
    if own in ("pe_backed", "vc_backed"):
        reasons.append(f"institutional capital ({own})")
    if own == "public":
        reasons.append("publicly listed")
    if own == "corporate":
        reasons.append("subsidiary / corporate-owned")
    inv = _investors(row)
    raw_inv = json.loads(row["investors_json"]) if row["investors_json"] else []
    only_non_institutional = bool(raw_inv) and not inv
    if ((row["capital_raised_total"] or 0) > 0 or (row["funding_rounds"] or 0) > 0) and not only_non_institutional:
        reasons.append(f"funding on record (rounds={row['funding_rounds'] or 0}, raised={row['capital_raised_total'] or 0:.0f})")
    if inv:
        reasons.append(f"investors listed: {', '.join(inv[:3])}")
    desc = f"{row['description'] or ''} {row['ownership_detail'] or ''}"
    if CORPORATE_PATTERNS.search(desc):
        m = CORPORATE_PATTERNS.search(desc)
        reasons.append(f"description indicates non-independent ownership ('{m.group(0)}')")
    if ESOP_PATTERNS.search(desc) or own == "esop":
        track = "esop"
    name = (row["name"] or "").lower()
    for c in consolidators:
        cl = c.lower()
        if cl and (cl == name or f" {cl} " in f" {name} "):
            reasons.append(f"roll-up / consolidator match ({c})")
            break
    countries = thesis.geography.get("countries") or ["US"]
    if row["hq_country"]:
        hc = row["hq_country"].strip().lower()
        if countries == ["US"] and hc not in ("us", "usa", "united states", "united states of america"):
            reasons.append(f"outside geography ({row['hq_country']})")
    emp_band = thesis.size.get("employees") or [25, 250]
    emp = row["employee_count"]
    if emp is not None and not (emp_band[0] <= emp <= emp_band[1]):
        reasons.append(f"headcount {emp} outside band {emp_band[0]}-{emp_band[1]}")
    if thesis.founded_before and row["year_founded"] and row["year_founded"] >= thesis.founded_before:
        reasons.append(f"founded {row['year_founded']} (thesis requires before {thesis.founded_before})")
    outcome = "excluded" if reasons else "screened"
    return ScreenResult(row["id"], outcome, reasons, track)


def run_screen(conn: sqlite3.Connection, thesis_id: int, thesis: Thesis, only_new: bool = True) -> dict[str, Any]:
    consolidators: list[str] = []
    for v in thesis.verticals:
        consolidators += v.known_consolidators
    q = ("SELECT c.* FROM companies c JOIN thesis_companies tc ON tc.company_id = c.id AND tc.thesis_id = ? "
         + ("WHERE tc.stage = 'Identified'" if only_new else "WHERE tc.stage IN ('Identified','Screened','Excluded')"))
    rows = conn.execute(q, (thesis_id,)).fetchall()
    stats = {"checked": 0, "screened": 0, "excluded": 0, "esop_track": 0, "reasons": {}}
    for row in rows:
        res = screen_company(row, thesis, consolidators)
        stats["checked"] += 1
        conn.execute("UPDATE thesis_companies SET track = ? WHERE thesis_id = ? AND company_id = ?",
                     (res.track, thesis_id, row["id"]))
        if res.track == "esop":
            stats["esop_track"] += 1
        if res.outcome == "excluded":
            pipeline.move(conn, thesis_id, row["id"], "Excluded", "; ".join(res.reasons), actor="system", force=True)
            stats["excluded"] += 1
            for r in res.reasons:
                key = r.split(" (")[0]
                stats["reasons"][key] = stats["reasons"].get(key, 0) + 1
        else:
            cur = pipeline.current_stage(conn, thesis_id, row["id"])
            if cur in (None, "Identified", "Excluded"):
                pipeline.move(conn, thesis_id, row["id"], "Screened", "passed hard rules", actor="system", force=True)
            stats["screened"] += 1
    return stats

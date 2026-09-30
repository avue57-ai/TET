"""Inven integration: search planning (bridge jobs), interpretation-note halting, paging, and row ingest.

Tool names match the Inven MCP connector: build_company_search, build_columns, run_company_search, get_company_info.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from vertex.bridge.jobs import enqueue
from vertex.bridge.router import register
from vertex.core.dedupe import Candidate
from vertex.core.normalize import normalize_domain
from vertex.db.connection import utcnow
from vertex.db.repo import ensure_thesis_company, record_source, upsert_company
from vertex.settings import get_settings
from vertex.utils.logging import get_logger

log = get_logger("inven")

# Discovery column pack: lean on purpose (bridge mode transcribes every page). Rich pack is used for accepted targets.
INVEN_COLUMNS = [
    "company", "website", "description", "industry", "keywords", "founded", "country", "region", "locality",
    "size", "linkedin_employees_count", "linkedin_url", "headcount_growth_12month", "headcount_growth_24month",
    "latest_revenue", "ownership_type", "ownership_details", "current_owners", "investors", "total_funding",
    "num_funding_rounds", "last_funding_type", "acquisitions_count", "traffic_growth_12month",
]
COLUMNS_DESCRIPTION = (
    "Company name, website, description, industry, keywords, founded year, country, region, locality, size band, "
    "LinkedIn employee count, LinkedIn URL, headcount growth over 12 and 24 months, latest revenue with basis, "
    "ownership type, ownership details, current owners, investors, total funding, number of funding rounds, "
    "last funding type, acquisitions count, website traffic growth over 12 months. No news, no directors, "
    "no physical locations, no registry financials."
)
RICH_COLUMNS_DESCRIPTION = (
    "Company overview and financials: description, keywords, founded year, headquarters, physical locations, "
    "LinkedIn employee count and headcount growth over 12, 24 and 36 months, latest revenue with basis and fiscal year, "
    "EBITDA and EBITDA margin where available, ownership type and details, ownership percentage, current owners and "
    "directors, investors, total funding and funding rounds, last funding type and date, acquisitions and transactions "
    "in the last 60 months, recent news, Google Maps review average and count, website traffic growth and yearly visits, "
    "legal entity name and registry code."
)

INSTITUTIONAL_PATTERNS = re.compile(
    r"private equity|venture|\bvc\b|growth equity|search fund|family office|capital partners|\bpartners\b|"
    r"holdings?\b.*(portfolio|acquired)|portfolio company|backed by|invest(ed|ment) (from|by)|sponsor",
    re.IGNORECASE,
)
OWNERSHIP_MAP = [
    (re.compile(r"private equity|pe[- ]backed|buyout", re.I), "pe_backed"),
    (re.compile(r"venture|vc[- ]backed|seed|series [a-z]", re.I), "vc_backed"),
    (re.compile(r"public|listed|stock exchange", re.I), "public"),
    (re.compile(r"subsidiar|corporate|division|acquired|owned by .*(inc|corp|group|plc)", re.I), "corporate"),
    (re.compile(r"esop|employee[- ]owned", re.I), "esop"),
    (re.compile(r"founder|family|owner[- ]operated|privately held|private company|independent|bootstrapped", re.I), "founder"),
]


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("results", "rows", "companies", "data", "items"):
            v = payload.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def _text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, default=str)
    s = str(v).strip()
    return s or None


def _list(v: Any) -> list[Any]:
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        return [s.strip() for s in re.split(r"[;,|]", v) if s.strip()]
    return [v]


INVEN_OWNERSHIP_CODES = {
    "private_unknown": ("unknown", None), "private": ("unknown", None), "family_owned": ("family", "med"),
    "founder_owned": ("founder", "med"), "management_owned": ("management", "med"), "employee_owned": ("esop", "med"),
    "pe_backed": ("pe_backed", "med"), "private_equity": ("pe_backed", "med"), "vc_backed": ("vc_backed", "med"),
    "venture_backed": ("vc_backed", "med"), "public": ("public", "high"), "listed": ("public", "high"),
    "subsidiary": ("corporate", "med"), "corporate": ("corporate", "med"),
}


def map_ownership(ownership_type: Any, ownership_details: Any, investors: Any) -> tuple[str, str | None, str | None]:
    """Return (our_enum, confidence, detail) from Inven ownership fields. Unknown stays unknown."""
    code = (_text(ownership_type) or "").strip().lower()
    raw = " ".join(x for x in (_text(ownership_type) or "", _text(ownership_details) or "") if x)
    inv = _list(investors)
    if code in INVEN_OWNERSHIP_CODES:
        val, conf = INVEN_OWNERSHIP_CODES[code]
        if inv and val in ("unknown", "family", "founder", "management") and any(INSTITUTIONAL_PATTERNS.search(_text(i) or "") for i in inv):
            return "pe_backed", "med", f"investors: {', '.join(_text(i) or '' for i in inv[:5])}"
        detail = _text(ownership_details) or (f"inven ownership_type={code}" if val != "unknown" else None)
        return val, conf, detail
    if inv and any(INSTITUTIONAL_PATTERNS.search(_text(i) or "") for i in inv):
        return "pe_backed", "med", f"investors: {', '.join(_text(i) or '' for i in inv[:5])}"
    for pat, val in OWNERSHIP_MAP:
        if raw and pat.search(raw):
            conf = "med" if val == "founder" else "med"
            return val, conf, raw[:500] or None
    if inv:
        return "unknown", "low", f"investors listed: {', '.join(_text(i) or '' for i in inv[:5])}"
    return "unknown", None, raw[:500] or None


def map_row(row: dict[str, Any]) -> tuple[Candidate, dict[str, Any], dict[str, Any]]:
    """Map an Inven result row to (Candidate, company fields, extras kept for scoring/personalization)."""
    domain = row.get("domain") or row.get("website")
    name = row.get("company") or row.get("name")
    state = row.get("region")
    linkedin = row.get("linkedin_url")
    cand = Candidate(domain=domain, name=_text(name), state=_text(state), linkedin_url=_text(linkedin),
                     inven_id=_text(row.get("inven_url")))
    own, own_conf, own_detail = map_ownership(row.get("ownership_type"), row.get("ownership_details"), row.get("investors"))
    growth = {k: _num(row.get(k)) for k in (
        "headcount_growth_6month", "headcount_growth_12month", "headcount_growth_24month",
        "headcount_growth_36month_cagr", "traffic_growth_12month", "year_total_visits") if row.get(k) is not None}
    acq = {k: _num(row.get(k)) for k in (
        "acquisitions_count", "acquisitions_last_60_months", "transactions_count", "transactions_last_60_months")
        if row.get(k) is not None}
    if row.get("transaction_type_percentages") is not None:
        acq["transaction_type_percentages"] = row.get("transaction_type_percentages")
    revenue = _num(row.get("latest_revenue"))
    basis_raw = _text(row.get("latest_revenue_basis")) or ""
    basis = "registry" if basis_raw.startswith("registry") else "public_reported" if basis_raw == "public_reported" \
        else "estimate" if revenue is not None else "unknown"
    fields: dict[str, Any] = {
        "name": _text(name),
        "legal_name": _text(row.get("legal_entity_name")),
        "website": _text(row.get("website")),
        "hq_city": _text(row.get("locality")),
        "hq_state": _text(state),
        "hq_country": _text(row.get("country")),
        "industry": _text(row.get("industry")),
        "description": _text(row.get("description")),
        "ownership_type": own,
        "ownership_confidence": own_conf,
        "ownership_detail": own_detail,
        "est_revenue_low": revenue,
        "est_revenue_high": revenue,
        "revenue_basis": basis,
        "revenue_fiscal_year": _int(row.get("latest_revenue_fiscal_year")),
        "employee_count": _int(row.get("linkedin_employees_count")),
        "employee_source": "linkedin" if row.get("linkedin_employees_count") is not None else None,
        "year_founded": _int(row.get("founded")),
        "growth_json": growth or None,
        "acquisition_history_json": acq or None,
        "capital_raised_total": _num(row.get("total_funding")),
        "funding_rounds": _int(row.get("num_funding_rounds")),
        "last_funding_date": _text(row.get("last_funding_date")),
        "last_funding_type": _text(row.get("last_funding_type")),
        "investors_json": _list(row.get("investors")) or None,
        "linkedin_url": _text(linkedin),
        "inven_url": _text(row.get("inven_url")),
    }
    owners = _list(row.get("current_owners")) or _list(row.get("directors"))
    if owners:
        first = owners[0]
        if isinstance(first, dict):
            fields["founder_name"] = _text(first.get("name"))
            fields["founder_title"] = _text(first.get("title") or first.get("role"))
        else:
            fields["founder_name"] = _text(first)
    extras = {
        "keywords": _list(row.get("keywords")),
        "news": row.get("news"),
        "size": _text(row.get("size")),
        "physical_locations": row.get("physical_locations"),
        "gmaps_reviews_average": _num(row.get("gmaps_reviews_average")),
        "total_gmaps_review_count": _int(row.get("total_gmaps_review_count")),
        "current_owners": _list(row.get("current_owners")),
        "directors": _list(row.get("directors")),
        "ownership_percentage": row.get("ownership_percentage"),
        "relevance": _num(row.get("relevance") or row.get("relevance_score")),
    }
    return cand, fields, extras


def keeper_proxy(fields: dict[str, Any], employees_band: tuple[int, int], consolidators: list[str]) -> bool:
    if fields.get("ownership_type") in ("pe_backed", "vc_backed", "public", "corporate"):
        return False
    if (fields.get("capital_raised_total") or 0) > 0 or (fields.get("funding_rounds") or 0) > 0:
        return False
    emp = fields.get("employee_count")
    if emp is not None and not (employees_band[0] <= emp <= employees_band[1]):
        return False
    name = (fields.get("name") or "").lower()
    if any(c.lower() in name for c in consolidators if c):
        return False
    return True


# ---------------------------------------------------------------- planning

def plan_build_search(conn: sqlite3.Connection, thesis_id: int, source_query_id: int, description: str,
                      exclude_domains: list[str]) -> int:
    args: dict[str, Any] = {"description": description, "save": True}
    if exclude_domains:
        args["exclude_domains"] = exclude_domains[:2000]
    return enqueue(conn, "inven", "build_company_search", args, "inven.build_search",
                   batch_key=f"thesis:{thesis_id}", context={"thesis_id": thesis_id, "source_query_id": source_query_id},
                   est_credits=2, credit_type="inven_ai")


def plan_build_columns(conn: sqlite3.Connection, thesis_id: int, source_query_id: int) -> int:
    return enqueue(conn, "inven", "build_columns", {"entity": "company", "description": COLUMNS_DESCRIPTION},
                   "inven.build_columns", batch_key=f"thesis:{thesis_id}",
                   context={"thesis_id": thesis_id, "source_query_id": source_query_id}, est_credits=3,
                   credit_type="inven_ai")


def plan_run_search(conn: sqlite3.Connection, thesis_id: int, source_query_id: int, search_id: str,
                    column_selection_id: str, offset: int, limit: int) -> int:
    return enqueue(conn, "inven", "run_company_search",
                   {"search_id": search_id, "column_selection_id": column_selection_id, "limit": limit, "offset": offset},
                   "inven.search_rows", batch_key=f"thesis:{thesis_id}",
                   context={"thesis_id": thesis_id, "source_query_id": source_query_id, "offset": offset, "limit": limit},
                   est_credits=limit, credit_type="inven_export")


def plan_company_info(conn: sqlite3.Connection, domains: list[str], purpose_note: str = "enrichment") -> int:
    return enqueue(conn, "inven", "get_company_info",
                   {"companies": domains[:100], "data_description": RICH_COLUMNS_DESCRIPTION},
                   "inven.company_info", context={"domains": domains[:100], "note": purpose_note},
                   est_credits=len(domains[:100]), credit_type="inven_export")


def known_column_selection(conn: sqlite3.Connection, thesis_id: int) -> str | None:
    row = conn.execute(
        "SELECT column_selection_id FROM source_queries WHERE thesis_id = ? AND column_selection_id IS NOT NULL ORDER BY id DESC LIMIT 1",
        (thesis_id,),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- handlers

def _notes_list(notes: Any) -> list[str]:
    if notes is None:
        return []
    if isinstance(notes, str):
        return [notes]
    if isinstance(notes, list):
        return [n if isinstance(n, str) else json.dumps(n, default=str) for n in notes]
    if isinstance(notes, dict):
        return [f"{k}: {v}" for k, v in notes.items()]
    return [str(notes)]


def _halting_note(notes: list[str]) -> str | None:
    """A dropped size or geography criterion halts the query; dropped ownership is expected."""
    for n in notes:
        low = n.lower()
        if "missing_criterion" in low or "clarification_needed" in low:
            if any(k in low for k in ("employee", "headcount", "revenue", "size", "country", "united states", "geograph", "location", "state")):
                return n
    return None


@register("inven.build_search")
def ingest_build_search(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, **kw: Any) -> dict[str, Any]:
    sq_id = job["context"]["source_query_id"]
    thesis_id = job["context"]["thesis_id"]
    search_id = payload.get("search_id") if isinstance(payload, dict) else None
    notes = _notes_list(payload.get("interpretation_notes")) if isinstance(payload, dict) else []
    est = _int(payload.get("estimated_total_results")) if isinstance(payload, dict) else None
    halt = _halting_note(notes)
    conn.execute(
        "UPDATE source_queries SET inven_search_id = ?, interpretation_notes_json = ?, estimated_total = ?, status = ?, halted_reason = ? WHERE id = ?",
        (search_id, json.dumps(notes), est, "halted" if halt else "built", halt, sq_id),
    )
    if halt:
        from vertex.db.repo import add_review_item
        add_review_item(conn, "exception", "source_queries", sq_id, None,
                        {"kind": "inven_query_halted", "note": halt, "source_query_id": sq_id}, priority=2)
        log.warning(f"source_query {sq_id} halted: {halt[:160]}")
        return {"rows": 0, "halted": True}
    if not search_id:
        raise ValueError("build_company_search payload has no search_id")
    settings = get_settings()
    col = known_column_selection(conn, thesis_id)
    if col:
        limit = int(settings.bridge.get("inven_page_limit", 50)) if settings.backend_for("inven") == "bridge" else 200
        plan_run_search(conn, thesis_id, sq_id, search_id, col, 0, limit)
    else:
        plan_build_columns(conn, thesis_id, sq_id)
    return {"rows": 0, "search_id": search_id, "estimated_total": est}


@register("inven.build_columns")
def ingest_build_columns(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, **kw: Any) -> dict[str, Any]:
    thesis_id = job["context"]["thesis_id"]
    col = payload.get("column_selection_id") if isinstance(payload, dict) else None
    if not col:
        raise ValueError("build_columns payload has no column_selection_id")
    conn.execute("UPDATE source_queries SET column_selection_id = ? WHERE thesis_id = ? AND column_selection_id IS NULL",
                 (col, thesis_id))
    settings = get_settings()
    limit = int(settings.bridge.get("inven_page_limit", 50)) if settings.backend_for("inven") == "bridge" else 200
    n = 0
    for r in conn.execute(
        "SELECT id, inven_search_id FROM source_queries WHERE thesis_id = ? AND status = 'built' AND inven_search_id IS NOT NULL",
        (thesis_id,),
    ).fetchall():
        plan_run_search(conn, thesis_id, r["id"], r["inven_search_id"], col, 0, limit)
        n += 1
    return {"rows": 0, "queued_searches": n}


def _thesis_cfg(conn: sqlite3.Connection, thesis_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT config_json FROM theses WHERE id = ?", (thesis_id,)).fetchone()
    return json.loads(row[0]) if row else {}


@register("inven.search_rows")
def ingest_search_rows(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None,
                       **kw: Any) -> dict[str, Any]:
    ctx = job["context"]
    thesis_id, sq_id = ctx["thesis_id"], ctx["source_query_id"]
    offset, limit = int(ctx.get("offset", 0)), int(ctx.get("limit", 50))
    rows = _rows(payload)
    cfg = _thesis_cfg(conn, thesis_id)
    band = tuple(cfg.get("size", {}).get("employees", [25, 250]))
    consolidators: list[str] = []
    for v in cfg.get("verticals", []):
        consolidators += v.get("known_consolidators", [])
    sq = conn.execute("SELECT vertical FROM source_queries WHERE id = ?", (sq_id,)).fetchone()
    vertical = sq["vertical"] if sq else None
    inserted = updated = skipped = keepers = 0
    for row in rows:
        cand, fields, extras = map_row(row)
        if not normalize_domain(cand.domain):
            skipped += 1
            continue
        fields["vertical"] = vertical
        fields["source_query_id"] = sq_id
        fields["last_refreshed"] = utcnow()
        cid, created, _ = upsert_company(conn, cand, fields, source="inven", source_ref=fields.get("inven_url"),
                                         confidence=0.7, lead_source=f"inven:{vertical}")
        record_source(conn, "inven", fields.get("inven_url") or cand.domain, cid, {"row": row, "extras": extras},
                      batch_id=batch_id)
        ensure_thesis_company(conn, thesis_id, cid, sq_id, extras.get("relevance"))
        if keeper_proxy(fields, band, consolidators):
            keepers += 1
        inserted += int(created)
        updated += int(not created)
    conn.execute(
        "UPDATE source_queries SET rows_returned = rows_returned + ?, new_companies = new_companies + ?, status = 'running', run_at = ? WHERE id = ?",
        (len(rows), inserted, utcnow(), sq_id),
    )
    credits = payload.get("credits_used") if isinstance(payload, dict) else None
    if isinstance(credits, dict):
        from vertex.db.repo import add_credit
        if credits.get("export"):
            add_credit(conn, kw.get("run_id"), "inven", "inven_export", float(credits["export"]), f"job {job['id']}")
        if credits.get("ai"):
            add_credit(conn, kw.get("run_id"), "inven", "inven_ai", float(credits["ai"]), f"job {job['id']}")
    # paging decision
    settings = get_settings()
    cap = int(settings.budgets.inven_export_rows_per_run)
    total_so_far = conn.execute("SELECT rows_returned FROM source_queries WHERE id = ?", (sq_id,)).fetchone()[0]
    proxy_rate = keepers / len(rows) if rows else 0.0
    per_query_cap = int(cfg.get("max_rows_per_query", 600))
    if rows and len(rows) >= limit and proxy_rate >= 0.30 and total_so_far < min(cap, per_query_cap):
        sqrow = conn.execute("SELECT inven_search_id, column_selection_id FROM source_queries WHERE id = ?", (sq_id,)).fetchone()
        plan_run_search(conn, thesis_id, sq_id, sqrow["inven_search_id"], sqrow["column_selection_id"], offset + limit, limit)
    else:
        conn.execute("UPDATE source_queries SET status = 'done' WHERE id = ?", (sq_id,))
    return {"rows": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped,
            "keeper_proxy_rate": round(proxy_rate, 2)}


@register("inven.company_info")
def ingest_company_info(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None,
                        **kw: Any) -> dict[str, Any]:
    rows = _rows(payload)
    if not rows and isinstance(payload, dict) and isinstance(payload.get("companies"), list):
        rows = payload["companies"]
    inserted = updated = skipped = 0
    for row in rows:
        cand, fields, extras = map_row(row)
        if not normalize_domain(cand.domain):
            skipped += 1
            continue
        fields["last_refreshed"] = utcnow()
        cid, created, _ = upsert_company(conn, cand, fields, source="inven", source_ref=fields.get("inven_url"), confidence=0.7)
        record_source(conn, "inven", fields.get("inven_url") or cand.domain, cid, {"row": row, "extras": extras}, batch_id=batch_id)
        inserted += int(created)
        updated += int(not created)
    return {"rows": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def latest_inven_extras(conn: sqlite3.Connection, company_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_json FROM source_records WHERE company_id = ? AND source = 'inven' ORDER BY id DESC LIMIT 1",
        (company_id,),
    ).fetchone()
    if not row:
        return {}
    data = json.loads(row[0])
    return data.get("extras", {}) | {"row": data.get("row", {})}

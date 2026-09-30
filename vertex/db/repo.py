"""Repository layer: upserts with provenance, precedence and locked fields. All writes go through here."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from vertex.core.dedupe import Candidate, register_aliases, resolve
from vertex.core.normalize import normalize_domain, normalize_linkedin, normalize_name, normalize_state
from vertex.db.connection import today, utcnow

# Higher wins when a field is already set. Equal precedence = fresher value wins.
SOURCE_PRECEDENCE = {
    "human": 100,
    "registry": 80,
    "apollo_org": 60,
    "apollo_person": 60,
    "inven": 50,
    "lemlist": 40,
    "granola": 40,
    "llm": 30,
    "websearch": 20,
    "csv": 10,
}

COMPANY_FIELDS = {
    "name", "name_norm", "legal_name", "website", "hq_city", "hq_state", "hq_country", "region", "industry",
    "sub_industry", "vertical", "description", "products_services", "end_markets", "business_model",
    "ownership_type", "ownership_detail", "ownership_confidence", "est_revenue_low", "est_revenue_high",
    "revenue_basis", "revenue_fiscal_year", "est_ebitda", "ebitda_status", "employee_count", "employee_source",
    "year_founded", "recurring_revenue_type", "customer_concentration_status", "customer_concentration_note",
    "growth_json", "acquisition_history_json", "capital_raised_total", "funding_rounds", "last_funding_date",
    "last_funding_type", "investors_json", "ownership_changes_json", "founder_name", "founder_title",
    "founder_tenure_years", "founder_active", "linkedin_url", "inven_url", "apollo_org_id", "lead_source",
    "source_query_id", "last_refreshed", "notes",
}


def _json(v: Any) -> str | None:
    if v is None:
        return None
    return json.dumps(v, default=str, ensure_ascii=False)


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- companies

def get_company(conn: sqlite3.Connection, company_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()


def get_company_by_domain(conn: sqlite3.Connection, domain: str) -> sqlite3.Row | None:
    d = normalize_domain(domain)
    if not d:
        return None
    return conn.execute("SELECT * FROM companies WHERE domain = ?", (d,)).fetchone()


def upsert_company(
    conn: sqlite3.Connection,
    cand: Candidate,
    fields: dict[str, Any],
    source: str,
    source_ref: str | None = None,
    confidence: float | None = None,
    is_estimate: bool = False,
    lead_source: str | None = None,
) -> tuple[int, bool, list[str]]:
    """Resolve the candidate to a company (or create it), then apply fields under precedence rules.

    Returns (company_id, created, fields_written).
    """
    res = resolve(conn, cand)
    now = utcnow()
    clean: dict[str, Any] = {}
    for k, v in fields.items():
        if k in COMPANY_FIELDS and v is not None and v != "" and v != []:
            clean[k] = v
    if "name" in clean and "name_norm" not in clean:
        clean["name_norm"] = normalize_name(clean["name"])
    if "hq_state" in clean:
        clean["hq_state"] = normalize_state(clean["hq_state"]) or clean["hq_state"]
    if "linkedin_url" in clean:
        clean["linkedin_url"] = normalize_linkedin(clean["linkedin_url"]) or clean["linkedin_url"]
    for jk in ("growth_json", "acquisition_history_json", "investors_json", "ownership_changes_json"):
        if jk in clean and not isinstance(clean[jk], str):
            clean[jk] = _json(clean[jk])

    written: list[str] = []
    if res.company_id is None:
        domain = normalize_domain(cand.domain)
        if not domain:
            raise ValueError("cannot create a company without a normalizable domain")
        cols = ["domain", "date_discovered", "created_at", "updated_at", "lead_source"]
        vals: list[Any] = [domain, today(), now, now, lead_source or source]
        for k, v in clean.items():
            cols.append(k)
            vals.append(v)
        placeholders = ",".join("?" for _ in cols)
        cur = conn.execute(f"INSERT INTO companies({','.join(cols)}) VALUES ({placeholders})", vals)
        company_id = int(cur.lastrowid)
        written = list(clean.keys())
        created = True
    else:
        company_id = res.company_id
        row = get_company(conn, company_id)
        locked = set(json.loads(row["locked_fields_json"]) if row["locked_fields_json"] else [])
        prec = SOURCE_PRECEDENCE.get(source, 0)
        updates: dict[str, Any] = {}
        for k, v in clean.items():
            if k in locked:
                continue
            current = row[k]
            if current is None or current == "" or (k in ("ownership_type", "revenue_basis", "ebitda_status",
                                                            "recurring_revenue_type", "customer_concentration_status")
                                                      and current == "unknown"):
                updates[k] = v
                continue
            existing = conn.execute(
                "SELECT source FROM field_provenance WHERE company_id = ? AND field = ? ORDER BY id DESC LIMIT 1",
                (company_id, k),
            ).fetchone()
            existing_prec = SOURCE_PRECEDENCE.get(existing["source"], 0) if existing else 0
            if prec >= existing_prec and v != current:
                updates[k] = v
        if updates:
            updates["updated_at"] = now
            sets = ",".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE companies SET {sets} WHERE id = ?", [*updates.values(), company_id])
            written = [k for k in updates if k != "updated_at"]
        created = False

    register_aliases(conn, company_id, cand, reason=source)
    for k in written:
        if k == "name_norm":
            continue
        conn.execute(
            "INSERT INTO field_provenance(company_id, field, value_text, source, source_ref, confidence, is_estimate, observed_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (company_id, k, str(clean.get(k)), source, source_ref, confidence, int(is_estimate), now),
        )
    return company_id, created, written


def lock_fields(conn: sqlite3.Connection, company_id: int, fields: list[str]) -> None:
    row = get_company(conn, company_id)
    locked = set(json.loads(row["locked_fields_json"]) if row and row["locked_fields_json"] else [])
    locked.update(fields)
    conn.execute("UPDATE companies SET locked_fields_json = ?, updated_at = ? WHERE id = ?",
                 (_json(sorted(locked)), utcnow(), company_id))


def record_source(
    conn: sqlite3.Connection, source: str, source_id: str | None, company_id: int | None, payload: Any,
    batch_id: int | None = None,
) -> tuple[int | None, bool]:
    h = payload_hash(payload)
    cur = conn.execute(
        "INSERT OR IGNORE INTO source_records(source, source_id, company_id, payload_json, payload_hash, fetched_at, batch_id)"
        " VALUES (?,?,?,?,?,?,?)",
        (source, source_id, company_id, _json(payload), h, utcnow(), batch_id),
    )
    if cur.rowcount == 0:
        return None, False
    return int(cur.lastrowid), True


# ---------------------------------------------------------------- theses

def get_thesis(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM theses WHERE slug = ?", (slug,)).fetchone()


def upsert_thesis(conn: sqlite3.Connection, slug: str, name: str, config: dict[str, Any]) -> tuple[int, bool]:
    vh = payload_hash(config)[:16]
    now = utcnow()
    row = get_thesis(conn, slug)
    if row:
        conn.execute("UPDATE theses SET name = ?, version_hash = ?, config_json = ?, updated_at = ? WHERE id = ?",
                     (name, vh, _json(config), now, row["id"]))
        return int(row["id"]), False
    cur = conn.execute(
        "INSERT INTO theses(slug, name, version_hash, config_json, active, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
        (slug, name, vh, _json(config), now, now),
    )
    return int(cur.lastrowid), True


def ensure_thesis_company(
    conn: sqlite3.Connection, thesis_id: int, company_id: int, source_query_id: int | None = None,
    relevance: float | None = None,
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO thesis_companies(thesis_id, company_id, source_query_id, inven_relevance, stage, stage_updated_at, added_at)"
        " VALUES (?,?,?,?,'Identified',?,?)",
        (thesis_id, company_id, source_query_id, relevance, utcnow(), utcnow()),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------- contacts

def upsert_contact(conn: sqlite3.Connection, company_id: int, data: dict[str, Any]) -> tuple[int, bool]:
    now = utcnow()
    apollo_id = data.get("apollo_person_id")
    existing = None
    if apollo_id:
        existing = conn.execute("SELECT id FROM contacts WHERE apollo_person_id = ?", (apollo_id,)).fetchone()
    if existing is None and data.get("email"):
        existing = conn.execute("SELECT id FROM contacts WHERE company_id = ? AND email = ?",
                                (company_id, data["email"])).fetchone()
    if existing is None and data.get("linkedin_url"):
        existing = conn.execute("SELECT id FROM contacts WHERE company_id = ? AND linkedin_url = ?",
                                (company_id, data["linkedin_url"])).fetchone()
    cols = {k: v for k, v in data.items() if v is not None}
    if existing:
        cols["updated_at"] = now
        sets = ",".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE contacts SET {sets} WHERE id = ?", [*cols.values(), existing["id"]])
        return int(existing["id"]), False
    cols.update({"company_id": company_id, "created_at": now, "updated_at": now})
    placeholders = ",".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO contacts({','.join(cols)}) VALUES ({placeholders})", list(cols.values()))
    return int(cur.lastrowid), True


# ---------------------------------------------------------------- tasks, review, credits, runs

def add_task(conn: sqlite3.Connection, company_id: int | None, type_: str, due_date: str | None = None,
             contact_id: int | None = None, created_from: str | None = None, notes: str | None = None) -> int:
    dup = conn.execute(
        "SELECT id FROM tasks WHERE company_id IS ? AND type = ? AND status = 'open' AND created_from IS ?",
        (company_id, type_, created_from),
    ).fetchone()
    if dup:
        return int(dup["id"])
    cur = conn.execute(
        "INSERT INTO tasks(company_id, contact_id, type, due_date, status, created_from, notes, created_at)"
        " VALUES (?,?,?,?, 'open', ?,?,?)",
        (company_id, contact_id, type_, due_date, created_from, notes, utcnow()),
    )
    return int(cur.lastrowid)


def add_review_item(conn: sqlite3.Connection, item_type: str, ref_table: str | None, ref_id: int | None,
                    company_id: int | None, payload: dict[str, Any], priority: int = 3,
                    review_date: str | None = None) -> int:
    existing = conn.execute(
        "SELECT id FROM review_items WHERE item_type = ? AND ref_table IS ? AND ref_id IS ? AND status = 'pending'",
        (item_type, ref_table, ref_id),
    ).fetchone()
    if existing:
        conn.execute("UPDATE review_items SET payload_json = ?, priority = ? WHERE id = ?",
                     (_json(payload), priority, existing["id"]))
        return int(existing["id"])
    prefix = {"reply_action": "R", "enroll": "E", "accept_target": "T", "contact_choice": "C",
              "message": "M", "exception": "X", "weights_proposal": "W", "score_dispute": "S", "prescreen_gate": "P"}[item_type]
    n = conn.execute("SELECT COUNT(*) FROM review_items WHERE item_type = ?", (item_type,)).fetchone()[0] + 1
    cur = conn.execute(
        "INSERT INTO review_items(item_type, ref_table, ref_id, company_id, priority, short_id, payload_json, review_date, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
        (item_type, ref_table, ref_id, company_id, priority, f"{prefix}{n}", _json(payload), review_date or today(), utcnow()),
    )
    return int(cur.lastrowid)


def add_credit(conn: sqlite3.Connection, run_id: int | None, provider: str, credit_type: str, amount: float,
               note: str | None = None) -> None:
    conn.execute("INSERT INTO credit_ledger(run_id, provider, credit_type, amount, note, at) VALUES (?,?,?,?,?,?)",
                 (run_id, provider, credit_type, amount, note, utcnow()))


def start_run(conn: sqlite3.Connection, job: str, max_open_hours: float = 2.0) -> int:
    open_run = conn.execute(
        "SELECT id, job, started_at FROM runs WHERE status = 'running' AND started_at > datetime('now', ?)",
        (f"-{int(max_open_hours * 60)} minutes",),
    ).fetchone()
    if open_run:
        raise RuntimeError(f"run {open_run['id']} ({open_run['job']}) is still open since {open_run['started_at']}")
    cur = conn.execute("INSERT INTO runs(job, started_at, status) VALUES (?,?, 'running')", (job, utcnow()))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, summary: dict[str, Any] | None = None,
               error: str | None = None) -> None:
    conn.execute("UPDATE runs SET finished_at = ?, status = ?, summary_json = ?, error = ? WHERE id = ?",
                 (utcnow(), status, _json(summary), error, run_id))


def credits_used_today(conn: sqlite3.Connection, provider: str, credit_type: str | None = None) -> float:
    q = "SELECT COALESCE(SUM(amount),0) FROM credit_ledger WHERE provider = ? AND at >= date('now')"
    args: list[Any] = [provider]
    if credit_type:
        q += " AND credit_type = ?"
        args.append(credit_type)
    return float(conn.execute(q, args).fetchone()[0])

"""Dedup: resolve any candidate identity to one company row via the alias table, then fuzzy name+state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from vertex.core.normalize import name_state_key, normalize_domain, normalize_linkedin, normalize_name, normalize_state
from vertex.db.connection import utcnow

FUZZY_NAME_THRESHOLD = 92


@dataclass
class Candidate:
    domain: str | None = None
    name: str | None = None
    state: str | None = None
    linkedin_url: str | None = None
    inven_id: str | None = None
    apollo_id: str | None = None
    extra_domains: list[str] = field(default_factory=list)


@dataclass
class Resolution:
    company_id: int | None
    matched_by: str | None
    confidence: float


def _lookup_alias(conn: sqlite3.Connection, alias_type: str, value: str | None) -> int | None:
    if not value:
        return None
    row = conn.execute(
        "SELECT company_id FROM company_aliases WHERE alias_type = ? AND alias_value = ?", (alias_type, value)
    ).fetchone()
    return int(row[0]) if row else None


def resolve(conn: sqlite3.Connection, cand: Candidate) -> Resolution:
    """Cascade: domain -> alias domains -> linkedin -> inven/apollo ids -> exact name+state -> fuzzy name+state."""
    dom = normalize_domain(cand.domain)
    if dom:
        row = conn.execute("SELECT id FROM companies WHERE domain = ?", (dom,)).fetchone()
        if row:
            return Resolution(int(row[0]), "domain", 1.0)
        cid = _lookup_alias(conn, "domain", dom)
        if cid:
            return Resolution(cid, "alias_domain", 0.98)
    for extra in cand.extra_domains:
        ed = normalize_domain(extra)
        if ed:
            row = conn.execute("SELECT id FROM companies WHERE domain = ?", (ed,)).fetchone()
            if row:
                return Resolution(int(row[0]), "extra_domain", 0.97)
            cid = _lookup_alias(conn, "domain", ed)
            if cid:
                return Resolution(cid, "alias_domain", 0.97)
    li = normalize_linkedin(cand.linkedin_url)
    if li:
        cid = _lookup_alias(conn, "linkedin", li)
        if cid:
            return Resolution(cid, "linkedin", 0.95)
        row = conn.execute("SELECT id FROM companies WHERE linkedin_url = ?", (li,)).fetchone()
        if row:
            return Resolution(int(row[0]), "linkedin", 0.95)
    for alias_type, value in (("inven_id", cand.inven_id), ("apollo_id", cand.apollo_id)):
        cid = _lookup_alias(conn, alias_type, value)
        if cid:
            return Resolution(cid, alias_type, 0.95)
    key = name_state_key(cand.name, cand.state)
    if key:
        cid = _lookup_alias(conn, "name_state", key)
        if cid:
            return Resolution(cid, "name_state", 0.9)
        nn = normalize_name(cand.name)
        st = normalize_state(cand.state)
        if nn and st:
            rows = conn.execute(
                "SELECT id, name_norm FROM companies WHERE hq_state = ? AND name_norm IS NOT NULL", (st,)
            ).fetchall()
            best_id, best = None, 0.0
            for r in rows:
                score = fuzz.token_set_ratio(nn, r["name_norm"])
                if score > best:
                    best_id, best = int(r["id"]), score
            if best_id is not None and best >= FUZZY_NAME_THRESHOLD:
                return Resolution(best_id, "fuzzy_name_state", round(best / 100.0, 2))
    return Resolution(None, None, 0.0)


def add_alias(conn: sqlite3.Connection, company_id: int, alias_type: str, value: str | None, reason: str) -> bool:
    if not value:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO company_aliases(alias_type, alias_value, company_id, reason, created_at) VALUES (?,?,?,?,?)",
        (alias_type, value, company_id, reason, utcnow()),
    )
    return cur.rowcount > 0


def register_aliases(conn: sqlite3.Connection, company_id: int, cand: Candidate, reason: str) -> int:
    n = 0
    n += add_alias(conn, company_id, "domain", normalize_domain(cand.domain), reason)
    for extra in cand.extra_domains:
        n += add_alias(conn, company_id, "domain", normalize_domain(extra), reason)
    n += add_alias(conn, company_id, "linkedin", normalize_linkedin(cand.linkedin_url), reason)
    n += add_alias(conn, company_id, "inven_id", cand.inven_id, reason)
    n += add_alias(conn, company_id, "apollo_id", cand.apollo_id, reason)
    n += add_alias(conn, company_id, "name_state", name_state_key(cand.name, cand.state), reason)
    return n

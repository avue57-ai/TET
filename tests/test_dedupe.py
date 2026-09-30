"""Tests for alias-based dedup resolution and migrations."""

import sqlite3

import pytest

from vertex.core.dedupe import Candidate, register_aliases, resolve
from vertex.db.connection import migrate, utcnow


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"), isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    migrate(c)
    return c


def _insert_company(conn, domain, name, state, linkedin=None):
    from vertex.core.normalize import normalize_name

    now = utcnow()
    cur = conn.execute(
        "INSERT INTO companies(domain, name, name_norm, hq_state, linkedin_url, date_discovered, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (domain, name, normalize_name(name), state, linkedin, now, now, now),
    )
    return cur.lastrowid


def test_migrations_apply_once(conn):
    assert migrate(conn) == []  # already applied by fixture
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("companies", "contacts", "scores", "score_components", "bridge_jobs", "review_decisions", "suppression"):
        assert t in tables


def test_resolve_by_domain_alias_linkedin_and_fuzzy(conn):
    cid = _insert_company(conn, "acme.com", "Acme Compliance Software, Inc.", "OH", "https://www.linkedin.com/company/acme")
    register_aliases(conn, cid, Candidate(domain="acme.com", name="Acme Compliance Software", state="OH",
                                          linkedin_url="linkedin.com/company/acme", extra_domains=["acmecompliance.com"]), "test")
    assert resolve(conn, Candidate(domain="https://www.acme.com/x")).matched_by == "domain"
    assert resolve(conn, Candidate(domain="acmecompliance.com")).company_id == cid
    assert resolve(conn, Candidate(linkedin_url="https://linkedin.com/company/Acme/")).company_id == cid
    r = resolve(conn, Candidate(name="ACME Compliance Software LLC", state="Ohio"))
    assert r.company_id == cid and r.matched_by in ("name_state", "fuzzy_name_state")
    assert resolve(conn, Candidate(domain="other.com", name="Totally Different", state="OH")).company_id is None


def test_fuzzy_does_not_cross_states(conn):
    _insert_company(conn, "acme.com", "Acme Compliance Software", "OH")
    assert resolve(conn, Candidate(name="Acme Compliance Software", state="TX")).company_id is None

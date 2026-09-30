"""Suppression list: seed sources, upserts, lookups, and seed status used by the enrollment gates."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from vertex.core.normalize import normalize_domain, normalize_email, normalize_linkedin
from vertex.db.connection import utcnow
from vertex.settings import get_settings

PERMANENT_REASONS = {"unsubscribed", "remove_me", "manual", "portfolio", "pe_backed", "existing_relationship"}


def classify_value(value: str) -> tuple[str, str] | None:
    v = (value or "").strip()
    if not v:
        return None
    if "@" in v:
        e = normalize_email(v)
        return ("email", e) if e else None
    if "linkedin.com" in v.lower():
        li = normalize_linkedin(v)
        return ("linkedin", li) if li else None
    d = normalize_domain(v)
    return ("domain", d) if d else None


def add_suppression(conn: sqlite3.Connection, kind: str, value: str, reason: str, source: str,
                    expires_at: str | None = None, hold_for_human: bool = False) -> bool:
    """Upsert. A permanent row is never downgraded to an expiring one; a hold flag is sticky until a human clears it."""
    row = conn.execute("SELECT id, expires_at, hold_for_human, reason FROM suppression WHERE kind = ? AND value = ?", (kind, value)).fetchone()
    if row is None:
        conn.execute("INSERT INTO suppression(kind, value, reason, source, expires_at, hold_for_human, added_at) VALUES (?,?,?,?,?,?,?)",
                     (kind, value, reason, source, expires_at, int(hold_for_human), utcnow()))
        return True
    if row["expires_at"] is None and expires_at is not None:
        new_exp = None
    elif row["expires_at"] is not None and expires_at is not None:
        new_exp = max(row["expires_at"], expires_at)
    else:
        new_exp = None if (row["expires_at"] is None or expires_at is None) else expires_at
    conn.execute("UPDATE suppression SET expires_at = ?, hold_for_human = MAX(hold_for_human, ?), reason = CASE WHEN ? IN ('unsubscribed','remove_me','manual') THEN ? ELSE reason END, source = ? WHERE id = ?",
                 (new_exp, int(hold_for_human), reason, reason, source, row["id"]))
    return False


def cooldown(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def import_never_contact(conn: sqlite3.Connection, path: Path | None = None) -> dict[str, int]:
    path = path or Path(get_settings().config_dir) / "suppression" / "never_contact.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    n = 0
    for kind, key in (("domain", "domains"), ("email", "emails"), ("linkedin", "linkedin")):
        for v in data.get(key) or []:
            cv = classify_value(v)
            if cv:
                n += int(add_suppression(conn, cv[0], cv[1], "manual", "never_contact"))
    conn.execute("INSERT INTO ingest_batches(source, kind, file_path, file_sha256, rows, inserted, status, started_at, finished_at) VALUES ('never_contact','yaml',?,?,?,?,'done',?,?)",
                 (str(path), f"never_contact:{utcnow()}", n, n, utcnow(), utcnow()))
    return {"new": n}


def hits(conn: sqlite3.Connection, email: str | None = None, domain: str | None = None, linkedin: str | None = None) -> list[sqlite3.Row]:
    now = utcnow()
    out: list[sqlite3.Row] = []
    for kind, val in (("email", normalize_email(email) if email else None), ("domain", normalize_domain(domain) if domain else None),
                      ("linkedin", normalize_linkedin(linkedin) if linkedin else None)):
        if not val:
            continue
        out += conn.execute("SELECT * FROM suppression WHERE kind = ? AND value = ? AND (expires_at IS NULL OR expires_at > ?)", (kind, val, now)).fetchall()
    return out


def seed_status(conn: sqlite3.Connection) -> dict[str, Any]:
    by_source = {r["source"]: r["n"] for r in conn.execute("SELECT source, COUNT(*) AS n FROM suppression GROUP BY source")}
    by_reason = {r["reason"]: r["n"] for r in conn.execute("SELECT reason, COUNT(*) AS n FROM suppression GROUP BY reason")}
    jobs = {r["purpose"]: {"done": r["n"], "last": r["last"]} for r in conn.execute(
        "SELECT purpose, COUNT(*) AS n, MAX(completed_at) AS last FROM bridge_jobs WHERE status = 'done' AND (purpose LIKE 'lemlist.%' OR purpose LIKE 'apollo.crm%' OR purpose LIKE 'granola.%') GROUP BY purpose")}
    batches = {r["source"]: r["last"] for r in conn.execute("SELECT source, MAX(finished_at) AS last FROM ingest_batches WHERE status = 'done' GROUP BY source")}
    return {"by_source": by_source, "by_reason": by_reason, "jobs": jobs, "batches": batches,
            "existing_relationship_source_present": any(s in batches for s in ("never_contact", "apollo_crm", "granola")) or any(k.startswith("apollo.crm") or k.startswith("granola.") for k in jobs),
            "unsubscribes_synced_at": jobs.get("lemlist.unsubscribes", {}).get("last")}

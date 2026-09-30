"""Enrichment + contacts workflow: Apollo org enrich for screened/accepted companies, people search, selection, reveal."""

from __future__ import annotations

import sqlite3
from typing import Any

from vertex.core import pipeline
from vertex.db.repo import add_task
from vertex.integrations.apollo import plan_bulk_match, plan_org_enrich, plan_people_search, select_primary
from vertex.settings import get_settings
from vertex.utils.logging import get_logger

log = get_logger("enrich")


def target_company_ids(conn: sqlite3.Connection, thesis_id: int, tiers: list[str], keepers_only: bool = True,
                       limit: int = 100) -> list[int]:
    q = ("SELECT tc.company_id FROM thesis_companies tc JOIN v_latest_score s ON s.company_id = tc.company_id AND s.thesis_id = tc.thesis_id "
         "WHERE tc.thesis_id = ? AND tc.stage NOT IN ('Excluded','Passed') AND s.tier IN (%s)" % ",".join("?" * len(tiers)))
    args: list[Any] = [thesis_id, *tiers]
    if keepers_only:
        q += " AND tc.prescreen_status = 'keep'"
    q += " ORDER BY s.vertex_score DESC LIMIT ?"
    args.append(limit)
    return [r[0] for r in conn.execute(q, args)]


def plan_org_enrichment(conn: sqlite3.Connection, company_ids: list[int]) -> list[int]:
    rows = conn.execute(
        f"SELECT id, domain FROM companies WHERE id IN ({','.join('?' * len(company_ids))}) AND apollo_org_id IS NULL",
        company_ids,
    ).fetchall()
    domains = [r["domain"] for r in rows]
    cap = int(get_settings().budgets.apollo_org_enrich_credits_per_run)
    if len(domains) > cap:
        log.warning(f"org enrich capped at {cap} of {len(domains)} domains this run")
        domains = domains[:cap]
    return plan_org_enrich(conn, domains)


def plan_contact_search(conn: sqlite3.Connection, company_ids: list[int]) -> list[int]:
    rows = conn.execute(
        f"SELECT c.id, c.domain FROM companies c WHERE c.id IN ({','.join('?' * len(company_ids))}) "
        "AND c.id NOT IN (SELECT company_id FROM contacts WHERE source = 'apollo')",
        company_ids,
    ).fetchall()
    return plan_people_search(conn, [r["domain"] for r in rows])


def select_contacts(conn: sqlite3.Connection, thesis_id: int, company_ids: list[int]) -> dict[str, Any]:
    stats = {"selected": 0, "needs_owner_contact": 0, "review": 0}
    for cid in company_ids:
        conn.execute("BEGIN IMMEDIATE")
        try:
            contact_id, reason = select_primary(conn, cid)
            if contact_id is None:
                stats["needs_owner_contact"] += 1
            else:
                stats["selected"] += 1
                ct = conn.execute("SELECT role_rank, first_name, last_name, title FROM contacts WHERE id = ?", (contact_id,)).fetchone()
                rank = ct["role_rank"]
                # A founder/owner-titled executive is ownership evidence (one source → at best "med" when no funding rows exist).
                co = conn.execute("SELECT ownership_type, investors_json, funding_rounds, capital_raised_total FROM companies WHERE id = ?", (cid,)).fetchone()
                if rank is not None and rank <= 4 and (co["ownership_type"] or "unknown") == "unknown":
                    funded = bool(co["investors_json"] and co["investors_json"] not in ("[]", "null")) or (co["funding_rounds"] or 0) > 0 or (co["capital_raised_total"] or 0) > 0
                    conn.execute(
                        "UPDATE companies SET ownership_type = 'founder', ownership_confidence = ?, ownership_detail = ?, founder_name = COALESCE(founder_name, ?), founder_title = COALESCE(founder_title, ?), founder_active = 1 WHERE id = ?",
                        ("low" if funded else "med", f"founder-titled executive per Apollo: {ct['first_name']} {ct['last_name']}, {ct['title']}",
                         f"{ct['first_name']} {ct['last_name']}".strip(), ct["title"], cid))
                if rank is not None and rank > 7:
                    from vertex.db.repo import add_review_item
                    add_review_item(conn, "contact_choice", "contacts", contact_id, cid,
                                    {"reason": f"best available rank {rank}: {reason}"}, priority=2)
                    stats["review"] += 1
                pipeline.advance_if_behind(conn, thesis_id, cid, "Contact Found", f"primary contact selected: {reason}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return stats


def plan_reveals(conn: sqlite3.Connection, company_ids: list[int]) -> list[int]:
    rows = conn.execute(
        f"SELECT id FROM contacts WHERE company_id IN ({','.join('?' * len(company_ids))}) AND is_primary = 1 "
        "AND (email IS NULL OR email_status IS NULL) AND apollo_person_id IS NOT NULL",
        company_ids,
    ).fetchall()
    return plan_bulk_match(conn, [r["id"] for r in rows])


def contact_summary(conn: sqlite3.Connection, company_ids: list[int]) -> list[dict[str, Any]]:
    out = []
    for cid in company_ids:
        c = conn.execute("SELECT name, domain FROM companies WHERE id = ?", (cid,)).fetchone()
        p = conn.execute("SELECT first_name, last_name, title, role_rank, email, email_status, linkedin_url FROM contacts WHERE company_id = ? AND is_primary = 1", (cid,)).fetchone()
        n = conn.execute("SELECT COUNT(*) FROM contacts WHERE company_id = ?", (cid,)).fetchone()[0]
        out.append({"company": c["name"], "domain": c["domain"], "candidates": n,
                    "primary": dict(p) if p else None})
    return out

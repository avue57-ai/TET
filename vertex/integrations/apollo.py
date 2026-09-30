"""Apollo integration: organization enrichment, people search ladder, bulk match. Handlers registered by purpose.

Tool names match the Apollo MCP connector (apollo_organizations_bulk_enrich, apollo_mixed_people_api_search,
apollo_people_bulk_match). The REST backend maps the same names to api.apollo.io endpoints.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from vertex.bridge.jobs import enqueue
from vertex.bridge.router import register
from vertex.core.dedupe import Candidate
from vertex.core.normalize import normalize_domain, normalize_email, normalize_linkedin
from vertex.db.connection import utcnow
from vertex.db.repo import add_credit, add_task, record_source, upsert_company, upsert_contact
from vertex.settings import get_settings
from vertex.utils.logging import get_logger

log = get_logger("apollo")

# Title ladder. Lower = better. None = not a decision-maker for this mandate.
_LADDER: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b(founder|co-?founder|founding)\b.*\b(ceo|chief executive|president)\b|\b(ceo|president)\b.*\bfounder\b", re.I), 1),
    (re.compile(r"\bfounder\b(?!.*co-?founder)", re.I), 2),
    (re.compile(r"\bowner\b|\bproprietor\b|\bowner[- ]operator\b", re.I), 3),
    (re.compile(r"\bco-?founder\b", re.I), 4),
    (re.compile(r"\b(executive )?chairman\b|\bchairwoman\b|\bchair\b", re.I), 5),
    (re.compile(r"\bceo\b|\bchief executive\b", re.I), 6),
    (re.compile(r"\bpresident\b", re.I), 7),
    (re.compile(r"\bmanaging (partner|director|member|principal)\b|\bmanaging member\b", re.I), 8),
    (re.compile(r"\bprincipal\b|\bgeneral manager\b|\bpartner\b", re.I), 9),
]
_DISQUALIFY = re.compile(
    r"\b(vice president|vp|svp|evp|director|manager|head of|cfo|coo|cto|cmo|cro|chief (financial|operating|technology|"
    r"marketing|revenue|people|information)|controller|engineer|developer|analyst|associate|assistant|coordinator|"
    r"sales|marketing|account|recruit|hr\b|human resources|intern|consultant|advisor|board member|investor)\b",
    re.I,
)


def title_rank(title: str | None) -> int | None:
    if not title:
        return None
    t = title.strip()
    for pat, rank in _LADDER:
        if pat.search(t):
            if rank >= 5 and _DISQUALIFY.search(t) and not re.search(r"\b(ceo|president|chairman|founder|owner)\b", t, re.I):
                return None
            return rank
    return None


# ---------------------------------------------------------------- planning

def plan_org_enrich(conn: sqlite3.Connection, domains: list[str]) -> list[int]:
    jobs = []
    for i in range(0, len(domains), 10):
        chunk = domains[i : i + 10]
        jobs.append(enqueue(conn, "apollo", "apollo_organizations_bulk_enrich", {"domains": chunk}, "apollo.org_enrich",
                            context={"domains": chunk}, est_credits=len(chunk), credit_type="apollo"))
    return jobs


def plan_people_search(conn: sqlite3.Connection, domains: list[str], page: int = 1) -> list[int]:
    s = get_settings().apollo
    per = int(s.get("domains_per_people_search", 25))
    jobs = []
    for i in range(0, len(domains), per):
        chunk = domains[i : i + per]
        args = {
            "q_organization_domains_list": chunk,
            "person_titles": s.get("people_titles"),
            "person_locations": ["United States"],
            "per_page": int(s.get("people_per_page", 100)),
            "page": page,
        }
        jobs.append(enqueue(conn, "apollo", "apollo_mixed_people_api_search", args, "apollo.people_search",
                            context={"domains": chunk, "page": page}, est_credits=1, credit_type="apollo"))
    return jobs


def plan_bulk_match(conn: sqlite3.Connection, contact_ids: list[int]) -> list[int]:
    rows = conn.execute(
        f"SELECT id, apollo_person_id FROM contacts WHERE id IN ({','.join('?' * len(contact_ids))}) AND apollo_person_id IS NOT NULL",
        contact_ids,
    ).fetchall()
    jobs = []
    for i in range(0, len(rows), 10):
        chunk = rows[i : i + 10]
        details = [{"id": r["apollo_person_id"]} for r in chunk]
        jobs.append(enqueue(conn, "apollo", "apollo_people_bulk_match", {"details": details}, "apollo.bulk_match",
                            context={"contact_ids": [r["id"] for r in chunk]}, est_credits=len(chunk), credit_type="apollo"))
    return jobs


# ---------------------------------------------------------------- handlers

def _orgs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for k in ("organizations", "accounts", "data"):
            if isinstance(payload.get(k), list):
                return payload[k]
    return payload if isinstance(payload, list) else []


def _map_org(org: dict[str, Any]) -> tuple[Candidate, dict[str, Any]]:
    domain = org.get("primary_domain") or org.get("domain") or org.get("website_url")
    cand = Candidate(domain=domain, name=org.get("name"), state=org.get("state"), linkedin_url=org.get("linkedin_url"),
                     apollo_id=org.get("id"))
    fields: dict[str, Any] = {
        "name": org.get("name"),
        "website": org.get("website_url"),
        "hq_city": org.get("city"), "hq_state": org.get("state"), "hq_country": org.get("country"),
        "industry": org.get("industry"),
        "description": org.get("short_description") or org.get("seo_description"),
        "employee_count": org.get("estimated_num_employees"),
        "employee_source": "apollo" if org.get("estimated_num_employees") else None,
        "year_founded": org.get("founded_year"),
        "linkedin_url": org.get("linkedin_url"),
        "apollo_org_id": org.get("id"),
        "last_refreshed": utcnow(),
    }
    funding_events = org.get("funding_events") or []
    total = org.get("total_funding")
    if total not in (None, 0, "0") or funding_events or org.get("latest_funding_stage"):
        fields["capital_raised_total"] = float(total) if total not in (None, "") else None
        fields["funding_rounds"] = len(funding_events) or None
        fields["last_funding_type"] = org.get("latest_funding_stage")
        fields["last_funding_date"] = org.get("latest_funding_round_date")
        inv = sorted({i for e in funding_events for i in (e.get("investors") or "").split(",") if i.strip()})
        if inv:
            fields["investors_json"] = inv
    if org.get("publicly_traded_symbol") or org.get("publicly_traded_exchange"):
        fields["ownership_type"], fields["ownership_confidence"] = "public", "high"
        fields["ownership_detail"] = f"listed {org.get('publicly_traded_exchange')}:{org.get('publicly_traded_symbol')}"
    elif org.get("owned_by_organization"):
        parent = org["owned_by_organization"].get("name") if isinstance(org["owned_by_organization"], dict) else org["owned_by_organization"]
        fields["ownership_type"], fields["ownership_confidence"] = "corporate", "med"
        fields["ownership_detail"] = f"owned by {parent}"
    return cand, fields


@register("apollo.org_enrich")
def ingest_org_enrich(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None,
                      run_id: int | None = None, **kw: Any) -> dict[str, Any]:
    orgs = _orgs(payload)
    matched = updated = 0
    for org in orgs:
        if not org or not (org.get("primary_domain") or org.get("website_url")):
            continue
        cand, fields = _map_org(org)
        existing = conn.execute("SELECT id, est_revenue_low FROM companies WHERE domain = ?", (normalize_domain(cand.domain),)).fetchone()
        if existing is None:
            # Apollo may return an alias domain; try to resolve before creating anything.
            from vertex.core.dedupe import resolve
            r = resolve(conn, cand)
            if r.company_id is None:
                log.warning(f"apollo org {cand.domain} does not match a known company; skipped")
                continue
            existing = conn.execute("SELECT id, est_revenue_low FROM companies WHERE id = ?", (r.company_id,)).fetchone()
        if existing["est_revenue_low"] is None and org.get("annual_revenue"):
            fields["est_revenue_low"] = fields["est_revenue_high"] = float(org["annual_revenue"])
            fields["revenue_basis"] = "estimate"
        cid, _, written = upsert_company(conn, cand, fields, source="apollo_org", source_ref=org.get("id"), confidence=0.7)
        record_source(conn, "apollo_org", org.get("id") or cand.domain, cid, org, batch_id=batch_id)
        matched += 1
        updated += int(bool(written))
    add_credit(conn, run_id, "apollo", "org_enrich", matched, f"job {job['id']}")
    return {"rows": len(orgs), "updated": updated, "matched": matched}


def _company_by_org_name(name: str, pool: list[dict[str, Any]]) -> dict[str, Any] | None:
    from rapidfuzz import fuzz

    from vertex.core.normalize import normalize_name

    n = normalize_name(name) or name.lower().strip()
    for c in pool:
        if c.get("name_norm") and c["name_norm"] == n:
            return c
    best, score = None, 0.0
    for c in pool:
        s = max(fuzz.token_set_ratio(n, c.get("name_norm") or ""), fuzz.partial_ratio(n, (c.get("domain") or "").split(".")[0]))
        if s > score:
            best, score = c, s
    return best if score >= 85 else None


def _people(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        out = []
        for k in ("people", "contacts"):
            if isinstance(payload.get(k), list):
                out += payload[k]
        return out
    return payload if isinstance(payload, list) else []


@register("apollo.people_search")
def ingest_people_search(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None,
                         run_id: int | None = None, **kw: Any) -> dict[str, Any]:
    people = _people(payload)
    stored = skipped = 0
    # The MCP search response often carries only the organization name (no domain): match names against the
    # companies this job searched for, exact on normalized name first, then fuzzy within that small set.
    searched = job.get("args", {}).get("q_organization_domains_list") or []
    pool = [dict(r) for r in conn.execute(
        f"SELECT id, name, name_norm, domain FROM companies WHERE domain IN ({','.join('?' * len(searched))})", searched)] if searched else []
    for p in people:
        org = p.get("organization") or p.get("account") or {}
        dom = normalize_domain(org.get("primary_domain") or org.get("website_url") or org.get("domain"))
        row = conn.execute("SELECT id FROM companies WHERE domain = ?", (dom,)).fetchone() if dom else None
        if not row and org.get("name") and pool:
            row = _company_by_org_name(org["name"], pool)
        if not row:
            skipped += 1
            continue
        rank = title_rank(p.get("title"))
        data = {
            "apollo_person_id": p.get("id"),
            "first_name": p.get("first_name"), "last_name": p.get("last_name"),
            "title": p.get("title"), "seniority": p.get("seniority"), "role_rank": rank,
            "linkedin_url": normalize_linkedin(p.get("linkedin_url")),
            "email_status": None,
            "source": "apollo",
            "confidence": 0.5 if rank else 0.2,
        }
        upsert_contact(conn, row["id"], data)
        record_source(conn, "apollo_person", p.get("id"), row["id"], p, batch_id=batch_id)
        stored += 1
    add_credit(conn, run_id, "apollo", "people_search", 1, f"job {job['id']}")
    return {"rows": len(people), "inserted": stored, "skipped": skipped}


@register("apollo.bulk_match")
def ingest_bulk_match(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None,
                      run_id: int | None = None, **kw: Any) -> dict[str, Any]:
    matches = payload.get("matches") if isinstance(payload, dict) else payload
    matches = [m for m in (matches or []) if m]
    revealed = 0
    for m in matches:
        pid = m.get("id")
        if not pid:
            continue
        row = conn.execute("SELECT id, company_id FROM contacts WHERE apollo_person_id = ?", (pid,)).fetchone()
        if not row:
            continue
        email = normalize_email(m.get("email"))
        status_raw = (m.get("email_status") or "").lower()
        status = {"verified": "verified", "likely to engage": "likely", "likely_to_engage": "likely", "guessed": "unverified",
                  "unverified": "unverified", "unavailable": "unavailable", "invalid": "invalid"}.get(status_raw, "unverified" if email else "unavailable")
        conf = {"verified": 0.9, "likely": 0.75, "unverified": 0.5, "unavailable": 0.0, "invalid": 0.0}[status]
        data = {
            "apollo_person_id": pid,
            "email": email, "email_status": status, "email_confidence": conf,
            "first_name": m.get("first_name"), "last_name": m.get("last_name"), "title": m.get("title"),
            "linkedin_url": normalize_linkedin(m.get("linkedin_url")),
            "employment_verified_at": utcnow(),
            "confidence": max(0.3, min(0.95, 0.5 + conf / 2)),
        }
        rank = title_rank(m.get("title"))
        if rank is not None:
            data["role_rank"] = rank
        upsert_contact(conn, row["company_id"], data)
        record_source(conn, "apollo_person", pid, row["company_id"], m, batch_id=batch_id)
        revealed += int(bool(email))
    add_credit(conn, run_id, "apollo", "bulk_match", len(matches), f"job {job['id']}")
    return {"rows": len(matches), "updated": revealed}


# ---------------------------------------------------------------- selection

def select_primary(conn: sqlite3.Connection, company_id: int) -> tuple[int | None, str]:
    """Pick the primary decision-maker: lowest role_rank, then verified email, then LinkedIn present."""
    rows = conn.execute(
        "SELECT id, role_rank, email_status, linkedin_url, title FROM contacts WHERE company_id = ? AND do_not_contact = 0 AND role_rank IS NOT NULL",
        (company_id,),
    ).fetchall()
    conn.execute("UPDATE contacts SET is_primary = 0 WHERE company_id = ?", (company_id,))
    if not rows:
        add_task(conn, company_id, "needs_owner_contact", created_from="select_primary",
                 notes="No founder/owner/CEO/president-level contact found in Apollo; source manually (LinkedIn/website).")
        return None, "no decision-maker found"
    def key(r):
        return (r["role_rank"], 0 if r["email_status"] in ("verified", "likely") else 1, 0 if r["linkedin_url"] else 1)
    best = sorted(rows, key=key)[0]
    reason = f"rank {best['role_rank']} ({best['title']})"
    conn.execute("UPDATE contacts SET is_primary = 1, selection_reason = ? WHERE id = ?", (reason, best["id"]))
    return int(best["id"]), reason


# ---------------------------------------------------------------- saved account lists → existing-relationship holds
def plan_crm_accounts(conn: sqlite3.Connection, label_id: str, label_name: str, pages: range, per_page: int = 100) -> list[int]:
    """One job per page of apollo_mixed_companies_search filtered to a saved account list (1 credit per non-empty page)."""
    return [enqueue(conn, "apollo", "apollo_mixed_companies_search",
                    {"account_label_ids": [label_id], "page": p, "per_page": per_page}, "apollo.crm_accounts",
                    context={"label_id": label_id, "label_name": label_name, "page": p}, est_credits=1, credit_type="apollo")
            for p in pages]


@register("apollo.crm_accounts")
def ingest_crm_accounts(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    from vertex.workflows.suppression import add_suppression

    ctx = job.get("context") or {}
    label = ctx.get("label_name") or ctx.get("label_id") or "list"
    accounts = (payload.get("accounts") if isinstance(payload, dict) else None) or []
    pag = (payload.get("pagination") if isinstance(payload, dict) else None) or {}
    inserted = 0
    domains: list[str] = []
    for a in accounts:
        d = normalize_domain(a.get("domain") or a.get("primary_domain") or a.get("website_url") or "")
        if not d:
            continue
        domains.append(d)
        inserted += int(add_suppression(conn, "domain", d, "existing_relationship", f"apollo_crm:{label[:40]}", expires_at=None, hold_for_human=True))
    record_source(conn, "apollo_crm", f"{ctx.get('label_id')}:page{ctx.get('page')}", None,
                  {"label": label, "page": ctx.get("page"), "total_pages": pag.get("total_pages"), "total_entries": pag.get("total_entries"), "domains": domains},
                  batch_id=batch_id)
    if job.get("backend") == "key" and pag.get("page") and pag.get("total_pages") and pag["page"] < pag["total_pages"]:
        plan_crm_accounts(conn, ctx["label_id"], label, range(pag["page"] + 1, pag["page"] + 2), int(pag.get("per_page") or 100))
    add_credit(conn, kw.get("run_id"), "apollo", "credits", 1 if accounts else 0, f"crm_accounts page {ctx.get('page')}")
    return {"rows": len(accounts), "inserted": inserted, "skipped": len(accounts) - inserted, "total_entries": pag.get("total_entries")}

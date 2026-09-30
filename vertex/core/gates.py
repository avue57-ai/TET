"""Enrollment gates. Every gate must pass (or be explicitly overridden by a human decision) before a lead is pushed to Lemlist."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from vertex.db.connection import today, utcnow
from vertex.settings import get_settings
from vertex.workflows.suppression import hits, seed_status

REQUIRED_STEPS = {"linkedin_led": ["li_note", "li_msg", "email1", "email2", "email3", "email4", "email5"],
                  "email_first": ["li_note", "email1", "email2", "email3", "email4", "email5"]}
GATE_ORDER = ["suppression", "prior_outreach_checked", "relationship_source", "unsubscribes_fresh", "contact_quality",
              "copy_approved", "capacity", "campaign_state"]


@dataclass
class GateResult:
    name: str
    passed: bool
    hold: bool = False
    detail: str = ""


def _age_days(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return (datetime.now(UTC) - t).total_seconds() / 86400


def check_enrollment(conn: sqlite3.Connection, contact_id: int, campaign_id: int, channel_scope: str = "full") -> tuple[bool, list[GateResult], int | None]:
    """Returns (all_passed, results, decision_id). decision_id is the approving review decision, when one exists."""
    s = get_settings()
    cfg = s.campaign
    ct = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    co = conn.execute("SELECT * FROM companies WHERE id = ?", (ct["company_id"],)).fetchone()
    camp = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    res: list[GateResult] = []

    # 1. suppression (permanent, cooldown, unsubscribe, existing relationship hold)
    h = hits(conn, ct["email"], co["domain"], ct["linkedin_url"])
    if h:
        holds = [r for r in h if r["hold_for_human"]]
        blocks = [r for r in h if not r["hold_for_human"]]
        if blocks:
            res.append(GateResult("suppression", False, False, "; ".join(f"{r['kind']}={r['value']} ({r['reason']}, {r['source']})" for r in blocks)))
        else:
            res.append(GateResult("suppression", False, True, "held for a human: " + "; ".join(f"{r['kind']}={r['value']} ({r['reason']})" for r in holds)))
    else:
        res.append(GateResult("suppression", True))

    # 2. prior outreach was actually looked up for this contact / company (bulk import or per-target lookups)
    bulk = conn.execute("SELECT 1 FROM ingest_batches WHERE source = 'lemlist' AND kind = 'campaign_leads' AND status = 'done' LIMIT 1").fetchone()
    looked = conn.execute("SELECT MAX(fetched_at) FROM source_records WHERE source = 'lemlist' AND source_id IN (?, ?)",
                          (f"lead_lookup:{ct['email'] or ''}", f"contact_lookup:{co['domain']}")).fetchone()[0]
    age = _age_days(looked)
    if bulk or (age is not None and age <= 30):
        res.append(GateResult("prior_outreach_checked", True))
    else:
        res.append(GateResult("prior_outreach_checked", False, False, "no Lemlist lead/contact lookup in the last 30 days and no bulk import"))

    # 3. an existing-relationship source has been ingested at least once
    st = seed_status(conn)
    res.append(GateResult("relationship_source", bool(st["existing_relationship_source_present"]), False,
                          "" if st["existing_relationship_source_present"] else "no existing-relationship source ingested (never_contact / apollo_crm / granola)"))

    # 4. unsubscribes synced within 7 days
    ua = _age_days(st.get("unsubscribes_synced_at"))
    res.append(GateResult("unsubscribes_fresh", ua is not None and ua <= 7, False, "" if ua is not None and ua <= 7 else "Lemlist unsubscribes not synced in the last 7 days"))

    # 5. contact quality
    problems = []
    if not ct["is_primary"]:
        problems.append("not the primary contact")
    if ct["do_not_contact"]:
        problems.append("do_not_contact")
    if (ct["role_rank"] or 99) > 8:
        problems.append(f"role_rank {ct['role_rank']} is below owner level")
    if (ct["confidence"] or 0) < float(cfg.get("contact_confidence_min", 0.6)):
        problems.append(f"confidence {ct['confidence']} < {cfg.get('contact_confidence_min', 0.6)}")
    if channel_scope == "full" and ct["email_status"] not in ("verified", "likely"):
        problems.append(f"email_status {ct['email_status']}")
    if channel_scope == "linkedin_only" and not ct["linkedin_url"]:
        problems.append("linkedin_only without a LinkedIn URL")
    ea = _age_days(ct["employment_verified_at"])
    if ea is None or ea > int(cfg.get("employment_verified_max_age_days", 30)):
        problems.append("employment not verified in the last 30 days")
    res.append(GateResult("contact_quality", not problems, False, "; ".join(problems)))

    # 6. copy approved + an approving enroll decision
    steps = REQUIRED_STEPS[camp["arm"]]
    if channel_scope == "linkedin_only":
        steps = [k for k in steps if k.startswith("li_")]
    rows = conn.execute("SELECT step_key, status FROM messages WHERE contact_id = ? AND sequence_version = ?", (contact_id, camp["sequence_version"])).fetchall()
    status = {r["step_key"]: r["status"] for r in rows}
    missing = [k for k in steps if status.get(k) not in ("approved", "edited")]
    dec = conn.execute(
        "SELECT d.id FROM review_decisions d JOIN review_items i ON i.id = d.review_item_id WHERE i.item_type = 'enroll' AND i.ref_table = 'contacts' AND i.ref_id = ? "
        "AND d.decision = 'approve' ORDER BY d.id DESC LIMIT 1", (contact_id,)).fetchone()
    decision_id = dec["id"] if dec else None
    detail = []
    if missing:
        detail.append("unapproved steps: " + ", ".join(missing))
    if not decision_id:
        detail.append("no approving enroll decision")
    res.append(GateResult("copy_approved", not detail, False, "; ".join(detail)))

    # 7. capacity: daily cap and mailbox headroom
    pushed_today = conn.execute("SELECT COUNT(*) FROM enrollments WHERE pushed_at >= ?", (today() + "T00:00:00",)).fetchone()[0]
    cap = int(cfg.get("enroll_cap_per_day", 25))
    headroom = sum(m.engine_allocation for m in s.mailboxes.values() if m.enabled)
    li_cap = int(s.linkedin_limits.get("daily_invites", 20) * s.linkedin_limits.get("engine_share", 0.75))
    limit = min(cap, headroom if channel_scope == "full" else li_cap)
    res.append(GateResult("capacity", pushed_today < limit, False, "" if pushed_today < limit else f"{pushed_today} pushed today >= limit {limit} (cap {cap}, mailbox headroom {headroom}, linkedin {li_cap})"))

    # 8. campaign state: engine-owned, draft or paused
    prefix = cfg.get("name_prefix", "VX |")
    ok = bool(camp["lemlist_campaign_id"]) and camp["name"].startswith(prefix) and (camp["lemlist_state"] in (None, "draft", "paused"))
    res.append(GateResult("campaign_state", ok, False, "" if ok else f"campaign '{camp['name']}' state={camp['lemlist_state']} lemlist_id={camp['lemlist_campaign_id']}"))

    return all(r.passed for r in res), res, decision_id


def results_json(results: list[GateResult]) -> str:
    return json.dumps([asdict(r) for r in results])

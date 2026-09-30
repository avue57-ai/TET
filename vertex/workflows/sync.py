"""Engagement sync (polling): campaign stats, inbox conversations and threads → events, replies, enrollment states, stages.

Bridge purposes:
  lemlist.campaign_stats   get_campaigns_stats(campaignIds)          -> engagement rollups per engine campaign + deliverability checks
  lemlist.inbox_list       get_inbox_conversations(...)              -> replies for engine contacts (matched by email / LinkedIn)
  lemlist.conversation     get_inbox_conversation(contactId)         -> full reply text + AI interest for a conversation
  lemlist.leads_state      search_campaign_leads(campaignId, include=activities) -> per-lead activities → engagement_events
  lemlist.paused           set_campaign_state(pause)                 -> the only state change automation may make (deliverability breach)
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from vertex.ai.classify import classify_reply
from vertex.bridge.jobs import enqueue
from vertex.bridge.router import register
from vertex.core.normalize import normalize_email, normalize_linkedin
from vertex.core.pipeline import advance_if_behind
from vertex.db.connection import utcnow
from vertex.db.repo import add_review_item, record_source
from vertex.settings import get_settings
from vertex.utils.logging import get_logger

log = get_logger("sync")
EVENT_STAGE = {"emailsSent": "Contacted", "linkedinInviteDone": "Contacted", "linkedinSent": "Contacted", "emailsOpened": "Engaged",
               "emailsClicked": "Engaged", "linkedinInviteAccepted": "Engaged", "emailsReplied": "Engaged", "linkedinReplied": "Engaged"}


# ---------------------------------------------------------------- planners
def engine_campaigns(conn: sqlite3.Connection, running_only: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM campaigns WHERE lemlist_campaign_id IS NOT NULL"
    if running_only:
        q += " AND (lemlist_state = 'running' OR launched_at IS NOT NULL)"
    return conn.execute(q + " ORDER BY id").fetchall()


def plan_sync(conn: sqlite3.Connection, since_hours: int = 24, running_only: bool = True) -> list[int]:
    camps = engine_campaigns(conn, running_only)
    jobs: list[int] = []
    if camps:
        ids = [c["lemlist_campaign_id"] for c in camps]
        jobs.append(enqueue(conn, "lemlist", "get_campaigns_stats", {"campaignIds": ids}, "lemlist.campaign_stats", context={"campaign_ids": [c["id"] for c in camps]}, est_credits=0, credit_type="lemlist"))
        for c in camps:
            jobs.append(enqueue(conn, "lemlist", "search_campaign_leads", {"campaignId": c["lemlist_campaign_id"], "include": ["activities"], "limit": 50, "offset": 0},
                                "lemlist.leads_state", context={"campaign_id": c["id"], "offset": 0}, est_credits=0, credit_type="lemlist"))
        since = (datetime.now(UTC) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        jobs.append(enqueue(conn, "lemlist", "get_inbox_conversations", {"listId": "teamConversations", "limit": 50, "page": 1,
                                                                        "campaignFilter": {"in": ids}, "dateFilter": {"from": since, "to": utcnow()}},
                            "lemlist.inbox_list", context={"page": 1}, est_credits=0, credit_type="lemlist"))
    return jobs


# ---------------------------------------------------------------- helpers
def _contact_for(conn: sqlite3.Connection, email: str | None, linkedin: str | None, lead_id: str | None = None) -> sqlite3.Row | None:
    if lead_id:
        r = conn.execute("SELECT c.* FROM contacts c JOIN enrollments e ON e.contact_id = c.id WHERE e.lemlist_lead_id = ?", (lead_id,)).fetchone()
        if r:
            return r
    e = normalize_email(email) if email else None
    if e:
        r = conn.execute("SELECT * FROM contacts WHERE email = ?", (e,)).fetchone()
        if r:
            return r
    li = normalize_linkedin(linkedin) if linkedin else None
    if li:
        return conn.execute("SELECT * FROM contacts WHERE linkedin_url = ?", (li,)).fetchone()
    return None


def _enrollment_for(conn: sqlite3.Connection, contact_id: int, lemlist_campaign_id: str | None = None) -> sqlite3.Row | None:
    if lemlist_campaign_id:
        r = conn.execute("SELECT e.* FROM enrollments e JOIN campaigns c ON c.id = e.campaign_id WHERE e.contact_id = ? AND c.lemlist_campaign_id = ?", (contact_id, lemlist_campaign_id)).fetchone()
        if r:
            return r
    return conn.execute("SELECT * FROM enrollments WHERE contact_id = ? ORDER BY id DESC LIMIT 1", (contact_id,)).fetchone()


def _record_event(conn: sqlite3.Connection, enr: sqlite3.Row | None, activity: dict[str, Any], campaign_lemlist_id: str | None) -> bool:
    aid = activity.get("_id") or activity.get("id")
    if not aid:
        return False
    etype = activity.get("type") or activity.get("activityType") or "unknown"
    when = activity.get("createdAt") or activity.get("date") or utcnow()
    cur = conn.execute(
        "INSERT OR IGNORE INTO engagement_events(enrollment_id, lemlist_activity_id, lemlist_lead_id, campaign_lemlist_id, event_type, step_index, channel, occurred_at, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (enr["id"] if enr else None, aid, activity.get("leadId"), campaign_lemlist_id, etype, activity.get("sequenceStep") or activity.get("stepIndex"),
         activity.get("channel") or ("linkedin" if "linkedin" in etype.lower() else "email"), when, json.dumps(activity, default=str)[:4000]))
    if cur.rowcount and enr:
        stage = EVENT_STAGE.get(etype)
        if stage:
            camp = conn.execute("SELECT thesis_id FROM campaigns WHERE id = ?", (enr["campaign_id"],)).fetchone()
            advance_if_behind(conn, camp["thesis_id"], enr["company_id"], stage, f"lemlist {etype}", "system")
        if etype in ("emailsSent", "linkedinInviteDone", "linkedinSent") and enr["state"] in ("pushed", "queued"):
            conn.execute("UPDATE enrollments SET state = 'active' WHERE id = ?", (enr["id"],))
        if etype in ("emailsBounced",):
            conn.execute("UPDATE enrollments SET state = 'stopped', pause_reason = 'bounced' WHERE id = ?", (enr["id"],))
            conn.execute("UPDATE contacts SET email_status = 'bounced', email_confidence = 0 WHERE id = ?", (enr["contact_id"],))
        if etype in ("emailsUnsubscribed",):
            conn.execute("UPDATE enrollments SET state = 'stopped', pause_reason = 'unsubscribed' WHERE id = ?", (enr["id"],))
    return bool(cur.rowcount)


# ---------------------------------------------------------------- handlers
@register("lemlist.campaign_stats")
def h_campaign_stats(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    s = get_settings().campaign
    entries = payload if isinstance(payload, list) else (payload.get("campaigns") or payload.get("stats") or [payload]) if isinstance(payload, dict) else []
    n = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        lid = e.get("campaignId") or e.get("_id") or e.get("id")
        camp = conn.execute("SELECT * FROM campaigns WHERE lemlist_campaign_id = ?", (lid,)).fetchone()
        if not camp:
            continue
        record_source(conn, "lemlist", f"stats:{lid}:{utcnow()[:13]}", None, e, batch_id=batch_id)
        n += 1
        mm = e.get("messageMetrics") or {}
        sent = float(mm.get("sent") or 0)
        bounced = float(mm.get("bounced") or (mm.get("perChannel", {}).get("email", {}) or {}).get("bounced") or 0)
        unsub = float(e.get("leadMetrics", {}).get("unsubscribed") or mm.get("unsubscribed") or 0)
        cm = e.get("channelMetrics") or {}
        invites = float(cm.get("linkedinInvitationSent") or cm.get("linkedinInviteDone") or 0)
        accepted = float(cm.get("linkedinInvitationAccepted") or 0)
        breaches = []
        if sent >= 50 and bounced / sent * 100 > float(s.get("bounce_rate_pause_pct", 3.0)):
            breaches.append(f"bounce rate {bounced / sent * 100:.1f}%")
        if sent >= 50 and unsub / sent * 100 > float(s.get("unsubscribe_rate_pause_pct", 1.0)):
            breaches.append(f"unsubscribe rate {unsub / sent * 100:.1f}%")
        if invites >= 100 and accepted / invites * 100 < float(s.get("linkedin_accept_min_pct_after_100", 10.0)):
            breaches.append(f"LinkedIn acceptance {accepted / invites * 100:.1f}% after {int(invites)} invites")
        if breaches and camp["lemlist_state"] == "running":
            enqueue(conn, "lemlist", "set_campaign_state", {"campaignId": lid, "state": "pause"}, "lemlist.paused",
                    context={"campaign_id": camp["id"], "reason": "; ".join(breaches)}, est_credits=0, credit_type="lemlist")
            add_review_item(conn, "exception", "campaigns", camp["id"], None, {"kind": "deliverability_breach", "campaign_id": camp["id"], "breaches": breaches,
                                                                             "note": "Automated pause requested (the only state change automation makes)."}, priority=1)
    return {"rows": len(entries), "inserted": n}


@register("lemlist.paused")
def h_paused(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    cid = job["context"]["campaign_id"]
    conn.execute("UPDATE campaigns SET lemlist_state = 'paused' WHERE id = ?", (cid,))
    conn.execute("UPDATE enrollments SET state = 'paused', paused_at = ?, pause_reason = ? WHERE campaign_id = ? AND state = 'active'", (utcnow(), job["context"].get("reason"), cid))
    return {"rows": 1, "inserted": 1}


@register("lemlist.leads_state")
def h_leads_state(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    cid = job["context"]["campaign_id"]
    camp = conn.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()
    leads = payload.get("leads") if isinstance(payload, dict) else payload
    new_events = 0
    for lead in leads or []:
        ct = _contact_for(conn, lead.get("email"), lead.get("linkedinUrl"), lead.get("_id"))
        enr = _enrollment_for(conn, ct["id"], camp["lemlist_campaign_id"]) if ct else None
        if enr and not enr["lemlist_lead_id"] and lead.get("_id"):
            conn.execute("UPDATE enrollments SET lemlist_lead_id = ? WHERE id = ?", (lead["_id"], enr["id"]))
        for a in lead.get("activities") or []:
            new_events += int(_record_event(conn, enr, a, camp["lemlist_campaign_id"]))
    if isinstance(payload, dict) and payload.get("hasMore") and payload.get("nextOffset") is not None:
        enqueue(conn, "lemlist", "search_campaign_leads", {**job["args"], "offset": payload["nextOffset"]}, "lemlist.leads_state",
                context={"campaign_id": cid, "offset": payload["nextOffset"]}, est_credits=0, credit_type="lemlist")
    return {"rows": len(leads or []), "inserted": new_events}


@register("lemlist.inbox_list")
def h_inbox_list(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    convs = payload.get("conversations") if isinstance(payload, dict) else payload
    queued = 0
    for c in convs or []:
        if not c.get("lastRepliedAt"):
            continue
        ct = _contact_for(conn, c.get("contactEmail"), c.get("contactLinkedinUrl"))
        if not ct:
            continue
        known = conn.execute("SELECT 1 FROM replies WHERE contact_id = ? AND received_at >= ?", (ct["id"], c["lastRepliedAt"])).fetchone()
        if known:
            continue
        enqueue(conn, "lemlist", "get_inbox_conversation", {"contactId": c["contactId"], "limit": 10}, "lemlist.conversation",
                context={"contact_id": ct["id"], "lemlist_contact_id": c["contactId"]}, est_credits=0, credit_type="lemlist")
        queued += 1
    pag = payload.get("pagination") if isinstance(payload, dict) else None
    if pag and pag.get("nextPage"):
        enqueue(conn, "lemlist", "get_inbox_conversations", {**job["args"], "page": pag["nextPage"]}, "lemlist.inbox_list", context={"page": pag["nextPage"]}, est_credits=0, credit_type="lemlist")
    return {"rows": len(convs or []), "inserted": queued}


def _strip_quotes(text: str) -> str:
    text = re.split(r"\n(On .{5,80}wrote:|From: |-----Original Message-----|Sent from my)", text, maxsplit=1)[0]
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith(">")).strip()


@register("lemlist.conversation")
def h_conversation(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    ctx = job["context"]
    contact_id = ctx["contact_id"]
    acts = payload.get("activities") if isinstance(payload, dict) else payload
    enr = _enrollment_for(conn, contact_id)
    inserted = 0
    for a in acts or []:
        if not isinstance(a, dict):
            continue
        t = (a.get("type") or "").lower()
        if "replied" not in t and a.get("direction") not in ("inbound", "received") and not a.get("isReply"):
            continue
        mid = a.get("_id") or a.get("id")
        if not mid or conn.execute("SELECT 1 FROM replies WHERE lemlist_message_id = ?", (mid,)).fetchone():
            continue
        text = _strip_quotes(a.get("text") or a.get("message") or a.get("body") or "")
        level = a.get("aiLeadInterestLevel")
        conn.execute(
            "INSERT INTO replies(enrollment_id, contact_id, lemlist_contact_id, lemlist_message_id, channel, received_at, text, ai_lead_interest) VALUES (?,?,?,?,?,?,?,?)",
            (enr["id"] if enr else None, contact_id, ctx.get("lemlist_contact_id"), mid, a.get("channel") or ("linkedin" if "linkedin" in t else "email"),
             a.get("createdAt") or a.get("date") or utcnow(), text, level))
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        inserted += 1
        if enr:
            conn.execute("UPDATE enrollments SET state = 'stopped', pause_reason = 'replied' WHERE id = ? AND state IN ('active','pushed')", (enr["id"],))
        try:
            classify_reply(conn, rid, use_llm=True)
        except Exception as ex:  # classification failure never blocks the sync; the reply lands in review
            log.warning(f"classify failed for reply {rid}: {ex}")
            add_review_item(conn, "reply_action", "replies", rid, None, {"error": str(ex)[:200]}, priority=1)
    return {"rows": len(acts or []), "inserted": inserted}

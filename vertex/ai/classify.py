"""Reply classification: deterministic rules first, then Claude into eleven classes with extracted fields; actions are idempotent.

Humans send every reply. The engine classifies, routes high-intent or low-confidence replies to review, creates tasks,
updates ownership facts and suppression, and drafts answers for approval.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from vertex.ai.claude import run_prompt
from vertex.core.pipeline import move
from vertex.db.connection import utcnow
from vertex.db.repo import add_review_item, add_task
from vertex.workflows.suppression import add_suppression, cooldown

CLASSES = ["Interested", "Open to conversation", "Not now", "Follow up in X months", "Not interested", "Already sold / PE-backed",
           "Wrong person", "Referral", "Remove me", "OOO", "Bounce", "Unclear"]
HIGH_INTENT = {"Interested", "Open to conversation"}
AUTO_ACTION_MIN_CONF = 0.7

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("Bounce", re.compile(r"(delivery (has )?failed|undeliverable|address not found|mailbox (is )?full|user unknown|550 5\.1\.1|does not exist|delivery status notification)", re.I)),
    ("OOO", re.compile(r"(out of (the )?office|on (annual |parental |maternity |paternity )?leave|away from (my )?(email|the office)|auto-?reply|automatic reply|currently traveling|limited access to email)", re.I)),
    ("Remove me", re.compile(r"(unsubscribe|remove me|take me off|stop (emailing|contacting|sending)|do not (contact|email) (me|us)( again)?|opt me out)", re.I)),
]


class Referral(BaseModel):
    name: str | None = None
    title: str | None = None
    company: str | None = None
    hint: str | None = None


class ReplyVerdict(BaseModel):
    class_: str = Field(alias="class")
    confidence: float = Field(ge=0, le=1)
    follow_up_months: int | None = None
    referral: Referral | None = None
    ownership_fact: str | None = None
    return_date: str | None = None
    rationale: str = ""

    model_config = {"populate_by_name": True}


def rule_class(text: str) -> tuple[str, float] | None:
    t = (text or "").strip()
    if not t or len(re.sub(r"\W+", "", t)) < 3:
        return "Unclear", 0.9
    for cls, pat in _RULES:
        if pat.search(t):
            return cls, 0.92
    return None


def _context(conn: sqlite3.Connection, reply: sqlite3.Row) -> dict[str, Any]:
    ct = conn.execute("SELECT * FROM contacts WHERE id = ?", (reply["contact_id"],)).fetchone() if reply["contact_id"] else None
    co = conn.execute("SELECT * FROM companies WHERE id = ?", (ct["company_id"],)).fetchone() if ct else None
    our = None
    if ct:
        our = conn.execute("SELECT subject, body FROM messages WHERE contact_id = ? AND status IN ('approved','edited') ORDER BY CASE step_key WHEN 'email1' THEN 0 ELSE 1 END, id LIMIT 1", (ct["id"],)).fetchone()
    thread = conn.execute("SELECT received_at, text FROM replies WHERE contact_id = ? AND id != ? ORDER BY received_at", (reply["contact_id"], reply["id"])).fetchall() if ct else []
    return {
        "contact_name": f"{ct['first_name'] or ''} {ct['last_name'] or ''}".strip() if ct else "unknown", "contact_title": ct["title"] if ct else "",
        "company_name": co["name"] if co else "unknown", "vertical": co["vertical"] if co else "",
        "channel": reply["channel"] or "email", "ai_lead_interest": reply["ai_lead_interest"] if reply["ai_lead_interest"] is not None else "missing",
        "our_message": (f"Subject: {our['subject']}\n{our['body']}" if our else "(not on file)"), "reply_text": reply["text"] or "",
        "thread": "\n---\n".join(f"{t['received_at']}: {t['text']}" for t in thread[-3:]) if thread else "",
    }


def classify_reply(conn: sqlite3.Connection, reply_id: int, use_llm: bool = True) -> dict[str, Any]:
    reply = conn.execute("SELECT * FROM replies WHERE id = ?", (reply_id,)).fetchone()
    rc = rule_class(reply["text"] or "")
    if rc:
        cls, conf = rc
        by, extracted, rationale = "rule", {}, f"rule match: {cls}"
        if cls == "OOO":
            m = re.search(r"(?:back|return(?:ing)?)\s+(?:on|in the office on)?\s*([A-Z][a-z]+ \d{1,2}(?:, \d{4})?|\d{1,2}/\d{1,2}(?:/\d{2,4})?)", reply["text"] or "")
            if m:
                extracted["return_date"] = m.group(1)
    elif use_llm:
        v = run_prompt(conn, "classify_reply", _context(conn, reply), ReplyVerdict)
        cls = v.class_ if v.class_ in CLASSES else "Unclear"
        conf, by = float(v.confidence), "llm"
        extracted = {k: val for k, val in v.model_dump(by_alias=True).items() if k not in ("class", "confidence", "rationale") and val}
        rationale = v.rationale
    else:
        cls, conf, by, extracted, rationale = "Unclear", 0.0, "rule", {}, "llm disabled"
    needs_review = conf < AUTO_ACTION_MIN_CONF or cls in HIGH_INTENT or (cls in HIGH_INTENT) != ((reply["ai_lead_interest"] or 0) >= 4 if reply["ai_lead_interest"] is not None else cls in HIGH_INTENT)
    conn.execute("UPDATE replies SET predicted_class = ?, predicted_confidence = ?, extracted_json = ?, classified_by = ?, rationale = ?, final_class = CASE WHEN ? THEN NULL ELSE ? END WHERE id = ?",
                 (cls, conf, json.dumps(extracted), by, rationale, int(needs_review), cls, reply_id))
    if needs_review:
        add_review_item(conn, "reply_action", "replies", reply_id, None, {"predicted_class": cls, "confidence": conf}, priority=1 if cls in HIGH_INTENT else 2)
    else:
        apply_actions(conn, reply_id, cls, extracted, actor="llm" if by == "llm" else "system")
    return {"reply_id": reply_id, "class": cls, "confidence": conf, "by": by, "needs_review": needs_review, "extracted": extracted}


def apply_actions(conn: sqlite3.Connection, reply_id: int, cls: str, extracted: dict[str, Any], actor: str = "system") -> list[str]:
    """Idempotent per reply: recorded in replies.auto_actions_json; never sends anything."""
    reply = conn.execute("SELECT * FROM replies WHERE id = ?", (reply_id,)).fetchone()
    done = json.loads(reply["auto_actions_json"] or "[]")
    if done:
        return done
    ct = conn.execute("SELECT * FROM contacts WHERE id = ?", (reply["contact_id"],)).fetchone() if reply["contact_id"] else None
    co = conn.execute("SELECT * FROM companies WHERE id = ?", (ct["company_id"],)).fetchone() if ct else None
    tid = conn.execute("SELECT thesis_id FROM thesis_companies WHERE company_id = ? LIMIT 1", (co["id"],)).fetchone()[0] if co else None
    actions: list[str] = []
    today = datetime.now(UTC)

    def task(kind: str, days: int | None, notes: str, prio_note: str = "") -> None:
        due = (today + timedelta(days=days)).strftime("%Y-%m-%d") if days is not None else None
        add_task(conn, co["id"] if co else None, kind, due_date=due, contact_id=ct["id"] if ct else None, created_from=f"reply {reply_id}", notes=(prio_note + " " + notes).strip())
        actions.append(f"task:{kind}:{due or 'now'}")

    def stop_lead(reason: str) -> None:
        if ct:
            conn.execute("UPDATE enrollments SET state = 'stopped', pause_reason = ? WHERE contact_id = ? AND state IN ('queued','pushed','active','paused')", (reason, ct["id"]))
        actions.append(f"enrollment:stopped:{reason}")

    if cls in ("Interested", "Open to conversation"):
        task("reply_needed", 0, f"{cls}: draft an answer with two time windows; buyer stays anonymous until the call.", "P1" if cls == "Interested" else "P2")
        stop_lead("replied_positive")
    elif cls == "Not now":
        task("follow_up", 90, "Not now; check back in ~3 months.")
        stop_lead("not_now")
    elif cls == "Follow up in X months":
        months = int(extracted.get("follow_up_months") or 6)
        task("follow_up", 30 * months, f"Asked to reconnect in {months} months.")
        if ct and ct["email"]:
            add_suppression(conn, "email", ct["email"], "prior_outreach", f"reply:{reply_id}", expires_at=cooldown(30 * months))
        stop_lead("follow_up_later")
    elif cls == "Not interested":
        if co:
            add_suppression(conn, "domain", co["domain"], "not_interested", f"reply:{reply_id}", expires_at=cooldown(365))
            if tid:
                move(conn, tid, co["id"], "Passed", "declined in reply", actor, force=True)
        stop_lead("not_interested")
    elif cls == "Already sold / PE-backed":
        if co:
            conn.execute("UPDATE companies SET ownership_type = CASE WHEN ownership_type IN ('unknown','founder','family','management') THEN 'pe_backed' ELSE ownership_type END, ownership_confidence = 'high', ownership_detail = ? WHERE id = ?",
                         (f"reply {reply_id}: {extracted.get('ownership_fact') or reply['text'][:200]}", co["id"]))
            add_suppression(conn, "domain", co["domain"], "pe_backed", f"reply:{reply_id}")
            if tid:
                move(conn, tid, co["id"], "Passed", "institutional ownership per reply", actor, force=True)
            conn.execute("INSERT INTO signals(company_id, hook_type, text, evidence_quote, evidence_source, confidence, safe_to_cite, banned_theme, freshness, captured_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (co["id"], "internal_note", "scoring miss: ownership was institutional per the owner's reply", (reply["text"] or "")[:400], "reply", 0.95, 0, 1, "fresh", utcnow()))
        stop_lead("pe_backed")
    elif cls == "Wrong person":
        if ct:
            conn.execute("UPDATE contacts SET is_primary = 0, notes = COALESCE(notes,'') || ' wrong person per reply' WHERE id = ?", (ct["id"],))
        task("replace_contact", 0, f"Wrong person. Referral: {json.dumps(extracted.get('referral') or {})}")
        stop_lead("wrong_person")
    elif cls == "Referral":
        ref = extracted.get("referral") or {}
        if co:
            add_review_item(conn, "contact_choice", "replies", reply_id, co["id"], {"kind": "referral", "referral": ref, "note": "review before any outreach"}, priority=2)
        task("reply_needed", 0, "Thank them for the referral (draft for approval).", "P2")
        actions.append("review:referral")
    elif cls == "Remove me":
        if ct and ct["email"]:
            add_suppression(conn, "email", ct["email"], "remove_me", f"reply:{reply_id}")
        if co:
            add_suppression(conn, "domain", co["domain"], "remove_me", f"reply:{reply_id}")
        stop_lead("remove_me")
        actions.append("lemlist:add_unsubscribe (bridge job)")
        if ct and ct["email"]:
            from vertex.bridge.jobs import enqueue
            enqueue(conn, "lemlist", "add_unsubscribe", {"value": ct["email"]}, "lemlist.unsubscribe_added", context={"reply_id": reply_id}, est_credits=0, credit_type="lemlist")
    elif cls == "OOO":
        rd = extracted.get("return_date")
        task("follow_up", 14, f"Out of office; return date {rd or 'unknown'}. Resend if the sequence ended before they were back.")
    elif cls == "Bounce":
        if ct:
            conn.execute("UPDATE contacts SET email_status = 'bounced', email_confidence = 0 WHERE id = ?", (ct["id"],))
            add_suppression(conn, "email", ct["email"] or "", "bounced", f"reply:{reply_id}") if ct["email"] else None
        task("refind_email", 0, "Email bounced; find another address or switch to LinkedIn.")
        stop_lead("bounced")
    else:
        actions.append("none:unclear")
    conn.execute("UPDATE replies SET auto_actions_json = ? WHERE id = ?", (json.dumps(actions), reply_id))
    return actions


def draft_reply_note(cls: str, first_name: str | None) -> str:
    """Skeleton the reviewer edits; the LLM draft (reply_draft prompt) replaces it when enabled."""
    fn = first_name or "there"
    if cls == "Interested":
        return f"Hi {fn}, thanks for the note. Happy to walk you through who the buyer is and how they think about ownership. Would either of these work for a 20-minute call: [window 1] or [window 2]? No materials needed on your side."
    if cls == "Open to conversation":
        return f"Hi {fn}, appreciate you replying. Short version: the buyer is a permanent-capital group that buys and holds, and keeps the team and the name. If it's easier than a call, I can send two paragraphs on how they operate. Otherwise [window 1] or [window 2] work on my end."
    return ""

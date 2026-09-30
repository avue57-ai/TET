"""Daily review page (single HTML file) and the decisions applier.

Sections: A replies needing a human, B ready-to-enroll cards, C targets to accept, D exceptions/proposals.
Decisions come back as JSON ({"date": ..., "decisions": [{"id": "E3", "decision": "approve", ...}]}) and are applied
through `apply_decisions`, which writes immutable review_decisions rows and then changes state.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from vertex.db.connection import today, utcnow
from vertex.db.repo import add_review_item, add_task
from vertex.settings import get_settings
from vertex.utils.errors import FatalError
from vertex.workflows.suppression import add_suppression
from vertex.workflows.targets import decide_target

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
REASONS = ["pe_backed", "wrong_size", "wrong_industry", "subsidiary", "bad_contact", "weak_hook", "tone", "existing_relationship", "other"]
CLASSES = ["Interested", "Open to conversation", "Not now", "Follow up in X months", "Not interested", "Already sold / PE-backed", "Wrong person",
           "Referral", "Remove me", "OOO", "Bounce", "Unclear"]
STEP_LABELS = {"li_note": "LinkedIn connection note", "li_msg": "LinkedIn message after accept", "email1": "Email 1 (day 1)", "email2": "Email 2 (+4d, threads)",
               "email3": "Email 3 (+8d, new subject)", "email4": "Email 4 (+14d, bump)", "email5": "Email 5 (+25d, close)", "email1_subject_b": "Email 1 subject B (A/B)"}
STEP_ORDER = ["li_note", "li_msg", "email1", "email1_subject_b", "email2", "email3", "email4", "email5"]


def _header(conn: sqlite3.Connection, thesis_id: int) -> dict[str, Any]:
    s = get_settings()
    d = today()
    credits = {r["provider"]: r["n"] for r in conn.execute("SELECT provider, SUM(amount) AS n FROM credit_ledger WHERE created_at >= ? GROUP BY provider", (d,))} if _has(conn, "credit_ledger", "created_at") else {}
    llm = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE created_at >= ?", (d,)).fetchone()[0]
    headroom = sum(m.engine_allocation for m in s.mailboxes.values() if m.enabled)
    camps = ", ".join(f"{r['name'].split('|')[-2].strip() if '|' in r['name'] else r['name']}={r['lemlist_state'] or 'building'}" for r in conn.execute("SELECT name, lemlist_state FROM campaigns")) or "none"
    funnel = ", ".join(f"{r['stage']} {r['n']}" for r in conn.execute("SELECT stage, COUNT(*) AS n FROM thesis_companies WHERE thesis_id = ? GROUP BY stage ORDER BY n DESC", (thesis_id,)))
    sup = conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0]
    return {"credits": ", ".join(f"{k} {int(v)}" for k, v in credits.items()) or "0", "llm_usd": llm or 0.0, "headroom": headroom, "campaigns": camps, "funnel": funnel, "suppression": sup}


def _has(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _replies(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute(
        "SELECT r.*, ct.first_name, ct.last_name, co.name AS company FROM replies r LEFT JOIN contacts ct ON ct.id = r.contact_id LEFT JOIN companies co ON co.id = ct.company_id "
        "WHERE r.human_reviewed = 0 ORDER BY r.received_at DESC LIMIT 40"):
        item = add_review_item(conn, "reply_action", "replies", r["id"], None, {"predicted_class": r["predicted_class"]}, priority=1)
        sid = conn.execute("SELECT short_id FROM review_items WHERE id = ?", (item,)).fetchone()[0]
        out.append({"short_id": sid, "contact": f"{r['first_name'] or ''} {r['last_name'] or ''}".strip(), "company": r["company"], "channel": r["channel"], "received_at": r["received_at"],
                    "predicted_class": r["predicted_class"] or "?", "predicted_confidence": r["predicted_confidence"], "text": r["text"], "suggested_reply": r["suggested_reply"]})
    return out


def _cards(conn: sqlite3.Connection, thesis_id: int, cap: int) -> tuple[list[dict[str, Any]], int]:
    rows = conn.execute(
        "SELECT DISTINCT m.contact_id FROM messages m JOIN contacts ct ON ct.id = m.contact_id JOIN thesis_companies tc ON tc.company_id = ct.company_id AND tc.thesis_id = ? "
        "WHERE ct.is_primary = 1 AND m.status IN ('draft','needs_edit') AND m.contact_id NOT IN "
        "(SELECT i.ref_id FROM review_items i JOIN review_decisions d ON d.review_item_id = i.id WHERE i.item_type = 'enroll' AND i.ref_table = 'contacts' AND d.decision IN ('approve','reject','edit'))",
        (thesis_id,)).fetchall()
    total = len(rows)
    cards: list[dict[str, Any]] = []
    ordered = conn.execute(
        "SELECT ct.id FROM contacts ct JOIN v_latest_score s ON s.company_id = ct.company_id AND s.thesis_id = ? WHERE ct.id IN (%s) ORDER BY s.vertex_score DESC" % ",".join("?" * total),
        (thesis_id, *[r[0] for r in rows])).fetchall() if total else []
    for (cid,) in ordered[:cap]:
        ct = conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()
        co = conn.execute("SELECT * FROM companies WHERE id = ?", (ct["company_id"],)).fetchone()
        sc = conn.execute("SELECT * FROM v_latest_score WHERE company_id = ? AND thesis_id = ?", (co["id"], thesis_id)).fetchone()
        item = add_review_item(conn, "enroll", "contacts", cid, co["id"], {"company": co["name"], "score": sc["vertex_score"] if sc else None}, priority=2)
        sid = conn.execute("SELECT short_id FROM review_items WHERE id = ?", (item,)).fetchone()[0]
        conf = (sc["vertex_conf"] or 0) if sc else 0
        band = round(25 * (1 - conf))
        score = round(sc["vertex_score"]) if sc and sc["vertex_score"] is not None else 0
        missing = [r[0] for r in conn.execute("SELECT component FROM score_components WHERE score_id = ? AND status = 'missing_no_data' ORDER BY component", (sc["id"],))] if sc else []
        evidence = []
        for g in conn.execute("SELECT hook_type, text, evidence_quote, evidence_source FROM signals WHERE company_id = ? AND safe_to_cite = 1 ORDER BY confidence DESC LIMIT 3", (co["id"],)):
            evidence.append({"type": g["hook_type"], "text": g["text"], "quote": (g["evidence_quote"] or "")[:160], "summary_only": (g["evidence_source"] or "").startswith("websearch")})
        if len(evidence) < 3 and sc:
            for r in conn.execute("SELECT component, evidence_text FROM score_components WHERE score_id = ? AND evidence_text IS NOT NULL AND status = 'scored' ORDER BY value DESC LIMIT ?", (sc["id"], 3 - len(evidence))):
                evidence.append({"type": r["component"], "text": r["evidence_text"][:200], "quote": None, "summary_only": False})
        msgs = {m["step_key"]: m for m in conn.execute("SELECT * FROM messages WHERE contact_id = ? AND status IN ('draft','needs_edit','approved','edited') ORDER BY id", (cid,))}
        steps = []
        flags: list[str] = []
        critic = None
        for k in STEP_ORDER:
            m = msgs.get(k)
            if not m:
                continue
            flags += [f"{k}:{f}" for f in json.loads(m["lint_flags_json"] or "[]")]
            critic = critic or m["critic_score"]
            steps.append({"key": k, "label": STEP_LABELS.get(k, k), "subject": m["subject"] if k in ("email1", "email3", "email1_subject_b") else None,
                          "body": m["body"], "collapsed": k in ("email2", "email3", "email4", "email5")})
        first = msgs.get("email1") or next(iter(msgs.values()), None)
        hook = conn.execute("SELECT text FROM signals WHERE id = ?", (first["hook_signal_id"],)).fetchone() if first and first["hook_signal_id"] else None
        alts = [{"id": a["id"], "name": f"{a['first_name'] or ''} {a['last_name'] or ''}".strip(), "title": a["title"], "rank": a["role_rank"]}
                for a in conn.execute("SELECT * FROM contacts WHERE company_id = ? AND id != ? AND do_not_contact = 0 ORDER BY role_rank, id LIMIT 4", (co["id"], cid))]
        cards.append({"short_id": sid, "company": co["name"], "domain": co["domain"], "vertical": co["vertical"], "hq": ", ".join(x for x in (co["hq_city"], co["hq_state"]) if x),
                      "employees": co["employee_count"], "ownership": co["ownership_type"], "ownership_confidence": co["ownership_confidence"],
                      "tier": sc["tier"] if sc else "?", "provisional": bool(sc["provisional"]) if sc else True, "score": score, "band_lo": max(0, score - band), "band_hi": min(100, score + band),
                      "coverage": round(sc["coverage_pct"]) if sc and sc["coverage_pct"] is not None else 0, "attr": round(sc["attractiveness"]) if sc and sc["attractiveness"] is not None else "?",
                      "trans": round(sc["transactability"]) if sc and sc["transactability"] is not None else "?", "missing": ", ".join(missing[:8]), "evidence": evidence,
                      "contact": {"name": f"{ct['first_name'] or ''} {ct['last_name'] or ''}".strip(), "title": ct["title"], "rank": ct["role_rank"], "email": ct["email"], "email_status": ct["email_status"],
                                  "confidence": ct["confidence"], "linkedin": ct["linkedin_url"]},
                      "alternates": alts, "hook_type": first["hook_type"] if first else "", "hook": hook["text"] if hook else "", "variant": first["variant_key"] if first else "",
                      "critic": critic if critic is not None else "-", "flags": ", ".join(flags), "steps": steps})
    return cards, total


def _targets(conn: sqlite3.Connection, thesis_id: int, cap: int) -> list[dict[str, Any]]:
    out = []
    for it in conn.execute(
        "SELECT i.*, c.name, c.domain, c.vertical, c.hq_city, c.hq_state, c.employee_count, c.ownership_type, c.ownership_confidence, "
        "(SELECT d.decided_by FROM review_decisions d WHERE d.review_item_id = i.id AND d.decision = 'approve' ORDER BY d.id DESC LIMIT 1) AS accepted_by "
        "FROM review_items i JOIN companies c ON c.id = i.ref_id WHERE i.item_type = 'accept_target' AND (i.status = 'pending' OR "
        "(i.status = 'decided' AND NOT EXISTS (SELECT 1 FROM review_decisions d WHERE d.review_item_id = i.id AND d.decided_by != 'system'))) ORDER BY i.priority, i.id LIMIT ?", (cap,)):
        p = json.loads(it["payload_json"] or "{}")
        out.append({"short_id": it["short_id"], "company": it["name"], "domain": it["domain"], "vertical": it["vertical"], "hq": ", ".join(x for x in (it["hq_city"], it["hq_state"]) if x),
                    "score": round(p.get("score") or 0), "tier": p.get("tier"), "provisional": p.get("provisional"), "confidence": round(p.get("confidence") or 0, 2), "coverage": round(p.get("coverage") or 0),
                    "ownership": it["ownership_type"], "ownership_confidence": it["ownership_confidence"], "ownership_evidence": (p.get("ownership_evidence") or "")[:140],
                    "employees": it["employee_count"], "fit_reason": p.get("fit_reason"), "pre_accepted": it["accepted_by"] == "system", "cohort": p.get("cohort")})
    return out


def _exceptions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for it in conn.execute("SELECT i.*, c.name AS company FROM review_items i LEFT JOIN companies c ON c.id = i.company_id WHERE i.item_type IN ('exception','contact_choice','weights_proposal','score_dispute','prescreen_gate') AND i.status = 'pending' ORDER BY i.priority, i.id LIMIT 40"):
        p = json.loads(it["payload_json"] or "{}")
        out.append({"short_id": it["short_id"], "item_type": it["item_type"], "company": it["company"], "title": p.get("kind") or it["item_type"], "detail": json.dumps({k: v for k, v in p.items() if k != "kind"}, default=str)[:600]})
    return out


def render(conn: sqlite3.Connection, thesis_slug: str, date: str | None = None) -> Path:
    s = get_settings()
    t = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis_slug,)).fetchone()
    if not t:
        raise FatalError(f"thesis {thesis_slug} not registered")
    date = date or today()
    cards, total = _cards(conn, t["id"], int(s.review.get("enroll_cards_per_day", 25)))
    ctx = {"date": date, "thesis": thesis_slug, "hdr": _header(conn, t["id"]), "replies": _replies(conn), "cards": cards, "cards_more": total if total > len(cards) else 0,
           "targets": _targets(conn, t["id"], int(s.review.get("new_target_rows_per_day", 30)) * 2), "exceptions": _exceptions(conn), "reasons": REASONS, "classes": CLASSES}
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html", "j2"]))
    html = env.get_template("review_page.html.j2").render(**ctx)
    out_dir = Path(s.data_dir) / "review"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{date}.html"
    path.write_text(html, encoding="utf-8")
    return path


# ---------------------------------------------------------------- decisions
def _item_by_short_id(conn: sqlite3.Connection, short_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM review_items WHERE short_id = ?", (short_id,)).fetchone()
    if not row:
        raise FatalError(f"unknown review id {short_id}")
    return row


def apply_decisions(conn: sqlite3.Connection, decisions: list[dict[str, Any]], decided_by: str = "human", source: str = "page") -> list[str]:
    log: list[str] = []
    for d in decisions:
        sid, decision = d.get("id"), d.get("decision")
        if not sid or not decision:
            continue
        item = _item_by_short_id(conn, sid)
        kind = item["item_type"]
        payload = {k: v for k, v in d.items() if k not in ("id", "decision")}
        conn.execute("INSERT INTO review_decisions(review_item_id, decision, reason_code, payload_json, decided_by, source, decided_at) VALUES (?,?,?,?,?,?,?)",
                     (item["id"], decision if decision in ("approve", "reject", "hold", "edit", "replace_contact", "snooze", "reclassify") else "edit", d.get("reason"), json.dumps(payload), decided_by, source, utcnow()))
        conn.execute("UPDATE review_items SET status = ? WHERE id = ?", ("pending" if decision == "hold" else "decided", item["id"]))
        if kind == "enroll":
            log.append(_apply_enroll(conn, item, decision, d))
        elif kind == "accept_target":
            tid = conn.execute("SELECT thesis_id FROM thesis_companies WHERE company_id = ? ORDER BY thesis_id LIMIT 1", (item["ref_id"],)).fetchone()[0]
            decide_target(conn, tid, item["ref_id"], "approve" if decision == "approve" else ("reject" if decision == "reject" else "hold"), decided_by, source, d.get("reason"), d.get("note"))
            log.append(f"{sid}: target {decision}")
        elif kind == "reply_action":
            log.append(_apply_reply(conn, item, decision, d))
        else:
            log.append(f"{sid}: {kind} {decision}")
    return log


def _apply_enroll(conn: sqlite3.Connection, item: sqlite3.Row, decision: str, d: dict[str, Any]) -> str:
    cid = item["ref_id"]
    if d.get("contact_id"):
        conn.execute("UPDATE contacts SET is_primary = 0 WHERE company_id = ? AND is_primary = 1", (item["company_id"],))
        conn.execute("UPDATE contacts SET is_primary = 1, selection_reason = 'human swap', updated_at = ? WHERE id = ?", (utcnow(), d["contact_id"]))
        conn.execute("UPDATE messages SET status = 'rejected' WHERE contact_id = ? AND status IN ('draft','needs_edit')", (cid,))
        add_task(conn, item["company_id"], "regenerate_copy", contact_id=d["contact_id"], created_from=f"review {item['short_id']}", notes="contact swapped by reviewer")
        return f"{item['short_id']}: contact swapped to {d['contact_id']}; copy regeneration queued"
    if decision in ("approve", "edit"):
        for key, val in (d.get("edits") or {}).items():
            step, _, part = key.partition(":")
            if part in ("subject", "body") and step:
                conn.execute(f"UPDATE messages SET {part} = ?, status = 'edited', reviewed_at = ?, updated_at = ? WHERE contact_id = ? AND step_key = ?", (val, utcnow(), utcnow(), cid, step))
        conn.execute("UPDATE messages SET status = 'approved', reviewed_at = ?, updated_at = ? WHERE contact_id = ? AND status IN ('draft','needs_edit')", (utcnow(), utcnow(), cid))
        return f"{item['short_id']}: approved{' with edits' if d.get('edits') else ''}"
    if decision == "reject":
        conn.execute("UPDATE messages SET status = 'rejected', reviewed_at = ? WHERE contact_id = ? AND status IN ('draft','needs_edit','approved','edited')", (utcnow(), cid))
        reason = d.get("reason") or "other"
        if reason == "bad_contact":
            conn.execute("UPDATE contacts SET do_not_contact = 1, is_primary = 0 WHERE id = ?", (cid,))
            add_task(conn, item["company_id"], "replace_contact", contact_id=cid, created_from=f"review {item['short_id']}")
        elif reason in ("weak_hook", "tone"):
            add_task(conn, item["company_id"], "regenerate_copy", contact_id=cid, created_from=f"review {item['short_id']}", notes=reason)
        else:
            co = conn.execute("SELECT domain FROM companies WHERE id = ?", (item["company_id"],)).fetchone()
            add_suppression(conn, "domain", co["domain"], "manual", f"review:{item['short_id']}:{reason}")
            tid = conn.execute("SELECT thesis_id FROM thesis_companies WHERE company_id = ? LIMIT 1", (item["company_id"],)).fetchone()[0]
            decide_target(conn, tid, item["company_id"], "reject", "human", "page", reason)
        return f"{item['short_id']}: rejected ({reason})"
    return f"{item['short_id']}: {decision}"


def _apply_reply(conn: sqlite3.Connection, item: sqlite3.Row, decision: str, d: dict[str, Any]) -> str:
    rid = item["ref_id"]
    r = conn.execute("SELECT * FROM replies WHERE id = ?", (rid,)).fetchone()
    final = d.get("class") or r["predicted_class"]
    conn.execute("UPDATE replies SET final_class = ?, classified_by = 'human', human_reviewed = 1, reviewed_at = ? WHERE id = ?", (final, utcnow(), rid))
    edits = d.get("edits") or {}
    if "reply_draft" in edits:
        conn.execute("UPDATE replies SET suggested_reply = ? WHERE id = ?", (edits["reply_draft"], rid))
    if decision == "snooze":
        due = (datetime.now(UTC) + timedelta(days=7)).strftime("%Y-%m-%d")
        add_task(conn, None, "follow_up", due_date=due, contact_id=r["contact_id"], created_from=f"review {item['short_id']}", notes="snoozed reply")
    return f"{item['short_id']}: reply {decision} class={final}"

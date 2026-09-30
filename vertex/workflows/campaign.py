"""Lemlist campaign build (DRAFT only), gated enrollment, readiness, and state sync.

Build path "api": create_campaign_with_sequence → add_sequence_step per step (root, then the conditional's accepted
and fallback branches) → set_ab_variant on step 1 → set_campaign_senders → update_settings (stop on reply, pause company)
→ folder → validate_campaign_readiness. Every call is a bridge job; Claude executes it (or `bridge run` with a key).
The engine never enqueues launch_campaign / set_campaign_state(start) / send_message / send_task.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from typing import Any

from vertex.ai.personalize import load_sequence
from vertex.bridge.jobs import enqueue
from vertex.bridge.router import register
from vertex.core.gates import check_enrollment, results_json
from vertex.core.pipeline import advance_if_behind
from vertex.db.connection import today, utcnow
from vertex.db.repo import add_review_item
from vertex.settings import get_settings
from vertex.utils.errors import FatalError
from vertex.utils.logging import get_logger
from vertex.workflows.suppression import add_suppression, cooldown

log = get_logger("campaign")
_ID = {"cam": re.compile(r"\bcam_[A-Za-z0-9]{6,}"), "seq": re.compile(r"\bseq_[A-Za-z0-9]{6,}"),
       "stp": re.compile(r"\bstp_[A-Za-z0-9]{6,}"), "lea": re.compile(r"\blea_[A-Za-z0-9]{6,}"), "cfo": re.compile(r"\bcfo_[A-Za-z0-9]{6,}")}
FORBIDDEN_VARS = {"email", "firstName", "lastName", "picture", "phone", "linkedinUrl", "timezone", "jobTitle", "jobDescription",
                  "linkedinVideoUrl", "companyName", "companyDomain"}
STEP_VAR_KEYS = {"li_note": "liNote", "li_msg": "liMsg", "email1": ("email1Subject", "email1Body"), "email2": (None, "email2Body"),
                 "email3": ("email3Subject", "email3Body"), "email4": (None, "email4Body"), "email5": (None, "email5Body"),
                 "email1_subject_b": ("email1SubjectB", None)}


def _ids(payload: Any, prefix: str) -> list[str]:
    seen: list[str] = []
    for m in _ID[prefix].findall(json.dumps(payload, default=str)):
        if m not in seen:
            seen.append(m)
    return seen


def _state(conn: sqlite3.Connection, campaign_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT build_state_json FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    return json.loads(row["build_state_json"] or "{}") if row else {}


def _save(conn: sqlite3.Connection, campaign_id: int, st: dict[str, Any]) -> None:
    conn.execute("UPDATE campaigns SET build_state_json = ? WHERE id = ?", (json.dumps(st), campaign_id))


def _camp(conn: sqlite3.Connection, campaign_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if not row:
        raise FatalError(f"campaign {campaign_id} not found")
    return row


# ---------------------------------------------------------------- build
def create_campaign(conn: sqlite3.Connection, thesis_slug: str, arm: str, wave: int = 1, emoji: str = "🧭") -> tuple[int, int]:
    s = get_settings()
    seq = load_sequence(arm)
    t = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis_slug,)).fetchone()
    if not t:
        raise FatalError(f"thesis {thesis_slug} not registered")
    name = f"{s.campaign.get('name_prefix', 'VX |')} {thesis_slug} | W{wave} | {arm} | {today()}"
    cur = conn.execute(
        "INSERT INTO campaigns(name, thesis_id, wave, arm, sequence_version, senders_json, build_path, created_at, build_state_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (name, t["id"], wave, arm, seq["version"], json.dumps([s.persona.lemlist_user_id]), "api", utcnow(), json.dumps({"steps": {}, "branches": {}})))
    cid = int(cur.lastrowid)
    job = enqueue(conn, "lemlist", "create_campaign_with_sequence", {"name": name, "timezone": s.timezone, "emoji": emoji},
                  "lemlist.campaign_created", context={"campaign_id": cid}, est_credits=0, credit_type="lemlist")
    return cid, job


def _step_args(lemlist_id: str, sequence_id: str, step: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {"campaignId": lemlist_id, "sequenceId": sequence_id, "type": step["type"], "delay": int(step.get("delay", 0)), "userConfirmed": True}
    if step["type"] == "email":
        args["message"] = step["message"]
        if step.get("subject"):
            args["subject"] = step["subject"]
    elif step["type"] in ("linkedinInvite", "linkedinSend"):
        args["message"] = step.get("message", "")
    elif step["type"] == "conditional":
        args["conditionKey"] = step["conditionKey"]
        args["delayType"] = step.get("delayType", "waitUntil")
    return args


def _enqueue_steps(conn: sqlite3.Connection, campaign_id: int, lemlist_id: str, sequence_id: str, steps: list[dict[str, Any]], branch: str) -> list[int]:
    jobs = []
    for st in steps:
        clean = {k: v for k, v in st.items() if k not in ("accepted", "fallback")}
        jobs.append(enqueue(conn, "lemlist", "add_sequence_step", _step_args(lemlist_id, sequence_id, clean), "lemlist.step_added",
                            context={"campaign_id": campaign_id, "branch": branch, "step": clean}, est_credits=0, credit_type="lemlist"))
    return jobs


def _enqueue_sequences_read(conn: sqlite3.Connection, campaign_id: int, lemlist_id: str, phase: str) -> int:
    return enqueue(conn, "lemlist", "get_campaign_sequences", {"campaignId": lemlist_id}, "lemlist.sequences_read",
                   context={"campaign_id": campaign_id, "phase": phase}, est_credits=0, credit_type="lemlist")


@register("lemlist.campaign_created")
def h_campaign_created(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    cid = job["context"]["campaign_id"]
    cams, seqs = _ids(payload, "cam"), _ids(payload, "seq")
    if not cams:
        raise FatalError("create_campaign response carried no cam_ id")
    conn.execute("UPDATE campaigns SET lemlist_campaign_id = ?, lemlist_state = 'draft' WHERE id = ?", (cams[0], cid))
    st = _state(conn, cid)
    st["root_seq"] = seqs[0] if seqs else None
    _save(conn, cid, st)
    if st["root_seq"]:
        seq = load_sequence(_camp(conn, cid)["arm"])
        _enqueue_steps(conn, cid, cams[0], st["root_seq"], seq["steps"], "root")
    else:
        _enqueue_sequences_read(conn, cid, cams[0], "root")
    return {"rows": 1, "inserted": 1}


@register("lemlist.sequences_read")
def h_sequences_read(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    cid, phase = job["context"]["campaign_id"], job["context"].get("phase", "root")
    camp = _camp(conn, cid)
    entries = payload if isinstance(payload, list) else [payload]
    entry = next((e for e in entries if isinstance(e, dict) and (e.get("campaignId") == camp["lemlist_campaign_id"] or "sequences" in e)), None)
    sequences = (entry or {}).get("sequences") or []
    if not sequences:
        raise FatalError("no sequences in response")
    st = _state(conn, cid)
    st["root_seq"] = sequences[0]["id"]
    seq_cfg = load_sequence(camp["arm"])
    if phase == "root":
        _save(conn, cid, st)
        _enqueue_steps(conn, cid, camp["lemlist_campaign_id"], st["root_seq"], seq_cfg["steps"], "root")
        return {"rows": len(sequences), "inserted": 0}
    # branches: find the conditional step in the root sequence
    cond = next((s for s in sequences[0].get("steps", []) if s.get("type") == "conditional"), None)
    if not cond:
        raise FatalError("conditional step not found in root sequence")
    accepted = next((c["sequenceId"] for c in cond.get("conditions", []) if c.get("key") and not c.get("fallback")), None)
    fallback = next((c["sequenceId"] for c in cond.get("conditions", []) if c.get("fallback")), None)
    st["branches"] = {"accepted": accepted, "fallback": fallback}
    st["steps"]["root:cond_accept"] = {"step_id": cond.get("id"), "sequence_id": st["root_seq"]}
    _save(conn, cid, st)
    cond_cfg = next(s for s in seq_cfg["steps"] if s["type"] == "conditional")
    n = 0
    if accepted:
        n += len(_enqueue_steps(conn, cid, camp["lemlist_campaign_id"], accepted, cond_cfg.get("accepted", []), "accepted"))
    if fallback:
        n += len(_enqueue_steps(conn, cid, camp["lemlist_campaign_id"], fallback, cond_cfg.get("fallback", []), "fallback"))
    return {"rows": len(sequences), "inserted": n}


@register("lemlist.step_added")
def h_step_added(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    ctx = job["context"]
    cid, step, branch = ctx["campaign_id"], ctx["step"], ctx.get("branch", "root")
    camp = _camp(conn, cid)
    stps = _ids(payload, "stp")
    step_id = stps[-1] if stps else None
    st = _state(conn, cid)
    st.setdefault("steps", {})[f"{branch}:{step['key']}"] = {"step_id": step_id, "sequence_id": job["args"]["sequenceId"]}
    _save(conn, cid, st)
    if step_id and step.get("ab_no_note"):
        enqueue(conn, "lemlist", "set_ab_variant", {"sequenceId": job["args"]["sequenceId"], "stepId": step_id, "message": ""}, "lemlist.ab_set",
                context={"campaign_id": cid, "step_key": step["key"], "variant": "no_note"}, est_credits=0, credit_type="lemlist")
    if step_id and step.get("ab_subject_b"):
        enqueue(conn, "lemlist", "set_ab_variant", {"sequenceId": job["args"]["sequenceId"], "stepId": step_id, "subject": step["ab_subject_b"]}, "lemlist.ab_set",
                context={"campaign_id": cid, "step_key": step["key"], "variant": "subject_b"}, est_credits=0, credit_type="lemlist")
    if step["type"] == "conditional":
        _enqueue_sequences_read(conn, cid, camp["lemlist_campaign_id"], "branches")
    return {"rows": 1, "inserted": 1 if step_id else 0}


def _simple_handler(purpose: str, state_key: str):
    def _h(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
        cid = job["context"]["campaign_id"]
        st = _state(conn, cid)
        st.setdefault(state_key, []).append({"job": job["id"], "at": utcnow(), "args": {k: v for k, v in job["args"].items() if k != "campaignId"}})
        _save(conn, cid, st)
        return {"rows": 1, "inserted": 1}
    register(purpose)(_h)
    return _h


_simple_handler("lemlist.ab_set", "ab")
_simple_handler("lemlist.senders_set", "senders")
_simple_handler("lemlist.settings_set", "settings")
_simple_handler("lemlist.folder_moved", "folder")


def finalize(conn: sqlite3.Connection, campaign_id: int) -> list[int]:
    """Senders, reply behaviour, folder, readiness. Run after every step job is done."""
    s = get_settings()
    camp = _camp(conn, campaign_id)
    lid = camp["lemlist_campaign_id"]
    if not lid:
        raise FatalError("campaign has no Lemlist id yet")
    jobs = [
        enqueue(conn, "lemlist", "set_campaign_senders", {"campaignId": lid, "senderIds": json.loads(camp["senders_json"])}, "lemlist.senders_set",
                context={"campaign_id": campaign_id}, est_credits=0, credit_type="lemlist"),
        enqueue(conn, "lemlist", "update_settings", {"items": [{"type": "campaign", "id": lid, "settings": {
            "onReplied": {"campaignProgress": "stop", "propagateProgressToCompany": True, "createNewTask": True, "disableOutOfOffice": False},
            "onMeetingBooked": {"campaignProgress": "stop", "propagateProgressToCompany": True},
            "tracking": {"trackOpens": True, "trackClicks": True, "trackReplies": True},
            "aiFeatures": {"scoreReplies": True}}}]}, "lemlist.settings_set", context={"campaign_id": campaign_id}, est_credits=0, credit_type="lemlist"),
        enqueue(conn, "lemlist", "list_campaign_folders", {}, "lemlist.folders_read", context={"campaign_id": campaign_id, "folder": s.campaign.get("folder", "Vertex Engine")},
                est_credits=0, credit_type="lemlist"),
        enqueue(conn, "lemlist", "validate_campaign_readiness", {"campaignId": lid}, "lemlist.readiness", context={"campaign_id": campaign_id}, est_credits=0, credit_type="lemlist"),
    ]
    return jobs


@register("lemlist.folders_read")
def h_folders_read(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    cid, want = job["context"]["campaign_id"], job["context"]["folder"]
    camp = _camp(conn, cid)
    folders = payload.get("folders") if isinstance(payload, dict) else payload
    fid = None
    for f in folders or []:
        if isinstance(f, dict) and (f.get("name") or "").strip().lower() == want.lower():
            fid = f.get("id") or f.get("_id")
    if fid:
        enqueue(conn, "lemlist", "move_campaigns_to_folder", {"campaignIds": [camp["lemlist_campaign_id"]], "folderId": fid}, "lemlist.folder_moved",
                context={"campaign_id": cid}, est_credits=0, credit_type="lemlist")
    else:
        enqueue(conn, "lemlist", "create_campaign_folder", {"names": [want]}, "lemlist.folder_created", context={"campaign_id": cid, "folder": want},
                est_credits=0, credit_type="lemlist")
    return {"rows": len(folders or []), "inserted": int(bool(fid))}


@register("lemlist.folder_created")
def h_folder_created(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    cid = job["context"]["campaign_id"]
    camp = _camp(conn, cid)
    fids = _ids(payload, "cfo")
    if fids:
        enqueue(conn, "lemlist", "move_campaigns_to_folder", {"campaignIds": [camp["lemlist_campaign_id"]], "folderId": fids[0]}, "lemlist.folder_moved",
                context={"campaign_id": cid}, est_credits=0, credit_type="lemlist")
    return {"rows": 1, "inserted": int(bool(fids))}


@register("lemlist.readiness")
def h_readiness(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    cid = job["context"]["campaign_id"]
    conn.execute("UPDATE campaigns SET readiness_json = ? WHERE id = ?", (json.dumps({"checked_at": utcnow(), "result": payload}, default=str), cid))
    status = payload.get("status") if isinstance(payload, dict) else None
    if status and status != "ready":
        add_review_item(conn, "exception", "campaigns", cid, None, {"kind": "campaign_not_ready", "campaign_id": cid, "result": payload}, priority=2)
    return {"rows": 1, "inserted": 1}


@register("lemlist.campaign_details")
def h_campaign_details(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    entries = payload if isinstance(payload, list) else [payload]
    n = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        lid = e.get("_id") or e.get("id") or e.get("campaignId")
        status = e.get("status") or e.get("state")
        row = conn.execute("SELECT id, launched_at FROM campaigns WHERE lemlist_campaign_id = ?", (lid,)).fetchone()
        if not row:
            continue
        conn.execute("UPDATE campaigns SET lemlist_state = ? WHERE id = ?", (status, row["id"]))
        n += 1
        if status == "running" and not row["launched_at"]:
            conn.execute("UPDATE campaigns SET launched_at = ?, launched_by = 'detected' WHERE id = ?", (utcnow(), row["id"]))
            add_review_item(conn, "exception", "campaigns", row["id"], None, {"kind": "campaign_running_detected", "campaign_id": row["id"], "lemlist_id": lid,
                                                                            "note": "Campaign is running in Lemlist; enable the engagement-sync Routine."}, priority=1)
    return {"rows": len(entries), "inserted": n}


def plan_state_sync(conn: sqlite3.Connection) -> list[int]:
    ids = [r["lemlist_campaign_id"] for r in conn.execute("SELECT lemlist_campaign_id FROM campaigns WHERE lemlist_campaign_id IS NOT NULL")]
    jobs = []
    for i in range(0, len(ids), 20):
        jobs.append(enqueue(conn, "lemlist", "get_campaign_details", {"campaignIds": ids[i:i + 20]}, "lemlist.campaign_details", context={}, est_credits=0, credit_type="lemlist"))
    return jobs


# ---------------------------------------------------------------- enrollment
def to_html(body: str) -> str:
    paras = [p.strip() for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    return "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paras)


def lead_variables(conn: sqlite3.Connection, contact_id: int, sequence_version: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in conn.execute("SELECT step_key, subject, body, hook_type, variant_key, hook_signal_id FROM messages WHERE contact_id = ? AND sequence_version = ? AND status IN ('approved','edited')",
                          (contact_id, sequence_version)):
        spec = STEP_VAR_KEYS.get(m["step_key"])
        if not spec:
            continue
        if isinstance(spec, str):
            if m["body"]:
                out[spec] = m["body"].strip()
        else:
            skey, bkey = spec
            if skey and m["subject"]:
                out[skey] = m["subject"].strip()
            if bkey and m["body"]:
                out[bkey] = to_html(m["body"])
        if m["hook_type"]:
            out["hookType"] = m["hook_type"]
        if m["variant_key"]:
            out["vxVariant"] = m["variant_key"]
        if m["hook_signal_id"] and "hook" not in out:
            sig = conn.execute("SELECT text FROM signals WHERE id = ?", (m["hook_signal_id"],)).fetchone()
            if sig:
                out["hook"] = sig["text"]
    return {k: v for k, v in out.items() if v and k not in FORBIDDEN_VARS}


def build_lead(conn: sqlite3.Connection, enr: sqlite3.Row) -> dict[str, Any]:
    s = get_settings()
    ct = conn.execute("SELECT * FROM contacts WHERE id = ?", (enr["contact_id"],)).fetchone()
    co = conn.execute("SELECT * FROM companies WHERE id = ?", (enr["company_id"],)).fetchone()
    camp = _camp(conn, enr["campaign_id"])
    th = conn.execute("SELECT slug FROM theses WHERE id = ?", (camp["thesis_id"],)).fetchone()
    lead: dict[str, Any] = {"firstName": ct["first_name"], "lastName": ct["last_name"], "jobTitle": ct["title"], "linkedinUrl": ct["linkedin_url"],
                            "companyName": co["name"], "companyDomain": co["domain"], "contactOwner": s.persona.lemlist_user_id}
    if enr["channel_scope"] == "full" and ct["email"]:
        lead["email"] = ct["email"]
    loc = ", ".join(x for x in (co["hq_city"], co["hq_state"]) if x)
    if loc:
        lead["companyLocation"] = loc
    if co["linkedin_url"]:
        lead["companyLinkedinUrl"] = co["linkedin_url"]
    vars_ = lead_variables(conn, ct["id"], camp["sequence_version"])
    vars_.update({"vertical": co["vertical"] or "", "vxCompanyId": str(co["id"]), "vxContactId": str(ct["id"]), "vxTier": enr["tier_at_enroll"] or "",
                  "vxThesis": th["slug"] if th else "", "vxWave": str(camp["wave"]), "vxScope": enr["channel_scope"]})
    lead["customVariables"] = {k: v for k, v in vars_.items() if v}
    return {k: v for k, v in lead.items() if v not in (None, "")}


def plan_enrollment(conn: sqlite3.Connection, campaign_id: int, contact_ids: list[int], channel_scope: str = "full", batch_size: int = 50) -> dict[str, Any]:
    camp = _camp(conn, campaign_id)
    report: dict[str, Any] = {"queued": [], "blocked": {}, "held": {}, "jobs": []}
    for cid in contact_ids:
        if conn.execute("SELECT 1 FROM enrollments WHERE contact_id = ? AND campaign_id = ?", (cid, campaign_id)).fetchone():
            report["blocked"][cid] = "already enrolled in this campaign"
            continue
        ok, results, decision_id = check_enrollment(conn, cid, campaign_id, channel_scope)
        if not ok:
            failed = [r for r in results if not r.passed]
            if any(r.hold for r in failed) and all(r.passed or r.hold for r in results):
                report["held"][cid] = "; ".join(f"{r.name}: {r.detail}" for r in failed)
                add_review_item(conn, "exception", "contacts", cid, conn.execute("SELECT company_id FROM contacts WHERE id = ?", (cid,)).fetchone()[0],
                                {"kind": "enrollment_hold", "contact_id": cid, "campaign_id": campaign_id, "gates": [r.__dict__ for r in failed]}, priority=2)
            else:
                report["blocked"][cid] = "; ".join(f"{r.name}: {r.detail}" for r in failed)
            continue
        ct = conn.execute("SELECT company_id FROM contacts WHERE id = ?", (cid,)).fetchone()
        sc = conn.execute("SELECT id, tier FROM v_latest_score WHERE company_id = ? AND thesis_id = ?", (ct["company_id"], camp["thesis_id"])).fetchone()
        msg = conn.execute("SELECT variant_key, hook_type FROM messages WHERE contact_id = ? AND step_key = 'email1' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        conn.execute(
            "INSERT INTO enrollments(contact_id, campaign_id, company_id, variant_key, hook_type, channel_order, channel_scope, tier_at_enroll, score_id, decision_id, gate_results_json, state, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',?)",
            (cid, campaign_id, ct["company_id"], msg["variant_key"] if msg else None, msg["hook_type"] if msg else None, camp["arm"], channel_scope,
             sc["tier"] if sc else None, sc["id"] if sc else None, decision_id, results_json(results), utcnow()))
        report["queued"].append(cid)
    queued = conn.execute("SELECT * FROM enrollments WHERE campaign_id = ? AND state = 'queued' ORDER BY id", (campaign_id,)).fetchall()
    for i in range(0, len(queued), batch_size):
        chunk = queued[i:i + batch_size]
        leads = [build_lead(conn, e) for e in chunk]
        job = enqueue(conn, "lemlist", "add_leads_to_campaign",
                      {"campaignId": camp["lemlist_campaign_id"], "leads": leads, "deduplicate": True, "updateStrategy": "fillEmptyOnly"},
                      "lemlist.leads_pushed", context={"campaign_id": campaign_id, "enrollment_ids": [e["id"] for e in chunk]}, est_credits=0, credit_type="lemlist")
        report["jobs"].append(job)
    return report


def _lead_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("results", "leads", "data", "items"):
            v = payload.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


@register("lemlist.leads_pushed")
def h_leads_pushed(conn: sqlite3.Connection, job: dict[str, Any], payload: Any, batch_id: int | None = None, **kw: Any) -> dict[str, Any]:
    ctx = job["context"]
    enr_ids = ctx["enrollment_ids"]
    camp = _camp(conn, ctx["campaign_id"])
    rows = _lead_rows(payload)
    by_email: dict[str, dict[str, Any]] = {}
    by_li: dict[str, dict[str, Any]] = {}
    for r in rows:
        lead = r.get("lead") if isinstance(r.get("lead"), dict) else r
        e = (lead.get("email") or r.get("email") or "").lower()
        li = (lead.get("linkedinUrl") or r.get("linkedinUrl") or "").lower().rstrip("/")
        if e:
            by_email[e] = r
        if li:
            by_li[li] = r
    pushed = skipped = 0
    for idx, eid in enumerate(enr_ids):
        enr = conn.execute("SELECT e.*, c.email, c.linkedin_url FROM enrollments e JOIN contacts c ON c.id = e.contact_id WHERE e.id = ?", (eid,)).fetchone()
        r = by_email.get((enr["email"] or "").lower()) or by_li.get((enr["linkedin_url"] or "").lower().rstrip("/")) or (rows[idx] if idx < len(rows) and len(rows) == len(enr_ids) else None)
        outcome = (r or {}).get("outcome") or (r or {}).get("status") or ("added" if r and _ids(r, "lea") else "unknown")
        lead_ids = _ids(r, "lea") if r else []
        if outcome in ("added", "created", "success", "ok") and lead_ids:
            conn.execute("UPDATE enrollments SET state = 'pushed', lemlist_lead_id = ?, pushed_at = ?, push_outcome = ? WHERE id = ?", (lead_ids[0], utcnow(), outcome, eid))
            advance_if_behind(conn, camp["thesis_id"], enr["company_id"], "Ready for Outreach", "lead pushed to draft campaign", "system")
            pushed += 1
        else:
            conn.execute("UPDATE enrollments SET state = 'stopped', push_outcome = ?, pushed_at = ? WHERE id = ?", (outcome, utcnow(), eid))
            skipped += 1
            if outcome in ("skippedDuplicateCrossCampaign", "skippedAlreadyInCampaign") and enr["email"]:
                add_suppression(conn, "email", enr["email"].lower(), "prior_outreach", "lemlist_dedup", expires_at=cooldown(int(get_settings().campaign.get("cooldown_days_prior_outreach", 180))))
    return {"rows": len(rows), "inserted": pushed, "skipped": skipped}


def launch_instructions(conn: sqlite3.Connection, campaign_id: int) -> str:
    camp = _camp(conn, campaign_id)
    n = conn.execute("SELECT COUNT(*) FROM enrollments WHERE campaign_id = ? AND state = 'pushed'", (campaign_id,)).fetchone()[0]
    rd = json.loads(camp["readiness_json"] or "{}").get("result", {})
    return (f"Campaign {camp['name']} ({camp['lemlist_campaign_id']}) has {n} pushed lead(s); readiness: {rd.get('status', 'not checked')}.\n"
            f"The engine never launches. To launch it yourself, run in Lemlist or ask Claude to call:\n"
            f"  launch_campaign(campaignId=\"{camp['lemlist_campaign_id']}\")\n"
            f"then record it with: vertex campaign mark-launched {campaign_id}")


def mark_launched(conn: sqlite3.Connection, campaign_id: int, by: str = "human") -> None:
    conn.execute("UPDATE campaigns SET launched_at = ?, launched_by = ?, lemlist_state = 'running' WHERE id = ?", (utcnow(), by, campaign_id))
    for e in conn.execute("SELECT id, company_id FROM enrollments WHERE campaign_id = ? AND state = 'pushed'", (campaign_id,)).fetchall():
        conn.execute("UPDATE enrollments SET state = 'active' WHERE id = ?", (e["id"],))

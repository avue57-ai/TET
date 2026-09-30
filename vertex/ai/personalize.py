"""Personalization: hook selection, variant assignment, copy generation, lint, critic, storage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from vertex.ai.claude import prompt_version, run_prompt
from vertex.ai.qa import lint_step, similarity
from vertex.ai.signals import best_hooks
from vertex.core.thesis import Thesis
from vertex.db.connection import utcnow
from vertex.settings import get_settings
from vertex.utils.logging import get_logger

log = get_logger("personalize")

VARIANT_AXES = {
    "opener": ["hook-first: the first sentence is the hook itself", "context-first: one sentence of who we are, then the hook"],
    "cta": ["ask for a 15-minute call", "ask 'worth a short conversation at some point?'"],
    "positioning": ["describe the buyer as a permanent-capital group", "describe the buyer as a family-office-backed holding group"],
    "length": ["email 1 at 60-80 words", "email 1 at 90-110 words"],
}
WAVE1_EXPERIMENT_AXIS = "opener"   # one variable at a time


class CopySet(BaseModel):
    li_note: str
    li_msg: str
    email1_subject: str
    email1_body: str
    email1_subject_b: str = Field(default="", description="alternative subject for native A/B on step 1")
    email2_body: str
    email3_subject: str
    email3_body: str
    email4_body: str
    email5_body: str


class Critique(BaseModel):
    sounds_ai: bool = False
    vague_compliment: bool = False
    pressure: bool = False
    claims_not_in_hook: bool = False
    sensitive_theme: bool = False
    too_long: bool = False
    score: int = Field(default=3, ge=1, le=5)
    fix: str = ""


def assign_variant(experiment_id: str, domain: str, arms: int = 2) -> int:
    return int(hashlib.sha1(f"{experiment_id}:{domain}".encode()).hexdigest(), 16) % arms


def variant_key(domain: str, experiment_id: str = "wave1_opener") -> tuple[str, str]:
    arm = assign_variant(experiment_id, domain)
    instr = VARIANT_AXES[WAVE1_EXPERIMENT_AXIS][arm]
    defaults = {k: v[0] for k, v in VARIANT_AXES.items() if k != WAVE1_EXPERIMENT_AXIS}
    text = "; ".join([instr, *defaults.values()])
    return f"O{arm + 1}-C1-P1-L1", text


def load_sequence(arm: str) -> dict[str, Any]:
    fn = {"linkedin_led": "arm_a_linkedin_led.yaml", "email_first": "arm_b_email_first.yaml"}[arm]
    with open(Path(get_settings().config_dir) / "sequences" / fn, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def company_summary(row: sqlite3.Row) -> str:
    d = (row["description"] or "").strip()
    return d[:400] if d else f"{row['name']} ({row['industry'] or 'software'})"


def generate_for_contact(conn: sqlite3.Connection, contact_id: int, thesis: Thesis, arm: str = "linkedin_led",
                         critic: bool = True) -> dict[str, Any]:
    ct = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    co = conn.execute("SELECT * FROM companies WHERE id = ?", (ct["company_id"],)).fetchone()
    hooks = best_hooks(conn, co["id"], n=2)
    if not hooks:
        return {"status": "no_hook", "contact_id": contact_id}
    seq = load_sequence(arm)
    vkey, vinstr = variant_key(co["domain"])
    payload = {
        "first_name": ct["first_name"] or "there", "title": ct["title"] or "owner", "company_name": co["name"],
        "hq": ", ".join(x for x in (co["hq_city"], co["hq_state"]) if x), "company_summary": company_summary(co),
        "hook_type": hooks[0]["hook_type"], "hook_text": hooks[0]["text"],
        "backup_hook_text": hooks[1]["text"] if len(hooks) > 1 else "",
        "variant_instructions": vinstr, "step_purposes": seq["step_purposes"],
    }
    copy = run_prompt(conn, "personalize", payload, CopySet)
    allowed_numbers = set()
    for h in hooks:
        allowed_numbers |= set(__import__("re").findall(r"\b\d[\d,\.]*\b", h["text"] + " " + (h["evidence_quote"] or "")))
    steps = {
        "li_note": (None, copy.li_note), "li_msg": (None, copy.li_msg),
        "email1": (copy.email1_subject, copy.email1_body), "email2": (None, copy.email2_body),
        "email3": (copy.email3_subject, copy.email3_body), "email4": (None, copy.email4_body), "email5": (None, copy.email5_body),
    }
    if copy.email1_subject_b:
        steps["email1_subject_b"] = (copy.email1_subject_b, "")
    lint: dict[str, list[str]] = {}
    for key, (subj, body) in steps.items():
        if key == "email1_subject_b":
            continue
        lint[key] = lint_step(key, subj, body, hooks[0]["text"], allowed_numbers)
    # cross-company templating check on email1
    others = conn.execute("SELECT body FROM messages WHERE step_key = 'email1' AND company_id != ? ORDER BY id DESC LIMIT 40", (co["id"],)).fetchall()
    if any(similarity(copy.email1_body, o["body"]) > 0.6 for o in others):
        lint["email1"].append("templated_vs_other_company")
    crit = None
    if critic:
        msgs = "\n\n".join(f"[{k}] subject: {s or ''}\n{b}" for k, (s, b) in steps.items())
        crit = run_prompt(conn, "copy_critic", {"company_name": co["name"], "hook_type": hooks[0]["hook_type"],
                                                 "hook_text": hooks[0]["text"], "messages": msgs}, Critique)
    flags_total = sum(len(v) for v in lint.values())
    status = "draft" if flags_total == 0 and (crit is None or (crit.score >= 4 and not crit.sensitive_theme and not crit.claims_not_in_hook)) else "needs_edit"
    pv = prompt_version("personalize")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM messages WHERE contact_id = ? AND sequence_version = ? AND status IN ('draft','needs_edit')", (contact_id, seq["version"]))
        for key, (subj, body) in steps.items():
            channel = "linkedin" if key.startswith("li_") else "email"
            conn.execute(
                "INSERT INTO messages(contact_id, company_id, sequence_version, step_key, channel, variant_key, hook_signal_id, hook_type, subject, body, prompt_version, lint_flags_json, critic_score, critic_flags_json, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (contact_id, co["id"], seq["version"], key, channel, vkey, hooks[0]["id"], hooks[0]["hook_type"], subj, body, pv,
                 json.dumps(lint.get(key, [])), crit.score if crit else None, crit.model_dump_json() if crit else None, status, utcnow(), utcnow()),
            )
        conn.execute("UPDATE signals SET used_in_outreach = 1 WHERE id = ?", (hooks[0]["id"],))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"status": status, "contact_id": contact_id, "variant": vkey, "lint": {k: v for k, v in lint.items() if v},
            "critic": crit.model_dump() if crit else None}

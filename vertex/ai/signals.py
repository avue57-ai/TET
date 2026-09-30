"""Signal extraction: evidence-quoted, source-tagged facts for scoring and personalization."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from vertex.ai.claude import run_prompt
from vertex.core.thesis import Thesis
from vertex.db.connection import utcnow
from vertex.integrations.inven import latest_inven_extras
from vertex.utils.logging import get_logger

log = get_logger("signals")

HOOK_TYPES = {"capability", "product_niche", "customer_base", "end_market", "geography", "growth", "mission_critical",
              "thesis_fit", "award", "marquee_project", "expansion", "credential", "longevity", "team_tenure"}


class Signal(BaseModel):
    hook_type: str
    text: str
    evidence_quote: str
    source_tag: str
    evidence_url: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    safe_to_cite: bool = True
    banned_theme: bool = False
    observed_date: str | None = None


class SignalSet(BaseModel):
    signals: list[Signal] = Field(default_factory=list)


def _blocks(conn: sqlite3.Connection, row: sqlite3.Row, extras: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """(tag, text, url)"""
    out: list[tuple[str, str, str | None]] = []
    if row["description"]:
        out.append(("inven_description", row["description"], row["inven_url"]))
    kws = extras.get("keywords") or []
    if kws:
        out.append(("inven_keywords", ", ".join(str(k) for k in kws[:40]), row["inven_url"]))
    news = extras.get("news")
    if news:
        out.append(("inven_news", json.dumps(news, ensure_ascii=False, default=str)[:3000], row["inven_url"]))
    ap = conn.execute("SELECT payload_json FROM source_records WHERE company_id = ? AND source = 'apollo_org' ORDER BY id DESC LIMIT 1",
                      (row["id"],)).fetchone()
    if ap:
        p = json.loads(ap[0])
        txt = " ".join(str(p.get(k) or "") for k in ("short_description", "seo_description"))
        if txt.strip():
            out.append(("apollo_org", txt.strip()[:2000], p.get("website_url")))
        if p.get("keywords"):
            out.append(("apollo_keywords", ", ".join(map(str, p["keywords"]))[:800], None))
    for r in conn.execute("SELECT payload_json FROM source_records WHERE company_id = ? AND source = 'websearch' ORDER BY id", (row["id"],)):
        p = json.loads(r[0])
        summ = p.get("summary") or p.get("text") or ""
        links = p.get("links") or []
        url = links[0].get("url") if links and isinstance(links[0], dict) else (links[0] if links else None)
        if summ:
            out.append((f"websearch_summary:{p.get('kind', 'search')}", str(summ)[:3500], url))
    return out


def build_material(blocks: list[tuple[str, str, str | None]]) -> str:
    return "\n\n".join(f"[SOURCE:{tag}]\n{text}" for tag, text, _ in blocks)


def _freshness(date: str | None) -> str:
    if not date:
        return "undated"
    try:
        d = datetime.fromisoformat(date[:10]).replace(tzinfo=UTC)
    except ValueError:
        return "undated"
    days = (datetime.now(UTC) - d).days
    return "fresh" if days <= 30 else "recent" if days <= 90 else "aging" if days <= 180 else "stale"


def extract_signals(conn: sqlite3.Connection, company_id: int, thesis: Thesis) -> dict[str, int]:
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    extras = latest_inven_extras(conn, company_id)
    blocks = _blocks(conn, row, extras)
    if not blocks:
        return {"signals": 0, "rejected": 0}
    material = build_material(blocks)
    vdesc = next((v.description for v in thesis.verticals if v.key == row["vertical"]), thesis.verticals[0].description)
    out = run_prompt(conn, "extract_signals", {
        "company_name": row["name"], "domain": row["domain"],
        "hq": ", ".join(x for x in (row["hq_city"], row["hq_state"]) if x), "vertical_description": vdesc,
        "material": material,
    }, SignalSet)
    by_tag = {tag: " ".join(text.lower().split()) for tag, text, _ in blocks}
    urls = {tag: url for tag, text, url in blocks}
    kept = rejected = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for s in out.signals:
            tag = s.source_tag.strip()
            q = " ".join(s.evidence_quote.lower().split())
            src_text = by_tag.get(tag) or ""
            if not q or q not in src_text:
                # try any block (model may mislabel the tag)
                hit = next((t for t, txt in by_tag.items() if q and q in txt), None)
                if not hit:
                    rejected += 1
                    continue
                tag = hit
            summary_only = tag.startswith("websearch_summary")
            conf = min(float(s.confidence), 0.6 if summary_only else 0.85)
            safe = bool(s.safe_to_cite) and not s.banned_theme
            htype = s.hook_type if s.hook_type in HOOK_TYPES else "thesis_fit"
            h = hashlib.sha1(f"{company_id}:{s.text.strip().lower()}".encode()).hexdigest()[:16]
            dup = conn.execute("SELECT id FROM signals WHERE company_id = ? AND text = ?", (company_id, s.text.strip())).fetchone()
            if dup:
                continue
            conn.execute(
                "INSERT INTO signals(company_id, hook_type, text, evidence_quote, evidence_source, evidence_url, confidence, safe_to_cite, banned_theme, freshness, observed_date, captured_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (company_id, htype, s.text.strip(), s.evidence_quote.strip()[:600], tag.split(":")[0], urls.get(tag),
                 conf, int(safe), int(bool(s.banned_theme)), _freshness(s.observed_date), s.observed_date, utcnow()),
            )
            kept += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"signals": kept, "rejected": rejected}


def best_hooks(conn: sqlite3.Connection, company_id: int, n: int = 2, min_conf: float = 0.5) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM signals WHERE company_id = ? AND safe_to_cite = 1 AND banned_theme = 0 AND (confidence >= ? OR human_confirmed = 1) "
        "AND freshness != 'stale' ORDER BY human_confirmed DESC, confidence DESC, id LIMIT ?",
        (company_id, min_conf, n),
    ).fetchall()

"""Deterministic copy lint: banned themes/phrases, lengths, dashes, CTAs, anonymity, number parroting, templating."""

from __future__ import annotations

import re
from pathlib import Path

from vertex.settings import get_settings

_LIMITS = {  # (min_words, max_words) or chars for li_note
    "li_msg": (10, 90), "email1": (60, 110), "email2": (30, 60), "email3": (50, 90), "email4": (1, 25), "email5": (30, 50),
}
_DASH = re.compile(r"—|–|\s-\s")
_BUYER_NAMES = re.compile(r"\b(elmore|emergence|family office of|our client [A-Z][a-z]+ [A-Z][a-z]+)\b", re.I)
_PLACEHOLDER = re.compile(r"\{\{|\}\}|\[(name|company|first name)\]", re.I)
_CALL_LENGTH = re.compile(r"\b\d+[- ]min(?:ute)?s?\b", re.I)   # the CTA's "15-minute call" is not a parroted fact


def _load_banned() -> list[re.Pattern[str]]:
    path = Path(get_settings().config_dir) / "banned_phrases.txt"
    pats = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pats.append(re.compile(line, re.I))
    return pats


_BANNED = None


def banned_hits(text: str) -> list[str]:
    global _BANNED
    if _BANNED is None:
        _BANNED = _load_banned()
    hits = []
    for p in _BANNED:
        m = p.search(text)
        if m:
            hits.append(m.group(0))
    return hits


def words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9'’]+", text))


def shingles(text: str, k: int = 4) -> set[str]:
    toks = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(toks[i : i + k]) for i in range(max(0, len(toks) - k + 1))}


def similarity(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def lint_step(key: str, subject: str | None, body: str, hook_text: str, allowed_numbers: set[str]) -> list[str]:
    flags: list[str] = []
    text = f"{subject or ''}\n{body}"
    for hit in banned_hits(text):
        flags.append(f"banned:{hit}")
    if _DASH.search(text):
        flags.append("dash_punctuation")
    if "!" in text:
        flags.append("exclamation")
    if _PLACEHOLDER.search(text):
        flags.append("unresolved_placeholder")
    if _BUYER_NAMES.search(text):
        flags.append("buyer_named")
    if key == "li_note":
        if len(body) > 180:
            flags.append(f"too_long:{len(body)}chars")
        if "?" in body and body.count("?") > 1:
            flags.append("li_note_pitchy")
    elif key in _LIMITS:
        lo, hi = _LIMITS[key]
        n = words(body)
        if n > hi:
            flags.append(f"too_long:{n}w")
        if n < lo:
            flags.append(f"too_short:{n}w")
    if key == "email1":
        if body.count("?") != 1:
            flags.append(f"cta_count:{body.count('?')}")
        if not subject or not (2 <= len(subject.split()) <= 7):
            flags.append("subject_length")
        # hook reference: token overlap with the hook sentence
        ht = {t for t in re.findall(r"[a-z]{5,}", hook_text.lower())}
        bt = {t for t in re.findall(r"[a-z]{5,}", body.lower())}
        if ht and len(ht & bt) / len(ht) < 0.15:
            flags.append("hook_not_referenced")
    if key in ("email2", "email4", "email5") and subject:
        flags.append("subject_should_be_empty")
    for num in re.findall(r"\b\d[\d,\.]*\b", _CALL_LENGTH.sub("", body)):
        if num not in allowed_numbers and len(num.replace(",", "")) >= 2:
            flags.append(f"number_parroted:{num}")
    return flags

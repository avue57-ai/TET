"""Normalization for the identifiers dedup relies on: domains, names, LinkedIn URLs, emails, states."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)

_LEGAL_SUFFIXES = {
    "inc", "inc.", "incorporated", "llc", "l.l.c.", "ltd", "ltd.", "limited", "corp", "corp.", "corporation",
    "co", "co.", "company", "plc", "gmbh", "sa", "s.a.", "srl", "pty", "llp", "lp", "pc", "p.c.", "pllc",
    "holdings", "group", "the",
}

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
_STATE_CODES = set(US_STATES.values())


def normalize_domain(value: str | None) -> str | None:
    """Return the registrable domain in lowercase (IDNA-encoded), or None.

    'https://WWW.Example.com/about?x=1' -> 'example.com'; 'sub.example.co.uk' -> 'example.co.uk'.
    Path-only or empty values return None. A subdomain other than www is dropped, which is what we want for
    a company identity key (the alias table keeps the original when it matters).
    """
    if not value:
        return None
    v = value.strip().lower()
    if not v or "@" in v:
        return None
    if "://" not in v:
        v = "http://" + v
    try:
        host = urlparse(v).hostname or ""
    except ValueError:
        return None
    host = host.strip(".")
    if not host or "." not in host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    ext = _extract(host)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


def normalize_name(value: str | None) -> str | None:
    """Lowercase, strip accents, punctuation and legal suffixes, collapse whitespace."""
    if not value:
        return None
    v = unicodedata.normalize("NFKD", value)
    v = "".join(ch for ch in v if not unicodedata.combining(ch))
    v = v.lower().replace("&", " and ")
    v = re.sub(r"[^a-z0-9. ]+", " ", v)
    tokens = [t for t in v.split() if t not in _LEGAL_SUFFIXES]
    tokens = [t.strip(".") for t in tokens]
    tokens = [t for t in tokens if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens) or None


def normalize_state(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if len(v) == 2 and v.upper() in _STATE_CODES:
        return v.upper()
    return US_STATES.get(v.lower())


def normalize_linkedin(value: str | None) -> str | None:
    """Canonical LinkedIn URL: https://www.linkedin.com/<in|company>/<slug> (lowercase, no query, no trailing slash)."""
    if not value:
        return None
    v = value.strip()
    if "linkedin.com" not in v.lower():
        return None
    if "://" not in v:
        v = "https://" + v
    parsed = urlparse(v)
    path = parsed.path.rstrip("/")
    m = re.search(r"/(in|company|sales/(?:lead|company))/([^/?#]+)", path, flags=re.IGNORECASE)
    if not m:
        return None
    kind, slug = m.group(1).lower(), m.group(2).lower()
    if kind.startswith("sales/"):
        return f"https://www.linkedin.com/{kind}/{slug}"
    return f"https://www.linkedin.com/{kind}/{slug}"


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if "@" not in v or v.startswith("@") or v.endswith("@"):
        return None
    return v


def name_state_key(name: str | None, state: str | None) -> str | None:
    n = normalize_name(name)
    if not n:
        return None
    s = normalize_state(state) or ""
    return f"{n}|{s}"

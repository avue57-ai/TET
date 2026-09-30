"""Thesis loading, validation, and translation into Inven query specs (six families beyond SIC/NAICS)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from vertex.db.connection import utcnow
from vertex.db.repo import upsert_thesis
from vertex.settings import get_settings

QueryKind = Literal["taxonomy", "product", "end_market", "business_model", "lookalike", "negative_space", "semantic"]


class Vertical(BaseModel):
    key: str
    description: str
    inclusion_terms: list[str] = Field(default_factory=list)
    product_terms: list[str] = Field(default_factory=list)
    end_market_terms: list[str] = Field(default_factory=list)
    customer_reference_terms: list[str] = Field(default_factory=list)
    business_model_indicators: list[str] = Field(default_factory=list)
    exemplar_companies: list[str] = Field(default_factory=list)
    false_positive_terms: list[str] = Field(default_factory=list)
    known_consolidators: list[str] = Field(default_factory=list)
    priors: dict[str, float] = Field(default_factory=dict)


class Thesis(BaseModel):
    slug: str
    name: str
    buyer_positioning: str
    size: dict[str, Any]
    geography: dict[str, Any]
    ownership: dict[str, Any]
    clarifications: dict[str, str] = Field(default_factory=dict)
    founded_before: int | None = None
    growth: dict[str, Any] = Field(default_factory=dict)
    customer_base: dict[str, Any] = Field(default_factory=dict)
    recurring_revenue: dict[str, Any] = Field(default_factory=dict)
    mission_critical: dict[str, Any] = Field(default_factory=dict)
    max_new_companies_per_week: int = 300
    max_rows_per_query: int = 600
    priors: dict[str, float] = Field(default_factory=dict)
    verticals: list[Vertical]

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not v or not all(ch.isalnum() or ch == "_" for ch in v):
            raise ValueError("slug must be alphanumeric/underscore")
        return v

    def vertical(self, key: str) -> Vertical:
        for v in self.verticals:
            if v.key == key:
                return v
        raise KeyError(key)

    def prior(self, vertical: Vertical, name: str, default: float) -> float:
        return float(vertical.priors.get(name, self.priors.get(name, default)))


def thesis_path(slug: str) -> Path:
    return get_settings().config_dir / "theses" / f"{slug}.yaml"


def load_thesis(slug_or_path: str) -> Thesis:
    path = Path(slug_or_path)
    if not path.exists():
        path = thesis_path(slug_or_path)
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    try:
        return Thesis(**raw)
    except ValidationError as ex:
        raise ValueError(f"invalid thesis {path.name}: {ex}") from ex


def register_thesis(conn: sqlite3.Connection, thesis: Thesis) -> int:
    tid, _ = upsert_thesis(conn, thesis.slug, thesis.name, thesis.model_dump(mode="json"))
    return tid


# ---------------------------------------------------------------- query compilation

def _size_clause(t: Thesis) -> str:
    emp = t.size.get("employees") or [25, 250]
    rev = t.size.get("revenue_usd")
    parts = [f"{int(emp[0])} to {int(emp[1])} employees on LinkedIn"]
    if rev:
        parts.append(f"estimated annual revenue between ${int(rev[0]) / 1e6:g} million and ${int(rev[1]) / 1e6:g} million")
    return ", ".join(parts)


def _geo_clause(t: Thesis) -> str:
    countries = t.geography.get("countries") or ["US"]
    states = t.geography.get("states") or []
    txt = "headquartered in the United States" if countries == ["US"] else "headquartered in " + ", ".join(countries)
    if states:
        txt += f" ({', '.join(states)})"
    return txt


def _ownership_clause(t: Thesis) -> str:
    c = t.clarifications
    base = ("privately held and independently owned by its founders, a family or its management; "
            "not a subsidiary or division of another company; not publicly listed. Ownership strictness: exclude any "
            "company with known institutional investment (private equity, venture capital, growth equity, search fund "
            "or family-office sponsor) and include privately held companies whose ownership is unknown")
    if c.get("no_vc_funding"):
        base += f". No VC funding means: {c['no_vc_funding']}"
    return base


def _founded_clause(t: Thesis) -> str:
    # Inven's founding-year coverage is ~30%, so a founded filter in the search would drop most of the universe.
    # The founded rule is applied by screen_rules only when the year is known.
    return ""


def _join(items: list[str], n: int) -> str:
    return ", ".join(items[:n])


def compile_queries(t: Thesis, vertical: Vertical, families: list[str] | None = None) -> list[tuple[QueryKind, str]]:
    """Return (kind, description) pairs for Inven build_company_search."""
    size, geo, own, founded = _size_clause(t), _geo_clause(t), _ownership_clause(t), _founded_clause(t)
    tail = f" Companies {geo}, with {size}, {founded + ', ' if founded else ''}{own}."
    fp = f" Exclude {_join(vertical.false_positive_terms, 6)}." if vertical.false_positive_terms else ""
    out: list[tuple[QueryKind, str]] = []
    out.append(("taxonomy", f"{vertical.description} Industry keywords: {_join(vertical.inclusion_terms, 10)}.{tail}{fp}"))
    if vertical.product_terms:
        out.append(("product", (
            f"Software companies whose websites describe products or features such as {_join(vertical.product_terms, 10)} "
            f"for {_join(vertical.end_market_terms, 5)}.{tail}{fp}")))
    if vertical.end_market_terms:
        out.append(("end_market", (
            f"Software vendors that serve {_join(vertical.end_market_terms, 8)}; websites reference customers with phrases "
            f"like {_join(vertical.customer_reference_terms, 4)}; products in {_join(vertical.inclusion_terms, 5)}.{tail}{fp}")))
    if vertical.business_model_indicators:
        out.append(("business_model", (
            f"{vertical.description} Business model indicators on the website: {_join(vertical.business_model_indicators, 8)}; "
            f"recurring revenue from {', '.join(t.recurring_revenue.get('forms', [])[:4])}.{tail}{fp}")))
    if vertical.exemplar_companies:
        out.append(("lookalike", (
            f"Companies similar to {', '.join(vertical.exemplar_companies[:5])}: {vertical.description}{tail}{fp}")))
    out.append(("negative_space", (
        f"{vertical.description} Keywords: {_join(vertical.inclusion_terms, 6)}. Not a reseller, not a staffing firm, "
        f"not a consultancy, not an agency, not a hardware manufacturer, not PE-backed, not venture-backed.{tail}")))
    if families:
        out = [q for q in out if q[0] in families]
    return out


def ensure_source_queries(conn: sqlite3.Connection, thesis_id: int, vertical_key: str,
                          queries: list[tuple[QueryKind, str]]) -> list[int]:
    ids: list[int] = []
    for kind, desc in queries:
        row = conn.execute(
            "SELECT id FROM source_queries WHERE thesis_id = ? AND vertical = ? AND query_kind = ? AND description = ?",
            (thesis_id, vertical_key, kind, desc),
        ).fetchone()
        if row:
            ids.append(int(row["id"]))
            continue
        cur = conn.execute(
            "INSERT INTO source_queries(thesis_id, vertical, query_kind, description, status, created_at) VALUES (?,?,?,?, 'planned', ?)",
            (thesis_id, vertical_key, kind, desc, utcnow()),
        )
        ids.append(int(cur.lastrowid))
    return ids

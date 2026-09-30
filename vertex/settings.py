"""Settings: YAML config + environment secrets. Secrets are never logged or persisted."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Persona(BaseModel):
    name: str
    first_name: str
    title: str
    firm: str
    phone: str
    mailbox: str
    lemlist_user_id: str
    linkedin_user_id: str
    signature_owner: str

    def coherent(self) -> bool:
        return self.signature_owner == self.name and self.lemlist_user_id == self.linkedin_user_id


class MailboxCfg(BaseModel):
    daily_limit: int
    legacy_usage_estimate: int = 0
    engine_allocation: int = 0
    enabled: bool = False


class Budgets(BaseModel):
    inven_ai_credits_per_run: int = 150
    inven_export_rows_per_run: int = 600
    apollo_credits_per_run: int = 400
    apollo_org_enrich_credits_per_run: int = 300
    llm_usd_per_run: float = 15.0
    websearch_queries_per_run: int = 200


class Settings(BaseModel):
    timezone: str = "America/Detroit"
    persona: Persona
    mailboxes: dict[str, MailboxCfg] = Field(default_factory=dict)
    linkedin_limits: dict[str, Any] = Field(default_factory=dict)
    campaign: dict[str, Any] = Field(default_factory=dict)
    budgets: Budgets = Field(default_factory=Budgets)
    apollo: dict[str, Any] = Field(default_factory=dict)
    bridge: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] = Field(default_factory=dict)
    scoring: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)
    routines: dict[str, Any] = Field(default_factory=dict)

    # Derived paths
    config_dir: Path = PROJECT_ROOT / "config"
    db_path: Path = PROJECT_ROOT / "data" / "vertex.db"
    data_dir: Path = PROJECT_ROOT / "data"

    # --- secrets (read from env only, never serialized) ---
    @property
    def lemlist_api_key(self) -> str | None:
        return os.environ.get("LEMLIST_API_KEY") or None

    @property
    def apollo_api_key(self) -> str | None:
        return os.environ.get("APOLLO_API_KEY") or None

    @property
    def inven_api_key(self) -> str | None:
        return os.environ.get("INVEN_API_KEY") or None

    @property
    def anthropic_api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY") or None

    def backend_for(self, provider: str) -> str:
        """Return 'key' when a provider secret exists, else 'bridge'."""
        key = {
            "lemlist": self.lemlist_api_key,
            "apollo": self.apollo_api_key,
            "inven": self.inven_api_key,
        }.get(provider)
        return "key" if key else "bridge"

    def redacted(self) -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude={"config_dir", "db_path", "data_dir"})
        d["secrets_present"] = {
            "LEMLIST_API_KEY": bool(self.lemlist_api_key),
            "APOLLO_API_KEY": bool(self.apollo_api_key),
            "INVEN_API_KEY": bool(self.inven_api_key),
            "ANTHROPIC_API_KEY": bool(self.anthropic_api_key),
        }
        return d


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    config_dir = Path(os.environ.get("VERTEX_CONFIG_DIR") or PROJECT_ROOT / "config")
    if not config_dir.is_absolute():
        config_dir = PROJECT_ROOT / config_dir
    with open(config_dir / "settings.yaml", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    raw["config_dir"] = config_dir
    db_path = Path(os.environ.get("VERTEX_DB_PATH") or PROJECT_ROOT / "data" / "vertex.db")
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    raw["db_path"] = db_path
    raw["data_dir"] = db_path.parent
    if os.environ.get("VERTEX_TZ"):
        raw["timezone"] = os.environ["VERTEX_TZ"]
    return Settings(**raw)


def reset_settings_cache() -> None:
    get_settings.cache_clear()

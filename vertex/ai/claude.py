"""Headless Claude harness: prompt template + JSON schema -> validated pydantic object, cached and cost-logged.

Backend 'claude_cli' shells out to `claude -p --output-format json`. Backend 'anthropic_sdk' is used when
ANTHROPIC_API_KEY exists. Every call is cached in llm_calls by (prompt_name, prompt_version, input_hash).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, TypeVar

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ValidationError

from vertex.db.connection import utcnow
from vertex.settings import get_settings
from vertex.utils.errors import FatalError, RetryableError
from vertex.utils.logging import get_logger

log = get_logger("llm")
PROMPTS_DIR = Path(__file__).parent / "prompts"
T = TypeVar("T", bound=BaseModel)

_env = Environment(loader=FileSystemLoader(str(PROMPTS_DIR)), undefined=StrictUndefined, autoescape=False,
                   trim_blocks=True, lstrip_blocks=True)


def prompt_version(prompt_name: str) -> str:
    text = (PROMPTS_DIR / f"{prompt_name}.md").read_text(encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def render(prompt_name: str, payload: dict[str, Any], schema_model: type[BaseModel]) -> str:
    tmpl = _env.get_template(f"{prompt_name}.md")
    schema = schema_model.model_json_schema()
    body = tmpl.render(**payload, payload_json=json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return (
        f"{body}\n\n"
        "OUTPUT CONTRACT: respond with a single JSON object and nothing else (no prose, no code fences) that validates "
        f"against this JSON schema:\n{json.dumps(schema, ensure_ascii=False)}\n"
    )


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _call_claude_cli(prompt: str, model: str | None, timeout: int) -> tuple[str, float, str | None]:
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--max-turns", "1"]
    if model:
        cmd += ["--model", model]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as ex:
        raise RetryableError(f"claude -p timed out after {timeout}s") from ex
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[:500]
        if any(k in err.lower() for k in ("rate", "overloaded", "timeout", "529", "503")):
            raise RetryableError(f"claude -p failed (retryable): {err}")
        raise FatalError(f"claude -p failed rc={proc.returncode}: {err}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as ex:
        raise RetryableError(f"claude -p returned non-JSON envelope: {proc.stdout[:200]}") from ex
    result = envelope.get("result", "")
    cost = float(envelope.get("total_cost_usd") or 0.0)
    served = None
    mu = envelope.get("modelUsage") or {}
    if isinstance(mu, dict) and mu:
        served = next(iter(mu.keys()))
    log.debug(f"claude -p ok in {time.time() - t0:.1f}s cost=${cost:.4f}")
    return result, cost, served


def _call_anthropic_sdk(prompt: str, model: str | None, timeout: int) -> tuple[str, float, str | None]:
    try:
        import anthropic  # type: ignore
    except ImportError as ex:
        raise FatalError("anthropic SDK not installed") from ex
    client = anthropic.Anthropic(timeout=timeout)
    m = model or "claude-sonnet-5-5"
    resp = client.messages.create(model=m, max_tokens=4096, messages=[{"role": "user", "content": prompt}])
    text = "".join(getattr(b, "text", "") for b in resp.content)
    return text, 0.0, m


def run_prompt(
    conn: sqlite3.Connection | None,
    prompt_name: str,
    payload: dict[str, Any],
    schema_model: type[T],
    use_cache: bool = True,
) -> T:
    """Render, call, validate (retry once with the validation error appended), cache."""
    settings = get_settings()
    cfg = settings.llm or {}
    backend = "anthropic_sdk" if settings.anthropic_api_key and cfg.get("backend") == "anthropic_sdk" else "claude_cli"
    timeout = int(cfg.get("timeout_seconds", 180))
    max_retries = int(cfg.get("max_retries", 2))
    model = cfg.get("model")
    version = prompt_version(prompt_name)
    prompt = render(prompt_name, payload, schema_model)
    input_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    if conn is not None and use_cache:
        row = conn.execute(
            "SELECT output_json FROM llm_calls WHERE prompt_name = ? AND prompt_version = ? AND input_hash = ?",
            (prompt_name, version, input_hash),
        ).fetchone()
        if row and row["output_json"]:
            return schema_model.model_validate_json(row["output_json"])

    call = _call_anthropic_sdk if backend == "anthropic_sdk" else _call_claude_cli
    last_err: Exception | None = None
    attempt_prompt = prompt
    t0 = time.time()
    total_cost = 0.0
    served = None
    for attempt in range(max_retries + 1):
        try:
            text, cost, served = call(attempt_prompt, model, timeout)
            total_cost += cost
            data = _extract_json(text)
            obj = schema_model.model_validate(data)
            break
        except (ValidationError, json.JSONDecodeError, ValueError) as ex:
            last_err = ex
            attempt_prompt = (
                prompt + f"\n\nYour previous answer did not validate: {str(ex)[:800]}. "
                "Return ONLY a corrected JSON object."
            )
            log.warning(f"{prompt_name}: invalid output on attempt {attempt + 1}: {str(ex)[:200]}")
            continue
        except RetryableError as ex:
            last_err = ex
            time.sleep(2 * (attempt + 1))
            continue
    else:
        raise FatalError(f"{prompt_name}: no valid output after {max_retries + 1} attempts: {last_err}")

    if conn is not None:
        conn.execute(
            "INSERT OR REPLACE INTO llm_calls(prompt_name, prompt_version, input_hash, model, output_json, cost_usd, duration_ms, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (prompt_name, version, input_hash, served or model or backend, obj.model_dump_json(), total_cost,
             int((time.time() - t0) * 1000), utcnow()),
        )
    return obj


def llm_spend_today(conn: sqlite3.Connection) -> float:
    return float(conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE created_at >= date('now')").fetchone()[0])

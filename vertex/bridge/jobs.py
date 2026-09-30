"""Bridge jobs: Python plans every connector call; Claude (MCP) or a key-based backend executes; ingest is idempotent."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from vertex.db.connection import utcnow
from vertex.utils.errors import FatalError, RetryableError
from vertex.utils.logging import get_logger

log = get_logger("bridge")


def enqueue(
    conn: sqlite3.Connection,
    connector: str,
    tool: str,
    args: dict[str, Any],
    purpose: str,
    batch_key: str | None = None,
    context: dict[str, Any] | None = None,
    est_credits: float = 0.0,
    credit_type: str | None = None,
) -> int:
    """Insert a pending job. Identical pending jobs (same connector/tool/args/purpose) are not duplicated."""
    args_json = json.dumps(args, sort_keys=True, default=str)
    dup = conn.execute(
        "SELECT id FROM bridge_jobs WHERE connector = ? AND tool = ? AND args_json = ? AND purpose = ? AND status IN ('pending','claimed')",
        (connector, tool, args_json, purpose),
    ).fetchone()
    if dup:
        return int(dup["id"])
    cur = conn.execute(
        "INSERT INTO bridge_jobs(connector, tool, args_json, purpose, batch_key, context_json, est_credits, credit_type, status, attempts, created_at)"
        " VALUES (?,?,?,?,?,?,?,?, 'pending', 0, ?)",
        (connector, tool, args_json, purpose, batch_key, json.dumps(context or {}, default=str), est_credits, credit_type, utcnow()),
    )
    return int(cur.lastrowid)


def pending(conn: sqlite3.Connection, connector: str | None = None, limit: int = 10,
            budget_remaining: dict[str, float] | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM bridge_jobs WHERE status = 'pending'"
    args: list[Any] = []
    if connector:
        q += " AND connector = ?"
        args.append(connector)
    q += " ORDER BY id LIMIT ?"
    args.append(limit * 3)
    out: list[dict[str, Any]] = []
    withheld = 0
    for r in conn.execute(q, args).fetchall():
        job = _row_to_job(r)
        if budget_remaining is not None and job["credit_type"]:
            remaining = budget_remaining.get(job["credit_type"])
            if remaining is not None and job["est_credits"] > remaining:
                withheld += 1
                continue
        out.append(job)
        if len(out) >= limit:
            break
    if withheld:
        log.warning(f"withheld {withheld} job(s) that would exceed today's remaining budget")
    return out


def claim(conn: sqlite3.Connection, job_ids: list[int]) -> None:
    for jid in job_ids:
        conn.execute("UPDATE bridge_jobs SET status = 'claimed', claimed_at = ? WHERE id = ? AND status = 'pending'",
                     (utcnow(), jid))


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM bridge_jobs WHERE id = ?", (job_id,)).fetchone()
    if not r:
        raise FatalError(f"no bridge job {job_id}")
    return _row_to_job(r)


def _row_to_job(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["args"] = json.loads(d.pop("args_json") or "{}")
    d["context"] = json.loads(d.pop("context_json") or "{}")
    return d


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_payload(path: Path) -> Any:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise FatalError(f"{path} is empty")
    # MCP tool results are sometimes saved as a text wrapper: [{"type":"text","text":"{...}"}]
    data = json.loads(text)
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("type") == "text":
        inner = data[0].get("text", "")
        try:
            data = json.loads(inner)
        except json.JSONDecodeError:
            data = {"text": inner}
    return data


def complete(conn: sqlite3.Connection, job_id: int, file_path: Path, run_id: int | None = None) -> dict[str, Any]:
    """Validate + ingest a job result. Re-completing the same file is a no-op (sha256 on ingest_batches)."""
    from vertex.bridge.router import get_handler

    job = get_job(conn, job_id)
    if job["status"] == "done":
        return {"status": "already_done", "job_id": job_id}
    sha = file_sha256(file_path)
    dup = conn.execute("SELECT id, status FROM ingest_batches WHERE file_sha256 = ?", (sha,)).fetchone()
    if dup and dup["status"] == "ok":
        conn.execute("UPDATE bridge_jobs SET status = 'done', completed_at = ?, result_file = ? WHERE id = ?",
                     (utcnow(), str(file_path), job_id))
        return {"status": "no_op_duplicate_file", "job_id": job_id, "batch_id": dup["id"]}
    payload = load_payload(file_path)
    handler = get_handler(job["purpose"])
    cur = conn.execute(
        "INSERT INTO ingest_batches(source, kind, file_path, file_sha256, job_id, status, started_at) VALUES (?,?,?,?,?, 'running', ?)",
        (job["connector"], job["purpose"], str(file_path), sha, job_id, utcnow()),
    )
    batch_id = int(cur.lastrowid)
    try:
        conn.execute("BEGIN")
        stats = handler(conn, job, payload, batch_id=batch_id, run_id=run_id) or {}
        conn.execute(
            "UPDATE ingest_batches SET status = 'ok', finished_at = ?, rows = ?, inserted = ?, updated = ?, skipped = ? WHERE id = ?",
            (utcnow(), stats.get("rows", 0), stats.get("inserted", 0), stats.get("updated", 0), stats.get("skipped", 0), batch_id),
        )
        conn.execute(
            "UPDATE bridge_jobs SET status = 'done', completed_at = ?, result_file = ?, result_bytes = ? WHERE id = ?",
            (utcnow(), str(file_path), file_path.stat().st_size, job_id),
        )
        conn.execute("COMMIT")
    except Exception as ex:  # noqa: BLE001
        conn.execute("ROLLBACK")
        conn.execute("UPDATE ingest_batches SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                     (utcnow(), str(ex)[:2000], batch_id))
        fail(conn, job_id, f"ingest error: {ex}")
        raise
    stats.update({"status": "ok", "job_id": job_id, "batch_id": batch_id})
    log.info(f"job {job_id} ({job['purpose']}) ingested: {json.dumps({k: v for k, v in stats.items() if k != 'status'}, default=str)}")
    return stats


def fail(conn: sqlite3.Connection, job_id: int, error: str, max_attempts: int = 3) -> str:
    job = get_job(conn, job_id)
    attempts = int(job["attempts"]) + 1
    status = "pending" if attempts < max_attempts else "failed"
    conn.execute("UPDATE bridge_jobs SET status = ?, attempts = ?, error = ?, claimed_at = NULL WHERE id = ?",
                 (status, attempts, error[:2000], job_id))
    return status


def skip(conn: sqlite3.Connection, job_id: int, reason: str) -> None:
    conn.execute("UPDATE bridge_jobs SET status = 'skipped', error = ?, completed_at = ? WHERE id = ?",
                 (reason[:500], utcnow(), job_id))


def describe(job: dict[str, Any]) -> str:
    """Human/Claude-readable instruction for executing one job through MCP."""
    return json.dumps(
        {
            "job_id": job["id"],
            "connector": job["connector"],
            "tool": job["tool"],
            "args": job["args"],
            "purpose": job["purpose"],
            "est_credits": job["est_credits"],
            "save_to": f"data/inbox/{job['id']}.json",
        },
        indent=2,
        default=str,
    )


class BridgeUnavailable(RetryableError):
    """Raised by key backends when the network or key is not available; the job stays pending for the bridge."""

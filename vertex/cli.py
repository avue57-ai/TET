"""Vertex CLI. Commands are grouped by workflow stage; every command is idempotent and safe to re-run."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from vertex import __version__
from vertex.db.connection import connect, dump_sql, migrate, table_counts
from vertex.settings import PROJECT_ROOT, get_settings
from vertex.utils.logging import setup_logging

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Vertex Equity origination engine")
db_app = typer.Typer(help="Database maintenance")
run_app = typer.Typer(help="Run lock / bookkeeping for scheduled jobs")
bridge_app = typer.Typer(help="Connector bridge: plan → execute (Claude/MCP or key backend) → ingest")
thesis_app = typer.Typer(help="Thesis configs and query compilation")
review_app = typer.Typer(help="Human review queue")
campaign_app = typer.Typer(help="Lemlist campaigns and enrollment")
replies_app = typer.Typer(help="Reply classification")
suppression_app = typer.Typer(help="Suppression lists")
feedback_app = typer.Typer(help="Feedback loop")
for name, sub in (("db", db_app), ("run", run_app), ("bridge", bridge_app), ("thesis", thesis_app),
                  ("review", review_app), ("campaign", campaign_app), ("replies", replies_app),
                  ("suppression", suppression_app), ("feedback", feedback_app)):
    app.add_typer(sub, name=name)

console = Console()


def _conn():
    settings = get_settings()
    setup_logging(settings.data_dir / "logs")
    conn = connect(settings.db_path)
    migrate(conn)
    return conn


@app.callback()
def _main() -> None:
    """Vertex origination engine CLI."""


@app.command()
def version() -> None:
    console.print(f"vertex {__version__}")


@app.command()
def init() -> None:
    """Create the database, apply migrations, create data folders."""
    settings = get_settings()
    for sub in ("exports", "review", "inbox", "raw", "logs", "review/decisions"):
        (settings.data_dir / sub).mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "inbox" / ".gitkeep").touch()
    conn = _conn()
    applied = migrate(conn)
    console.print(f"db: {settings.db_path} (migrations applied now: {applied or 'none, up to date'})")


@app.command()
def doctor(net: Annotated[bool, typer.Option(help="Probe provider hosts (needs network policy allowing them)")] = False) -> None:
    """Check config, secrets presence, DB, open runs, backends, and (optionally) network reachability."""
    settings = get_settings()
    conn = _conn()
    t = Table(title="vertex doctor", show_lines=False)
    t.add_column("check")
    t.add_column("result")
    t.add_row("config", str(settings.config_dir / "settings.yaml"))
    t.add_row("db", f"{settings.db_path} ({settings.db_path.stat().st_size // 1024} KB)" if settings.db_path.exists() else "missing")
    counts = table_counts(conn)
    t.add_row("companies / contacts / scores", f"{counts.get('companies',0)} / {counts.get('contacts',0)} / {counts.get('scores',0)}")
    t.add_row("pending bridge jobs", str(conn.execute("SELECT COUNT(*) FROM bridge_jobs WHERE status='pending'").fetchone()[0]))
    for p in ("lemlist", "apollo", "inven"):
        t.add_row(f"backend {p}", settings.backend_for(p))
    t.add_row("anthropic sdk key", "present" if settings.anthropic_api_key else "absent (claude -p CLI used)")
    t.add_row("persona coherent", str(settings.persona.coherent()))
    open_run = conn.execute("SELECT id, job, started_at FROM runs WHERE status='running'").fetchone()
    t.add_row("open run", f"{open_run['id']} {open_run['job']} since {open_run['started_at']}" if open_run else "none")
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=20)
        t.add_row("claude cli", out.stdout.strip() or out.stderr.strip()[:80])
    except (OSError, subprocess.TimeoutExpired) as ex:
        t.add_row("claude cli", f"unavailable ({ex})")
    if net:
        from vertex.integrations.base import probe_hosts
        for p, res in probe_hosts().items():
            t.add_row(f"net {p}", res)
    console.print(t)


@db_app.command("checkpoint")
def db_checkpoint(no_git_check: bool = False) -> None:
    """Write the text dump next to the DB and print the commit command. Refuses if the tree is behind origin."""
    settings = get_settings()
    conn = _conn()
    if not no_git_check:
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=PROJECT_ROOT, check=False)
        behind = subprocess.run(["git", "rev-list", "--count", "HEAD..@{u}"], cwd=PROJECT_ROOT,
                                capture_output=True, text=True, check=False)
        if behind.returncode == 0 and behind.stdout.strip().isdigit() and int(behind.stdout.strip()) > 0:
            console.print("[red]working tree is behind origin; pull first[/red]")
            raise typer.Exit(2)
    n = dump_sql(conn, settings.data_dir / "vertex_dump.sql")
    console.print(f"dump written ({n} lines). Commit with:\n  git add data/vertex.db data/vertex_dump.sql data/exports data/review && git commit -m 'data: checkpoint'")


@db_app.command("counts")
def db_counts() -> None:
    conn = _conn()
    for k, v in table_counts(conn).items():
        console.print(f"{k:22} {v}")


@run_app.command("start")
def run_start(job: str) -> None:
    from vertex.db.repo import start_run
    conn = _conn()
    try:
        rid = start_run(conn, job)
    except RuntimeError as ex:
        console.print(f"[red]{ex}[/red]")
        raise typer.Exit(2) from ex
    console.print(str(rid))


@run_app.command("finish")
def run_finish(run_id: int, status: str = "ok", summary: Optional[str] = None, error: Optional[str] = None) -> None:
    from vertex.db.repo import finish_run
    conn = _conn()
    finish_run(conn, run_id, status, json.loads(summary) if summary else None, error)
    console.print(f"run {run_id} {status}")


@app.command()
def budget() -> None:
    """Show today's credit and LLM spend against per-run budgets."""
    from vertex.ai.claude import llm_spend_today
    from vertex.db.repo import credits_used_today
    settings = get_settings()
    conn = _conn()
    b = settings.budgets
    rows = [
        ("inven_ai", credits_used_today(conn, "inven", "inven_ai"), b.inven_ai_credits_per_run),
        ("inven_export", credits_used_today(conn, "inven", "inven_export"), b.inven_export_rows_per_run),
        ("apollo", credits_used_today(conn, "apollo"), b.apollo_credits_per_run),
        ("llm_usd", llm_spend_today(conn), b.llm_usd_per_run),
    ]
    t = Table(title="budget (today)")
    t.add_column("type"); t.add_column("used"); t.add_column("cap"); t.add_column("remaining")
    for name, used, cap in rows:
        t.add_row(name, f"{used:.2f}", f"{cap}", f"{max(0.0, cap - used):.2f}")
    console.print(t)


def _budget_remaining(conn) -> dict[str, float]:
    from vertex.ai.claude import llm_spend_today
    from vertex.db.repo import credits_used_today
    b = get_settings().budgets
    return {
        "inven_ai": max(0.0, b.inven_ai_credits_per_run - credits_used_today(conn, "inven", "inven_ai")),
        "inven_export": max(0.0, b.inven_export_rows_per_run - credits_used_today(conn, "inven", "inven_export")),
        "apollo": max(0.0, b.apollo_credits_per_run - credits_used_today(conn, "apollo")),
        "llm_usd": max(0.0, b.llm_usd_per_run - llm_spend_today(conn)),
    }


@bridge_app.command("next")
def bridge_next(max: int = 10, connector: Optional[str] = None, claim: bool = True, no_budget: bool = False) -> None:
    """Print the next pending jobs as exact MCP tool calls for Claude to execute (or for `bridge run`)."""
    from vertex.bridge import jobs as J
    conn = _conn()
    jobs = J.pending(conn, connector, max, None if no_budget else _budget_remaining(conn))
    if claim:
        J.claim(conn, [j["id"] for j in jobs])
    print(json.dumps([{"job_id": j["id"], "connector": j["connector"], "tool": j["tool"], "args": j["args"],
                       "purpose": j["purpose"], "est_credits": j["est_credits"],
                       "save_to": f"data/inbox/{j['id']}.json"} for j in jobs], indent=2, default=str))


@bridge_app.command("complete")
def bridge_complete(job_id: int, file: Path, run_id: Optional[int] = None) -> None:
    """Ingest a saved MCP/REST response for a job (idempotent by file hash)."""
    from vertex.bridge import jobs as J
    conn = _conn()
    try:
        stats = J.complete(conn, job_id, file, run_id)
    except Exception as ex:  # noqa: BLE001
        console.print(f"[red]job {job_id} failed: {ex}[/red]")
        raise typer.Exit(1) from ex
    console.print(json.dumps(stats, default=str))


@bridge_app.command("fail")
def bridge_fail(job_id: int, error: str) -> None:
    from vertex.bridge import jobs as J
    conn = _conn()
    console.print(J.fail(conn, job_id, error))


@bridge_app.command("list")
def bridge_list(status: str = "pending", limit: int = 50) -> None:
    conn = _conn()
    rows = conn.execute("SELECT id, connector, tool, purpose, est_credits, status, attempts, error FROM bridge_jobs WHERE status = ? ORDER BY id LIMIT ?",
                        (status, limit)).fetchall()
    t = Table(title=f"bridge jobs ({status})")
    for c in ("id", "connector", "tool", "purpose", "credits", "attempts", "error"):
        t.add_column(c)
    for r in rows:
        t.add_row(str(r["id"]), r["connector"], r["tool"], r["purpose"], str(r["est_credits"]), str(r["attempts"]), (r["error"] or "")[:60])
    console.print(t)


@bridge_app.command("run")
def bridge_run(max: int = 20, connector: Optional[str] = None, run_id: Optional[int] = None) -> None:
    """Execute pending jobs with the key-based backends (only where a provider key exists and the host is reachable)."""
    from vertex.bridge import jobs as J
    from vertex.integrations.base import execute_with_key, save_result
    settings = get_settings()
    conn = _conn()
    jobs = J.pending(conn, connector, max, _budget_remaining(conn))
    done = 0
    for job in jobs:
        if settings.backend_for(job["connector"]) != "key":
            continue
        J.claim(conn, [job["id"]])
        try:
            payload = execute_with_key(job)
            path = save_result(job["id"], payload, settings.data_dir / "inbox")
            J.complete(conn, job["id"], path, run_id)
            done += 1
        except J.BridgeUnavailable as ex:
            J.fail(conn, job["id"], f"key backend unavailable: {ex}")
        except Exception as ex:  # noqa: BLE001
            J.fail(conn, job["id"], str(ex))
    remaining = conn.execute("SELECT COUNT(*) FROM bridge_jobs WHERE status = 'pending'").fetchone()[0]
    console.print(f"executed {done} job(s) via key backends; remaining pending: {remaining}")


@app.command()
def ingest(source: str, file: Path, thesis: Optional[str] = None) -> None:
    """Ingest a raw payload file without a bridge job (csv | inven_rows). Mostly for manual imports."""
    conn = _conn()
    if source == "inven_rows":
        from vertex.bridge.jobs import load_payload
        from vertex.integrations.inven import ingest_search_rows
        if not thesis:
            raise typer.BadParameter("--thesis required for inven_rows")
        row = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis,)).fetchone()
        job = {"context": {"thesis_id": row["id"], "source_query_id": None, "offset": 0, "limit": 10_000}}
        console.print(json.dumps(ingest_search_rows(conn, job, load_payload(file)), default=str))
    else:
        raise typer.BadParameter(f"unknown source {source}")


# ---------------------------------------------------------------- thesis / discovery / screening

@thesis_app.command("validate")
def thesis_validate(slug: str) -> None:
    from vertex.core.thesis import load_thesis
    t = load_thesis(slug)
    console.print(f"ok: {t.slug} — {t.name}; {len(t.verticals)} vertical(s): {', '.join(v.key for v in t.verticals)}")


@thesis_app.command("compile")
def thesis_compile(slug: str, vertical: Optional[str] = None) -> None:
    """Print the Inven query descriptions the thesis compiles to (no jobs created)."""
    from vertex.core.thesis import compile_queries, load_thesis
    t = load_thesis(slug)
    for v in t.verticals:
        if vertical and v.key != vertical:
            continue
        console.rule(v.key)
        for kind, desc in compile_queries(t, v):
            console.print(f"[bold]{kind}[/bold]: {desc}\n")


@app.command()
def discover(thesis: str, vertical: Optional[list[str]] = typer.Option(None), family: Optional[list[str]] = typer.Option(None)) -> None:
    """Register the thesis and enqueue Inven build_company_search jobs (one per vertical x query family)."""
    from vertex.core.thesis import load_thesis
    from vertex.workflows.discover import plan_discovery
    conn = _conn()
    t = load_thesis(thesis)
    res = plan_discovery(conn, t, vertical or None, family or None)
    console.print(json.dumps(res, indent=2, default=str))
    console.print("next: `vertex bridge next --connector inven` and execute the printed MCP calls, then `vertex bridge complete`.")


@app.command("discover-status")
def discover_status(thesis: str) -> None:
    from vertex.workflows.discover import discovery_status
    conn = _conn()
    row = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis,)).fetchone()
    if not row:
        console.print("thesis not registered yet; run `vertex discover` first")
        raise typer.Exit(1)
    t = Table(title=f"discovery status: {thesis}")
    for c in ("id", "vertical", "kind", "status", "est_total", "rows", "new", "keeper", "halted"):
        t.add_column(c)
    for r in discovery_status(conn, row["id"]):
        t.add_row(str(r["id"]), r["vertical"], r["query_kind"], r["status"], str(r["estimated_total"] or ""),
                  str(r["rows_returned"]), str(r["new_companies"]), str(r["prescreen_keeper_rate"] or ""),
                  (r["halted_reason"] or "")[:40])
    console.print(t)


@app.command()
def screen(thesis: str, all: bool = typer.Option(False, "--all", help="Re-screen already screened/excluded companies too")) -> None:
    """Apply hard exclusion rules to Identified companies (institutional capital, public, subsidiary, size, geography, consolidators)."""
    from vertex.core.screen_rules import run_screen
    from vertex.core.thesis import load_thesis
    conn = _conn()
    t = load_thesis(thesis)
    row = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis,)).fetchone()
    conn.execute("BEGIN")
    stats = run_screen(conn, row["id"], t, only_new=not all)
    conn.execute("COMMIT")
    console.print(json.dumps(stats, indent=2))


@app.command()
def score(thesis: str, company: Optional[list[str]] = typer.Option(None, help="domain(s); default = all Screened+ companies"),
          no_llm: bool = False, rescore: bool = False, limit: int = 500) -> None:
    """Score companies (rules + Claude components). Appends a new score snapshot per company."""
    from vertex.ai.score import score_company
    from vertex.core.scoring import load_weights
    from vertex.core.thesis import load_thesis
    conn = _conn()
    t = load_thesis(thesis)
    trow = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis,)).fetchone()
    weights = load_weights()
    if company:
        ids = [r[0] for d in company for r in conn.execute("SELECT id FROM companies WHERE domain = ?", (d,))]
    else:
        q = ("SELECT tc.company_id FROM thesis_companies tc WHERE tc.thesis_id = ? AND tc.stage NOT IN ('Identified','Excluded','Passed')"
             + ("" if rescore else " AND tc.company_id NOT IN (SELECT company_id FROM scores WHERE thesis_id = ? AND weights_version = ?)")
             + " LIMIT ?")
        args = (trow["id"], trow["id"], weights["version"], limit) if not rescore else (trow["id"], limit)
        ids = [r[0] for r in conn.execute(q, args)]
    tiers: dict[str, int] = {}
    for i, cid in enumerate(ids, 1):
        try:
            conn.execute("BEGIN")
            res = score_company(conn, cid, trow["id"], t, use_llm=not no_llm, weights=weights)
            conn.execute("COMMIT")
        except Exception as ex:  # noqa: BLE001
            conn.execute("ROLLBACK")
            console.print(f"[red]company {cid}: {ex}[/red]")
            continue
        tiers[res.tier] = tiers.get(res.tier, 0) + 1
        if i % 10 == 0:
            console.print(f"scored {i}/{len(ids)} …")
    console.print(json.dumps({"scored": len(ids), "tiers": tiers}, indent=2))


prescreen_app = typer.Typer(help="Prescreen samples per vertical (size + ownership verification)")
app.add_typer(prescreen_app, name="prescreen")


@prescreen_app.command("sample")
def prescreen_sample(thesis: str, vertical: str, n: int = 25) -> None:
    """Pick a stratified sample and enqueue one WebSearch ownership query per company."""
    from vertex.ai.screen import plan_websearch_for, sample_companies
    conn = _conn()
    trow = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis,)).fetchone()
    ids = sample_companies(conn, trow["id"], vertical, n)
    jobs = [j for cid in ids for j in plan_websearch_for(conn, cid)]
    console.print(f"sampled {len(ids)} companies; enqueued {len(jobs)} websearch job(s). Run `vertex bridge next --connector websearch`.")


@prescreen_app.command("judge")
def prescreen_judge(thesis: str, vertical: Optional[str] = None, limit: int = 50) -> None:
    """Judge sampled companies whose WebSearch summaries are in (or Inven-only when none)."""
    from vertex.ai.screen import judge
    from vertex.core.thesis import load_thesis
    conn = _conn()
    t = load_thesis(thesis)
    trow = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis,)).fetchone()
    q = ("SELECT tc.company_id FROM thesis_companies tc JOIN companies c ON c.id = tc.company_id WHERE tc.thesis_id = ? "
         "AND tc.prescreen_status = 'pending' AND tc.company_id IN (SELECT company_id FROM source_records WHERE source = 'websearch')")
    args: list = [trow["id"]]
    if vertical:
        q += " AND c.vertical = ?"
        args.append(vertical)
    q += " LIMIT ?"
    args.append(limit)
    ids = [r[0] for r in conn.execute(q, args)]
    out = {"keep": 0, "drop": 0, "unclear": 0}
    for cid in ids:
        conn.execute("BEGIN")
        v = judge(conn, cid, trow["id"], t)
        conn.execute("COMMIT")
        st = conn.execute("SELECT prescreen_status FROM thesis_companies WHERE thesis_id = ? AND company_id = ?", (trow["id"], cid)).fetchone()[0]
        out[st] = out.get(st, 0) + 1
    console.print(json.dumps({"judged": len(ids), **out}))


@prescreen_app.command("rates")
def prescreen_rates(thesis: str) -> None:
    from vertex.ai.screen import keeper_rates
    conn = _conn()
    trow = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis,)).fetchone()
    t = Table(title="prescreen keeper rates")
    for c in ("query", "vertical", "kind", "judged", "keep", "drop", "unclear", "rate"):
        t.add_column(c)
    for r in keeper_rates(conn, trow["id"]):
        t.add_row(str(r["source_query_id"]), r["vertical"], r["kind"], str(r["judged"]), str(r["keep"]), str(r["drop"]),
                  str(r["unclear"]), f"{r['keeper_rate']:.0%}" if r["keeper_rate"] is not None else "")
    console.print(t)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

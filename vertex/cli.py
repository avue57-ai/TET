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
          no_llm: bool = False, rescore: bool = False, limit: int = 1000, workers: int = 4,
          keepers_only: bool = typer.Option(False, help="only companies with prescreen_status = keep")) -> None:
    """Score companies (rules + Claude components). Appends a new score snapshot per company."""
    from vertex.ai.score import score_company
    from vertex.core.scoring import load_weights
    from vertex.core.thesis import load_thesis
    from vertex.utils.parallel import parallel_map
    conn = _conn()
    t = load_thesis(thesis)
    trow = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis,)).fetchone()
    weights = load_weights()
    if company:
        ids = [r[0] for d in company for r in conn.execute("SELECT id FROM companies WHERE domain = ?", (d,))]
    else:
        q = ("SELECT tc.company_id FROM thesis_companies tc WHERE tc.thesis_id = ? AND tc.stage NOT IN ('Identified','Excluded','Passed')"
             + (" AND tc.prescreen_status = 'keep'" if keepers_only else "")
             + ("" if rescore else " AND tc.company_id NOT IN (SELECT company_id FROM scores WHERE thesis_id = ? AND weights_version = ? AND scored_by = ?)")
             + " LIMIT ?")
        by = "rules" if no_llm else "engine"
        args = (trow["id"], trow["id"], weights["version"], by, limit) if not rescore else (trow["id"], limit)
        ids = [r[0] for r in conn.execute(q, args)]
    tiers: dict[str, int] = {}
    tid = trow["id"]

    def work(c, cid):
        return score_company(c, cid, tid, t, use_llm=not no_llm, weights=weights)

    def ok(cid, res):
        tiers[res.tier] = tiers.get(res.tier, 0) + 1
        n = sum(tiers.values())
        if n % 25 == 0:
            console.print(f"scored {n}/{len(ids)} … {tiers}")

    def bad(cid, ex):
        console.print(f"[red]company {cid}: {ex}[/red]")

    parallel_map(work, ids, workers=1 if no_llm else workers, on_result=ok, on_error=bad)
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
def prescreen_judge(thesis: str, vertical: Optional[str] = None, limit: int = 1000, workers: int = 4,
                    inven_only: bool = typer.Option(False, help="judge from Inven material even without WebSearch summaries")) -> None:
    """Judge thesis fit + size + ownership for pending companies (Claude; verbatim-evidence guarded)."""
    from vertex.ai.screen import judge
    from vertex.core.thesis import load_thesis
    from vertex.utils.parallel import parallel_map
    conn = _conn()
    t = load_thesis(thesis)
    trow = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis,)).fetchone()
    q = ("SELECT tc.company_id FROM thesis_companies tc JOIN companies c ON c.id = tc.company_id WHERE tc.thesis_id = ? "
         "AND tc.prescreen_status = 'pending' AND tc.stage NOT IN ('Excluded','Passed')")
    if not inven_only:
        q += " AND tc.company_id IN (SELECT company_id FROM source_records WHERE source = 'websearch')"
    args: list = [trow["id"]]
    if vertical:
        q += " AND c.vertical = ?"
        args.append(vertical)
    q += " LIMIT ?"
    args.append(limit)
    ids = [r[0] for r in conn.execute(q, args)]
    out: dict[str, int] = {}
    tid = trow["id"]

    def work(c, cid):
        judge(c, cid, tid, t)
        return c.execute("SELECT prescreen_status FROM thesis_companies WHERE thesis_id = ? AND company_id = ?", (tid, cid)).fetchone()[0]

    def ok(cid, st):
        out[st] = out.get(st, 0) + 1
        n = sum(out.values())
        if n % 25 == 0:
            console.print(f"judged {n}/{len(ids)} … {out}")

    def bad(cid, ex):
        console.print(f"[red]company {cid}: {ex}[/red]")

    parallel_map(work, ids, workers=workers, on_result=ok, on_error=bad)
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


# ---------------------------------------------------------------- enrichment / contacts

def _targets(conn, thesis: str, tiers: str, limit: int, keepers_only: bool):
    from vertex.workflows.enrich import target_company_ids
    trow = conn.execute("SELECT id FROM theses WHERE slug = ?", (thesis,)).fetchone()
    return trow["id"], target_company_ids(conn, trow["id"], tiers.split(","), keepers_only, limit)


@app.command()
def enrich(thesis: str, tiers: str = "T1,T2", limit: int = 100, keepers_only: bool = True) -> None:
    """Enqueue Apollo organization enrichment (1 credit/match) for top-tier companies without Apollo data."""
    from vertex.workflows.enrich import plan_org_enrichment
    conn = _conn()
    _, ids = _targets(conn, thesis, tiers, limit, keepers_only)
    jobs = plan_org_enrichment(conn, ids)
    console.print(f"{len(ids)} target companies; enqueued {len(jobs)} org-enrich job(s). Run `vertex bridge next --connector apollo`.")


contacts_app = typer.Typer(help="Decision-maker discovery and selection (Apollo)")
app.add_typer(contacts_app, name="contacts")


@contacts_app.command("find")
def contacts_find(thesis: str, tiers: str = "T1,T2", limit: int = 100, keepers_only: bool = True) -> None:
    """Enqueue Apollo people searches (owner/founder/CEO/president titles) for target companies without contacts."""
    from vertex.workflows.enrich import plan_contact_search
    conn = _conn()
    _, ids = _targets(conn, thesis, tiers, limit, keepers_only)
    jobs = plan_contact_search(conn, ids)
    console.print(f"{len(ids)} target companies; enqueued {len(jobs)} people-search job(s).")


@contacts_app.command("select")
def contacts_select(thesis: str, tiers: str = "T1,T2", limit: int = 100, keepers_only: bool = True) -> None:
    """Pick the primary decision-maker per company by the title ladder; flag companies with none."""
    from vertex.workflows.enrich import select_contacts
    conn = _conn()
    tid, ids = _targets(conn, thesis, tiers, limit, keepers_only)
    console.print(json.dumps(select_contacts(conn, tid, ids)))


@contacts_app.command("reveal")
def contacts_reveal(thesis: str, tiers: str = "T1,T2", limit: int = 100, keepers_only: bool = True) -> None:
    """Enqueue Apollo bulk-match reveals (work email) for primary contacts only."""
    from vertex.workflows.enrich import plan_reveals
    conn = _conn()
    _, ids = _targets(conn, thesis, tiers, limit, keepers_only)
    jobs = plan_reveals(conn, ids)
    console.print(f"enqueued {len(jobs)} bulk-match job(s) for primary contacts of {len(ids)} companies.")


@contacts_app.command("show")
def contacts_show(thesis: str, tiers: str = "T1,T2", limit: int = 100, keepers_only: bool = True) -> None:
    from vertex.workflows.enrich import contact_summary
    conn = _conn()
    _, ids = _targets(conn, thesis, tiers, limit, keepers_only)
    t = Table(title="primary contacts")
    for c in ("company", "domain", "cands", "name", "title", "rank", "email", "status"):
        t.add_column(c)
    for r in contact_summary(conn, ids):
        p = r["primary"] or {}
        t.add_row(r["company"][:28], r["domain"], str(r["candidates"]), f"{p.get('first_name','') or ''} {p.get('last_name','') or ''}".strip(),
                  (p.get("title") or "")[:28], str(p.get("role_rank") or ""), p.get("email") or "", p.get("email_status") or "")
    console.print(t)


# ---------------------------------------------------------------- signals + personalization
signals_app = typer.Typer(help="Signal research (WebSearch via bridge) and extraction")
app.add_typer(signals_app, name="signals")
personalize_app = typer.Typer(help="Per-contact outreach copy: generate, lint, critic")
app.add_typer(personalize_app, name="personalize")


@signals_app.command("plan")
def signals_plan(thesis: str, tiers: str = "T1,T2", limit: int = 50, keepers_only: bool = True,
                 kinds: Optional[list[str]] = typer.Option(None, help="overview,credentials,customers,people")) -> None:
    """Enqueue WebSearch jobs for target companies (Claude executes them via `vertex bridge next --connector websearch`)."""
    from vertex.integrations.websearch import plan_signal_searches
    conn = _conn()
    _, ids = _targets(conn, thesis, tiers, limit, keepers_only)
    n = 0
    for cid in ids:
        n += len(plan_signal_searches(conn, cid, kinds))
    console.print(f"enqueued {n} websearch job(s) for {len(ids)} companies.")


@signals_app.command("extract")
def signals_extract(thesis: str, tiers: str = "T1,T2", limit: int = 50, keepers_only: bool = True, workers: int = 4,
                    company: Optional[list[str]] = typer.Option(None, help="domain(s)")) -> None:
    """Extract evidence-quoted signals per company (one Claude call each; cached)."""
    from vertex.ai.signals import extract_signals
    from vertex.core.thesis import load_thesis
    from vertex.utils.parallel import parallel_map
    conn = _conn()
    t = load_thesis(thesis)
    if company:
        ids = [r[0] for r in conn.execute(f"SELECT id FROM companies WHERE domain IN ({','.join('?' * len(company))})", company)]
    else:
        _, ids = _targets(conn, thesis, tiers, limit, keepers_only)
    totals = {"signals": 0, "rejected": 0, "companies": 0, "errors": 0}

    def _one(c, cid):
        return extract_signals(c, cid, t)

    def _ok(cid, res):
        totals["companies"] += 1
        totals["signals"] += res["signals"]
        totals["rejected"] += res["rejected"]

    def _err(cid, exc):
        totals["errors"] += 1
        console.print(f"[red]company {cid}: {exc}")

    parallel_map(_one, ids, workers=workers, on_result=_ok, on_error=_err)
    console.print(json.dumps(totals))


@signals_app.command("show")
def signals_show(domain: str) -> None:
    conn = _conn()
    row = conn.execute("SELECT id, name FROM companies WHERE domain = ?", (domain,)).fetchone()
    if not row:
        raise typer.Exit(1)
    t = Table(title=f"signals: {row['name']}")
    for c in ("id", "type", "conf", "cite", "banned", "fresh", "text", "quote"):
        t.add_column(c)
    for s in conn.execute("SELECT * FROM signals WHERE company_id = ? ORDER BY safe_to_cite DESC, confidence DESC", (row["id"],)):
        t.add_row(str(s["id"]), s["hook_type"], f"{s['confidence']:.2f}", "y" if s["safe_to_cite"] else "", "y" if s["banned_theme"] else "",
                  s["freshness"] or "", (s["text"] or "")[:70], (s["evidence_quote"] or "")[:60])
    console.print(t)


@personalize_app.command("run")
def personalize_run(thesis: str, tiers: str = "T1,T2", limit: int = 50, keepers_only: bool = True, workers: int = 3,
                    arm: str = "linkedin_led", no_critic: bool = False, contact: Optional[list[int]] = typer.Option(None)) -> None:
    """Generate the full message set for each company's primary contact; lint + critic; store as draft/needs_edit."""
    from vertex.ai.personalize import generate_for_contact
    from vertex.core.thesis import load_thesis
    from vertex.utils.parallel import parallel_map
    conn = _conn()
    t = load_thesis(thesis)
    if contact:
        cids = list(contact)
    else:
        _, ids = _targets(conn, thesis, tiers, limit, keepers_only)
        q = f"SELECT id FROM contacts WHERE is_primary = 1 AND company_id IN ({','.join('?' * len(ids))})"
        cids = [r[0] for r in conn.execute(q, ids)] if ids else []
    out = {"draft": 0, "needs_edit": 0, "no_hook": 0, "errors": 0}

    def _one(c, cid):
        return generate_for_contact(c, cid, t, arm=arm, critic=not no_critic)

    def _ok(cid, res):
        out[res["status"]] = out.get(res["status"], 0) + 1
        if res.get("lint"):
            console.print(f"contact {cid} [{res['status']}] lint: {res['lint']}")

    def _err(cid, exc):
        out["errors"] += 1
        console.print(f"[red]contact {cid}: {exc}")

    parallel_map(_one, cids, workers=workers, on_result=_ok, on_error=_err)
    console.print(json.dumps(out))


@personalize_app.command("show")
def personalize_show(domain: str, steps: Optional[list[str]] = typer.Option(None)) -> None:
    conn = _conn()
    rows = conn.execute(
        "SELECT m.*, ct.first_name, ct.last_name, ct.title AS ctitle FROM messages m JOIN contacts ct ON ct.id = m.contact_id JOIN companies c ON c.id = m.company_id "
        "WHERE c.domain = ? AND m.status != 'rejected' ORDER BY m.contact_id, m.id", (domain,)).fetchall()
    for m in rows:
        if steps and m["step_key"] not in steps:
            continue
        flags = json.loads(m["lint_flags_json"] or "[]")
        console.rule(f"{m['step_key']} → {m['first_name']} {m['last_name']} ({m['ctitle']}) [{m['status']}] variant {m['variant_key']} critic {m['critic_score']} lint {flags}")
        if m["subject"]:
            console.print(f"[bold]Subject:[/bold] {m['subject']}")
        console.print(m["body"])


# ---------------------------------------------------------------- suppression + gates
@suppression_app.command("status")
def suppression_status() -> None:
    from vertex.workflows.suppression import seed_status
    console.print(json.dumps(seed_status(_conn()), indent=1, default=str))


@suppression_app.command("import-never-contact")
def suppression_import_never_contact() -> None:
    from vertex.workflows.suppression import import_never_contact
    console.print(json.dumps(import_never_contact(_conn())))


@suppression_app.command("plan-lemlist")
def suppression_plan_lemlist(thesis: Optional[str] = None, tiers: str = "T1,T2", limit: int = 100, keepers_only: bool = True,
                             unsubscribes: bool = True, lookups: bool = False) -> None:
    """Enqueue Lemlist unsubscribe sync and (optionally) per-target lead/contact lookups for the thesis targets."""
    from vertex.integrations.lemlist import plan_lookups, plan_unsubscribes
    conn = _conn()
    out: dict[str, Any] = {}
    if unsubscribes:
        out["unsubscribes_job"] = plan_unsubscribes(conn)
    if lookups and thesis:
        _, ids = _targets(conn, thesis, tiers, limit, keepers_only)
        out["lookup_jobs"] = plan_lookups(conn, ids)
    console.print(json.dumps(out))


@suppression_app.command("import-lemlist-leads")
def suppression_import_lemlist_leads() -> None:
    """REST-key path only: bulk import every legacy campaign's leads as prior outreach (needs LEMLIST_API_KEY + network)."""
    from vertex.integrations.lemlist import import_campaign_leads_rest
    console.print(json.dumps(import_campaign_leads_rest(_conn())))


@suppression_app.command("check")
def suppression_check(value: str) -> None:
    """Show active suppression rows for an email, domain, or LinkedIn URL."""
    from vertex.workflows.suppression import classify_value, hits
    cv = classify_value(value)
    if not cv:
        raise typer.Exit(1)
    rows = hits(_conn(), **{cv[0] if cv[0] != "linkedin" else "linkedin": cv[1]})
    console.print(json.dumps([dict(r) for r in rows], indent=1))


@suppression_app.command("add")
def suppression_add(value: str, reason: str = "manual", days: Optional[int] = None, hold: bool = False) -> None:
    from vertex.workflows.suppression import add_suppression, classify_value, cooldown
    cv = classify_value(value)
    if not cv:
        raise typer.Exit(1)
    new = add_suppression(_conn(), cv[0], cv[1], reason, "cli", expires_at=cooldown(days) if days else None, hold_for_human=hold)
    console.print(f"{'added' if new else 'updated'} {cv[0]}={cv[1]} reason={reason}")


@campaign_app.command("gates")
def campaign_gates(contact_id: int, campaign_id: int, channel_scope: str = "full") -> None:
    """Run the eight enrollment gates for one contact against one engine campaign."""
    from vertex.core.gates import check_enrollment
    ok, results, decision = check_enrollment(_conn(), contact_id, campaign_id, channel_scope)
    t = Table(title=f"gates contact {contact_id} → campaign {campaign_id}: {'PASS' if ok else 'BLOCKED'} (decision {decision})")
    t.add_column("gate"); t.add_column("result"); t.add_column("detail")
    for r in results:
        t.add_row(r.name, "[green]pass" if r.passed else ("[yellow]hold" if r.hold else "[red]fail"), r.detail)
    console.print(t)


@suppression_app.command("plan-granola")
def suppression_plan_granola(time_range: str = "last_30_days") -> None:
    """Enqueue a Granola list_meetings job (Claude executes it); matched company names become held existing relationships."""
    from vertex.integrations.granola import plan_meetings
    console.print(json.dumps({"granola_job": plan_meetings(_conn(), time_range)}))


@suppression_app.command("plan-apollo-crm")
def suppression_plan_apollo_crm(label_id: str, label_name: str, first_page: int = 1, last_page: int = 1, per_page: int = 100) -> None:
    """Enqueue Apollo saved-account-list pages (1 credit each) as existing-relationship holds."""
    from vertex.integrations.apollo import plan_crm_accounts
    jobs = plan_crm_accounts(_conn(), label_id, label_name, range(first_page, last_page + 1), per_page)
    console.print(json.dumps({"jobs": jobs, "pages": [first_page, last_page]}))


# ---------------------------------------------------------------- campaigns (draft only)
@campaign_app.command("create")
def campaign_create(thesis: str, arm: str = "linkedin_led", wave: int = 1, emoji: str = "🧭") -> None:
    """Create a DRAFT Lemlist campaign for a thesis × wave × arm and enqueue its build jobs (steps follow via the bridge)."""
    from vertex.workflows.campaign import create_campaign
    cid, job = create_campaign(_conn(), thesis, arm, wave, emoji)
    console.print(json.dumps({"campaign_id": cid, "job": job, "next": "vertex bridge next --connector lemlist"}))


@campaign_app.command("finalize")
def campaign_finalize(campaign_id: int) -> None:
    """After all step jobs are done: senders, reply behaviour, folder, readiness check."""
    from vertex.workflows.campaign import finalize
    console.print(json.dumps({"jobs": finalize(_conn(), campaign_id)}))


@campaign_app.command("show")
def campaign_show(campaign_id: Optional[int] = None) -> None:
    conn = _conn()
    rows = conn.execute("SELECT * FROM campaigns" + (" WHERE id = ?" if campaign_id else "") + " ORDER BY id", (campaign_id,) if campaign_id else ()).fetchall()
    for c in rows:
        st = json.loads(c["build_state_json"] or "{}")
        n = conn.execute("SELECT state, COUNT(*) AS n FROM enrollments WHERE campaign_id = ? GROUP BY state", (c["id"],)).fetchall()
        rd = json.loads(c["readiness_json"] or "{}").get("result", {})
        enr = ", ".join(f"{r['state']}: {r['n']}" for r in n) or "none"
        readiness = rd.get("status", "-") if isinstance(rd, dict) else "-"
        console.rule(f"[{c['id']}] {c['name']}  lemlist={c['lemlist_campaign_id']} state={c['lemlist_state']} launched={c['launched_at'] or '-'}")
        console.print(f"arm={c['arm']} seq={c['sequence_version']} root_seq={st.get('root_seq')} branches={st.get('branches')} steps={len(st.get('steps', {}))} "
                      f"ab={len(st.get('ab', []))} senders={len(st.get('senders', []))} settings={len(st.get('settings', []))} folder={len(st.get('folder', []))} "
                      f"readiness={readiness} enrollments={enr}")


@campaign_app.command("enroll")
def campaign_enroll(campaign_id: int, contact: Optional[list[int]] = typer.Option(None, help="contact ids; default = every contact with an approving enroll decision"),
                    channel_scope: str = "full", dry_run: bool = False) -> None:
    """Run the gates and queue approved contacts into the draft campaign (Lemlist add_leads with deduplicate=true)."""
    from vertex.core.gates import check_enrollment
    from vertex.workflows.campaign import plan_enrollment
    conn = _conn()
    ids = list(contact) if contact else [r[0] for r in conn.execute(
        "SELECT DISTINCT i.ref_id FROM review_items i JOIN review_decisions d ON d.review_item_id = i.id WHERE i.item_type = 'enroll' AND i.ref_table = 'contacts' AND d.decision = 'approve'")]
    if dry_run:
        for cid in ids:
            ok, results, decision = check_enrollment(conn, cid, campaign_id, channel_scope)
            console.print(f"contact {cid}: {'PASS' if ok else 'BLOCKED'} decision={decision} " + "; ".join(f"{r.name}={'ok' if r.passed else ('hold' if r.hold else 'FAIL')}{(' (' + r.detail + ')') if r.detail else ''}" for r in results))
        return
    console.print(json.dumps(plan_enrollment(conn, campaign_id, ids, channel_scope), default=str))


@campaign_app.command("sync-state")
def campaign_sync_state() -> None:
    """Enqueue get_campaign_details for every engine campaign (detects a human launch)."""
    from vertex.workflows.campaign import plan_state_sync
    console.print(json.dumps({"jobs": plan_state_sync(_conn())}))


@campaign_app.command("launch")
def campaign_launch(campaign_id: int, i_have_reviewed: bool = typer.Option(False, "--i-have-reviewed")) -> None:
    """Never launches. Prints the exact human step once the reviewer confirms they have reviewed the campaign."""
    from vertex.workflows.campaign import launch_instructions
    if not i_have_reviewed:
        console.print("[yellow]Add --i-have-reviewed after reviewing every lead and message in Lemlist. The engine itself never launches campaigns.")
        raise typer.Exit(1)
    console.print(launch_instructions(_conn(), campaign_id))


@campaign_app.command("mark-launched")
def campaign_mark_launched(campaign_id: int, by: str = "human") -> None:
    """Record that a human launched the campaign in Lemlist (pushed enrollments become active)."""
    from vertex.workflows.campaign import mark_launched
    mark_launched(_conn(), campaign_id, by)
    console.print(f"campaign {campaign_id} marked launched by {by}")

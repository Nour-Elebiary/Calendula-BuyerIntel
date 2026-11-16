"""CLI entry point (spec section 17): buyerintel <command>.

Commands are small, testable wrappers around library functions.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    name="buyerintel",
    help="Calendula Buyer Intel - personal buyer and market intelligence system.",
    no_args_is_help=True,
)


@app.command()
def init() -> None:
    """Create the database schema and print project status."""
    from buyerintel.config import load_settings
    from buyerintel.db.engine import get_engine, init_db

    settings = load_settings()
    engine = get_engine(settings.db_url)
    init_db(engine)
    typer.echo(f"Database ready: {settings.db_url}")


@app.command()
def import_workbook(
    file: Path = typer.Argument(..., exists=True, readable=True, help="xlsx or csv file"),
    mapping: Path = typer.Argument(..., exists=True, readable=True, help="mapping YAML"),
    expo_name: Optional[str] = typer.Option(None, help="Register rows under this expo"),
    expo_year: Optional[int] = typer.Option(None, help="Expo edition year"),
) -> None:
    """Import an existing lead workbook idempotently (M0)."""
    from buyerintel.config import load_settings
    from buyerintel.connectors.importer import import_workbook
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.db.audit import install_audit

    settings = load_settings()
    init_db(get_engine(settings.db_url))
    session = get_session(settings.db_url)
    install_audit(type(session))
    session.info["audit_source"] = f"import:{mapping.stem}"
    try:
        stats = import_workbook(session, file, mapping, expo_name=expo_name, expo_year=expo_year)
        session.commit()
        typer.echo(stats.summary())
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("ingest-expo")
def ingest_expo_cmd(
    file: Path = typer.Argument(..., exists=True, readable=True, help="xlsx or csv exhibitor list"),
    map_name: str = typer.Argument(..., help="Expo map slug from config/expo_maps/"),
    expo_year: Optional[int] = typer.Option(None, help="Edition year (overrides the map)"),
) -> None:
    """Ingest an exhibitor list idempotently through an expo map (M1)."""
    from buyerintel.config import load_settings
    from buyerintel.connectors.expos import ingest_expo
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db

    settings = load_settings()
    init_db(get_engine(settings.db_url))
    session = get_session(settings.db_url)
    install_audit(type(session))
    session.info["audit_source"] = f"expo:{map_name}"
    try:
        result = ingest_expo(session, file, map_name, expo_year=expo_year)
        session.commit()
        typer.echo(
            f"rows={result['rows_total']} new={result['rows_new']} "
            f"duplicate={result['rows_duplicate']} "
            f"companies_created={result['companies_created']} "
            f"appearances={result['appearances_created']}"
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command()
def dedupe() -> None:
    """Run the entity-resolution sweep over all companies (M1)."""
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.resolve import dedupe

    settings = load_settings()
    init_db(get_engine(settings.db_url))
    session = get_session(settings.db_url)
    install_audit(type(session))
    try:
        stats = dedupe.sweep(session)
        session.commit()
        typer.echo(
            f"pairs={stats['pairs']} merged={stats['merged']} linked={stats['linked']} "
            f"adjudicated={stats['adjudicated']} distinct={stats['distinct']}"
        )
        if stats["adjudicated"]:
            typer.echo(
                f"⚠ {stats['adjudicated']} borderline pairs await adjudication "
                "(recorded, not guessed)"
            )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("seed-products")
def seed_products_cmd() -> None:
    """Upsert the 42-product catalog and its HS candidates (idempotent)."""
    from buyerintel.catalog import seed_hs, seed_products
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db

    settings = load_settings()
    init_db(get_engine(settings.db_url))
    session = get_session(settings.db_url)
    install_audit(type(session))
    try:
        stats = seed_products(session)
        typer.echo(stats.summary())
        hs_stats = seed_hs(session)  # after products; unknown names are reported
        session.commit()
        typer.echo(hs_stats.summary())
        if hs_stats.skipped:
            typer.echo(f"HS codes skipped (no matching product): {hs_stats.skipped}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _open_market_session():
    """Shared bootstrapping for market commands."""
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db

    settings = load_settings()
    init_db(get_engine(settings.db_url))
    session = get_session(settings.db_url)
    install_audit(type(session))
    return session


@app.command("market-comtrade")
def market_comtrade(
    years: str = typer.Option("", help="Comma-separated years (blank = config window)"),
    hs: str = typer.Option("", help="Comma-separated HS codes (blank = catalog codes)"),
    max_calls: int = typer.Option(0, help="Call budget (0 = config default)"),
) -> None:
    """Fetch Egypt trade flows from UN Comtrade into trade_flow (M2)."""
    from buyerintel.market.comtrade import fetch_comtrade

    session = _open_market_session()
    try:
        year_list = [int(y) for y in years.split(",") if y.strip()] or None
        hs_list = [c.strip() for c in hs.split(",") if c.strip()] or None
        stats = fetch_comtrade(
            session,
            years=year_list,
            hs_codes=hs_list,
            max_calls=max_calls or None,
        )
        session.commit()
        typer.echo(stats.summary())
        if stats.skipped:
            typer.echo(f"skipped: {stats.skipped}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("market-comext")
def market_comext(
    years: str = typer.Option("", help="Comma-separated years (blank = 2023-2025)"),
    hs: str = typer.Option("", help="Comma-separated HS codes (blank = catalog codes)"),
) -> None:
    """Fetch EU27 import flows from Egypt via Eurostat Comext (M2)."""
    from buyerintel.market.comext import fetch_comext

    session = _open_market_session()
    try:
        year_list = [int(y) for y in years.split(",") if y.strip()] or None
        hs_list = [c.strip() for c in hs.split(",") if c.strip()] or None
        stats = fetch_comext(session, years=year_list, hs_codes=hs_list)
        session.commit()
        typer.echo(stats.summary())
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("market-signals")
def market_signals(
    no_trends: bool = typer.Option(False, help="Skip Google Trends (pytrends)"),
) -> None:
    """Collect demand signals: FX rates, PubMed counts, Google Trends (M2)."""
    from buyerintel.market.signals import fetch_signals

    session = _open_market_session()
    try:
        stats = fetch_signals(session, include_trends=not no_trends)
        session.commit()
        typer.echo(stats.summary())
        if stats.errors:
            typer.echo("errors:")
            for err in stats.errors:
                typer.echo(f"  - {err}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("market-trademap")
def market_trademap(
    file: Path = typer.Argument(..., exists=True, readable=True, help="ITC Trade Map CSV export"),
) -> None:
    """Import a user-downloaded ITC Trade Map CSV into trade_flow (M2)."""
    from buyerintel.market.trademap import import_trademap_csv

    session = _open_market_session()
    try:
        stats = import_trademap_csv(session, file)
        session.commit()
        typer.echo(stats.summary())
        if stats.skipped:
            typer.echo(f"skipped rows: {len(stats.skipped)}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("search-test")
def search_test(
    query: str = typer.Argument(..., help="Search query to route"),
    country: Optional[str] = typer.Option(None, help="ISO3 country for engine hints"),
) -> None:
    """Run one query through the search router and print results (M3)."""
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.search.router import SearchRouter

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    install_audit(type(session))
    try:
        results, stats = SearchRouter(session).search(query, country=country)
        session.commit()
        typer.echo(stats.summary())
        for r in results[:10]:
            typer.echo(f"[{r.engine}] {r.title} - {r.url}")
        if stats.unhealthy:
            typer.echo(f"⚠ engines blocked/unhealthy: {stats.unhealthy}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("enrich")
def enrich(
    limit: Optional[int] = typer.Option(None, help="Max companies to process (None = whole queue)"),
    tier: Optional[int] = typer.Option(None, help="Only this tier"),
    company_id: Optional[int] = typer.Option(None, help="Run a single company"),
) -> None:
    """Run the progressive enrichment pipeline S0-S4, resumable (M4)."""
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.enrich.engine import EnrichEngine
    from buyerintel.llm.router import LLMRouter
    from buyerintel.search.router import SearchRouter

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    install_audit(type(session))
    try:
        tiers = [tier] if tier is not None else None
        engine = EnrichEngine(session, llm=LLMRouter(session), search=SearchRouter(session))
        if company_id is not None:
            from sqlalchemy import select as _select
            from buyerintel.db.models import Company as _Company

            target = session.scalar(_select(_Company).where(_Company.id == company_id))
            if target is None:
                typer.echo(f"no company {company_id}")
                raise typer.Exit(1)
            engine.run_company(target)
            session.commit()
        else:
            engine.run(limit=limit, tiers=tiers)
            session.commit()
        typer.echo(engine.stats.summary())
        for error in engine.stats.errors[:10]:
            typer.echo(f"error: {error}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("enrich-status")
def enrich_status() -> None:
    """Coverage meter per tier (enriched / total) (M4)."""
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.enrich.engine import coverage

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    install_audit(type(session))
    try:
        session.commit()
        rows = coverage(session)
        if not rows:
            typer.echo("no companies yet")
        for row in rows:
            typer.echo(
                f"tier {row['tier']}: enriched {row['enriched']}/{row['total']} "
                f"({row['coverage']:.0%})"
            )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("llm-test")
def llm_test(
    prompt: str = typer.Argument(..., help="Prompt to send"),
    task: str = typer.Option("default", help="Task from providers.yaml task_routing"),
) -> None:
    """Send one prompt through the LLM router (cache + fallback chain) (M3)."""
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.llm.router import LLMRouter

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    install_audit(type(session))
    try:
        router = LLMRouter(session)
        text = router.complete(prompt, task=task)
        session.commit()
        typer.echo(router.stats.summary())
        typer.echo(text[:2000])
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("llm-health")
def llm_health() -> None:
    """Print per-provider LLM router health, quotas and cache stats (M3)."""
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.llm.router import LLMRouter

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    install_audit(type(session))
    try:
        router = LLMRouter(session)
        session.commit()
        typer.echo(router.stats.summary())
        for row in router.health():
            flags = []
            if not row["enabled"]:
                flags.append("disabled")
            if not row["has_key"]:
                flags.append("no-key")
            if row["cooling_down"]:
                flags.append("cooling")
            typer.echo(
                f"{row['provider']:12} enabled={row['enabled']} key={'Y' if row['has_key'] else 'N'} "
                f"day_calls={row['day_calls']} day_tokens={row['day_tokens']} "
                f"fails={row['consecutive_failures']} {' '.join(flags)}"
            )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("llm-sync-catalog")
def llm_sync_catalog() -> None:
    """Sync the provider catalog from mnfst/awesome-free-llm-apis (M3)."""
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.llm.catalog import sync_catalog

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    install_audit(type(session))
    try:
        report = sync_catalog()
        session.commit()
        typer.echo(f"fetched_entries={report['fetched_entries']}")
        typer.echo(f"matched={report['matched']}")
        typer.echo(f"new={report['new']}")
        typer.echo(f"quota_updates={report['quota_updates']}")
        typer.echo("Snapshot saved to data/llm_catalog_snapshot.json (providers.yaml untouched).")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("jobs")
def jobs_list() -> None:
    """List scheduler jobs with last-run status (config/schedules.yaml + job table)."""
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.sched import runner

    s = load_settings()
    init_db(get_engine(s.db_url))
    session = get_session(s.db_url)
    install_audit(type(session))
    try:
        runner.seed_jobs(session)
        session.commit()
        rows = runner.list_jobs(session)
    finally:
        session.close()
    if not rows:
        typer.echo("No jobs configured (config/schedules.yaml).")
        return
    for row in rows:
        state = "enabled" if row["enabled"] else "PAUSED"
        last = f" last={row['last_run']} ({row['last_status']})" if row["last_run"] else ""
        typer.echo(f"{row['job_id']}  cron='{row['cron']}' task={row['task']} [{state}]{last}")


def _jobs_state(job_id: str, enabled: bool) -> None:
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.sched import runner

    s = load_settings()
    init_db(get_engine(s.db_url))
    session = get_session(s.db_url)
    install_audit(type(session))
    try:
        (runner.resume if enabled else runner.pause)(session, job_id)
        session.commit()
        typer.echo(f"{job_id}: {'resumed' if enabled else 'paused'}")
    except LookupError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1)
    finally:
        session.close()


@app.command("jobs-pause")
def jobs_pause(job_id: str = typer.Argument(...)) -> None:
    """Pause a scheduler job."""
    _jobs_state(job_id, False)


@app.command("jobs-resume")
def jobs_resume(job_id: str = typer.Argument(...)) -> None:
    """Resume a paused scheduler job."""
    _jobs_state(job_id, True)


@app.command("jobs-import")
def jobs_import(
    file: str = typer.Argument(..., help="CSV of schedules: id,cron,task,enabled,retries"),
) -> None:
    """Import schedules from a CSV (optional spec feature)."""
    import csv

    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.sched import runner

    with open(file, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    specs = [{"id": r["id"], "cron": r.get("cron"), "task": r.get("task"),
              "enabled": str(r.get("enabled", "true")).lower() != "false",
              "retries": int(r.get("retries") or 0)} for r in rows if r.get("id")]
    s = load_settings()
    init_db(get_engine(s.db_url))
    session = get_session(s.db_url)
    install_audit(type(session))
    try:
        result = runner.seed_jobs(session, config={"jobs": specs})
        session.commit()
        typer.echo(f"imported={result['total']} new={result['created']}")
    finally:
        session.close()


@app.command()
def run(job_id: str = typer.Argument(..., help="Job id from config/schedules.yaml")) -> None:
    """Run a scheduler job once, right now."""
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.sched import runner

    s = load_settings()
    init_db(get_engine(s.db_url))
    session = get_session(s.db_url)
    install_audit(type(session))
    try:
        from sqlalchemy import select as _select

        from buyerintel.db.models import Job

        runner.seed_jobs(session)
        job = session.scalar(_select(Job).where(Job.job_id == job_id))
        if job is None:
            typer.echo(f"job {job_id!r} not found (buyerintel jobs list)")
            raise typer.Exit(1)
        job_run = runner.run_job(session, job)
        session.commit()
        typer.echo(f"{job_id}: {job_run.status}"
                   + (f" - {job_run.error}" if job_run.error else ""))
        if job_run.status == "error":
            raise typer.Exit(1)
    finally:
        session.close()


@app.command("catchup")
def catchup() -> None:
    """Run missed (coalesced) jobs now - the startup misfire path."""
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.sched import runner

    s = load_settings()
    init_db(get_engine(s.db_url))
    session = get_session(s.db_url)
    install_audit(type(session))
    try:
        missed = runner.missed_jobs(session)
        if not missed:
            typer.echo("no missed jobs within the grace window")
            return
        typer.echo(f"missed: {missed}")
        for job_id in runner.startup_catchup(session):
            typer.echo(f"ran {job_id}")
    finally:
        session.close()


@app.command("scheduler")
def scheduler() -> None:
    """Run the built-in APScheduler loop (SQLite job store, catch-up on start)."""
    from buyerintel.config import load_settings
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.sched import runner

    s = load_settings()
    init_db(get_engine(s.db_url))
    session = get_session(s.db_url)
    try:
        caught_up = runner.startup_catchup(session)
        if caught_up:
            typer.echo(f"catch-up ran: {caught_up}")
    finally:
        session.close()
    typer.echo("scheduler running (Ctrl+C to stop)")
    runner.serve_forever(db_url=load_settings().db_url)


@app.command()
def serve(host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8501)) -> None:
    """Start the local Streamlit UI (Market Intelligence at M2, full UI at M8)."""
    import subprocess
    import sys

    app_path = Path(__file__).parent / "ui" / "app.py"
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.address", host,
        "--server.port", str(port),
        "--server.headless", "true",
    ]
    typer.echo(f"UI starting on http://{host}:{port} (Ctrl+C to stop)")
    raise typer.Exit(subprocess.call(cmd))


@app.command("score-run")
def score_run(
    limit: Optional[int] = typer.Option(None, help="Max companies to score"),
    company_id: Optional[int] = typer.Option(None, help="Score a single company"),
    tier: Optional[int] = typer.Option(None, help="Only this tier"),
    rescore: bool = typer.Option(False, help="Re-score companies that already have a score run"),
) -> None:
    """Run the M5 rule chain: match products + personas, score, assign tiers."""
    from sqlalchemy import select as _select

    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.db.models import Company
    from buyerintel.llm.router import LLMRouter
    from buyerintel.score import rules as score_rules

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    install_audit(type(session))
    try:
        session.commit()
        ctx = score_rules.load_context(llm=LLMRouter(session))
        if company_id is not None:
            companies = [session.scalar(_select(Company).where(Company.id == company_id))]
            if companies[0] is None:
                typer.echo(f"no company {company_id}")
                raise typer.Exit(1)
        else:
            from buyerintel.score.scoring import pending_companies

            companies = pending_companies(session, limit=limit,
                                          tiers=[tier] if tier is not None else None)
            if rescore:
                query = _select(Company)
                if tier is not None:
                    query = query.where(Company.tier == tier)
                companies = list(session.scalars(query).all())[:limit] if limit else \
                    list(session.scalars(query).all())
        counts: dict[str, int] = {}
        for company in companies:
            result = score_rules.run_rule_chain(session, company, ctx)
            counts[result.state or result.reason or "?"] = counts.get(result.state or result.reason or "?", 0) + 1
        session.commit()
        typer.echo(f"scored {len(companies)} companies")
        for state, count in sorted(counts.items()):
            typer.echo(f"  {state}: {count}")
        for company in companies[:20]:
            typer.echo(f"  #{company.id} {company.canonical_name}: "
                       f"tier={company.tier} score={company.score} state={company.state}"
                       f"({company.state_reason})")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("score-show")
def score_show(company_id: int = typer.Argument(..., help="Company id")) -> None:
    """Print the latest score breakdown for a company (reproducible)."""
    from sqlalchemy import select as _select

    from buyerintel.config import load_settings
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.db.models import Company, ScoreRun

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    try:
        company = session.scalar(_select(Company).where(Company.id == company_id))
        if company is None:
            typer.echo(f"no company {company_id}")
            raise typer.Exit(1)
        run = session.scalar(
            _select(ScoreRun).where(ScoreRun.company_id == company_id)
            .order_by(ScoreRun.id.desc())
        )
        if run is None:
            typer.echo(f"company {company_id} has no score run")
            raise typer.Exit(1)
        components = (run.components or {}).get("components", {})
        typer.echo(f"{company.canonical_name}: total={run.total} tier={run.tier} "
                   f"weights=v{run.weights_version} state={company.state}({company.state_reason})")
        for name, points in components.items():
            typer.echo(f"  {name}: {points}")
        detail = (run.components or {}).get("detail", {})
        for name, info in detail.items():
            typer.echo(f"  [{name}] {info}")
    finally:
        session.close()


@app.command("score-tune")
def score_tune() -> None:
    """Auto-tune weights (no-op until 200 outcome-labelled leads exist)."""
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.score import autotune

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    install_audit(type(session))
    try:
        report = autotune.tune(session)
        session.commit()
        typer.echo(str(report))
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("people-list")
def people_list(
    company_id: Optional[int] = typer.Option(None, help="Only this company"),
    decision_makers_only: bool = typer.Option(False, "--decision-makers", help="Only decision-makers"),
) -> None:
    """List people with seniority, decision-maker flag and contacts (M6)."""
    from sqlalchemy import select as _select

    from buyerintel.config import load_settings
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.db.models import Company, ContactPoint, Person

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    try:
        query = _select(Person).order_by(Person.company_id, Person.id)
        if company_id is not None:
            query = query.where(Person.company_id == company_id)
        if decision_makers_only:
            query = query.where(Person.is_decision_maker.is_(True))
        people = session.scalars(query).all()
        if not people:
            typer.echo("no people found")
            return
        companies = {c.id: c.canonical_name for c in session.scalars(_select(Company)).all()}
        for person in people:
            contacts = session.scalars(
                _select(ContactPoint).where(ContactPoint.owner_type == "person",
                                            ContactPoint.owner_id == person.id)
            ).all()
            contact_text = "; ".join(f"{c.kind}:{c.value}" for c in contacts) or "-"
            typer.echo(
                f"#{person.id} {companies.get(person.company_id, '?')} | "
                f"{person.full_name} | {person.title or '-'} | {person.seniority or '-'} | "
                f"dm={'yes' if person.is_decision_maker else 'no'} | {contact_text}"
            )
    finally:
        session.close()


@app.command("paid-gate-status")
def paid_gate_status() -> None:
    """Show S7 paid-tool gate config, credits used this month, eligible companies."""
    from sqlalchemy import select as _select

    from buyerintel.config import load_settings
    from buyerintel.db.engine import get_engine, get_session, init_db
    from buyerintel.db.models import Company
    from buyerintel.enrich.paid import PaidToolGate, credits_used_this_month

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    try:
        gate = PaidToolGate(session)
        used = credits_used_this_month(session)
        budget = float(gate.gate_cfg.get("monthly_credit_budget", 10.0))
        typer.echo(f"enabled={gate.gate_cfg.get('enabled', False)} "
                   f"min_score={gate.gate_cfg.get('min_score')} "
                   f"credits={used}/{budget} budget_left={gate.budget_left():g}")
        for company in session.scalars(_select(Company)).all():
            eligible, reason = gate.eligibility(company)
            if eligible:
                typer.echo(f"  ELIGIBLE #{company.id} {company.canonical_name} "
                           f"(score={company.score})")
    finally:
        session.close()


def _open_session():
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db

    init_db(get_engine(load_settings().db_url))
    session = get_session(load_settings().db_url)
    install_audit(type(session))
    return session


@app.command("shipments-import")
def shipments_import(
    file: str = typer.Argument(..., help="Path to the bill-of-lading CSV"),
    source: str = typer.Option("csv", help="Source label stored on the rows"),
) -> None:
    """Import a shipments CSV (generic column mapping, config/shipments.yaml)."""
    from buyerintel.shipments.csv_import import import_shipments_csv

    session = _open_session()
    try:
        stats = import_shipments_csv(session, file, source=source)
        session.commit()
        typer.echo(stats.summary())
        for skip in stats.skipped[:10]:
            typer.echo(f"  skipped: {skip}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("shipments-fetch")
def shipments_fetch(
    tier: Optional[int] = typer.Option(1, help="Fetch for this tier (S6 is Tier 1)"),
    limit: Optional[int] = typer.Option(None, help="Max companies this run"),
) -> None:
    """Fetch shipments for companies via the enabled ShipmentSources (M7)."""
    from sqlalchemy import select as _select

    from buyerintel.db.models import Company
    from buyerintel.shipments.us_bol import ImportYetiPublicSource

    session = _open_session()
    try:
        query = _select(Company).where(Company.tier == tier).order_by(Company.id)
        companies = session.scalars(query).all()
        if limit is not None:
            companies = companies[:limit]
        source = ImportYetiPublicSource(session)
        if not source.enabled():
            typer.echo("us_bol source is disabled in config/shipments.yaml")
            return
        for company in companies:
            result = source.run_for_company(company)
            typer.echo(f"#{company.id} {company.canonical_name}: {result}")
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("forecast")
def forecast(
    company_id: Optional[int] = typer.Option(None, help="Only this company"),
) -> None:
    """Compute/refresh order forecasts from shipments; print the windows."""
    from buyerintel.forecast import forecast_all

    session = _open_session()
    try:
        rows = forecast_all(session, company_id=company_id)
        session.commit()
        if not rows:
            typer.echo("no forecasts (needs >= 2 dated shipments per company)")
            return
        for f in rows:
            typer.echo(
                f"#{f.company_id} {f.product_key} | n={f.n_shipments} "
                f"conf={f.confidence} | last={f.last_shipment:%Y-%m-%d} "
                f"median={f.median_interval_days}d | "
                f"window={f.window_start:%Y-%m-%d}..{f.window_end:%Y-%m-%d} | "
                f"qty={f.qty_min}-{f.qty_max} | "
                f"value=${f.value_min_usd}-{f.value_max_usd} "
                f"(uv={f.unit_value_usd} from {f.uv_source})"
            )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("outreach-log")
def outreach_log(
    company_id: int = typer.Option(..., help="Company id"),
    channel: str = typer.Option(..., help="email|phone|whatsapp|linkedin|..."),
    direction: str = typer.Option("out", help="out|in"),
    outcome: Optional[str] = typer.Option(None, help="sent|opened|replied|bounced|no_reply|..."),
    subject: Optional[str] = typer.Option(None),
    notes: Optional[str] = typer.Option(None),
) -> None:
    """Log one contact event (the system logs and recommends; it never sends)."""
    from buyerintel.crm.outreach import log_outreach

    session = _open_session()
    try:
        event = log_outreach(session, company_id=company_id, channel=channel,
                             direction=direction, outcome=outcome,
                             subject=subject, notes=notes)
        session.commit()
        typer.echo(f"logged #{event.id} {direction} {channel} for company {company_id}")
    finally:
        session.close()


@app.command("outreach-next")
def outreach_next(
    tier: Optional[int] = typer.Option(None, help="Only this tier"),
    limit: Optional[int] = typer.Option(20),
) -> None:
    """Next-action engine: one suggested action per company."""
    from buyerintel.crm.outreach import next_actions

    session = _open_session()
    try:
        rows = next_actions(session, tiers=[tier] if tier else None, limit=limit)
        for row in rows:
            channel = f" via {row['channel']}" if row.get("channel") else ""
            typer.echo(f"#{row['company_id']} {row['name']}: "
                       f"{row['action']}{channel} ({row['reason']})")
    finally:
        session.close()


@app.command("outreach-list")
def outreach_list(
    never_contacted: bool = typer.Option(False, "--never-contacted",
                                         help="Companies never contacted"),
    company_id: Optional[int] = typer.Option(None, help="History for one company"),
) -> None:
    """Outreach filters: --never-contacted, or one company's history."""
    from buyerintel.crm.outreach import last_outreach, never_contacted as never

    session = _open_session()
    try:
        if never_contacted:
            companies = never(session)
            typer.echo(f"never contacted: {len(companies)}")
            for company in companies:
                typer.echo(f"  #{company.id} {company.canonical_name}")
            return
        if company_id is None:
            typer.echo("give --never-contacted or --company-id")
            return
        event = last_outreach(session, company_id)
        if event is None:
            typer.echo(f"#{company_id}: never contacted")
            return
        typer.echo(f"#{company_id} last: {event.ts:%Y-%m-%d %H:%M} "
                   f"{event.direction} {event.channel} ({event.outcome or '?'})")
    finally:
        session.close()


@app.command("sample-offer")
def sample_offer(
    company_id: int = typer.Option(...),
    status: str = typer.Option("offered", help="offered|shipped|delivered|feedback|closed"),
    product_ids: str = typer.Option("", help="Comma-separated product ids"),
) -> None:
    """Log a sample offer with the FIXED terms text (free sample; buyer pays shipping)."""
    from buyerintel.crm.samples import SAMPLE_TERMS, offer_sample

    session = _open_session()
    try:
        ids = [int(x) for x in product_ids.split(",") if x.strip()]
        sample = offer_sample(session, company_id=company_id, product_ids=ids, status=status)
        session.commit()
        typer.echo(f"sample #{sample.id} {status} for company {company_id}")
        typer.echo(f"terms: {SAMPLE_TERMS}")
    finally:
        session.close()


@app.command("sample-status")
def sample_status(
    sample_id: int = typer.Argument(...),
    status: str = typer.Argument(...),
) -> None:
    """Update a sample's status."""
    from buyerintel.crm.samples import set_status

    session = _open_session()
    try:
        sample = set_status(session, sample_id, status)
        session.commit()
        typer.echo(f"sample #{sample.id}: {sample.status}")
    except (LookupError, ValueError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1)
    finally:
        session.close()


@app.command("quote-log")
def quote_log(
    company_id: int = typer.Option(...),
    price: float = typer.Option(..., help="Calendula's own price - user's call"),
    currency: str = typer.Option("USD"),
    incoterm: str = typer.Option("FOB"),
    qty: Optional[float] = typer.Option(None),
    hs: Optional[str] = typer.Option(None, help="Show the trade-data benchmark for this HS"),
) -> None:
    """Record a quote (date, currency, amount, incoterm). No stored price list."""
    from buyerintel.crm.quotes import benchmark_unit_value, log_quote

    session = _open_session()
    try:
        quote = log_quote(session, company_id=company_id, price=price,
                          currency=currency, incoterm=incoterm, qty=qty)
        session.commit()
        typer.echo(f"quote #{quote.id} {price} {currency} {incoterm} for company {company_id}")
        if hs:
            bench = benchmark_unit_value(session, hs)
            if bench:
                typer.echo(f"benchmark (reference only): {bench}")
    finally:
        session.close()


@app.command("channel-test")
def channel_test(
    company_id: int = typer.Argument(...),
) -> None:
    """Channel recommender: ranked channels for one company (re-learned from replies)."""
    from sqlalchemy import select as _select

    from buyerintel.crm.channels import recommend
    from buyerintel.db.models import Company

    session = _open_session()
    try:
        company = session.scalar(_select(Company).where(Company.id == company_id))
        if company is None:
            typer.echo(f"company #{company_id} not found")
            raise typer.Exit(1)
        for row in recommend(session, company):
            observed = f"{row['observed']:.2f}" if row["observed"] is not None else "-"
            typer.echo(f"{row['channel']}: score={row['score']} "
                       f"(prior={row['prior']} observed={observed} src={row['source']})")
    finally:
        session.close()


@app.command("content-draft")
def content_draft(
    subject: Optional[str] = typer.Option(None, help="Product to draft for (blank = all trending)"),
    trend_index: float = typer.Option(80.0),
) -> None:
    """Draft B2B content for trending herbs (nothing is posted automatically)."""
    from buyerintel.crm import content as content_mod

    session = _open_session()
    try:
        if subject:
            item = content_mod.draft_content(session, subject=subject,
                                             trend_index=trend_index)
            session.commit()
            typer.echo(f"draft #{item.id}: {item.title}")
        else:
            drafts = content_mod.draft_for_trends(session)
            session.commit()
            for item in drafts:
                typer.echo(f"draft #{item.id}: {item.title}")
            if not drafts:
                typer.echo("no trending products above the threshold")
    finally:
        session.close()


@app.command("content-suggest")
def content_suggest(
    content_id: int = typer.Argument(...),
) -> None:
    """Suggestion engine: send/skip per company with reasons."""
    from sqlalchemy import select as _select

    from buyerintel.crm import content as content_mod
    from buyerintel.db.models import Company, ContentItem

    session = _open_session()
    try:
        item = session.get(ContentItem, content_id)
        if item is None:
            typer.echo(f"content #{content_id} not found")
            raise typer.Exit(1)
        rows = content_mod.suggest_recipients(session, item)
        session.commit()
        names = {c.id: c.canonical_name
                 for c in session.scalars(_select(Company)).all()}
        for row in rows:
            typer.echo(f"{row.decision.upper()}: {names.get(row.company_id, row.company_id)} "
                       f"- {row.reason}")
    finally:
        session.close()


@app.command("export")
def export(
    out: Optional[str] = typer.Option(None, help="Output .xlsx path (default data/)"),
) -> None:
    """Generate the multi-sheet Excel workbook (summary, tiers, personas, contacts...)."""
    from buyerintel.export.workbook import export_workbook

    session = _open_session()
    try:
        path = out or str(Path("data") / f"buyerintel_{datetime.now():%Y%m%d_%H%M}.xlsx")
        written = export_workbook(session, path)
        typer.echo(f"workbook written: {written}")
    finally:
        session.close()


if __name__ == "__main__":
    app()

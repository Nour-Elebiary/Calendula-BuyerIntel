"""CLI entry point (spec section 17): buyerintel <command>.

Commands are small, testable wrappers around library functions.
"""
from __future__ import annotations

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
    """List configured scheduler jobs."""
    from buyerintel.config import load_settings

    jobs = load_settings().schedules.get("jobs", [])
    if not jobs:
        typer.echo("No jobs configured (config/schedules.yaml).")
        return
    for job in jobs:
        state = "enabled" if job.get("enabled", True) else "disabled"
        typer.echo(f"{job.get('id')}  {job.get('cron', '')}  [{state}]")


@app.command()
def run(job_id: str = typer.Argument(..., help="Job id from config/schedules.yaml")) -> None:
    """Run a scheduler job once (stub until the scheduler module lands)."""
    typer.echo(f"Job '{job_id}' is registered; execution lands with the sched module.")


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


if __name__ == "__main__":
    app()

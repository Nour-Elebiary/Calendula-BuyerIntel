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
    """Upsert the 42-product catalog from config/products.yaml (idempotent)."""
    from buyerintel.catalog import seed_products
    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db

    settings = load_settings()
    init_db(get_engine(settings.db_url))
    session = get_session(settings.db_url)
    install_audit(type(session))
    try:
        stats = seed_products(session)
        session.commit()
        typer.echo(stats.summary())
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
    """Start the local UI (stub until the ui module lands)."""
    typer.echo(f"UI will serve on http://{host}:{port} at milestone M8.")


if __name__ == "__main__":
    app()

"""Built-in scheduler (spec section 17): no external service.

APScheduler (3.x) with a SQLite job store, on top of the project's own
`job` + `job_run` tables (pause/resume state and run history live there -
audit-friendly and readable by the UI).

- jobs are seeded from config/schedules.yaml (id, cron, task, params,
  enabled, retries); the job table is the SOURCE OF TRUTH afterwards
- misfire handling: when the PC was off, missed runs execute on startup
  within config misfire_grace_s, COALESCED (several missed fires -> one run)
- CLI: buyerintel run <job> | buyerintel jobs list | buyerintel serve
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Job, JobRun
from buyerintel.sched.tasks import TASKS

DEFAULT_GRACE_S = 3600


def seed_jobs(session: Session, *, config: dict | None = None) -> dict:
    """Upsert config/schedules.yaml jobs into the job table (idempotent).

    The task name rides in params['_task']; the job table keeps the user's
    pause/resume decision (enabled is only SET on first seed).
    """
    cfg = config or load_yaml("schedules.yaml")
    created = 0
    for spec in cfg.get("jobs") or []:
        job_id = spec.get("id")
        if not job_id:
            continue
        row = session.scalar(select(Job).where(Job.job_id == job_id))
        if row is None:
            row = Job(job_id=job_id)
            session.add(row)
            created += 1
        row.cron = spec.get("cron")
        row.params = {**(row.params or {}), "_task": spec.get("task", job_id)}
        row.retries = int(spec.get("retries", 0))
        if row.enabled is None:   # first seed: yaml decides, later the user does
            row.enabled = bool(spec.get("enabled", True))
    session.flush()
    return {"created": created, "total": len(cfg.get("jobs") or [])}


def _task_name(job: Job) -> str:
    return (job.params or {}).get("_task", job.job_id)


def run_job(session: Session, job: Job, *, reason: str = "manual") -> JobRun:
    """Execute one job NOW (direct dispatch, no scheduler loop), record it."""
    run = JobRun(job_id=job.job_id, status="ok")
    session.add(run)
    session.flush()
    name = _task_name(job)
    fn = TASKS.get(name)
    if fn is None:
        run.status = "error"
        run.error = f"unknown task {name!r}"
        run.finished_at = datetime.now(timezone.utc)
        session.flush()
        return run
    attempts = max(1, (job.retries or 0) + 1)
    for attempt in range(1, attempts + 1):
        try:
            summary = fn(session, **(job.params or {}))
            run.status = "ok"
            run.error = summary if attempt > 1 else None
            break
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            if attempt >= attempts:
                run.status = "error"
                run.error = f"attempt {attempt}: {str(exc)[:400]}"
            else:
                # a failed attempt may have left dirty state; the rollback
                # also discards the JobRun row, so re-add it before retrying
                session.rollback()
                run = JobRun(job_id=job.job_id, status="ok")
                session.add(run)
                session.flush()
    run.finished_at = datetime.now(timezone.utc)
    session.flush()
    return run


# ------------------------------------------------------------ misfire logic
def missed_jobs(session: Session, *, now: datetime | None = None,
                grace_s: int | None = None) -> list[dict]:
    """Enabled cron jobs whose scheduled fire times since the last run were
    missed (PC off), COALESCED to one entry each, within the grace window."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:      # SQLite stores naive datetimes; APScheduler
        now = now.replace(tzinfo=timezone.utc)  # fires are AWARE - align them
    grace = int(grace_s if grace_s is not None else
                (load_yaml("schedules.yaml").get("misfire_grace_s") or DEFAULT_GRACE_S))
    out: list[dict] = []
    for job in session.scalars(select(Job)).all():
        if not job.enabled or not job.cron:
            continue
        last = session.scalar(
            select(JobRun.finished_at).where(JobRun.job_id == job.job_id,
                                             JobRun.status == "ok")
            .order_by(JobRun.finished_at.desc()).limit(1))
        anchor = last.replace(tzinfo=timezone.utc) if (last and last.tzinfo is None) else last
        anchor = anchor or (now - timedelta(days=365))
        try:
            # crons are interpreted in UTC - the same clock the job_run
            # timestamps use, so catch-up and live firing agree
            trigger = CronTrigger.from_crontab(job.cron, timezone=timezone.utc)
        except ValueError:
            continue
        # only fires inside the grace window can trigger a catch-up; walking
        # from max(anchor, now-grace) keeps the walk short and coalesced
        fire = max(anchor, now - timedelta(seconds=grace + 1))
        missed = 0
        while True:
            nxt = trigger.get_next_fire_time(fire, now)
            if nxt is None:
                break
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            if nxt >= now:
                break       # future fire - nothing missed yet
            missed += 1
            fire = nxt
            if missed > 32:   # paranoid cap
                break
        if missed:
            out.append({"job_id": job.job_id, "missed_fires": missed,
                        "coalesced": missed > 1})
    return out


def startup_catchup(session: Session, *, now: datetime | None = None) -> list[str]:
    """Run each missed job ONCE on startup (coalescing)."""
    ran = []
    for entry in missed_jobs(session, now=now):
        job = session.scalar(select(Job).where(Job.job_id == entry["job_id"]))
        run_job(session, job, reason="startup_catchup")
        session.commit()
        ran.append(entry["job_id"])
    return ran


def list_jobs(session: Session) -> list[dict]:
    """Job rows + their last run for the UI/CLI."""
    out = []
    for job in session.scalars(select(Job).order_by(Job.job_id)).all():
        last = session.scalar(
            select(JobRun).where(JobRun.job_id == job.job_id)
            .order_by(JobRun.started_at.desc()).limit(1))
        out.append({
            "job_id": job.job_id, "cron": job.cron, "enabled": job.enabled,
            "retries": job.retries, "task": _task_name(job),
            "last_run": (last.started_at.isoformat() if last and last.started_at else None),
            "last_status": last.status if last else None,
        })
    return out


def pause(session: Session, job_id: str) -> Job:
    job = session.scalar(select(Job).where(Job.job_id == job_id))
    if job is None:
        raise LookupError(f"job {job_id!r} not found")
    job.enabled = False
    session.flush()
    return job


def resume(session: Session, job_id: str) -> Job:
    job = session.scalar(select(Job).where(Job.job_id == job_id))
    if job is None:
        raise LookupError(f"job {job_id!r} not found")
    job.enabled = True
    session.flush()
    return job


# ------------------------------------------------------- the live loop
def serve_forever(*, db_url: str | None = None, poll_seconds: int = 30) -> None:
    """APScheduler BlockingScheduler with the SQLite job store.

    Jobs are re-added at every start (idempotent replace_existing), so a
    restart never loses a schedule; APScheduler's misfire_grace_time +
    coalesce give the same startup semantics as missed_jobs() while running.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    from buyerintel.config import load_settings
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db

    settings = load_settings()
    init_db(get_engine(settings.db_url))
    session = get_session(settings.db_url)
    install_audit(type(session))
    seed_jobs(session)
    session.commit()

    scheduler = BlockingScheduler(
        jobstores={"default": SQLAlchemyJobStore(engine=get_engine(settings.db_url))},
        job_defaults={"coalesce": True,
                      "misfire_grace_time": int(load_yaml("schedules.yaml")
                                                .get("misfire_grace_s") or DEFAULT_GRACE_S)},
    )
    for job in session.scalars(select(Job)).all():
        if not job.enabled or not job.cron:
            continue
        scheduler.add_job(
            _run_in_scheduler, CronTrigger.from_crontab(job.cron, timezone=timezone.utc),
            id=job.job_id, replace_existing=True,
            kwargs={"job_id": job.job_id, "db_url": settings.db_url},
        )
    scheduler.start()


def _run_in_scheduler(job_id: str, db_url: str) -> None:
    """APScheduler entrypoint: fresh session per fire, run + record."""
    from buyerintel.db.audit import install_audit
    from buyerintel.db.engine import get_engine, get_session, init_db

    init_db(get_engine(db_url))
    session = get_session(db_url)
    install_audit(type(session))
    try:
        job = session.scalar(select(Job).where(Job.job_id == job_id))
        if job is not None:
            run_job(session, job, reason="scheduled")
        session.commit()
    finally:
        session.close()

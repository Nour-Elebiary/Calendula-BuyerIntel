"""Scheduler tests (spec section 17, M8): job dispatch + run history,
retries, pause/resume, seeding, and the startup misfire catch-up
(scheduler survives restart; missed runs execute within the grace window,
coalesced)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from buyerintel.db.models import Job, JobRun
from buyerintel.sched import runner
from buyerintel.sched.tasks import TASKS, register_task


@pytest.fixture(autouse=True)
def _fake_task():
    calls: list[str] = []

    def fake_task(session, **params):
        calls.append("ok")
        return f"ran n={len(calls)}"

    register_task("fake", fake_task)

    def failing(session, **params):
        calls.append("fail")
        raise RuntimeError("boom")

    register_task("failing", failing)
    yield calls
    TASKS.pop("fake", None)
    TASKS.pop("failing", None)


def _job(session, job_id="fake_job", cron="0 * * * *", enabled=True, retries=0,
         params=None):
    job = Job(job_id=job_id, cron=cron, enabled=enabled, retries=retries,
              params={"_task": "fake", **(params or {})})
    session.add(job)
    session.flush()
    return job


def test_run_job_records_ok(session):
    job = _job(session)
    run = runner.run_job(session, job)
    assert run.status == "ok" and run.finished_at is not None
    assert session.query(JobRun).filter_by(job_id="fake_job").count() == 1


def test_run_job_retries_then_errors(session):
    job = _job(session, job_id="bad_job", params={"_task": "failing"}, retries=2)
    run = runner.run_job(session, job)
    assert run.status == "error"
    assert "attempt 3" in run.error        # 1 + 2 retries recorded
    history = session.query(JobRun).filter_by(job_id="bad_job").all()
    assert len(history) == 1               # one run row, retry loop inside


def test_unknown_task_recorded_not_raised(session):
    job = _job(session, job_id="ghost", params={"_task": "does_not_exist"})
    run = runner.run_job(session, job)
    assert run.status == "error"
    assert "unknown task" in run.error


def test_seed_jobs_idempotent_and_preserves_pause(session):
    result = runner.seed_jobs(session, config={"jobs": [
        {"id": "seeded", "cron": "0 6 * * 1", "task": "fake", "enabled": True},
    ]})
    assert result["created"] == 1
    runner.pause(session, "seeded")
    session.commit()
    # re-seed must NOT un-pause the user's decision
    runner.seed_jobs(session, config={"jobs": [
        {"id": "seeded", "cron": "0 6 * * 1", "task": "fake", "enabled": True},
    ]})
    assert session.query(Job).filter_by(job_id="seeded").one().enabled is False
    runner.resume(session, "seeded")
    assert session.query(Job).filter_by(job_id="seeded").one().enabled is True


def test_list_jobs_shape(session):
    _job(session)
    rows = runner.list_jobs(session)
    assert rows[0]["job_id"] == "fake_job"
    assert rows[0]["task"] == "fake"
    assert rows[0]["last_status"] is None


# ------------------------------------------------------- misfire / catchup
# Fixed naive-UTC clock: the runner treats naive datetimes as UTC and builds
# cron triggers in UTC, so these tests are deterministic on any machine.
NOW = datetime(2026, 3, 10, 14, 0)


def _mark_ok(session, job_id, finished_at):
    run = JobRun(job_id=job_id, status="ok", finished_at=finished_at)
    session.add(run)
    session.flush()


def test_missed_within_grace_is_caught_up(session):
    job = _job(session, cron="*/30 * * * *")  # every 30 minutes
    _mark_ok(session, "fake_job", NOW - timedelta(days=3, hours=2))
    session.flush()
    missed = runner.missed_jobs(session, now=NOW, grace_s=3600)
    assert len(missed) == 1
    assert missed[0]["coalesced"] is True   # 13:00 and 13:30 coalesced to one
    ran = runner.startup_catchup(session, now=NOW)
    assert ran == ["fake_job"]
    run = session.query(JobRun).filter_by(job_id="fake_job") \
        .order_by(JobRun.started_at.desc()).first()
    assert run.status == "ok"


def test_no_catchup_when_fresh(session):
    job = _job(session, cron="0 * * * *")
    _mark_ok(session, "fake_job", NOW - timedelta(minutes=5))
    session.flush()
    assert runner.missed_jobs(session, now=NOW, grace_s=3600) == []
    assert runner.startup_catchup(session, now=NOW) == []


def test_missed_beyond_grace_is_skipped(session):
    """The 13:00 fire was missed but is already 60 min old - older than the
    30-min grace window - so nothing executes on startup."""
    _job(session, cron="0 * * * *")
    _mark_ok(session, "fake_job", NOW - timedelta(minutes=90))
    session.flush()
    assert runner.missed_jobs(session, now=NOW, grace_s=1800) == []


def test_paused_and_cronless_jobs_never_catch_up(session):
    _job(session, enabled=False)
    _job(session, job_id="no_cron", cron=None)
    _mark_ok(session, "fake_job", NOW - timedelta(days=3))
    _mark_ok(session, "no_cron", NOW - timedelta(days=3))
    session.flush()
    assert runner.missed_jobs(session, now=NOW, grace_s=3600) == []


def test_every_typical_task_dispatches(session):
    """All 8 typical jobs are registered and run against an empty DB."""
    from buyerintel.sched import tasks

    expected = ["market_refresh", "enrich_queue", "dedupe_sweep",
                "score_recompute", "forecast_refresh", "content_suggestions",
                "archive_sweep", "expo_refresh"]
    for name in expected:
        assert name in TASKS, f"{name} not registered"
        summary = TASKS[name](session)          # empty DB: must not raise
        assert isinstance(summary, str)
    session.commit()

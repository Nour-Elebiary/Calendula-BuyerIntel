from __future__ import annotations

from typer.testing import CliRunner

from buyerintel.cli import app

runner = CliRunner()


def test_jobs_list_shows_seeded_typical_jobs():
    result = runner.invoke(app, ["jobs"])
    assert result.exit_code == 0
    for job_id in ("market_refresh", "enrich_queue", "dedupe_sweep",
                   "score_recompute", "forecast_refresh", "content_suggestions",
                   "archive_sweep", "expo_refresh"):
        assert job_id in result.output


def test_run_executes_job_and_records_it():
    result = runner.invoke(app, ["run", "dedupe_sweep"])
    assert result.exit_code == 0
    assert "dedupe_sweep" in result.output and "ok" in result.output


def test_serve_launches_streamlit(monkeypatch):
    import subprocess

    called: dict = {}

    def fake_call(cmd):
        called["cmd"] = cmd
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0
    assert any("app.py" in str(part) for part in called["cmd"])
    assert any("streamlit" in str(part) for part in called["cmd"])

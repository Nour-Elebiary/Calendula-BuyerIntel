from __future__ import annotations

from typer.testing import CliRunner

from buyerintel.cli import app

runner = CliRunner()


def test_jobs_list_empty_config_is_graceful():
    result = runner.invoke(app, ["jobs"])
    assert result.exit_code == 0
    assert "No jobs configured" in result.output


def test_run_reports_pending_module():
    result = runner.invoke(app, ["run", "expo_refresh"])
    assert result.exit_code == 0


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

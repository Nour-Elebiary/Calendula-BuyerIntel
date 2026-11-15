"""Non-login LinkedIn public scraper tests (ADD-012, spec 12.8, M6).

Acceptance: "the non-login scraper enriches fixture profiles with no
account involved" - the parser runs over saved HTML fixtures; the fetch
sends NO cookies/credentials and degrades to None on auth walls.
"""
from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def profile_html() -> str:
    return (FIXTURES / "linkedin_profile_public.html").read_text(encoding="utf-8")


@pytest.fixture()
def authwall_html() -> str:
    return (FIXTURES / "linkedin_authwall.html").read_text(encoding="utf-8")


def test_parses_fixture_profile_no_account(profile_html):
    from buyerintel.enrich.people.linkedin_public import parse_profile_html

    profile = parse_profile_html(profile_html)
    assert profile is not None
    assert profile["full_name"] == "Klaus Mueller"
    assert profile["title"] == "Head of Purchasing"  # from ' at ' split
    assert "Hamburg" in (profile.get("location") or "")
    assert profile["email"] == "k.mueller@rhein-gewuerze.example"
    assert profile["source"] == "linkedin_public"


def test_authwall_returns_none_never_invents(authwall_html):
    from buyerintel.enrich.people.linkedin_public import parse_profile_html

    assert parse_profile_html(authwall_html) is None
    assert parse_profile_html("") is None
    assert parse_profile_html("   ") is None


def test_fetch_sends_no_cookies_or_credentials(monkeypatch):
    """No account involved: no cookie header, no auth header, ever."""
    from buyerintel.enrich.people import linkedin_public

    captured = {}

    class FakeResponse:
        status_code = 200
        text = (FIXTURES / "linkedin_profile_public.html").read_text(encoding="utf-8")

    def fake_get(url, *, headers, timeout, follow_redirects):
        captured["headers"] = dict(headers)
        return FakeResponse()

    monkeypatch.setattr(linkedin_public.httpx, "get", fake_get)
    monkeypatch.setattr(linkedin_public.time, "sleep", lambda s: None)
    profile = linkedin_public.fetch_profile("https://de.linkedin.com/in/klaus-mueller")
    assert profile is not None and profile["full_name"] == "Klaus Mueller"
    assert "cookie" not in {k.lower() for k in captured["headers"]}
    assert "authorization" not in {k.lower() for k in captured["headers"]}


def test_fetch_disabled_by_config_returns_none(monkeypatch):
    from buyerintel.enrich.people import linkedin_public

    monkeypatch.setattr(linkedin_public.time, "sleep", lambda s: None)
    config = {"linkedin": {"public_scraper": {"enabled": False}}}
    assert linkedin_public.fetch_profile("https://linkedin.com/in/x",
                                         config=config) is None


def test_fetch_network_error_degrades(monkeypatch):
    from buyerintel.enrich.people import linkedin_public

    def boom(url, *, headers, timeout, follow_redirects):
        raise OSError("network down")

    monkeypatch.setattr(linkedin_public.httpx, "get", boom)
    monkeypatch.setattr(linkedin_public.time, "sleep", lambda s: None)
    assert linkedin_public.fetch_profile("https://linkedin.com/in/x") is None

"""Enrichment fetch ladder tests (spec 12.1): rung order, flags, caching (M4)."""
from __future__ import annotations

import pytest

from buyerintel.enrich import fetch as fetch_mod
from buyerintel.enrich.fetch import BlockedError, FetchLadder, FetchResult, looks_blocked


@pytest.fixture()
def ladder(session):
    return FetchLadder(session, config={"ladder": {
        "rung2_scrapling_stealth": True,
        "rung3_scrapling_dynamic": True,
        "rung4_botasaurus": False,
        "rung5_flaresolverr": False,
        "rung6_captcha": False,
    }})


def test_looks_blocked_detects_status_and_challenges():
    assert looks_blocked(403, "")
    assert looks_blocked(429, "")
    assert looks_blocked(503, "")
    assert looks_blocked(200, "<html>Just a moment...</html>")
    assert looks_blocked(None, "cf_chl_opt challenge")
    assert not looks_blocked(200, "<html>normal content</html>")
    assert not looks_blocked(404, "not found")  # a 404 is not a block


def test_first_rung_success_never_touches_later_rungs(ladder, monkeypatch):
    called = []

    def fake_rung1(url, timeout):
        called.append(1)
        return "<html>ok</html>"

    def fail_rung(rung):
        def _inner(url):
            called.append(rung)
            raise BlockedError(rung, "blocked")
        return _inner

    monkeypatch.setattr(fetch_mod, "_rung1_direct", fake_rung1)
    monkeypatch.setattr(fetch_mod, "_rung2_scrapling_stealth", fail_rung(2))
    monkeypatch.setattr(fetch_mod, "_rung3_scrapling_dynamic", fail_rung(3))
    monkeypatch.setattr(fetch_mod, "_rung4_botasaurus", fail_rung(4))

    result = ladder.fetch("https://example.com")
    ladder.session.commit()
    assert called == [1]                # later rungs never ran
    assert result.rung == 1


def test_ladder_escapes_only_after_block(ladder, monkeypatch):
    """Rung 2 runs only because rung 1 was blocked, and 3 only after 2."""
    order = []

    def fake_rung1(url, timeout):
        order.append(1)
        raise BlockedError(1, "403 challenge")

    def fake_rung2(url):
        order.append(2)
        raise BlockedError(2, "challenge page")

    def fake_rung3(url):
        order.append(3)
        return "<html>real content</html>"

    monkeypatch.setattr(fetch_mod, "_rung1_direct", fake_rung1)
    monkeypatch.setattr(fetch_mod, "_rung2_scrapling_stealth", fake_rung2)
    monkeypatch.setattr(fetch_mod, "_rung3_scrapling_dynamic", fake_rung3)

    result = ladder.fetch("https://example.com/blocked")
    ladder.session.commit()
    assert order == [1, 2, 3]
    assert result.rung == 3


def test_flagged_off_rungs_are_skipped(ladder, monkeypatch):
    """Rungs 4-6 are config-flagged off: never called even after rungs 1-3 block."""
    called = []

    def fake_rung(rung):
        def _inner(*a, **kw):
            called.append(rung)
            raise BlockedError(rung, "blocked")
        return _inner

    monkeypatch.setattr(fetch_mod, "_rung1_direct", fake_rung(1))
    monkeypatch.setattr(fetch_mod, "_rung2_scrapling_stealth", fake_rung(2))
    monkeypatch.setattr(fetch_mod, "_rung3_scrapling_dynamic", fake_rung(3))
    monkeypatch.setattr(fetch_mod, "_rung4_botasaurus", fake_rung(4))
    monkeypatch.setattr(fetch_mod, "_rung5_flaresolverr", fake_rung(5))
    monkeypatch.setattr(fetch_mod, "_rung6_captcha", fake_rung(6))

    with pytest.raises(BlockedError):
        ladder.fetch("https://example.com/hard")
    ladder.session.commit()
    assert called == [1, 2, 3]          # 4-6 skipped by flags, not by silence


def test_enabled_flag_unblocks_rung(session, monkeypatch):
    """Turning rung 4's flag on makes it part of the chain."""
    ladder = FetchLadder(session, config={"ladder": {
        "rung2_scrapling_stealth": True, "rung3_scrapling_dynamic": True,
        "rung4_botasaurus": True,
    }})
    order = []

    def fake_rung(rung):
        def _inner(*a, **kw):
            order.append(rung)
            raise BlockedError(rung, "blocked")
        return _inner

    monkeypatch.setattr(fetch_mod, "_rung1_direct", fake_rung(1))
    monkeypatch.setattr(fetch_mod, "_rung2_scrapling_stealth", fake_rung(2))
    monkeypatch.setattr(fetch_mod, "_rung3_scrapling_dynamic", fake_rung(3))
    monkeypatch.setattr(fetch_mod, "_rung4_botasaurus", lambda url: "<html>via bota</html>")

    result = ladder.fetch("https://example.com/hard")
    ladder.session.commit()
    assert order == [1, 2, 3]
    assert result.rung == 4


def test_404_is_permanent_failure_not_escalation(ladder, monkeypatch):
    """A 404 means the page does not exist: the ladder aborts, later rungs
    (browser, solver) are pointless and never run."""
    called = []

    def fake_rung1(url, timeout):
        called.append(1)
        raise BlockedError(1, "status 404", permanent=True)

    monkeypatch.setattr(fetch_mod, "_rung1_direct", fake_rung1)
    monkeypatch.setattr(fetch_mod, "_rung2_scrapling_stealth",
                        lambda url: called.append(2) or "<html>x</html>")
    monkeypatch.setattr(fetch_mod, "_rung3_scrapling_dynamic",
                        lambda url: called.append(3) or "<html>x</html>")

    with pytest.raises(BlockedError) as excinfo:
        ladder.fetch("https://example.com/missing")
    ladder.session.commit()
    assert excinfo.value.permanent
    assert called == [1]


def test_network_error_falls_through_to_next_rung(ladder, monkeypatch):
    """A rung-1 network error is a block signal for rung 2, not a crash."""
    def fake_rung1(url, timeout):
        raise BlockedError(1, "network error: connect timeout")

    monkeypatch.setattr(fetch_mod, "_rung1_direct", fake_rung1)
    monkeypatch.setattr(fetch_mod, "_rung2_scrapling_stealth",
                        lambda url: "<html>via stealth</html>")

    result = ladder.fetch("https://example.com/flaky")
    ladder.session.commit()
    assert result.rung == 2


def test_success_is_cached(ladder, monkeypatch):
    calls = []

    def fake_rung1(url, timeout):
        calls.append(url)
        return "<html>cached page</html>"

    monkeypatch.setattr(fetch_mod, "_rung1_direct", fake_rung1)
    first = ladder.fetch("https://example.com")
    ladder.session.commit()
    second = ladder.fetch("https://example.com")

    assert calls == ["https://example.com"]     # second fetch from cache
    assert second.from_cache
    assert second.html == first.html


def test_cache_ttl_expiry(session, monkeypatch):
    from datetime import datetime, timedelta, timezone

    ladder = FetchLadder(session)
    calls = []
    monkeypatch.setattr(fetch_mod, "_rung1_direct", lambda url, timeout: (calls.append(url), "<html>x</html>")[1])
    first = ladder.fetch("https://example.com")
    # Age the cache row past the TTL.
    row = session.execute(
        __import__("sqlalchemy").select(fetch_mod.SearchCache)
        .where(fetch_mod.SearchCache.cache_key == fetch_mod._page_cache_key("https://example.com"))
    ).scalar_one()
    row.fetched_at = datetime.now(timezone.utc) - timedelta(hours=fetch_mod.CACHE_TTL_HOURS + 1)
    session.commit()
    second = ladder.fetch("https://example.com")
    assert calls == ["https://example.com", "https://example.com"]
    assert not second.from_cache

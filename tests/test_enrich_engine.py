"""Enrichment pipeline engine tests (spec section 6, M4).

Fixture site yields expected contacts and tags; resumable after a forced
stop; prescore threshold stops S2 for weak companies; queue never samples.
"""
from __future__ import annotations

import pytest

from buyerintel.db.models import (
    Company,
    ContactPoint,
    EnrichCheckpoint,
    Person,
)
from buyerintel.enrich.engine import EnrichEngine, coverage, prescore
from buyerintel.enrich.fetch import BlockedError, FetchResult


@pytest.fixture()
def company(session):
    c = Company(canonical_name="Herbhandel GmbH", domain="herbhandel.example",
                country="DEU", tier=2)
    session.add(c)
    session.commit()
    return c


FIXTURE_HOME = """
<html lang="de"><head><title>Herbhandel GmbH - Kräuter und Gewürze</title></head>
<body>
<h1>Herbhandel GmbH</h1>
<p>Leading importer of organic herbs and spices. ISO 9001 certified. Halal certified facility.</p>
<a href="/contact">Contact</a><a href="/about">About</a><a href="/products">Products</a>
<a href="https://www.linkedin.com/company/herbhandel-gmbh">LinkedIn</a>
<a href="mailto:info@herbhandel.de">info@herbhandel.de</a>
</body></html>
"""

FIXTURE_CONTACT = """
<html lang="de"><head><title>Kontakt - Herbhandel GmbH</title></head>
<body>
<p>Purchasing: John Smith, john.smith@herbhandel.de, +49 40 123456</p>
<p>Export sales@herbhandel.de</p>
</body></html>
"""


@pytest.fixture()
def enrich_engine(session, monkeypatch):
    """Engine whose crawler is fed fixture HTML instead of the network."""
    eng = EnrichEngine(session, llm=None, search=None, config={
        "stages": {"prescore_threshold": 3.0},
        "crawl": {"discover_paths": [""], "max_pages": 6},
        "tags": {"organic": ["organic"], "halal": ["halal"], "iso": ["iso 9001"]},
        "ladder": {"rung2_scrapling_stealth": True, "rung3_scrapling_dynamic": True,
                   "rung4_botasaurus": False, "rung5_flaresolverr": False,
                   "rung6_captcha": False},
        "social": {"platforms": {}, "max_results_per_platform": 3},
        "jobs": {"role_keywords": ["purchasing"], "max_results": 5},
    })

    pages = {
        "https://herbhandel.example": FIXTURE_HOME,
        "https://herbhandel.example/contact": FIXTURE_CONTACT,
    }

    def fake_fetch(self, url):
        if url in pages:
            from buyerintel.enrich.fetch import FetchResult

            return FetchResult(url=url, html=pages[url], rung=1)
        raise BlockedError(1, "404")

    monkeypatch.setattr(type(eng.crawler.ladder), "fetch", fake_fetch)
    # archive.org must never be touched in tests
    monkeypatch.setattr("buyerintel.enrich.engine.fetch_archive_snapshots",
                        lambda domain, **kw: [])
    return eng


def test_prescore_deterministic_and_bounded(company):
    score = prescore(company)
    assert 0 <= score <= 10
    assert score >= 3.0  # herb keyword + importer keyword + domain + country
    empty = Company(canonical_name="Random Shop", domain=None, country=None)
    assert prescore(empty) < 2.0


def test_fixture_site_yields_contacts_and_tags(enrich_engine, session, company):
    enrich_engine.run_company(company)
    session.commit()

    contacts = {c.value: c for c in session.query(ContactPoint).filter(
        ContactPoint.owner_type == "company", ContactPoint.owner_id == company.id)}
    # regex contacts off the fixture HTML
    assert "info@herbhandel.de" in contacts
    assert any(c.kind == "email" and c.value == "john.smith@herbhandel.de"
               for c in contacts.values())
    assert any(c.kind == "linkedin" for c in contacts.values())
    # verification: published from own pages
    assert contacts["info@herbhandel.de"].verified_status == "published"
    # tags parsed from site text
    assert {"organic", "halal", "iso"} <= set(company.tags or [])


def test_checkpoint_all_stages_done(enrich_engine, session, company):
    enrich_engine.run_company(company)
    session.commit()
    checkpoint = session.query(EnrichCheckpoint).filter_by(company_id=company.id).one()
    assert checkpoint.stage == "s4"
    assert checkpoint.status == "done"


def test_resumable_after_forced_stop(enrich_engine, session, company, monkeypatch):
    """Crash during S2: S0/S1 stay committed; resume runs S2 once, never S1."""
    s2_calls, s1_calls = [], []
    real_s2 = enrich_engine._stage_s2

    def crashing_s2(c):
        s2_calls.append("s2")
        raise RuntimeError("forced stop")

    def spy_s1(c):
        s1_calls.append("s1")
        return {"prescore": 9.0, "s2": "queued"}

    monkeypatch.setattr(enrich_engine, "_stage_s2", crashing_s2)
    with pytest.raises(RuntimeError):
        enrich_engine.run_company(company)  # stages s0, s1 already committed inside
    session.commit()

    checkpoint = session.query(EnrichCheckpoint).filter_by(company_id=company.id).one()
    assert checkpoint.status == "running"
    assert checkpoint.stage == "s1"     # s1 done; crash hit s2

    # Resume with the real s2 (fixtures): s1 must not run again.
    resumed_s2 = []

    def spy_resumed_s2(c):
        resumed_s2.append("s2")
        return real_s2(c)

    monkeypatch.setattr(enrich_engine, "_stage_s1", spy_s1)
    monkeypatch.setattr(enrich_engine, "_stage_s2", spy_resumed_s2)
    enrich_engine.run_company(company)
    session.commit()

    assert s1_calls == []               # s1 never re-ran
    assert resumed_s2 == ["s2"]         # s2 ran exactly once on resume
    checkpoint = session.query(EnrichCheckpoint).filter_by(company_id=company.id).one()
    assert checkpoint.stage == "s4" and checkpoint.status == "done"


def test_completed_company_never_requeued(enrich_engine, session, company):
    enrich_engine.run_company(company)
    session.commit()
    assert enrich_engine.pending_companies() == []
    enrich_engine.run_company(company)         # no-op, never re-runs deeper stages
    checkpoint = session.query(EnrichCheckpoint).filter_by(company_id=company.id).one()
    assert checkpoint.stage == "s4"


def test_prescore_threshold_stops_at_s1(session):
    weak = Company(canonical_name="Totally Unrelated Shop", domain=None, country=None,
                   tier=3)
    session.add(weak)
    session.commit()
    engine = EnrichEngine(session, llm=None, search=None, config={
        "stages": {"prescore_threshold": 9.5},
        "crawl": {"discover_paths": [""], "max_pages": 2},
    })
    engine.run_company(weak)
    session.commit()
    checkpoint = session.query(EnrichCheckpoint).filter_by(company_id=weak.id).one()
    assert checkpoint.stage == "s1"     # stopped here, no deeper stage ran
    assert checkpoint.status == "done"
    assert checkpoint.detail["s2"] == "skipped_below_threshold"
    assert engine.stats.skipped_prescore == 1
    assert engine.pending_companies() == []  # not re-queued


def test_coverage_meter(enrich_engine, session, company):
    enrich_engine.run_company(company)
    session.commit()
    unenriched = Company(canonical_name="Empty Co", tier=2)
    session.add(unenriched)
    session.commit()
    rows = {r["tier"]: r for r in coverage(session)}
    assert rows[2]["total"] == 2
    assert rows[2]["enriched"] == 1
    assert rows[2]["coverage"] == 0.5


def test_queue_covers_every_company_never_samples(enrich_engine, session):
    """Pending queue returns ALL unfinished companies (no silent sampling)."""
    for i in range(5):
        session.add(Company(canonical_name=f"Co {i}", tier=3))
    session.commit()
    pending = enrich_engine.pending_companies()
    assert len(pending) == 5
    enrich_engine.run_company(pending[0])
    session.commit()
    assert len(enrich_engine.pending_companies()) == 4


def test_weak_company_counts_unenriched_in_coverage(session):
    weak = Company(canonical_name="Weak Co", tier=3)
    session.add(weak)
    session.commit()
    engine = EnrichEngine(session, llm=None, search=None, config={
        "stages": {"prescore_threshold": 9.5}})
    engine.run_company(weak)
    session.commit()
    rows = {r["tier"]: r for r in coverage(session)}
    assert rows[3]["enriched"] == 0     # stopped at S1: not "enriched"
    assert rows[3]["total"] == 1

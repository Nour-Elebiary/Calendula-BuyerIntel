"""Engine stage wiring for M6: S5/S6/S7 in the resumable queue.

Covers: tier gating of S5 (tier 2+ only, deep for tier 1), the S6
placeholder, S7 via the gate, and resumability - an s4-done company from
the M4 era becomes pending again and runs through to s7.
"""
from __future__ import annotations

import pytest

from buyerintel.db.models import Company, EnrichCheckpoint
from buyerintel.enrich.engine import STAGES, EnrichEngine
from buyerintel.score.personas import sync_personas


@pytest.fixture(autouse=True)
def personas(session):
    sync_personas(session)
    session.commit()


def test_stage_order_includes_m6_stages():
    assert STAGES == ["s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7"]


def test_s5_gated_by_tier(session):
    unscored = Company(canonical_name="No Tier Yet")
    t3 = Company(canonical_name="Tier Three Co", tier=3)
    t2 = Company(canonical_name="Tier Two Co", tier=2)
    session.add_all([unscored, t3, t2])
    session.commit()
    engine = EnrichEngine(session, config={"people": {}})

    assert engine._stage_s5(unscored)["skipped"] == "awaiting tier (score-run first)"
    assert "tier 2 and up" in engine._stage_s5(t3)["skipped"]
    detail = engine._stage_s5(t2)
    assert "people_stats" in detail and detail["deep"] is False
    t2.tier = 1
    assert engine._stage_s5(t2)["deep"] is True


def test_s6_placeholder(session):
    company = Company(canonical_name="Tier One Co", tier=1)
    session.add(company)
    session.commit()
    detail = EnrichEngine(session, config={})._stage_s6(company)
    assert "M7" in detail["deferred"]


def test_s7_uses_the_paid_gate(session):
    t3 = Company(canonical_name="Tier Three Co", tier=3, score=90.0)
    session.add(t3)
    session.commit()
    engine = EnrichEngine(session, config={})
    detail = engine._stage_s7(t3)
    assert detail["fired"] is False
    assert detail["reason"] == "not_tier_1"


def test_s4_done_company_resumes_through_m6_stages(session, monkeypatch):
    """A company with an M4-era checkpoint (stage=s4, status=done) is PENDING
    again once S5-S7 exist, and completes all the way to s7."""
    from buyerintel.enrich.people import discovery as discovery_mod

    company = Company(canonical_name="M4 Era Co", tier=2, domain="m4era.example")
    session.add(company)
    session.flush()
    session.add(EnrichCheckpoint(company_id=company.id, stage="s4", status="done",
                                 detail={"pages": 3}))
    session.commit()

    # keep S5's search-free path; nothing to enumerate
    monkeypatch.setattr(discovery_mod, "load_yaml", lambda *a, **k: {})
    engine = EnrichEngine(session, config={"people": {}})
    pending = engine.pending_companies()
    assert [c.id for c in pending] == [company.id]

    stats = engine.run()
    assert stats.errors == []
    checkpoint = session.query(EnrichCheckpoint).filter_by(company_id=company.id).one()
    assert checkpoint.stage == "s7"
    assert checkpoint.status == "done"
    assert engine.pending_companies() == []  # now fully complete

    # summary includes the new counters
    assert "s5=" in stats.summary() and "s7=" in stats.summary()

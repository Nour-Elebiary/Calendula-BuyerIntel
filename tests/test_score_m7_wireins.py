"""M7 wire-ins for the M5 scoring components (DECISIONS.md rescale).

scale_buying_frequency (25) = order_size 8 + size_band 4 + expo 5
    + shipments 8 (frequency 0-5 + volume-vs-MOQ 0-3)
timing_signals (10) = expo 3 + archive 2 + multi 2 + shipment recency 3
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from buyerintel.db.models import Company, Shipment
from buyerintel.score.scoring import component_scale_frequency, component_timing

NOW = datetime.now(timezone.utc)
CFG = {"forecast": {"confidence": {"low_below": 3, "high_from": 5}},
       "scoring": {"shipment_recency_days": 180}}


def _shipments(session, company, days_agos, qty=1000.0):
    for d in days_agos:
        session.add(Shipment(source="test", company_id=company.id,
                             date=NOW - timedelta(days=d), hs_code="0903",
                             qty=qty, value_usd=qty * 3))
    session.flush()


def test_shipment_frequency_points(session):
    company = Company(canonical_name="Freq Co")
    session.add(company)
    session.flush()

    # 0 shipments: freq 0
    pts, detail = component_scale_frequency(session, company,
                                            {"_definition": {"typical_order_kg": 1000}}, 25)
    assert detail["shipments"] == 0 and detail["shipment_freq_points"] == 0

    # 1 shipment: 2 points
    _shipments(session, company, [100])
    pts, detail = component_scale_frequency(session, company,
                                            {"_definition": {"typical_order_kg": 1000}}, 25)
    assert detail["shipment_freq_points"] == 2.0
    # 2-3 shipments: 4 points
    _shipments(session, company, [200, 300])
    _, detail = component_scale_frequency(session, company,
                                          {"_definition": {"typical_order_kg": 1000}}, 25)
    assert detail["shipment_freq_points"] == 4.0
    # 4+ shipments: 5 points
    _shipments(session, company, [400, 500])
    _, detail = component_scale_frequency(session, company,
                                          {"_definition": {"typical_order_kg": 1000}}, 25)
    assert detail["shipment_freq_points"] == 5.0


def test_shipment_volume_against_persona_moq(session):
    company = Company(canonical_name="Volume Co")
    session.add(company)
    session.flush()
    persona = {"_definition": {"typical_order_kg": 1000}}

    # median 1000 kg >= MOQ -> 3 volume points (plus 5 freq with 4 shipments)
    _shipments(session, company, [100, 200, 300, 400], qty=1000)
    _, detail = component_scale_frequency(session, company, persona, 25)
    assert detail["shipment_volume_points"] == 3.0
    assert detail["shipment_points"] == 8.0          # 5 + 3, at the sub-cap

    # median 600 < 1000 but >= 500 -> 2 points
    session.query(Shipment).delete()
    session.flush()
    _shipments(session, company, [100, 200, 300, 400], qty=600)
    _, detail = component_scale_frequency(session, company, persona, 25)
    assert detail["shipment_volume_points"] == 2.0

    # no persona MOQ but real volume -> 1 point
    _, detail = component_scale_frequency(session, company, {"_definition": {}}, 25)
    assert detail["shipment_volume_points"] == 1.0


def test_component_subcaps_hold(session):
    """Order-size 8 / size-band 4 / expo 5 / shipments 8 sum to <= 25."""
    company = Company(canonical_name="Capped Co", size_band="large")
    session.add(company)
    session.flush()
    _shipments(session, company, [100, 200, 300, 400, 500], qty=5000)
    pts, detail = component_scale_frequency(session, company,
                                            {"_definition": {"typical_order_kg": 5000}}, 25)
    assert pts <= 25.0
    assert detail["order_size_points"] == 8.0
    assert detail["size_band_points"] == 4.0
    assert detail["expo_points"] == 0.0
    assert detail["shipment_points"] == 8.0
    assert pts == pytest.approx(20.0)


def test_stronger_buying_record_scores_higher(session):
    """The ordering the wire-in exists for: shipments lift the score."""
    sleepy = Company(canonical_name="Sleepy Co", size_band="medium")
    buying = Company(canonical_name="Buying Co", size_band="medium")
    session.add_all([sleepy, buying])
    session.flush()
    persona = {"_definition": {"typical_order_kg": 1000}}
    _shipments(session, buying, [60, 160, 260], qty=1500)

    sleepy_pts, _ = component_scale_frequency(session, sleepy, persona, 25)
    buying_pts, _ = component_scale_frequency(session, buying, persona, 25)
    assert buying_pts > sleepy_pts


def test_timing_shipment_recency(session, monkeypatch):
    recency_days = float(CFG["scoring"]["shipment_recency_days"])
    company = Company(canonical_name="Timing Co")
    session.add(company)
    session.flush()

    def timing():
        return component_timing(session, company, 10)

    # no shipments: 0 recency points
    _, detail = timing()
    assert detail["shipment_recency_points"] == 0.0

    # recent shipment (60d <= 180) -> 3
    _shipments(session, company, [60])
    _, detail = timing()
    assert detail["shipment_recency_points"] == 3.0
    assert detail["latest_shipment_days_ago"] is not None

    # stale shipment (400d > 2x180) -> 0
    session.query(Shipment).update({"date": NOW - timedelta(days=400)})
    session.flush()
    _, detail = timing()
    assert detail["shipment_recency_points"] == 0.0

    # middle band (200d <= 360) -> 2
    session.query(Shipment).update({"date": NOW - timedelta(days=200)})
    session.flush()
    _, detail = timing()
    assert detail["shipment_recency_points"] == 2.0


def test_timing_component_subcap_total(session):
    company = Company(canonical_name="Full Timing Co")
    session.add(company)
    session.flush()
    _shipments(session, company, [30])
    pts, detail = component_timing(session, company, 10)
    assert pts <= 10.0
    assert detail["shipment_recency_points"] == 3.0

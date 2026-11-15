"""Paid-tool gate S7 tests (spec 12.9, M6).

Acceptance:
- the gate NEVER fires for non-Tier-1
- the credit budget is enforced across runs
- eligible requires enabled + min_score + still lacking a key contact
"""
from __future__ import annotations

import pytest

from buyerintel.db.models import Company, ContactPoint, PaidToolCall, Person
from buyerintel.enrich.paid import PaidToolGate, credits_used_this_month


def base_cfg(**overrides) -> dict:
    cfg = {"paid_gate": {"enabled": True, "min_score": 60.0,
                         "monthly_credit_budget": 3.0,
                         "tools": ["apollo", "hunter"],
                         "credit_cost": {"apollo": 1.0, "hunter": 1.0}}}
    cfg["paid_gate"].update(overrides)
    return cfg


def tier1_company(session, *, score: float = 80.0, with_contact: bool = False) -> Company:
    company = Company(canonical_name="Tier One Importers", domain="t1.example",
                      tier=1, score=score)
    session.add(company)
    session.flush()
    person = Person(company_id=company.id, full_name="Boss Person",
                    title="Purchasing Manager", is_decision_maker=True)
    session.add(person)
    session.flush()
    if with_contact:
        session.add(ContactPoint(owner_type="person", owner_id=person.id, kind="email",
                                 value="boss@t1.example", verified_status="published",
                                 confidence=0.9))
    session.commit()
    return company


def test_gate_never_fires_for_non_tier1(session):
    """M6 acceptance: the gate never fires for non-Tier-1 - checked FIRST,
    even when every other condition would allow it."""
    for tier, score in ((2, 95.0), (3, 90.0)):
        company = Company(canonical_name=f"Tier {tier} Co", tier=tier, score=score)
        session.add(company)
        session.commit()
        gate = PaidToolGate(session, config=base_cfg())
        eligible, reason = gate.eligibility(company)
        assert eligible is False and reason == "not_tier_1"
        detail = gate.run(company)
        assert detail["fired"] is False
    assert session.query(PaidToolCall).count() == 0


def test_gate_requires_enabled_and_min_score(session):
    company = tier1_company(session, score=59.0)
    # disabled by config
    cfg = base_cfg()
    cfg["paid_gate"]["enabled"] = False
    gate = PaidToolGate(session, config=cfg)
    assert gate.run(company) == {"fired": False, "reason": "disabled_by_config"}
    # below min_score
    gate = PaidToolGate(session, config=base_cfg())
    eligible, reason = gate.eligibility(company)
    assert eligible is False and "score_below" in reason


def test_gate_skips_when_key_contact_exists(session):
    company = tier1_company(session, with_contact=True)
    gate = PaidToolGate(session, config=base_cfg())
    eligible, reason = gate.eligibility(company)
    assert eligible is False and reason == "has_key_contact"


def test_gate_without_keys_spends_nothing(session):
    """No API keys configured: adapters return no_key, zero credits spent."""
    from buyerintel.config import load_secrets

    company = tier1_company(session)
    gate = PaidToolGate(session, config=base_cfg())
    detail = gate.run(company)
    assert detail["fired"] is True
    assert detail["tools"]["apollo"]["outcome"] == "no_key"
    assert detail["budget_left"] == 3.0
    calls = session.query(PaidToolCall).all()
    assert all(c.credits == 0.0 for c in calls)


def test_credit_budget_enforced(session, monkeypatch):
    """M6 acceptance: budget enforced across runs - a 3-credit budget with
    1-credit successful calls allows exactly 3, the 4th is skipped."""
    company = tier1_company(session)

    def fake_call(self, tool, company_obj):
        return "ok", {"people": 1}

    monkeypatch.setattr(PaidToolGate, "_call", fake_call)
    gate = PaidToolGate(session, config=base_cfg())
    first = gate.run(company)
    assert first["fired"] is True
    assert first["budget_left"] == 2.0  # apollo succeeded (1 credit), run ended

    # exhaust the budget with a company that never gets a contact
    def fake_call_empty(self, tool, company_obj):
        return "ok", {"people": 0}

    monkeypatch.setattr(PaidToolGate, "_call", fake_call_empty)
    for _ in range(2):
        gate.run(company)
    assert credits_used_this_month(session) == 3.0
    third = gate.run(company)
    assert third["tools"]["apollo"]["outcome"] == "skipped_budget"
    assert third["tools"]["hunter"]["outcome"] == "skipped_budget"
    assert third["budget_left"] == 0.0


def test_successful_tool_ends_the_run(session, monkeypatch):
    """One tool finding a usable contact ends the run (no double spend)."""
    company = tier1_company(session)
    calls = []

    def fake_call(self, tool, company_obj):
        calls.append(tool)
        return "ok", {"people": 2} if tool == "apollo" else {"emails": 0}

    monkeypatch.setattr(PaidToolGate, "_call", fake_call)
    detail = PaidToolGate(session, config=base_cfg()).run(company)
    assert calls == ["apollo"]  # hunter never called
    assert detail["budget_left"] == 2.0


def test_apollo_adapter_stores_people(session, monkeypatch):
    """The Apollo adapter maps API people into Person rows + contact points."""
    company = tier1_company(session)

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"people": [{"first_name": "Klaus", "last_name": "Mueller",
                                "title": "Head of Purchasing",
                                "email": "k.mueller@t1.example"}]}

    import buyerintel.enrich.paid as paid_mod

    monkeypatch.setattr(paid_mod.httpx, "post",
                        lambda *a, **k: FakeResponse())
    gate = PaidToolGate(session, config=base_cfg())
    outcome, detail = gate._apollo(company, "fake-key")
    assert outcome == "ok"
    person = session.query(Person).filter_by(company_id=company.id,
                                             full_name="Klaus Mueller").one()
    email = session.query(ContactPoint).filter_by(owner_type="person",
                                                  owner_id=person.id).one()
    assert email.value == "k.mueller@t1.example"

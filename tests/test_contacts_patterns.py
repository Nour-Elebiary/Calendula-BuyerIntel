"""Email pattern engine tests (ADD-004, spec 12.4, M6)."""
from __future__ import annotations

import pytest

from buyerintel.db.models import Company, ContactPoint, Person
from buyerintel.enrich.contacts.patterns import (
    guess_missing_emails,
    guess_person_email,
    learn_pattern,
    name_parts,
    store_pattern_guess,
)

# DNS is not touched in unit tests: monkeypatch MX to a known state.
MX_ON = {"rhein.example": True, "nomx.example": False}


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    from buyerintel.enrich.contacts import patterns as patterns_mod

    monkeypatch.setattr(patterns_mod, "has_mx",
                        lambda domain: MX_ON.get(domain, True))


def test_name_parts_handles_variants():
    assert name_parts("Klaus Mueller") == ("klaus", "mueller")
    assert name_parts("Klaus Dieter Mueller") == ("klaus", "mueller")
    # single name: usable only for the 'first' pattern
    first, last = name_parts("Cher")
    assert first == "cher" and last is None
    # non-Latin script: no invented transliterations
    assert name_parts("محمد علي") == (None, None)


def test_learn_pattern_structure():
    assert learn_pattern(["jane.doe@x.com", "john.smith@x.com"]) == "dotted"
    assert learn_pattern(["janedoe@x.com", "johnsmith@x.com"]) == "plain"
    assert learn_pattern(["jane_doe@x.com"]) == "underscored"
    assert learn_pattern([]) is None


def test_guess_ranking_follows_learned_structure(monkeypatch):
    guesses = guess_person_email("Jane Doe", "rhein.example",
                                 published_emails=["john.smith@rhein.example"])
    assert guesses[0].pattern == "first.last"
    assert guesses[0].email == "jane.doe@rhein.example"
    assert guesses[0].confidence >= 0.55  # learned + MX


def test_guess_never_emits_for_unusable_names():
    assert guess_person_email("محمد علي", "rhein.example") == []
    assert guess_person_email("Jane Doe", "") == []


def test_store_guess_respects_real_addresses(session):
    company = Company(canonical_name="Rhein GmbH", domain="rhein.example")
    session.add(company)
    session.flush()
    person = Person(company_id=company.id, full_name="Jane Doe")
    session.add(person)
    session.flush()  # id must exist before contact rows reference it
    # published email already present: a guess must NOT be stored
    session.add(ContactPoint(owner_type="person", owner_id=person.id, kind="email",
                             value="jane@rhein.example", verified_status="published",
                             confidence=0.9))
    session.commit()
    guesses = guess_person_email("Jane Doe", "rhein.example")
    assert store_pattern_guess(session, company, person, guesses) is None
    assert session.query(ContactPoint).filter_by(owner_id=person.id).count() == 1


def test_guess_missing_emails_stores_pattern_guessed(session):
    company = Company(canonical_name="Rhein GmbH", domain="rhein.example")
    session.add(company)
    session.flush()
    # published domain email teaches the dotted pattern
    session.add(ContactPoint(owner_type="company", owner_id=company.id, kind="email",
                             value="info@rhein.example", verified_status="published",
                             confidence=0.9))
    session.add(Person(company_id=company.id, full_name="Jane Doe",
                       title="Purchasing Manager"))
    session.commit()

    stored = guess_missing_emails(session, company)
    assert stored == 1
    contact = session.query(ContactPoint).filter_by(owner_type="person").one()
    assert contact.verified_status == "pattern-guessed"
    assert contact.value == "jane.doe@rhein.example"
    # idempotent: re-run adds nothing
    assert guess_missing_emails(session, company) == 0

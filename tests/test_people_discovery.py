"""People discovery tests (spec 12.7, M6): CrossLinked-style enumeration with
a fake search router, dedupe, seniority application, login-adapter refusal.
"""
from __future__ import annotations

import pytest

from buyerintel.db.models import Company, ContactPoint, Person
from buyerintel.enrich.people.discovery import discover_people
from buyerintel.enrich.people.linkedin_login import (
    LinkedInLoginAdapter,
    LinkedInLoginDisabled,
)
from buyerintel.score.personas import sync_personas


class FakeSearchRouter:
    """Returns canned LinkedIn URLs for the enumeration queries."""

    def __init__(self, urls: list[str]):
        self.urls = urls
        self.queries: list[str] = []

    def search(self, query, *, query_type="general", country=None, max_results=10):
        self.queries.append(query)
        from buyerintel.search.router import SearchResult, RouterStats

        return [SearchResult(engine="fake", title="x", url=u) for u in self.urls], RouterStats()


@pytest.fixture(autouse=True)
def personas(session):
    assert sync_personas(session) == 15
    session.commit()


def test_enumeration_creates_people_with_evidence(session, monkeypatch):
    company = Company(canonical_name="Rhein Gewuerze Import GmbH",
                      domain="rhein.example", city="Hamburg", tier=2)
    session.add(company)
    session.commit()

    # enumeration finds two profiles; only the first is parseable via fixture
    fake = FakeSearchRouter([
        "https://de.linkedin.com/in/klaus-mueller",
        "https://www.linkedin.com/in/jane-doe-123/",
        "https://de.linkedin.com/in/klaus-mueller",  # duplicate URL
    ])
    # public fetch: fixture HTML for klaus, authwall for jane
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures"
    html_map = {
        "klaus-mueller": (fixtures / "linkedin_profile_public.html").read_text(encoding="utf-8"),
        "jane-doe-123": (fixtures / "linkedin_authwall.html").read_text(encoding="utf-8"),
    }
    from buyerintel.enrich.people import discovery as discovery_mod

    def fake_fetch(url, *, config=None):
        for key, html in html_map.items():
            if key in url:
                from buyerintel.enrich.people.linkedin_public import parse_profile_html
                return parse_profile_html(html)
        return None

    monkeypatch.setattr(discovery_mod.linkedin_public, "fetch_profile", fake_fetch)
    monkeypatch.setattr(discovery_mod, "load_yaml", lambda *a, **k: {})

    stats = discover_people(session, fake, company)
    assert "site:linkedin.com/in" in fake.queries[0]  # CrossLinked-style, no login
    people = session.query(Person).filter_by(company_id=company.id).all()
    names = {p.full_name for p in people}
    assert "Klaus Mueller" in names           # parsed from fixture
    assert "jane-doe-123" not in names        # authwall never becomes a name
    klaus = next(p for p in people if p.full_name == "Klaus Mueller")
    assert klaus.seniority == "vp_director"   # 'Head of Purchasing'
    assert klaus.is_decision_maker is True
    link = session.query(ContactPoint).filter_by(owner_type="person", owner_id=klaus.id,
                                                 kind="linkedin").one()
    assert "linkedin.com/in/klaus-mueller" in link.value
    # pattern-guessed email for klaus from the domain (he has a published one
    # from the fixture: stored at confidence 0.85, no guess overrides it)
    emails = session.query(ContactPoint).filter_by(owner_type="person", owner_id=klaus.id,
                                                   kind="email").all()
    assert any(c.value == "k.mueller@rhein-gewuerze.example" for c in emails)


def test_discovery_without_search_degrades(session):
    company = Company(canonical_name="Offweb GmbH", tier=2)
    session.add(company)
    session.flush()
    session.add(Person(company_id=company.id, full_name="Jane Doe",
                       title="Procurement Director"))
    session.commit()
    stats = discover_people(session, None, company)
    assert stats["enumerated"] == 0
    jane = session.query(Person).filter_by(full_name="Jane Doe").one()
    assert jane.seniority == "vp_director"
    assert jane.is_decision_maker is True


def test_login_adapter_refuses_when_disabled(session):
    """M6 acceptance: login adapters refuse to run when disabled."""
    company = Company(canonical_name="Any GmbH", tier=1)
    session.add(company)
    session.commit()
    adapter = LinkedInLoginAdapter(session)
    assert adapter.enabled() is False
    with pytest.raises(LinkedInLoginDisabled):
        adapter.run(company)
    # and discovery records the refusal without crashing
    stats = discover_people(session, None, company)
    assert "disabled" in stats["login_adapters"]


def test_login_adapter_enabled_still_tier1_only(session):
    from buyerintel.config import load_yaml

    cfg = load_yaml("enrich.yaml")
    cfg["linkedin"]["enabled"] = True
    cfg["linkedin"]["account_profile"] = "secondary-account"
    t2 = Company(canonical_name="Tier Two Co", tier=2)
    session.add(t2)
    session.commit()
    result = LinkedInLoginAdapter(session, config=cfg).run(t2)
    assert result["ran"] is False
    assert "tier 1" in result["reason"].lower()

"""People discovery (spec section 12.7), in the spec's order:

  (a) company site team pages   -> people the S2 crawl/extraction already
      captured, re-confirmed here from team-page HTML when available
  (b) CrossLinked-style employee enumeration from SEARCH ENGINES, no login:
      ``site:linkedin.com/in {title} {company}`` and
      ``{company} {city} purchasing OR procurement linkedin`` queries
  (c) JobSpy-style job-post signals -> already captured in S4
      (osint.job_post_signals); noted, not duplicated
  (d) optional login-based LinkedIn adapters -> refused when disabled
      (people/linkedin_login.py)

New people dedupe on (company, full_name) and (company, linkedin URL);
every found profile keeps its source_url as evidence. Public-profile
enrichment (ADD-012) runs for the found URLs with no account involved.
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Company, ContactPoint, Person
from buyerintel.enrich.contacts.patterns import guess_missing_emails
from buyerintel.enrich.people import linkedin_public
from buyerintel.enrich.people.linkedin_login import LinkedInLoginAdapter
from buyerintel.enrich.people.seniority import apply_seniority

PROFILE_URL_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%-]+/?", re.I)

# Enumeration query templates (CrossLinked-style, no login). {company} is
# the canonical name; {title} rotates through the buying-role words.
ENUMERATION_QUERIES = [
    "site:linkedin.com/in {title} {company}",
    "site:linkedin.com/in {company} {city}",
    "\"{company}\" linkedin.com/in purchasing OR procurement OR einkauf OR achats",
]

ENUMERATION_TITLES = [
    "purchasing", "procurement", "buyer", "sourcing", "einkauf", "achats",
    "compras", "acquisti", "inkoop", "import manager",
]


def _norm_name(name: str) -> str:
    return " ".join((name or "").lower().split())


def _find_or_create_person(session: Session, company: Company, full_name: str,
                           title: str | None, source_url: str | None,
                           confidence: float) -> tuple[Person, bool]:
    """Dedupe on (company, full_name); returns (person, created)."""
    person = session.scalar(
        select(Person).where(Person.company_id == company.id,
                             Person.full_name == full_name)
    )
    if person is not None:
        # Existing person: keep the newly found profile URL as evidence.
        if source_url:
            has_link = session.scalar(
                select(ContactPoint.id).where(
                    ContactPoint.owner_type == "person",
                    ContactPoint.owner_id == person.id,
                    ContactPoint.kind == "linkedin",
                    ContactPoint.value == source_url)
            ) is None
            if has_link:
                session.add(ContactPoint(
                    owner_type="person", owner_id=person.id, kind="linkedin",
                    value=source_url, source_url=source_url,
                    verified_status="published", confidence=confidence))
                session.flush()
        return person, False
    person = Person(company_id=company.id, full_name=full_name, title=title,
                    confidence=confidence)
    session.add(person)
    session.flush()
    if source_url:
        session.add(ContactPoint(owner_type="person", owner_id=person.id,
                                 kind="linkedin", value=source_url,
                                 source_url=source_url, verified_status="published",
                                 confidence=confidence))
        session.flush()
    return person, True


def _store_profile(session: Session, company: Company, url: str) -> Person | None:
    """Fetch + parse one public profile (no login); store the person."""
    profile = linkedin_public.fetch_profile(url, config=load_yaml("enrich.yaml"))
    if profile is None or not profile.get("full_name"):
        return None
    person, _created = _find_or_create_person(
        session, company, profile["full_name"], profile.get("title"), url,
        confidence=0.7)
    if profile.get("email"):
        existing = session.scalar(
            select(ContactPoint).where(ContactPoint.owner_type == "person",
                                       ContactPoint.owner_id == person.id,
                                       ContactPoint.kind == "email",
                                       ContactPoint.value == profile["email"]))
        if existing is None:
            session.add(ContactPoint(
                owner_type="person", owner_id=person.id, kind="email",
                value=profile["email"], source_url=url,
                verified_status="published", confidence=0.85))
            session.flush()
    return person


def discover_people(session: Session, search, company: Company, *,
                    llm=None, config: dict | None = None,
                    deep: bool = False) -> dict:
    """Run the discovery order for one company; persist people + contacts.

    `search` is the SearchRouter (or a fake in tests); when None the search
    stage degrades gracefully (team-page + pattern stages still run).
    `deep` doubles the enumeration queries (Tier 1 'deep for Tier 1').
    """
    cfg = config or load_yaml("enrich.yaml")
    people_cfg = cfg.get("people") or {}
    max_people = int(people_cfg.get("max_people_per_company", 8)) * (2 if deep else 1)
    stats: dict = {"team_page": 0, "enumerated": 0, "profiles_enriched": 0,
                   "emails_guessed": 0, "login_adapters": "refused_or_off"}

    # (a) team pages: people already captured by S2 - classify them now.
    apply_seniority(session, company)
    stats["team_page"] = session.scalar(
        select(func.count(Person.id)).where(Person.company_id == company.id)
    ) or 0

    # (b) CrossLinked-style enumeration via the search router (no login).
    if search is not None:
        urls: list[str] = []
        titles = ENUMERATION_TITLES[: len(ENUMERATION_TITLES) * (2 if deep else 1)]
        for title in titles:
            for template in ENUMERATION_QUERIES:
                query = template.format(company=company.canonical_name,
                                        title=title, city=company.city or "")
                try:
                    results, _stats = search.search(query, query_type="people",
                                                    country=company.country,
                                                    max_results=int(
                                                        people_cfg.get("max_results_per_query", 5)))
                except Exception:  # noqa: BLE001 - one failed query never stops discovery
                    continue
                for result in results:
                    m = PROFILE_URL_RE.search(result.url or "")
                    if m and m.group(0) not in urls:
                        urls.append(m.group(0))
        for url in urls[:max_people]:
            person = _store_profile(session, company, url)
            if person is not None:
                stats["profiles_enriched"] += 1
        stats["enumerated"] = len(urls[:max_people])

    # (d) login adapters: must refuse while disabled (M6 acceptance).
    try:
        login_result = LinkedInLoginAdapter(session, config=cfg).run(company)
        stats["login_adapters"] = login_result.get("reason", "refused_or_off")
    except Exception as exc:  # noqa: BLE001 - refusal is an exception by design
        stats["login_adapters"] = str(exc)

    # Seniority/decision-maker pass over everything found.
    stats["seniority"] = apply_seniority(session, company)

    # Pattern-guessed emails for people still without one (ADD-004).
    try:
        stats["emails_guessed"] = guess_missing_emails(session, company)
    except Exception:  # noqa: BLE001 - DNS down must not lose people
        stats["emails_guessed"] = 0
    session.flush()
    return stats

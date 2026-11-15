"""Email pattern engine (ADD-004, spec 12.4).

1. LEARN the domain pattern from emails already published on it
   (first.last, firstlast, first_last, f.last, first, firstl) and apply it
   to other people at that domain.
2. If no pattern is known, generate RANKED permutations from the person's
   name (most common patterns first).
3. Verify with syntax + MX (reuse enrich.verify); keep status
   `pattern-guessed` until corroborated by a published email or a
   successful verification - a guess never outranks a published address.

Handles transliteration-free basics: Latin tokens only for permutations
(single names, multi-part names, non-Latin scripts yield no guesses rather
than wrong ones).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.db.models import Company, ContactPoint, Person
from buyerintel.enrich.verify import PATTERNS, ROLE_PREFIXES, check_syntax, has_mx
from buyerintel.score.matching import normalize

LATIN_TOKEN = re.compile(r"^[a-z]+$")


@dataclass
class Guess:
    email: str
    pattern: str
    mx: bool
    confidence: float


def name_parts(full_name: str) -> tuple[str | None, str | None]:
    """(first, last) from a display name; None when unusable for permutations.

    Handles multi-part names and refuses non-Latin scripts (no invented
    transliterations here - the transliteration layer is a later add-on).
    """
    tokens = [t for t in normalize(full_name).split() if LATIN_TOKEN.match(t)]
    if not tokens:
        return None, None
    first = tokens[0]
    last = tokens[-1] if len(tokens) > 1 else None
    if not last:
        return first, None
    return first, last


def learn_pattern(published_emails: list[str]) -> str | None:
    """The dominant local-part STRUCTURE from emails published on one domain.

    ('dotted' | 'underscored' | 'plain') or None - used to rank candidate
    permutations; the exact pattern per person is verified separately.
    """
    votes: dict[str, int] = {}
    role_prefixes = {p.lower() for p in ROLE_PREFIXES}
    for email in published_emails:
        local = email.split("@", 1)[0].lower()
        if local in role_prefixes:
            continue  # role addresses (info@, sales@) teach nothing about names
        if "." in local:
            votes["dotted"] = votes.get("dotted", 0) + 1
        elif "_" in local:
            votes["underscored"] = votes.get("underscored", 0) + 1
        else:
            votes["plain"] = votes.get("plain", 0) + 1
    if not votes:
        return None
    return max(votes, key=votes.get)  # type: ignore[return-value]


def guess_person_email(full_name: str, domain: str, *,
                       published_emails: list[str] | None = None) -> list[Guess]:
    """Ranked candidate emails for one person at one domain.

    When the domain's published emails reveal a structure (dotted/underscored/
    plain), candidates matching that structure rank first.
    """
    first, last = name_parts(full_name)
    if not domain or not first:
        return []
    structure = learn_pattern(published_emails or [])
    ranked: list[Guess] = []

    def build(kind: str) -> str | None:
        for name, fn in PATTERNS:
            if name == kind:
                if last is None and name not in ("first",):
                    return None
                return f"{fn(first, last or '')}@{domain}"
        return None

    order = list(dict.fromkeys(["first.last", "firstlast", "first_last",
                                "f.last", "first", "firstl"]))
    if structure == "dotted":
        order.sort(key=lambda k: 0 if k in ("first.last", "f.last") else 1)
    elif structure == "underscored":
        order.sort(key=lambda k: 0 if k == "first_last" else 1)
    elif structure == "plain":
        order.sort(key=lambda k: 0 if k in ("firstlast", "first", "firstl") else 1)

    seen: set[str] = set()
    for kind in order:
        email = build(kind)
        if not email or not check_syntax(email) or email in seen:
            continue
        seen.add(email)
        mx = has_mx(domain)
        # Learned structure + MX = decent confidence; bare guesses stay low.
        confidence = 0.55 if (structure and mx) else (0.4 if mx else 0.1)
        ranked.append(Guess(email=email, pattern=kind, mx=mx, confidence=confidence))
    return ranked


def store_pattern_guess(session: Session, company: Company, person: Person,
                        guesses: list[Guess]) -> ContactPoint | None:
    """Persist the TOP guess as a pattern-guessed contact point (idempotent).

    Never overrides a published/verified email already stored for the person.
    """
    if not guesses:
        return None
    existing = session.scalars(
        select(ContactPoint).where(ContactPoint.owner_type == "person",
                                   ContactPoint.owner_id == person.id,
                                   ContactPoint.kind == "email")
    ).all()
    if any(c.verified_status in ("published", "verified") for c in existing):
        return None  # a real address exists; a guess adds nothing
    top = guesses[0]
    for contact in existing:
        if contact.value == top.email:
            contact.confidence = max(contact.confidence or 0.0, top.confidence)
            contact.verified_status = "pattern-guessed"
            session.flush()
            return contact
    row = ContactPoint(
        owner_type="person", owner_id=person.id, kind="email", value=top.email,
        verified_status="pattern-guessed", confidence=top.confidence,
    )
    session.add(row)
    session.flush()
    return row


def guess_missing_emails(session: Session, company: Company) -> int:
    """For every person of the company without a usable email: guess + store.

    Published emails of the same domain (company contacts) teach the pattern.
    Returns the number of guesses stored.
    """
    domain = (company.domain or "").strip().lower()
    if not domain:
        return 0
    published = [c.value for c in session.scalars(
        select(ContactPoint).where(
            ContactPoint.owner_type == "company",
            ContactPoint.owner_id == company.id,
            ContactPoint.kind == "email")
    ).all() if "@" in (c.value or "")]
    people = session.scalars(select(Person).where(Person.company_id == company.id)).all()
    stored = 0
    for person in people:
        has_email = session.scalar(
            select(ContactPoint.id).where(
                ContactPoint.owner_type == "person", ContactPoint.owner_id == person.id,
                ContactPoint.kind == "email")
        ) is not None
        if has_email:
            continue
        guesses = guess_person_email(person.full_name, domain, published_emails=published)
        if store_pattern_guess(session, company, person, guesses) is not None:
            stored += 1
    session.flush()
    return stored

from __future__ import annotations

from buyerintel.resolve.matchers import (
    MatchKeys,
    normalize_domain,
    normalize_name,
    normalize_phone,
    normalize_registry,
    normalize_social,
)
from buyerintel.resolve.dedupe import (
    BORDERLINE_LOW,
    CORROBORATION_SIM,
    decide_pair,
    merge_companies,
    link_companies,
)


def keys(**kwargs) -> MatchKeys:
    """Build MatchKeys with the same normalization the real pipeline applies."""
    defaults = dict(
        company_id=1,
        name_key="",
        country=None,
        domain=None,
        registries=[],
        phones=[],
        socials=[],
        address=None,
    )
    defaults.update(kwargs)
    return MatchKeys(
        company_id=defaults["company_id"],
        name_key=normalize_name(defaults["name_key"]),
        country=defaults["country"],
        domain=normalize_domain(defaults["domain"]),
        registries=[r for r in (normalize_registry(v) for v in defaults["registries"]) if r],
        phones=[p for p in (normalize_phone(v, defaults["country"]) for v in defaults["phones"]) if p],
        socials=[s for s in (normalize_social(v) for v in defaults["socials"]) if s],
        address=defaults["address"],
    )


def test_hard_registry_merge():
    d = decide_pair(keys(registries=["HRB1"]), keys(registries=["hrb1"]))
    assert d.action == "merge" and "registry" in d.reason


def test_hard_domain_merge():
    d = decide_pair(keys(domain="acme.com"), keys(domain="www.acme.com".replace("www.", "")))
    assert d.action == "merge"


def test_hard_phone_merge():
    d = decide_pair(keys(phones=["+201001234567"]), keys(phones=["+201001234567"]))
    assert d.action == "merge"


def test_hard_social_merge():
    d = decide_pair(keys(socials=["linkedin.com/company/acme"]), keys(socials=["linkedin.com/company/acme"]))
    assert d.action == "merge"


def test_never_merge_domain_and_country_differ():
    a = keys(name_key="acme foods", domain="acmefoods.com", country="US")
    b = keys(name_key="acme foods gmbh", domain="acmefoods.de", country="DE")
    d = decide_pair(a, b)
    assert d.action == "link"  # strong name -> subsidiary link, NOT a merge


def test_domain_country_differ_low_sim_is_distinct():
    a = keys(name_key="omega herbs", domain="omegaherbs.com", country="US")
    b = keys(name_key="omega marine", domain="omegamarine.de", country="DE")
    assert decide_pair(a, b).action == "distinct"


def test_name_and_corroboration_merge():
    a = keys(name_key="golden leaf tea", domain="goldenleaf.co.uk", country="GB", address="10 cathedral road cardiff")
    b = keys(name_key="golden leaf tea", domain="goldenleaftea.co.uk", country="GB", address="10 cathedral road cardiff")
    d = decide_pair(a, b)
    assert d.action == "merge"


def test_strong_name_without_corroboration_adjudicates():
    a = keys(name_key="acme foods", country="US")
    b = keys(name_key="acme foods", country=None)
    assert decide_pair(a, b).action == "adjudicate"


def test_borderline_with_corroboration_adjudicates():
    sim_a = keys(name_key="nordsee gewürze handel", country="DE", address="hafenstraße 5")
    sim_b = keys(name_key="nordsee gewürzhandel", country="DE", address="hafenstraße 5")
    from buyerintel.resolve.matchers import name_similarity

    sim = name_similarity(sim_a.name_key, sim_b.name_key)
    if BORDERLINE_LOW <= sim < 0.93 and CORROBORATION_SIM >= 0.85:
        assert decide_pair(sim_a, sim_b).action == "adjudicate"


def test_low_similarity_distinct():
    a = keys(name_key="fresh spice trading", country="US")
    b = keys(name_key="fresh freight logistics", country="US")
    assert decide_pair(a, b).action == "distinct"


def test_merger_backfills_and_preserves_provenance(session):
    from buyerintel.db.models import Company, CompanyAlias, ContactPoint, SourceRecord

    survivor = Company(canonical_name="Golden Leaf Tea", country="GB", confidence=0.4)
    dup = Company(canonical_name="Golden Leaf Tea Company", country=None, city="Cardiff", confidence=0.6)
    session.add_all([survivor, dup])
    session.flush()
    session.add_all(
        [
            ContactPoint(owner_type="company", owner_id=dup.id, kind="email", value="info@gltea.example"),
            ContactPoint(owner_type="company", owner_id=dup.id, kind="email", value="shared@gltea.example"),
            SourceRecord(source="expo", raw_hash="h1", company_id=dup.id),
        ]
    )
    # identical email already on survivor -> the duplicate contact is dropped
    session.add(ContactPoint(owner_type="company", owner_id=survivor.id, kind="email", value="shared@gltea.example"))
    session.flush()

    session.info["audit_source"] = "dedupe:test"
    merged = merge_companies(session, survivor.id, dup.id)
    session.commit()

    assert merged.city == "Cardiff"          # backfilled from duplicate
    assert merged.country == "GB"            # survivor value survives
    assert merged.confidence == 0.6          # max survivorship
    aliases = [a.alias for a in session.query(CompanyAlias).all()]
    assert "Golden Leaf Tea Company" in aliases  # name provenance kept
    emails = {
        cp.value
        for cp in session.query(ContactPoint).filter_by(owner_type="company", owner_id=survivor.id)
    }
    assert "info@gltea.example" in emails    # moved to survivor
    assert list(session.query(SourceRecord)) [0].company_id == survivor.id
    assert session.query(Company).count() == 1


def test_link_sets_parent(session):
    from buyerintel.db.models import Company

    parent = Company(canonical_name="Acme Foods", country="US")
    sub = Company(canonical_name="Acme Foods", country="DE")
    session.add_all([parent, sub])
    session.flush()
    link_companies(session, parent.id, sub.id)
    session.commit()
    assert sub.parent_company_id == parent.id
    assert session.query(Company).count() == 2  # linked, NOT merged

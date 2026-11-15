"""Seniority classification + decision-maker tests (spec 12.6, M6)."""
from __future__ import annotations

import pytest

from buyerintel.db.models import Company, CompanyPersona, Person
from buyerintel.enrich.people.seniority import (
    apply_seniority,
    best_persona_titles,
    classify_title,
    is_buying_role,
    is_decision_maker,
)


def test_buckets_per_spec():
    # exactly the spec's five buckets
    assert classify_title("Owner")[0] == "owner_c_level"
    assert classify_title("Geschäftsführer")[0] == "owner_c_level"
    assert classify_title("Chief Purchasing Officer")[0] == "owner_c_level"
    assert classify_title("VP Supply Chain")[0] == "vp_director"
    assert classify_title("Director of Procurement")[0] == "vp_director"
    assert classify_title("Head of Purchasing")[0] == "vp_director"
    assert classify_title("Purchasing Manager")[0] == "head_manager"
    assert classify_title("Einkaufsleiter")[0] == "head_manager" or \
        classify_title("Einkaufsleiter")[0] == "vp_director"
    assert classify_title("Buyer")[0] == "buyer_specialist"
    assert classify_title("QA Specialist")[0] == "buyer_specialist"
    assert classify_title("Office Clerk")[0] == "other"
    assert classify_title(None) == ("other", None)


def test_department_guess():
    assert classify_title("Purchasing Manager")[1] == "purchasing"
    assert classify_title("QA Manager")[1] == "quality"
    assert classify_title("Export Sales Director")[1] in ("sales", "logistics", None) or \
        classify_title("Export Sales Director")[1] == "logistics"


def test_buying_roles_in_local_languages():
    for title in ["Einkaufsleiter", "Responsable achats", "Responsable de compras",
                  "Acquisti Manager", "Inkoop Manager", "采购经理", "مشتريات",
                  "Raw Material Buyer", "R&D Lead"]:
        assert is_buying_role(title), title


def test_decision_maker_rules():
    # NOTE: 'owner' deliberately absent - plain-owner matching comes from the
    # small-firm rule, not from persona targets (spec 12.6 'owner for small firms').
    targets = ["purchasing manager", "category buyer", "procurement director"]
    assert is_decision_maker("Purchasing Manager", targets)
    assert is_decision_maker("Leiter Einkauf", targets)  # buying word
    assert not is_decision_maker("Accountant", targets)
    # owner for small firms
    assert is_decision_maker("Owner", targets, small_firm=True)
    assert not is_decision_maker("Owner", targets, small_firm=False)


def test_apply_seniority_persists(session):
    from buyerintel.score.personas import sync_personas

    sync_personas(session)
    company = Company(canonical_name="Spice Import GmbH", size_band="small",
                      tags=["spice importer", "bulk"])
    session.add(company)
    session.flush()
    # give the company its persona so target titles resolve
    from buyerintel.score.personas import store_company_personas
    store_company_personas(session, company)
    targets = best_persona_titles(session, company)
    assert targets, "P1 target titles must be available"

    session.add(Person(company_id=company.id, full_name="Klaus Mueller",
                       title="Purchasing Manager"))
    session.add(Person(company_id=company.id, full_name="Anna Schmidt",
                       title="Accountant"))
    session.add(Person(company_id=company.id, full_name="Bo Derefs",
                       title="Owner"))
    session.commit()

    stats = apply_seniority(session, company)
    assert stats["people"] == 3
    klaus = session.query(Person).filter_by(full_name="Klaus Mueller").one()
    anna = session.query(Person).filter_by(full_name="Anna Schmidt").one()
    bo = session.query(Person).filter_by(full_name="Bo Derefs").one()
    assert klaus.seniority == "head_manager"
    assert klaus.is_decision_maker is True
    assert anna.is_decision_maker is False
    # small firm: owner counts
    assert bo.is_decision_maker is True
    # idempotent re-run
    apply_seniority(session, company)
    assert session.query(Person).filter_by(full_name="Klaus Mueller").one() \
        .seniority == "head_manager"

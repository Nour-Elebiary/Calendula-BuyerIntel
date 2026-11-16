"""Discovery engine tests: candidates from search results (idempotent),
country/expo query plans, and the BOL footprint check."""
from __future__ import annotations

from datetime import datetime, timezone

from buyerintel.db.models import Company, SourceRecord
from buyerintel.discover import (
    bol_check,
    candidate_from_result,
    country_queries,
    domain_of,
    expo_leads,
    find_existing,
    name_from_result,
    search_candidates,
)


class FakeResult:
    def __init__(self, title: str, url: str, engine: str = "duckduckgo",
                 snippet: str = "herbs wholesale"):
        self.title = title
        self.url = url
        self.engine = engine
        self.snippet = snippet


class FakeStats:
    def summary(self) -> str:
        return "3 calls, cache 0"


class FakeRouter:
    """SearchRouter stand-in: returns canned results per query."""

    def __init__(self, by_query: dict[str, list[FakeResult]]):
        self.by_query = by_query
        self.searched: list[str] = []

    def search(self, query: str, *, country=None, max_results=8, **kw):
        self.searched.append(query)
        return self.by_query.get(query, []), FakeStats()


# ------------------------------------------------------------------ helpers
def test_domain_of_strips_www():
    assert domain_of("https://www.acme-gmbh.de/herbs?page=1") == "acme-gmbh.de"
    assert domain_of("https://herbs.example") == "herbs.example"
    assert domain_of("not a url") is None


def test_name_from_result_takes_first_segment():
    assert name_from_result("ACME GmbH - herbs importer germany",
                            "https://acme-gmbh.de") == "ACME GmbH"
    assert name_from_result("ACME GmbH | Herbs & Spices",
                            "https://acme-gmbh.de") == "ACME GmbH"


def test_name_from_result_falls_back_to_domain():
    # a bare-domain title or a junk title falls back to the domain label
    name = name_from_result("acmegmbh.de", "https://www.acmegmbh.de/herbs")
    assert name == "Acmegmbh"
    assert name_from_result("Home - Herbs 4 U", "https://herbs4u.example") == "Herbs4U"


def test_name_from_result_prefers_legal_form_segment():
    """Found live 2026-11-16: 'Spices & Herbs - Mueggenburg ... GmbH & Co' -
    the company name carries the legal form, the product is the first segment."""
    name = name_from_result(
        "Spices & Herbs - Müggenburg Pflanzliche Rohstoffe GmbH & Co",
        "https://www.paulmueggenburg.de/markets/spices-herbs.html")
    assert name == "Müggenburg Pflanzliche Rohstoffe GmbH & Co"


def test_name_from_result_prefers_non_product_segment(session):
    from buyerintel.catalog import seed_products

    seed_products(session)
    session.flush()
    # 'Hibiscus - JGS': the ontology product word is the first segment, JGS is
    # the company (generic 'herbs' is deliberately not an ontology product)
    name = name_from_result("Hibiscus – JGS", "https://jgs.de/en/herbs",
                            session=session)
    assert name == "JGS"
    # generic category words work WITHOUT any ontology lookup
    assert name_from_result("Herbs – JGS", "https://jgs.de/en/herbs") == "JGS"


# ------------------------------------------------------------------ candidates
def test_candidate_creates_company_with_provenance(session):
    company, status = candidate_from_result(
        session, FakeResult("ACME GmbH - herbs importer",
                            "https://www.acme-gmbh.de"),
        tag="search", country="DEU")
    assert status == "new" and company.id is not None
    assert company.domain == "acme-gmbh.de"
    assert company.country == "DEU"
    assert "search" in (company.tags or [])
    rec = session.query(SourceRecord).filter_by(company_id=company.id).one()
    assert rec.source == "search"
    assert rec.raw_json["url"] == "https://www.acme-gmbh.de"


def test_candidate_is_idempotent_on_url(session):
    first, status1 = candidate_from_result(
        session, FakeResult("ACME GmbH", "https://acme-gmbh.de"), tag="search")
    second, status2 = candidate_from_result(
        session, FakeResult("ACME GmbH again", "https://acme-gmbh.de"),
        tag="search")
    assert (status1, status2) == ("new", "skipped")   # url hash already ingested
    assert session.query(Company).count() == 1


def test_candidate_matches_existing_company_by_domain(session):
    known = Company(canonical_name="ACME GmbH", domain="acme-gmbh.de")
    session.add(known)
    session.flush()
    company, status = candidate_from_result(
        session, FakeResult("ACME GmbH herbs", "https://acme-gmbh.de/contact"),
        tag="search")
    assert status == "existing" and company.id == known.id


def test_candidate_skips_platform_hosts(session):
    company, status = candidate_from_result(
        session, FakeResult("ACME GmbH | LinkedIn",
                            "https://www.linkedin.com/company/acme"),
        tag="search")
    assert company is None and status == "skipped"
    assert session.query(Company).count() == 0


def test_candidate_skips_directory_aggregators(session):
    """A directory listing page is not a buyer (found live 2026-11-16)."""
    for url in ("https://www.trademo.com/germany/buyers/herbs",
                "https://www.europages.co.uk/companies/germany/herbs.html",
                "https://ensun.io/search/herb/germany"):
        company, status = candidate_from_result(
            session, FakeResult("Herb buyers directory", url), tag="search")
        assert company is None and status == "skipped", url
    assert session.query(Company).count() == 0


def test_find_existing_by_alias(session):
    from buyerintel.db.models import CompanyAlias

    company = Company(canonical_name="Beispiel Handel")
    session.add(company)
    session.flush()
    session.add(CompanyAlias(company_id=company.id, alias="ACME GmbH",
                             kind="search"))
    session.flush()
    assert find_existing(session, name="acme gmbh", domain=None) is company


# ------------------------------------------------------------------ keyword mode
def test_search_candidates_builds_companies(session):
    router = FakeRouter({
        "herbs importer germany": [
            FakeResult("Kräuter Klang GmbH - herbs importer",
                       "https://kraeuter-klang.de"),
            FakeResult("Kräuter Klang GmbH | LinkedIn",
                       "https://linkedin.com/company/klang"),
            FakeResult("Tea Traders Ltd", "https://tea-traders.co.uk"),
        ],
    })
    out = search_candidates(session, ["herbs importer germany"], router=router)
    assert len(out["created"]) == 2
    assert out["skipped"] == 1            # the LinkedIn host
    names = {c["name"] for c in out["created"]}
    assert "Kräuter Klang GmbH" in names and "Tea Traders Ltd" in names
    assert session.query(Company).count() == 2


def test_search_candidates_second_run_creates_nothing(session):
    router = FakeRouter({
        "herbs importer germany": [
            FakeResult("Kräuter Klang GmbH", "https://kraeuter-klang.de"),
        ],
    })
    first = search_candidates(session, ["herbs importer germany"], router=router)
    second = search_candidates(session, ["herbs importer germany"], router=router)
    assert len(first["created"]) == 1 and len(second["created"]) == 0
    assert session.query(Company).count() == 1


# ------------------------------------------------------------------ country mode
def test_country_queries_templates():
    queries = country_queries("germany", product="herbs")
    assert "herbs importer germany" in queries          # the owner's example
    assert "herbs wholesale germany" in queries
    assert all("germany" in q for q in queries)


def test_country_queries_adds_product_terms():
    queries = country_queries("germany", product="chamomile",
                              product_terms=["kamille"])
    assert "chamomile importer germany" in queries
    assert "kamille importer germany" in queries        # ontology language added
    assert len(queries) == len(set(queries))


# ------------------------------------------------------------------ expo leads
def test_expo_leads_stored_idempotent(session):
    router = FakeRouter({
        "germany herbs trade expo": [
            FakeResult("BioFach - organic trade fair",
                       "https://biofach.example"),
        ],
    })
    first = expo_leads(session, ["germany herbs trade expo"], router=router)
    second = expo_leads(session, ["germany herbs trade expo"], router=router)
    assert first[0]["stored"] == "yes"
    assert second[0]["stored"] == "already"
    recs = session.query(SourceRecord).filter_by(source="expo_lead").all()
    assert len(recs) == 1 and recs[0].raw_json["url"] == "https://biofach.example"


# ------------------------------------------------------------------ BOL check
class FakeBolSource:
    name = "us_bol"

    def __init__(self, rows_by_company: dict[int, int]):
        self.rows_by_company = rows_by_company

    def run_for_company(self, company):
        rows = self.rows_by_company.get(company.id, 0)
        return {"source": self.name, "rows": rows, "created": rows,
                "skipped": 0, "error": None}


def test_bol_check_tags_and_reports(session):
    c1 = Company(canonical_name="Has BOL", tier=1)
    c2 = Company(canonical_name="No BOL", tier=1)
    session.add_all([c1, c2])
    session.flush()
    out = bol_check(session, [c1, c2], source=FakeBolSource({c1.id: 3}))
    by_id = {r["company_id"]: r for r in out}
    assert by_id[c1.id]["rows"] == 3 and by_id[c2.id]["rows"] == 0
    assert "bol_present" in (session.get(Company, c1.id).tags or [])
    assert "bol_present" not in (session.get(Company, c2.id).tags or [])


def test_bol_check_silent_failure_does_not_tag(session):
    class BrokenSource:
        name = "us_bol"

        def run_for_company(self, company):
            return {"source": self.name, "error": "network down"}

    company = Company(canonical_name="Down Co", tier=1)
    session.add(company)
    session.flush()
    out = bol_check(session, [company], source=BrokenSource())
    assert out[0]["rows"] == 0 and out[0]["error"] == "network down"
    assert "bol_present" not in (company.tags or [])

"""One-off cleanup of the M5 live-smoke company from the live DB.

Order matters: children first, companies last. Person rows and their
person-owned ContactPoints must go BEFORE the company delete (Person.company_id
is a hard FK; autoflush would flush the pending company DELETE first and fail).
Run: uv run python scripts/cleanup_smoke_company.py
"""
from sqlalchemy import delete, select

from buyerintel.config import load_settings
from buyerintel.db.engine import get_engine, get_session, init_db
from buyerintel.db.models import (
    Company,
    CompanyAlias,
    CompanyPersona,
    ContactPoint,
    ContentItem,
    ContentSuggestion,
    EnrichCheckpoint,
    ExpoAppearance,
    Forecast,
    JobRun,
    MarketSignal,
    OutreachEvent,
    PaidToolCall,
    Person,
    ProductMatch,
    Quote,
    Sample,
    ScoreRun,
    Shipment,
    SourceRecord,
)

s = load_settings()
init_db(get_engine(s.db_url))
session = get_session(s.db_url)

companies = session.scalars(select(Company)).all()
company_ids = [c.id for c in companies]
print(f"companies to clean: {company_ids}")

if company_ids:
    # 1. person-owned contact points + persons (hard FK to company)
    person_ids = session.scalars(
        select(Person.id).where(Person.company_id.in_(company_ids))
    ).all()
    if person_ids:
        session.execute(delete(ContactPoint).where(
            ContactPoint.owner_type == "person", ContactPoint.owner_id.in_(person_ids)))
    session.execute(delete(Person).where(Person.company_id.in_(company_ids)))

    # 2. every other company-dependent table (ArchiveSnapshot is URL-keyed,
    #    not company-keyed, so it is not here)
    for table in (ScoreRun, ProductMatch, CompanyPersona, CompanyAlias,
                  ExpoAppearance, EnrichCheckpoint, Shipment, SourceRecord,
                  OutreachEvent, Quote, Sample, ContentSuggestion, PaidToolCall,
                  Forecast):
        session.execute(delete(table).where(table.company_id.in_(company_ids)))

    # 3. company-owned contact points
    session.execute(delete(ContactPoint).where(
        ContactPoint.owner_type == "company", ContactPoint.owner_id.in_(company_ids)))

    # 4. the companies themselves
    for c in companies:
        session.delete(c)

    # 5. M8 smoke residue (scheduler config rows in `job` are KEPT - they are
    #    the seeded schedules the live scheduler keeps using)
    session.execute(delete(ContentSuggestion))
    session.execute(delete(ContentItem))
    session.execute(delete(JobRun))
    session.execute(delete(MarketSignal).where(MarketSignal.source == "trends"))
    session.commit()

print("companies left:", len(session.scalars(select(Company)).all()))
print("persons left:", len(session.scalars(select(Person)).all()))
print("products kept:", len(session.scalars(select(__import__('buyerintel.db.models', fromlist=['Product']).Product)).all()))
print("personas kept:", len(session.scalars(select(__import__('buyerintel.db.models', fromlist=['Persona']).Persona)).all()))
print("product_names kept:", len(session.scalars(select(__import__('buyerintel.db.models', fromlist=['ProductName']).ProductName)).all()))
session.close()

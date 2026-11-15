"""Core data model (spec section 5). Every table supports the audit layer.

Confidence floats are 0..1 unless noted. All times are UTC ISO strings or datetimes.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

# NOTE: JSON is dialect-agnostic (SQLAlchemy renders it for SQLite as TEXT-backed
# JSON via the JSON type); keep types portable so DuckDB/Postgres stays possible.


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    table_name: Mapped[str] = mapped_column(String(64), index=True)
    row_id: Mapped[str | None] = mapped_column(String(64))
    field: Mapped[str] = mapped_column(String(128))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(128))


class Company(Base):
    __tablename__ = "company"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(255), index=True)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    country: Mapped[str | None] = mapped_column(String(8), index=True)
    city: Mapped[str | None] = mapped_column(String(128))
    address: Mapped[str | None] = mapped_column(Text)
    registry_id: Mapped[str | None] = mapped_column(String(64))
    vat_id: Mapped[str | None] = mapped_column(String(64))
    lei: Mapped[str | None] = mapped_column(String(32))
    type: Mapped[str | None] = mapped_column(String(64))
    size_band: Mapped[str | None] = mapped_column(String(32))
    tier: Mapped[int | None] = mapped_column(Integer, index=True)
    score: Mapped[float | None] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(32), default="active", index=True)
    state_reason: Mapped[str | None] = mapped_column(String(255))
    tags: Mapped[list | None] = mapped_column(JSON, default=list)
    parent_company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"))
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_enriched: Mapped[datetime | None] = mapped_column(DateTime)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)


class CompanyAlias(Base):
    __tablename__ = "company_alias"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    alias: Mapped[str] = mapped_column(String(255), index=True)
    script: Mapped[str | None] = mapped_column(String(16))
    kind: Mapped[str | None] = mapped_column(String(16))  # legal|trade|brand|local|translit
    __table_args__ = (UniqueConstraint("company_id", "alias", "kind"),)


class SourceRecord(Base):
    """Raw ingested row. Idempotency key: (source, raw_hash)."""

    __tablename__ = "source_record"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    expo_edition_id: Mapped[int | None] = mapped_column(ForeignKey("expo_edition.id"))
    raw_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("source", "raw_hash"),)


class Expo(Base):
    __tablename__ = "expo"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)


class ExpoEdition(Base):
    __tablename__ = "expo_edition"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    expo_id: Mapped[int] = mapped_column(ForeignKey("expo.id"), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)


class ExpoAppearance(Base):
    __tablename__ = "expo_appearance"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("expo_edition.id"), index=True)
    booth: Mapped[str | None] = mapped_column(String(64))
    listed_products: Mapped[list | None] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("company_id", "edition_id", "booth"),)


class Person(Base):
    __tablename__ = "person"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    name_local: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    title_normalized: Mapped[str | None] = mapped_column(String(255), index=True)
    seniority: Mapped[str | None] = mapped_column(String(32), index=True)
    department: Mapped[str | None] = mapped_column(String(64))
    is_decision_maker: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (Index("ix_person_company_name", "company_id", "full_name"),)


class ContactPoint(Base):
    __tablename__ = "contact_point"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(16), index=True)  # company|person
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)  # email|phone|linkedin|...
    value: Mapped[str] = mapped_column(String(512), index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    found_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    verified_status: Mapped[str] = mapped_column(
        String(24), default="unverified"
    )  # published|pattern-guessed|verified|bounced
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    is_historical: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("owner_type", "owner_id", "kind", "value"),)


class Product(Base):
    __tablename__ = "product"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    latin_name: Mapped[str | None] = mapped_column(String(128))
    part: Mapped[str | None] = mapped_column(String(64))
    forms: Mapped[list | None] = mapped_column(JSON)
    conventional: Mapped[bool] = mapped_column(Boolean, default=True)
    organic: Mapped[bool] = mapped_column(Boolean, default=False)


class ProductName(Base):
    __tablename__ = "product_name"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True)
    language: Mapped[str] = mapped_column(String(8), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # common|synonym|latin|trade|misspelling
    name: Mapped[str] = mapped_column(String(192), index=True)


class HsCandidate(Base):
    __tablename__ = "hs_candidate"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True)
    hs_code: Mapped[str] = mapped_column(String(16), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)


class ProductMatch(Base):
    __tablename__ = "product_match"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    matched_term: Mapped[str | None] = mapped_column(String(192))
    lang: Mapped[str | None] = mapped_column(String(8))
    evidence_url: Mapped[str | None] = mapped_column(Text)
    evidence_snippet: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("company_id", "product_id", "matched_term"),)


class Persona(Base):
    __tablename__ = "persona"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    persona_id: Mapped[str] = mapped_column(String(8), unique=True)  # P1..P8
    name: Mapped[str] = mapped_column(String(64))
    definition: Mapped[dict | None] = mapped_column(JSON)
    embedding: Mapped[list | None] = mapped_column(JSON)


class CompanyPersona(Base):
    __tablename__ = "company_persona"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    persona_id: Mapped[int] = mapped_column(ForeignKey("persona.id"), index=True)
    similarity: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (UniqueConstraint("company_id", "persona_id"),)


class ScoreRun(Base):
    __tablename__ = "score_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    components: Mapped[dict | None] = mapped_column(JSON)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    tier: Mapped[int | None] = mapped_column(Integer)
    weights_version: Mapped[int] = mapped_column(Integer, default=1)


class Shipment(Base):
    __tablename__ = "shipment"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"), index=True)
    shipper: Mapped[str | None] = mapped_column(String(255))
    consignee: Mapped[str | None] = mapped_column(String(255))
    date: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    hs_code: Mapped[str | None] = mapped_column(String(16), index=True)
    product_desc: Mapped[str | None] = mapped_column(Text)
    qty: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(16))
    value_usd: Mapped[float | None] = mapped_column(Float)
    origin: Mapped[str | None] = mapped_column(String(8))
    dest: Mapped[str | None] = mapped_column(String(8), index=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON)


class TradeFlow(Base):
    __tablename__ = "trade_flow"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reporter: Mapped[str] = mapped_column(String(8), index=True)
    partner: Mapped[str] = mapped_column(String(8), index=True)
    hs: Mapped[str] = mapped_column(String(16), index=True)
    period: Mapped[str] = mapped_column(String(16), index=True)  # e.g. 2025-01
    value_usd: Mapped[float | None] = mapped_column(Float)
    qty: Mapped[float | None] = mapped_column(Float)
    unit_value: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("reporter", "partner", "hs", "period"),)


class MarketSignal(Base):
    """Demand signals (spec section 15): Google Trends, PubMed counts, FX, GDELT.

    Addition to the core table list, logged in docs/DECISIONS.md: the spec's
    section-5 core tables have no home for demand signals, but section 15
    requires storing them. One row per (source, subject, period).
    """

    __tablename__ = "market_signal"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # trends|pubmed|fx|gdelt
    subject: Mapped[str] = mapped_column(String(128), index=True)  # product / currency pair / topic
    period: Mapped[str] = mapped_column(String(16), index=True)
    value: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[dict | None] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("source", "subject", "period"),)


class ArchiveSnapshot(Base):
    __tablename__ = "archive_snapshot"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String(32))  # wayback|commoncrawl|archive.today
    content_hash: Mapped[str | None] = mapped_column(String(64))
    extracted_json: Mapped[dict | None] = mapped_column(JSON)


class OutreachEvent(Base):
    __tablename__ = "outreach_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    channel: Mapped[str] = mapped_column(String(24), index=True)
    direction: Mapped[str | None] = mapped_column(String(8))  # out|in
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    subject: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)


class Quote(Base):
    __tablename__ = "quote"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    products: Mapped[list | None] = mapped_column(JSON)
    qty: Mapped[float | None] = mapped_column(Float)
    incoterm: Mapped[str | None] = mapped_column(String(8))
    price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8))
    date: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str | None] = mapped_column(String(24))


class Sample(Base):
    __tablename__ = "sample"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    product_ids: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str | None] = mapped_column(String(24), index=True)
    shipping_terms: Mapped[str | None] = mapped_column(String(64))
    date: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)


class ContentItem(Base):
    __tablename__ = "content_item"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text)
    image_brief: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ContentSuggestion(Base):
    __tablename__ = "content_suggestion"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("content_item.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    decision: Mapped[str] = mapped_column(String(8))  # send|skip
    reason: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "job"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True)
    cron: Mapped[str | None] = mapped_column(String(64))
    params: Mapped[dict | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    retries: Mapped[int] = mapped_column(Integer, default=0)


class JobRun(Base):
    __tablename__ = "job_run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str | None] = mapped_column(String(16))  # ok|error|skipped
    error: Mapped[str | None] = mapped_column(Text)


class SearchCache(Base):
    __tablename__ = "search_cache"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    engine: Mapped[str | None] = mapped_column(String(32))
    results_json: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LlmCallLog(Base):
    __tablename__ = "llm_call_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str | None] = mapped_column(String(64))
    task: Mapped[str | None] = mapped_column(String(64))
    prompt_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(16))
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

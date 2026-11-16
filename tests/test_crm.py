"""CRM module tests (spec section 16, M8): outreach log + filters,
next-action engine, fixed sample terms, quote tracker, channel recommender,
content queue + suggestion engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from buyerintel.db.models import (
    Company,
    ContentItem,
    ContentSuggestion,
    MarketSignal,
    OutreachEvent,
    ProductMatch,
    Quote,
    Sample,
)
from buyerintel.crm.channels import recommend
from buyerintel.crm.content import draft_content, draft_for_trends, suggest_recipients
from buyerintel.crm.outreach import (
    last_outreach,
    log_outreach,
    never_contacted,
    next_action,
    outreach_summary,
)
from buyerintel.crm.quotes import benchmark_unit_value, log_quote
from buyerintel.crm.samples import SAMPLE_TERMS, offer_sample, set_status
from buyerintel.catalog import seed_products

NOW = datetime.now(timezone.utc)  # DB convention: naive-UTC timestamps


@pytest.fixture(autouse=True)
def products(session):
    seed_products(session)
    session.commit()


# --------------------------------------------------------------- outreach
def test_log_and_filters(session):
    a = Company(canonical_name="Contacted Co")
    b = Company(canonical_name="Silent Co")
    session.add_all([a, b])
    session.flush()
    log_outreach(session, company_id=a.id, channel="email", direction="out",
                 outcome="sent")
    session.commit()

    assert never_contacted(session) == [b]
    last = last_outreach(session, a.id)
    assert last is not None and last.channel == "email"
    assert last_outreach(session, b.id) is None
    summary = outreach_summary(session)
    assert summary[a.id]["n_events"] == 1


def test_next_action_first_contact(session):
    company = Company(canonical_name="Fresh Co", country="DE")
    session.add(company)
    session.flush()
    action = next_action(session, company)
    assert action["action"] == "first_contact"
    assert "never contacted" in action["reason"]
    assert action["channel"]            # recommender picked a channel


def test_next_action_follow_up_after_silence(session):
    company = Company(canonical_name="Quiet Co")
    session.add(company)
    session.flush()
    log_outreach(session, company_id=company.id, channel="email", direction="out",
                 ts=NOW - timedelta(days=45))
    session.flush()
    action = next_action(session, company)
    assert action["action"] == "follow_up"
    assert "45d" in action["reason"]


def test_next_action_within_window_waits(session):
    company = Company(canonical_name="Patient Co")
    session.add(company)
    session.flush()
    log_outreach(session, company_id=company.id, channel="email", direction="out",
                 ts=NOW - timedelta(days=10))
    session.flush()
    assert next_action(session, company)["action"] == "wait"


def test_next_action_sample_feedback_and_quote_chase(session):
    company = Company(canonical_name="Pipeline Co")
    session.add(company)
    session.flush()
    sample = offer_sample(session, company_id=company.id,
                          date=NOW - timedelta(days=20))
    log_outreach(session, company_id=company.id, channel="email", direction="out",
                 ts=NOW - timedelta(days=20))
    session.flush()
    assert next_action(session, company)["action"] == "sample_feedback"

    set_status(session, sample.id, "closed")
    log_quote(session, company_id=company.id, price=2.4,
              date=NOW - timedelta(days=10))
    session.flush()
    assert next_action(session, company)["action"] == "quote_followup"


def test_next_action_reply_keeps_thread(session):
    company = Company(canonical_name="Replier Co")
    session.add(company)
    session.flush()
    log_outreach(session, company_id=company.id, channel="email", direction="in",
                 ts=NOW - timedelta(days=3))
    session.flush()
    action = next_action(session, company)
    assert action["action"] == "reply_sent"


# ---------------------------------------------------------------- samples
def test_sample_terms_are_fixed(session):
    company = Company(canonical_name="Sampler Co")
    session.add(company)
    session.flush()
    sample = offer_sample(session, company_id=company.id, product_ids=[1])
    assert sample.shipping_terms == SAMPLE_TERMS
    # the spec's three fixed clauses, verbatim
    assert "the sample is free" in SAMPLE_TERMS
    assert "freight collect" in SAMPLE_TERMS
    assert "third-party facility" in SAMPLE_TERMS


def test_sample_status_flow(session):
    company = Company(canonical_name="Sample Flow Co")
    session.add(company)
    session.flush()
    sample = offer_sample(session, company_id=company.id)
    set_status(session, sample.id, "shipped")
    assert sample.status == "shipped"
    with pytest.raises(ValueError):
        set_status(session, sample.id, "nonsense")


# ----------------------------------------------------------------- quotes
def test_quote_log_and_benchmark(session):
    from buyerintel.db.models import TradeFlow

    company = Company(canonical_name="Quote Co")
    session.add(company)
    session.add(TradeFlow(reporter="EGY", partner="DEU", hs="0903", period="2025-06",
                          unit_value=2.75))
    session.flush()
    quote = log_quote(session, company_id=company.id, price=3.1, currency="USD",
                      incoterm="CIF", products=["0903"], qty=1000)
    assert quote.incoterm == "CIF" and quote.currency == "USD"
    with pytest.raises(ValueError):
        log_quote(session, company_id=company.id, price=3.1, incoterm="XXX")

    bench = benchmark_unit_value(session, "0903")
    assert bench["unit_value_usd_per_kg"] == 2.75
    assert "not a suggested price" in bench["note"]


# ---------------------------------------------------------------- channels
def test_channel_priors_and_country_override(session):
    de = Company(canonical_name="De Buyer", country="DE")
    vn = Company(canonical_name="Vn Buyer", country="VN")
    session.add_all([de, vn])
    session.flush()
    de_rank = [r["channel"] for r in recommend(session, de, top=3)]
    vn_rank = [r["channel"] for r in recommend(session, vn, top=3)]
    assert de_rank[0] == "email"          # default prior 0.8
    assert vn_rank[0] == "facebook"       # country override 0.8


def test_channel_relearn_from_replies(session):
    company = Company(canonical_name="Learning Co", country="DE")
    session.add(company)
    session.flush()
    for _ in range(10):
        log_outreach(session, company_id=company.id, channel="linkedin", direction="out")
        log_outreach(session, company_id=company.id, channel="linkedin", direction="in")
    for _ in range(10):
        log_outreach(session, company_id=company.id, channel="email", direction="out")
    session.flush()
    rows = {r["channel"]: r for r in recommend(session, company, top=20)}
    assert rows["linkedin"]["observed"] is not None
    # 10 sends / 10 replies -> smoothed ~0.77; email with no replies ~0.25
    assert rows["linkedin"]["score"] > rows["email"]["score"]


def test_channel_availability_filter(session):
    company = Company(canonical_name="No Phone Co", country="DE")
    session.add(company)
    session.flush()
    from buyerintel.db.models import ContactPoint

    session.add(ContactPoint(owner_type="company", owner_id=company.id, kind="email",
                             value="a@b.example", verified_status="published"))
    session.flush()
    channels = {r["channel"] for r in recommend(session, company, top=20)}
    assert "email" in channels
    assert "whatsapp" not in channels      # no phone on file
    assert "linkedin" not in channels


# ---------------------------------------------------------------- content
def _trend(session, subject, value):
    session.add(MarketSignal(source="trends", subject=subject, period="2026-01",
                             value=value))


def test_trending_draft_is_b2b_and_has_image_brief(session):
    _trend(session, "hibiscus", 85.0)
    session.flush()
    items = draft_for_trends(session)
    assert len(items) == 1
    item = items[0]
    assert item.status == "draft"
    assert item.image_brief
    lowered = (item.body or "").lower()
    assert "yield" in lowered and "shelf-life" in lowered
    for banned in ("cure", "treat", "heal", "remedy"):
        assert banned not in lowered
    assert "free" in lowered              # fixed sample terms referenced


def test_draft_only_once_per_product(session):
    _trend(session, "chamomile", 90.0)
    session.flush()
    first = draft_for_trends(session)
    second = draft_for_trends(session)
    assert len(first) == 1 and second == []


def test_suggestions_send_and_skip(session):
    _trend(session, "hibiscus", 85.0)
    item = draft_content(session, subject="hibiscus", trend_index=85.0)
    hot = Company(canonical_name="Hibiscus Buyer", tags=["hibiscus importer"])
    cold = Company(canonical_name="Random Shop", tags=["retail"])
    session.add_all([hot, cold])
    session.flush()
    session.add(ProductMatch(company_id=hot.id, product_id=1, score=5.0,
                             matched_term="hibiscus"))
    session.flush()
    rows = suggest_recipients(session, item)
    by_company = {r.company_id: r for r in rows}
    assert by_company[hot.id].decision == "send"
    assert "product fit" in by_company[hot.id].reason
    assert by_company[cold.id].decision == "skip"


def test_suggestions_skip_competitors(session):
    _trend(session, "peppermint", 80.0)
    item = draft_content(session, subject="peppermint", trend_index=80.0)
    competitor = Company(canonical_name="Rival Exports", tags=["herb exporter"])
    session.add(competitor)
    session.flush()
    session.add(ProductMatch(company_id=competitor.id, product_id=1, score=5.0,
                             matched_term="peppermint"))
    session.flush()
    rows = suggest_recipients(session, item)
    assert all(r.decision == "skip" for r in rows)
    assert "competitor" in rows[0].reason


def test_suggestions_idempotent(session):
    _trend(session, "chamomile", 80.0)
    item = draft_content(session, subject="chamomile", trend_index=80.0)
    company = Company(canonical_name="Idem Co", tags=["chamomile importer"])
    session.add(company)
    session.flush()
    session.add(ProductMatch(company_id=company.id, product_id=1, score=4.0,
                             matched_term="chamomile"))
    session.flush()
    suggest_recipients(session, item)
    suggest_recipients(session, item)
    assert session.query(ContentSuggestion).count() == 1

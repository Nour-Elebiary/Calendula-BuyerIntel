"""Content queue + suggestion engine (spec section 16).

When a herb or segment trends (Google Trends signal above the threshold),
draft a B2B-framed post - extract yield, shelf-life stability, constituent
density; NO consumer medical claims. Each draft carries an image brief.
NOTHING is posted automatically - the user posts manually.

The suggestion engine then lists which companies to share each item with
and which to skip, with reasons: role (decision maker), persona, product
fit, past engagement, recency of last contact.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import (
    Company,
    CompanyPersona,
    ContentItem,
    ContentSuggestion,
    MarketSignal,
    OutreachEvent,
    Person,
    ProductMatch,
)

TREND_THRESHOLD = 60.0  # trends interest index above this = "trending"

# B2B post template (English; other languages come from the confirmed
# languages list - an open slot in docs/03_ADDITIONS). Facts only, no
# consumer medical claims.
POST_TEMPLATE = (
    "{product}: sourcing note for {month_year}.\n\n"
    "Buyers in the {segment} space are watching this origin closely - "
    "search interest is up (index {trend_index:.0f}/100).\n"
    "What matters for your specs:\n"
    "- Extract yield and active constituent density (batch-tested)\n"
    "- Shelf-life stability: whole vs. cut, packaging and storage\n"
    "- Sterilization options (third-party facility, on request)\n\n"
    "Samples are free - the buyer covers shipping. "
    "Egyptian origin, {moq}+ kg orders.\n"
)

IMAGE_BRIEF = (
    "Clean flat-lay: raw {product} on a neutral background, a few whole "
    "and cut pieces, a lab scoop and a small kraft pouch. Natural light, "
    "no text overlay. B2B sourcing mood."
)

FORBIDDEN_CLAIMS = ["cure", "treat", "heal", "remedy", "prevents", "medicines"]


def trending_products(session: Session, *, threshold: float = TREND_THRESHOLD) -> list[dict]:
    """Products with a recent trends signal above the threshold."""
    rows = session.execute(
        select(MarketSignal.subject, MarketSignal.period, MarketSignal.value)
        .where(MarketSignal.source == "trends")
        .order_by(MarketSignal.period.desc())
    ).all()
    best: dict[str, float] = {}
    for subject, _period, value in rows:
        if value is not None and subject not in best:
            best[subject] = float(value)
    return [{"subject": s, "value": v} for s, v in best.items() if v >= threshold]


def draft_content(session: Session, *, subject: str, trend_index: float,
                  segment: str = "food manufacturing",
                  title: str | None = None) -> ContentItem:
    """One B2B-framed draft with an image brief (status=draft, never posted)."""
    product = subject.replace("_", " ").strip()
    body = POST_TEMPLATE.format(product=product.title(),
                                segment=segment,
                                trend_index=trend_index,
                                month_year=datetime.now(timezone.utc).strftime("%B %Y"),
                                moq="500-1,000 kg")
    lowered = body.lower()
    for claim in FORBIDDEN_CLAIMS:
        if claim in lowered:
            raise ValueError(f"template produced a banned claim: {claim!r}")
    item = ContentItem(title=title or f"{product.title()} - sourcing note",
                       body=body, image_brief=IMAGE_BRIEF.format(product=product),
                       status="draft")
    session.add(item)
    session.flush()
    return item


def draft_for_trends(session: Session, *, threshold: float = TREND_THRESHOLD) -> list[ContentItem]:
    """Draft one post per trending product without an existing draft."""
    existing = {item.title.split(" - ")[0].lower()
                for item in session.scalars(select(ContentItem)).all()}
    out = []
    for trend in trending_products(session, threshold=threshold):
        product = trend["subject"].replace("_", " ").strip()
        if product.lower() in existing:
            continue
        out.append(draft_content(session, subject=product,
                                 trend_index=trend["value"]))
    return out


# ------------------------------------------------------- suggestion engine
def suggest_recipients(session: Session, item: ContentItem, *,
                       recency_days: int = 21) -> list[ContentSuggestion]:
    """send/skip per active company, with a stated reason.

    Recompute replaces the item's previous suggestions (idempotent).
    """
    from sqlalchemy import delete

    session.execute(delete(ContentSuggestion).where(ContentSuggestion.content_id == item.id))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    product_words = set((item.title or "").lower().replace(" - ", " ").split())
    suggestions: list[ContentSuggestion] = []

    for company in session.scalars(
        select(Company).where(Company.state == "active")
    ).all():
        decision, reason = _decide(session, company, item, product_words, recency_days, now)
        row = ContentSuggestion(content_id=item.id, company_id=company.id,
                                decision=decision, reason=reason)
        session.add(row)
        suggestions.append(row)
    session.flush()
    return suggestions


def _decide(session: Session, company: Company, item: ContentItem,
            product_words: set[str], recency_days: int, now: datetime) -> tuple[str, str]:
    # state/role gates first - a competitor or excluded firm never gets content
    if (company.state or "active") != "active":
        return "skip", f"state is {company.state}"
    tags = " ".join(company.tags or []).lower()
    if any(word in tags for word in ("exporter", "supplier", "competitor")) \
            and "import" not in tags:
        return "skip", "role: looks like a competitor (exporter/supplier tags)"

    # product fit: matched products overlapping the content subject
    matches = session.scalars(
        select(ProductMatch).where(ProductMatch.company_id == company.id)
    ).all()
    matched_terms = " ".join((m.matched_term or "").lower() for m in matches)
    if product_words & set(matched_terms.split()):
        fit = f"product fit: {matched_terms.strip()[:60]}"
    else:
        return "skip", "no product fit with the content subject"

    # past engagement + recency sharpen the reason, not the decision
    last_out = session.scalars(
        select(OutreachEvent.ts).where(OutreachEvent.company_id == company.id,
                                       OutreachEvent.direction.in_(["out", "in"]))
        .order_by(OutreachEvent.ts.desc()).limit(1)
    ).first()
    engagement = "past engagement: they replied before" \
        if session.scalar(select(OutreachEvent.id)
                          .where(OutreachEvent.company_id == company.id,
                                 OutreachEvent.direction == "in").limit(1)) else None
    recency = None
    if last_out is not None:
        days = (now - (last_out.replace(tzinfo=None) if last_out.tzinfo else last_out)).days
        if days <= recency_days:
            recency = f"fresh thread ({days}d since last contact)"
        elif days > 180:
            recency = f"last contact {days}d ago"

    dm = session.scalar(select(Person.id).where(Person.company_id == company.id,
                                                Person.is_decision_maker.is_(True)).limit(1))
    persona = session.scalar(
        select(CompanyPersona).where(CompanyPersona.company_id == company.id)
        .order_by(CompanyPersona.similarity.desc()).limit(1))

    parts = [fit]
    if dm:
        parts.append("role: decision maker on file")
    if persona:
        parts.append(f"persona: {persona.persona_id}")
    if engagement:
        parts.append(engagement)
    if recency:
        parts.append(recency)
    return "send", "; ".join(parts)

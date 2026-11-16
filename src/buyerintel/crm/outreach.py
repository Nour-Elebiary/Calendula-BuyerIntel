"""Outreach log + next-action engine (spec section 16).

The system logs every contact the user makes and recommends next actions;
it never sends anything itself. Filters (Companies page + CLI):
  - never contacted
  - last reached out (per company / per channel)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from buyerintel.crm.channels import recommend
from buyerintel.db.models import Company, OutreachEvent, Quote, Sample

FOLLOW_UP_DAYS = 30  # no reply after this -> a follow-up becomes the action


def log_outreach(session: Session, *, company_id: int, channel: str,
                 direction: str = "out", outcome: str | None = None,
                 subject: str | None = None, notes: str | None = None,
                 person_id: int | None = None,
                 ts: datetime | None = None) -> OutreachEvent:
    """Log one contact event (channel, date, outcome)."""
    event = OutreachEvent(company_id=company_id, channel=channel, direction=direction,
                          outcome=outcome, subject=subject, notes=notes,
                          person_id=person_id, ts=ts or datetime.now(timezone.utc))
    session.add(event)
    session.flush()
    return event


def never_contacted(session: Session) -> list[Company]:
    """Companies with zero outreach events."""
    contacted_ids = set(session.scalars(
        select(OutreachEvent.company_id).distinct()
    ).all())
    return [c for c in session.scalars(select(Company)).all()
            if c.id not in contacted_ids]


def last_outreach(session: Session, company_id: int,
                  channel: str | None = None) -> OutreachEvent | None:
    query = select(OutreachEvent).where(OutreachEvent.company_id == company_id)
    if channel:
        query = query.where(OutreachEvent.channel == channel)
    return session.scalars(query.order_by(OutreachEvent.ts.desc()).limit(1)).first()


def outreach_summary(session: Session) -> dict[int, dict]:
    """company_id -> {last_ts, last_channel, n_events, replies_in}."""
    rows = session.execute(
        select(OutreachEvent.company_id, OutreachEvent.ts, OutreachEvent.channel,
               OutreachEvent.direction, OutreachEvent.outcome)
    ).all()
    summary: dict[int, dict] = {}
    for company_id, ts, channel, direction, _outcome in rows:
        entry = summary.setdefault(company_id, {"last_ts": None, "last_channel": None,
                                                "n_events": 0, "replies_in": 0})
        entry["n_events"] += 1
        if direction == "in":
            entry["replies_in"] += 1
        if entry["last_ts"] is None or ts > entry["last_ts"]:
            entry["last_ts"] = ts
            entry["last_channel"] = channel
    return summary


# ------------------------------------------------------------- next actions
def next_action(session: Session, company: Company) -> dict:
    """One recommended action per company (spec: the next-action engine).

    Returns {action, reason, channel?, detail?}. Ordered by what is known:
    nothing logged -> first contact; silence -> follow-up; sample pending ->
    wait / ask for feedback; quote open -> chase; recent reply -> keep going.
    """
    last = last_outreach(session, company.id)
    now = datetime.now(timezone.utc)
    if last is None:
        top = recommend(session, company, top=1)
        channel = top[0]["channel"] if top else "email"
        return {"action": "first_contact",
                "reason": "never contacted",
                "channel": channel,
                "detail": f"open with {channel} (recommended channel)"}

    # DB datetimes are naive UTC; event objects in-session may carry tzinfo
    last_utc = last.ts if last.ts.tzinfo else last.ts.replace(tzinfo=timezone.utc)
    days = (now - last_utc).days

    open_sample = session.scalar(
        select(Sample).where(Sample.company_id == company.id,
                             Sample.status.in_(["offered", "shipped", "delivered"]))
        .order_by(Sample.date.desc()).limit(1))
    if open_sample is not None and days >= 14:
        return {"action": "sample_feedback", "reason": "sample in play, no reply",
                "detail": f"sample #{open_sample.id} is {open_sample.status}"}

    open_quote = session.scalar(
        select(Quote).where(Quote.company_id == company.id,
                            Quote.status.in_(["sent", "viewed"]))
        .order_by(Quote.date.desc()).limit(1))
    if open_quote is not None and days >= 7:
        return {"action": "quote_followup", "reason": "quote open, no reply",
                "detail": f"quote #{open_quote.id} is {open_quote.status}"}

    if last.direction == "in":
        top = recommend(session, company, top=1)
        channel = top[0]["channel"] if top else "email"
        return {"action": "reply_sent", "reason": "they replied - keep the thread moving",
                "channel": channel,
                "detail": f"reply came in {days}d ago via {last.channel}"}

    if days >= FOLLOW_UP_DAYS:
        top = recommend(session, company, top=1)
        channel = top[0]["channel"] if top else last.channel
        return {"action": "follow_up", "reason": f"no reply for {days}d (>{FOLLOW_UP_DAYS})",
                "channel": channel, "detail": f"last touch: {last.channel}"}

    return {"action": "wait", "reason": f"last touch {days}d ago - within the follow-up window",
            "detail": f"last touch: {last.channel}"}


def next_actions(session: Session, *, tiers: list[int] | None = None,
                 limit: int | None = None) -> list[dict]:
    """Next action for every active company (optionally tier-scoped)."""
    query = select(Company).where(Company.state == "active").order_by(Company.score.desc())
    if tiers:
        query = query.where(Company.tier.in_(tiers))
    companies = session.scalars(query).all()
    out = [{"company_id": c.id, "name": c.canonical_name, "tier": c.tier,
            **next_action(session, c)} for c in companies]
    return out[:limit] if limit else out

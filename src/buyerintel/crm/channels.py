"""Channel recommender (spec section 16).

Config (config/channels.yaml): channel id -> default_prior, plus per-country
overrides (e.g. Indonesia/Philippines/Bangladesh/Vietnam lean Facebook over
LinkedIn - the spec's own example; China -> WeChat, Russia -> VK, etc.).

Ranking for one company:
  1. start from the country override when present, else the default prior
  2. BLEND with the observed reply rate from outreach_event history
     (Laplace-smoothed: observed = (replies_in + alpha) / (out + alpha + beta))
  3. drop channels with no usable contact point (WhatsApp needs a phone,
     email needs an email address, social channels need the matching kind)

Reply rates re-learn automatically: every ranking reads fresh history.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Company, ContactPoint, OutreachEvent

# channel id -> contact-point kinds that make the channel usable
CHANNEL_REQUIRES = {
    "email": ["email"],
    "phone": ["phone"],
    "whatsapp": ["phone"],
    "contact_form": ["form"],
    "linkedin": ["linkedin"],
    "facebook": ["facebook"],
    "instagram": ["instagram"],
    "tiktok": ["tiktok"],
}

# channels that work WITHOUT a matching contact-point kind (you can reach a
# company through their public page, a marketplace, an RFQ board, expo
# matchmaking or an association without a stored handle)
CHANNEL_FREE = {
    "wechat", "telegram", "vk", "line", "kakao", "zalo",
    "b2b_marketplace", "rfq_board", "expo_matchmaking", "trade_association",
}

ALPHA, BETA = 1.0, 3.0  # Laplace smoothing: 1 reply and 3 non-replies of prior


def _channel_priors() -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    cfg = load_yaml("channels.yaml")
    defaults = {c["id"]: float(c.get("default_prior", 0.5)) for c in cfg.get("channels", [])}
    return defaults, cfg.get("country_priors") or {}


def observed_reply_rates(session: Session, company_id: int) -> dict[str, float]:
    """channel -> smoothed reply rate from this company's outreach history."""
    rows = session.execute(
        select(OutreachEvent.channel, OutreachEvent.direction, OutreachEvent.outcome)
        .where(OutreachEvent.company_id == company_id)
    ).all()
    sent: dict[str, int] = {}
    got: dict[str, int] = {}
    for channel, direction, _outcome in rows:
        if direction == "out":
            sent[channel] = sent.get(channel, 0) + 1
        elif direction == "in":
            got[channel] = got.get(channel, 0) + 1
    rates: dict[str, float] = {}
    for channel, n_sent in sent.items():
        n_got = got.get(channel, 0)
        rates[channel] = (n_got + ALPHA) / (n_sent + ALPHA + BETA)
    return rates


def available_channels(session: Session, company: Company) -> set[str]:
    """Channels this company can actually be reached on.

    A channel with a kind requirement (email needs an email address,
    whatsapp a phone, ...) is dropped when the company has contact points
    but none of the matching kind. When NOTHING is on file yet, every
    channel stays recommendable - the priors decide until contacts land.
    """
    kinds = set(session.scalars(
        select(ContactPoint.kind).where(ContactPoint.owner_type == "company",
                                        ContactPoint.owner_id == company.id)
    ).all())
    if not kinds:
        return set(CHANNEL_REQUIRES) | CHANNEL_FREE  # nothing known: all open
    return {cid for cid, req in CHANNEL_REQUIRES.items() if set(req) & kinds} \
        | CHANNEL_FREE


def recommend(session: Session, company: Company, *, top: int = 3) -> list[dict]:
    """Ranked channels for one company: [{channel, score, prior, observed, source}]."""
    defaults, country_priors = _channel_priors()
    rates = observed_reply_rates(session, company.id)
    usable = available_channels(session, company)
    country = country_priors.get(company.country or "") or {}
    out: list[dict] = []
    for channel, default_prior in defaults.items():
        if channel not in usable:
            continue
        prior = country.get(channel, default_prior)
        observed = rates.get(channel)
        score = 0.5 * prior + 0.5 * observed if observed is not None else prior
        out.append({
            "channel": channel,
            "score": round(score, 3),
            "prior": prior,
            "observed": observed,
            "source": "country_prior" if channel in country else "default",
        })
    # country-specific overrides beat generic defaults on a tie
    out.sort(key=lambda r: (-r["score"], 0 if r["source"] == "country_prior" else 1))
    return out[:top]

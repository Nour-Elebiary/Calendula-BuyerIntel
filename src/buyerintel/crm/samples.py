"""Sample offers (spec section 16).

The sample-offer terms are FIXED TEXT from the spec - never paraphrased,
never negotiated by the system:

  the sample is free; the buyer pays all shipping costs (freight collect
  or their courier account); sterilization is performed at a third-party
  facility and paid by the buyer on request.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from buyerintel.db.models import Sample

SAMPLE_TERMS = (
    "the sample is free; the buyer pays all shipping costs "
    "(freight collect or their courier account); sterilization is "
    "performed at a third-party facility and paid by the buyer on request."
)

STATUSES = ["offered", "shipped", "delivered", "feedback", "closed"]


def offer_sample(session: Session, *, company_id: int, product_ids: list[int] | None = None,
                 status: str = "offered", date: datetime | None = None) -> Sample:
    """Log one sample offer with the fixed terms stored in shipping_terms."""
    if status not in STATUSES:
        raise ValueError(f"unknown sample status {status!r}; allowed: {STATUSES}")
    sample = Sample(company_id=company_id, product_ids=product_ids or [],
                    status=status, shipping_terms=SAMPLE_TERMS,
                    date=date or datetime.now(timezone.utc))
    session.add(sample)
    session.flush()
    return sample


def set_status(session: Session, sample_id: int, status: str) -> Sample:
    sample = session.get(Sample, sample_id)
    if sample is None:
        raise LookupError(f"sample #{sample_id} not found")
    if status not in STATUSES:
        raise ValueError(f"unknown sample status {status!r}; allowed: {STATUSES}")
    sample.status = status
    session.flush()
    return sample

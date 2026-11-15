"""Paid-tool gate S7: Apollo / Hunter (spec section 12.9).

The gate fires ONLY when ALL of these hold:

  1. the company is Tier 1 (never for Tier 2/3 - M6 acceptance)
  2. ``paid_gate.enabled`` is true (config flag, off by default)
  3. company score >= ``paid_gate.min_score``
  4. the company STILL LACKS a key contact after all free methods
     (no decision-maker with a published/verified email)
  5. the monthly credit budget has room left (budget enforced across runs
     via the paid_tool_call table - M6 acceptance)

Each adapter call costs credits (config per tool); every call - including
refusals that consumed a call attempt and every spent credit - is recorded
in ``paid_tool_call``. Without API keys the adapters return ``no_key`` and
spend nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from buyerintel.config import load_secrets, load_yaml
from buyerintel.db.models import Company, ContactPoint, PaidToolCall, Person

# Per-call credit costs (config can override; Hunter 1, Apollo 1 by default).
DEFAULT_CREDIT_COST = {"apollo": 1.0, "hunter": 1.0}


def credits_used_this_month(session: Session, *, now: datetime | None = None) -> float:
    """Sum of credits spent in the current calendar month (all tools)."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return float(session.scalar(
        select(func.coalesce(func.sum(PaidToolCall.credits), 0.0))
        .where(PaidToolCall.ts >= start)
    ) or 0.0)


def lacks_key_contact(session: Session, company: Company) -> bool:
    """True when no decision-maker has a published/verified email.

    'Key contact' per spec 12.9: a named decision maker with a usable
    (published or verified) work email. Pattern-guessed does not count.
    """
    people = session.scalars(
        select(Person).where(Person.company_id == company.id,
                             Person.is_decision_maker.is_(True))
    ).all()
    for person in people:
        contact = session.scalar(
            select(ContactPoint).where(
                ContactPoint.owner_type == "person",
                ContactPoint.owner_id == person.id,
                ContactPoint.kind == "email",
                ContactPoint.verified_status.in_(["published", "verified"]))
        )
        if contact is not None:
            return False
    return True


class PaidToolGate:
    """The S7 gate: eligibility + budget + thin Apollo/Hunter adapters."""

    def __init__(self, session: Session, *, config: dict | None = None):
        self.session = session
        self.cfg = config or load_yaml("enrich.yaml")
        self.gate_cfg = self.cfg.get("paid_gate") or {}

    # ------------------------------------------------------------- eligibility
    def eligibility(self, company: Company) -> tuple[bool, str]:
        """(eligible, reason). Non-Tier-1 NEVER passes - checked first."""
        if (company.tier or 3) != 1:
            return False, "not_tier_1"
        if not self.gate_cfg.get("enabled", False):
            return False, "disabled_by_config"
        min_score = float(self.gate_cfg.get("min_score", 60.0))
        if (company.score or 0.0) < min_score:
            return False, f"score_below_{min_score:g}"
        if not lacks_key_contact(self.session, company):
            return False, "has_key_contact"
        return True, "eligible"

    # ----------------------------------------------------------------- budget
    def budget_left(self) -> float:
        budget = float(self.gate_cfg.get("monthly_credit_budget", 10.0))
        return max(0.0, budget - credits_used_this_month(self.session))

    # ------------------------------------------------------------------- run
    def run(self, company: Company) -> dict:
        """Run the gate for one company; returns a detail dict for S7."""
        eligible, reason = self.eligibility(company)
        if not eligible:
            return {"fired": False, "reason": reason}

        remaining: float = self.budget_left()
        results: dict[str, dict] = {}
        for tool in (self.gate_cfg.get("tools") or ["apollo", "hunter"]):
            cost = float((self.gate_cfg.get("credit_cost") or DEFAULT_CREDIT_COST)
                         .get(tool, 1.0))
            if remaining < cost:
                results[tool] = {"outcome": "skipped_budget",
                                 "detail": {"budget_left": remaining}}
                self._record(tool, company, credits=0.0, outcome="skipped_budget",
                             detail={"budget_left": remaining})
                continue
            outcome, detail = self._call(tool, company)
            if outcome == "ok":
                remaining -= cost
            self._record(tool, company, credits=cost if outcome == "ok" else 0.0,
                         outcome=outcome, detail=detail)
            results[tool] = {"outcome": outcome, "detail": detail}
            if outcome == "ok":
                break  # a usable contact from one tool ends the gate run
        return {"fired": True, "tools": results,
                "budget_left": round(remaining, 2)}

    # --------------------------------------------------------------- adapters
    def _call(self, tool: str, company: Company) -> tuple[str, dict]:
        """Thin HTTP adapter. Without an API key: 'no_key' (spends nothing)."""
        secrets = load_secrets()
        key = secrets.get(f"{tool}_api_key") or secrets.get(f"{tool.upper()}_API_KEY")
        if not key:
            return "no_key", {"note": f"no {tool} api key configured"}
        if tool == "apollo":
            return self._apollo(company, key)
        if tool == "hunter":
            return self._hunter(company, key)
        return "error", {"note": f"unknown tool {tool}"}

    def _apollo(self, company: Company, key: str) -> tuple[str, dict]:
        """Apollo person-enrichment search (domain + title). Free tier."""
        try:
            response = httpx.post(
                "https://api.apollo.io/v1/mixed_people/search",
                json={"api_key": key, "q_organization_domains": company.domain,
                      "person_titles": ["purchasing", "procurement"],
                      "page": 1, "per_page": 3},
                timeout=20.0)
            response.raise_for_status()
            people = response.json().get("people") or []
        except Exception as exc:  # noqa: BLE001 - paid API down is not a crash
            return "error", {"note": str(exc)[:200]}
        if not people:
            return "ok", {"people": 0}
        for entry in people[:3]:
            name = " ".join(str(p) for p in
                            (entry.get("first_name"), entry.get("last_name")) if p)
            if not name:
                continue
            person = Person(company_id=company.id, full_name=name,
                            title=entry.get("title"), confidence=0.6)
            self.session.add(person)
            self.session.flush()
            email = entry.get("email")
            if email:
                self.session.add(ContactPoint(
                    owner_type="person", owner_id=person.id, kind="email",
                    value=str(email).lower(), source_url="apollo",
                    verified_status="published", confidence=0.75))
        self.session.flush()
        return "ok", {"people": len(people[:3])}

    def _hunter(self, company: Company, key: str) -> tuple[str, dict]:
        """Hunter domain-search (published emails for the domain). Free tier."""
        if not company.domain:
            return "error", {"note": "no domain"}
        try:
            response = httpx.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": company.domain, "api_key": key, "limit": 5},
                timeout=20.0)
            response.raise_for_status()
            emails = (response.json().get("data") or {}).get("emails") or []
        except Exception as exc:  # noqa: BLE001
            return "error", {"note": str(exc)[:200]}
        for entry in emails[:5]:
            value = (entry.get("value") or "").lower()
            if not value:
                continue
            existing = self.session.scalar(
                select(ContactPoint).where(ContactPoint.owner_type == "company",
                                           ContactPoint.owner_id == company.id,
                                           ContactPoint.kind == "email",
                                           ContactPoint.value == value))
            if existing is None:
                self.session.add(ContactPoint(
                    owner_type="company", owner_id=company.id, kind="email",
                    value=value, source_url="hunter",
                    verified_status="published", confidence=0.75))
        self.session.flush()
        return "ok", {"emails": len(emails[:5])}

    # ----------------------------------------------------------------- record
    def _record(self, tool: str, company: Company, *, credits: float,
                outcome: str, detail: dict) -> None:
        self.session.add(PaidToolCall(tool=tool, company_id=company.id,
                                      credits=credits, outcome=outcome,
                                      detail=detail))
        self.session.flush()

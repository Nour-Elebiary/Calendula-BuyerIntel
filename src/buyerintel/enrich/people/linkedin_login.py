"""Login-based LinkedIn adapters (spec section 12.8) - DISABLED BY DEFAULT.

StaffSpy / linkedin_scraper / linkedin-mcp-server style adapters need a
logged-in account. Per the spec and the addendum open slot ("LinkedIn
secondary account: not provided - login adapters are disabled"), these
adapters REFUSE to run while ``linkedin.enabled`` is false, and when
enabled they enforce: secondary account only (never the user's personal
profile), Tier 1 companies only, hard daily caps, slow pacing.

The non-login path (people/linkedin_public.py) is the default for person
enrichment and runs for all tiers; this module exists so the optional
adapter is a real refusal, not a silent absence (M6 acceptance:
"login adapters refuse to run when disabled").
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Company


class LinkedInLoginDisabled(Exception):
    """Raised when a login adapter is invoked while ``linkedin.enabled`` is false."""


class LinkedInLoginAdapter:
    """Stub for StaffSpy-style login adapters (never implemented by default)."""

    def __init__(self, session: Session, *, config: dict | None = None):
        self.session = session
        self.cfg = config or load_yaml("enrich.yaml")
        self.linkedin_cfg = self.cfg.get("linkedin") or {}

    def enabled(self) -> bool:
        return bool(self.linkedin_cfg.get("enabled", False))

    def run(self, company: Company) -> dict:
        """Run a login-based people scan for one company - or refuse.

        Refusal (M6 acceptance): raises LinkedInLoginDisabled when the
        config flag is off. When enabled, still refuses unless every spec
        guard holds (tier 1 only, secondary account configured, under the
        daily cap).
        """
        if not self.enabled():
            raise LinkedInLoginDisabled(
                "login-based LinkedIn adapters are disabled "
                "(config/enrich.yaml linkedin.enabled: false); the non-login "
                "public-profile scraper is the default path")
        if (company.tier or 3) != 1:
            return {"ran": False, "reason": "login adapters are Tier 1 only"}
        if not self.linkedin_cfg.get("account_profile") or \
                self.linkedin_cfg.get("account_profile") == "personal":
            return {"ran": False, "reason": "no secondary account configured"}
        # A real adapter (StaffSpy etc.) would go here behind its own flag,
        # pinned, with hard daily caps and slow pacing. None is shipped.
        return {"ran": False, "reason": "no login adapter shipped (stub)"}

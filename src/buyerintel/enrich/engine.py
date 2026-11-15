"""Progressive enrichment pipeline (spec section 6, M4 scope S0-S4).

Every company enters at the cheap stage and goes deeper only if promising:
  S0 ingest/normalize/dedupe check (all)
  S1 cheap pre-score from name, domain, segment keywords, country, expo type (all)
  S2 website crawl + LLM-assisted extraction + tags (pre-score >= threshold)
  S3 multi-engine search passes in local languages (Tier 3 and up)
  S4 social discovery + job-post signals + archive snapshots (Tier 2 and up)

The pipeline is a resumable queue: work is checkpointed per company in
`enrich_checkpoint`, so a forced stop resumes at the next stage, and the
coverage meter per tier (enriched / total) is derived from the checkpoints.
It never silently samples - the queue covers every matching company.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import (
    Company,
    ContactPoint,
    EnrichCheckpoint,
    ExpoAppearance,
    Person,
)
from buyerintel.enrich.crawl import SiteCrawler, strip_tags
from buyerintel.enrich.extract import extract_site, extract_tags, regex_contacts
from buyerintel.enrich.osint import fetch_archive_snapshots, job_post_signals, store_archive
from buyerintel.enrich.social import discover_socials, store_socials
from buyerintel.enrich.verify import verify_email

STAGES = ["s0", "s1", "s2", "s3", "s4"]

# Cheap pre-score keyword seeds (name/domain hits). Segment words for herbs.
SEGMENT_KEYWORDS = [
    "herb", "herbs", "spice", "spices", "botanical", "botanicals", "seasoning",
    "tea", "infusion", "extract", "ingredient", "food", "kräuter", "gewürz",
    "épice", "especias", "香辛料", "herbal",
]
IMPORTER_KEYWORDS = ["import", "export", "trading", "trade", "handel", "comercio",
                     "commercio", "групп", "impex", "impex"]

PERSON_KIND_MAP = {"email": "email", "phone": "phone", "linkedin": "linkedin",
                   "facebook": "facebook", "instagram": "instagram", "tiktok": "tiktok",
                   "youtube": "youtube", "x": "x", "form": "form", "other": "other"}


@dataclass
class EnrichStats:
    companies: int = 0
    s1_prescored: int = 0
    s2_crawled: int = 0
    s3_searched: int = 0
    s4_signals: int = 0
    skipped_prescore: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"companies={self.companies} s1={self.s1_prescored} s2={self.s2_crawled} "
            f"s3={self.s3_searched} s4={self.s4_signals} "
            f"skipped(prescore)={self.skipped_prescore} errors={len(self.errors)}"
        )


def prescore(company: Company, *, expo_count: int = 0) -> float:
    """S1 cheap pre-score from name, domain, keywords, country, expo type.

    Deterministic 0-10; decides whether S2 (crawl) is worth the requests.
    """
    score = 0.0
    name = (company.canonical_name or "").lower()
    domain = (company.domain or "").lower()
    text = f"{name} {domain}"
    hits = sum(1 for kw in SEGMENT_KEYWORDS if kw in text)
    score += min(3.0, hits * 1.5)
    score += 1.0 if any(kw in text for kw in IMPORTER_KEYWORDS) else 0.0
    if domain:
        score += 1.0  # has its own domain: a real business
        tld = domain.rsplit(".", 1)[-1]
        score += 0.5 if tld in ("com", "net") else 1.0  # ccTLD = local presence
    if company.country:
        score += 0.5
    score += min(2.0, expo_count * 0.5)  # expo appearances = real trader signal
    return round(min(10.0, score), 2)


def coverage(session: Session) -> list[dict]:
    """Coverage meter per tier: enriched (s2 done) / total companies."""
    rows = session.execute(
        select(Company.tier, func.count(Company.id)).group_by(Company.tier)
    ).all()
    enriched_rows = session.execute(
        select(Company.tier, func.count(Company.id))
        .join(EnrichCheckpoint, EnrichCheckpoint.company_id == Company.id)
        .where(EnrichCheckpoint.stage.in_(["s2", "s3", "s4"]))
        .where(EnrichCheckpoint.status == "done")
        .group_by(Company.tier)
    ).all()
    totals = {tier: count for tier, count in rows}
    enriched = {tier: count for tier, count in enriched_rows}
    out = []
    for tier in sorted({t for t in totals if t is not None} | {t for t in enriched if t is not None}):
        total = totals.get(tier, 0)
        done = enriched.get(tier, 0)
        out.append({"tier": tier, "enriched": done, "total": total,
                    "coverage": round(done / total, 3) if total else 0.0})
    return out


class EnrichEngine:
    """Resumable, checkpointed enrichment over the company queue."""

    def __init__(self, session: Session, *, llm=None, search=None, config: dict | None = None):
        self.session = session
        self.cfg = config or load_yaml("enrich.yaml")
        self.llm = llm            # LLMRouter or None (S2 extraction degrades)
        self.search = search      # SearchRouter or None (S3/S4 search degrades)
        self.crawler = SiteCrawler(session, config=self.cfg)
        self.stats = EnrichStats()

    # ------------------------------------------------------------ checkpoint
    def _checkpoint(self, company_id: int) -> EnrichCheckpoint:
        row = self.session.scalar(
            select(EnrichCheckpoint).where(EnrichCheckpoint.company_id == company_id)
        )
        if row is None:
            row = EnrichCheckpoint(company_id=company_id, stage="", status="pending")
            self.session.add(row)
            self.session.flush()
        return row

    def _mark_done(self, checkpoint: EnrichCheckpoint, stage: str, detail: dict) -> None:
        checkpoint.stage = stage
        checkpoint.status = "done"
        checkpoint.detail = detail
        self.session.flush()

    # ------------------------------------------------------------------ queue
    def pending_companies(self, *, limit: int | None = None,
                          tiers: list[int] | None = None) -> list[Company]:
        """Every company still needing work - never silently sampled.

        A checkpoint with status 'done' means the company is fully processed
        for now (either finished S4 or stopped at S1 below threshold); it does
        not re-enter the queue.
        """
        query = select(Company).order_by(Company.id)
        if tiers:
            query = query.where(Company.tier.in_(tiers))
        companies = list(self.session.scalars(query).unique().all())
        checkpoints = {
            row.company_id: row
            for row in self.session.scalars(select(EnrichCheckpoint)).all()
        }
        pending = []
        for company in companies:
            checkpoint = checkpoints.get(company.id)
            if checkpoint is not None and checkpoint.status == "done" and self._is_complete(checkpoint):
                continue  # finished S4 or stopped at S1 by design: not pending
            pending.append(company)
        return pending[:limit] if limit else pending

    @staticmethod
    def _is_complete(checkpoint: EnrichCheckpoint) -> bool:
        """Done means finished S4 or deliberately stopped (e.g. at S1).

        A crash can leave (stage=X, status=done) with no stop marker when the
        process died between the stage commit and the next running-flip
        commit - such a company is still pending and resumes.
        """
        detail = checkpoint.detail or {}
        return checkpoint.stage == "s4" or bool(detail.get("stop"))

    def run(self, *, limit: int | None = None, tiers: list[int] | None = None) -> EnrichStats:
        companies = self.pending_companies(limit=limit, tiers=tiers)
        for company in companies:
            try:
                self.run_company(company)
                self.session.commit()
            except Exception as exc:  # noqa: BLE001 - one company never stops the queue
                self.session.rollback()
                self.stats.errors.append(f"company {company.id}: {exc}")
        return self.stats

    # ------------------------------------------------------------ per company
    def run_company(self, company: Company) -> None:
        """Run all stages for one company, resuming at its checkpoint.

        A stage may return {"stop": True} - the pipeline then records the
        checkpoint as done at that stage and goes no deeper (e.g. S1
        pre-score below threshold).
        """
        checkpoint = self._checkpoint(company.id)
        if checkpoint.status == "done" and self._is_complete(checkpoint):
            return  # fully processed (or deliberately stopped) - resumable no-op
        done = set()
        if checkpoint.status in ("done", "running") and checkpoint.stage:
            # stage=X recorded means X completed; resume at the next stage.
            done = {STAGES[i] for i in range(STAGES.index(checkpoint.stage) + 1)}
        for stage in STAGES:
            if stage in done:
                continue
            checkpoint.status = "running"
            # Commit the running-flip BEFORE the stage: a crash during the
            # stage leaves (stage=last-done, status=running) on disk, so the
            # resume knows exactly where to continue.
            self.session.commit()
            handler = getattr(self, f"_stage_{stage}")
            detail = handler(company) or {}
            if detail.get("stop"):
                checkpoint.stage = stage
                checkpoint.status = "done"
                checkpoint.detail = detail
                self.session.flush()
                return
            self._mark_done(checkpoint, stage, detail)
            # Per-stage commit: a forced stop loses at most the stage in flight.
            self.session.commit()

    # ----------------------------------------------------------------- stages
    def _stage_s0(self, company: Company) -> dict:
        """Ingest/normalize/dedupe sanity: domain normalized, aliases intact."""
        if company.domain:
            company.domain = company.domain.lower().removeprefix("http://").removeprefix(
                "https://").strip("/")
        return {"domain": company.domain}

    def _stage_s1(self, company: Company) -> dict:
        """Cheap pre-score; decides whether S2 runs (spec section 6)."""
        expo_count = self.session.scalar(
            select(func.count(ExpoAppearance.id)).where(ExpoAppearance.company_id == company.id)
        ) or 0
        score = prescore(company, expo_count=expo_count)
        company.score = score
        self.stats.s1_prescored += 1
        threshold = float((self.cfg.get("stages") or {}).get("prescore_threshold", 3.0))
        if score < threshold:
            self.stats.skipped_prescore += 1
            return {"prescore": score, "s2": "skipped_below_threshold", "stop": True}
        return {"prescore": score, "s2": "queued"}

    def _stage_s2(self, company: Company) -> dict:
        """Website crawl, extraction, tags, people and contacts (spec 12.1-12.4)."""
        self.stats.s2_crawled += 1
        detail: dict = {"tags": [], "contacts": 0, "people": 0, "pages": 0}
        if not company.domain:
            return {"skipped": "no domain"}

        crawl = self.crawler.crawl(company.domain)
        detail["pages"] = len(crawl.pages)
        detail["blocked_urls"] = crawl.failed
        if not crawl.pages:
            detail["note"] = "no page fetched"
            return detail

        all_html = "\n".join(p.html for p in crawl.pages)
        plain_text = " ".join(strip_tags(p.html) for p in crawl.pages)

        # 1. Regex contacts from raw HTML -> contact_point rows (published).
        added = self._store_contacts(company, regex_contacts(all_html))
        detail["contacts"] = added

        # 2. Tags from site text (informational).
        tags = extract_tags(plain_text, config=self.cfg)
        if tags:
            company.tags = sorted(set((company.tags or []) + tags))
        detail["tags"] = tags

        # 3. LLM structured extraction (skips gracefully without providers).
        if self.llm is not None:
            try:
                extracted = extract_site(self.session, self.llm, plain_text)
                self._store_extraction(company, extracted, all_html)
                detail["extraction"] = "ok"
            except Exception as exc:  # noqa: BLE001 - extraction never kills the stage
                detail["extraction"] = f"failed: {exc}"

        company.last_enriched = datetime.now(timezone.utc)
        return detail

    def _store_contacts(self, company: Company, contacts: list[dict]) -> int:
        added = 0
        for contact in contacts:
            kind = PERSON_KIND_MAP.get(contact["kind"], "other")
            value = contact["value"][:512]
            existing = self.session.scalar(
                select(ContactPoint).where(
                    ContactPoint.owner_type == "company",
                    ContactPoint.owner_id == company.id,
                    ContactPoint.kind == kind,
                    ContactPoint.value == value,
                )
            )
            if existing:
                continue
            status = "published"
            confidence = 0.7
            if kind == "email":
                verdict = verify_email(value, found_on_page=True)
                status = verdict["status"]
                confidence = verdict["confidence"]
            session_contact = ContactPoint(
                owner_type="company",
                owner_id=company.id,
                kind=kind,
                value=value,
                verified_status=status,
                confidence=confidence,
            )
            self.session.add(session_contact)
            added += 1
        self.session.flush()
        return added

    def _store_extraction(self, company: Company, extracted: dict, html: str) -> None:
        """Persist people + contacts from LLM extraction (tags already done)."""
        for person in extracted.get("people") or []:
            if not person.get("full_name"):
                continue
            existing = self.session.scalar(
                select(Person).where(Person.company_id == company.id,
                                     Person.full_name == person["full_name"])
            )
            if existing:
                continue
            session_person = Person(company_id=company.id, full_name=person["full_name"],
                                    title=person.get("title"), confidence=0.5)
            self.session.add(session_person)
            self.session.flush()
            if person.get("email"):
                verdict = verify_email(person["email"])
                self.session.add(
                    ContactPoint(
                        owner_type="person", owner_id=session_person.id, kind="email",
                        value=person["email"].lower(),
                        verified_status=verdict["status"],
                        confidence=verdict["confidence"],
                    )
                )
        emails = {c["value"].lower() for c in regex_contacts(html)}
        extra = [c for c in (extracted.get("contacts") or [])
                 if c.get("kind") == "email" and c.get("value", "").lower() not in emails]
        self._store_contacts(company, extra)
        self.session.flush()

    def _stage_s3(self, company: Company) -> dict:
        """Multi-engine search passes in local languages (spec 12 / section 8)."""
        self.stats.s3_searched += 1
        if self.search is None:
            return {"skipped": "no search router"}
        from buyerintel.search.querygen import build_company_plan

        plan = build_company_plan(
            company.canonical_name, country=company.country, domain=company.domain,
        )
        run, hits = 0, 0
        for query in plan.queries:
            try:
                results, _stats = self.search.search(query.text, query_type=query.query_type,
                                                     country=company.country, max_results=5)
            except Exception:  # noqa: BLE001 - a failed query never stops the plan
                continue
            run += 1
            hits += len(results)
        return {"queries": len(plan.queries), "run": run, "results": hits,
                "languages": plan.as_dicts() and sorted({q["language"] for q in plan.as_dicts()})}

    def _stage_s4(self, company: Company) -> dict:
        """Social discovery, job-post signals, archive snapshots (spec 12.5-12.7)."""
        self.stats.s4_signals += 1
        detail: dict = {"socials": 0, "job_posts": 0, "archive": 0}
        if self.search is not None:
            try:
                socials = discover_socials(self.session, self.search, company.canonical_name,
                                           config=self.cfg)
                detail["socials"] = store_socials(self.session, company.id, socials)
            except Exception as exc:  # noqa: BLE001
                detail["socials_error"] = str(exc)
            try:
                posts = job_post_signals(self.session, self.search, company.canonical_name,
                                         config=self.cfg)
                detail["job_posts"] = len(posts)
            except Exception as exc:  # noqa: BLE001
                detail["jobs_error"] = str(exc)
        if company.domain:
            try:
                snapshots = fetch_archive_snapshots(company.domain)
                detail["archive"] = store_archive(self.session, company.domain, snapshots)
            except Exception as exc:  # noqa: BLE001 - archive.org down is not fatal
                detail["archive_error"] = str(exc)
        return detail

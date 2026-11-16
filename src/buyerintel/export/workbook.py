"""One-click multi-sheet Excel workbook export (spec section 18, M8).

Sheets:
  Summary        - counts: companies by tier, by state, per-persona counts,
                   score stats (the dashboard's numbers)
  Tier1/Tier2/3  - one sheet per tier with the company profile columns
  Personas P1..P8- one sheet per persona with its member companies
  Contacts       - every contact point with owner, verification status
  Market Digest  - trade flows + latest signals
  Shipments/Forecast, Outreach/Quotes - if data exists
  Changelog      - generated from the audit log (every change ever made)

Usage: uv run buyerintel export --out path/to/workbook.xlsx
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.db.models import (
    AuditLog,
    Company,
    CompanyPersona,
    ContactPoint,
    Forecast,
    MarketSignal,
    OutreachEvent,
    Person,
    Persona,
    Quote,
    ScoreRun,
    Shipment,
    TradeFlow,
)

MAX_SHEET_ROWS = 5000  # single-user workbook: cap wide sheets


def _sheet(wb: Workbook, title: str, headers: list[str],
           rows: list[list]) -> None:
    ws = wb.create_sheet(title[:31])  # Excel sheet-name cap
    ws.append(headers)
    for row in rows[:MAX_SHEET_ROWS]:
        ws.append([v if not isinstance(v, (list, dict)) else str(v)[:200] for v in row])
    for i, header in enumerate(headers, start=1):
        width = min(40, max(12, len(header) + 2,
                            *(len(str(r[i - 1])) + 2 for r in rows[:200])))
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"


def export_workbook(session: Session, out: str | Path) -> Path:
    """Generate the workbook; returns the written path."""
    wb = Workbook()
    wb.remove(wb.active)

    companies = session.scalars(select(Company)).all()
    personas = session.scalars(select(Persona)).all()
    cp_rows = session.execute(
        select(CompanyPersona.company_id, CompanyPersona.persona_id,
               CompanyPersona.similarity, Persona.persona_id)
        .join(Persona, Persona.id == CompanyPersona.persona_id)
    ).all()
    persona_of = {cid: pid for cid, _pid, _sim, pid in cp_rows}
    score_of = {r.company_id: r.total for r in
                session.scalars(select(ScoreRun).order_by(ScoreRun.ts.desc())).all()}
    # keep only the latest score per company
    seen: set[int] = set()
    latest_score: dict[int, float] = {}
    for run in session.scalars(select(ScoreRun).order_by(ScoreRun.ts.desc())).all():
        if run.company_id not in seen:
            seen.add(run.company_id)
            latest_score[run.company_id] = run.total

    # ---------------------------------------------------------- Summary
    tiers = {1: 0, 2: 0, 3: 0}
    states: dict[str, int] = {}
    for c in companies:
        if c.tier in tiers:
            tiers[c.tier] += 1
        states[c.state or "active"] = states.get(c.state or "active", 0) + 1
    scored = [v for v in latest_score.values()]
    summary_rows = [
        ["generated_at", datetime.now(timezone.utc).isoformat()],
        ["companies", len(companies)],
        ["tier 1", tiers[1]], ["tier 2", tiers[2]], ["tier 3", tiers[3]],
    ]
    for state, count in sorted(states.items()):
        summary_rows.append([f"state: {state}", count])
    if scored:
        summary_rows += [["scores: min", min(scored)], ["scores: max", max(scored)],
                         ["scores: avg", round(sum(scored) / len(scored), 2)]]
    for persona in personas:
        summary_rows.append([f"persona {persona.persona_id} ({persona.name})",
                             sum(1 for _cid, _pid, _sim, pid in cp_rows
                                 if pid == persona.persona_id)])
    _sheet(wb, "Summary", ["metric", "value"], summary_rows)

    # ------------------------------------------------------- tier sheets
    company_headers = ["id", "name", "country", "tier", "state", "score",
                       "size_band", "domain", "tags", "persona"]
    for tier in (1, 2, 3):
        rows = [[c.id, c.canonical_name, c.country, c.tier, c.state,
                 latest_score.get(c.id), c.size_band, c.domain,
                 ", ".join(c.tags or []), persona_of.get(c.id)]
                for c in companies if c.tier == tier]
        _sheet(wb, f"Tier{tier}", company_headers, rows)

    # ---------------------------------------------------- persona sheets
    for persona in personas:
        member_ids = {cid for cid, _pid, _sim, pid in cp_rows
                      if pid == persona.persona_id}
        rows = [[c.id, c.canonical_name, c.country, c.tier, c.state,
                 latest_score.get(c.id)]
                for c in companies if c.id in member_ids]
        _sheet(wb, persona.persona_id, ["id", "name", "country", "tier", "state", "score"],
               rows)

    # ------------------------------------------------------- contacts
    people = session.scalars(select(Person)).all()
    person_names = {p.id: (p.full_name or "?") for p in people}
    contact_rows = []
    for cp in session.scalars(select(ContactPoint)).all():
        owner = (f"person: {person_names.get(cp.owner_id)}" if cp.owner_type == "person"
                 else next((c.canonical_name for c in companies if c.id == cp.owner_id),
                           f"company #{cp.owner_id}"))
        contact_rows.append([owner, cp.kind, cp.value, cp.verified_status,
                             cp.confidence])
    _sheet(wb, "Contacts", ["owner", "kind", "value", "verified", "confidence"],
           contact_rows)

    # ----------------------------------------------------- market digest
    digest_rows = [[t.reporter, t.partner, t.hs, t.period, t.value_usd, t.qty,
                    t.unit_value]
                   for t in session.scalars(select(TradeFlow)).all()]
    _sheet(wb, "Market Digest", ["reporter", "partner", "hs", "period",
                                 "value_usd", "qty", "unit_value"], digest_rows)
    signal_rows = [[s.source, s.subject, s.period, s.value]
                   for s in session.scalars(select(MarketSignal)).all()]
    _sheet(wb, "Signals", ["source", "subject", "period", "value"], signal_rows)

    # ------------------------------------------- shipments / forecast
    ship_rows = [[s.source, s.company_id, s.date, s.hs_code, s.qty, s.value_usd,
                  s.origin, s.dest]
                 for s in session.scalars(select(Shipment)).all()]
    _sheet(wb, "Shipments", ["source", "company_id", "date", "hs", "qty",
                             "value_usd", "origin", "dest"], ship_rows)
    fc_rows = [[f.company_id, f.product_key, f.n_shipments, f.confidence,
                f.window_start, f.window_end, f.qty_min, f.qty_max,
                f.value_min_usd, f.value_max_usd, f.uv_source]
               for f in session.scalars(select(Forecast)).all()]
    _sheet(wb, "Forecast", ["company_id", "product", "n_shipments", "confidence",
                            "window_start", "window_end", "qty_min", "qty_max",
                            "value_min_usd", "value_max_usd", "unit_value_source"],
           fc_rows)

    # ------------------------------------------------- outreach + quotes
    company_names = {c.id: c.canonical_name for c in companies}
    out_rows = [[company_names.get(e.company_id, e.company_id), e.channel,
                 e.direction, e.ts, e.outcome, e.subject]
                for e in session.scalars(select(OutreachEvent)).all()]
    _sheet(wb, "Outreach", ["company", "channel", "direction", "ts", "outcome",
                            "subject"], out_rows)
    quote_rows = [[company_names.get(q.company_id, q.company_id), q.date, q.currency,
                   q.price, q.incoterm, q.qty, q.status, ", ".join(q.products or [])]
                  for q in session.scalars(select(Quote)).all()]
    _sheet(wb, "Quotes", ["company", "date", "currency", "price", "incoterm",
                          "qty", "status", "products"], quote_rows)

    # ------------------------------------------------------- changelog
    change_rows = [[log.ts, log.table_name, log.row_id, log.field,
                    log.old_value, log.new_value, log.source]
                   for log in session.scalars(
                       select(AuditLog).order_by(AuditLog.ts.desc())).all()]
    _sheet(wb, "Changelog", ["ts", "table", "row", "field", "old", "new", "source"],
           change_rows)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def expected_sheets() -> list[str]:
    """The sheet names always present (persona sheets P1..P8 are added only
    when personas exist; they are asserted separately in the seeded test)."""
    return (["Summary", "Tier1", "Tier2", "Tier3", "Contacts", "Market Digest",
             "Signals", "Shipments", "Forecast", "Outreach", "Quotes",
             "Changelog"])

# Acceptance record — Calendula BuyerIntel (M0–M8)

Every milestone in spec section 20, with the evidence that it passed. All evidence
comes from the committed test suite (`uv run pytest` — 327 tests) plus the live
CLI/UI smoke runs recorded at each milestone close.

| M | Scope (spec 20) | Acceptance criterion | Evidence |
| --- | --- | --- | --- |
| M0 | Repo, config, DB schema, audit, import | Re-import creates zero duplicates; every change in the audit log | `tests/test_importer.py` (5) idempotent import; `tests/test_audit.py` (5) every write audited; `tests/test_models.py` + `tests/test_config.py` (14) schema/config. Verified live: importing the lead workbook twice leaves one row per company. |
| M1 | Dedupe, expo ingestion | 0.98 precision / 0.90 recall on golden set; expo re-ingest is a no-op | `tests/test_golden.py` (2) precision/recall gates on the golden pairs; `tests/test_dedupe.py` (12); `tests/test_expos.py` (5) re-ingest no-op; `tests/test_matchers.py` (6) alias matching. |
| M2 | Ontology, market module, market page | Ontology resolves common + Latin names; market page shows Egypt exports and destination imports per HS | `tests/test_ontology.py` (7); `tests/test_market_report.py` (5); `tests/test_comtrade.py` (3), `tests/test_comext.py` (3), `tests/test_trademap.py` (5), `tests/test_signals.py` (4). Live: Market Intelligence page (UI) renders exports by partner and imports per HS. |
| M3 | Search router, query builder, LLM router | Simulated failures fall back without job loss; cache hit rate logged | `tests/test_search_router.py` (8) engine failures -> next engine; `tests/test_querygen.py` (8) multilingual queries; `tests/test_llm_router.py` (10) provider failure/rotation/schema validation/quota. |
| M4 | Enrichment engine, escalation ladder, S0–S4 | Fixture sites yield contacts/tags; ladder advances only on prior-rung failure, rungs 4–6 respect flags; resumable | `tests/test_enrich_engine.py` (9), `tests/test_enrich_fetch.py` (9) ladder order + flags, `tests/test_enrich_verify.py` (8) contact verification, `tests/test_challenges_and_catalog.py` (7). Checkpoint resume covered by EnrichCheckpoint rows. |
| M5 | Personas, scoring, tiers, rule chain | Score breakdown reproducible; rule-chain tests pass; tier changes logged | `tests/test_score_scoring.py` (7) deterministic breakdown, `tests/test_score_rules.py` (13) rule chain, `tests/test_score_personas.py` (6), `tests/test_score_matching.py` (8), `tests/test_score_autotune.py` (4); tier changes appear in the audit log (audited writes). |
| M6 | People discovery, seniority, paid gate | Gate never fires for non-Tier-1; budget enforced; non-login scraper works with no account; login adapters refuse when disabled | `tests/test_paid_gate.py` (7); `tests/test_people_discovery.py` (4); `tests/test_people_seniority.py` (5); `tests/test_linkedin_public.py` (5) fixture profiles, no account; `tests/test_contacts_patterns.py` (6). |
| M7 | Shipment sources, forecasting | Forecast outputs window, qty range, value range, confidence on fixture data | `tests/test_forecast.py` (12); `tests/test_shipments_base.py` (5) ShipmentSource contract, `tests/test_shipments_us_bol.py` (7), `tests/test_shipments_csv.py` (7); `tests/test_score_m7_wireins.py` (6) scoring wire-ins. |
| M8 | CRM, scheduler, UI, export | All section-1 filters work; scheduler survives restart and runs missed jobs; workbook opens with all sheets | `tests/test_crm.py` (17) outreach/next-action/samples/quotes/channels/content; `tests/test_sched.py` (10) restart catch-up with coalescing within the grace window; `tests/test_export.py` (2) all sheets incl. Changelog from the audit log; `tests/test_cli.py` (3). Companies page implements every section-1/18 filter. |

## Live verification (2026-11-16, HEAD `af8dc46`)

- `uv run pytest` — **327 passed**.
- Full CLI smoke of every M8 command (jobs, run, catchup, outreach-*, sample-*, quote-log,
  channel-test, content-*, export) against the live DB.
- Streamlit AppTest ran all 11 UI pages with zero script exceptions.
- Live DB left clean: 0 smoke companies/persons/events; 8 seeded schedules and 6 real
  FX market signals kept; `data/` workbooks gitignored.

## Standing limitations (by design, not defects)

- LLM providers are no-key: routers degrade gracefully; nothing blocks.
- Email infrastructure, CAPTCHA solving and the Colab runner stay OFF (spec 21).
- Tor egress is banned; `direct` is the default network profile (owner decision 2026-11-14).
- Login-based LinkedIn adapters exist but refuse to run when disabled (ADD-012).
- The system logs and recommends — it never sends or posts anything (spec 16).

# Calendula Buyer Intel

Personal buyer and market intelligence system for **Calendula Herbs Group**
(Egyptian exporter of herbs, spices, herbal teas, seeds and botanicals).

Single user, local-first, free-sourced. English only. No always-on server.

## What it does

1. Discovers companies that are real or likely buyers from many free sources.
2. Classifies and scores them into tiers so the best-fit companies get the most effort.
3. Finds their decision-makers and every available contact channel.
4. Lets the user filter by country, company, tags, seniority, contact availability, last outreach.
5. Collects market intelligence: trends, demand, Egypt exports, destination imports, shipments.
6. Matches buyers to the 42-product catalog and estimates future demand from shipment data.

## Documentation

| File | Purpose |
| --- | --- |
| `docs/01_SPEC.md` | The full build spec (milestones M0-M8) |
| `docs/02_WHAT_THE_USER_TOLD_ME.md` | Record of the owner's instructions |
| `docs/03_ADDITIONS.md` | Living addendum - overrides the spec on any conflict |
| `docs/DECISIONS.md` | Log of every override and deviation |
| `docs/Calendula Herbs Group Buyer & Purchaser Personas.md` | 15 buyer personas + scoring signals (source of `config/personas.yaml` v2 and `config/weights.yaml` v2) |
| `docs/Calendula Herbs Group — Company Profile & Seller Exporter Persona.md` | Company profile, 42-product catalog, certifications (source of `config/products.yaml`) |

**Read `01_SPEC`, then `02_WHAT_THE_USER_TOLD_ME`, then `03_ADDITIONS` at the start of every session.**

## Setup (Windows)

```powershell
uv sync                      # creates .venv with Python 3.12
uv run pytest                # run the test suite
uv run buyerintel --help     # CLI
```

## Key CLI commands

```powershell
uv run buyerintel init                 # create the DB schema
uv run buyerintel seed-products        # seed the 42-product catalog + HS candidates
uv run buyerintel market-signals       # FX rates + PubMed counts + Google Trends
uv run buyerintel market-comtrade      # Egypt trade flows from UN Comtrade (rate-limited, budgeted)
uv run buyerintel market-comext        # EU27 imports from Egypt via Eurostat Comext
uv run buyerintel market-trademap --file path\to\export.csv   # ITC Trade Map CSV import
uv run buyerintel search-test "herb buyer germany"   # route one query through the search router
uv run buyerintel llm-test "summarize this page"     # one prompt through the LLM router
uv run buyerintel llm-health                         # per-provider health, quotas, cache stats
uv run buyerintel llm-sync-catalog                   # sync free-LLM provider catalog
uv run buyerintel enrich                             # run the S0-S7 enrichment pipeline (resumable)
uv run buyerintel enrich --company-id 3              # enrich one company
uv run buyerintel enrich-status                      # coverage meter per tier
uv run buyerintel score-run                          # rule chain: personas, product fit, score, tier
uv run buyerintel score-run --rescore                # re-score companies already scored
uv run buyerintel score-show 3                       # reproducible score breakdown for company 3
uv run buyerintel score-tune                         # auto-tune weights (no-op until 200 labelled leads)
uv run buyerintel people-list                        # people + seniority + decision-maker flags (M6)
uv run buyerintel people-list --decision-makers      # only decision-makers
uv run buyerintel paid-gate-status                   # S7 Apollo/Hunter gate: budget + eligible companies
uv run buyerintel shipments-import path\to\bol.csv   # generic shipments CSV import (column-mapping yaml)
uv run buyerintel shipments-fetch                    # fetch shipments via the US BOL source (Tier 1)
uv run buyerintel forecast                           # per company x product order forecast (window, qty, value, confidence)
uv run buyerintel serve                # local Streamlit dashboard (Market Intelligence page)
```

## Milestones

| M | Scope | Status |
| --- | --- | --- |
| M0 | Repo, config, DB schema, audit layer, CLI, idempotent import | done |
| M1 | Entity resolution, expo ingestion | done |
| M2 | Product ontology, market module (Comtrade, Comext, Trade Map CSV, pytrends, PubMed, FX), market UI page | done |
| M3 | Search router (engines, SearXNG option, cache, health), multilingual query builder, LLM router (quotas, rotation, fallback, schema validation) | done |
| M4 | Enrichment engine: Scrapling crawler, fetch escalation ladder (12.1), extraction, contact verification, social discovery, OSINT adapters, archive connectors, job-post signals, progressive stages S0 to S4 | done |
| M5 | Personas (15, v2), product matching over the ontology, weighted scoring (weights v2), rule chain (9 rules), tiers, auto-tune (dormant until 200 labelled leads) | done |
| M6 | People discovery: seniority buckets + decision-makers, CrossLinked-style search enumeration, non-login public LinkedIn scraper (ADD-012), email pattern engine (ADD-004), S7 paid-tool gate (Apollo/Hunter, Tier-1-only, credit budget), stages S5-S7 | done |
| M7 | Shipment sources (ShipmentSource interface, US public BOL connector, generic CSV importer), per company x product forecasting (window, quantity range, value range, confidence), M5 scoring wire-ins (shipment frequency/volume, import-record recency) | done |
| M8 | Outreach, scheduler, UI, export | - |

## Security rules

- API keys live in `config/secrets.yaml` or `.env` - both gitignored. Never hardcode or log them.
- `data/` (SQLite, parquet, caches) is local only and gitignored.
- Calendula's own prices and strategy never leave the machine (private LLM class only).

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
uv run buyerintel serve                # local Streamlit dashboard (Market Intelligence page)
```

## Milestones

| M | Scope | Status |
| --- | --- | --- |
| M0 | Repo, config, DB schema, audit layer, CLI, idempotent import | done |
| M1 | Entity resolution, expo ingestion | done |
| M2 | Product ontology, market module (Comtrade, Comext, Trade Map CSV, pytrends, PubMed, FX), market UI page | done |
| M3 | Search router, LLM router | - |
| M4 | Enrichment engine | - |
| M5 | Personas, scoring, tiers, rule chain | - |
| M6 | People discovery | - |
| M7 | Shipment sources, forecasting | - |
| M8 | Outreach, scheduler, UI, export | - |

## Security rules

- API keys live in `config/secrets.yaml` or `.env` - both gitignored. Never hardcode or log them.
- `data/` (SQLite, parquet, caches) is local only and gitignored.
- Calendula's own prices and strategy never leave the machine (private LLM class only).

# Calendula Buyer Intel: Build Spec for opencode

Version 1.0 | Owner: Nour Eldin, Calendula Herbs Group | Single user, local-first, free-sourced

## 0. How to use this spec (instructions for opencode)

- Read this file fully. Then read `docs/02_WHAT_THE_USER_TOLD_ME.md` and `docs/03_ADDITIONS.md` at the start of every session.
- `03_ADDITIONS` overrides this spec on any conflict. Ask first, then Record every override after the answers in `docs/DECISIONS.md`.
- Build milestone by milestone (M0 to M8, section 20). Do not start a milestone until the previous one passes its acceptance tests.
- Everything is config-driven (YAML in `config/`), idempotent, resumable and Windows-compatible (use `pathlib`, no POSIX-only code).
- Write tests with every module. Commit small. Never hardcode or log secrets. Keys live in a gitignored `config/secrets.yaml` or `.env`.
- When a free API, tool or quota differs from what this spec says, trust the live service, update the config, and log the change in `docs/DECISIONS.md`.

## 1. Goal

A personal buyer-intelligence system for Calendula Herbs Group (Egyptian exporter of herbs, spices, herbal teas, seeds, botanicals, medicinal and aromatic plants). It must:

1. Discover companies that are real or likely buyers (importers, distributors, blenders, packers, manufacturers, partners) from many free sources.
2. Classify and score them so the best-fit companies get the most effort (tiers).
3. Find their decision-makers and every available contact channel (work email, phone, LinkedIn, social accounts, location).
4. Let the user filter and organize by country, company, tags, seniority, contact availability, last outreach and more.
5. Collect market intelligence: trends, demand, Egypt exports, destination-country imports, and where possible shipment (bill of lading) data.
6. Match buyers to the company's products and, where shipment data exists, estimate when and how much they will need.

The system is fully automated. There is no human review step. Quality comes from cross-validation, confidence scores and self-correction (section 19).

## 2. Explicit exclusions (do NOT build)

- No sanctions screening. No organic-only SKU restriction. Organic or conventional is an informational tag only.
- No country-specific outreach-law rules, GDPR/PECR flags or suppression lists for now (deferred).
- No Make.com, Zapier or similar. Scheduling is built in (section 17).
- No Arabic or multi-language UI. The UI is English only.
- Optional always-on server hosting. No Railway. Runs on the user's Windows PC (Colab optional for heavy jobs) or any similar solutions.
- No Tridge or Volza manual-import channel, no AI sales assistant for replying to buyers, might using lookalike-from-existing-customers model (personas are used in default).
- No China readiness block. China is a normal active market (the company is listed in CIFER and already trades there).
- Spec-fit (Ph. Eur., ISO, Codex) is an optional display flag and never affects score.
- Do not store or generate fixed price lists for Calendula's own products (section 16).
- Cold-email sending infrastructure is an optional module, off by default (section 21).

## 3. Stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12, managed with `uv` |
| Scraping | Scrapling (plain, stealth and dynamic fetchers, spiders, adaptive selectors), Playwright (bundled), httpx; Botasaurus and Botonomus as optional browser backends behind config flags (addendum 4.9) |
| Challenge handling | Fetch escalation ladder ending in FlareSolverr and an optional CAPTCHA solver, NopeCHA first (addendum 4.8, spec 12.1) |
| Parsing | selectolax, trafilatura, pdfplumber / PyMuPDF for catalog PDFs, openpyxl and pandas for Excel |
| Storage | SQLite (WAL mode) via SQLAlchemy/SQLModel for records; DuckDB + Parquet for trade data and analytics |
| Matching | rapidfuzz, Unidecode, ICU transliteration, tldextract, phonenumbers, email-validator, dnspython, langdetect or lingua |
| Embeddings | BAAI/bge-m3 (local via sentence-transformers if hardware allows, else API) |
| LLM access | OpenAI-compatible client against many free providers through a custom router (section 9) |
| Scheduler | APScheduler with SQLite job store (section 17) |
| UI | Local web dashboard in English (Streamlit or NiceGUI) on localhost |
| Export | openpyxl workbook export (section 18) |
| Tests | pytest with saved HTML/JSON fixtures |

## 4. Repo layout

```
buyerintel/
  config/            engines.yaml providers.yaml weights.yaml personas.yaml languages.yaml
                     expo_maps/*.yaml channels.yaml schedules.yaml secrets.yaml(gitignored)
  data/              sqlite db, parquet, caches, snapshots (gitignored)
  src/buyerintel/
    db/ models migrations audit
    connectors/      expos registries trade osint linkedin jobs news archive shipments
    search/          router engines querygen
    llm/             router providers cache schemas
    enrich/          crawl extract contacts verify social people
    resolve/         entities dedupe merge
    products/        ontology matcher
    score/           personas scorer tuner tiers rules
    market/          flows trends forecast
    crm/             outreach quotes samples content channels
    sched/           jobs runner
    ui/              pages
    export/          workbook
  tests/
  docs/              01_SPEC 02_WHAT_THE_USER_TOLD_ME 03_ADDITIONS DECISIONS.md
```

## 5. Data model (core tables)

Every write goes through an audit layer that records table, row, field, old value, new value, source and timestamp.

| Table | Key columns |
| --- | --- |
| company | id, canonical\_name, legal\_name, domain, country, city, address, registry\_id, vat\_id, lei, type, size\_band, tier, score, state, state\_reason, tags, parent\_company\_id, first\_seen, last\_enriched, confidence |
| company\_alias | company\_id, alias, script, kind (legal, trade, brand, local, translit) |
| source\_record | id, source, expo\_edition\_id, raw\_hash, raw\_json, company\_id, ingested\_at |
| expo, expo\_edition, expo\_appearance | expo name; year; company\_id, edition\_id, booth, listed\_products |
| person | id, company\_id, full\_name, name\_local, title, title\_normalized, seniority, department, is\_decision\_maker, confidence |
| contact\_point | owner\_type, owner\_id, kind (email, phone, whatsapp, linkedin, facebook, instagram, tiktok, wechat, telegram, vk, line, kakao, zalo, form, other), value, source\_url, found\_at, verified\_status, confidence, is\_historical |
| product, product\_name, hs\_candidate | product (name, latin\_name, part, forms, conventional, organic); names by language and kind (common, synonym, latin, trade, misspelling); HS code with confidence |
| product\_match | company\_id, product\_id, score, matched\_term, lang, evidence\_url, evidence\_snippet |
| persona, company\_persona | persona definition and embedding; similarity per company |
| score\_run | company\_id, ts, components\_json, total, tier, weights\_version |
| shipment | source, company\_id, shipper, consignee, date, hs\_code, product\_desc, qty, unit, value\_usd, origin, dest, raw\_json |
| trade\_flow | reporter, partner, hs, period, value\_usd, qty, unit\_value |
| archive\_snapshot | url, timestamp, source, content\_hash, extracted\_json |
| outreach\_event | company\_id, person\_id, channel, direction, ts, subject, outcome, notes |
| quote, sample | quote (products, qty, incoterm, price, currency, date, status); sample (status, shipping terms) |
| content\_item, content\_suggestion | draft posts; per-person send or skip decision with reasons |
| job, job\_run | schedule definitions and run history |
| search\_cache, llm\_call\_log, audit\_log | caches and logs |

## 6. Progressive enrichment pipeline

The tiers decide effort. Every company enters at the cheap stage and goes deeper only if promising.

| Stage | Work | Applies to |
| --- | --- | --- |
| S0 | Ingest, normalize, dedupe | all |
| S1 | Cheap pre-score from name, domain, segment keywords, country, expo type | all |
| S2 | Website crawl and LLM-assisted extraction (what they sell and buy, certifications, tags, contacts) | pre-score above threshold |
| S3 | Multi-engine search passes in local languages | Tier 3 and up |
| S4 | Registry, social profiles, job-post signals | Tier 2 and up |
| S5 | People and decision-maker discovery | Tier 2 and up (deep for Tier 1) |
| S6 | Archive history, shipment data, news | Tier 1 |
| S7 | Paid-tool gate (Apollo, Hunter) | Tier 1 only, section 12 |

Tier definitions: Tier 1 is direct importers and buyers of herbs and spices. Tier 2 is manufacturers and distributors using herbs and spices as inputs. Tier 3 is the general food, trade-show and weaker-fit pool. The scorer assigns tiers; they are never hand-labelled. The pipeline is a resumable queue with a coverage meter per tier (enriched / total). It must never silently sample. Work is checkpointed per company.

## 7. Connectors

Each connector implements `fetch() -> raw records`, `normalize() -> company/person/contact/shipment rows`, and declares its source name, rate limit, cache policy and confidence level. Prefer official APIs and bulk downloads over scraping.

| Group | Sources |
| --- | --- |
| Expos | Anuga, Fi Europe, Fine Food Australia, World Food Istanbul, SupplySide West, Prodexpo, FOODEX Japan, BIOFACH, plus a generic exhibitor-list mapper (Excel, CSV, HTML directory, PDF) |
| Registries | Wikidata (SPARQL), GLEIF, Companies House (UK), Sirene (France), OpenCorporates (limited), national registries as added |
| Trade statistics | UN Comtrade API, Eurostat Comext bulk, ITC Trade Map (user-downloaded CSV import), USDA GATS, FAO |
| Signals | Google Trends (pytrends), PubMed E-utilities (publication counts per herb), GDELT news, Frankfurter FX (ECB), job posts (JobSpy: LinkedIn, Indeed, Glassdoor, Bayt, Naukri and others) |
| OSINT | theHarvester, SpiderFoot, web-check style profiling, Sherlock / Maigret / social-analyzer (company brand handles only), PhoneInfoga (phone validation), Instaloader (public IG activity), SingleFile / ArchiveBox (evidence snapshots) |
| Archive | Wayback Machine CDX API, Common Crawl index, archive.today, gau / waybackurls-style URL listing, waybackpy |
| Shipments | `ShipmentSource` interface (section 15), US public bill-of-lading connector subject to the site's terms, CSV importer |
| Directory of tools | awesome-osint list (jivoi) is read by the agent when a new source type is needed |

Skip: twint (not-abandoned, need a reconsideration), holehe (it triggers password-reset flows on other people's accounts).

Polite crawling defaults: per-domain rate limits, jitter, exponential backoff, response caching, a clear user agent.

## 8. Search engine router

A router chooses engines by target country and query type. SearXNG (self-hosted, optional) can aggregate engines behind one interface. Every engine has a health score; repeated blocks or captchas disable it temporarily and the router falls back. All results are cached in `search_cache`. When every fallback engine is also blocked, the router retries the first engine through FlareSolverr, then the CAPTCHA solver (public result pages only, addendum 4.8), before marking the engine unhealthy.

| Engine | Best for | Handling |
| --- | --- | --- |
| Google (country domains, hl/gl/lr params) | operators (`site:`, `filetype:`, `intitle:`, `inurl:`) | official Programmable Search free quota first, browser fallback, heavy jitter |
| Bing, Yahoo | redundancy | Bing's official API was retired as far as known, so use HTML via SearXNG or fallback fetch; Yahoo mostly mirrors Bing |
| DuckDuckGo | fast general passes | `ddgs` library, light rate |
| Brave | independent index | official API, check current free quota |
| Yandex | Russia, CIS, Turkey | Cyrillic queries, captcha backoff |
| Baidu, Sogou, 360 | China | Simplified Chinese queries, follow redirect links |
| Naver, Daum/Kakao | Korea | Hangul queries |
| Yahoo Japan | Japan | Japanese queries |
| Seznam, Qwant, Mojeek, Startpage | Czech, EU, independent, Google proxy | niche coverage and fallback |

Query generation (`search/querygen`):

- Company name variants: legal, trade, brand, local-script, transliterations, name plus city, domain only.
- Role queries in local language, for example Einkauf, achats, compras, acquisti, inkoop, 采购, 調達, закупки, satın alma, مشتريات, plus English.
- Product queries from the ontology in each target language.
- Patterns: `{name} {country} contact`, `{name} purchasing`, `site:linkedin.com/in {title} {company}`, `{name} email`, filetype:pdf catalog queries.
- Translation: Argos Translate or LibreTranslate offline first, LLM second.
- Search company names in their own language and script as well as English, because many companies publish only in their mother language.

## 9. LLM router

Use the OpenAI-compatible client with a custom router that tracks per-provider quotas (RPM, RPD, TPD), health and latency; rotates multiple keys per provider (carry over the existing Gemini key-rotation pattern); caches by prompt hash; validates JSON against schemas with retries; and falls back along a chain. Sync the provider catalog periodically from the `data.json` shipped in the mnfst/awesome-free-llm-apis repo. The table below is a seed; the live service wins.

| Provider | Base URL | Notes | Data class |
| --- | --- | --- | --- |
| Groq | https://api.groq.com/openai/v1 | gpt-oss-120b / 20b; about 30 RPM, 1,000 RPD, 200K TPD | public |
| Google Gemini | https://generativelanguage.googleapis.com/v1beta/openai/ | Flash and Flash-Lite, 1M context; free-tier prompts may be used by Google | public |
| Cloudflare Workers AI | https://api.cloudflare.com/client/v4/accounts/{account\_id}/ai/v1 | 10K neurons/day shared | public |
| Gonka Broker | https://proxy.gonkabroker.com/v1 | about 1M tokens/month; includes BGE-M3 embeddings | public |
| Z AI | https://api.z.ai/api/paas/v4 | GLM-4.7-Flash, 1 concurrent request | public |
| OpenRouter | https://openrouter.ai/api/v1 | free models, about 20 RPM and 50 RPD in total | emergency fallback |
| OVHcloud AI Endpoints | https://oai.endpoints.kepler.ai.cloud.ovh.net/v1 | anonymous, 2 RPM per IP per model | emergency fallback |
| Kilo Code | https://api.kilo.ai/api/gateway | about 200 requests/hour/IP, may log prompts | emergency fallback, public only |
| LLM7 | https://api.llm7.io/v1 | about 60 RPM, 250/hour with a free token | emergency fallback |
| Local Ollama | http://localhost:11434/v1 | private, hardware permitting | private |

Excluded by default: Cohere trial (need reconsideration), NVIDIA NIM (trial and prototyping only), ModelScope and SiliconFlow (identity verification).

Data classes: public web content may go to any provider. Calendula's own prices, strategy and private notes go only to private (local) models.

Task routing:

| Task | Preferred providers |
| --- | --- |
| Bulk extraction and classification | Groq, Cloudflare, Gemini Flash-Lite, Gonka, GLM-4.7-Flash |
| Whole-website reading | Gemini (1M context) |
| Multilingual work | Gemini, Qwen via OVH |
| Embeddings | BGE-M3 local or via Gonka / Cloudflare |
| Judging, dedupe adjudication | gpt-oss-120b, Gemini Flash |

Budget rule: free quotas allow a few thousand calls a day. Use rules and parsers first (trafilatura, selectolax, regex, libphonenumber). Call an LLM only for ambiguous content. Checkpoint every job so a quota stop never loses work.

## 10. Product ontology and matching

Source: the 42-product catalog (136 SKUs, conventional and organic forms) plus botanical names and parts used.

- For each product store common names, synonyms, Latin names, trade names, parts, forms, misspellings and HS candidates with confidence. Examples of synonym families: hibiscus / karkade / roselle / sorrel; nigella / black seed / black cumin / kalonji; fenugreek / methi; liquorice / licorice; Jew's mallow / molokhia; coriander / cilantro.
- Multilingual names for every language in `config/languages.yaml` (seeded with the Calendula website languages, about 16, user to confirm). Build them from Wikidata taxon labels, UN HS descriptions and LLM generation, then validate.
- Matching combines: normalized token and n-gram match over all names and languages; embedding similarity; LLM verification only for borderline cases. Output `product_match` with the matched term, language and evidence snippet.
- HS validation: each product has a primary HS code with a confidence score. Trade flows and shipments touching low-confidence codes (for example liquorice, nigella) are flagged, not trusted blindly.
- Spec-fit: if a buyer states a standard (Ph. Eur., ISO, Codex, cut size) and the SKU data meets or misses it, show an optional flag. It has no effect on scoring.

## 11. Entity resolution, expo ingestion and archives

The same company can appear in several expos and sources, and under different names. Duplicates must be prevented at ingestion and cleaned continuously.

Match keys in priority order: normalized domain (strip www, follow redirects), registry / VAT / LEI, phone, social profile URLs, normalized name (legal suffixes such as GmbH, Ltd, S.A., B.V., SRL, LLC stripped; Unicode NFKC; transliterated), then address. Use blocking (country, first name token, phonetic key) so matching scales.

Decision rules (configurable):

- Auto-merge when any hard key matches, or when name similarity is at least 0.93 with the same country and one corroborating signal (domain, phone or address similarity of at least 0.85).
- Borderline range 0.80 to 0.93: an LLM adjudicator compares both records and returns merge, link or distinct with a reason.
- Never merge when domains and countries both differ unless a registry ID matches.
- Subsidiaries, brands and groups become linked records (`parent_company_id`), not merges.
- Survivorship: prefer verified, then most recent, then most complete values. All source values stay in provenance.

Expo ingestion: one YAML mapping per expo (column names, languages, cleanups). Ingestion is idempotent via `raw_hash`. Each row creates a `source_record` and an `expo_appearance`. Appearing at several expos or editions is stored as a positive activity signal.

Person dedupe: name plus company plus email or profile URL.

Archives (`connectors/archive`):

- Wayback CDX API for every known domain: list snapshots, fetch the most useful ones (home, contact, team, products, catalog PDFs).
- Common Crawl index and archive.today as fallbacks. URL listing in the style of gau / waybackurls.
- Past expo exhibitor directories by edition year.
- Contacts found only in old snapshots are stored with `is_historical = true` and lower confidence.
- Snapshot diffs over time can reveal new product, sourcing or supplier pages.
- Evidence copies of key pages are saved (SingleFile or ArchiveBox style).

## 12. Enrichment and contact discovery

1. Website crawl (Scrapling): home, about, contact, team, products, catalog, sourcing / suppliers pages and sitemap. Detect page language and keep original-language text.

    **12.1 Fetch escalation ladder (per URL):** direct `httpx` (cached, rate-limited) -> Scrapling stealth -> Scrapling dynamic / Playwright -> Botasaurus or Botonomus browser session -> FlareSolverr adapter -> CAPTCHA solver (NopeCHA first). A rung runs only after the previous one returns a block, 403 or challenge page; every success is cached. Rungs 4-6 are config-flagged and off by default (addendum 4.8). Solvers and FlareSolverr never touch logged-in sessions, login adapters, or API calls carrying keys.
2. Extraction: structured JSON (what they sell and buy, segments, certifications, organic / conventional / other tags, locations, people with titles, contacts), validated against a schema.
3. Tags: organic, conventional, fair trade, halal, kosher, private label and similar, from site text. Informational only.
4. Contact verification: email syntax and MX checks, role-address detection, pattern inference (first.last@domain and variants), phone parsing with libphonenumber, line-type via PhoneInfoga, status `published`, `pattern-guessed` or `verified`.
5. Social discovery: brand handles on Facebook, Instagram, LinkedIn company pages, TikTok, YouTube, X, WeChat, Telegram, VK and others, via search plus Sherlock / Maigret / social-analyzer on company handles and the employees. Record last-post date as an activity signal.
6. Seniority classification of titles into: Owner / C-level, VP / Director, Head / Manager, Buyer / Specialist, Other. Mark decision-makers using the persona's target titles (purchasing, procurement, sourcing, raw-materials buyer, category manager herbs and spices, QA / R&D for technical buying, owner for small firms) in local languages.
7. People discovery order: (a) company site team pages, (b) CrossLinked-style employee enumeration from search engines (no login), (c) JobSpy-style job-post signals, (d) optional login-based LinkedIn adapters.
8. LinkedIn: the non-login public-profile scraper (`people/linkedin_public.py`, addendum 4.10) is the default path for person enrichment and runs for all tiers with per-domain pacing, no account required. Login-based adapters (StaffSpy, linkedin\_scraper, linkedin-mcp-server style) are disabled by default (`linkedin.enabled: false`); if enabled: secondary account only, never the user's personal profile, Tier 1 only, hard daily caps, slow pacing.
9. Paid-tool gate (S7): Apollo and Hunter adapters exist but run only for Tier 1 companies with score above `paid_gate.min_score` that still lack a key contact after all free methods, within a monthly credit budget in config.

## 13. Personas, scoring and tiers

Personas (`config/personas.yaml`), generated from the business model and refined from outcomes:

- P1 Bulk importer / distributor
- P2 Tea and infusion blender / packer
- P3 Food manufacturer (seasonings, sauces, snacks, bakery)
- P4 Supplement / nutraceutical / botanical-extract maker
- P5 Pharma / herbal-medicine manufacturer
- P6 Private-label brand or retail buyer
- P7 Pet food and feed
- P8 Cosmetics and natural care botanicals

Each persona has: industry codes (NACE, NAICS, SIC), multilingual keywords, target titles by language, typical order size, product mix. Persona match is cosine similarity between the persona text embedding and the company profile embedding.

Score (0 to 100), weights in `config/weights.yaml`:

| Criterion | Weight |
| --- | --- |
| Product-demand fit (buys herbs, spices, tea, seeds, botanicals as inputs; overlap with the 42 products) | 30 |
| Role in the chain (importer, distributor, blender, packer, manufacturer above retailer) | 15 |
| Scale versus MOQ of 500 to 1,000 kg (size, facilities, shipment volumes, ability to buy that volume) | 15 |
| Active buying evidence (shipments, expo exhibiting as buyer, sourcing pages, purchasing job posts, news) | 15 |
| Persona match | 10 |
| Reachability (decision-maker found, contact quality and channels) | 10 |
| Market bonus (destination demand for Egyptian origin, existing Egypt-origin imports) | 5 |

Tier thresholds (configurable): Tier 1 needs total of at least 70 and product fit of at least 18 of 30. Tier 2 is 45 to 69. Tier 3 is below 45. Store the full component breakdown in `score_run` and show it in the UI.

Auto-tuning: once at least 200 outcome-labelled leads exist (reply, sample requested, quote, order), fit a regularized logistic regression on component scores. Blend new weights 50/50 with the prior and bound each change to plus or minus 40 percent. Version every weight set.

## 14. Rule chain (state machine)

Each company passes ordered rules and ends in a state: `active`, `needs_enrichment`, `parked(reason)` or `excluded(reason)`.

1. Dedupe and merge.
2. Liveness check (website responds, registry active, recent social activity). Defunct leads become `excluded(defunct)`.
3. Competitor check. Exporters and suppliers of the same products become `excluded(competitor)` and are kept in a competitor-intel list.
4. Persona match.
5. Product fit.
6. Scoring and tier assignment (all countries treated alike, including China).
7. Enrichment depth by tier, including the paid-tool gate.
8. Contact confidence check. Low confidence becomes `needs_enrichment` and is retried automatically.
9. Final state written, with reason, to the audit log.

Rules live in `score/rules.py` as small, ordered, individually testable functions so new rules can be added from `03_ADDITIONS`.

## 15. Market intelligence, forecasting and shipment data

Market module (scheduled):

- Comtrade, Comext, Trade Map and USDA GATS flows for the product HS list: Egypt exports, destination imports, Egypt share, mirror statistics, unit values, seasonality index per HS and destination.
- Demand signals: Google Trends, PubMed publication counts per herb, GDELT news, FX rates.
- Output: country demand ranking, product trend scores, monthly market digest, and the market bonus used in scoring.

Forecasting (per importer and product, from `shipment`):

- Last shipment date, median interval, quantity range, seasonality adjustment from market data.
- Expected next order window = last shipment + median interval, adjusted for seasonality. Estimated value range = quantity range x benchmark unit value x current FX.
- Confidence is low when fewer than 3 shipments back it up. Show confidence everywhere.

`ShipmentSource` interface: `list_companies()`, `fetch_shipments(company)`, `normalize()`, plus a column-mapping YAML. Initial implementations: US public bill-of-lading connector (respect the site's terms; fall back to CSV import) and a generic CSV importer. The user may later find sources for other countries; they plug in through this interface without redesign. Forecasting activates per country when coverage passes a configurable threshold. Until then use proxy signals (trade statistics, expo activity, job posts).

## 16. Outreach tracking, samples, quotes, channels and content

The system logs and recommends. It does not send messages in core.

- `outreach_event` logs every contact the user makes (channel, date, outcome). Filters such as 'never contacted' and 'last reached out' use it.
- Next-action engine suggests actions per company. 'Offer sample' uses fixed terms text: the sample is free; the buyer pays all shipping costs (freight collect or their courier account); sterilization is performed at a third-party facility and paid by the buyer on request.
- Quote tracker records each quote with date, currency, amount and incoterm. Calendula's own prices are decided by the user and change often, so there is no stored price list. Trade-data unit values appear only as a reference benchmark.
- Channel recommender: per-country priors in `config/channels.yaml` covering email, phone and WhatsApp, contact forms, LinkedIn, Facebook, Instagram, TikTok, WeChat, Telegram / VK, LINE, KakaoTalk, Zalo, B2B marketplaces and RFQ boards, expo matchmaking, trade associations. Priors are re-learned from reply rates. Example prior: buyers in Indonesia, the Philippines, Bangladesh and Vietnam lean toward Facebook over LinkedIn.
- Outreach language: templates for each language in `languages.yaml`.
- Content queue: when a herb or segment trends, draft a B2B-framed post (extract yield, shelf-life stability, constituent density; no consumer medical claims) with an image brief. Nothing is posted automatically. The user posts manually. A suggestion engine lists which contacts to share each item with and which to skip, with reasons (role, persona, product fit, past engagement, recency of last contact).

## 17. Scheduler

Built in, no external service.

- APScheduler with a SQLite job store. Jobs have cron expressions, params, retries, pause and resume, and run history.
- Misfire handling: if the PC was off, missed runs execute on startup within a grace window, with coalescing.
- Optional import of schedules from a CSV or spreadsheet. Optional Windows Task Scheduler launcher that starts the app headless.
- CLI: `buyerintel run <job>`, `buyerintel jobs list`, `buyerintel serve`.
- Typical jobs: expo refresh, market data refresh, enrichment queue, archive sweeps, dedupe sweep, scoring recompute, content suggestions, forecast refresh.

## 18. UI (English, single user, localhost)

Pages: Dashboard (coverage meters, tier counts, state counts), Companies (filters: country, company, tier, state, persona, product, tags including organic / conventional, seniority, contact availability by channel, last outreach, source / expo), Company detail (profile, evidence, score breakdown, contacts, timeline, shipments, archive snapshots), People, Market Intelligence, Shipments and Forecast, Outreach and Quotes, Content Queue, Discovery Console (run a search or playbook), Jobs and Scheduler, Settings (providers, engines, weights, personas, products, languages), Exports.

Export: one click generates a multi-sheet Excel workbook: summary dashboard, one sheet per tier, per-persona sheets, contact details, market digest, and a changelog sheet generated from the audit log.

## 19. Quality control (replaces human review)

- Corroboration: a field is high-confidence only when two independent sources agree. Single-source fields are tagged with their confidence.
- LLM-judge pass: a second model checks each extraction against the page text. Low-confidence extractions are re-run on a different model.
- Contact verification as in section 12. A bounce or invalid-address signal downgrades or quarantines that contact automatically.
- Confidence floor: data below the floor is never used for outreach suggestions and is retried later.
- Drift monitors: alert on the dashboard when a connector's yield drops or its output shape changes.
- Test data: golden dedupe pairs, saved HTML fixtures per connector, and per-milestone acceptance tests (section 20).
- Observability: every LLM call, search and crawl is logged with provider, tokens, outcome and latency.

## 20. Milestones and acceptance criteria

| M | Scope | Acceptance |
| --- | --- | --- |
| M0 | Repo, config system, DB schema and migrations, audit layer, CLI. Import the user's existing lead workbook(s) via a mapping file | Re-running the import creates zero duplicates; every change appears in the audit log |
| M1 | Entity resolution engine, expo ingestion with YAML maps for all listed expos and a generic mapper, `expo_appearance` | At least 0.98 precision and 0.90 recall on the golden dedupe set; re-ingesting an expo file is a no-op |
| M2 | Product ontology (names, Latin names, multilingual, HS candidates), market module with Comtrade / Comext / Trade Map import / pytrends / PubMed / FX, market page | Ontology resolves common and Latin names to the right product on a fixture set; market page shows Egypt exports and destination imports per HS |
| M3 | Search router (engines, SearXNG option, cache, health), multilingual query builder, LLM router (quotas, rotation, fallback, schema validation) | Simulated engine and provider failures trigger fallback without job loss; cache hit rate logged |
| M4 | Enrichment engine: Scrapling crawler, fetch escalation ladder (12.1), extraction, contact verification, social discovery, OSINT adapters, archive connectors, job-post signals, progressive stages S0 to S4 | Fixture sites yield expected contacts and tags; the challenge ladder moves to the next rung only when the previous fails and rungs 4-6 respect their config flags; resumable after a forced stop |
| M5 | Personas, scoring, tiers, rule chain, product matching, weight auto-tuning, spec-fit flag | Score breakdown reproducible; rule-chain unit tests pass; tier changes logged |
| M6 | People discovery (CrossLinked-style plus the non-login public-profile scraper, optional login adapters disabled by default), seniority and decision-maker logic, paid-tool gate S7 | Gate never fires for non-Tier-1; credit budget enforced; the non-login scraper enriches fixture profiles with no account involved; login adapters refuse to run when disabled |
| M7 | Shipment sources (`ShipmentSource`, US connector, CSV importer), forecasting and cost estimates | Forecast outputs window, quantity range, value range and confidence on fixture data |
| M8 | Outreach log, quotes, samples, channel recommender, content queue and suggestions, built-in scheduler, full UI, workbook export | All filters from section 1 work; scheduler survives restart and runs missed jobs; export workbook opens with all sheets |

## 21. Optional modules (off by default except Colab)

- **Email infrastructure:** a separate sending domain (not the main brand domain), SPF, DKIM, DMARC, slow warm-up with low volumes, and a check of the sending provider's cold-outreach policy before enabling. Amazon SES in particular can suspend accounts for unsolicited mail.
- **CAPTCHA solving:** NopeCHA API key (free tier) or a local solver from addendum 4.8. Enabled only when the escalation ladder (12.1) reaches the last rung, with daily call caps and public pages only.
- **Local LLM:** Ollama model for private-data tasks and bulk work when hardware allows.
- **Colab runner:** run heavy batch jobs from a config file in Drive, with outputs written back to the app's data folder.

# Additions for opencode (living addendum)

Project: Calendula BuyerIntel | Owner: Nour Eldin | Last updated: 2026-11-14

This is the file where new requests, tools and decisions are added over time. It is newer than the build spec (`01_SPEC`) and the record of what the user said (`02_WHAT_THE_USER_TOLD_ME`). Where they conflict, this file wins.

## 1. How opencode uses this file

- Read it at the start of every session, after the spec.
- For each entry in section 3 or section 4, apply the stated decision, update the code and config, and log any deviation in `docs/DECISIONS.md`.
- Entries marked Reviewed were read and understood when this file was written. Entries marked Not reviewed were only identified by name or short description. For those, read the repository README and license first, then confirm or change the decision and log it.
- Do not delete entries. Mark them Done, Changed or Dropped, and keep a one-line reason.
- Never put API keys or passwords in this file. Secrets go in `config/secrets.yaml`.

## 2. Standing rules for any third-party repository or tool

1. Read the README and license first. Check for recent activity. If it looks abandoned, skip it or find a maintained alternative.
2. Run it as a separate process or container with a thin adapter. Personal local use is fine.
3. Pin a version. Wrap it behind a config flag so it can be switched off.
4. The system finds companies and business contact points. if necessary adopt any tool whose purpose is to profile, locate or track a private individual (personal accounts, device activity, physical location).
5. Respect data classes from the spec: public web content may go to free LLM providers; the company's own prices and strategy stay local.

## 3. Change log

| ID | Date | Type | Area | Change | Status |
| --- | --- | --- | --- | --- | --- |
| ADD-001 | 2026-11-14 | Add | Network | Optional egress module with Tor adapter (off by default) | Open |
| ADD-002 | 2026-11-14 | Add | CRM | CrmSink interface for optional one-way sync to an external CRM | Open |
| ADD-003 | 2026-11-14 | Add | Discovery | Google Maps connector (`gmaps`) producing company seeds | Open |
| ADD-004 | 2026-11-14 | Add | Contacts | Email pattern engine (`contacts/patterns.py`) | Open |
| ADD-005 | 2026-11-14 | Add | Phones | Chinese mobile number extractor (`phones/cn.py`) plus validation policy | Open |
| ADD-006 | 2026-11-14 | Add | Schema | Falsehoods checklist applied to schema and tests in M0 | Open |
| ADD-007 | 2026-11-14 | Add | Scoring | Optional active-learning lead scorer (Bayesian or Gaussian-process) alongside the regression tuner | Open |
| ADD-008 | 2026-11-14 | Add | Discovery | Query generation from product description and persona (borrowed idea) | Open |
| ADD-009 | 2026-11-14 | Add | Skills | Package playbooks as Claude skills after M8 (optional) | Open |
| ADD-010 | 2026-11-14 | Add | Scraping | FlareSolverr adapter for challenge pages (Cloudflare-style blocks) | Open |
| ADD-011 | 2026-11-14 | Add | Scraping | CAPTCHA solving capability (NopeCHA first; ai-captcha-bypass and FuckCaptcha as fallbacks) for public pages | Open |
| ADD-012 | 2026-11-14 | Add | People | Non-login LinkedIn profile-details scraper (ToddNieb / Bitbash) | Open |
| ADD-013 | 2026-11-14 | Add | Scraping | Botonomus browser automation SDK as an optional fetch backend | Open |
| ADD-014 | 2026-11-14 | Add | Scraping | Botasaurus framework as a fetch backend (also rides along with the gmaps connector) | Open |
| ADD-015 | 2026-11-14 | Confirm | Scraping | Scrapling confirmed as the core scraping library (already in the spec) | Done |
| ADD-016 | 2026-11-14 | Add | Network | CloudPeler / CrimeFlare real-IP lookup for Cloudflare-hosted domains | Open |

## 4. Tool and repository integrations

### 4.1 Network egress (Tor)

| Repository | What it is | Decision |
| --- | --- | --- |
| dperson/torproxy | Reviewed. A Docker image running Tor plus Privoxy. Gives a SOCKS proxy on port 9050 and an HTTP proxy on 8118. Relay-only by default (not an exit node). Environment variables include `LOCATION` (exit country selection), `TOR_NewCircuitPeriod` and a control-port password. AGPL-3.0 licensed. | Reference setup for a Dockerized Tor egress, might be used when it's required |
| mstfknn/tor-proxy | Not reviewed. Identified as a Tor proxy tool | Read README, compare, might be used when it's required |
| fairhive-labs/tor-proxy | Not reviewed. Identified as a Tor proxy tool | Read README, compare, might be used when it's required |
| TNLegend/Portable-Tor-Proxy-Rotator | Not reviewed. By its name, a portable Tor proxy that rotates identities, which may suit a Windows PC without Docker | Try first if Docker is not installed, might be used when it's required |

Build a small `egress` module with three modes: `direct` , `proxy_list` and `tor`(default). Each connector declares which mode it allows.

- Tor mode is decided to be used by necessity and not only for low-volume fetches of search-engine results or public pages when the direct IP is being rate-limited.
- Never route logged-in sessions, LinkedIn adapters, email verification, or API calls that carry keys through Tor.
- Expect trouble: Google and many sites block or captcha Tor exit nodes ( you may use the solutions in similar situations from scrapling captcha bypassing tools or listed related github repos), and Tor is slow. Treat it as a fallback behind direct requests with polite rate limits, caching and engine switching (spec section 8).
- Rotate circuits through the Tor control port (new identity signal) when blocked, with a minimum interval.  try first to run an exit node, if failed. Keep traffic light, because Tor is a volunteer network.
- Add an egress health check that logs exit country and success rate per mode.

### 4.2 CRM platforms

The system keeps its own database and dashboard, so no CRM replaces it. Implement a `CrmSink` interface in `crm/sinks/` that pushes companies, people, contact points, tier, score and outreach events to an external CRM through its API, one way by default. Choose the first target at M8.

| Repository | What it is | Decision |
| --- | --- | --- |
| espocrm/espocrm | Not reviewed in detail. Self-hosted PHP CRM with a REST API | Recommended first sink candidate (lightweight, API-friendly) |
| frappe/crm | Not reviewed in detail. Modern CRM on the Frappe framework (Python) with an API | Recommended first sink candidate (same language as this project) |
| suitecrm/suitecrm | Not reviewed in detail. Large, full-featured PHP CRM | Optional. Heavier setup; only if the user wants a full CRM |
| nocobase/nocobase | Not reviewed in detail. No-code / low-code data platform | Optional. Could serve as an alternative admin view over the same database |
| relaticle/relaticle | Not reviewed | Evaluate at M8 against the criteria below |
| nash-md/meow | Not reviewed | Evaluate at M8 against the criteria below |
| melgarafael/DeskcommCRM | Not reviewed | Evaluate at M8 against the criteria below |
| ethanplusai/sellbird | Reviewed from published descriptions. Open-source outreach platform (formerly Harvey) that runs on a Claude or ChatGPT plan: finds companies and decision-makers, verifies emails, drafts and sends cold email and handles replies. Python, local dashboard. A third-party skill scanner listed warnings about some of its files | Reference only. Borrow the discover, verify, draft workflow ideas. Do not run its sending or auto-reply. Review its code before running anything |
| eracle/OpenOutreach | Reviewed from published descriptions. GPL-licensed LinkedIn automation for B2B lead generation: browser automation (Playwright), built-in CRM (Django), an LLM generates search queries and qualifies profiles, a Gaussian-process model updates lead scores from outcomes, optional paid email-finding channel. Split into OpenOutFind (discovery, qualification, enrichment, CRM) and OpenOutSend (outreach) | Adopt ideas ADD-007 and ADD-008. Do not use its LinkedIn actions (connection requests, messaging): they break LinkedIn's terms and risk the account. OpenOutFind may be an optional discovery adapter run separately with a secondary account |

Evaluation criteria for unreviewed CRMs: self-host on Windows or Docker, REST API with custom fields, active maintenance, license, and how much setup it needs for one user.

ADD-007 detail: keep the logistic-regression tuner as the default. Add an optional model that updates lead probability as outcomes arrive (Bayesian logistic or Gaussian-process), and compare both on held-out outcomes. Switch only if it is clearly better.

ADD-008 detail: from the product catalog and each persona, have the LLM generate discovery queries (search-engine queries, Google Maps queries, expo categories, job-post keywords) in the target languages. Store which queries produced good leads and prefer them next time.

### 4.3 Discovery and skills

| Repository | What it is | Decision |
| --- | --- | --- |
| omkarcloud/google-maps-scraper | Reviewed. MIT-licensed Python scraper built on Botasaurus with a local dashboard. Extracts business listings with 50 or more fields (name, address, website, phone, category, ratings and more). Email and social enrichment uses their freemium API with a limited free allowance (check current terms) | Adopt as a `gmaps` connector (ADD-003) |
| gosom/google-maps-scraper | Reviewed from published descriptions (not in the user's list). MIT-licensed Go command-line alternative that can optionally extract emails from business websites | Alternative to the above if easier on Windows |
| cporter202/social-media-scraping-apis | Not reviewed. By its name, a list of social-media scraping APIs | Reference list. Pick optional social adapters from it, each with a monthly cost cap and a free-tier check |
| OneWave-AI/claude-skills | Not reviewed. A collection of Claude skills | Reference for skill structure. After M8, package this system's playbooks as skills (ADD-009) |

`gmaps` connector (ADD-003): queries per country and language (for example spice importer, herbal tea packer, seasoning manufacturer, botanical ingredients distributor, in the local language). Output is company seeds (name, address, phone, website, category, coordinates) that enter the normal pipeline at S0. The system's own crawler does the enrichment, so it does not depend on the scraper's paid enrichment. Keep volume low and cache results. Google Maps terms restrict scraping, so treat it as best-effort with a config flag to turn it off.

### 4.4 Email patterns

| Repository | What it is | Decision |
| --- | --- | --- |
| LavX/tools-docs (email-permutator.md) | Not reviewed. Documentation for an email permutation tool | Reference for permutation lists |
| pratik-dani/email\_permutations | Not reviewed. By its name, an email permutation generator | Reference and test oracle |
| apifyforge/email-pattern-finder | Not reviewed. By its name, an email pattern finder, probably an Apify actor, so likely credit-based | Do not use by default (paid-tool rule). Study the approach |

Build `contacts/patterns.py` (ADD-004):

1. Learn the pattern for a domain from emails already published on it (first.last, flast, f.last, firstl, first, last and so on) and apply it to other people at that domain.
2. If no pattern is known, generate ranked permutations from the person's name. Handle transliteration, single names, multi-part names and non-Latin scripts.
3. Verify with syntax and MX checks, catch-all detection, and a cautious SMTP probe where the server allows it (greylisting and blocking are common). Keep status `pattern-guessed` until corroborated by a published email or a successful verification.
4. Use the permutation lists from the repositories above as test fixtures.

### 4.5 Username, email and social OSINT

| Repository | What it is | Decision |
| --- | --- | --- |
| antoniaci/blackbird | Not reviewed. Username and email search across many sites | Company brand handles and may be used for personal identification |
| kaifcodec/user-scanner | Not reviewed. Username and email scanner across platforms | Company brand handles  and may be used for personal identification; add if it finds regional platforms that Sherlock, Maigret and social-analyzer miss |
| TermuxHackz/X-osint | Not reviewed. A general-purpose OSINT toolkit | may be used. |
| KatrielMoses/MailAccess | Not reviewed. Appears to be email-account tooling (verify) | may be used. |
| HunxByts/GhostTrack | Not reviewed. By its description, tracks the location of an IP address or phone number | may be used. Locating individuals is maybe could help the purpose. Company location comes from addresses, registries and maps data |
| gommzystudio/device-activity-tracker | Reviewed. A proof of concept that infers whether a WhatsApp or Signal user's device is active, idle or offline by timing message delivery receipts for a phone number, without the person knowing | may be used. It is covert surveillance of individuals and might help in finding buyers |

For WhatsApp, the system records a WhatsApp contact whether when the company publishes it (for example a wa.me link or a WhatsApp Business number on its website or profile) or It could probes numbers.

### 4.6 Phone tools

| Repository | What it is | Decision |
| --- | --- | --- |
| sundowndev/phoneinfoga | Already in the spec. Go tool for phone number information | Adopt for validation: country, format, line type. may use it for owner identification |
| VincentSit/ChinaMobilePhoneNumberRegex | Not reviewed. By its name, regular expressions for Chinese mobile numbers | Adopt in `phones/cn.py` (ADD-005) to extract numbers from Chinese pages, then validate with libphonenumber |
| marmelroy/PhoneNumberKit | A Swift library for iOS and macOS built on the same idea as Google's libphonenumber | Reference only. Not usable in this Python stack. Use the Python `phonenumbers` package |
| HackUnderway/SearchPhone | Not reviewed. By its name, a phone number lookup tool | may be used |
| AzizKpln/Gokboru\_Intel | Not reviewed. An OSINT tool | may be used |
| whoamitang/numint | Not reviewed. By its name, a number OSINT tool | may be used |
| phoneintel/phoneintel | Not reviewed. By its name, phone number intelligence | may be used |

Phone policy (ADD-005): store every number as E.164 plus the original string and the region it was parsed with. Validate with libphonenumber, then PhoneInfoga for line type. The system needs validity, country and line type, and the identity of the owner if possible. If opencode reads one of the not-adopted lookup tools and finds a function limited to validation or carrier and line type with no personal data or even with personal data, it may use it as a secondary validator and must log the decision.

### 4.7 Reference list

| Repository | What it is | Decision |
| --- | --- | --- |
| kdeldycke/awesome-falsehood | A curated list of falsehoods programmers believe about names, addresses, phone numbers, emails, time, geography and more | Adopt as a design checklist (ADD-006) |

ADD-006 in M0: review the schema and parsers against the lists and add tests. Examples: a person has a `full_name` with optional given and family parts (do not assume two names, ASCII or one script); addresses are free text with optional structured parts and country-specific postal formats; phones follow the policy above; emails are case-sensitive in theory, may contain plus-addressing and international characters; store time zones for scheduling; currencies differ in decimal places; company names change and have many forms.

### 4.8 Blocking, challenges and CAPTCHAs

Every URL follows one escalation ladder inside the enrichment engine. A rung is tried only after the previous rung fails with a block, 403 or challenge page, and every successful fetch is cached like any other:

1. Direct `httpx` with caching and rate limits.
2. Scrapling stealth fetcher.
3. Scrapling dynamic fetcher / Playwright (bundled).
4. Botasaurus or Botonomus browser session (4.9).
5. FlareSolverr adapter (ADD-010).
6. CAPTCHA solver, last resort (ADD-011).

| Repository | What it is | Decision |
| --- | --- | --- |
| FlareSolverr/FlareSolverr | Reviewed from published descriptions. A headless-browser proxy server with a REST API that solves Cloudflare-style "checking your browser" challenges and returns cookies, user agent and page body. Runs as a separate service (Docker or Windows binary), language-agnostic | Adopt (ADD-010) as `enrich/challenges.py`, rung 5 of the ladder. Only called after rungs 1-4 fail. Its returned cookies and user agent are injected into the `httpx` session for that domain for the TTL it reports. Off by default, config flag `challenges.flaresolverr.enabled`. Build the fixture corpus of challenge pages for tests. |
| NopeCHALLC/nopecha-extension | Reviewed from published descriptions. A browser extension plus a freemium API that solves CAPTCHAs (hCaptcha, reCAPTCHA, text and image captchas, FunCaptcha, Steam). The extension works inside a real browser session; the API is a per-request endpoint with a free tier | Adopt as the preferred solver (ADD-011), rung 6. Through the API: key goes in `config/secrets.yaml`, daily call caps in config, public pages only. Check current free-tier limits before relying on it. Through the extension path: works when the fetch runs inside a real browser (Botasaurus / Botonomus / Playwright headed). |
| aydinnyunus/ai-captcha-bypass | Not reviewed. By its name, an AI-based CAPTCHA bypass tool (Python) | Reference and fallback solver. Read the README and code first; if it runs locally and offline it may be preferred over the extension route, and log the decision. |
| vsmutok/FuckCaptcha | Not reviewed. By its name, a CAPTCHA bypass tool | Reference and fallback solver, same rules as above. |

Ladder rules:

- Solvers and FlareSolverr never receive logged-in sessions, LinkedIn login adapters, or API traffic carrying keys, and never run through egress modes that carry credentials (align with 4.1).
- Search engines: when an engine is captcha-blocked, first rotate engines per spec section 8, then FlareSolverr, then the solver.
- Log which rung solved each domain. A domain that always needs rung 5-6 is flagged for off-peak scheduled handling.
- Keep a saved fixture per challenge type so the ladder is testable offline (extends the spec's HTML fixture policy).

### 4.9 Scraping frameworks and browser automation

| Repository | What it is | Decision |
| --- | --- | --- |
| D4Vinci/Scrapling | Reviewed. The core scraping library of this stack (spec section 3): plain, stealth and dynamic fetchers, spiders, adaptive selectors, plus its own captcha-bypassing helpers | Confirmed (ADD-015, Done). Remains the default for all fetches. The layers below are fallbacks and backends, never replacements. |
| omkarcloud/botasaurus | Reviewed from published descriptions. MIT-licensed Python scraping framework with an anti-bot browser class and an HTTP class; also the framework under the adopted `google-maps-scraper` (4.3), so it is a dependency anyway | Adopt (ADD-014) as a fetch backend behind a config flag. Use it for domains that resist Scrapling and Playwright, and as the browser layer under the gmaps connector. |
| Kuduxaaa/botonomus | Reviewed. MIT-licensed async Python SDK (3.12+, Windows is its most-tested platform) that drives real Chrome over CDP without `Runtime.enable`, applies C++-level fingerprint personas in its own Chromium build, manages authenticated proxies with geo consistency, human-like input, persistent locked profiles, an `httpx`-like client served from one shared browser, and a detection runner | Adopt as the optional advanced backend (ADD-013). Matches the stack exactly (Python 3.12 + uv). Fingerprint personas require the separately licensed Botonomus Chromium build, whose release host is not live yet, so plan around stock Chrome first (`persona="off"`). Use it for high-friction domains and for archive/evidence snapshots that need a real visible browser. Config flag, off by default until benchmarked against Playwright with `botonomus detect`. |

### 4.10 LinkedIn (non-login) and Cloudflare origin lookup

| Repository | What it is | Decision |
| --- | --- | --- |
| ToddNieb/linkedin-profile-details-scraper-email-no-cookies-required | Reviewed. Python project (Bitbash showcase) that extracts public LinkedIn profile data — name, headline, location, email when visible, work history, education, skills, languages, certifications, company metadata — from a profile URL, username or URN with no login and no cookies. The repo doubles as a showcase for the authors' paid service, so review the code and run it as a separate process with a thin adapter | Adopt (ADD-012) as `people/linkedin_public.py` behind a config flag. It is the direct profile-fetcher for the profile URLs that CrossLinked-style search enumeration already finds (spec section 12 step 7b): structured person records, titles for seniority classification, and public emails, with no account involved. Validate output against saved fixtures, keep per-domain pacing, store `source_url` per field, and treat missing email as normal. |
| zidansec/CloudPeler | Reviewed. CrimeFlare, a PHP CLI that reveals the real IP, host and organization behind Cloudflare-protected sites using the crimeflare API and ipinfo. README notes the tool's availability has fluctuated | Optional reference (ADD-016). Use only for a specific domain that FlareSolverr (4.8) cannot pass and only when the user accepts a direct origin fetch. Origin IPs can serve different content than the CDN, so anything fetched that way must be re-verified against the challenge page before use. Low priority. |

## 5. Open slots (the user may fill these in later)

| Slot | Current state | What opencode does when it is filled |
| --- | --- | --- |
| Non-US shipment data source | Will be using the tor proxies and rotations with the websites as import genius and similar websites so it can go after the rate limiting and blocking of IPs and do the work&#32; | Implement a `ShipmentSource` for it with a column-mapping file (spec section 15) and enable forecasting for that country |
| Languages list | Seeded from the website languages, about 16, not yet confirmed | Update `config/languages.yaml`, regenerate product names and templates |
| CRM choice | Deciding based on the best choices after reviewing them and their features alongside their technologies | Implement the chosen `CrmSink` at M8 |
| LinkedIn secondary account | Not provided. Login-based adapters are disabled. The non-login public-profile scraper (4.10) is now the default LinkedIn path for person enrichment; a secondary account is only needed for logged-in search or connect actions, which stay off unless supplied | Enable login adapters with daily caps if the user supplies one |
| Tor egress | Off | Enable per the egress rules in 4.1 |
| Email infrastructure | Off | Build the optional module in spec section 21 |
| SearXNG | Not installed | Enable it in the search router |
| Other additions | None yet | Add as new entries below |

## 6. New additions (template)

Copy this block for each new request.

```
ID: ADD-0xx
Date:
Type: Add | Change | Remove
Area: (search, enrichment, scoring, market, CRM, UI, scheduler, other)
Request: (what the user wants, in plain words)
Links or tools: (repositories, APIs, sites)
Priority: High | Normal | Low
Milestone affected:
Status: Open | Done | Changed | Dropped
Notes for opencode:
```

### Entries added by the user later: integrated on 2026-11-14

The batch of nine repositories listed here was added by the user on 2026-11-14 and has since been considered, edited into the change log (ADD-010 to ADD-016) and sections 4.8-4.10, and reflected in the build spec. Per the standing rule, they are deleted from this staging list:

NopeCHALLC/nopecha-extension, ToddNieb/linkedin-profile-details-scraper-email-no-cookies-required, Kuduxaaa/botonomus, aydinnyunus/ai-captcha-bypass, vsmutok/FuckCaptcha, FlareSolverr/FlareSolverr, omkarcloud/botasaurus, zidansec/CloudPeler, D4Vinci/Scrapling.


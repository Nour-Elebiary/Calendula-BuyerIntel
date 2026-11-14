# What the User Told Me

Project: Calendula BuyerIntel (buyer and market intelligence system for Calendula Herbs Group) User: Nour Eldin. Purpose of this file: a plain record of what the user has said, so opencode and later sessions never have to guess. If this file and `03_ADDITIONS` conflict, `03_ADDITIONS` wins (it is newer).

## 1. The original request

- Build a system for the user's own work to search, retrieve and use content from many sources: search engines, databases, files, archives, OSINT tools, public records, government sites, using tools like Scrapling or similar.
- Targets are companies and their profiles that match the ideal customer persona: buyers, purchasers, importers and partners. Calendula Herbs Group is the seller and exporter.
- It must also find each company's responsible employees, specialists and decision-makers, with work emails, phone numbers, LinkedIn accounts, social media accounts, location and more.
- It must organize and filter the final data by country, company, special tags (organic or conventional), seniority level, availability of contact details (email, phone, LinkedIn, social), last time reached out or emailed, and more.
- It must collect detailed market information for the field: direction and trends, products currently in demand related to the company's products, market analysis, Egypt's exports, and the imports of the countries the company deals with.
- Suggested techniques: public or no-registration trials of import/export websites and publicly available bill of lading sites, to learn which companies in which countries do business in the company's products with Egypt.
- The system should decide which of the company's products may be wanted by which companies, by cross-referencing their product catalogs and services with bill of lading data for them or their related companies. Knowing the timing, amount and type of past purchases, plus related knowledge, it should estimate when and how much they will need, with an estimated cost that takes partners, economic conditions, currency and other factors into account.
- The user has built similar pieces before, separately or in older ways, and wants the modern and effective version.
- It must be free-sourced. It is for the user's own use only.

## 2. Corrections and decisions (first round)

### Priorities and process

- Use the tiers while gathering information so priority goes to worthy, suitable companies. Put more effort into classification and scoring using several methods: scraping websites, public records, social media info, professional info.
- The earlier tier counts (86 / 550 / about 4,950) are irrelevant. Ignore them.
- The tool should be perfect and professional, so no human review is needed.
- The weighted scoring rubric must be adjusted for this system.

### Products, samples and pricing

- Match product names by botanical (Latin) names and also by normal common names.
- Spec-fit matching is an optional flag only and does not affect the score.
- Offering a sample is a great feature. The sample is free, but the buyer pays all shipping-related costs. Sterilization happens at a third-party facility and the buyer pays for it on demand.
- The company's own product prices are decided by the company and are not stable. (Other companies' price lists are a different matter.)
- The company website is now in about 16 languages.

### Data gathering

- Expo exhibitor lists overlap: a company can appear in several expos or editions. The system must avoid duplicates.
- Learn how to find and use old versions of companies' information: Internet Archive, Google searches, previous records on their websites, or similar approaches.
- Use many search engines (Google, Bing, Yahoo, DuckDuckGo, Yandex, Baidu, Brave, Kakao/Naver and others), each in the most effective way, chosen per region (for example Baidu for Chinese companies, Yandex for Russian ones).
- Search with translated meanings of targets and company names in several languages and scripts, because some companies publish only in their mother language. Gathering must be thorough.
- Use the GitHub OSINT topic (github.com/topics/osint), the public-apis repository, free-apis.github.io, and awesome-free-llm-apis for APIs and LLMs.
- Use LinkedIn-scraper tools and repos (github.com/topics/linkedin-scraper, the Scrapfly article on best LinkedIn scrapers on GitHub, and similar).
- Prefer not to use credit-based tools such as Apollo or Hunter until the candidate is top tier and worth it.
- The user may not be able to provide example customers, so build ideal-buyer personas from the available details instead of a lookalike model.
- For markets other than the US, the user will try to find a data source. Add a consideration section for it.

### Things to ignore or drop

- Ignore the Tridge supplier plan and Volza trial manual-import idea.
- Ignore the sales-assistant idea (the AI that drafts replies to buyers).
- No sanctions screening. No organic-only SKU rule.
- China is no longer blocked: the company is listed in CIFER and already does business with China.
- Skip, for now, per-country outreach rules (GDPR/PECR flags) and the contact-data compliance and suppression-list note.
- No Arabic interface. English only. The user is the only user.
- Railway is not relevant to this project.
- Do not depend on Make.com or similar tools. But scheduling must still exist as a built-in personal-use feature.

### Things the user liked or wants changed

- The rule chain is wanted, updated for the new decisions.
- The channel recommender is fine, but it must cover all channels, not only Facebook and LinkedIn.
- Science posts: do not post immediately. Produce content-ready optional reminders so the user can post if they want. Not every contact wants such content, so suggest which contacts to send to and which not.
- The email infrastructure note (separate sending domain, SPF/DKIM/DMARC, warm-up, SES policy check) stays as an optional module.
- Cost-aware design (no always-on scraper server, heavy jobs on schedule from PC, Colab or cron) is a good idea.
- Pricing, forecasting and demand estimates should account for the company's own unstable prices.

## 3. Business facts on record (from the user's earlier work)

- Calendula Herbs Group is an Egyptian wholesale manufacturer and exporter of herbs, spices, herbal teas, seeds, botanicals, and medicinal and aromatic plants.
- Buyers served globally: pharmaceutical, food and beverage, health supplement and herbal tea companies.
- Certifications: ISO 9001, ISO 22000, FDA, BRCGS, USDA NOP Organic, EU Organic, HALAL, KOSHER.
- Catalog: 42 products, 136 SKUs, available conventional and 100 percent organic.
- Minimum order quantity 500 to 1,000 kg. Free samples, sterilization and external lab analysis are offered (with the cost terms above).
- The company is listed in China's CIFER database and trades with China. Existing buyers are in the USA, Europe, Canada, South America and China.
- Work to date on leads: expo and trade-data buyer lists (Anuga, Fi Europe, Fine Food Australia, World Food Istanbul, SupplySide West, Prodexpo, FOODEX Japan, BIOFACH), with companies classified into Tier 1, Tier 2 and Tier 3 and enriched company by company, insisting on full coverage rather than a partial sample.
- Earlier methods the user has used or planned: a 2025 prototype (DuckDuckGo search, ScrapingBee, Gemini), Apollo-style domain-to-contacts enrichment, an OSINT Framework plan, HS code mapping for the whole product range, US market-entry research (ACE, ImportInfo.com, USA Trade Online, Trade Map, Volza), seasonal-demand analysis, a Facebook B2B approach for Asian markets (Indonesia, Philippines, Bangladesh, Vietnam), buyer quality-requirement notes (colour issues on some SKUs), science-backed LinkedIn post batches, Gemini API key rotation, bulk CSV/JSON-driven jobs on Colab, and Google Sheets plus Apps Script scheduling.

## 4. Working style and tools

- Builds with opencode from spec sheets on Windows. Prefers free tools and free API tiers.
- Wants complete, professional, well-organized documents, and wants the agent to consider every point the user raises.
- Wants these documents created: the build spec for opencode, this record of what the user told me, and a separate additions file for new items the user may add for opencode later.

## 5. Second round: tools and repositories to learn and use

The user asked to add the following links to the additions file, understand them, and make use of them. They are tracked one by one in `03_ADDITIONS`.

- Tor proxy tools: mstfknn/tor-proxy, fairhive-labs/tor-proxy, TNLegend/Portable-Tor-Proxy-Rotator, dperson/torproxy.
- CRM platforms: SuiteCRM, Frappe CRM, EspoCRM, Relaticle, NocoBase, nash-md/meow, ethanplusai/sellbird, eracle/OpenOutreach, melgarafael/DeskcommCRM.
- Claude skills collection: OneWave-AI/claude-skills.
- Discovery and social: omkarcloud/google-maps-scraper, cporter202/social-media-scraping-apis.
- Email patterns: LavX email-permutator, pratik-dani/email\_permutations, apifyforge/email-pattern-finder.
- Username, email and social OSINT: kaifcodec/user-scanner, TermuxHackz/X-osint, KatrielMoses/MailAccess, antoniaci/blackbird, HunxByts/GhostTrack, gommzystudio/device-activity-tracker.
- Phone tools: sundowndev/phoneinfoga, VincentSit/ChinaMobilePhoneNumberRegex, HackUnderway/SearchPhone, AzizKpln/Gokboru\_Intel, marmelroy/PhoneNumberKit, whoamitang/numint, phoneintel/phoneintel.
- Reference: kdeldycke/awesome-falsehood.
- Other:\
<https://github.com/NopeCHALLC/nopecha-extension>\
<https://github.com/ToddNieb/linkedin-profile-details-scraper-email-no-cookies-required>\
<https://github.com/Kuduxaaa/botonomus>\
<https://github.com/aydinnyunus/ai-captcha-bypass>\
<https://github.com/vsmutok/FuckCaptcha>\
<https://github.com/FlareSolverr/FlareSolverr>\
<https://github.com/omkarcloud/botasaurus>\
<https://github.com/zidansec/CloudPeler>\
<https://github.com/D4Vinci/Scrapling>

(2026-11-14: this batch has been integrated into `03_ADDITIONS` as ADD-010 to ADD-016 with full decisions in sections 4.8-4.10, and wired into the build spec sections 3, 8, 12, 20 and 21. Staging list removed from `03_ADDITIONS` section 6.)

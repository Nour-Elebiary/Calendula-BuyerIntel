"""Calendula BuyerIntel - local web UI (spec section 18, M8).

Run with `buyerintel serve` (wraps `streamlit run src/buyerintel/ui/app.py`).

Pages: Dashboard, Companies (all section-1/18 filters), Company detail,
People, Market Intelligence, Shipments and Forecast, Outreach and Quotes,
Content Queue, Jobs and Scheduler, Settings, Exports.

The system LOGS and RECOMMENDS - every write here is a log entry for the
user's own records; nothing is ever sent or posted automatically.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import func, select

from buyerintel.db.engine import get_engine, get_session, init_db
from buyerintel.db import models as m
from buyerintel.market import report
from buyerintel.crm.channels import CHANNEL_REQUIRES, CHANNEL_FREE, recommend

st.set_page_config(page_title="Calendula BuyerIntel", page_icon="\U0001f33c", layout="wide")

PAGE = st.sidebar.radio(
    "Page",
    ["Dashboard", "Discovery and Pipeline", "Companies", "Company detail", "People",
     "Market Intelligence", "Shipments and Forecast", "Outreach and Quotes",
     "Content Queue", "Jobs and Scheduler", "Settings", "Exports"],
    label_visibility="collapsed",
)
st.sidebar.caption("Calendula BuyerIntel - logs and recommends, never sends.")


@st.cache_resource
def _session():
    init_db(get_engine())
    return get_session()


session = _session()


def _df(rows: list[dict], columns: list[str] | None = None) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if columns:
        df = df.reindex(columns=columns)
    return df


def _company_options() -> dict[str, int]:
    return {f"#{c.id} {c.canonical_name}": c.id
            for c in session.scalars(select(m.Company).order_by(m.Company.canonical_name)).all()}


def _flows_frame(rows: list) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "period": r.period,
                "hs": r.hs,
                "partner": r.partner,
                "value_usd": r.value_usd,
                "qty": r.qty,
                "unit_value": r.unit_value,
            }
            for r in rows
        ]
    )


def _latest_scores() -> dict[int, float]:
    out: dict[int, float] = {}
    for run in session.scalars(select(m.ScoreRun).order_by(m.ScoreRun.ts.desc())).all():
        if run.company_id not in out:
            out[run.company_id] = run.total
    return out


def _persona_of() -> dict[int, str]:
    rows = session.execute(
        select(m.CompanyPersona.company_id, m.Persona.persona_id)
        .join(m.Persona, m.Persona.id == m.CompanyPersona.persona_id)
    ).all()
    return {cid: pid for cid, pid in rows}


# ===================================================================== pages
if PAGE == "Dashboard":
    st.title("Dashboard")
    companies = session.scalars(select(m.Company)).all()
    scores = _latest_scores()
    personas = _persona_of()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Companies", len(companies))
    col2.metric("People", session.scalar(select(func.count(m.Person.id))) or 0)
    col3.metric("Contact points", session.scalar(select(func.count(m.ContactPoint.id))) or 0)
    col4.metric("Trade flow rows", session.scalar(select(func.count(m.TradeFlow.id))) or 0)

    st.subheader("Coverage meters")
    with_domain = sum(1 for c in companies if c.domain)
    enriched = sum(1 for c in companies if c.last_enriched)
    scored = sum(1 for c in companies if c.id in scores)
    contacted_ids = set(session.scalars(
        select(m.OutreachEvent.company_id).distinct()).all())
    contacted = sum(1 for c in companies if c.id in contacted_ids)
    met1, met2, met3, met4 = st.columns(4)
    met1.progress((with_domain / len(companies)) if companies else 0,
                  text=f"domain known {with_domain}/{len(companies)}")
    met2.progress((enriched / len(companies)) if companies else 0,
                  text=f"enriched {enriched}/{len(companies)}")
    met3.progress((scored / len(companies)) if companies else 0,
                  text=f"scored {scored}/{len(companies)}")
    met4.progress((contacted / len(companies)) if companies else 0,
                  text=f"contacted {contacted}/{len(companies)}")

    left, right = st.columns(2)
    with left:
        st.subheader("Tier counts")
        tiers = pd.DataFrame(
            [{"tier": f"Tier {t}",
              "companies": sum(1 for c in companies if c.tier == t)} for t in (1, 2, 3)])
        st.bar_chart(tiers.set_index("tier"))
    with right:
        st.subheader("State counts")
        states: dict[str, int] = {}
        for c in companies:
            states[c.state or "active"] = states.get(c.state or "active", 0) + 1
        st.dataframe(_df([{"state": k, "companies": v} for k, v in sorted(states.items())]),
                     width="stretch", hide_index=True)

    st.subheader("Persona coverage")
    persona_names = {p.persona_id: p.name for p in session.scalars(select(m.Persona)).all()}
    st.dataframe(_df(
        [{"persona": f"{pid} {persona_names.get(pid, '?')}",
          "companies": sum(1 for p in personas.values() if p == pid)}
         for pid in sorted(persona_names)]),
        width="stretch", hide_index=True)

elif PAGE == "Discovery and Pipeline":
    st.title("Discovery and Pipeline")
    st.caption("Every pipeline step, drivable from here: import files, search "
               "for companies yourself (keywords, countries -> expos, BOL "
               "records), then run the pipeline on what you found. The system "
               "only LISTENS on public pages - it never contacts anyone.")

    # ------------------------------------------------------------ import
    st.subheader("1. Import files")
    with st.expander("Lead workbook (CSV / Excel) + column mapping"):
        from buyerintel.connectors.importer import import_workbook
        from buyerintel.config import CONFIG_DIR

        lead_file = st.file_uploader(
            "Lead workbook", type=["csv", "xlsx", "xls"], key="lead_file")
        maps = sorted(p.name for p in (CONFIG_DIR / "expo_maps").glob("*.yaml"))
        mapping_pick = st.selectbox("Column mapping", maps or ["(none)"],
                                    help="A mapping YAML defines the source name "
                                         "and column aliases (config/expo_maps/).")
        mapping_file = st.file_uploader(
            "...or upload your own mapping YAML", type=["yaml", "yml"],
            key="mapping_file")
        expo_name = st.text_input("Register as expo (optional)", "")
        expo_year = st.number_input("Expo year (0 = none)", 0, 2100, 0)
        if st.button("Import lead workbook") and lead_file is not None:
            import tempfile
            from pathlib import Path as _P

            try:
                tmp = _P(tempfile.gettempdir()) / lead_file.name
                tmp.write_bytes(lead_file.getvalue())
                if mapping_file is not None:
                    mtmp = _P(tempfile.gettempdir()) / mapping_file.name
                    mtmp.write_bytes(mapping_file.getvalue())
                    mapping_path: str | _P = str(mtmp)
                elif mapping_pick != "(none)":
                    mapping_path = str(CONFIG_DIR / "expo_maps" / mapping_pick)
                else:
                    st.error("Pick or upload a column mapping first.")
                    st.stop()
                stats = import_workbook(
                    session, tmp, mapping_path,
                    expo_name=expo_name or None,
                    expo_year=int(expo_year) or None)
                session.commit()
                st.success(f"imported: {getattr(stats, '__dict__', stats)}")
            except Exception as exc:  # noqa: BLE001 - shown, not raised
                session.rollback()
                st.error(f"import failed: {exc}")

    with st.expander("Expo exhibitor list (CSV + expo map)"):
        from buyerintel.connectors.expos import ingest_expo

        expo_file = st.file_uploader(
            "Exhibitor list CSV", type=["csv", "xlsx"], key="expo_file")
        expo_map = st.selectbox("Expo map", [m for m in maps if m != "(none)"],
                                key="expo_map_pick")
        if st.button("Ingest expo list") and expo_file is not None and expo_map:
            import tempfile
            from pathlib import Path as _P

            try:
                tmp = _P(tempfile.gettempdir()) / expo_file.name
                tmp.write_bytes(expo_file.getvalue())
                out = ingest_expo(session, tmp, str(CONFIG_DIR / "expo_maps" / expo_map))
                session.commit()
                st.success(f"ingested: {out}")
            except Exception as exc:  # noqa: BLE001 - shown, not raised
                session.rollback()
                st.error(f"ingest failed: {exc}")

    # ------------------------------------------------------------ search yourself
    st.subheader("2. Search by yourself")
    with st.expander("Free keywords (e.g. 'herbs importer germany')"):
        kw = st.text_area("Queries - one per line", "herbs importer germany\n"
                          "herbs wholesale germany", height=80)
        from buyerintel.search.querygen import COUNTRY_LANG

        iso_options = ["(none)"] + sorted(COUNTRY_LANG)
        kw_country = st.selectbox("Country hint (engine choice)", iso_options,
                                  key="kw_country")
        if st.button("Run keyword search"):
            from buyerintel.discover import search_candidates

            queries = [q for q in kw.splitlines() if q.strip()]
            with st.spinner("searching (cached, rate-limited, fallback engines)..."):
                try:
                    out = search_candidates(
                        session, queries,
                        country=None if kw_country == "(none)" else kw_country)
                    session.commit()
                except Exception as exc:  # noqa: BLE001 - shown, not raised
                    session.rollback()
                    st.error(f"search failed: {exc}")
                    st.stop()
            st.write(out["stats"])
            c1, c2, c3 = st.columns(3)
            c1.metric("New candidates", len(out["created"]))
            c2.metric("Already known", len(out["existing"]))
            c3.metric("Skipped (platforms / seen)", out["skipped"])
            st.dataframe(_df(out["created"]), width="stretch", hide_index=True)
            with st.expander("Known companies matched"):
                st.dataframe(_df(out["existing"]), width="stretch", hide_index=True)
            st.caption("Re-running the same search adds nothing (one provenance "
                       "row per URL). New candidates land un-tiered - run the "
                       "scoring step below to place them.")

    with st.expander("Country sweep - its companies AND expos"):
        country_name = st.text_input("Country (as you'd write it in a query)",
                                     "germany", key="cs_country")
        product_rows = session.scalars(
            select(m.Product).order_by(m.Product.name)).all()
        product = st.selectbox("Product focus", ["herbs"]
                               + [p.name for p in product_rows], key="cs_product")
        show_preview = st.button("Preview queries")
        if show_preview:
            from buyerintel.discover import country_queries, expo_queries
            from buyerintel.search.querygen import product_terms

            terms = [t for t, _lang in product_terms(session, product)]
            st.code("\n".join(country_queries(country_name, product=product,
                                              product_terms=terms)[:12])
                    + "\n... (+ expo queries)\n"
                    + "\n".join(expo_queries(country_name, product=product)))
            st.caption("Role words x product terms x ontology languages.")
        c_run, c_expo = st.columns(2)
        if c_run.button("Run country company sweep"):
            from buyerintel.discover import country_queries, search_candidates
            from buyerintel.search.querygen import product_terms

            terms = [t for t, _lang in product_terms(session, product)]
            queries = country_queries(country_name, product=product,
                                      product_terms=terms)
            with st.spinner("sweeping..."):
                try:
                    out = search_candidates(session, queries, tag="search")
                    session.commit()
                except Exception as exc:  # noqa: BLE001 - shown, not raised
                    session.rollback()
                    st.error(f"sweep failed: {exc}")
                    st.stop()
            st.write(out["stats"])
            st.metric("New candidates", len(out["created"]))
            st.dataframe(_df(out["created"]), width="stretch", hide_index=True)
        if c_expo.button("Find the country's expos (leads)"):
            from buyerintel.discover import expo_leads, expo_queries

            with st.spinner("searching expos..."):
                try:
                    leads = expo_leads(
                        session, expo_queries(country_name, product=product))
                    session.commit()
                except Exception as exc:  # noqa: BLE001 - shown, not raised
                    session.rollback()
                    st.error(f"expo search failed: {exc}")
                    st.stop()
            st.dataframe(_df(leads), width="stretch", hide_index=True)
            st.caption("Expos are stored as LEADS (links), not companies. To "
                       "ingest an actual exhibitor list, download its CSV and "
                       "use the ingest box above with the matching expo map.")

    with st.expander("BOL / shipment records - who is already listed"):
        st.caption("Checks each company's PUBLIC bill-of-lading footprint "
                   "(ImportYeti public pages, no account; ~3 s per company). "
                   "Companies with records are tagged bol_present and their "
                   "shipment rows are stored (used later by forecasting).")
        bol_tier = st.selectbox("Tier scope", [1, 2, 3, "all tiers"], key="bol_tier")
        bol_limit = st.number_input("Max companies this run", 1, 200, 20)
        if st.button("Check BOL records"):
            from buyerintel.discover import run_bol_queue

            with st.spinner("checking public BOL pages (paced)..."):
                try:
                    rows = run_bol_queue(
                        session, tier=None if bol_tier == "all tiers" else bol_tier,
                        limit=int(bol_limit))
                    session.commit()
                except Exception as exc:  # noqa: BLE001 - shown, not raised
                    session.rollback()
                    st.error(f"BOL check failed: {exc}")
                    st.stop()
            st.dataframe(_df(rows), width="stretch", hide_index=True)
            hits = sum(1 for r in rows if r["rows"])
            st.metric("Companies with BOL records", hits)

    # ------------------------------------------------------------ pipeline
    st.subheader("3. Run the pipeline on what you found")
    pc1, pc2 = st.columns(2)
    enrich_limit = pc1.number_input("Enrich: max companies", 1, 500, 25)
    if pc1.button("Run enrichment (S0-S4) on the queue"):
        from buyerintel.enrich.engine import EnrichEngine
        from buyerintel.llm.router import LLMRouter
        from buyerintel.search.router import SearchRouter

        with st.spinner("enriching (resumable)..."):
            try:
                engine = EnrichEngine(session, llm=LLMRouter(session),
                                      search=SearchRouter(session))
                stats = engine.run(limit=int(enrich_limit))
                session.commit()
                st.write(stats.summary())
            except Exception as exc:  # noqa: BLE001 - shown, not raised
                session.rollback()
                st.error(f"enrichment failed: {exc}")
    if pc2.button("Run scoring + tiers (rule chain)"):
        from buyerintel.llm.router import LLMRouter
        from buyerintel.score import rules as score_rules

        with st.spinner("scoring..."):
            try:
                ctx = score_rules.load_context(llm=LLMRouter(session))
                scored = 0
                for company in score_rules.pending_companies(session, limit=None):
                    score_rules.run_rule_chain(session, company, ctx)
                    scored += 1
                session.commit()
                st.success(f"scored {scored} companies")
            except Exception as exc:  # noqa: BLE001 - shown, not raised
                session.rollback()
                st.error(f"scoring failed: {exc}")
    pc3, pc4 = st.columns(2)
    if pc3.button("Run dedupe sweep"):
        from buyerintel.resolve.dedupe import sweep

        with st.spinner("sweeping duplicates..."):
            try:
                out = sweep(session)
                session.commit()
                st.success(f"dedupe: {out}")
            except Exception as exc:  # noqa: BLE001 - shown, not raised
                session.rollback()
                st.error(f"dedupe failed: {exc}")
    if pc4.button("Run forecasts (companies with shipments)"):
        from buyerintel.forecast import forecast_all

        with st.spinner("forecasting..."):
            try:
                rows = forecast_all(session)
                session.commit()
                st.success(f"{len(rows)} forecast row(s) refreshed"
                           if rows else
                           "no forecasts yet (needs >= 2 dated shipments per company)")
            except Exception as exc:  # noqa: BLE001 - shown, not raised
                session.rollback()
                st.error(f"forecast failed: {exc}")
    st.caption("Everything above also runs automatically through the scheduler "
               "(Jobs and Scheduler page) - these buttons are the manual path.")

elif PAGE == "Companies":
    st.title("Companies")
    st.caption("Filters per spec section 18: country, company, tier, state, persona, "
               "product, tags (organic / conventional), seniority, contact "
               "availability by channel, last outreach, source / expo.")

    countries = sorted({c.country for c in session.scalars(select(m.Company)).all() if c.country})
    all_tags = sorted({t for c in session.scalars(select(m.Company)).all()
                       for t in (c.tags or [])})
    persona_rows = session.execute(
        select(m.Persona.persona_id, m.Persona.name)).all()
    products = [p.name for p in session.scalars(select(m.Product).order_by(m.Product.name)).all()]
    expos = [e.name for e in session.scalars(select(m.Expo)).all()]

    f1, f2, f3, f4 = st.columns(4)
    name_q = f2.text_input("Company name contains", "")
    country = f1.selectbox("Country", ["(any)"] + countries)
    tier = f3.selectbox("Tier", ["(any)", 1, 2, 3])
    state = f4.selectbox("State", ["(any)"] + sorted(
        {c.state or "active" for c in session.scalars(select(m.Company)).all()}))
    f5, f6, f7, f8 = st.columns(4)
    persona_f = f5.selectbox("Persona", ["(any)"] + [f"{pid} {nm}" for pid, nm in persona_rows])
    product_f = f6.selectbox("Product match", ["(any)"] + products)
    tags_f = f7.multiselect("Tags (incl. organic / conventional)", all_tags)
    seniority_f = f8.selectbox("Seniority (has contact)", ["(any)"] + sorted(
        {p.seniority for p in session.scalars(select(m.Person)).all() if p.seniority}))
    f9, f10, f11, _ = st.columns(4)
    channel_f = f9.selectbox("Reachable via channel", ["(any)"] + sorted(
        set(CHANNEL_REQUIRES) | CHANNEL_FREE))
    outreach_f = f10.selectbox("Last outreach", ["(any)", "never contacted",
                                                 "recent (30d)", "stale (>30d)"])
    expo_f = f11.selectbox("Expo appearance", ["(any)"] + expos)

    query = select(m.Company)
    if country != "(any)":
        query = query.where(m.Company.country == country)
    if name_q:
        query = query.where(m.Company.canonical_name.ilike(f"%{name_q}%"))
    if tier != "(any)":
        query = query.where(m.Company.tier == tier)
    if state != "(any)":
        query = query.where(m.Company.state == state)
    if persona_f != "(any)":
        pid = persona_f.split(" ")[0]
        query = query.where(m.Company.id.in_(
            select(m.CompanyPersona.company_id).where(
                m.CompanyPersona.persona_id.in_(
                    select(m.Persona.id).where(m.Persona.persona_id == pid)))))
    if product_f != "(any)":
        query = query.where(m.Company.id.in_(
            select(m.ProductMatch.company_id).where(
                m.ProductMatch.product_id.in_(
                    select(m.Product.id).where(m.Product.name == product_f)))))
    if seniority_f != "(any)":
        query = query.where(m.Company.id.in_(
            select(m.Person.company_id).where(m.Person.seniority == seniority_f)))
    if expo_f != "(any)":
        query = query.where(m.Company.id.in_(
            select(m.ExpoAppearance.company_id).where(
                m.ExpoAppearance.edition_id.in_(
                    select(m.ExpoEdition.id).where(
                        m.ExpoEdition.expo_id.in_(
                            select(m.Expo.id).where(m.Expo.name == expo_f)))))))
    companies = session.scalars(query.order_by(m.Company.score.desc())).all()

    scores = _latest_scores()
    personas_map = _persona_of()
    persona_names = dict(persona_rows)
    contacted = {cid: ts for cid, ts in session.execute(
        select(m.OutreachEvent.company_id, func.max(m.OutreachEvent.ts))
        .group_by(m.OutreachEvent.company_id)).all()}
    kinds_by_company: dict[int, set[str]] = {}
    for owner_id, kind in session.execute(
            select(m.ContactPoint.owner_id, m.ContactPoint.kind)
            .where(m.ContactPoint.owner_type == "company")).all():
        kinds_by_company.setdefault(owner_id, set()).add(kind)

    rows = []
    for c in companies:
        if tags_f and not set(tags_f) & set(c.tags or []):
            continue
        if channel_f != "(any)":
            required = CHANNEL_REQUIRES.get(channel_f)
            kinds = kinds_by_company.get(c.id, set())
            if required and not (set(required) & kinds):
                continue
            if channel_f in CHANNEL_FREE and not kinds:
                continue
        last_ts = contacted.get(c.id)
        if outreach_f == "never contacted" and last_ts is not None:
            continue
        if outreach_f == "recent (30d)" and (last_ts is None
                                             or last_ts < datetime.now(timezone.utc) - timedelta(days=30)):
            continue
        if outreach_f == "stale (>30d)" and (last_ts is None
                                             or last_ts >= datetime.now(timezone.utc) - timedelta(days=30)):
            continue
        pid = personas_map.get(c.id)
        rows.append({
            "id": c.id, "name": c.canonical_name, "country": c.country,
            "tier": c.tier, "state": c.state, "score": scores.get(c.id, c.score),
            "persona": pid and f"{pid} {persona_names.get(pid, '')}",
            "domain": c.domain, "tags": ", ".join(c.tags or []),
            "last_outreach": last_ts.strftime("%Y-%m-%d") if last_ts else "-",
        })
    st.dataframe(_df(rows), width="stretch", hide_index=True,
                  height=480, column_config={"id": st.column_config.NumberColumn(width="small")})
    st.caption(f"{len(rows)} companies shown. Set the id in Company detail to open one.")

elif PAGE == "Company detail":
    st.title("Company detail")
    options = _company_options()
    if not options:
        st.info("No companies yet - import leads or run discovery first.")
        st.stop()
    default = f"#{st.session_state['selected_company']} ..." \
        if "selected_company" in st.session_state else next(iter(options))
    pick = st.selectbox("Company", list(options),
                        index=list(options).index(default) if default in options else 0)
    company = session.get(m.Company, options[pick])
    scores = _latest_scores()
    st.subheader(f"{company.canonical_name}")
    st.caption(f"id {company.id} - state {company.state}"
               + (f" ({company.state_reason})" if company.state_reason else ""))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tier", company.tier or "-")
    c2.metric("Score", round(scores.get(company.id, company.score or 0), 1))
    c3.metric("Country", company.country or "-")
    c4.metric("Size band", company.size_band or "-")
    st.write(f"domain: {company.domain or '-'} - city: {company.city or '-'}"
             f" - type: {company.type or '-'} - confidence: {company.confidence:.2f}")
    st.write("tags: " + (", ".join(company.tags or []) or "-"))

    tab_score, tab_evi, tab_contacts, tab_timeline, tab_ships, tab_arch = st.tabs(
        ["Score breakdown", "Evidence & products", "Contacts", "Timeline",
         "Shipments", "Archive snapshots"])

    with tab_score:
        run = session.scalars(
            select(m.ScoreRun).where(m.ScoreRun.company_id == company.id)
            .order_by(m.ScoreRun.ts.desc()).limit(1)).first()
        if run is None:
            st.info("Not scored yet - run the score task.")
        else:
            st.write(f"scored {run.ts} (weights v{run.weights_version}) total **{run.total:.1f}**")
            st.json(run.components or {}, expanded=True)

    with tab_evi:
        matches = session.scalars(
            select(m.ProductMatch).where(m.ProductMatch.company_id == company.id)).all()
        st.dataframe(_df([{
            "product": session.get(m.Product, x.product_id).name if x.product_id else "?",
            "matched term": x.matched_term, "score": x.score,
            "evidence": x.evidence_url, "snippet": (x.evidence_snippet or "")[:140],
        } for x in matches]), width="stretch", hide_index=True)
        appearances = session.execute(
            select(m.Expo, m.ExpoEdition.year, m.ExpoAppearance.booth,
                   m.ExpoAppearance.listed_products)
            .join(m.ExpoEdition, m.ExpoEdition.id == m.ExpoAppearance.edition_id)
            .join(m.Expo, m.Expo.id == m.ExpoEdition.expo_id)
            .where(m.ExpoAppearance.company_id == company.id)).all()
        st.dataframe(_df([{"expo": e, "year": y, "booth": b,
                           "listed": ", ".join(p or [])}
                          for e, y, b, p in appearances]),
                     width="stretch", hide_index=True)

    with tab_contacts:
        cps = session.scalars(select(m.ContactPoint).where(
            m.ContactPoint.owner_type == "company",
            m.ContactPoint.owner_id == company.id)).all()
        st.dataframe(_df([{"kind": x.kind, "value": x.value,
                           "verified": x.verified_status, "confidence": x.confidence,
                           "source": x.source_url}
                          for x in cps]), width="stretch", hide_index=True)
        st.subheader("Recommended channels")
        for row in recommend(session, company, top=5):
            obs = f"{row['observed']:.2f}" if row["observed"] is not None else "-"
            st.write(f"**{row['channel']}** score {row['score']}"
                     f" (prior {row['prior']}, observed {obs}, {row['source']})")

    with tab_timeline:
        events = session.scalars(
            select(m.OutreachEvent).where(m.OutreachEvent.company_id == company.id)
            .order_by(m.OutreachEvent.ts.desc())).all()
        st.dataframe(_df([{"ts": x.ts, "dir": x.direction, "channel": x.channel,
                           "outcome": x.outcome, "subject": x.subject, "notes": x.notes}
                          for x in events]), width="stretch", hide_index=True)
        st.dataframe(_df([{"sample #": s.id, "status": s.status, "date": s.date,
                           "terms": s.shipping_terms}
                          for s in session.scalars(select(m.Sample)
                          .where(m.Sample.company_id == company.id)).all()]),
                     width="stretch", hide_index=True)

    with tab_ships:
        ships = session.scalars(
            select(m.Shipment).where(m.Shipment.company_id == company.id)
            .order_by(m.Shipment.date.desc())).all()
        st.dataframe(_df([{"date": x.date, "hs": x.hs_code, "qty": x.qty,
                           "unit": x.unit, "value_usd": x.value_usd,
                           "origin": x.origin, "dest": x.dest, "source": x.source}
                          for x in ships]), width="stretch", hide_index=True)
        fcs = session.scalars(
            select(m.Forecast).where(m.Forecast.company_id == company.id)).all()
        st.dataframe(_df([{"product": x.product_key, "n_shipments": x.n_shipments,
                           "window": f"{x.window_start} - {x.window_end}",
                           "qty": f"{x.qty_min} - {x.qty_max}",
                           "value": f"${x.value_min_usd} - ${x.value_max_usd}",
                           "confidence": x.confidence}
                          for x in fcs]), width="stretch", hide_index=True)

    with tab_arch:
        snaps = []
        if company.domain:
            snaps = session.scalars(
                select(m.ArchiveSnapshot).where(m.ArchiveSnapshot.url.like(
                    f"%{company.domain}%")).order_by(m.ArchiveSnapshot.timestamp.desc())).all()
        st.dataframe(_df([{"url": x.url, "timestamp": x.timestamp, "source": x.source}
                          for x in snaps]), width="stretch", hide_index=True)

elif PAGE == "People":
    st.title("People")
    seniorities = sorted({p.seniority for p in session.scalars(select(m.Person)).all()
                          if p.seniority})
    options = _company_options()
    c1, c2, c3 = st.columns(3)
    seniority_f = c1.selectbox("Seniority", ["(any)"] + seniorities)
    dm_only = c2.checkbox("Decision makers only")
    company_f = c3.selectbox("Company", ["(any)"] + list(options))
    query = select(m.Person)
    if seniority_f != "(any)":
        query = query.where(m.Person.seniority == seniority_f)
    if dm_only:
        query = query.where(m.Person.is_decision_maker.is_(True))
    if company_f != "(any)":
        query = query.where(m.Person.company_id == options[company_f])
    names = {cid: name for cid, name in session.execute(
        select(m.Company.id, m.Company.canonical_name)).all()}
    st.dataframe(_df([{
        "id": p.id, "name": p.full_name, "company": names.get(p.company_id, p.company_id),
        "title": p.title, "seniority": p.seniority, "department": p.department,
        "decision maker": "yes" if p.is_decision_maker else "",
        "confidence": p.confidence,
    } for p in session.scalars(query.order_by(m.Person.full_name)).all()]),
        width="stretch", hide_index=True, height=520)

elif PAGE == "Market Intelligence":
    st.title("Market Intelligence")
    st.caption("Egypt herb exports and destination imports per HS code (M2 page).")

    st.subheader("Egypt exports by partner")
    year_filter = st.text_input("Year filter (blank = all)", "")
    flows = report.exports_by_partner(session, year=year_filter or None)
    df = _flows_frame(flows)
    st.dataframe(df, width="stretch", hide_index=True)
    if not df.empty:
        by_partner = (
            df.groupby("partner", as_index=False)["value_usd"]
            .sum()
            .sort_values("value_usd", ascending=False)
        )
        st.bar_chart(by_partner.set_index("partner")["value_usd"])

    st.subheader("Destination imports per HS")
    hs_codes = [code for code, _products in report.hs_products(session)]
    hs_filter = st.selectbox("HS code", ["(all)"] + hs_codes)
    imports = report.imports_by_year(session, hs=None if hs_filter == "(all)" else hs_filter)
    st.dataframe(_flows_frame(imports), width="stretch", hide_index=True)

    st.subheader("Unit values (USD/kg)")
    uv = report.unit_values(session, year=year_filter or None)
    st.dataframe(
        pd.DataFrame(uv, columns=["period", "hs", "partner", "usd_per_kg"]),
        width="stretch",
        hide_index=True,
    )

    st.subheader("Demand signals")
    rows = session.scalars(select(m.MarketSignal)
                           .order_by(m.MarketSignal.source, m.MarketSignal.subject)).all()
    st.dataframe(
        pd.DataFrame(
            [{"source": r.source, "subject": r.subject, "period": r.period, "value": r.value}
             for r in rows],
            columns=["source", "subject", "period", "value"],
        ),
        width="stretch",
        hide_index=True,
    )
    digest = report.market_digest(session)
    d1, d2, d3 = st.columns(3)
    d1.metric("Trade flow rows", digest["trade_rows"])
    d2.metric("HS codes covered", digest["hs_codes"])
    d3.metric("EUR/USD (latest)", digest["latest_eur_usd"] or "-")

elif PAGE == "Shipments and Forecast":
    st.title("Shipments and Forecast")
    options = _company_options()
    c1, c2 = st.columns(2)
    company_f = c1.selectbox("Company", ["(any)"] + list(options))
    hs_f = c2.text_input("HS code contains", "")
    query = select(m.Shipment)
    if company_f != "(any)":
        query = query.where(m.Shipment.company_id == options[company_f])
    if hs_f:
        query = query.where(m.Shipment.hs_code.ilike(f"%{hs_f}%"))
    names = {cid: name for cid, name in session.execute(
        select(m.Company.id, m.Company.canonical_name)).all()}
    ships = session.scalars(query.order_by(m.Shipment.date.desc()).limit(500)).all()
    st.subheader(f"Shipments ({len(ships)} shown)")
    st.dataframe(_df([{"company": names.get(x.company_id, x.company_id),
                       "date": x.date, "hs": x.hs_code, "qty": x.qty, "unit": x.unit,
                       "value_usd": x.value_usd, "origin": x.origin, "dest": x.dest,
                       "shipper": x.shipper, "consignee": x.consignee, "source": x.source}
                      for x in ships]), width="stretch", hide_index=True, height=420)

    st.subheader("Order forecasts")
    fcq = select(m.Forecast)
    if company_f != "(any)":
        fcq = fcq.where(m.Forecast.company_id == options[company_f])
    st.dataframe(_df([{"company": names.get(x.company_id, x.company_id),
                       "product": x.product_key, "n_shipments": x.n_shipments,
                       "window": f"{x.window_start} - {x.window_end}",
                       "qty": f"{x.qty_min} - {x.qty_max}",
                       "value": f"${x.value_min_usd} - ${x.value_max_usd}",
                       "unit value": x.unit_value_usd, "uv source": x.uv_source,
                       "confidence": x.confidence}
                      for x in session.scalars(fcq).all()]),
                 width="stretch", hide_index=True)

elif PAGE == "Outreach and Quotes":
    st.title("Outreach and Quotes")
    st.caption("The system logs and recommends - it never sends.")

    st.subheader("Next actions (next-action engine)")
    from buyerintel.crm.outreach import next_actions

    tier_f = st.selectbox("Tier scope", ["all", 1, 2, 3])
    actions = next_actions(session, tiers=None if tier_f == "all" else [tier_f], limit=100)
    st.dataframe(_df([{"id": a["company_id"], "company": a["name"], "tier": a["tier"],
                       "action": a["action"], "channel": a.get("channel", ""),
                       "reason": a["reason"], "detail": a.get("detail", "")}
                      for a in actions]), width="stretch", hide_index=True, height=320)

    st.subheader("Log an outreach event")
    options = _company_options()
    if not options:
        st.info("No companies yet.")
        st.stop()
    with st.form("log_outreach", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        company_pick = c1.selectbox("Company", list(options), key="ol_company")
        channel = c2.selectbox("Channel", ["email", "phone", "whatsapp", "contact_form",
                                           "linkedin", "facebook", "instagram", "wechat",
                                           "telegram", "b2b_marketplace", "rfq_board",
                                           "expo_matchmaking", "trade_association", "other"])
        direction = c3.selectbox("Direction", ["out", "in"])
        c4, c5 = st.columns(2)
        outcome = c4.text_input("Outcome (sent / replied / no_reply / bounced ...)", "")
        subject = c5.text_input("Subject", "")
        notes = st.text_area("Notes", height=60)
        if st.form_submit_button("Log event"):
            from buyerintel.crm.outreach import log_outreach

            log_outreach(session, company_id=options[company_pick], channel=channel,
                         direction=direction, outcome=outcome or None,
                         subject=subject or None, notes=notes or None)
            session.commit()
            st.success("Outreach event logged.")
            st.rerun()

    st.subheader("Quotes")
    from buyerintel.crm.quotes import INCOTERMS, benchmark_unit_value, log_quote

    q_rows = session.execute(
        select(m.Quote, m.Company.canonical_name).join(
            m.Company, m.Company.id == m.Quote.company_id)).all()
    st.dataframe(_df([{"id": q.id, "company": cname, "date": q.date,
                       "price": q.price, "currency": q.currency,
                       "incoterm": q.incoterm, "qty": q.qty, "status": q.status,
                       "products": ", ".join(q.products or [])}
                      for q, cname in q_rows]), width="stretch", hide_index=True)
    st.caption("Calendula's own prices are the user's call - no stored price list. "
               "Trade-data unit values are a reference benchmark only.")
    with st.form("log_quote", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        q_company = c1.selectbox("Company", list(options), key="q_company")
        q_price = c2.number_input("Price", min_value=0.0, step=0.01)
        q_currency = c3.text_input("Currency", "USD")
        q_incoterm = c4.selectbox("Incoterm", INCOTERMS)
        c5, c6 = st.columns(2)
        q_qty = c5.number_input("Quantity (kg)", min_value=0.0, step=1.0)
        q_hs = c6.text_input("HS (show benchmark, optional)", "")
        if st.form_submit_button("Log quote"):
            log_quote(session, company_id=options[q_company], price=q_price,
                      currency=q_currency or "USD", incoterm=q_incoterm,
                      qty=q_qty or None)
            session.commit()
            if q_hs:
                bench = benchmark_unit_value(session, q_hs)
                if bench:
                    st.info(f"Benchmark (reference only): {bench}")
            st.success("Quote logged.")
            st.rerun()

    st.subheader("Samples")
    from buyerintel.crm.samples import SAMPLE_TERMS, STATUSES, offer_sample

    st.info(f"Fixed sample terms: {SAMPLE_TERMS}")
    s_rows = session.execute(
        select(m.Sample, m.Company.canonical_name).join(
            m.Company, m.Company.id == m.Sample.company_id)).all()
    st.dataframe(_df([{"id": s.id, "company": cname, "date": s.date,
                       "status": s.status, "terms": s.shipping_terms}
                      for s, cname in s_rows]), width="stretch", hide_index=True)
    with st.form("offer_sample", clear_on_submit=True):
        s_company = st.selectbox("Company", list(options), key="s_company")
        if st.form_submit_button("Log sample offer"):
            offer_sample(session, company_id=options[s_company])
            session.commit()
            st.success("Sample offer logged with the fixed terms.")
            st.rerun()
    with st.expander("Update a sample status"):
        s_id = st.number_input("Sample id", min_value=1, step=1, key="s_id")
        s_status = st.selectbox("New status", STATUSES, key="s_status")
        if st.button("Update sample status"):
            from buyerintel.crm.samples import set_status

            try:
                set_status(session, int(s_id), s_status)
                session.commit()
                st.success(f"Sample #{int(s_id)} -> {s_status}")
                st.rerun()
            except (LookupError, ValueError) as exc:
                st.error(str(exc))

elif PAGE == "Content Queue":
    st.title("Content Queue")
    st.caption("Drafts for trending herbs (B2B framing, image brief). Nothing is "
               "posted automatically - the user posts manually.")
    from buyerintel.crm import content as content_mod

    if st.button("Draft posts for trending products"):
        drafts = content_mod.draft_for_trends(session)
        session.commit()
        st.success(f"{len(drafts)} new draft(s)" if drafts else "No new trending products.")
        st.rerun()

    items = session.scalars(select(m.ContentItem)
                            .order_by(m.ContentItem.created_at.desc())).all()
    if not items:
        st.info("No content yet - draft posts from the trends signals.")
    for item in items:
        with st.expander(f"#{item.id} {item.title} [{item.status}]"):
            st.write(item.body)
            st.caption(f"Image brief: {item.image_brief}")
            if st.button("Refresh send/skip suggestions", key=f"sug_{item.id}"):
                content_mod.suggest_recipients(session, item)
                session.commit()
                st.rerun()
            sug = session.execute(
                select(m.ContentSuggestion, m.Company.canonical_name)
                .join(m.Company, m.Company.id == m.ContentSuggestion.company_id)
                .where(m.ContentSuggestion.content_id == item.id)
                .order_by(m.ContentSuggestion.decision, m.Company.canonical_name)).all()
            st.dataframe(_df([{"company": cname, "decision": s.decision, "reason": s.reason}
                              for s, cname in sug]),
                         width="stretch", hide_index=True)

elif PAGE == "Jobs and Scheduler":
    st.title("Jobs and Scheduler")
    from buyerintel.sched import runner

    runner.seed_jobs(session)   # keeps the job table in sync with the yaml
    session.commit()
    if st.button("Run startup catch-up (missed jobs, coalesced)"):
        ran = runner.startup_catchup(session)
        st.success(f"catch-up ran: {ran}" if ran else "nothing missed")
        st.rerun()

    st.subheader("Jobs")
    for row in runner.list_jobs(session):
        c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
        c1.write(f"**{row['job_id']}**")
        c2.write(f"cron `{row['cron']}`" if row["cron"] else "-")
        c3.write(row["last_status"] or "never run")
        c4.write(row["last_run"] or "-")
        b1, b2, b3 = st.columns(3)
        if b1.button("Run now", key=f"run_{row['job_id']}"):
            job = session.scalar(select(m.Job).where(m.Job.job_id == row["job_id"]))
            if job is not None:
                run_result = runner.run_job(session, job)
                session.commit()
                st.write(f"status **{run_result.status}**"
                         + (f" - {run_result.error}" if run_result.error else ""))
        if b2.button("Pause" if row["enabled"] else "Resume", key=f"tog_{row['job_id']}"):
            (runner.pause if row["enabled"] else runner.resume)(session, row["job_id"])
            session.commit()
            st.rerun()

    st.subheader("Recent run history")
    runs = session.scalars(select(m.JobRun)
                           .order_by(m.JobRun.started_at.desc()).limit(50)).all()
    st.dataframe(_df([{"job": x.job_id, "started": x.started_at, "finished": x.finished_at,
                       "status": x.status, "detail": (x.error or "")[:120]}
                      for x in runs]), width="stretch", hide_index=True)

elif PAGE == "Settings":
    st.title("Settings")
    st.caption("Config-driven system (English, single user, local-first). "
               "Edit the YAML files in config/ - shown read-only here.")
    from buyerintel.config import CONFIG_DIR

    for name in ("providers", "engines", "weights", "personas", "products",
                 "languages", "channels", "network", "score"):
        path = CONFIG_DIR / f"{name}.yaml"
        with st.expander(f"config/{name}.yaml"):
            if path.exists():
                st.code(path.read_text(encoding="utf-8"), language="yaml")
            else:
                st.write("(missing)")

elif PAGE == "Exports":
    st.title("Exports")
    st.caption("One-click multi-sheet Excel workbook: summary, tier sheets, persona "
               "sheets, contacts, market digest, changelog from the audit log.")
    from buyerintel.export.workbook import export_workbook

    if st.button("Generate workbook"):
        out = f"data/buyerintel_{datetime.now():%Y%m%d_%H%M}.xlsx"
        written = export_workbook(session, out)
        st.session_state["workbook_path"] = str(written)
        st.success(f"written: {written}")
    path = st.session_state.get("workbook_path")
    if path:
        from pathlib import Path as _P

        p = _P(path)
        if p.exists():
            st.download_button("Download workbook", data=p.read_bytes(),
                               file_name=p.name,
                               mime="application/vnd.openxmlformats-officedocument"
                                    ".spreadsheetml.sheet")

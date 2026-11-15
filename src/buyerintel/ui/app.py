"""Local web dashboard (spec section 16). M2 scope: the Market Intelligence page.

Run with `buyerintel serve` (wraps `streamlit run src/buyerintel/ui/app.py`).
Everything else (Dashboard, Companies, People, ...) lands at M8.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from buyerintel.db.engine import get_engine, get_session, init_db
from buyerintel.market import report

st.set_page_config(page_title="Calendula BuyerIntel", page_icon="\U0001f33c", layout="wide")
st.title("\u2637 Calendula BuyerIntel")
st.caption("Market Intelligence (M2) - Egypt herb exports and destination imports per HS code")


@st.cache_resource
def _session():
    init_db(get_engine())
    return get_session()


session = _session()
tabs = st.tabs(["Market Intelligence", "Signals", "Dashboard"])


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


with tabs[0]:  # Market Intelligence
    st.subheader("Egypt exports by partner")
    year_filter = st.text_input("Year filter (blank = all)", "")
    flows = report.exports_by_partner(session, year=year_filter or None)
    df = _flows_frame(flows)
    st.dataframe(df, use_container_width=True, hide_index=True)
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
    st.dataframe(_flows_frame(imports), use_container_width=True, hide_index=True)

    st.subheader("Unit values (USD/kg)")
    uv = report.unit_values(session, year=year_filter or None)
    st.dataframe(
        pd.DataFrame(uv, columns=["period", "hs", "partner", "usd_per_kg"]),
        use_container_width=True,
        hide_index=True,
    )

with tabs[1]:  # Signals
    st.subheader("Demand signals")
    from sqlalchemy import select

    from buyerintel.db.models import MarketSignal

    rows = session.scalars(select(MarketSignal).order_by(MarketSignal.source, MarketSignal.subject)).all()
    st.dataframe(
        pd.DataFrame(
            [
                {"source": r.source, "subject": r.subject, "period": r.period, "value": r.value}
                for r in rows
            ],
            columns=["source", "subject", "period", "value"],
        ),
        use_container_width=True,
        hide_index=True,
    )

with tabs[2]:  # Dashboard digest
    digest = report.market_digest(session)
    col1, col2, col3 = st.columns(3)
    col1.metric("Trade flow rows", digest["trade_rows"])
    col2.metric("HS codes covered", digest["hs_codes"])
    col3.metric("EUR/USD (latest)", digest["latest_eur_usd"] or "-")
    st.json(digest["signals"], expanded=True)

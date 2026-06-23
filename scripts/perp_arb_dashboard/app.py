"""Streamlit UI for the local perp arbitrage dashboard."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import config
from scanner import ScanResult, load_history_tail, run_scan, save_snapshot


st.set_page_config(page_title="Perp Arbitrage Dashboard", layout="wide")


def main() -> None:
    st.title("Perp Arbitrage Dashboard")

    render_sidebar()

    refresh = st.sidebar.button("Refresh", type="primary", use_container_width=True)
    if refresh or "scan_result" not in st.session_state:
        with st.spinner("Scanning public exchange data..."):
            result = run_scan()
            save_snapshot(result.raw)
            st.session_state["scan_result"] = result

    result = st.session_state["scan_result"]
    render_summary(result)
    render_errors(result.errors)
    render_alert_tables(result)
    render_raw_table(result.raw)
    render_history()


def render_sidebar() -> None:
    st.sidebar.header("Config")
    st.sidebar.metric("Min quote volume", f"{config.MIN_QUOTE_VOLUME:,.0f}")
    st.sidebar.metric("Max bid/ask spread", f"{config.MAX_BID_ASK_SPREAD_BPS:,.0f} bps")
    st.sidebar.metric("Funding spread alert", f"{config.FUNDING_SPREAD_ALERT_BPS:,.0f} bps")
    st.sidebar.metric("Price dispersion alert", f"{config.PRICE_DISPERSION_ALERT_BPS:,.0f} bps")
    st.sidebar.divider()
    st.sidebar.caption(f"Data path: {config.DATA_DIR}")


def render_summary(result: ScanResult) -> None:
    unique_symbols = result.raw["base"].nunique() if not result.raw.empty else 0
    columns = st.columns(4)
    columns[0].metric("Raw rows", f"{len(result.raw):,}")
    columns[1].metric("Funding alerts", f"{len(result.funding_alerts):,}")
    columns[2].metric("Price alerts", f"{len(result.price_alerts):,}")
    columns[3].metric("Unique symbols", f"{unique_symbols:,}")


def render_errors(errors: list[str]) -> None:
    if not errors:
        return

    with st.expander(f"Non-fatal fetch errors ({len(errors)})"):
        st.dataframe(pd.DataFrame({"error": errors}), use_container_width=True, hide_index=True)


def render_alert_tables(result: ScanResult) -> None:
    st.subheader("Funding Spread Alerts")
    st.dataframe(result.funding_alerts, use_container_width=True, hide_index=True)

    st.subheader("Price Dispersion Alerts")
    st.dataframe(result.price_alerts, use_container_width=True, hide_index=True)


def render_raw_table(raw: pd.DataFrame) -> None:
    st.subheader("Raw Cross-Section")
    if raw.empty:
        st.dataframe(raw, use_container_width=True, hide_index=True)
        return

    symbols = sorted(raw["base"].dropna().unique())
    selected = st.multiselect("Symbol filter", symbols, default=symbols)
    filtered = raw[raw["base"].isin(selected)] if selected else raw.iloc[0:0]
    st.dataframe(filtered, use_container_width=True, hide_index=True)


def render_history() -> None:
    st.subheader("Local History")
    history = load_history_tail()
    st.dataframe(history, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()

"""Streamlit UI for the local perp arbitrage dashboard."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

import config
from scanner import ScanResult, load_history_tail, load_latest_snapshot, run_scan, save_snapshot


st.set_page_config(page_title="Perp Arbitrage Dashboard", layout="wide")
TABLE_WIDTH = "stretch"


def main() -> None:
    st.title("Perp Arbitrage Dashboard")
    render_sidebar()

    refresh = st.sidebar.button("Refresh", type="primary", width=TABLE_WIDTH)
    if "scan_result" not in st.session_state:
        cached_result = load_latest_snapshot()
        if cached_result is not None:
            st.session_state["scan_result"] = cached_result
            st.session_state["data_source"] = "Latest local snapshot"

    if refresh or "scan_result" not in st.session_state:
        with st.spinner("Scanning public exchange data..."):
            result = run_scan()
            save_snapshot(result.raw)
            st.session_state["scan_result"] = result
            st.session_state["data_source"] = "Fresh public scan"

    result = st.session_state["scan_result"]
    st.caption(f"Data source: {st.session_state.get('data_source', 'Current session')}")
    render_summary(result)
    alerts_tab, details_tab, raw_tab, history_tab = st.tabs(
        ["Alerts", "Alert Details", "Raw Cross-Section", "Local History"]
    )
    with alerts_tab:
        render_errors(result.errors)
        render_alert_tables(result)
    with details_tab:
        render_alert_details(result)
    with raw_tab:
        render_raw_table(result.raw)
    with history_tab:
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
    if errors:
        with st.expander(f"Non-fatal fetch errors ({len(errors)})"):
            st.dataframe(pd.DataFrame({"error": errors}), width=TABLE_WIDTH, hide_index=True)


def render_alert_tables(result: ScanResult) -> None:
    st.subheader("Funding Spread Alerts")
    st.dataframe(result.funding_alerts, width=TABLE_WIDTH, hide_index=True)

    st.subheader("Price Dispersion Alerts")
    st.dataframe(result.price_alerts, width=TABLE_WIDTH, hide_index=True)


def render_alert_details(result: ScanResult) -> None:
    st.subheader("Alert Details")
    alert_type = st.radio(
        "Alert type",
        ["Funding spread", "Price dispersion"],
        horizontal=True,
        key="alert_detail_type",
    )
    if alert_type == "Funding spread":
        render_funding_detail(result)
    else:
        render_price_detail(result)


def render_funding_detail(result: ScanResult) -> None:
    alerts = result.funding_alerts
    if alerts.empty:
        st.info("No funding-spread alerts in the current scan.")
        return

    idx = st.selectbox(
        "Funding alert",
        range(len(alerts)),
        format_func=lambda i: funding_label(alerts.iloc[i]),
        key="funding_alert_selector",
    )
    alert = alerts.iloc[idx]
    legs = leg_table(
        result.raw,
        [
            ("Long lowest funding", alert["long_exchange"], alert["long_symbol"]),
            ("Short highest funding", alert["short_exchange"], alert["short_symbol"]),
        ],
    )
    cost = cost_inputs("funding", gross_bps=as_float(alert["net_8h_bps"]))
    overview, legs_tab, checks_tab, raw_tab = st.tabs(["Overview", "Legs", "Execution checks", "Raw rows"])

    with overview:
        metric_row(
            [
                ("Base", str(alert["base"])),
                ("Net 8h funding", fmt_bps(alert["net_8h_bps"])),
                ("Gross APY", fmt_pct(alert["gross_apy_pct"])),
                ("Round-trip cost", fmt_bps(cost["round_trip_cost_bps"])),
                ("After cost", fmt_bps(cost["net_after_cost_bps"])),
            ]
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "long_leg": f"{alert['long_exchange']} {alert['long_symbol']}",
                        "short_leg": f"{alert['short_exchange']} {alert['short_symbol']}",
                        "long_mid": alert["long_mid"],
                        "short_mid": alert["short_mid"],
                        "estimated_after_cost_usd": cost["net_after_cost_usd"],
                        "break_even_funding_events": break_even_events(
                            cost["round_trip_cost_bps"], alert["net_8h_bps"]
                        ),
                    }
                ]
            ),
            width=TABLE_WIDTH,
            hide_index=True,
        )

    with legs_tab:
        st.dataframe(legs, width=TABLE_WIDTH, hide_index=True)
    with checks_tab:
        st.dataframe(funding_checks(alert, legs, cost), width=TABLE_WIDTH, hide_index=True)
    with raw_tab:
        raw_rows(result.raw, alert["base"])


def render_price_detail(result: ScanResult) -> None:
    alerts = result.price_alerts
    if alerts.empty:
        st.info("No price-dispersion alerts in the current scan.")
        return

    idx = st.selectbox(
        "Price alert",
        range(len(alerts)),
        format_func=lambda i: price_label(alerts.iloc[i]),
        key="price_alert_selector",
    )
    alert = alerts.iloc[idx]
    legs = leg_table(
        result.raw,
        [
            ("Long low price", alert["low_exchange"], alert["low_symbol"]),
            ("Short high price", alert["high_exchange"], alert["high_symbol"]),
        ],
    )
    funding_bps = as_float(alert["net_funding_8h_bps_if_long_low_short_high"])
    gross_bps = as_float(alert["price_dispersion_bps"]) + (0.0 if pd.isna(funding_bps) else funding_bps)
    cost = cost_inputs("price", gross_bps=gross_bps)
    overview, legs_tab, checks_tab, raw_tab = st.tabs(["Overview", "Legs", "Execution checks", "Raw rows"])

    with overview:
        metric_row(
            [
                ("Base", str(alert["base"])),
                ("Price dispersion", fmt_bps(alert["price_dispersion_bps"])),
                ("Funding impact", fmt_bps(alert["net_funding_8h_bps_if_long_low_short_high"])),
                ("Round-trip cost", fmt_bps(cost["round_trip_cost_bps"])),
                ("After cost", fmt_bps(cost["net_after_cost_bps"])),
            ]
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "long_low_leg": f"{alert['low_exchange']} {alert['low_symbol']}",
                        "short_high_leg": f"{alert['high_exchange']} {alert['high_symbol']}",
                        "low_mid": alert["low_mid"],
                        "high_mid": alert["high_mid"],
                        "estimated_after_cost_usd": cost["net_after_cost_usd"],
                    }
                ]
            ),
            width=TABLE_WIDTH,
            hide_index=True,
        )

    with legs_tab:
        st.dataframe(legs, width=TABLE_WIDTH, hide_index=True)
    with checks_tab:
        st.dataframe(price_checks(alert, legs, cost), width=TABLE_WIDTH, hide_index=True)
    with raw_tab:
        raw_rows(result.raw, alert["base"])


def cost_inputs(prefix: str, gross_bps: float) -> dict[str, float]:
    st.caption("Cost model")
    columns = st.columns(5)
    notional = columns[0].number_input("Target notional", min_value=0.0, value=10_000.0, step=1_000.0, key=f"{prefix}_notional")
    fee_bps = columns[1].number_input("Fee bps / leg / side", min_value=0.0, value=5.0, step=0.5, key=f"{prefix}_fee")
    entry_bps = columns[2].number_input("Entry slippage bps / leg", min_value=0.0, value=2.0, step=0.5, key=f"{prefix}_entry")
    exit_bps = columns[3].number_input("Exit slippage bps / leg", min_value=0.0, value=2.0, step=0.5, key=f"{prefix}_exit")
    extra_bps = columns[4].number_input("Extra cost bps", min_value=0.0, value=0.0, step=0.5, key=f"{prefix}_extra")
    round_trip_cost_bps = (fee_bps * 4) + (entry_bps * 2) + (exit_bps * 2) + extra_bps
    net_after_cost_bps = gross_bps - round_trip_cost_bps
    return {
        "round_trip_cost_bps": round_trip_cost_bps,
        "net_after_cost_bps": net_after_cost_bps,
        "net_after_cost_usd": notional * net_after_cost_bps / 10000,
    }


def leg_table(raw: pd.DataFrame, specs: list[tuple[str, str, str]]) -> pd.DataFrame:
    rows = []
    for role, exchange, symbol in specs:
        row = raw_match(raw, exchange, symbol)
        rows.append(
            {
                "role": role,
                "exchange": exchange,
                "symbol": symbol,
                "bid": raw_value(row, "bid"),
                "ask": raw_value(row, "ask"),
                "mid": raw_value(row, "mid"),
                "last": raw_value(row, "last"),
                "quote_volume_24h": raw_value(row, "quote_volume_24h"),
                "bid_ask_spread_bps": raw_value(row, "bid_ask_spread_bps"),
                "funding_8h_bps": raw_value(row, "funding_8h_bps"),
                "minutes_to_funding": raw_value(row, "minutes_to_funding"),
            }
        )
    return pd.DataFrame(rows)


def funding_checks(alert: pd.Series, legs: pd.DataFrame, cost: dict[str, float]) -> pd.DataFrame:
    checks = common_checks(legs, cost)
    checks.insert(3, check("Funding timestamps aligned", funding_times_aligned(legs), "Large timing gaps can erase the edge."))
    checks.append(manual("Same underlying risk", f"Confirm both {alert['base']} contracts reference the same exposure."))
    return pd.DataFrame(checks)


def price_checks(alert: pd.Series, legs: pd.DataFrame, cost: dict[str, float]) -> pd.DataFrame:
    checks = common_checks(legs, cost)
    checks.append(manual("Same underlying risk", f"Confirm both {alert['base']} contracts reference the same exposure."))
    return pd.DataFrame(checks)


def common_checks(legs: pd.DataFrame, cost: dict[str, float]) -> list[dict[str, str]]:
    return [
        check("Live bid/ask on both legs", live_bid_ask(legs), "Required before sizing orders."),
        check("Bid/ask spread below threshold", spreads_ok(legs), f"Threshold: {config.MAX_BID_ASK_SPREAD_BPS} bps."),
        check("24h quote volume above threshold", volumes_ok(legs), f"Threshold: {config.MIN_QUOTE_VOLUME:,.0f}."),
        check("Costs entered", cost["round_trip_cost_bps"] > 0, "Fees and slippage are one-time costs."),
        check("Expected edge remains after cost", cost["net_after_cost_bps"] > 0, "Negative values are not actionable."),
        manual("Order book depth supports target size", "Fetch book depth for the chosen notional."),
        manual("Mark/index prices are consistent", "Check mark, index, and premium on both venues."),
        manual("Contract specs match", "Check multiplier, margin coin, trading status, and limits."),
    ]


def render_raw_table(raw: pd.DataFrame) -> None:
    st.subheader("Raw Cross-Section")
    if raw.empty:
        st.dataframe(raw, width=TABLE_WIDTH, hide_index=True)
        return
    symbols = sorted(raw["base"].dropna().unique())
    selected = st.multiselect("Symbol filter", symbols, default=symbols)
    filtered = raw[raw["base"].isin(selected)] if selected else raw.iloc[0:0]
    st.dataframe(filtered, width=TABLE_WIDTH, hide_index=True)


def render_history() -> None:
    st.subheader("Local History")
    st.dataframe(load_history_tail(), width=TABLE_WIDTH, hide_index=True)


def raw_rows(raw: pd.DataFrame, base: str) -> None:
    st.dataframe(raw[raw["base"] == base] if not raw.empty else raw, width=TABLE_WIDTH, hide_index=True)


def metric_row(items: list[tuple[str, str]]) -> None:
    for column, (label, value) in zip(st.columns(len(items)), items):
        column.metric(label, value)


def raw_match(raw: pd.DataFrame, exchange: str, symbol: str) -> pd.Series:
    if raw.empty:
        return pd.Series(dtype=object)
    matches = raw[(raw["exchange"] == exchange) & (raw["symbol"] == symbol)]
    return matches.iloc[0] if not matches.empty else pd.Series(dtype=object)


def raw_value(row: pd.Series, column: str) -> Any:
    return pd.NA if row.empty or column not in row else row[column]


def live_bid_ask(legs: pd.DataFrame) -> bool:
    return bool((legs["bid"].notna() & legs["ask"].notna()).all())


def spreads_ok(legs: pd.DataFrame) -> bool:
    spreads = pd.to_numeric(legs["bid_ask_spread_bps"], errors="coerce")
    return bool(spreads.notna().all() and (spreads <= config.MAX_BID_ASK_SPREAD_BPS).all())


def volumes_ok(legs: pd.DataFrame) -> bool:
    volumes = pd.to_numeric(legs["quote_volume_24h"], errors="coerce")
    return bool(volumes.notna().all() and (volumes >= config.MIN_QUOTE_VOLUME).all())


def funding_times_aligned(legs: pd.DataFrame) -> bool:
    minutes = pd.to_numeric(legs["minutes_to_funding"], errors="coerce")
    return bool(minutes.notna().all() and (minutes.max() - minutes.min()) <= 15)


def check(name: str, passed: bool, note: str) -> dict[str, str]:
    return {"check": name, "status": "OK" if passed else "Needs review", "note": note}


def manual(name: str, note: str) -> dict[str, str]:
    return {"check": name, "status": "Manual required", "note": note}


def funding_label(row: pd.Series) -> str:
    return f"{row['base']}: long {row['long_exchange']} / short {row['short_exchange']} ({fmt_bps(row['net_8h_bps'])})"


def price_label(row: pd.Series) -> str:
    return f"{row['base']}: low {row['low_exchange']} / high {row['high_exchange']} ({fmt_bps(row['price_dispersion_bps'])})"


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def fmt_bps(value: Any) -> str:
    number = as_float(value)
    return "n/a" if pd.isna(number) else f"{number:,.2f} bps"


def fmt_pct(value: Any) -> str:
    number = as_float(value)
    return "n/a" if pd.isna(number) else f"{number:,.2f}%"


def break_even_events(cost_bps: float, net_bps: float) -> str:
    net = as_float(net_bps)
    return "n/a" if pd.isna(net) or net <= 0 else f"{cost_bps / net:.2f}"


if __name__ == "__main__":
    main()

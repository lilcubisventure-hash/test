"""Manual broker spot leg against public perp funding legs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
import streamlit as st

import config
from scanner import ScanResult, fetch_order_book_depth


TABLE_WIDTH = "stretch"
DEX_COLUMNS = [
    "venue_type",
    "exchange",
    "symbol",
    "base",
    "mid",
    "funding_8h_bps",
    "quote_volume_24h",
    "bid_ask_spread_bps",
    "minutes_to_funding",
    "funding_timestamp",
    "mark_price",
    "index_price",
    "premium_bps",
    "contract_size",
    "taker_fee_bps",
    "min_amount",
    "max_amount",
    "min_cost",
    "max_cost",
    "source",
]


@dataclass(frozen=True)
class SpotLeg:
    broker: str
    base: str
    side: str
    quantity: float
    spot_price: float
    target_notional: float
    broker_cost_bps: float
    financing_apy_pct: float
    min_funding_8h_bps: float
    include_cex: bool
    include_dex_feed: bool


def render_spot_hedge_panel(result: ScanResult) -> None:
    st.subheader("Broker Spot vs Perp Funding")
    spot_leg = spot_leg_controls(result.raw)
    candidates = build_spot_funding_candidates(result.raw, spot_leg)

    spot_tab, candidates_tab, detail_tab, dex_tab = st.tabs(
        ["Spot Leg", "Funding Legs", "Hedge Plan", "DEX Feed"]
    )
    with spot_tab:
        render_spot_leg_summary(spot_leg)
    with candidates_tab:
        st.dataframe(candidates, width=TABLE_WIDTH, hide_index=True)
    with detail_tab:
        render_selected_candidate(spot_leg, candidates)
    with dex_tab:
        render_dex_feed_status()


def spot_leg_controls(raw: pd.DataFrame) -> SpotLeg:
    stock_bases = sorted(base for base in config.SPOT_HEDGE_STOCK_BASES if base in config.TARGET_BASES)
    all_bases = sorted(set(config.TARGET_BASES))

    columns = st.columns(4)
    broker = columns[0].text_input("Broker", value=config.SPOT_HEDGE_DEFAULT_BROKER, key="spot_broker")
    base = columns[1].selectbox("Spot base", stock_bases or all_bases, key="spot_base")
    side = columns[2].selectbox("Spot side", ["Long spot", "Short spot"], key="spot_side")
    quantity = columns[3].number_input("Spot quantity", min_value=0.0, value=0.0, step=1.0, key="spot_quantity")

    default_price = default_spot_price(raw, str(base))
    columns = st.columns(5)
    spot_price = columns[0].number_input(
        "Broker spot mark",
        min_value=0.0,
        value=default_price,
        step=max(0.01, default_price * 0.001),
        key="spot_price",
    )
    spot_notional = quantity * spot_price
    target_default = spot_notional if spot_notional > 0 else config.DEFAULT_TARGET_NOTIONAL
    target_notional = columns[1].number_input(
        "Perp hedge notional",
        min_value=0.0,
        value=float(target_default),
        step=1_000.0,
        key="spot_hedge_notional",
    )
    broker_cost_bps = columns[2].number_input(
        "Broker cost bps",
        min_value=0.0,
        value=config.SPOT_HEDGE_DEFAULT_BROKER_COST_BPS,
        step=0.5,
        key="spot_broker_cost_bps",
    )
    financing_apy_pct = columns[3].number_input(
        "Financing/borrow APY %",
        min_value=0.0,
        value=config.SPOT_HEDGE_DEFAULT_FINANCING_APY_PCT,
        step=0.5,
        key="spot_financing_apy_pct",
    )
    min_funding_8h_bps = columns[4].number_input(
        "Min funding 8h bps",
        value=config.SPOT_HEDGE_MIN_FUNDING_8H_BPS,
        step=0.5,
        key="spot_min_funding_8h_bps",
    )

    source_columns = st.columns(2)
    include_cex = source_columns[0].checkbox("Use CEX public scan", value=True, key="spot_use_cex")
    include_dex_feed = source_columns[1].checkbox("Use local DEX funding feed", value=False, key="spot_use_dex")

    return SpotLeg(
        broker=str(broker).strip() or config.SPOT_HEDGE_DEFAULT_BROKER,
        base=str(base),
        side=str(side),
        quantity=float(quantity),
        spot_price=float(spot_price),
        target_notional=float(target_notional),
        broker_cost_bps=float(broker_cost_bps),
        financing_apy_pct=float(financing_apy_pct),
        min_funding_8h_bps=float(min_funding_8h_bps),
        include_cex=bool(include_cex),
        include_dex_feed=bool(include_dex_feed),
    )


def build_spot_funding_candidates(raw: pd.DataFrame, spot_leg: SpotLeg) -> pd.DataFrame:
    market_rows: list[pd.DataFrame] = []
    if spot_leg.include_cex and not raw.empty:
        cex = raw[raw["base"] == spot_leg.base].copy()
        if not cex.empty:
            cex["venue_type"] = "CEX"
            cex["source"] = "public ccxt scan"
            market_rows.append(cex)
    if spot_leg.include_dex_feed:
        dex = load_manual_dex_funding()
        dex = dex[dex["base"] == spot_leg.base] if not dex.empty else dex
        if not dex.empty:
            market_rows.append(dex)

    if not market_rows:
        return empty_candidate_frame()

    markets = pd.concat(market_rows, ignore_index=True, sort=False)
    rows = [candidate_row(row, spot_leg) for _, row in markets.iterrows()]
    candidates = pd.DataFrame(rows)
    if candidates.empty:
        return empty_candidate_frame()
    return candidates.sort_values(
        ["status_rank", "net_carry_apy_pct", "funding_carry_8h_bps"],
        ascending=[True, False, False],
    ).drop(columns=["status_rank"]).reset_index(drop=True)


def candidate_row(row: pd.Series, spot_leg: SpotLeg) -> dict[str, Any]:
    perp_side = hedge_perp_side(spot_leg.side)
    funding_8h_bps = as_float(row.get("funding_8h_bps"))
    funding_carry_8h_bps = funding_8h_bps if perp_side == "Short perp" else -funding_8h_bps
    funding_apy_pct = annualize_8h_bps(funding_carry_8h_bps)
    net_carry_apy_pct = funding_apy_pct - spot_leg.financing_apy_pct
    one_time_cost_bps = estimate_one_time_cost_bps(row, spot_leg)
    first_cycle_after_cost_bps = funding_carry_8h_bps - one_time_cost_bps
    basis_edge = basis_edge_bps(spot_leg.spot_price, as_float(row.get("mid")), perp_side)
    status, status_rank = candidate_status(row, spot_leg, funding_carry_8h_bps)

    return {
        "status": status,
        "venue_type": row.get("venue_type", "CEX"),
        "exchange": row.get("exchange"),
        "symbol": row.get("symbol"),
        "base": row.get("base"),
        "spot_side": spot_leg.side,
        "perp_side": perp_side,
        "spot_price": spot_leg.spot_price,
        "perp_mid": row.get("mid"),
        "basis_edge_bps": basis_edge,
        "raw_funding_8h_bps": funding_8h_bps,
        "funding_carry_8h_bps": funding_carry_8h_bps,
        "funding_carry_apy_pct": funding_apy_pct,
        "net_carry_apy_pct": net_carry_apy_pct,
        "first_cycle_after_cost_bps": first_cycle_after_cost_bps,
        "one_time_cost_bps": one_time_cost_bps,
        "expected_funding_8h_usd": spot_leg.target_notional * funding_carry_8h_bps / 10000,
        "target_notional": spot_leg.target_notional,
        "estimated_contract_amount": estimated_contract_amount(row, spot_leg.target_notional),
        "quote_volume_24h": row.get("quote_volume_24h"),
        "bid_ask_spread_bps": row.get("bid_ask_spread_bps"),
        "minutes_to_funding": row.get("minutes_to_funding"),
        "mark_price": row.get("mark_price"),
        "index_price": row.get("index_price"),
        "premium_bps": row.get("premium_bps"),
        "source": row.get("source", "public scan"),
        "status_rank": status_rank,
    }


def render_spot_leg_summary(spot_leg: SpotLeg) -> None:
    spot_notional = spot_leg.quantity * spot_leg.spot_price
    metric_columns = st.columns(4)
    metric_columns[0].metric("Broker", spot_leg.broker)
    metric_columns[1].metric("Spot leg", f"{spot_leg.side} {spot_leg.base}")
    metric_columns[2].metric("Spot notional", fmt_usd(spot_notional))
    metric_columns[3].metric("Perp hedge notional", fmt_usd(spot_leg.target_notional))
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "broker": spot_leg.broker,
                    "base": spot_leg.base,
                    "spot_side": spot_leg.side,
                    "spot_quantity": spot_leg.quantity,
                    "spot_price": spot_leg.spot_price,
                    "spot_notional": spot_notional,
                    "perp_hedge_notional": spot_leg.target_notional,
                    "broker_cost_bps": spot_leg.broker_cost_bps,
                    "financing_or_borrow_apy_pct": spot_leg.financing_apy_pct,
                    "min_funding_8h_bps": spot_leg.min_funding_8h_bps,
                }
            ]
        ),
        width=TABLE_WIDTH,
        hide_index=True,
    )


def render_selected_candidate(spot_leg: SpotLeg, candidates: pd.DataFrame) -> None:
    if candidates.empty:
        st.info("No funding legs found for the selected spot base and enabled sources.")
        return

    idx = st.selectbox(
        "Funding leg",
        range(len(candidates)),
        format_func=lambda i: candidate_label(candidates.iloc[i]),
        key="spot_candidate_selector",
    )
    selected = candidates.iloc[idx]
    metric_columns = st.columns(4)
    metric_columns[0].metric("Status", str(selected["status"]))
    metric_columns[1].metric("Funding carry", fmt_bps(selected["funding_carry_8h_bps"]))
    metric_columns[2].metric("Net carry APY", fmt_pct(selected["net_carry_apy_pct"]))
    metric_columns[3].metric("Expected 8h USD", fmt_usd(selected["expected_funding_8h_usd"]))

    plan = pd.DataFrame(
        [
            {
                "leg": "Broker spot",
                "venue": spot_leg.broker,
                "symbol": spot_leg.base,
                "side": spot_leg.side.replace(" spot", ""),
                "notional": spot_leg.quantity * spot_leg.spot_price,
                "execution": "manual",
            },
            {
                "leg": "Perp funding hedge",
                "venue": selected["exchange"],
                "symbol": selected["symbol"],
                "side": str(selected["perp_side"]).replace(" perp", ""),
                "notional": selected["target_notional"],
                "estimated_amount": selected["estimated_contract_amount"],
                "execution": "dry-run only",
            },
        ]
    )
    checks = selected_candidate_checks(selected)
    detail_tab, checks_tab, depth_tab = st.tabs(["Plan", "Checks", "Depth"])
    with detail_tab:
        st.dataframe(plan, width=TABLE_WIDTH, hide_index=True)
    with checks_tab:
        st.dataframe(checks, width=TABLE_WIDTH, hide_index=True)
    with depth_tab:
        render_candidate_depth(selected)


def render_candidate_depth(selected: pd.Series) -> None:
    if selected.get("venue_type") != "CEX":
        st.info("Depth fetch is only wired for CEX candidates from the public CCXT scan.")
        return
    if st.button("Fetch funding-leg depth", key="spot_fetch_depth", width=TABLE_WIDTH):
        cached_spot_depth.clear()
    side = "sell" if selected["perp_side"] == "Short perp" else "buy"
    depth = cached_spot_depth(
        str(selected["exchange"]),
        str(selected["symbol"]),
        float(selected["target_notional"]),
    )
    depth_frame = pd.DataFrame(depth)
    if not depth_frame.empty:
        depth_frame = depth_frame[depth_frame["side"] == side]
    st.dataframe(depth_frame, width=TABLE_WIDTH, hide_index=True)


@st.cache_data(ttl=20, show_spinner=False)
def cached_spot_depth(exchange: str, symbol: str, target_notional: float) -> list[dict[str, Any]]:
    return fetch_order_book_depth(exchange, symbol, target_notional, config.ORDER_BOOK_LIMIT)


def selected_candidate_checks(selected: pd.Series) -> pd.DataFrame:
    checks = [
        check_row(
            "Funding carry positive",
            as_float(selected.get("funding_carry_8h_bps")) > 0,
            "Funding direction should pay the perp hedge leg.",
            fmt_bps(selected.get("funding_carry_8h_bps")),
        ),
        check_row(
            "Funding clears threshold",
            as_float(selected.get("funding_carry_8h_bps")) >= config.SPOT_HEDGE_MIN_FUNDING_8H_BPS,
            "Configured minimum funding carry must be met.",
            fmt_bps(config.SPOT_HEDGE_MIN_FUNDING_8H_BPS),
        ),
        check_row(
            "First cycle after cost positive",
            as_float(selected.get("first_cycle_after_cost_bps")) > 0,
            "One 8h funding event should cover estimated one-time costs.",
            fmt_bps(selected.get("first_cycle_after_cost_bps")),
        ),
        check_row(
            "Venue liquidity present",
            as_float(selected.get("quote_volume_24h")) >= config.MIN_QUOTE_VOLUME,
            "CEX quote volume should clear the dashboard liquidity threshold.",
            fmt_number(selected.get("quote_volume_24h")),
        ),
        check_row(
            "Spread below threshold",
            as_float(selected.get("bid_ask_spread_bps")) <= config.MAX_BID_ASK_SPREAD_BPS,
            "Wide spreads can consume the funding edge.",
            fmt_bps(selected.get("bid_ask_spread_bps")),
        ),
        {
            "check": "Broker spot borrow/financing",
            "status": "Manual required",
            "value": "",
            "note": "Confirm actual broker borrow, financing, locate, tax, dividend, and settlement constraints.",
        },
    ]
    return pd.DataFrame(checks)


def render_dex_feed_status() -> None:
    exists = config.MANUAL_DEX_FUNDING_PATH.exists()
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "source": "local DEX funding feed",
                    "path": str(config.MANUAL_DEX_FUNDING_PATH),
                    "status": "Available" if exists else "Not configured",
                    "note": "CSV is optional and ignored by git. No wallet or private key is loaded.",
                }
            ]
        ),
        width=TABLE_WIDTH,
        hide_index=True,
    )
    st.code(",".join(DEX_COLUMNS), language="text")
    if exists:
        st.dataframe(load_manual_dex_funding(), width=TABLE_WIDTH, hide_index=True)


def load_manual_dex_funding() -> pd.DataFrame:
    if not config.MANUAL_DEX_FUNDING_PATH.exists():
        return pd.DataFrame(columns=DEX_COLUMNS)
    try:
        data = pd.read_csv(config.MANUAL_DEX_FUNDING_PATH)
    except pd.errors.ParserError:
        return pd.DataFrame(columns=DEX_COLUMNS + ["error"])
    for column in DEX_COLUMNS:
        if column not in data:
            data[column] = pd.NA
    data = data[DEX_COLUMNS].copy()
    data["venue_type"] = data["venue_type"].fillna("DEX")
    data["source"] = data["source"].fillna("manual dex funding feed")
    return data


def default_spot_price(raw: pd.DataFrame, base: str) -> float:
    if raw.empty or "base" not in raw:
        return 0.0
    prices = pd.to_numeric(raw.loc[raw["base"] == base, "mid"], errors="coerce").dropna()
    return float(prices.median()) if not prices.empty else 0.0


def hedge_perp_side(spot_side: str) -> str:
    return "Short perp" if spot_side == "Long spot" else "Long perp"


def estimate_one_time_cost_bps(row: pd.Series, spot_leg: SpotLeg) -> float:
    taker_fee = as_float(row.get("taker_fee_bps"))
    if pd.isna(taker_fee):
        taker_fee = config.FALLBACK_TAKER_FEE_BPS
    spread = as_float(row.get("bid_ask_spread_bps"))
    spread_cross = 0.0 if pd.isna(spread) else max(0.0, spread / 2)
    return spot_leg.broker_cost_bps + taker_fee + spread_cross


def estimated_contract_amount(row: pd.Series, target_notional: float) -> float:
    mid = as_float(row.get("mid"))
    contract_size = as_float(row.get("contract_size"))
    if pd.isna(contract_size) or contract_size <= 0:
        contract_size = 1.0
    if pd.isna(mid) or mid <= 0:
        return float("nan")
    return target_notional / (mid * contract_size)


def basis_edge_bps(spot_price: float, perp_mid: float, perp_side: str) -> float:
    if spot_price <= 0 or pd.isna(perp_mid) or perp_mid <= 0:
        return float("nan")
    raw_basis = (perp_mid / spot_price - 1) * 10000
    return raw_basis if perp_side == "Short perp" else -raw_basis


def candidate_status(row: pd.Series, spot_leg: SpotLeg, funding_carry_8h_bps: float) -> tuple[str, int]:
    if pd.isna(funding_carry_8h_bps):
        return "Needs funding data", 3
    if funding_carry_8h_bps < spot_leg.min_funding_8h_bps:
        return "Funding below threshold", 2
    spread = as_float(row.get("bid_ask_spread_bps"))
    volume = as_float(row.get("quote_volume_24h"))
    if not pd.isna(spread) and spread > config.MAX_BID_ASK_SPREAD_BPS:
        return "Spread review", 1
    if not pd.isna(volume) and volume < config.MIN_QUOTE_VOLUME:
        return "Liquidity review", 1
    return "Candidate", 0


def empty_candidate_frame() -> pd.DataFrame:
    columns = [
        "status",
        "venue_type",
        "exchange",
        "symbol",
        "base",
        "spot_side",
        "perp_side",
        "spot_price",
        "perp_mid",
        "basis_edge_bps",
        "raw_funding_8h_bps",
        "funding_carry_8h_bps",
        "funding_carry_apy_pct",
        "net_carry_apy_pct",
        "first_cycle_after_cost_bps",
        "one_time_cost_bps",
        "expected_funding_8h_usd",
        "target_notional",
        "estimated_contract_amount",
        "quote_volume_24h",
        "bid_ask_spread_bps",
        "minutes_to_funding",
        "mark_price",
        "index_price",
        "premium_bps",
        "source",
    ]
    return pd.DataFrame(columns=columns)


def candidate_label(row: pd.Series) -> str:
    return (
        f"{row['status']} | {row['venue_type']} {row['exchange']} {row['symbol']} "
        f"{row['perp_side']} {fmt_bps(row['funding_carry_8h_bps'])}"
    )


def check_row(name: str, passed: bool, note: str, value: str) -> dict[str, str]:
    return {"check": name, "status": "OK" if passed else "Needs review", "value": value, "note": note}


def annualize_8h_bps(value: float) -> float:
    return float("nan") if pd.isna(value) else value / 10000 * 3 * 365 * 100


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


def fmt_usd(value: Any) -> str:
    number = as_float(value)
    return "n/a" if pd.isna(number) else f"${number:,.2f}"


def fmt_number(value: Any) -> str:
    number = as_float(value)
    return "n/a" if pd.isna(number) else f"{number:,.6g}"

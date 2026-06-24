"""Public-market-data scanner for funding and price-dispersion anomalies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any

import ccxt
import pandas as pd

import config


RAW_COLUMNS = [
    "timestamp",
    "exchange",
    "symbol",
    "base",
    "bid",
    "ask",
    "mid",
    "last",
    "quote_volume_24h",
    "bid_ask_spread_bps",
    "raw_funding_rate",
    "funding_8h_bps",
    "interval_hours",
    "minutes_to_funding",
]

FUNDING_ALERT_COLUMNS = [
    "timestamp",
    "base",
    "long_exchange",
    "long_symbol",
    "long_funding_8h_bps",
    "short_exchange",
    "short_symbol",
    "short_funding_8h_bps",
    "net_8h_bps",
    "gross_apy_pct",
    "long_mid",
    "short_mid",
]

PRICE_ALERT_COLUMNS = [
    "timestamp",
    "base",
    "low_exchange",
    "low_symbol",
    "low_mid",
    "low_funding_8h_bps",
    "high_exchange",
    "high_symbol",
    "high_mid",
    "high_funding_8h_bps",
    "price_dispersion_bps",
    "net_funding_8h_bps_if_long_low_short_high",
]


@dataclass
class ScanResult:
    raw: pd.DataFrame
    funding_alerts: pd.DataFrame
    price_alerts: pd.DataFrame
    errors: list[str]


def run_scan() -> ScanResult:
    """Fetch all configured markets and calculate alert tables."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for exchange_spec in config.EXCHANGES:
        exchange_rows, exchange_errors = scan_exchange(exchange_spec, timestamp)
        rows.extend(exchange_rows)
        errors.extend(exchange_errors)

    raw = pd.DataFrame(rows, columns=RAW_COLUMNS)
    return build_scan_result(raw, errors)


def build_scan_result(raw: pd.DataFrame, errors: list[str] | None = None) -> ScanResult:
    """Calculate derived alert tables from a raw cross-section."""
    raw = raw.copy()
    for column in RAW_COLUMNS:
        if column not in raw:
            raw[column] = pd.NA
    raw = raw[RAW_COLUMNS]
    funding_alerts = calculate_funding_spreads(raw)
    price_alerts = calculate_price_dispersions(raw)
    return ScanResult(raw=raw, funding_alerts=funding_alerts, price_alerts=price_alerts, errors=errors or [])


def load_latest_snapshot() -> ScanResult | None:
    """Load the most recent local snapshot so the UI can render before a fresh scan."""
    path = config.LATEST_SNAPSHOT_PATH
    if not path.exists():
        return None

    raw = pd.read_csv(path)
    return build_scan_result(raw)


def scan_exchange(exchange_spec: dict[str, Any], timestamp: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Scan one exchange without allowing its failures to stop the whole run."""
    exchange_name = exchange_spec["name"]
    errors: list[str] = []
    rows: list[dict[str, Any]] = []

    try:
        exchange = build_exchange(exchange_spec)
        markets = exchange.load_markets()
    except Exception as exc:  # noqa: BLE001 - all exchange/API failures should be non-fatal.
        return [], [f"{exchange_name}: failed to load markets: {exc}"]

    matching_markets = [
        market
        for market in markets.values()
        if is_target_linear_swap_market(market)
    ]

    for market in matching_markets:
        symbol = market.get("symbol")
        base = normalize_base(market.get("base"))
        if not symbol or not base:
            continue

        ticker: dict[str, Any] = {}
        try:
            ticker = exchange.fetch_ticker(symbol)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{exchange_name} {symbol}: failed to fetch ticker: {exc}")
            continue

        funding: dict[str, Any] = {}
        try:
            funding = exchange.fetch_funding_rate(symbol)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{exchange_name} {symbol}: failed to fetch funding rate: {exc}")

        rows.append(build_row(timestamp, exchange_name, symbol, base, ticker, funding))

    return rows, errors


def build_exchange(exchange_spec: dict[str, Any]) -> ccxt.Exchange:
    exchange_class = getattr(ccxt, exchange_spec["id"])
    params = {
        "enableRateLimit": True,
        "options": exchange_spec.get("options", {}),
    }
    return exchange_class(params)


def is_target_linear_swap_market(market: dict[str, Any]) -> bool:
    base = normalize_base(market.get("base"))
    if base not in config.TARGET_BASES:
        return False
    if market.get("active") is False:
        return False
    if market.get("swap") is not True:
        return False
    return market.get("linear") is not False


def build_row(
    timestamp: str,
    exchange_name: str,
    symbol: str,
    base: str,
    ticker: dict[str, Any],
    funding: dict[str, Any],
) -> dict[str, Any]:
    bid = to_float(ticker.get("bid"))
    ask = to_float(ticker.get("ask"))
    last = to_float(ticker.get("last"))
    mid = calculate_mid(bid, ask, last)
    quote_volume = calculate_quote_volume(ticker, mid, last)
    bid_ask_spread_bps = calculate_bid_ask_spread_bps(bid, ask, mid)

    raw_funding_rate = to_float(funding.get("fundingRate"))
    interval_hours = parse_interval_hours(funding)
    funding_8h_bps = calculate_funding_8h_bps(raw_funding_rate, interval_hours)
    minutes_to_funding = calculate_minutes_to_funding(funding)

    return {
        "timestamp": timestamp,
        "exchange": exchange_name,
        "symbol": symbol,
        "base": base,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": last,
        "quote_volume_24h": quote_volume,
        "bid_ask_spread_bps": bid_ask_spread_bps,
        "raw_funding_rate": raw_funding_rate,
        "funding_8h_bps": funding_8h_bps,
        "interval_hours": interval_hours,
        "minutes_to_funding": minutes_to_funding,
    }


def calculate_funding_spreads(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=FUNDING_ALERT_COLUMNS)

    candidates = filter_alert_candidates(raw)
    candidates = candidates.dropna(subset=["funding_8h_bps"])
    rows: list[dict[str, Any]] = []

    for base, group in candidates.groupby("base", sort=True):
        if len(group) < 2:
            continue

        long_leg = group.loc[group["funding_8h_bps"].idxmin()]
        short_leg = group.loc[group["funding_8h_bps"].idxmax()]
        net_8h_bps = short_leg["funding_8h_bps"] - long_leg["funding_8h_bps"]
        if net_8h_bps < config.FUNDING_SPREAD_ALERT_BPS:
            continue

        rows.append(
            {
                "timestamp": latest_timestamp(group),
                "base": base,
                "long_exchange": long_leg["exchange"],
                "long_symbol": long_leg["symbol"],
                "long_funding_8h_bps": long_leg["funding_8h_bps"],
                "short_exchange": short_leg["exchange"],
                "short_symbol": short_leg["symbol"],
                "short_funding_8h_bps": short_leg["funding_8h_bps"],
                "net_8h_bps": net_8h_bps,
                "gross_apy_pct": net_8h_bps / 10000 * 3 * 365 * 100,
                "long_mid": long_leg["mid"],
                "short_mid": short_leg["mid"],
            }
        )

    return sort_alerts(pd.DataFrame(rows, columns=FUNDING_ALERT_COLUMNS), "net_8h_bps")


def calculate_price_dispersions(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=PRICE_ALERT_COLUMNS)

    candidates = filter_alert_candidates(raw)
    candidates = candidates.dropna(subset=["mid"])
    candidates = candidates[candidates["mid"] > 0]
    rows: list[dict[str, Any]] = []

    for base, group in candidates.groupby("base", sort=True):
        if len(group) < 2:
            continue

        low_leg = group.loc[group["mid"].idxmin()]
        high_leg = group.loc[group["mid"].idxmax()]
        price_dispersion_bps = (high_leg["mid"] / low_leg["mid"] - 1) * 10000
        if price_dispersion_bps < config.PRICE_DISPERSION_ALERT_BPS:
            continue

        rows.append(
            {
                "timestamp": latest_timestamp(group),
                "base": base,
                "low_exchange": low_leg["exchange"],
                "low_symbol": low_leg["symbol"],
                "low_mid": low_leg["mid"],
                "low_funding_8h_bps": low_leg["funding_8h_bps"],
                "high_exchange": high_leg["exchange"],
                "high_symbol": high_leg["symbol"],
                "high_mid": high_leg["mid"],
                "high_funding_8h_bps": high_leg["funding_8h_bps"],
                "price_dispersion_bps": price_dispersion_bps,
                "net_funding_8h_bps_if_long_low_short_high": (
                    high_leg["funding_8h_bps"] - low_leg["funding_8h_bps"]
                    if pd.notna(high_leg["funding_8h_bps"]) and pd.notna(low_leg["funding_8h_bps"])
                    else math.nan
                ),
            }
        )

    return sort_alerts(pd.DataFrame(rows, columns=PRICE_ALERT_COLUMNS), "price_dispersion_bps")


def filter_alert_candidates(raw: pd.DataFrame) -> pd.DataFrame:
    candidates = raw.copy()
    candidates = candidates[candidates["quote_volume_24h"].fillna(0) >= config.MIN_QUOTE_VOLUME]
    candidates = candidates[
        candidates["bid_ask_spread_bps"].fillna(math.inf) <= config.MAX_BID_ASK_SPREAD_BPS
    ]
    return candidates


def save_snapshot(raw: pd.DataFrame) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw.to_csv(config.LATEST_SNAPSHOT_PATH, index=False)

    if raw.empty:
        return

    header = not config.SNAPSHOTS_PATH.exists()
    raw.to_csv(config.SNAPSHOTS_PATH, mode="a", header=header, index=False)


def load_history_tail(limit: int = 200) -> pd.DataFrame:
    path = config.SNAPSHOTS_PATH
    if not path.exists():
        return pd.DataFrame(columns=RAW_COLUMNS)

    history = pd.read_csv(path)
    if history.empty:
        return history
    return history.tail(limit)


def sort_alerts(alerts: pd.DataFrame, column: str) -> pd.DataFrame:
    if alerts.empty:
        return alerts
    return alerts.sort_values(column, ascending=False).reset_index(drop=True)


def normalize_base(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip().upper()


def latest_timestamp(group: pd.DataFrame) -> str:
    return str(group["timestamp"].dropna().max())


def to_float(value: Any) -> float:
    if value in (None, ""):
        return math.nan
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def calculate_mid(bid: float, ask: float, last: float) -> float:
    if is_positive(bid) and is_positive(ask):
        return (bid + ask) / 2
    if is_positive(last):
        return last
    return math.nan


def calculate_quote_volume(ticker: dict[str, Any], mid: float, last: float) -> float:
    quote_volume = to_float(ticker.get("quoteVolume"))
    if not math.isnan(quote_volume):
        return quote_volume

    base_volume = to_float(ticker.get("baseVolume"))
    price = mid if not math.isnan(mid) else last
    if math.isnan(base_volume) or math.isnan(price):
        return math.nan
    return base_volume * price


def calculate_bid_ask_spread_bps(bid: float, ask: float, mid: float) -> float:
    if not (is_positive(bid) and is_positive(ask) and is_positive(mid)):
        return math.nan
    return (ask - bid) / mid * 10000


def calculate_funding_8h_bps(raw_funding_rate: float, interval_hours: float) -> float:
    if math.isnan(raw_funding_rate) or not is_positive(interval_hours):
        return math.nan
    return raw_funding_rate * (8 / interval_hours) * 10000


def parse_interval_hours(funding: dict[str, Any]) -> float:
    candidates = [
        funding.get("interval"),
        funding.get("fundingInterval"),
        nested_get(funding, "info", "interval"),
        nested_get(funding, "info", "fundingInterval"),
        nested_get(funding, "info", "fundingIntervalHours"),
    ]

    for candidate in candidates:
        parsed = parse_interval_value(candidate)
        if parsed is not None:
            return parsed

    return config.DEFAULT_FUNDING_INTERVAL_HOURS


def parse_interval_value(value: Any) -> float | None:
    if value in (None, ""):
        return None

    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        if numeric > 1000:
            return numeric / 60 / 60 / 1000
        return numeric

    text = str(value).strip().lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*([mhd])", text)
    if not match:
        return None

    amount = float(match.group(1))
    unit = match.group(2)
    if unit == "m":
        return amount / 60
    if unit == "h":
        return amount
    if unit == "d":
        return amount * 24
    return None


def calculate_minutes_to_funding(funding: dict[str, Any]) -> float:
    timestamp = (
        to_float(funding.get("nextFundingTimestamp"))
        if funding.get("nextFundingTimestamp") is not None
        else to_float(funding.get("fundingTimestamp"))
    )
    if math.isnan(timestamp):
        return math.nan

    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    return max(0.0, (timestamp - now_ms) / 60_000)


def nested_get(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def is_positive(value: float) -> bool:
    return not math.isnan(value) and value > 0


if __name__ == "__main__":
    result = run_scan()
    save_snapshot(result.raw)
    print(f"Fetched {len(result.raw)} rows")
    print(f"Funding alerts: {len(result.funding_alerts)}")
    print(f"Price alerts: {len(result.price_alerts)}")
    if result.errors:
        print(f"Non-fatal errors: {len(result.errors)}")

"""Public-market-data scanner for funding and price-dispersion anomalies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
from typing import Any

import ccxt
import pandas as pd

import config


RAW_COLUMNS = [
    "timestamp",
    "exchange_id",
    "exchange",
    "market_id",
    "symbol",
    "base",
    "quote",
    "settle",
    "contract_size",
    "linear",
    "inverse",
    "active",
    "maker_fee_bps",
    "taker_fee_bps",
    "min_amount",
    "max_amount",
    "min_cost",
    "max_cost",
    "amount_precision",
    "price_precision",
    "bid",
    "ask",
    "mid",
    "last",
    "quote_volume_24h",
    "bid_ask_spread_bps",
    "raw_funding_rate",
    "funding_8h_bps",
    "interval_hours",
    "funding_timestamp",
    "minutes_to_funding",
    "mark_price",
    "index_price",
    "premium_bps",
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
    matching_symbols = [market["symbol"] for market in matching_markets if market.get("symbol")]
    tickers, ticker_errors = fetch_public_tickers(exchange, matching_symbols)
    funding_rates, funding_errors = fetch_public_funding_rates(exchange, matching_symbols)
    errors.extend(f"{exchange_name}: {error}" for error in ticker_errors)
    errors.extend(f"{exchange_name}: {error}" for error in funding_errors)

    for market in matching_markets:
        symbol = market.get("symbol")
        base = normalize_base(market.get("base"))
        if not symbol or not base:
            continue

        ticker = tickers.get(symbol) or {}
        if not ticker:
            try:
                ticker = exchange.fetch_ticker(symbol)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{exchange_name} {symbol}: failed to fetch ticker: {exc}")
                continue

        funding = funding_rates.get(symbol) or {}
        funding_error = ""
        if not funding:
            funding, funding_error = fetch_public_funding(exchange, market, symbol, ticker)
        row_exchange_name = display_exchange_name(exchange_spec, market)
        if funding_error:
            errors.append(f"{row_exchange_name} {symbol}: failed to fetch funding rate: {funding_error}")

        rows.append(build_row(timestamp, exchange_spec["id"], row_exchange_name, market, symbol, base, ticker, funding))

    return rows, errors


def build_exchange(exchange_spec: dict[str, Any]) -> ccxt.Exchange:
    exchange_class = getattr(ccxt, exchange_spec["id"])
    params = {
        "enableRateLimit": True,
        "options": exchange_spec.get("options", {}),
    }
    return exchange_class(params)


def exchange_spec_by_name(exchange_name: str) -> dict[str, Any]:
    for exchange_spec in config.EXCHANGES:
        exchange_names = [exchange_spec["name"], *exchange_spec.get("aliases", [])]
        exchange_names.extend(rule.get("name") for rule in exchange_spec.get("display_name_rules", []))
        if exchange_name in exchange_names:
            return exchange_spec
    raise KeyError(f"Unknown exchange: {exchange_name}")


def display_exchange_name(exchange_spec: dict[str, Any], market: dict[str, Any]) -> str:
    market_base = str(market.get("base") or "").upper()
    for rule in exchange_spec.get("display_name_rules", []):
        base_prefix = rule.get("base_prefix")
        if base_prefix and market_base.startswith(str(base_prefix).upper()):
            return rule["name"]
    return exchange_spec["name"]


def fetch_public_funding(
    exchange: ccxt.Exchange,
    market: dict[str, Any],
    symbol: str,
    ticker: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    try:
        return exchange.fetch_funding_rate(symbol), ""
    except Exception as exc:  # noqa: BLE001
        fallback = public_funding_fallback(market, ticker)
        if fallback:
            return fallback, ""
        return {}, str(exc)


def fetch_public_tickers(exchange: ccxt.Exchange, symbols: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if not symbols or not exchange.has.get("fetchTickers"):
        return {}, []
    try:
        tickers = exchange.fetch_tickers(symbols)
    except Exception as exc:  # noqa: BLE001
        return {}, [f"failed to bulk fetch tickers: {exc}"]
    return {symbol: ticker for symbol, ticker in tickers.items() if symbol in symbols}, []


def fetch_public_funding_rates(exchange: ccxt.Exchange, symbols: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if not symbols or not exchange.has.get("fetchFundingRates"):
        return {}, []
    try:
        funding_rates = exchange.fetch_funding_rates(symbols)
    except Exception as exc:  # noqa: BLE001
        return {}, [f"failed to bulk fetch funding rates: {exc}"]
    return {symbol: funding for symbol, funding in funding_rates.items() if symbol in symbols}, []


def public_funding_fallback(market: dict[str, Any], ticker: dict[str, Any]) -> dict[str, Any]:
    """Extract public funding fields from ticker or market metadata when CCXT lacks a standard endpoint."""
    info_sources = [ticker.get("info"), market.get("info")]
    for info in info_sources:
        if not isinstance(info, dict):
            continue
        funding_rate = first_numeric(info, "funding", "fundingRate")
        if math.isnan(funding_rate):
            continue
        interval = "1h" if is_hyperliquid_info(info) else config.DEFAULT_FUNDING_INTERVAL_HOURS
        return {
            "fundingRate": funding_rate,
            "interval": interval,
            "fundingTimestamp": next_interval_timestamp_ms(interval),
            "markPrice": first_numeric(info, "markPx", "fairPrice"),
            "indexPrice": first_numeric(info, "oraclePx", "idxPrice", "indexPrice"),
            "info": info,
        }
    return {}


def is_hyperliquid_info(info: dict[str, Any]) -> bool:
    return "oraclePx" in info or "markPx" in info or bool(info.get("hip3"))


def next_interval_timestamp_ms(interval: Any) -> int | None:
    interval_hours = parse_interval_value(interval)
    if interval_hours != 1:
        return None
    now = datetime.now(timezone.utc)
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return int(next_hour.timestamp() * 1000)


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
    exchange_id: str,
    exchange_name: str,
    market: dict[str, Any],
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
    funding_timestamp = calculate_funding_timestamp(funding)
    minutes_to_funding = calculate_minutes_to_funding(funding)
    mark_price = funding_price(funding, "markPrice", "mark_price", "markPx", "fairPrice")
    index_price = funding_price(funding, "indexPrice", "index_price", "indexPx", "idxPrice", "oraclePx")
    premium_bps = calculate_premium_bps(mark_price, index_price)
    metadata = contract_metadata(market)

    return {
        "timestamp": timestamp,
        "exchange_id": exchange_id,
        "exchange": exchange_name,
        "market_id": market.get("id"),
        "symbol": symbol,
        "base": base,
        **metadata,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": last,
        "quote_volume_24h": quote_volume,
        "bid_ask_spread_bps": bid_ask_spread_bps,
        "raw_funding_rate": raw_funding_rate,
        "funding_8h_bps": funding_8h_bps,
        "interval_hours": interval_hours,
        "funding_timestamp": funding_timestamp,
        "minutes_to_funding": minutes_to_funding,
        "mark_price": mark_price,
        "index_price": index_price,
        "premium_bps": premium_bps,
    }


def contract_metadata(market: dict[str, Any]) -> dict[str, Any]:
    precision = market.get("precision") or {}
    limits = market.get("limits") or {}
    amount_limits = limits.get("amount") or {}
    cost_limits = limits.get("cost") or {}
    contract_size = to_float(market.get("contractSize"))

    return {
        "quote": market.get("quote"),
        "settle": market.get("settle"),
        "contract_size": contract_size,
        "linear": market.get("linear"),
        "inverse": market.get("inverse"),
        "active": market.get("active"),
        "maker_fee_bps": fee_to_bps(market.get("maker")),
        "taker_fee_bps": fee_to_bps(market.get("taker")),
        "min_amount": to_float(amount_limits.get("min")),
        "max_amount": to_float(amount_limits.get("max")),
        "min_cost": to_float(cost_limits.get("min")),
        "max_cost": to_float(cost_limits.get("max")),
        "amount_precision": precision.get("amount"),
        "price_precision": precision.get("price"),
    }


def fetch_order_book_depth(
    exchange_name: str,
    symbol: str,
    target_notional: float,
    limit: int = config.ORDER_BOOK_LIMIT,
) -> list[dict[str, Any]]:
    """Fetch public order book depth and estimate buy/sell slippage for one market."""
    exchange_spec = exchange_spec_by_name(exchange_name)
    try:
        exchange = build_exchange(exchange_spec)
        markets = exchange.load_markets()
        market = markets.get(symbol) or exchange.market(symbol)
        order_book = exchange.fetch_order_book(symbol, limit=limit)
    except Exception as exc:  # noqa: BLE001 - public exchange failures should be visible, not fatal.
        return [
            depth_error_row(exchange_name, symbol, "buy", target_notional, str(exc)),
            depth_error_row(exchange_name, symbol, "sell", target_notional, str(exc)),
        ]

    contract_size = to_float(market.get("contractSize"))
    if math.isnan(contract_size) or contract_size <= 0:
        contract_size = 1.0

    bid = first_level_price(order_book.get("bids"))
    ask = first_level_price(order_book.get("asks"))
    mid = calculate_mid(bid, ask, math.nan)
    return [
        analyze_depth_side(exchange_name, symbol, "buy", order_book.get("asks") or [], mid, contract_size, target_notional),
        analyze_depth_side(exchange_name, symbol, "sell", order_book.get("bids") or [], mid, contract_size, target_notional),
    ]


def analyze_depth_side(
    exchange_name: str,
    symbol: str,
    side: str,
    levels: list[list[float]],
    mid: float,
    contract_size: float,
    target_notional: float,
) -> dict[str, Any]:
    remaining = max(0.0, target_notional)
    filled_notional = 0.0
    filled_base = 0.0
    available_notional = 0.0
    levels_used = 0

    for raw_level in levels:
        if len(raw_level) < 2:
            continue
        price = to_float(raw_level[0])
        amount = to_float(raw_level[1])
        if not is_positive(price) or not is_positive(amount):
            continue

        level_base = amount * contract_size
        level_notional = price * level_base
        available_notional += level_notional
        if remaining <= 0:
            continue

        take_notional = min(level_notional, remaining)
        filled_notional += take_notional
        filled_base += take_notional / price
        remaining -= take_notional
        levels_used += 1

    avg_price = filled_notional / filled_base if filled_base > 0 else math.nan
    fill_ratio = filled_notional / target_notional if target_notional > 0 else math.nan
    slippage_bps = calculate_depth_slippage_bps(side, avg_price, mid)
    return {
        "exchange": exchange_name,
        "symbol": symbol,
        "side": side,
        "target_notional": target_notional,
        "available_notional": available_notional,
        "filled_notional": filled_notional,
        "fill_pct": fill_ratio * 100 if not math.isnan(fill_ratio) else math.nan,
        "avg_price": avg_price,
        "mid": mid,
        "slippage_bps": slippage_bps,
        "levels_used": levels_used,
        "status": "OK" if fill_ratio >= 1 else "Insufficient depth",
        "error": "",
    }


def depth_error_row(exchange_name: str, symbol: str, side: str, target_notional: float, error: str) -> dict[str, Any]:
    return {
        "exchange": exchange_name,
        "symbol": symbol,
        "side": side,
        "target_notional": target_notional,
        "available_notional": math.nan,
        "filled_notional": math.nan,
        "fill_pct": math.nan,
        "avg_price": math.nan,
        "mid": math.nan,
        "slippage_bps": math.nan,
        "levels_used": 0,
        "status": "Error",
        "error": error,
    }


def first_level_price(levels: list[list[float]] | None) -> float:
    if not levels:
        return math.nan
    return to_float(levels[0][0]) if levels[0] else math.nan


def calculate_depth_slippage_bps(side: str, avg_price: float, mid: float) -> float:
    if not is_positive(avg_price) or not is_positive(mid):
        return math.nan
    if side == "buy":
        return max(0.0, (avg_price / mid - 1) * 10000)
    return max(0.0, (mid / avg_price - 1) * 10000)


def fee_to_bps(value: Any) -> float:
    fee = to_float(value)
    return math.nan if math.isnan(fee) else fee * 10000


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

    append = snapshot_schema_matches(raw)
    raw.to_csv(config.SNAPSHOTS_PATH, mode="a" if append else "w", header=not append, index=False)


def load_history_tail(limit: int = 200) -> pd.DataFrame:
    path = config.SNAPSHOTS_PATH
    if not path.exists():
        return pd.DataFrame(columns=RAW_COLUMNS)

    try:
        history = pd.read_csv(path)
    except pd.errors.ParserError:
        return load_latest_raw()
    if history.empty:
        return history
    return history.tail(limit)


def snapshot_schema_matches(raw: pd.DataFrame) -> bool:
    path = config.SNAPSHOTS_PATH
    if not path.exists():
        return False
    try:
        existing_columns = list(pd.read_csv(path, nrows=0).columns)
    except pd.errors.ParserError:
        return False
    return existing_columns == list(raw.columns)


def load_latest_raw() -> pd.DataFrame:
    path = config.LATEST_SNAPSHOT_PATH
    if not path.exists():
        return pd.DataFrame(columns=RAW_COLUMNS)
    return pd.read_csv(path)


def sort_alerts(alerts: pd.DataFrame, column: str) -> pd.DataFrame:
    if alerts.empty:
        return alerts
    return alerts.sort_values(column, ascending=False).reset_index(drop=True)


def normalize_base(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in config.TARGET_BASES:
        return text
    for prefix in config.BASE_PREFIX_ALIASES:
        prefix = prefix.upper()
        if text.startswith(prefix):
            candidate = text.removeprefix(prefix)
            if candidate in config.TARGET_BASES:
                return candidate
    for suffix in config.BASE_SUFFIX_ALIASES:
        suffix = suffix.upper()
        if text.endswith(suffix):
            candidate = text.removesuffix(suffix)
            if candidate in config.TARGET_BASES:
                return candidate
    return text


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


def first_numeric(data: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = to_float(data.get(key))
        if not math.isnan(value):
            return value
    return math.nan


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
    timestamp = calculate_funding_timestamp(funding)
    if math.isnan(timestamp):
        return math.nan

    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    return max(0.0, (timestamp - now_ms) / 60_000)


def calculate_funding_timestamp(funding: dict[str, Any]) -> float:
    candidates = [
        funding.get("nextFundingTimestamp"),
        funding.get("fundingTimestamp"),
        funding.get("timestamp"),
        nested_get(funding, "info", "nextFundingTimestamp"),
        nested_get(funding, "info", "nextFundingTime"),
        nested_get(funding, "info", "fundingTimestamp"),
        nested_get(funding, "info", "fundingTime"),
        nested_get(funding, "info", "fundingRateTimestamp"),
    ]
    for candidate in candidates:
        timestamp = to_float(candidate)
        if not math.isnan(timestamp):
            return timestamp
    return math.nan


def funding_price(funding: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = to_float(funding.get(key))
        if not math.isnan(value):
            return value
        value = to_float(nested_get(funding, "info", key))
        if not math.isnan(value):
            return value
    return math.nan


def calculate_premium_bps(mark_price: float, index_price: float) -> float:
    if not (is_positive(mark_price) and is_positive(index_price)):
        return math.nan
    return (mark_price / index_price - 1) * 10000


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

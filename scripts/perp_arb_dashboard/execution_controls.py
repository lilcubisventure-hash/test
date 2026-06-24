"""Dry-run execution controls for the perp arbitrage dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

import pandas as pd
import streamlit as st

import config
from scanner import ScanResult


TABLE_WIDTH = "stretch"


def render_operations_panel(result: ScanResult) -> None:
    st.subheader("Operations")
    columns = st.columns(4)
    columns[0].metric("Execution mode", "Dry-run")
    columns[1].metric("Dedup window", f"{config.ALERT_DEDUP_MINUTES} min")
    columns[2].metric("Max hold", f"{config.MAX_HOLD_HOURS:g} h")
    columns[3].metric("Max loss", f"{config.MAX_LOSS_BPS:g} bps")

    account_tab, monitoring_tab, api_tab, audit_tab = st.tabs(
        ["Private account", "Monitoring", "API isolation", "Audit log"]
    )
    with account_tab:
        st.dataframe(private_account_status(), width=TABLE_WIDTH, hide_index=True)
    with monitoring_tab:
        st.dataframe(monitoring_status(), width=TABLE_WIDTH, hide_index=True)
        st.subheader("Current Alert Dedup Keys")
        st.dataframe(alert_dedup_table(result), width=TABLE_WIDTH, hide_index=True)
    with api_tab:
        st.dataframe(api_key_isolation(), width=TABLE_WIDTH, hide_index=True)
    with audit_tab:
        st.dataframe(load_audit_log_tail(), width=TABLE_WIDTH, hide_index=True)


def render_signal_lifecycle(alert_type: str, base: str, legs: pd.DataFrame, cost: dict[str, Any]) -> None:
    action = lifecycle_action(legs, cost)
    metric_row(
        [
            ("Signal", action["state"]),
            ("After cost", fmt_bps(cost["net_after_cost_bps"])),
            ("Open threshold", fmt_bps(config.OPEN_EDGE_THRESHOLD_BPS)),
            ("Close threshold", fmt_bps(config.CLOSE_EDGE_THRESHOLD_BPS)),
        ]
    )
    st.dataframe(signal_lifecycle_rows(alert_type, base, legs, cost), width=TABLE_WIDTH, hide_index=True)


def render_pre_trade_panel(
    legs: pd.DataFrame,
    depth: pd.DataFrame,
    cost: dict[str, Any],
    target_notional: float,
) -> None:
    st.dataframe(pre_trade_checks(legs, depth, cost, target_notional), width=TABLE_WIDTH, hide_index=True)
    st.subheader("Estimated Order Sizing")
    st.dataframe(dry_run_order_plan("Pre-trade", "", legs, target_notional), width=TABLE_WIDTH, hide_index=True)


def render_dry_run_panel(
    alert_type: str,
    base: str,
    legs: pd.DataFrame,
    depth: pd.DataFrame,
    cost: dict[str, Any],
    target_notional: float,
) -> None:
    plan = dry_run_order_plan(alert_type, base, legs, target_notional)
    circuits = circuit_breaker_checks(legs, depth, cost)
    blocked = circuit_blocked(circuits)
    metric_row(
        [
            ("Mode", "Dry-run"),
            ("Circuit", "Blocked" if blocked else "Clear"),
            ("After cost", fmt_bps(cost["net_after_cost_bps"])),
            ("API scope", "trade_disabled"),
        ]
    )
    st.subheader("Order Plan")
    st.dataframe(plan, width=TABLE_WIDTH, hide_index=True)
    st.subheader("Circuit Breaker")
    st.dataframe(pd.DataFrame(circuits), width=TABLE_WIDTH, hide_index=True)
    if st.button("Record dry-run audit", key=f"audit_{safe_key(alert_type, base, legs)}", width=TABLE_WIDTH):
        path = record_dry_run_audit(alert_type, base, plan, circuits, cost, target_notional)
        st.success(f"Recorded dry-run audit: {path}")


def signal_lifecycle_rows(
    alert_type: str,
    base: str,
    legs: pd.DataFrame,
    cost: dict[str, Any],
) -> pd.DataFrame:
    edge = as_float(cost["net_after_cost_bps"])
    blackout = funding_blackout_active(legs)
    min_minutes = min_minutes_to_funding(legs)
    action = lifecycle_action(legs, cost)
    rows = [
        status_row("Alert type", "Info", alert_type, base),
        status_row("Lifecycle state", action["status"], action["note"], action["state"]),
        status_row(
            "Open threshold",
            "OK" if edge >= config.OPEN_EDGE_THRESHOLD_BPS and blackout is not True else "Wait",
            "Requires after-cost edge above threshold and outside funding blackout.",
            fmt_bps(config.OPEN_EDGE_THRESHOLD_BPS),
        ),
        status_row(
            "Close threshold",
            "Close candidate" if edge <= config.CLOSE_EDGE_THRESHOLD_BPS else "Hold/Watch",
            "Existing positions should be reviewed when after-cost edge decays below this level.",
            fmt_bps(config.CLOSE_EDGE_THRESHOLD_BPS),
        ),
        status_row(
            "Funding entry blackout",
            funding_blackout_status(blackout),
            "New entries are blocked inside the configured pre-funding window.",
            "n/a" if pd.isna(min_minutes) else f"{min_minutes:.1f} min to funding",
        ),
        status_row(
            "Max hold time",
            "Not tracked",
            "Private position open time is not connected yet.",
            f"{config.MAX_HOLD_HOURS:g} h",
        ),
        status_row(
            "Max loss",
            "Block" if edge <= -config.MAX_LOSS_BPS else "OK",
            "Dry-run circuit breaker blocks if after-cost edge breaches the configured loss guard.",
            fmt_bps(-config.MAX_LOSS_BPS),
        ),
    ]
    return pd.DataFrame(rows)


def lifecycle_action(legs: pd.DataFrame, cost: dict[str, Any]) -> dict[str, str]:
    edge = as_float(cost["net_after_cost_bps"])
    blackout = funding_blackout_active(legs)
    if edge <= -config.MAX_LOSS_BPS:
        return {"state": "Blocked", "status": "Block", "note": "After-cost edge is below the max-loss guard."}
    if blackout is True:
        return {"state": "Wait", "status": "Wait", "note": "Signal is inside the funding pre-entry blackout window."}
    if edge >= config.OPEN_EDGE_THRESHOLD_BPS:
        return {"state": "Open candidate", "status": "OK", "note": "After-cost edge clears the open threshold."}
    if edge <= config.CLOSE_EDGE_THRESHOLD_BPS:
        return {"state": "Close/Avoid candidate", "status": "Close candidate", "note": "After-cost edge is below the close threshold."}
    return {"state": "Watch", "status": "Watch", "note": "Signal is between open and close thresholds."}


def pre_trade_checks(
    legs: pd.DataFrame,
    depth: pd.DataFrame,
    cost: dict[str, Any],
    target_notional: float,
) -> pd.DataFrame:
    blackout = funding_blackout_active(legs)
    rows = [
        status_row(
            "Mark/index/premium present",
            "OK" if mark_index_present(legs) else "Needs review",
            "Requires mark price, index price, and premium from public funding metadata.",
            mark_index_summary(legs),
        ),
        status_row(
            "Premium within guardrail",
            "OK" if premium_within_guardrail(legs) else "Needs review",
            f"Absolute premium should stay within {config.MAX_MARK_INDEX_PREMIUM_BPS:g} bps before execution.",
            premium_summary(legs),
        ),
        status_row(
            "Funding timestamp present",
            "OK" if funding_timestamp_present(legs) else "Needs review",
            "Needed to avoid entering too close to the next funding event.",
            funding_time_summary(legs),
        ),
        status_row(
            "Funding timestamp outside blackout",
            funding_blackout_status(blackout),
            "New entries are blocked inside the pre-funding window.",
            funding_minutes_summary(legs),
        ),
    ]
    rows.extend(order_size_checks(legs, target_notional))
    rows.extend(
        [
            status_row(
                "Post-only flag",
                "Dry-run only",
                "Entry simulation uses marketable depth; live post-only routing is not enabled.",
                "post_only=false",
            ),
            status_row(
                "Reduce-only flag",
                "Dry-run only",
                "Entry simulation is reduce_only=false; live exits must set reduce_only=true after positions exist.",
                "entry=false / exit=true",
            ),
            status_row(
                "Order book depth",
                "OK" if depth_supports_target(depth) else "Block",
                "Target notional must be fillable on all entry and exit sides.",
                depth_status_summary(depth),
            ),
            status_row(
                "Circuit breaker",
                "Block" if circuit_blocked(circuit_breaker_checks(legs, depth, cost)) else "OK",
                "Dry-run circuit breaker combines edge, depth, contract, and blackout gates.",
                fmt_bps(cost["net_after_cost_bps"]),
            ),
        ]
    )
    return pd.DataFrame(rows)


def dry_run_order_plan(alert_type: str, base: str, legs: pd.DataFrame, target_notional: float) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, leg in legs.iterrows():
        rows.append(
            {
                "alert_type": alert_type,
                "base": base,
                "role": leg["role"],
                "exchange": leg["exchange"],
                "symbol": leg["symbol"],
                "side": entry_side(str(leg["role"])),
                "order_type": "market simulation",
                "target_notional": target_notional,
                "estimated_amount": estimated_amount(leg, target_notional),
                "mid": leg.get("mid"),
                "post_only": False,
                "reduce_only": False,
                "api_scope": "trade_disabled",
                "execution": "dry-run",
            }
        )
    return pd.DataFrame(rows)


def circuit_breaker_checks(legs: pd.DataFrame, depth: pd.DataFrame, cost: dict[str, Any]) -> list[dict[str, str]]:
    edge = as_float(cost["net_after_cost_bps"])
    blackout = funding_blackout_active(legs)
    return [
        status_row("Dry-run enforcement", "OK" if config.DRY_RUN_ONLY else "Block", "The dashboard is configured to simulate only.", "dry_run_only=true"),
        status_row("API write access", "OK", "No private trading key is loaded by this app.", "trade_disabled"),
        status_row(
            "Max-loss guard",
            "Block" if edge <= config.CIRCUIT_MAX_NEGATIVE_EDGE_BPS else "OK",
            "Blocks if after-cost edge breaches the configured negative edge guard.",
            fmt_bps(config.CIRCUIT_MAX_NEGATIVE_EDGE_BPS),
        ),
        status_row("Depth requirement", "OK" if depth_supports_target(depth) else "Block", "All entry and exit sides must support the target notional.", depth_status_summary(depth)),
        status_row(
            "Contract requirement",
            "OK" if contract_checks_passed(legs) else "Block",
            "Contract metadata, active linear status, quote, settle, size, and fee checks must pass.",
            "required" if config.CIRCUIT_REQUIRE_CONTRACT_CHECKS else "advisory",
        ),
        status_row("Funding blackout", funding_blackout_status(blackout), "Blocks new entry when inside the configured funding pre-entry window.", funding_minutes_summary(legs)),
    ]


def private_account_status() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"field": "balance", "value": "", "status": "Not connected", "source": "private API disabled"},
            {"field": "margin", "value": "", "status": "Not connected", "source": "private API disabled"},
            {"field": "positions", "value": "", "status": "Not connected", "source": "private API disabled"},
            {"field": "order_capacity", "value": "", "status": "Not connected", "source": "private API disabled"},
        ]
    )


def monitoring_status() -> pd.DataFrame:
    rows = [
        {"component": "auto_refresh", "status": "Available", "value": f"{config.AUTO_REFRESH_SECONDS}s default", "note": "Sidebar toggle controls local scan reruns."},
        {"component": "alert_dedup", "status": "Available", "value": f"{config.ALERT_DEDUP_MINUTES} min", "note": "Dedup keys are derived from alert type, base, venues, and symbols."},
    ]
    rows.extend(
        {"component": channel.lower(), "status": "Not connected", "value": "", "note": "No external notification sender is configured."}
        for channel in config.NOTIFICATION_CHANNELS
    )
    return pd.DataFrame(rows)


def api_key_isolation() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"scope": "read_only", "status": "No private key required", "allowed": "public market data", "blocked": "account state, orders, withdrawals"},
            {"scope": "notify_only", "status": "Not connected", "allowed": "future alert messages", "blocked": "exchange trading"},
            {"scope": "trade_disabled", "status": "Enforced", "allowed": "dry-run audit records", "blocked": "create/cancel/order amend"},
        ]
    )


def alert_dedup_table(result: ScanResult) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, row in result.funding_alerts.iterrows():
        rows.append(
            {
                "alert_type": "Funding spread",
                "base": row["base"],
                "dedup_key": f"funding:{row['base']}:{row['long_exchange']}:{row['long_symbol']}:{row['short_exchange']}:{row['short_symbol']}",
                "edge_bps": row["net_8h_bps"],
                "first_seen": row["timestamp"],
                "dedup_until": dedup_expires_at(row["timestamp"]),
                "notification_status": "not sent",
            }
        )
    for _, row in result.price_alerts.iterrows():
        rows.append(
            {
                "alert_type": "Price dispersion",
                "base": row["base"],
                "dedup_key": f"price:{row['base']}:{row['low_exchange']}:{row['low_symbol']}:{row['high_exchange']}:{row['high_symbol']}",
                "edge_bps": row["price_dispersion_bps"],
                "first_seen": row["timestamp"],
                "dedup_until": dedup_expires_at(row["timestamp"]),
                "notification_status": "not sent",
            }
        )
    columns = ["alert_type", "base", "dedup_key", "edge_bps", "first_seen", "dedup_until", "notification_status"]
    return pd.DataFrame(rows, columns=columns)


def load_audit_log_tail(limit: int = 100) -> pd.DataFrame:
    columns = [
        "timestamp",
        "alert_type",
        "base",
        "target_notional",
        "edge_after_cost_bps",
        "round_trip_cost_bps",
        "circuit_status",
        "order_plan",
        "circuit_checks",
    ]
    if not config.AUDIT_LOG_PATH.exists():
        return pd.DataFrame(columns=columns)
    try:
        return pd.read_csv(config.AUDIT_LOG_PATH).tail(limit).reset_index(drop=True)
    except pd.errors.ParserError:
        return pd.DataFrame([{"timestamp": "", "alert_type": "audit log parse error"}])


def record_dry_run_audit(
    alert_type: str,
    base: str,
    plan: pd.DataFrame,
    circuits: list[dict[str, str]],
    cost: dict[str, Any],
    target_notional: float,
) -> str:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "alert_type": alert_type,
        "base": base,
        "target_notional": target_notional,
        "edge_after_cost_bps": cost["net_after_cost_bps"],
        "round_trip_cost_bps": cost["round_trip_cost_bps"],
        "circuit_status": "blocked" if circuit_blocked(circuits) else "clear",
        "order_plan": json.dumps(plan.to_dict("records"), default=str),
        "circuit_checks": json.dumps(circuits, default=str),
    }
    append = config.AUDIT_LOG_PATH.exists()
    pd.DataFrame([record]).to_csv(config.AUDIT_LOG_PATH, mode="a" if append else "w", header=not append, index=False)
    return str(config.AUDIT_LOG_PATH)


def order_size_checks(legs: pd.DataFrame, target_notional: float) -> list[dict[str, str]]:
    min_status, min_note, min_value = amount_bound_status(legs, target_notional, "min")
    max_status, max_note, max_value = amount_bound_status(legs, target_notional, "max")
    min_cost_status, min_cost_note, min_cost_value = cost_bound_status(legs, target_notional, "min")
    max_cost_status, max_cost_note, max_cost_value = cost_bound_status(legs, target_notional, "max")
    return [
        status_row("Min order size", min_status, min_note, min_value),
        status_row("Max order size", max_status, max_note, max_value),
        status_row("Min order cost", min_cost_status, min_cost_note, min_cost_value),
        status_row("Max order cost", max_cost_status, max_cost_note, max_cost_value),
    ]


def amount_bound_status(legs: pd.DataFrame, target_notional: float, bound: str) -> tuple[str, str, str]:
    column = f"{bound}_amount"
    missing: list[str] = []
    failures: list[str] = []
    details: list[str] = []
    for _, leg in legs.iterrows():
        amount = estimated_amount(leg, target_notional)
        limit = as_float(leg.get(column))
        details.append(f"{leg['exchange']} amount {fmt_number(amount)} vs {bound} {fmt_number(limit)}")
        if pd.isna(amount) or pd.isna(limit):
            missing.append(str(leg["exchange"]))
        elif bound == "min" and amount < limit:
            failures.append(str(leg["exchange"]))
        elif bound == "max" and amount > limit:
            failures.append(str(leg["exchange"]))

    if failures:
        return "Block", f"Order amount violates {bound} amount on: {', '.join(failures)}.", "; ".join(details)
    if missing:
        return "Needs review", f"{bound.title()} amount metadata missing on: {', '.join(missing)}.", "; ".join(details)
    return "OK", f"Estimated order amount satisfies {bound} amount metadata.", "; ".join(details)


def cost_bound_status(legs: pd.DataFrame, target_notional: float, bound: str) -> tuple[str, str, str]:
    column = f"{bound}_cost"
    missing: list[str] = []
    failures: list[str] = []
    details: list[str] = []
    for _, leg in legs.iterrows():
        limit = as_float(leg.get(column))
        details.append(f"{leg['exchange']} notional {fmt_number(target_notional)} vs {bound} {fmt_number(limit)}")
        if pd.isna(limit):
            missing.append(str(leg["exchange"]))
        elif bound == "min" and target_notional < limit:
            failures.append(str(leg["exchange"]))
        elif bound == "max" and target_notional > limit:
            failures.append(str(leg["exchange"]))

    if failures:
        return "Block", f"Target notional violates {bound} cost on: {', '.join(failures)}.", "; ".join(details)
    if missing:
        return "Needs review", f"{bound.title()} cost metadata missing on: {', '.join(missing)}.", "; ".join(details)
    return "OK", f"Target notional satisfies {bound} cost metadata.", "; ".join(details)


def min_minutes_to_funding(legs: pd.DataFrame) -> float:
    if "minutes_to_funding" not in legs:
        return float("nan")
    minutes = pd.to_numeric(legs["minutes_to_funding"], errors="coerce").dropna()
    return float(minutes.min()) if not minutes.empty else float("nan")


def funding_blackout_active(legs: pd.DataFrame) -> bool | None:
    minimum = min_minutes_to_funding(legs)
    if pd.isna(minimum):
        return None
    return minimum <= config.FUNDING_ENTRY_BLACKOUT_MINUTES


def funding_blackout_status(blackout: bool | None) -> str:
    if blackout is None:
        return "Needs review"
    return "Block" if blackout else "OK"


def mark_index_present(legs: pd.DataFrame) -> bool:
    return required_values_present(legs, ["mark_price", "index_price", "premium_bps"])


def premium_within_guardrail(legs: pd.DataFrame) -> bool:
    if "premium_bps" not in legs:
        return False
    premiums = pd.to_numeric(legs["premium_bps"], errors="coerce")
    return bool(premiums.notna().all() and (premiums.abs() <= config.MAX_MARK_INDEX_PREMIUM_BPS).all())


def funding_timestamp_present(legs: pd.DataFrame) -> bool:
    return required_values_present(legs, ["funding_timestamp", "minutes_to_funding"])


def contract_checks_passed(legs: pd.DataFrame) -> bool:
    specs_present = required_values_present(legs, ["quote", "settle", "contract_size", "linear", "active"])
    return bool(
        specs_present
        and (legs["active"] == True).all()  # noqa: E712
        and (legs["linear"] == True).all()  # noqa: E712
        and same_known_value(legs, "quote")
        and same_known_value(legs, "settle")
        and same_numeric_value(legs, "contract_size")
        and required_values_present(legs, ["taker_fee_bps"])
    )


def depth_supports_target(depth: pd.DataFrame) -> bool:
    return not depth.empty and bool((depth["status"] == "OK").all())


def mark_index_summary(legs: pd.DataFrame) -> str:
    values = []
    for _, leg in legs.iterrows():
        values.append(
            f"{leg['exchange']} {fmt_number(leg.get('mark_price'))}/{fmt_number(leg.get('index_price'))}"
            f" premium {fmt_bps(leg.get('premium_bps'))}"
        )
    return "; ".join(values)


def premium_summary(legs: pd.DataFrame) -> str:
    if "premium_bps" not in legs:
        return "n/a"
    premiums = pd.to_numeric(legs["premium_bps"], errors="coerce").dropna()
    if premiums.empty:
        return "n/a"
    return f"max abs {premiums.abs().max():.2f} bps"


def funding_time_summary(legs: pd.DataFrame) -> str:
    if "funding_timestamp" not in legs:
        return "n/a"
    return "; ".join(
        f"{leg['exchange']} {timestamp_ms_to_iso(leg.get('funding_timestamp'))}"
        for _, leg in legs.iterrows()
    )


def funding_minutes_summary(legs: pd.DataFrame) -> str:
    minimum = min_minutes_to_funding(legs)
    if pd.isna(minimum):
        return "n/a"
    return f"min {minimum:.1f} min; blackout {config.FUNDING_ENTRY_BLACKOUT_MINUTES:g} min"


def depth_status_summary(depth: pd.DataFrame) -> str:
    if depth.empty or "status" not in depth:
        return "no depth"
    counts = depth["status"].value_counts(dropna=False)
    return ", ".join(f"{status}: {count}" for status, count in counts.items())


def estimated_amount(leg: pd.Series, target_notional: float) -> float:
    mid = as_float(leg.get("mid"))
    contract_size = as_float(leg.get("contract_size"))
    if pd.isna(contract_size) or contract_size <= 0:
        contract_size = 1.0
    if pd.isna(mid) or mid <= 0:
        return float("nan")
    return target_notional / (mid * contract_size)


def entry_side(role: str) -> str:
    return "buy" if str(role).lower().startswith("long") else "sell"


def circuit_blocked(rows: list[dict[str, str]]) -> bool:
    return any(row.get("status") == "Block" for row in rows)


def dedup_expires_at(timestamp: Any) -> str:
    parsed = pd.to_datetime(timestamp, errors="coerce", utc=True)
    if pd.isna(parsed):
        return "n/a"
    return (parsed + pd.Timedelta(minutes=config.ALERT_DEDUP_MINUTES)).isoformat()


def required_values_present(frame: pd.DataFrame, columns: list[str]) -> bool:
    return all(column in frame and frame[column].notna().all() for column in columns)


def same_known_value(frame: pd.DataFrame, column: str) -> bool:
    return column in frame and frame[column].notna().all() and frame[column].astype(str).nunique() == 1


def same_numeric_value(frame: pd.DataFrame, column: str) -> bool:
    if column not in frame:
        return False
    values = pd.to_numeric(frame[column], errors="coerce")
    return bool(values.notna().all() and values.nunique() == 1)


def metric_row(items: list[tuple[str, str]]) -> None:
    for column, (label, value) in zip(st.columns(len(items)), items):
        column.metric(label, value)


def status_row(name: str, status: str, note: str, value: Any = "") -> dict[str, str]:
    return {"check": name, "status": status, "value": str(value), "note": note}


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def fmt_bps(value: Any) -> str:
    number = as_float(value)
    return "n/a" if pd.isna(number) else f"{number:,.2f} bps"


def fmt_number(value: Any) -> str:
    number = as_float(value)
    return "n/a" if pd.isna(number) else f"{number:,.6g}"


def timestamp_ms_to_iso(value: Any) -> str:
    number = as_float(value)
    if pd.isna(number):
        return "n/a"
    timestamp = number / 1000 if number > 10_000_000_000 else number
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="minutes")


def safe_key(*parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    return "".join(char if char.isalnum() else "_" for char in raw)[:180]

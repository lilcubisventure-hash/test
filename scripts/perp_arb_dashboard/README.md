# Perp Arbitrage Dashboard

This is a local Streamlit research dashboard for scanning public perpetual-contract market data across centralized exchanges. It collects ticker and funding-rate data for selected crypto, stock, and RWA-style perpetuals, normalizes funding to 8-hour basis points, calculates funding spreads, and checks cross-exchange price dispersion.

It does not trade, route orders, send alerts, use private API keys, or store credentials. It is research tooling only.

## Install

```bash
cd scripts/perp_arb_dashboard
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

From the repository root, this also works:

```bash
streamlit run scripts/perp_arb_dashboard/app.py
```

## Metrics

Funding spread compares the lowest normalized 8-hour funding rate with the highest normalized 8-hour funding rate for the same base symbol. The suggested pair is long the lowest-funding venue and short the highest-funding venue.

Price dispersion compares the lowest midpoint price with the highest midpoint price for the same base symbol. The dashboard also shows the net funding impact if the low-price venue is bought and the high-price venue is sold.

The `Alert Details` section lets you select a funding or price alert and inspect the legs, live order book depth, automatic fee/slippage cost estimates, contract specs, execution checks, and related raw rows before considering any manual action.

Order book depth is fetched on demand for the selected alert legs. The automatic cost model uses public market taker-fee metadata when available, current order book VWAP slippage for entry and exit sides, and an optional manual extra-cost buffer.

Contract spec checks compare public market metadata such as quote/settle currency, contract size, linear/inverse flags, active status, and fee availability.

The alert detail drilldown also includes signal lifecycle, pre-trade, and dry-run execution views:

- Lifecycle applies the configured open threshold, close threshold, funding pre-entry blackout, maximum hold time placeholder, and maximum-loss guard to the after-cost edge.
- Pre-trade checks public mark/index/premium fields, funding timestamp, min/max order amount, min/max order cost, post-only/reduce-only status, order book depth, and circuit-breaker status.
- Dry-run builds a simulated two-leg order plan, enforces a circuit breaker, keeps API scope at `trade_disabled`, and can write a local audit record.

The `Operations` tab keeps private account state explicitly empty until a private integration exists. Balance, margin, positions, and order capacity are shown as not connected. Monitoring controls expose local auto-refresh, alert dedup keys, and notification channel placeholders for Telegram, email, and desktop notifications. Those notification senders are not connected yet.

The `Spot Hedge` tab supports a manual broker spot leg against public perp funding legs. Enter the broker, spot base, spot side, quantity, spot mark, broker cost, and financing/borrow APY. The dashboard then ranks CEX perp funding candidates for the hedge side:

- long broker spot maps to short perp funding legs;
- short broker spot maps to long perp funding legs;
- positive funding carry is treated as income for the hedge leg;
- candidate rows include funding carry, annualized carry, basis edge, one-time cost, expected 8h USD, estimated contract amount, and liquidity/spread status.

An optional local DEX funding feed can be included from `data/manual_dex_funding.csv`. This is a public/manual data feed slot only; no wallet, private key, or on-chain transaction path is loaded.

## Local Data

Each refresh writes:

```text
data/latest_snapshot.csv
data/snapshots.csv
data/dry_run_audit_log.csv
data/manual_dex_funding.csv
```

Generated CSV files are ignored by git. The committed `data/.gitkeep` file keeps the directory present.

## Notes

Apparent anomalies must be manually verified before any real decision. Check order book depth, mark price, index price, premium, exchange status, borrow or margin constraints, fees, slippage, funding timestamp alignment, and real execution cost.

The app remains dry-run only. It does not load private API keys, read account balances, read live positions, submit orders, cancel orders, or send Telegram/email/desktop notifications.

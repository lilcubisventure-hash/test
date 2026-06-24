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

## Local Data

Each refresh writes:

```text
data/latest_snapshot.csv
data/snapshots.csv
```

Generated CSV files are ignored by git. The committed `data/.gitkeep` file keeps the directory present.

## Notes

Apparent anomalies must be manually verified before any real decision. Check order book depth, mark price, index price, exchange status, borrow or margin constraints, fees, slippage, funding timestamp alignment, and real execution cost.

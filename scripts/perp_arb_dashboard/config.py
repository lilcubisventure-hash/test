"""Configuration for the local perp arbitrage dashboard."""

from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
LATEST_SNAPSHOT_PATH = DATA_DIR / "latest_snapshot.csv"
SNAPSHOTS_PATH = DATA_DIR / "snapshots.csv"

MIN_QUOTE_VOLUME = 100_000
MAX_BID_ASK_SPREAD_BPS = 50
FUNDING_SPREAD_ALERT_BPS = 5
PRICE_DISPERSION_ALERT_BPS = 100

DEFAULT_FUNDING_INTERVAL_HOURS = 8.0

TARGET_BASES = [
    "BTC",
    "ETH",
    "SOL",
    "DOGE",
    "XRP",
    "BNB",
    "LINK",
    "AVAX",
    "PEPE",
    "WIF",
    "NVDA",
    "TSLA",
    "AAPL",
    "MSTR",
    "ASML",
    "QQQ",
    "SPY",
    "MU",
    "MSFT",
    "GOOGL",
    "META",
]

EXCHANGES = [
    {
        "id": "binanceusdm",
        "name": "Binance USD-M futures",
        "options": {},
    },
    {
        "id": "bybit",
        "name": "Bybit",
        "options": {"defaultType": "swap", "defaultSubType": "linear"},
    },
    {
        "id": "okx",
        "name": "OKX",
        "options": {"defaultType": "swap"},
    },
    {
        "id": "gate",
        "name": "Gate.io",
        "options": {"defaultType": "swap"},
    },
    {
        "id": "kucoinfutures",
        "name": "KuCoin futures",
        "options": {},
    },
]

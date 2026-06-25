"""Configuration for the local perp arbitrage dashboard."""

from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
LATEST_SNAPSHOT_PATH = DATA_DIR / "latest_snapshot.csv"
SNAPSHOTS_PATH = DATA_DIR / "snapshots.csv"
AUDIT_LOG_PATH = DATA_DIR / "dry_run_audit_log.csv"
MANUAL_DEX_FUNDING_PATH = DATA_DIR / "manual_dex_funding.csv"

MIN_QUOTE_VOLUME = 100_000
MAX_BID_ASK_SPREAD_BPS = 50
FUNDING_SPREAD_ALERT_BPS = 5
PRICE_DISPERSION_ALERT_BPS = 100

DEFAULT_FUNDING_INTERVAL_HOURS = 8.0
DEFAULT_TARGET_NOTIONAL = 10_000.0
ORDER_BOOK_LIMIT = 50
FALLBACK_TAKER_FEE_BPS = 5.0
FALLBACK_SLIPPAGE_BPS = 2.0
MAX_MARK_INDEX_PREMIUM_BPS = 50.0

OPEN_EDGE_THRESHOLD_BPS = 10.0
CLOSE_EDGE_THRESHOLD_BPS = 2.0
FUNDING_ENTRY_BLACKOUT_MINUTES = 20.0
MAX_HOLD_HOURS = 24.0
MAX_LOSS_BPS = 25.0

AUTO_REFRESH_SECONDS = 60
ALERT_DEDUP_MINUTES = 30
NOTIFICATION_CHANNELS = ["Telegram", "Email", "Desktop"]

DRY_RUN_ONLY = True
CIRCUIT_MAX_NEGATIVE_EDGE_BPS = -MAX_LOSS_BPS
CIRCUIT_REQUIRE_DEPTH_OK = True
CIRCUIT_REQUIRE_CONTRACT_CHECKS = True
API_KEY_SCOPES = ["read_only", "notify_only", "trade_disabled"]

SPOT_HEDGE_DEFAULT_BROKER = "Manual broker"
SPOT_HEDGE_DEFAULT_BROKER_COST_BPS = 2.0
SPOT_HEDGE_DEFAULT_FINANCING_APY_PCT = 0.0
SPOT_HEDGE_MIN_FUNDING_8H_BPS = 1.0
SPOT_HEDGE_STOCK_BASES = [
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
    "AMZN",
    "AMD",
    "NFLX",
    "ORCL",
    "PLTR",
    "HOOD",
    "INTC",
    "SP500",
    "XYZ100",
]

BASE_PREFIX_ALIASES = ["XYZ-"]
BASE_SUFFIX_ALIASES = ["STOCK"]

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
    "AMZN",
    "AMD",
    "NFLX",
    "ORCL",
    "PLTR",
    "HOOD",
    "INTC",
    "SP500",
    "XYZ100",
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
    {
        "id": "mexc",
        "name": "MEXC futures",
        "options": {"defaultType": "swap"},
    },
    {
        "id": "bitget",
        "name": "Bitget futures",
        "options": {"defaultType": "swap", "defaultSubType": "linear"},
    },
    {
        "id": "hyperliquid",
        "name": "Hyperliquid",
        "aliases": ["tradeXYZ (Hyperliquid)"],
        "options": {},
        "display_name_rules": [
            {
                "base_prefix": "XYZ-",
                "name": "tradeXYZ (Hyperliquid)",
            }
        ],
    },
]

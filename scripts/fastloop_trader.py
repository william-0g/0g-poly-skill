#!/usr/bin/env python3
"""
Polymarket FastLoop Trader

Evaluates Polymarket BTC 5-minute and 15-minute fast markets using CEX price momentum.
Default signal: Binance BTCUSDT candles. Qualifying signals can be recorded to SQLite
for paper trading; real-money execution is intentionally disabled.

Usage:
    python fastloop_trader.py                 # Dry run (show opportunities, no trades)
    python fastloop_trader.py --record-paper  # Record a paper trade when signal qualifies
    python fastloop_trader.py --positions     # Show current live Polymarket fast positions
    python fastloop_trader.py --paper-positions  # Show current paper positions
    python fastloop_trader.py --live          # Execute a live trade when signal qualifies
    python fastloop_trader.py --quiet         # Only output on trades/errors

Requires:
    SQLite (bundled with Python) for paper journaling
    POLYMARKET_PRIVATE_KEY for live trading and live position checks
"""

import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

# Force line-buffered stdout for non-TTY environments (cron, Docker, OpenClaw)
sys.stdout.reconfigure(line_buffering=True)

# py-clob-client imports (deferred so dry-run/config still work without package)
_clob_imports_ready = False
ClobClient = None
MarketOrderArgs = None
OrderType = None
POLYGON = None
BUY = None
SELL = None
Account = None


def _ensure_clob_imports():
    """Lazy-import py-clob-client so dry-run and config remain lightweight."""
    global _clob_imports_ready, ClobClient, MarketOrderArgs, OrderType, POLYGON, BUY, SELL, Account
    if _clob_imports_ready:
        return
    try:
        from py_clob_client.client import ClobClient as _ClobClient
        from py_clob_client.clob_types import MarketOrderArgs as _MarketOrderArgs, OrderType as _OrderType
        from py_clob_client.constants import POLYGON as _POLYGON
        from py_clob_client.order_builder.constants import BUY as _BUY, SELL as _SELL
        from eth_account import Account as _Account
        ClobClient = _ClobClient
        MarketOrderArgs = _MarketOrderArgs
        OrderType = _OrderType
        POLYGON = _POLYGON
        BUY = _BUY
        SELL = _SELL
        Account = _Account
        _clob_imports_ready = True
    except ImportError:
        print("Error: py-clob-client not installed.")
        print("Run: uv sync")
        sys.exit(1)

# Optional: Trade Journal integration
try:
    from tradejournal import log_trade
    JOURNAL_AVAILABLE = True
except ImportError:
    try:
        from skills.tradejournal import log_trade
        JOURNAL_AVAILABLE = True
    except ImportError:
        JOURNAL_AVAILABLE = False
        def log_trade(*args, **kwargs):
            pass

# =============================================================================
# Configuration (config.json > env vars > defaults)
# =============================================================================

CONFIG_SCHEMA = {
    "entry_threshold": {"default": 0.05, "env": "PM_FASTLOOP_ENTRY", "type": float,
                        "help": "Min price divergence from 50¢ to trigger trade"},
    "min_momentum_pct": {"default": 0.5, "env": "PM_FASTLOOP_MOMENTUM", "type": float,
                         "help": "Min BTC % move in lookback window to trigger"},
    "max_position": {"default": 5.0, "env": "PM_FASTLOOP_MAX_POSITION", "type": float,
                     "help": "Max $ per trade"},
    "signal_source": {"default": "binance", "env": "PM_FASTLOOP_SIGNAL", "type": str,
                      "help": "Price feed source (binance, coingecko)"},
    "lookback_minutes": {"default": 5, "env": "PM_FASTLOOP_LOOKBACK", "type": int,
                         "help": "Minutes of price history for momentum calc"},
    "min_time_remaining": {"default": 60, "env": "PM_FASTLOOP_MIN_TIME", "type": int,
                           "help": "Skip fast_markets with less than this many seconds remaining"},
    "asset": {"default": "BTC", "env": "PM_FASTLOOP_ASSET", "type": str,
              "help": "Asset to trade (BTC, ETH, SOL)"},
    "window": {"default": "5m", "env": "PM_FASTLOOP_WINDOW", "type": str,
               "help": "Market window duration (5m or 15m)"},
    "volume_confidence": {"default": True, "env": "PM_FASTLOOP_VOL_CONF", "type": bool,
                          "help": "Weight signal by volume (higher volume = more confident)"},
    "paper_trade_db": {"default": "fastloop_paper.db", "env": "PM_FASTLOOP_DB", "type": str,
                       "help": "SQLite database path for recorded paper trades"},
}

TRADE_SOURCE = "clob:fastloop"
SMART_SIZING_PCT = 0.05  # 5% of balance per trade
MIN_SHARES_PER_ORDER = 5  # Polymarket minimum
DEFAULT_PAPER_CASH = 100.0
NEW_YORK_TZ = ZoneInfo("America/New_York")
CLOB_HOST = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# Asset → Binance symbol mapping
ASSET_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

# Asset → Gamma API search patterns
ASSET_PATTERNS = {
    "BTC": ["bitcoin up or down"],
    "ETH": ["ethereum up or down"],
    "SOL": ["solana up or down"],
}


def _resolve_config_path(skill_file, config_filename="config.json"):
    skill_path = Path(skill_file).resolve()
    candidate_paths = [
        skill_path.parents[1] / config_filename,
        skill_path.parent / config_filename,
    ]
    for path in candidate_paths:
        if path.exists():
            return path
    return candidate_paths[0]


def _load_config(schema, skill_file, config_filename="config.json"):
    """Load config with priority: config.json > env vars > defaults."""
    config_path = _resolve_config_path(skill_file, config_filename)
    file_cfg = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                file_cfg = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    result = {}
    for key, spec in schema.items():
        if key in file_cfg:
            result[key] = file_cfg[key]
        elif spec.get("env") and os.environ.get(spec["env"]):
            val = os.environ.get(spec["env"])
            type_fn = spec.get("type", str)
            try:
                if type_fn == bool:
                    result[key] = val.lower() in ("true", "1", "yes")
                else:
                    result[key] = type_fn(val)
            except (ValueError, TypeError):
                result[key] = spec.get("default")
        else:
            result[key] = spec.get("default")
    return result


def _get_config_path(skill_file, config_filename="config.json"):
    return _resolve_config_path(skill_file, config_filename)


def _update_config(updates, skill_file, config_filename="config.json"):
    """Update config.json with new values."""
    config_path = _resolve_config_path(skill_file, config_filename)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    existing.update(updates)
    with open(config_path, "w") as f:
        json.dump(existing, f, indent=2)
    return existing


# Load config
cfg = _load_config(CONFIG_SCHEMA, __file__)
ENTRY_THRESHOLD = cfg["entry_threshold"]
MIN_MOMENTUM_PCT = cfg["min_momentum_pct"]
MAX_POSITION_USD = cfg["max_position"]
SIGNAL_SOURCE = cfg["signal_source"]
LOOKBACK_MINUTES = cfg["lookback_minutes"]
MIN_TIME_REMAINING = cfg["min_time_remaining"]
ASSET = cfg["asset"].upper()
WINDOW = cfg["window"]  # "5m" or "15m"
VOLUME_CONFIDENCE = cfg["volume_confidence"]
PAPER_TRADE_DB = cfg["paper_trade_db"]


# =============================================================================
# API Helpers
# =============================================================================

def get_private_key():
    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        print("Error: POLYMARKET_PRIVATE_KEY environment variable not set")
        print("Set it to your Polygon wallet private key (hex string)")
        sys.exit(1)
    return key


def get_clob_client():
    """Initialize authenticated ClobClient from private key."""
    _ensure_clob_imports()
    pk = get_private_key()
    client = ClobClient(CLOB_HOST, key=pk, chain_id=POLYGON)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def get_wallet_address():
    """Derive wallet address from private key."""
    _ensure_clob_imports()
    pk = get_private_key()
    return Account.from_key(pk).address

def _api_request(url, method="GET", data=None, headers=None, timeout=15):
    """Make an HTTP request. Returns parsed JSON or None on error."""
    try:
        req_headers = headers or {}
        if "User-Agent" not in req_headers:
            req_headers["User-Agent"] = "polymarket-fastloop/1.0"
        body = None
        if data:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=req_headers, method=method)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            error_body = json.loads(e.read().decode("utf-8"))
            return {"error": error_body.get("detail", str(e)), "status_code": e.code}
        except Exception:
            return {"error": str(e), "status_code": e.code}
    except URLError as e:
        return {"error": f"Connection error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# Sprint Market Discovery
# =============================================================================

def discover_fast_market_markets(asset="BTC", window="5m"):
    """Find active fast markets on Polymarket via Gamma API."""
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    url = (
        "https://gamma-api.polymarket.com/markets"
        "?limit=20&closed=false&tag=crypto&order=createdAt&ascending=false"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict) and result.get("error"):
        return []

    markets = []
    for m in result:
        q = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        matches_window = f"-{window}-" in slug
        if any(p in q for p in patterns) and matches_window:
            condition_id = m.get("conditionId", "")
            closed = m.get("closed", False)
            if not closed and slug:
                # Parse end time from question (e.g., "5:30AM-5:35AM ET")
                end_time = _parse_fast_market_end_time(m.get("question", ""))
                # Extract CLOB token IDs for direct trading
                try:
                    clob_token_ids = json.loads(m.get("clobTokenIds", "[]"))
                except (json.JSONDecodeError, TypeError):
                    clob_token_ids = []
                markets.append({
                    "question": m.get("question", ""),
                    "slug": slug,
                    "condition_id": condition_id,
                    "end_time": end_time,
                    "outcomes": m.get("outcomes", []),
                    "outcome_prices": m.get("outcomePrices", "[]"),
                    "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
                    "clob_token_ids": clob_token_ids,
                })
    return markets


def _parse_fast_market_end_time(question):
    """Parse end time from fast market question.
    e.g., 'Bitcoin Up or Down - February 15, 5:30AM-5:35AM ET' → datetime
    """
    import re
    # Match pattern: "Month Day, StartTime-EndTime ET"
    pattern = r'(\w+ \d+),.*?-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET'
    match = re.search(pattern, question)
    if not match:
        return None
    try:
        date_str = match.group(1)
        time_str = match.group(2)
        year = datetime.now(NEW_YORK_TZ).year
        dt_str = f"{date_str} {year} {time_str}"
        dt_local = datetime.strptime(dt_str, "%B %d %Y %I:%M%p").replace(tzinfo=NEW_YORK_TZ)

        now_local = datetime.now(NEW_YORK_TZ)
        if dt_local < now_local and (now_local - dt_local).days > 300:
            dt_local = dt_local.replace(year=dt_local.year + 1)

        return dt_local.astimezone(timezone.utc)
    except Exception:
        return None


def find_best_fast_market(markets):
    """Pick the best fast_market to trade: soonest expiring with enough time remaining."""
    now = datetime.now(timezone.utc)
    candidates = []
    for m in markets:
        end_time = m.get("end_time")
        if not end_time:
            continue
        remaining = (end_time - now).total_seconds()
        if remaining > MIN_TIME_REMAINING:
            # Must have CLOB token IDs to trade directly
            if m.get("clob_token_ids") and len(m["clob_token_ids"]) >= 2:
                candidates.append((remaining, m))

    if not candidates:
        return None
    # Sort by soonest expiring
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


# =============================================================================
# CEX Price Signal
# =============================================================================

def get_binance_momentum(symbol="BTCUSDT", lookback_minutes=5):
    """Get price momentum from Binance public API.
    Returns: {momentum_pct, direction, price_now, price_then, avg_volume, candles}
    """
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=1m&limit={lookback_minutes}"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict):
        return None

    try:
        # Kline format: [open_time, open, high, low, close, volume, ...]
        candles = result
        if len(candles) < 2:
            return None

        price_then = float(candles[0][1])   # open of oldest candle
        price_now = float(candles[-1][4])    # close of newest candle
        momentum_pct = ((price_now - price_then) / price_then) * 100
        direction = "up" if momentum_pct > 0 else "down"

        volumes = [float(c[5]) for c in candles]
        avg_volume = sum(volumes) / len(volumes)
        latest_volume = volumes[-1]

        # Volume ratio: latest vs average (>1 = above average activity)
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 1.0

        return {
            "momentum_pct": momentum_pct,
            "direction": direction,
            "price_now": price_now,
            "price_then": price_then,
            "avg_volume": avg_volume,
            "latest_volume": latest_volume,
            "volume_ratio": volume_ratio,
            "candles": len(candles),
        }
    except (IndexError, ValueError, KeyError):
        return None


def get_coingecko_momentum(asset="bitcoin", lookback_minutes=5):
    """Fallback: get price from CoinGecko (less accurate, ~1-2 min lag)."""
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={asset}&vs_currencies=usd"
    result = _api_request(url)
    if not result or isinstance(result, dict) and result.get("error"):
        return None
    price_now = result.get(asset, {}).get("usd")
    if not price_now:
        return None
    # CoinGecko doesn't give candle data on free tier, so just return current price
    # Agent would need to track history across calls for momentum
    return {
        "momentum_pct": 0,  # Can't calculate without history
        "direction": "neutral",
        "price_now": price_now,
        "price_then": price_now,
        "avg_volume": 0,
        "latest_volume": 0,
        "volume_ratio": 1.0,
        "candles": 0,
    }


COINGECKO_ASSETS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}


def get_momentum(asset="BTC", source="binance", lookback=5):
    """Get price momentum from configured source."""
    if source == "binance":
        symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
        return get_binance_momentum(symbol, lookback)
    elif source == "coingecko":
        cg_id = COINGECKO_ASSETS.get(asset, "bitcoin")
        return get_coingecko_momentum(cg_id, lookback)
    else:
        return None


# =============================================================================
# Polymarket CLOB Trading
# =============================================================================

def get_live_positions(address):
    """Get current positions from Polymarket Data API."""
    url = f"{DATA_API}/positions?user={address}&sizeThreshold=0.1&limit=100"
    result = _api_request(url)
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and not result.get("error"):
        return result.get("positions", [])
    return []


def get_live_portfolio(address):
    """Get portfolio value from Polymarket Data API."""
    return _api_request(f"{DATA_API}/value?user={address}")


def execute_live_trade(client, token_id, amount):
    """Execute a FOK market order on Polymarket via CLOB."""
    try:
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=BUY,
        )
        signed_order = client.create_market_order(order_args)
        result = client.post_order(signed_order, orderType=OrderType.FOK)
        return result
    except Exception as e:
        return {"error": str(e)}

def _db_path():
    path = Path(PAPER_TRADE_DB)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path


def ensure_paper_db():
    """Create the SQLite schema if it does not exist yet."""
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fastloop_trades (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              market_id TEXT NOT NULL,
              market_name TEXT NOT NULL,
              strategy TEXT NOT NULL,
              direction TEXT NOT NULL,
              entry_price REAL NOT NULL,
              exit_price REAL,
              quantity REAL NOT NULL,
              pnl REAL,
              signal_momentum_pct REAL,
              volume_ratio REAL,
              status TEXT NOT NULL DEFAULT 'OPEN',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              closed_at TEXT,
              UNIQUE(market_id, direction, status)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              total_value REAL NOT NULL,
              cash REAL NOT NULL,
              positions TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO portfolio (id, total_value, cash, positions)
            VALUES (1, ?, ?, '[]')
            ON CONFLICT(id) DO NOTHING
            """,
            (DEFAULT_PAPER_CASH, DEFAULT_PAPER_CASH),
        )


def get_paper_portfolio():
    ensure_paper_db()
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT total_value, cash, positions, updated_at FROM portfolio WHERE id = 1").fetchone()
    if row is None:
        return {
            "total_value": DEFAULT_PAPER_CASH,
            "cash": DEFAULT_PAPER_CASH,
            "positions": [],
            "updated_at": None,
        }
    return {
        "total_value": float(row["total_value"]),
        "cash": float(row["cash"]),
        "positions": json.loads(row["positions"] or "[]"),
        "updated_at": row["updated_at"],
    }


def get_positions():
    """Return open paper positions from SQLite."""
    ensure_paper_db()
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT market_id, market_name, direction, entry_price, quantity, signal_momentum_pct,
                   volume_ratio, created_at
            FROM fastloop_trades
            WHERE status = 'OPEN'
            ORDER BY created_at DESC
            """
        ).fetchall()
    return rows


def calculate_position_size(max_size, smart_sizing=False, live_mode=False, address=None):
    """Calculate position size for either live or paper mode."""
    if not smart_sizing:
        return max_size
    if live_mode:
        portfolio = get_live_portfolio(address)
        if not portfolio or isinstance(portfolio, dict) and portfolio.get("error"):
            return max_size
        if isinstance(portfolio, dict):
            balance = float(portfolio.get("value", 0) or 0)
        else:
            balance = float(portfolio)
    else:
        portfolio = get_paper_portfolio()
        balance = float(portfolio.get("cash", 0) or 0)
    if balance <= 0:
        return 0.0
    smart_size = balance * SMART_SIZING_PCT
    return min(smart_size, max_size)


def record_paper_trade(market, direction, entry_price, quantity, momentum_pct, volume_ratio):
    """Persist a paper trade candidate and refresh the lightweight portfolio snapshot."""
    ensure_paper_db()
    db_path = _db_path()
    position_cost = entry_price * quantity

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        existing_open = conn.execute(
            """
            SELECT id FROM fastloop_trades
            WHERE market_id = ? AND direction = ? AND status = 'OPEN'
            """,
            (market["condition_id"], direction),
        ).fetchone()
        if existing_open is not None:
            return False, "Paper trade already recorded for this market and direction"

        portfolio_row = conn.execute(
            "SELECT cash, positions FROM portfolio WHERE id = 1"
        ).fetchone()
        cash = float(portfolio_row["cash"])
        if position_cost > cash + 1e-9:
            return False, f"Insufficient paper cash (${cash:.2f} available)"

        positions = json.loads(portfolio_row["positions"] or "[]")
        positions.append(
            {
                "market_id": market["condition_id"],
                "market_name": market["question"],
                "direction": direction,
                "entry_price": round(entry_price, 4),
                "quantity": round(quantity, 4),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        conn.execute(
            """
            INSERT INTO fastloop_trades (
              market_id, market_name, strategy, direction, entry_price, quantity,
              signal_momentum_pct, volume_ratio
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market["condition_id"],
                market["question"],
                TRADE_SOURCE,
                direction,
                entry_price,
                quantity,
                momentum_pct,
                volume_ratio,
            ),
        )
        updated_cash = cash - position_cost
        total_value = updated_cash + sum(p["entry_price"] * p["quantity"] for p in positions)
        conn.execute(
            """
            UPDATE portfolio
            SET cash = ?, total_value = ?, positions = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (updated_cash, total_value, json.dumps(positions)),
        )
    return True, None


# =============================================================================
# Main Strategy Logic
# =============================================================================

def run_fast_market_strategy(dry_run=True, positions_only=False, paper_positions_only=False, show_config=False,
                        smart_sizing=False, quiet=False, record_paper=False):
    """Run one cycle of the fast_market trading strategy."""

    def log(msg, force=False):
        """Print unless quiet mode is on. force=True always prints."""
        if not quiet or force:
            print(msg)

    log("⚡ Polymarket FastLoop Trader")
    log("=" * 50)

    live_mode = not dry_run and not record_paper

    if dry_run and not record_paper:
        log("\n  [DRY RUN] No trades will be recorded. Use --record-paper to persist paper trades.")
    elif record_paper:
        log("\n  [PAPER MODE] Qualifying signals will be recorded to SQLite.")
    else:
        log("\n  [LIVE MODE] Qualifying signals will place real Polymarket orders.", force=True)

    log(f"\n⚙️  Configuration:")
    log(f"  Asset:            {ASSET}")
    log(f"  Window:           {WINDOW}")
    log(f"  Entry threshold:  {ENTRY_THRESHOLD} (min divergence from 50¢)")
    log(f"  Min momentum:     {MIN_MOMENTUM_PCT}% (min price move)")
    log(f"  Max position:     ${MAX_POSITION_USD:.2f}")
    log(f"  Signal source:    {SIGNAL_SOURCE}")
    log(f"  Lookback:         {LOOKBACK_MINUTES} minutes")
    log(f"  Min time left:    {MIN_TIME_REMAINING}s")
    log(f"  Volume weighting: {'✓' if VOLUME_CONFIDENCE else '✗'}")

    if show_config:
        config_path = _get_config_path(__file__)
        log(f"\n  Config file: {config_path}")
        log(f"  Paper DB:    {_db_path()}")
        log(f"\n  To change settings:")
        log(f'    python fastloop_trader.py --set entry_threshold=0.08')
        log(f'    python fastloop_trader.py --set asset=ETH')
        log(f'    Or edit config.json directly')
        return

    # Show positions if requested
    if positions_only:
        _ensure_clob_imports()
        address = get_wallet_address()
        log("\n📊 Live Sprint Positions:")
        positions = get_live_positions(address)
        fast_market_positions = [p for p in positions if "up or down" in (p.get("title", "") or p.get("question", "") or "").lower()]
        if not fast_market_positions:
            log("  No open live fast market positions")
        else:
            for pos in fast_market_positions:
                title = pos.get("title", "") or pos.get("question", "Unknown")
                size = float(pos.get("size", 0) or 0)
                outcome = pos.get("outcome", "?")
                pnl = float(pos.get("cashPnl", 0) or pos.get("pnl", 0) or 0)
                log(f"  • {title[:60]}")
                log(f"    {outcome}: {size:.1f} shares | P&L: ${pnl:.2f}")
        return

    if paper_positions_only:
        ensure_paper_db()
        log("\n📊 Paper Positions:")
        positions = get_positions()
        if not positions:
            log("  No open paper positions")
        else:
            for pos in positions:
                log(f"  • {pos['market_name'][:60]}")
                log(
                    f"    {pos['direction'].upper()}: {float(pos['quantity']):.1f} shares"
                    f" @ ${float(pos['entry_price']):.3f}"
                )
        portfolio = get_paper_portfolio()
        log(f"\n💰 Paper Portfolio:")
        log(f"  Total value: ${portfolio['total_value']:.2f}")
        log(f"  Cash:        ${portfolio['cash']:.2f}")
        return

    # Show portfolio if smart sizing
    if smart_sizing:
        if live_mode:
            address = get_wallet_address()
            log("\n💰 Live Portfolio:")
            portfolio = get_live_portfolio(address)
            if portfolio and not (isinstance(portfolio, dict) and portfolio.get("error")):
                value = portfolio.get("value", 0) if isinstance(portfolio, dict) else portfolio
                log(f"  Value:       ${float(value):.2f}")
        else:
            ensure_paper_db()
            log("\n💰 Paper Portfolio:")
            portfolio = get_paper_portfolio()
            log(f"  Total value: ${portfolio['total_value']:.2f}")
            log(f"  Cash:        ${portfolio['cash']:.2f}")

    # Step 1: Discover fast markets
    log(f"\n🔍 Discovering {ASSET} fast markets...")
    markets = discover_fast_market_markets(ASSET, WINDOW)
    log(f"  Found {len(markets)} active fast markets")

    if not markets:
        log("  No active fast markets found")
        if not quiet:
            print("📊 Summary: No markets available")
        return

    # Step 2: Find best fast_market to trade
    best = find_best_fast_market(markets)
    if not best:
        log(f"  No fast_markets with >{MIN_TIME_REMAINING}s remaining (or missing token IDs)")
        if not quiet:
            print("📊 Summary: No tradeable fast_markets (too close to expiry)")
        return

    end_time = best.get("end_time")
    remaining = (end_time - datetime.now(timezone.utc)).total_seconds() if end_time else 0
    log(f"\n🎯 Selected: {best['question']}")
    log(f"  Expires in: {remaining:.0f}s")

    # Parse current market odds
    try:
        prices = json.loads(best.get("outcome_prices", "[]"))
        market_yes_price = float(prices[0]) if prices else 0.5
    except (json.JSONDecodeError, IndexError, ValueError):
        market_yes_price = 0.5
    log(f"  Current YES price: ${market_yes_price:.3f}")

    # Fee info (fast markets charge 10% on winnings)
    fee_rate_bps = best.get("fee_rate_bps", 0)
    fee_rate = fee_rate_bps / 10000  # 1000 bps -> 0.10
    if fee_rate > 0:
        log(f"  Fee rate:         {fee_rate:.0%} (Polymarket fast market fee)")

    # Token IDs for direct CLOB trading
    clob_token_ids = best.get("clob_token_ids", [])
    yes_token_id = clob_token_ids[0] if len(clob_token_ids) > 0 else None
    no_token_id = clob_token_ids[1] if len(clob_token_ids) > 1 else None

    # Step 3: Get CEX price momentum
    log(f"\n📈 Fetching {ASSET} price signal ({SIGNAL_SOURCE})...")
    momentum = get_momentum(ASSET, SIGNAL_SOURCE, LOOKBACK_MINUTES)

    if not momentum:
        log("  ❌ Failed to fetch price data", force=True)
        return

    log(f"  Price: ${momentum['price_now']:,.2f} (was ${momentum['price_then']:,.2f})")
    log(f"  Momentum: {momentum['momentum_pct']:+.3f}%")
    log(f"  Direction: {momentum['direction']}")
    if VOLUME_CONFIDENCE:
        log(f"  Volume ratio: {momentum['volume_ratio']:.2f}x avg")

    # Step 4: Decision logic
    log(f"\n🧠 Analyzing...")

    momentum_pct = abs(momentum["momentum_pct"])
    direction = momentum["direction"]

    # Check minimum momentum
    if momentum_pct < MIN_MOMENTUM_PCT:
        log(f"  ⏸️  Momentum {momentum_pct:.3f}% < minimum {MIN_MOMENTUM_PCT}% — skip")
        if not quiet:
            print(f"📊 Summary: No trade (momentum too weak: {momentum_pct:.3f}%)")
        return

    # Calculate expected fair price based on momentum direction
    # Simple model: strong momentum → higher probability of continuation
    if direction == "up":
        side = "yes"
        token_id = yes_token_id
        divergence = 0.50 + ENTRY_THRESHOLD - market_yes_price
        trade_rationale = f"{ASSET} up {momentum['momentum_pct']:+.3f}% but YES only ${market_yes_price:.3f}"
    else:
        side = "no"
        token_id = no_token_id
        divergence = market_yes_price - (0.50 - ENTRY_THRESHOLD)
        trade_rationale = f"{ASSET} down {momentum['momentum_pct']:+.3f}% but YES still ${market_yes_price:.3f}"

    if not token_id:
        log(f"  ❌ Missing CLOB token ID for {side.upper()} outcome", force=True)
        return

    # Volume confidence adjustment
    vol_note = ""
    if VOLUME_CONFIDENCE and momentum["volume_ratio"] < 0.5:
        log(f"  ⏸️  Low volume ({momentum['volume_ratio']:.2f}x avg) — weak signal, skip")
        if not quiet:
            print(f"📊 Summary: No trade (low volume)")
        return
    elif VOLUME_CONFIDENCE and momentum["volume_ratio"] > 2.0:
        vol_note = f" 📊 (high volume: {momentum['volume_ratio']:.1f}x avg)"

    # Check divergence threshold
    if divergence <= 0:
        log(f"  ⏸️  Market already priced in: divergence {divergence:.3f} ≤ 0 — skip")
        if not quiet:
            print(f"📊 Summary: No trade (market already priced in)")
        return

    # Fee-aware EV check: require enough divergence to cover fees
    if fee_rate > 0:
        buy_price = market_yes_price if side == "yes" else (1 - market_yes_price)
        win_profit = (1 - buy_price) * (1 - fee_rate)
        breakeven = buy_price / (win_profit + buy_price)
        fee_penalty = breakeven - 0.50  # how much fees shift breakeven above 50%
        min_divergence = fee_penalty + 0.02  # plus buffer
        log(f"  Breakeven:        {breakeven:.1%} win rate (fee-adjusted, min divergence {min_divergence:.3f})")
        if divergence < min_divergence:
            log(f"  ⏸️  Divergence {divergence:.3f} < fee-adjusted minimum {min_divergence:.3f} — skip")
            if not quiet:
                print(f"📊 Summary: No trade (fees eat the edge)")
            return

    # We have a signal!
    address = get_wallet_address() if live_mode and smart_sizing else None
    if record_paper:
        ensure_paper_db()
    position_size = calculate_position_size(MAX_POSITION_USD, smart_sizing, live_mode=live_mode, address=address)
    price = market_yes_price if side == "yes" else (1 - market_yes_price)

    # Check minimum order size
    if price > 0:
        min_cost = MIN_SHARES_PER_ORDER * price
        if min_cost > position_size:
            log(f"  ⚠️  Position ${position_size:.2f} too small for {MIN_SHARES_PER_ORDER} shares at ${price:.2f}")
            return
    if position_size <= 0:
        if live_mode:
            log("  ⏸️  No live portfolio value available for a new trade")
        else:
            log("  ⏸️  No paper cash available for a new trade")
        return

    log(f"  ✅ Signal: {side.upper()} — {trade_rationale}{vol_note}", force=True)
    log(f"  Divergence: {divergence:.3f}", force=True)
    est_shares = position_size / price if price > 0 else 0

    # Step 5: Trade or record
    recorded = False
    result = None
    if dry_run and not record_paper:
        log(f"  [DRY RUN] Would buy {side.upper()} ${position_size:.2f} (~{est_shares:.1f} shares)", force=True)
    elif record_paper:
        recorded, error = record_paper_trade(
            best,
            side,
            price,
            est_shares,
            momentum["momentum_pct"],
            momentum["volume_ratio"],
        )
        if recorded:
            log(f"  📝 Paper trade recorded: {side.upper()} ${position_size:.2f} (~{est_shares:.1f} shares)", force=True)
            if JOURNAL_AVAILABLE:
                confidence = min(0.9, 0.5 + divergence + (momentum_pct / 100))
                log_trade(
                    trade_id=f"paper:{best['condition_id']}:{side}",
                    source=f"{TRADE_SOURCE}:paper",
                    thesis=trade_rationale,
                    confidence=round(confidence, 2),
                    asset=ASSET,
                    momentum_pct=round(momentum["momentum_pct"], 3),
                    volume_ratio=round(momentum["volume_ratio"], 2),
                    signal_source=SIGNAL_SOURCE,
                )
        else:
            log(f"  ❌ Paper trade not recorded: {error}", force=True)
    else:
        client = get_clob_client()
        log(f"  Executing {side.upper()} trade for ${position_size:.2f} via CLOB...", force=True)
        result = execute_live_trade(client, token_id, position_size)
        if result and not (isinstance(result, dict) and result.get("error")):
            order_id = result.get("orderID") or result.get("id") if isinstance(result, dict) else None
            log(f"  ✅ Order placed: {side.upper()} ${position_size:.2f}", force=True)
            if order_id:
                log(f"  Order ID: {order_id}", force=True)
            if JOURNAL_AVAILABLE:
                confidence = min(0.9, 0.5 + divergence + (momentum_pct / 100))
                log_trade(
                    trade_id=order_id or "unknown",
                    source=TRADE_SOURCE,
                    thesis=trade_rationale,
                    confidence=round(confidence, 2),
                    asset=ASSET,
                    momentum_pct=round(momentum["momentum_pct"], 3),
                    volume_ratio=round(momentum["volume_ratio"], 2),
                    signal_source=SIGNAL_SOURCE,
                )
        else:
            error = result.get("error", "Unknown error") if isinstance(result, dict) else "No response"
            log(f"  ❌ Trade failed: {error}", force=True)

    # Summary
    if dry_run and not record_paper:
        total_trades = 0
    elif record_paper:
        total_trades = 1 if recorded else 0
    else:
        total_trades = 1 if result and not (isinstance(result, dict) and result.get("error")) else 0
    show_summary = not quiet or total_trades > 0
    if show_summary:
        print(f"\n📊 Summary:")
        print(f"  Sprint: {best['question'][:50]}")
        print(f"  Signal: {direction} {momentum_pct:.3f}% | YES ${market_yes_price:.3f}")
        action = "DRY RUN"
        if record_paper:
            action = "PAPER RECORDED" if total_trades else "PAPER SKIPPED"
        elif live_mode:
            action = "TRADED" if total_trades else "FAILED"
        print(f"  Action: {action}")


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Polymarket FastLoop Trader")
    parser.add_argument("--live", action="store_true", help="Execute real trades")
    parser.add_argument("--dry-run", action="store_true", help="(Default) Show opportunities without trading")
    parser.add_argument("--positions", action="store_true", help="Show current live fast market positions")
    parser.add_argument("--paper-positions", action="store_true", help="Show current paper positions")
    parser.add_argument("--config", action="store_true", help="Show current config")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="Update config (e.g., --set entry_threshold=0.08)")
    parser.add_argument("--smart-sizing", action="store_true", help="Use portfolio-based position sizing")
    parser.add_argument("--record-paper", action="store_true",
                        help="Record qualifying signals to the SQLite paper-trading journal")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only output on trades/errors (ideal for high-frequency runs)")
    args = parser.parse_args()

    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                print(f"Invalid --set format: {item}. Use KEY=VALUE")
                sys.exit(1)
            key, val = item.split("=", 1)
            if key in CONFIG_SCHEMA:
                type_fn = CONFIG_SCHEMA[key].get("type", str)
                try:
                    if type_fn == bool:
                        updates[key] = val.lower() in ("true", "1", "yes")
                    else:
                        updates[key] = type_fn(val)
                except ValueError:
                    print(f"Invalid value for {key}: {val}")
                    sys.exit(1)
            else:
                print(f"Unknown config key: {key}")
                print(f"Valid keys: {', '.join(CONFIG_SCHEMA.keys())}")
                sys.exit(1)
        result = _update_config(updates, __file__)
        print(f"✅ Config updated: {json.dumps(updates)}")
        sys.exit(0)

    dry_run = not args.live

    run_fast_market_strategy(
        dry_run=dry_run,
        positions_only=args.positions,
        paper_positions_only=args.paper_positions,
        show_config=args.config,
        smart_sizing=args.smart_sizing,
        record_paper=args.record_paper,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()

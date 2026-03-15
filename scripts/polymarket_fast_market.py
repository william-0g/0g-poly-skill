#!/usr/bin/env python3
"""
Polymarket Fast Market Trader

Evaluates Polymarket BTC 5-minute and 15-minute fast markets using CEX price momentum.
Default signal: Binance BTCUSDT candles. Qualifying signals can be recorded to SQLite
for paper trading

Usage:
    python polymarket_fast_market.py                 # Paper trade by default
    python polymarket_fast_market.py --positions     # Show current live Polymarket fast positions
    python polymarket_fast_market.py --paper-positions  # Show current paper positions
    python polymarket_fast_market.py --live          # Execute a live trade when signal qualifies
    python polymarket_fast_market.py --quiet         # Only output on trades/errors

Requires:
    SQLite (bundled with Python) for paper journaling
    POLYMARKET_PRIVATE_KEY for live trading and live position checks
"""

import os
import sys
import json
import sqlite3
import argparse
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

from scripts.quant_model import (
    evaluate_consensus_signal,
    load_quant_model,
)
from scripts.polymarket_features import OnlineFeatureBuffer

try:
    import websocket
except ImportError:
    websocket = None

# Force line-buffered stdout for non-TTY environments (cron, Docker, OpenClaw)
sys.stdout.reconfigure(line_buffering=True)

# py-clob-client imports (deferred so paper/config flows stay lightweight)
_clob_imports_ready = False
ClobClient = None
MarketOrderArgs = None
OrderType = None
POLYGON = None
BUY = None
SELL = None
Account = None


def _ensure_clob_imports():
    """Lazy-import py-clob-client so non-live commands remain lightweight."""
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
    "entry_threshold": {"default": 0.05, "env": "PM_FAST_MARKET_ENTRY", "type": float,
                        "help": "Min price divergence from 50¢ to trigger trade"},
    "min_momentum_pct": {"default": 0.5, "env": "PM_FAST_MARKET_MOMENTUM", "type": float,
                         "help": "Min BTC % move in lookback window to trigger"},
    "max_position": {"default": 5.0, "env": "PM_FAST_MARKET_MAX_POSITION", "type": float,
                     "help": "Max $ per trade"},
    "signal_source": {"default": "binance", "env": "PM_FAST_MARKET_SIGNAL", "type": str,
                      "help": "Price feed source (binance, coingecko)"},
    "lookback_minutes": {"default": 5, "env": "PM_FAST_MARKET_LOOKBACK", "type": int,
                         "help": "Minutes of price history for momentum calc"},
    "min_time_remaining": {"default": 60, "env": "PM_FAST_MARKET_MIN_TIME", "type": int,
                           "help": "Skip fast markets with less than this many seconds remaining"},
    "asset": {"default": "BTC", "env": "PM_FAST_MARKET_ASSET", "type": str,
              "help": "Asset to trade (BTC, ETH, SOL)"},
    "window": {"default": "5m", "env": "PM_FAST_MARKET_WINDOW", "type": str,
               "help": "Market window duration (5m or 15m)"},
    "volume_confidence": {"default": True, "env": "PM_FAST_MARKET_VOL_CONF", "type": bool,
                          "help": "Weight signal by volume (higher volume = more confident)"},
    "trade_db": {"default": "polymarket_fast_market_paper.db", "env": "PM_FAST_MARKET_DB", "type": str,
                 "help": "SQLite database path for recorded trades"},
}

TRADE_SOURCE = "clob:polymarket-fast-market"
SMART_SIZING_PCT = 0.05  # 5% of balance per trade
MIN_SHARES_PER_ORDER = 5  # Polymarket minimum
DEFAULT_PAPER_CASH = 100.0
NEW_YORK_TZ = ZoneInfo("America/New_York")
GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_LOOP_INTERVAL_SECONDS = 5
SESSION_SCHEMA_VERSION = "1.0"
TRADES_TABLE = "polymarket_fast_market_trades"
LEGACY_TRADES_TABLE = "".join(["fast", "loop", "_trades"])
ORDER_BOOK_FEATURE_DIM = 6
MODEL_CHECKPOINTS = {
    ("BTC", "15m"): Path(__file__).resolve().parents[1] / "models" / "btc-15min.pt",
    ("ETH", "15m"): Path(__file__).resolve().parents[1] / "models" / "eth-15min.pt",
}
_QUANT_MODEL_CACHE = {}
_FEATURE_BUFFER_CACHE = {}
_MARKET_STREAM_CACHE = {}


def _resolve_quant_signal_model(asset, window):
    key = (str(asset).upper(), str(window))
    cached = _QUANT_MODEL_CACHE.get(key)
    if cached is not None:
        return cached

    checkpoint_path = MODEL_CHECKPOINTS.get(key)
    if checkpoint_path and checkpoint_path.exists():
        try:
            model = load_quant_model(checkpoint_path)
            resolved = {
                "model": model,
                "enabled": True,
                "source": "checkpoint",
                "checkpoint": str(checkpoint_path),
                "loaded": True,
                "error": None,
            }
            _QUANT_MODEL_CACHE[key] = resolved
            return resolved
        except Exception as exc:
            resolved = {
                "model": None,
                "enabled": False,
                "source": "disabled",
                "checkpoint": str(checkpoint_path),
                "loaded": False,
                "error": str(exc),
            }
            _QUANT_MODEL_CACHE[key] = resolved
            return resolved

    resolved = {
        "model": None,
        "enabled": False,
        "source": "disabled",
        "checkpoint": str(checkpoint_path) if checkpoint_path else None,
        "loaded": False,
        "error": None,
    }
    _QUANT_MODEL_CACHE[key] = resolved
    return resolved


def _get_or_create_feature_buffer(
    slug,
    market_end_ts,
    *,
    sequence_steps,
    step_seconds,
    target_asset_id=None,
    strict_asset_id=True,
):
    if not slug or not market_end_ts:
        return None

    cache_key = str(slug)
    cached = _FEATURE_BUFFER_CACHE.get(cache_key)
    if cached is not None:
        if (
            cached.market_end_ts == market_end_ts
            and cached.sequence_steps == sequence_steps
            and cached.step_seconds == step_seconds
            and cached.target_asset_id == (str(target_asset_id) if target_asset_id is not None else None)
            and cached.strict_asset_id == bool(strict_asset_id)
        ):
            return cached
        _FEATURE_BUFFER_CACHE.pop(cache_key, None)

    buffer = OnlineFeatureBuffer(
        market_end_ts=market_end_ts,
        sequence_steps=sequence_steps,
        step_seconds=step_seconds,
        slug=slug,
        target_asset_id=target_asset_id,
        strict_asset_id=strict_asset_id,
    )
    _FEATURE_BUFFER_CACHE[cache_key] = buffer
    return buffer


def _feature_sampling_config(window):
    if str(window) == "5m":
        return {"sequence_steps": 15, "step_seconds": 20}
    return {"sequence_steps": 15, "step_seconds": 60}


def _coerce_price(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_timestamp_ms(value):
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if numeric > 1e12:
        return int(numeric)
    return int(numeric * 1000)


def _parse_order_levels(levels):
    parsed = []
    for level in levels or []:
        if isinstance(level, dict):
            price = _coerce_price(level.get("price"))
            size = _coerce_price(level.get("size"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price = _coerce_price(level[0])
            size = _coerce_price(level[1])
        else:
            continue
        if price is None or size is None:
            continue
        parsed.append({"price": price, "size": size})
    return parsed


def _normalize_market_ws_events(payload):
    if isinstance(payload, list):
        normalized = []
        for item in payload:
            normalized.extend(_normalize_market_ws_events(item))
        return normalized
    if not isinstance(payload, dict):
        return []

    asset_id = payload.get("asset_id") or payload.get("assetId")
    timestamp = payload.get("timestamp")
    event_type = str(payload.get("event_type") or payload.get("type") or "").lower()

    if event_type == "book":
        bids = _parse_order_levels(payload.get("bids"))
        asks = _parse_order_levels(payload.get("asks"))
        best_bid = max((level["price"] for level in bids), default=None)
        best_ask = min((level["price"] for level in asks), default=None)
        return [{
            "timestamp": _coerce_timestamp_ms(timestamp),
            "asset_id": asset_id,
            "event_type": "book",
            "hash": payload.get("hash"),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_sizes": [level["size"] for level in sorted(bids, key=lambda item: item["price"], reverse=True)[:5]],
            "ask_sizes": [level["size"] for level in sorted(asks, key=lambda item: item["price"])[:5]],
        }]

    if event_type == "price_change":
        return [{
            "timestamp": _coerce_timestamp_ms(timestamp),
            "asset_id": asset_id,
            "event_type": "price_change",
            "hash": payload.get("hash"),
            "best_bid": _coerce_price(payload.get("best_bid")),
            "best_ask": _coerce_price(payload.get("best_ask")),
            "pc_price": _coerce_price(payload.get("price")),
            "pc_size": _coerce_price(payload.get("size")),
            "pc_side": payload.get("side"),
        }]

    if event_type == "last_trade_price":
        return [{
            "timestamp": _coerce_timestamp_ms(timestamp),
            "asset_id": asset_id,
            "event_type": "last_trade_price",
            "hash": payload.get("hash"),
            "best_bid": _coerce_price(payload.get("best_bid")),
            "best_ask": _coerce_price(payload.get("best_ask")),
            "trade_price": _coerce_price(payload.get("price")),
            "trade_size": _coerce_price(payload.get("size")),
            "trade_side": payload.get("side"),
        }]

    if event_type == "best_bid_ask":
        return [{
            "timestamp": _coerce_timestamp_ms(timestamp),
            "asset_id": asset_id,
            "event_type": "best_bid_ask",
            "best_bid": _coerce_price(payload.get("bid") or payload.get("best_bid")),
            "best_ask": _coerce_price(payload.get("ask") or payload.get("best_ask")),
        }]

    if event_type == "tick_size_change":
        return [{
            "timestamp": _coerce_timestamp_ms(timestamp),
            "asset_id": asset_id,
            "event_type": "tick_size_change",
            "new_tick_size": _coerce_price(payload.get("tick_size") or payload.get("new_tick_size")),
        }]

    return []


class MarketEventStream:
    def __init__(self, *, slug, asset_id, feature_buffer):
        self.slug = slug
        self.asset_id = str(asset_id)
        self.feature_buffer = feature_buffer
        self.last_error = None
        self.last_message_at = None
        self.events_received = 0
        self.messages_received = 0
        self.connected = False
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=f"pm-market-ws:{slug}", daemon=True)
        self._thread.start()

    def _run(self):
        backoff = 1.0
        while not self._stop_event.is_set():
            if websocket is None:
                with self._lock:
                    self.last_error = "websocket-client-not-installed"
                return
            ws = None
            try:
                ws = websocket.create_connection(
                    MARKET_WS_URL,
                    timeout=10,
                    header=["User-Agent: polymarket-fast-market/1.0"],
                )
                subscribe_payload = {
                    "type": "market",
                    "assets_ids": [self.asset_id],
                }
                ws.send(json.dumps(subscribe_payload))
                with self._lock:
                    self.connected = True
                    self.last_error = None
                backoff = 1.0
                while not self._stop_event.is_set():
                    raw_message = ws.recv()
                    if raw_message is None:
                        raise RuntimeError("market-websocket-closed")
                    messages = _normalize_market_ws_events(json.loads(raw_message))
                    with self._lock:
                        self.messages_received += 1
                        self.last_message_at = datetime.now(timezone.utc).isoformat()
                    for message in messages:
                        if self.feature_buffer.ingest_event(message):
                            with self._lock:
                                self.events_received += 1
                                self._ready_event.set()
            except Exception as exc:
                with self._lock:
                    self.connected = False
                    self.last_error = str(exc)
                if self._stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2.0, 15.0)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
        with self._lock:
            self.connected = False

    def wait_until_ready(self, timeout=1.0):
        return self._ready_event.wait(timeout)

    def snapshot(self):
        with self._lock:
            return {
                "slug": self.slug,
                "asset_id": self.asset_id,
                "connected": self.connected,
                "events_received": self.events_received,
                "messages_received": self.messages_received,
                "last_message_at": self.last_message_at,
                "last_error": self.last_error,
            }

    def stop(self):
        self._stop_event.set()
        self._ready_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)


def _prune_market_streams(active_slug):
    for slug, stream in list(_MARKET_STREAM_CACHE.items()):
        if slug == active_slug:
            continue
        stream.stop()
        _MARKET_STREAM_CACHE.pop(slug, None)


def _ensure_market_event_stream(*, slug, feature_token_id, feature_buffer):
    if not slug or not feature_token_id or feature_buffer is None:
        return None
    _prune_market_streams(slug)
    cached = _MARKET_STREAM_CACHE.get(slug)
    if cached is not None and cached.asset_id == str(feature_token_id):
        return cached
    if cached is not None:
        cached.stop()
    stream = MarketEventStream(
        slug=slug,
        asset_id=feature_token_id,
        feature_buffer=feature_buffer,
    )
    _MARKET_STREAM_CACHE[slug] = stream
    return stream

# Asset → Binance symbol mapping
ASSET_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
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
TRADE_DB = cfg["trade_db"]


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

    wallet_type = int(os.environ.get('WALLET_TYPE', '0'))

    if wallet_type == 0:
        client = ClobClient(CLOB_HOST, key=pk, signature_type=0, chain_id=POLYGON)
    else:
        funder_address = os.environ.get('FUNDER_ADDRESS')
        if not funder_address:
            print("Error: FUNDER_ADDRESS environment variable not set")
            print("Set it when WALLET_TYPE is not 0")
            sys.exit(1)
        client = ClobClient(CLOB_HOST, key=pk, signature_type=wallet_type, funder=funder_address, chain_id=POLYGON)
    
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
            req_headers["User-Agent"] = "polymarket-fast-market/1.0"
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
# Sprint Market Resolution
# =============================================================================

def _asset_slug_prefix(asset):
    return asset.lower()


def _bucket_start(now, window):
    minutes = 15 if window == "15m" else 5
    return now - timedelta(
        minutes=now.minute % minutes,
        seconds=now.second,
        microseconds=now.microsecond,
    )


def _window_minutes(window):
    return 15 if window == "15m" else 5


def _direction_from_change(change_pct):
    if change_pct > 0:
        return "up"
    if change_pct < 0:
        return "down"
    return "neutral"


def expected_fast_market_slug(asset="BTC", window="5m", now=None):
    now = now or datetime.now(timezone.utc)
    bucket_start = _bucket_start(now, window)
    return f"{_asset_slug_prefix(asset)}-updown-{window}-{int(bucket_start.timestamp())}", bucket_start


def _parse_json_list(value):
    if isinstance(value, list):
        return value
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _resolve_binary_token_mapping(outcomes, clob_token_ids):
    labels = [str(label or "").strip() for label in outcomes]
    normalized = [label.lower() for label in labels]
    yes_index = None
    no_index = None
    for index, label in enumerate(normalized):
        if label in {"yes", "up", "higher", "above", "true"} and yes_index is None:
            yes_index = index
        if label in {"no", "down", "lower", "below", "false"} and no_index is None:
            no_index = index
    if yes_index is None and len(clob_token_ids) > 0:
        yes_index = 0
    if no_index is None and len(clob_token_ids) > 1:
        no_index = 1 if yes_index != 1 else 0
    yes_token_id = clob_token_ids[yes_index] if yes_index is not None and yes_index < len(clob_token_ids) else None
    no_token_id = clob_token_ids[no_index] if no_index is not None and no_index < len(clob_token_ids) else None
    return {
        "yes_index": yes_index,
        "no_index": no_index,
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "feature_token_id": clob_token_ids[0] if clob_token_ids else None,
    }


def fetch_market_by_slug(slug, window):
    """Fetch a specific fast market by its exact slug."""
    url = f"{GAMMA_HOST}/markets/slug/{quote(slug)}"
    result = _api_request(url)
    if not result or isinstance(result, dict) and result.get("error"):
        return None

    market = result[0] if isinstance(result, list) else result
    if market.get("slug") != slug:
        return None

    interval = _extract_market_interval(market, window)
    clob_token_ids = [str(token) for token in _parse_json_list(market.get("clobTokenIds"))]
    outcomes = _parse_json_list(market.get("outcomes"))
    outcome_prices = _parse_json_list(market.get("outcomePrices"))
    token_mapping = _resolve_binary_token_mapping(outcomes, clob_token_ids)
    return {
        "question": market.get("question", ""),
        "slug": slug,
        "condition_id": market.get("conditionId", ""),
        "slug_timestamp": _parse_slug_timestamp(slug),
        "start_time": interval["start_time"],
        "end_time": interval["end_time"],
        "closed": bool(market.get("closed", False)),
        "outcomes": outcomes,
        "outcome_prices": outcome_prices,
        "fee_rate_bps": int(market.get("fee_rate_bps") or market.get("feeRateBps") or 0),
        "clob_token_ids": clob_token_ids,
        "yes_index": token_mapping["yes_index"],
        "no_index": token_mapping["no_index"],
        "yes_token_id": token_mapping["yes_token_id"],
        "no_token_id": token_mapping["no_token_id"],
        "feature_token_id": token_mapping["feature_token_id"],
    }


def _window_duration(window):
    if window == "15m":
        return timedelta(minutes=15)
    return timedelta(minutes=5)


def _parse_slug_timestamp(slug):
    if not slug:
        return None
    tail = slug.rsplit("-", 1)[-1]
    if not tail.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(tail), tz=timezone.utc)
    except (ValueError, OSError):
        return None


def _parse_api_datetime(value):
    if not value or not isinstance(value, str):
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _extract_market_interval(market, window):
    """Prefer structured API timestamps; fall back to question parsing only if needed."""
    start_time = None
    end_time = None
    slug_timestamp = _parse_slug_timestamp(market.get("slug"))

    for key in ("startDate", "startTime", "activeStartDate", "start"):
        start_time = _parse_api_datetime(market.get(key))
        if start_time is not None:
            break

    for key in ("endDate", "endTime", "activeEndDate", "expirationDate", "expireDate", "end"):
        end_time = _parse_api_datetime(market.get(key))
        if end_time is not None:
            break

    if start_time is None and slug_timestamp is not None:
        start_time = slug_timestamp
    if start_time is None and end_time is not None:
        start_time = end_time - _window_duration(window)
    if end_time is None and start_time is not None:
        end_time = start_time + _window_duration(window)

    if start_time is None or end_time is None:
        parsed = _parse_fast_market_interval(market.get("question", ""))
        start_time = start_time or parsed["start_time"]
        end_time = end_time or parsed["end_time"]

    return {"start_time": start_time, "end_time": end_time}


def _parse_fast_market_interval(question):
    """Parse start/end time from fast market question."""
    import re

    pattern = r'(\w+ \d+),\s*(\d{1,2}:\d{2}(?:AM|PM))\s*-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET'
    match = re.search(pattern, question)
    if not match:
        return {"start_time": None, "end_time": None}
    try:
        date_str = match.group(1)
        start_time_str = match.group(2)
        end_time_str = match.group(3)
        year = datetime.now(NEW_YORK_TZ).year
        start_local = datetime.strptime(
            f"{date_str} {year} {start_time_str}",
            "%B %d %Y %I:%M%p",
        ).replace(tzinfo=NEW_YORK_TZ)
        end_local = datetime.strptime(
            f"{date_str} {year} {end_time_str}",
            "%B %d %Y %I:%M%p",
        ).replace(tzinfo=NEW_YORK_TZ)

        now_local = datetime.now(NEW_YORK_TZ)
        if end_local < now_local and (now_local - end_local).days > 300:
            start_local = start_local.replace(year=start_local.year + 1)
            end_local = end_local.replace(year=end_local.year + 1)

        if end_local <= start_local:
            end_local = end_local + timedelta(days=1)

        return {
            "start_time": start_local.astimezone(timezone.utc),
            "end_time": end_local.astimezone(timezone.utc),
        }
    except Exception:
        return {"start_time": None, "end_time": None}


# =============================================================================
# CEX Price Signal
# =============================================================================

def _resolve_binance_reference_close(candles, *, window, now=None):
    """Use the last closed 1m candle before the current market bucket as the anchor price."""
    now = now or datetime.now(timezone.utc)
    current_bucket_start = _bucket_start(now, window)
    reference_open_ts_ms = int((current_bucket_start - timedelta(minutes=1)).timestamp() * 1000)

    reference_candle = None
    for candle in candles:
        try:
            open_time_ms = int(candle[0])
        except (TypeError, ValueError, IndexError):
            continue
        if open_time_ms == reference_open_ts_ms:
            reference_candle = candle
            break
        if open_time_ms < int(current_bucket_start.timestamp() * 1000):
            reference_candle = candle

    if reference_candle is None:
        return None

    try:
        return float(reference_candle[4])
    except (TypeError, ValueError, IndexError):
        return None


def _resolve_binance_rolling_reference_close(candles, *, window, now=None):
    """Use the 1m close from exactly one rolling window ago."""
    now = now or datetime.now(timezone.utc)
    current_minute = now.replace(second=0, microsecond=0)
    target_open_ts_ms = int((current_minute - timedelta(minutes=_window_minutes(window))).timestamp() * 1000)

    reference_candle = None
    for candle in candles:
        try:
            open_time_ms = int(candle[0])
        except (TypeError, ValueError, IndexError):
            continue
        if open_time_ms == target_open_ts_ms:
            try:
                return float(candle[4])
            except (TypeError, ValueError, IndexError):
                return None
        if open_time_ms <= target_open_ts_ms:
            reference_candle = candle

    if reference_candle is None:
        return None

    try:
        return float(reference_candle[4])
    except (TypeError, ValueError, IndexError):
        return None


def get_binance_momentum(symbol="BTCUSDT", lookback_minutes=5, window="5m"):
    """Get price momentum from Binance public API.
    Returns: {momentum_pct, direction, price_now, price_then, avg_volume, candles}
    """
    window_minutes = _window_minutes(window)
    limit = max(lookback_minutes, window_minutes + 1, (window_minutes * 2) + 1)
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=1m&limit={limit}"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict):
        return None

    try:
        # Kline format: [open_time, open, high, low, close, volume, ...]
        candles = result
        if len(candles) < 2:
            return None

        price_then = _resolve_binance_reference_close(candles, window=window)
        rolling_reference_close = _resolve_binance_rolling_reference_close(candles, window=window)
        if price_then is None or rolling_reference_close is None:
            return None

        price_now = float(candles[-1][4])    # close of newest candle (latest trade on the active 1m candle)
        anchor_momentum_pct = ((price_now - price_then) / price_then) * 100
        window_momentum_pct = ((price_now - rolling_reference_close) / rolling_reference_close) * 100
        anchor_direction = _direction_from_change(anchor_momentum_pct)
        window_direction = _direction_from_change(window_momentum_pct)
        momentum_consistent = (
            anchor_direction in {"up", "down"}
            and anchor_direction == window_direction
        )
        momentum_pct = (
            min(abs(anchor_momentum_pct), abs(window_momentum_pct))
            * (1 if anchor_direction == "up" else -1)
            if momentum_consistent
            else 0.0
        )
        direction = anchor_direction if momentum_consistent else "neutral"

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
            "rolling_reference_close": rolling_reference_close,
            "anchor_momentum_pct": anchor_momentum_pct,
            "window_momentum_pct": window_momentum_pct,
            "anchor_direction": anchor_direction,
            "window_direction": window_direction,
            "momentum_consistent": momentum_consistent,
            "avg_volume": avg_volume,
            "latest_volume": latest_volume,
            "volume_ratio": volume_ratio,
            "candles": len(candles),
            "reference_window": window,
            "ohlcv": [
                [
                    float(c[1]),
                    float(c[2]),
                    float(c[3]),
                    float(c[4]),
                    float(c[5]),
                ]
                for c in candles
            ],
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
        "ohlcv": [[float(price_now), float(price_now), float(price_now), float(price_now), 0.0]],
    }


COINGECKO_ASSETS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}


def get_momentum(asset="BTC", source="binance", lookback=5):
    """Get price momentum from configured source."""
    if source == "binance":
        symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
        return get_binance_momentum(symbol, lookback, WINDOW)
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


def get_token_midpoint(token_id):
    """Get live midpoint price for a token from the public CLOB API."""
    if not token_id:
        return None
    result = _api_request(f"{CLOB_HOST}/midpoint?token_id={token_id}")
    if isinstance(result, dict) and not result.get("error"):
        value = result.get("mid")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def fetch_orderbook_summary(clob_token_ids):
    """Fetch a lightweight YES-token order book summary."""
    if not clob_token_ids or len(clob_token_ids) < 1:
        return None
    yes_token_id = clob_token_ids[0]
    result = _api_request(f"{CLOB_HOST}/book?token_id={quote(str(yes_token_id))}", timeout=5)
    if not result or not isinstance(result, dict) or result.get("error"):
        return None

    bids = result.get("bids", [])
    asks = result.get("asks", [])
    if not bids or not asks:
        return None

    try:
        bid_levels = [
            {
                "price": float(level.get("price", 0)),
                "size": float(level.get("size", 0)),
            }
            for level in bids
        ]
        ask_levels = [
            {
                "price": float(level.get("price", 0)),
                "size": float(level.get("size", 0)),
            }
            for level in asks
        ]
        bid_levels = [level for level in bid_levels if level["price"] > 0 and level["size"] > 0]
        ask_levels = [level for level in ask_levels if level["price"] > 0 and level["size"] > 0]
        if not bid_levels or not ask_levels:
            return None

        best_bid = max(level["price"] for level in bid_levels)
        best_ask = min(level["price"] for level in ask_levels)
        if best_bid > best_ask:
            best_bid, best_ask = best_ask, best_bid
        mid = (best_bid + best_ask) / 2
        spread_pct = (abs(best_ask - best_bid) / mid) if mid > 0 else 0.0
        top_bid_levels = sorted(bid_levels, key=lambda level: level["price"], reverse=True)[:5]
        top_ask_levels = sorted(ask_levels, key=lambda level: level["price"])[:5]
        bid_depth_usd = sum(level["price"] * level["size"] for level in top_bid_levels)
        ask_depth_usd = sum(level["price"] * level["size"] for level in top_ask_levels)
        bid_sizes = [round(level["size"], 6) for level in top_bid_levels]
        ask_sizes = [round(level["size"], 6) for level in top_ask_levels]
        return {
            "best_bid": round(best_bid, 6),
            "best_ask": round(best_ask, 6),
            "spread_pct": round(spread_pct, 6),
            "bid_depth_usd": round(bid_depth_usd, 6),
            "ask_depth_usd": round(ask_depth_usd, 6),
            "bid_sizes": bid_sizes,
            "ask_sizes": ask_sizes,
        }
    except (TypeError, ValueError, KeyError, IndexError):
        return None

def _db_path():
    path = Path(TRADE_DB)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path


def _skill_root():
    return Path(__file__).resolve().parents[1]


def _session_paths(session_started_at):
    root = _skill_root()
    return {
        "history": root / f"market_history_{session_started_at}.json",
        "summary": root / f"market_summary_{session_started_at}.json",
    }


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=_json_default)
        f.write("\n")
    os.replace(tmp_path, path)


def _append_history_event(session_started_at, event):
    paths = _session_paths(session_started_at)
    history_path = paths["history"]
    if history_path.exists():
        try:
            with open(history_path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            payload = {}
    else:
        payload = {}

    payload.setdefault("schema_version", SESSION_SCHEMA_VERSION)
    payload.setdefault(
        "session",
        {
            "session_started_at": session_started_at,
            "session_started_at_iso": datetime.fromtimestamp(session_started_at, tz=timezone.utc).isoformat(),
            "history_file": str(paths["history"]),
            "summary_file": str(paths["summary"]),
        },
    )
    payload.setdefault("config", {
        "asset": ASSET,
        "window": WINDOW,
        "trade_db": str(_db_path()),
    })
    payload.setdefault("events", [])
    compact_events = _compact_history_events(event)
    if compact_events:
        payload["events"].extend(compact_events)
    payload["runtime"] = {
        "event_count": len(payload["events"]),
        "last_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(history_path, payload)


def _write_summary(session_started_at, summary):
    _atomic_write_json(_session_paths(session_started_at)["summary"], summary)


def _runtime_mode(live_mode):
    return "live" if live_mode else "paper"


def _resolved_config_snapshot():
    return {
        "asset": ASSET,
        "window": WINDOW,
        "entry_threshold": ENTRY_THRESHOLD,
        "min_momentum_pct": MIN_MOMENTUM_PCT,
        "max_position_usd": MAX_POSITION_USD,
        "signal_source": SIGNAL_SOURCE,
        "lookback_minutes": LOOKBACK_MINUTES,
        "min_time_remaining_seconds": MIN_TIME_REMAINING,
        "volume_confidence": VOLUME_CONFIDENCE,
        "trade_db": str(_db_path()),
    }


def _compact_history_events(cycle):
    """Keep only trade-critical events for market_history."""
    compact = []
    market = cycle.get("market") or {}

    for closed_trade in cycle.get("closed_trades") or []:
        compact.append(
            {
                "event_type": "exit",
                "timestamp": closed_trade.get("closed_at") or cycle.get("cycle_completed_at"),
                "market_slug": closed_trade.get("market_slug"),
                "execution_mode": closed_trade.get("execution_mode"),
                "market_name": closed_trade.get("market_name"),
                "direction": closed_trade.get("direction"),
                "entry_price": closed_trade.get("entry_price"),
                "exit_price": closed_trade.get("exit_price"),
                "quantity": closed_trade.get("quantity"),
                "pnl": closed_trade.get("pnl"),
                "trade_id": closed_trade.get("trade_id"),
            }
        )

    trade = cycle.get("trade") or {}
    decision = cycle.get("decision") or {}
    if trade.get("recorded"):
        compact.append(
            {
                "event_type": "entry",
                "timestamp": cycle.get("cycle_completed_at"),
                "market_slug": market.get("slug"),
                "execution_mode": trade.get("mode"),
                "market_name": market.get("question"),
                "direction": trade.get("side"),
                "price": trade.get("price"),
                "quantity": trade.get("estimated_shares"),
                "position_size_usd": trade.get("position_size_usd"),
                "divergence": decision.get("divergence"),
                "trade_rationale": decision.get("trade_rationale"),
            }
        )
    elif cycle.get("status") == "error":
        compact.append(
            {
                "event_type": "error",
                "timestamp": cycle.get("cycle_completed_at"),
                "market_slug": market.get("slug"),
                "reason": cycle.get("reason"),
                "error": cycle.get("error"),
            }
        )

    return compact


def ensure_paper_db():
    """Create the SQLite schema if it does not exist yet."""
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if LEGACY_TRADES_TABLE in tables and TRADES_TABLE not in tables:
            conn.execute(f"ALTER TABLE {LEGACY_TRADES_TABLE} RENAME TO {TRADES_TABLE}")

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TRADES_TABLE} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              market_id TEXT NOT NULL,
              market_slug TEXT,
              execution_mode TEXT NOT NULL DEFAULT 'paper',
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
        columns = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({TRADES_TABLE})").fetchall()
        }
        if "market_slug" not in columns:
            conn.execute(f"ALTER TABLE {TRADES_TABLE} ADD COLUMN market_slug TEXT")
        if "execution_mode" not in columns:
            conn.execute(f"ALTER TABLE {TRADES_TABLE} ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'paper'")


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
            SELECT market_id, market_slug, execution_mode, market_name, direction, entry_price, quantity, signal_momentum_pct,
                   volume_ratio, created_at
            FROM polymarket_fast_market_trades
            WHERE status = 'OPEN' AND execution_mode = 'paper'
            ORDER BY created_at DESC
            """
        ).fetchall()
    return rows


def get_open_trades():
    """Return all open paper trades from SQLite."""
    ensure_paper_db()
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, market_id, market_slug, execution_mode, market_name, direction, entry_price, quantity,
                   signal_momentum_pct, volume_ratio, created_at
            FROM polymarket_fast_market_trades
            WHERE status = 'OPEN'
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    return rows


def get_latest_open_trade():
    """Return the latest open paper trade for mark-to-market reporting."""
    ensure_paper_db()
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, market_id, market_slug, execution_mode, market_name, direction, entry_price, quantity,
                   signal_momentum_pct, volume_ratio, created_at
            FROM polymarket_fast_market_trades
            WHERE status = 'OPEN' AND execution_mode = 'paper'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    return row


def get_latest_open_trade_for_mode(execution_mode):
    """Return the latest open trade for the given execution mode."""
    ensure_paper_db()
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, market_id, market_slug, execution_mode, market_name, direction, entry_price, quantity,
                   signal_momentum_pct, volume_ratio, created_at
            FROM polymarket_fast_market_trades
            WHERE status = 'OPEN' AND execution_mode = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (execution_mode,),
        ).fetchone()
    return row


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


def record_trade(market, direction, entry_price, quantity, momentum_pct, volume_ratio, execution_mode="paper"):
    """Persist a trade candidate and refresh the paper portfolio when needed."""
    ensure_paper_db()
    db_path = _db_path()
    position_cost = entry_price * quantity

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        existing_open = conn.execute(
            """
            SELECT id FROM polymarket_fast_market_trades
            WHERE market_id = ? AND direction = ? AND execution_mode = ? AND status = 'OPEN'
            """,
            (market["condition_id"], direction, execution_mode),
        ).fetchone()
        if existing_open is not None:
            return False, f"{execution_mode.capitalize()} trade already recorded for this market and direction"

        cash = None
        positions = []
        if execution_mode == "paper":
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
                    "market_slug": market.get("slug"),
                    "market_name": market["question"],
                    "direction": direction,
                    "entry_price": round(entry_price, 4),
                    "quantity": round(quantity, 4),
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        conn.execute(
            """
            INSERT INTO polymarket_fast_market_trades (
              market_id, market_slug, execution_mode, market_name, strategy, direction, entry_price, quantity,
              signal_momentum_pct, volume_ratio
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market["condition_id"],
                market.get("slug"),
                execution_mode,
                market["question"],
                TRADE_SOURCE,
                direction,
                entry_price,
                quantity,
                momentum_pct,
                volume_ratio,
            ),
        )
        if execution_mode == "paper":
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


def _parse_yes_price(market):
    live_yes_price = market.get("live_yes_price")
    if live_yes_price is not None:
        try:
            return float(live_yes_price)
        except (TypeError, ValueError):
            pass
    try:
        prices = market.get("outcome_prices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        yes_index = market.get("yes_index")
        if isinstance(prices, list) and yes_index is not None and yes_index < len(prices):
            return float(prices[yes_index])
        if isinstance(prices, list) and prices:
            return float(prices[0])
    except (json.JSONDecodeError, IndexError, TypeError, ValueError):
        return 0.5
    return 0.5


def refresh_market_price(market):
    """Overlay live CLOB midpoint onto the market snapshot when available."""
    yes_token_id = market.get("yes_token_id")
    live_yes_price = get_token_midpoint(yes_token_id)
    if live_yes_price is not None:
        market["live_yes_price"] = live_yes_price
    return market


def _mark_to_market_snapshot(trade_row, market):
    """Compute current unrealized P&L for the latest open paper trade."""
    if trade_row is None:
        return None

    yes_price = _parse_yes_price(market)
    direction = (trade_row["direction"] or "").lower()
    current_price = yes_price if direction == "yes" else (1 - yes_price)
    entry_price = float(trade_row["entry_price"])
    quantity = float(trade_row["quantity"])
    pnl = (current_price - entry_price) * quantity
    pnl_ratio = 0.0 if entry_price == 0 else (current_price - entry_price) / entry_price

    return {
        "trade_id": trade_row["id"],
        "market_id": trade_row["market_id"],
        "market_slug": trade_row["market_slug"],
        "execution_mode": trade_row["execution_mode"],
        "market_name": trade_row["market_name"],
        "direction": direction,
        "entry_price": entry_price,
        "current_price": round(current_price, 6),
        "quantity": quantity,
        "unrealized_pnl": round(pnl, 6),
        "unrealized_pnl_ratio": round(pnl_ratio, 6),
        "created_at": trade_row["created_at"],
    }


def _settlement_price(market, direction):
    yes_price = _parse_yes_price(market)
    direction = (direction or "").lower()
    if market.get("closed"):
        if yes_price >= 0.999:
            return 1.0 if direction == "yes" else 0.0
        if yes_price <= 0.001:
            return 0.0 if direction == "yes" else 1.0
    return yes_price if direction == "yes" else (1 - yes_price)


def close_expired_paper_trades(now=None):
    """Settle paper trades whose markets have ended and refresh portfolio state."""
    now = now or datetime.now(timezone.utc)
    open_trades = get_open_trades()
    if not open_trades:
        return []

    settlements = []
    db_path = _db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        portfolio_row = conn.execute(
            "SELECT cash, positions FROM portfolio WHERE id = 1"
        ).fetchone()
        cash = float(portfolio_row["cash"])
        positions = json.loads(portfolio_row["positions"] or "[]")

        for trade in open_trades:
            market_slug = trade["market_slug"]
            market = _resolve_open_trade_market(trade)
            if market is None:
                continue
            end_time = market.get("end_time")
            if end_time is None or end_time > now:
                continue

            exit_price = _settlement_price(market, trade["direction"])
            entry_price = float(trade["entry_price"])
            quantity = float(trade["quantity"])
            pnl = (exit_price - entry_price) * quantity
            proceeds = exit_price * quantity

            conn.execute(
                """
                UPDATE polymarket_fast_market_trades
                SET exit_price = ?, pnl = ?, status = 'CLOSED', closed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (exit_price, pnl, trade["id"]),
            )

            if trade["execution_mode"] == "paper":
                positions = [
                    p for p in positions
                    if not (
                        p.get("market_id") == trade["market_id"]
                        and p.get("direction") == trade["direction"]
                        and p.get("market_slug") == market_slug
                    )
                ]
                cash += proceeds
            settlements.append(
                {
                    "trade_id": trade["id"],
                    "market_id": trade["market_id"],
                    "market_slug": market_slug,
                    "execution_mode": trade["execution_mode"],
                    "market_name": trade["market_name"],
                    "direction": trade["direction"],
                    "entry_price": round(entry_price, 6),
                    "exit_price": round(exit_price, 6),
                    "quantity": round(quantity, 6),
                    "pnl": round(pnl, 6),
                    "closed_at": now.isoformat(),
                    "market_closed": market.get("closed", False),
                }
            )

        remaining_value = 0.0
        for position in positions:
            remaining_value += float(position.get("entry_price", 0) or 0) * float(position.get("quantity", 0) or 0)
        total_value = cash + remaining_value
        conn.execute(
            """
            UPDATE portfolio
            SET cash = ?, total_value = ?, positions = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (cash, total_value, json.dumps(positions)),
        )

    return settlements


def close_paper_trade(trade_row, market, closed_at=None, reason="signal-exit"):
    """Close an open paper trade immediately at the current market price."""
    if trade_row is None or market is None:
        return None

    closed_at = closed_at or datetime.now(timezone.utc)
    db_path = _db_path()
    exit_price = _settlement_price(market, trade_row["direction"])
    entry_price = float(trade_row["entry_price"])
    quantity = float(trade_row["quantity"])
    pnl = (exit_price - entry_price) * quantity
    proceeds = exit_price * quantity

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        portfolio_row = conn.execute(
            "SELECT cash, positions FROM portfolio WHERE id = 1"
        ).fetchone()
        cash = float(portfolio_row["cash"])
        positions = json.loads(portfolio_row["positions"] or "[]")

        conn.execute(
            """
            UPDATE polymarket_fast_market_trades
            SET exit_price = ?, pnl = ?, status = 'CLOSED', closed_at = ?
            WHERE id = ?
            """,
            (exit_price, pnl, closed_at.isoformat(), trade_row["id"]),
        )

        positions = [
            p for p in positions
            if not (
                p.get("market_id") == trade_row["market_id"]
                and p.get("direction") == trade_row["direction"]
                and p.get("market_slug") == trade_row["market_slug"]
            )
        ]
        cash += proceeds
        total_value = cash + sum(float(p.get("entry_price", 0) or 0) * float(p.get("quantity", 0) or 0) for p in positions)
        conn.execute(
            """
            UPDATE portfolio
            SET cash = ?, total_value = ?, positions = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (cash, total_value, json.dumps(positions)),
        )

    return {
        "trade_id": trade_row["id"],
        "market_id": trade_row["market_id"],
        "market_slug": trade_row["market_slug"],
        "execution_mode": "paper",
        "market_name": trade_row["market_name"],
        "direction": trade_row["direction"],
        "entry_price": round(entry_price, 6),
        "exit_price": round(exit_price, 6),
        "quantity": round(quantity, 6),
        "pnl": round(pnl, 6),
        "closed_at": closed_at.isoformat(),
        "close_reason": reason,
        "market_closed": bool(market.get("closed", False)),
    }


def build_trading_summary(session_started_at, loop_enabled, loop_interval_seconds=None, last_error=None):
    """Build a compact trading summary focused on PnL statistics."""
    ensure_paper_db()
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        stats = conn.execute(
            """
            SELECT
              execution_mode,
              COUNT(*) AS total_trades,
              SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_trades,
              SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed_trades,
              SUM(CASE WHEN status = 'CLOSED' AND pnl > 0 THEN 1 ELSE 0 END) AS winning_trades,
              SUM(CASE WHEN status = 'CLOSED' AND pnl < 0 THEN 1 ELSE 0 END) AS losing_trades,
              COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN pnl ELSE 0 END), 0) AS realized_pnl
            FROM polymarket_fast_market_trades
            GROUP BY execution_mode
            """
        ).fetchall()
        recent = conn.execute(
            """
            SELECT market_slug, execution_mode, direction, entry_price, exit_price, quantity, pnl, status, created_at, closed_at
            FROM polymarket_fast_market_trades
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

    stats_by_mode = {
        row["execution_mode"]: {
            "total_trades": int(row["total_trades"] or 0),
            "open_trades": int(row["open_trades"] or 0),
            "closed_trades": int(row["closed_trades"] or 0),
            "winning_trades": int(row["winning_trades"] or 0),
            "losing_trades": int(row["losing_trades"] or 0),
            "realized_pnl": round(float(row["realized_pnl"] or 0), 6),
        }
        for row in stats
    }

    def mode_summary(mode):
        mode_stats = stats_by_mode.get(
            mode,
            {
                "total_trades": 0,
                "open_trades": 0,
                "closed_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "realized_pnl": 0.0,
            },
        )
        closed_trades = mode_stats["closed_trades"]
        winning_trades = mode_stats["winning_trades"]
        mode_stats["win_rate"] = round((winning_trades / closed_trades) if closed_trades else 0.0, 6)
        return mode_stats

    paper_stats = mode_summary("paper")
    live_stats = mode_summary("live")
    portfolio = get_paper_portfolio()
    combined_total = paper_stats["total_trades"] + live_stats["total_trades"]
    combined_closed = paper_stats["closed_trades"] + live_stats["closed_trades"]
    combined_winning = paper_stats["winning_trades"] + live_stats["winning_trades"]

    return {
        "schema_version": SESSION_SCHEMA_VERSION,
        "session": {
            "session_started_at": session_started_at,
            "session_started_at_iso": datetime.fromtimestamp(session_started_at, tz=timezone.utc).isoformat(),
            "mode": "paper-default",
            "history_file": str(_session_paths(session_started_at)["history"]),
            "summary_file": str(_session_paths(session_started_at)["summary"]),
        },
        "runtime": {
            "loop_enabled": loop_enabled,
            "loop_interval_seconds": loop_interval_seconds,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_error": last_error,
        },
        "paper_pnl": {
            **paper_stats,
            "cash": round(float(portfolio["cash"]), 6),
            "portfolio_value": round(float(portfolio["total_value"]), 6),
        },
        "live_pnl": live_stats,
        "combined_trade_stats": {
            "total_trades": combined_total,
            "open_trades": paper_stats["open_trades"] + live_stats["open_trades"],
            "closed_trades": combined_closed,
            "winning_trades": combined_winning,
            "losing_trades": paper_stats["losing_trades"] + live_stats["losing_trades"],
            "win_rate": round((combined_winning / combined_closed) if combined_closed else 0.0, 6),
            "realized_pnl": round(paper_stats["realized_pnl"] + live_stats["realized_pnl"], 6),
        },
        "open_positions": [
            {
                "market_slug": position.get("market_slug"),
                "direction": position.get("direction"),
                "entry_price": position.get("entry_price"),
                "quantity": position.get("quantity"),
                "recorded_at": position.get("recorded_at"),
            }
            for position in portfolio.get("positions", [])
        ],
        "recent_trades": [
            {
                "market_slug": row["market_slug"],
                "execution_mode": row["execution_mode"],
                "direction": row["direction"],
                "entry_price": row["entry_price"],
                "exit_price": row["exit_price"],
                "quantity": row["quantity"],
                "pnl": row["pnl"],
                "status": row["status"],
                "created_at": row["created_at"],
                "closed_at": row["closed_at"],
            }
            for row in recent
        ],
    }


def _resolve_open_trade_market(trade_row, default_market=None):
    if trade_row is None:
        return default_market
    trade_market_slug = trade_row["market_slug"]
    if trade_market_slug:
        window = "15m" if "-15m-" in trade_market_slug else "5m"
        trade_market = fetch_market_by_slug(trade_market_slug, window)
        if trade_market is not None:
            return refresh_market_price(trade_market)
    return default_market


# =============================================================================
# Main Strategy Logic
# =============================================================================

def run_fast_market_strategy(
    session_started_at_ts=None,
    live_mode=False,
    positions_only=False,
    paper_positions_only=False,
    show_config=False,
    smart_sizing=False,
    quiet=False,
    simple_display=False,
    debug_model=False,
):
    """Run one cycle of the fast-market trading strategy and return a cycle summary."""

    cycle_started_at = datetime.now(timezone.utc)
    cycle = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "event_type": "cycle",
        "cycle_started_at": cycle_started_at.isoformat(),
        "mode": _runtime_mode(live_mode),
        "status": "running",
        "action": "none",
        "reason": None,
        "config": _resolved_config_snapshot(),
        "market": None,
        "momentum": None,
        "decision": None,
        "trade": None,
        "order_book": None,
        "portfolio": None,
        "last_open_trade": None,
        "closed_trades": [],
        "error": None,
        "debug_model": debug_model,
    }
    startup_started_at = (
        datetime.fromtimestamp(session_started_at_ts, timezone.utc)
        if session_started_at_ts is not None
        else None
    )

    def log(msg, force=False):
        if not quiet or force:
            print(msg)

    def finalize(status, action="none", reason=None, error=None):
        cycle["status"] = status
        cycle["action"] = action
        cycle["reason"] = reason
        cycle["error"] = error
        cycle["cycle_completed_at"] = datetime.now(timezone.utc).isoformat()
        return cycle

    def log_model_debug(feature_window, model_info):
        if not debug_model or feature_window is None:
            return
        try:
            ohlcv = feature_window.ohlcv
            order_book = feature_window.order_book
            price = feature_window.price
            log("\n🧪 Model Debug")
            log(
                "  Window: "
                f"slug={feature_window.slug} "
                f"start={feature_window.market_start_ts} "
                f"end={feature_window.market_end_ts} "
                f"observed={feature_window.observed_end_ts}"
            )
            log(
                "  Shapes: "
                f"ohlcv={getattr(ohlcv, 'shape', None)} "
                f"order_book={getattr(order_book, 'shape', None)} "
                f"price={getattr(price, 'shape', None)}"
            )
            log(
                "  Price stats: "
                f"first={float(price[0][0]):.6f} "
                f"last={float(price[-1][0]):.6f} "
                f"min={float(price.min()):.6f} "
                f"max={float(price.max()):.6f}"
            )
            log(
                "  Volume stats: "
                f"sum={float(ohlcv[:, 4].sum()):.6f} "
                f"last={float(ohlcv[-1, 4]):.6f} "
                f"nonzero={int((ohlcv[:, 4] > 0).sum())}/{ohlcv.shape[0]}"
            )
            log(
                "  Book stats: "
                f"best_bid={float(order_book[-1, 0]):.6f} "
                f"best_ask={float(order_book[-1, 1]):.6f} "
                f"spread={float(order_book[-1, 2]):.6f} "
                f"bid_depth={float(order_book[-1, 3]):.6f} "
                f"ask_depth={float(order_book[-1, 4]):.6f} "
                f"imbalance={float(order_book[-1, 5]):.6f}"
            )
            repeated_rows = 0
            for idx in range(1, len(price)):
                if float(price[idx][0]) == float(price[idx - 1][0]):
                    repeated_rows += 1
            log(f"  Sequence repeats: {repeated_rows}/{max(len(price) - 1, 1)} adjacent price rows unchanged")
            model = model_info.get("model")
            if model is not None:
                prepared = model.prepare_features(
                    ohlcv=feature_window.ohlcv,
                    order_book=feature_window.order_book,
                    price=feature_window.price,
                )
                log(
                    "  Prepared features: "
                    f"shape={tuple(prepared.shape)} "
                    f"mean={float(prepared.mean()):.6f} "
                    f"std={float(prepared.std()):.6f} "
                    f"min={float(prepared.min()):.6f} "
                    f"max={float(prepared.max()):.6f}"
                )
        except Exception as exc:
            log(f"  Model debug failed: {exc}")

    log("⚡ Polymarket Fast Market Trader")
    log("=" * 50)

    if live_mode:
        log("\n  [LIVE MODE] Qualifying signals will place real Polymarket orders.", force=True)
    else:
        log("\n  [PAPER MODE] Qualifying signals will be recorded to SQLite.")

    # log(f"\n⚙️  Configuration:")
    # log(f"  Asset:            {ASSET}")
    # log(f"  Window:           {WINDOW}")
    # log(f"  Entry threshold:  {ENTRY_THRESHOLD} (min divergence from 50¢)")
    # log(f"  Min momentum:     {MIN_MOMENTUM_PCT}% (min price move)")
    # log(f"  Max position:     ${MAX_POSITION_USD:.2f}")
    # log(f"  Signal source:    {SIGNAL_SOURCE}")
    # log(f"  Lookback:         {LOOKBACK_MINUTES} minutes")
    # log(f"  Min time left:    {MIN_TIME_REMAINING}s")
    # log(f"  Volume weighting: {'✓' if VOLUME_CONFIDENCE else '✗'}")

    if show_config:
        config_path = _get_config_path(__file__)
        log(f"\n  Config file: {config_path}")
        log(f"  Paper DB:    {_db_path()}")
        log(f"\n  To change settings:")
        log("    python polymarket_fast_market.py --set entry_threshold=0.08")
        log("    python polymarket_fast_market.py --set asset=ETH")
        log("    Or edit config.json directly")
        return finalize("ok", action="show-config")

    if positions_only:
        _ensure_clob_imports()
        address = get_wallet_address()
        log("\n📊 Live Sprint Positions:")
        positions = get_live_positions(address)
        fast_market_positions = [
            p for p in positions
            if "up or down" in (p.get("title", "") or p.get("question", "") or "").lower()
        ]
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
        cycle["positions"] = fast_market_positions
        return finalize("ok", action="show-live-positions")

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
        cycle["portfolio"] = portfolio
        cycle["positions"] = [dict(pos) for pos in positions]
        log("\n💰 Paper Portfolio:")
        log(f"  Total value: ${portfolio['total_value']:.2f}")
        log(f"  Cash:        ${portfolio['cash']:.2f}")
        return finalize("ok", action="show-paper-positions")

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
            cycle["portfolio"] = portfolio
            log(f"  Total value: ${portfolio['total_value']:.2f}")
            log(f"  Cash:        ${portfolio['cash']:.2f}")

    if not live_mode:
        closed_trades = close_expired_paper_trades(now=cycle_started_at)
        cycle["closed_trades"] = closed_trades
        if closed_trades:
            log(f"\n🧾 Settled {len(closed_trades)} expired paper trade(s)")
            for closed_trade in closed_trades:
                log(
                    f"  • {closed_trade['market_slug']} {closed_trade['direction'].upper()} "
                    f"PnL ${closed_trade['pnl']:+.2f}"
                )
        cycle["portfolio"] = get_paper_portfolio()

    log(f"\n🔍 Resolving current {ASSET} {WINDOW} fast market slug...")
    expected_slug, expected_start = expected_fast_market_slug(ASSET, WINDOW)
    log(f"  Expected slug: {expected_slug}")
    best = fetch_market_by_slug(expected_slug, WINDOW)
    cycle["markets_considered"] = 1 if best else 0
    cycle["expected_slug"] = expected_slug
    cycle["expected_bucket_start"] = expected_start.isoformat()

    if not best:
        log("  No market found for the current slug")
        if not quiet:
            print("📊 Summary: No market found for current time bucket")
        return finalize("ok", reason="no-market-for-current-slug")

    best = refresh_market_price(best)

    end_time = best.get("end_time")
    remaining = (end_time - datetime.now(timezone.utc)).total_seconds() if end_time else 0
    market_yes_price = _parse_yes_price(best)
    startup_skip_current_market = (
        startup_started_at is not None
        and startup_started_at > expected_start + timedelta(seconds=30)
    )
    startup_lag_seconds = (
        (startup_started_at - expected_start).total_seconds()
        if startup_started_at is not None
        else None
    )
    cycle["market"] = {
        "question": best["question"],
        "slug": best.get("slug"),
        "condition_id": best.get("condition_id"),
        "expected_slug": expected_slug,
        "start_time": best.get("start_time"),
        "end_time": best.get("end_time"),
        "seconds_to_expiry": round(remaining, 3),
        "yes_price": round(market_yes_price, 6),
        "startup_started_at": startup_started_at.isoformat() if startup_started_at is not None else None,
        "startup_lag_seconds": round(startup_lag_seconds, 3) if startup_lag_seconds is not None else None,
        "startup_skip_current_market": startup_skip_current_market,
    }

    log(f"\n🎯 Selected: {best['question']}")
    if best.get("slug"):
        log(f"  Slug: {best['slug']}")
    if best.get("start_time"):
        log(f"  Interval start: {best['start_time'].astimezone(NEW_YORK_TZ).strftime('%Y-%m-%d %I:%M:%S %p %Z')}")
    log(f"  Expires in: {remaining:.0f}s")
    log(f"  Current YES price: ${market_yes_price:.3f}")
    if startup_skip_current_market:
        log(
            "  Startup guard: current event started "
            f"{startup_lag_seconds:.0f}s before this process; wait for the next event"
        )
        if not quiet:
            print(
                "📊 Summary: No trade❌ "
                f"(started {startup_lag_seconds:.0f}s after event open; waiting for next event)"
            )
        return finalize(
            "ok",
            action="wait-next-event",
            reason="startup-too-late-for-current-event",
        )

    execution_mode = "live" if live_mode else "paper"
    latest_open_trade = get_latest_open_trade_for_mode(execution_mode)
    if latest_open_trade is not None:
        trade_market = _resolve_open_trade_market(latest_open_trade, default_market=best)
        cycle["last_open_trade"] = _mark_to_market_snapshot(latest_open_trade, trade_market)
        if cycle["last_open_trade"] is not None:
            pnl_ratio = cycle["last_open_trade"]["unrealized_pnl_ratio"]
            log(f"  Last open {execution_mode} trade PnL ratio: {pnl_ratio:+.2%}")

    fee_rate_bps = best.get("fee_rate_bps", 0)
    fee_rate = fee_rate_bps / 10000
    if fee_rate > 0:
        log(f"  Fee rate:         {fee_rate:.0%} (Polymarket fast market fee)")

    clob_token_ids = best.get("clob_token_ids", [])
    yes_token_id = best.get("yes_token_id")
    no_token_id = best.get("no_token_id")
    feature_token_id = best.get("feature_token_id")
    cycle["order_book"] = fetch_orderbook_summary([yes_token_id])
    if cycle["order_book"] is not None:
        log(
            f"  Order book: bid ${cycle['order_book']['best_bid']:.3f} "
            f"/ ask ${cycle['order_book']['best_ask']:.3f} "
            f"(spread {cycle['order_book']['spread_pct']:.2%})"
        )

    log(f"\n📈 Fetching {ASSET} price signal ({SIGNAL_SOURCE})...")
    momentum = get_momentum(ASSET, SIGNAL_SOURCE, LOOKBACK_MINUTES)
    if not momentum:
        log("  ❌ Failed to fetch price data", force=True)
        return finalize("error", reason="signal-fetch-failed", error="failed-to-fetch-price-data")

    cycle["momentum"] = momentum
    log(f"  Price: ${momentum['price_now']:,.2f} (was ${momentum['price_then']:,.2f})")
    log(f"  Momentum: {momentum['momentum_pct']:+.3f}%")
    log(f"  Direction: {momentum['direction']}")
    if momentum.get("anchor_momentum_pct") is not None and momentum.get("window_momentum_pct") is not None:
        log(
            "  Momentum detail: "
            f"anchor {momentum['anchor_momentum_pct']:+.3f}% "
            f"({momentum.get('anchor_direction')}) | "
            f"window {momentum['window_momentum_pct']:+.3f}% "
            f"({momentum.get('window_direction')})"
        )
    if VOLUME_CONFIDENCE:
        log(f"  Volume ratio: {momentum['volume_ratio']:.2f}x avg")

    log("\n🧠 Analyzing...")
    quant_model_info = _resolve_quant_signal_model(ASSET, WINDOW)
    market_end_ts = int(end_time.timestamp()) if end_time else None
    sampling = _feature_sampling_config(WINDOW)
    feature_buffer = _get_or_create_feature_buffer(
        best.get("slug"),
        market_end_ts,
        sequence_steps=sampling["sequence_steps"],
        step_seconds=sampling["step_seconds"],
        target_asset_id=feature_token_id,
        strict_asset_id=True,
    )
    feature_stream = _ensure_market_event_stream(
        slug=best.get("slug"),
        feature_token_id=feature_token_id,
        feature_buffer=feature_buffer,
    )
    if feature_stream is not None:
        feature_stream.wait_until_ready(timeout=1.0)
    feature_event_ingested = False
    live_buffer_prediction = None
    live_buffer_error = None
    feature_window = None
    if feature_buffer is not None and quant_model_info["enabled"]:
        feature_window = feature_buffer.build_feature_window(as_of_ts=int(cycle_started_at.timestamp()))
        feature_event_ingested = feature_window is not None
        if feature_event_ingested:
            log_model_debug(feature_window, quant_model_info)
            try:
                live_buffer_prediction = quant_model_info["model"].predict_from_live_buffer(
                    feature_buffer,
                    as_of_ts=int(cycle_started_at.timestamp()),
                )
            except Exception as exc:
                live_buffer_error = str(exc)
        else:
            stream_snapshot = feature_stream.snapshot() if feature_stream is not None else None
            if stream_snapshot and stream_snapshot.get("last_error"):
                live_buffer_error = f"feature-stream-error:{stream_snapshot['last_error']}"
            else:
                live_buffer_error = "feature-window-not-ready"

    cycle["model"] = {
        "enabled": quant_model_info["enabled"],
        "source": quant_model_info["source"],
        "checkpoint": quant_model_info["checkpoint"],
        "loaded": quant_model_info["loaded"],
        "error": quant_model_info["error"],
        "feature_buffer_slug": best.get("slug"),
        "feature_token_id": feature_token_id,
        "feature_event_ingested": feature_event_ingested,
        "feature_stream": feature_stream.snapshot() if feature_stream is not None else None,
        "live_buffer_prediction": live_buffer_prediction,
        "live_buffer_error": live_buffer_error,
    }
    stream_snapshot = cycle["model"]["feature_stream"]
    if quant_model_info["source"] == "checkpoint" and quant_model_info["loaded"]:
        log(f"  Quant model: loaded checkpoint {quant_model_info['checkpoint']}")
    elif quant_model_info["checkpoint"] and quant_model_info["error"]:
        log(
            "  Quant model: checkpoint load failed, using heuristic-only mode "
            f"({quant_model_info['error']})"
        )
    elif not quant_model_info["enabled"]:
        log("  Quant model: no checkpoint for this market, using heuristic-only mode")
    else:
        log("  Quant model: enabled")
    if stream_snapshot is not None:
        log(
            "  Feature stream: "
            f"connected={stream_snapshot.get('connected')} "
            f"messages={stream_snapshot.get('messages_received')} "
            f"events={stream_snapshot.get('events_received')}"
        )
        if stream_snapshot.get("last_message_at"):
            log(f"  Feature stream last message: {stream_snapshot.get('last_message_at')}")
        if stream_snapshot.get("last_error"):
            log(f"  Feature stream error: {stream_snapshot.get('last_error')}")
    else:
        log("  Feature stream: unavailable")
    log(
        "  Feature buffer: "
        f"token={feature_token_id} "
        f"window_ready={feature_event_ingested}"
    )
    if live_buffer_prediction is not None:
        log(
            "  Live buffer prediction: "
            f"{live_buffer_prediction['signal_direction']} "
            f"(score {live_buffer_prediction['score']:+.4f}, "
            f"p_up {live_buffer_prediction['probability_up']:.4f})"
        )
    elif live_buffer_error:
        log(f"  Live buffer prediction unavailable: {live_buffer_error}")
    consensus = evaluate_consensus_signal(
        model=quant_model_info["model"],
        asset=ASSET,
        live_buffer=feature_buffer,
        live_buffer_as_of_ts=int(cycle_started_at.timestamp()),
        model_prediction=live_buffer_prediction,
        momentum=momentum,
        market_yes_price=market_yes_price,
        entry_threshold=ENTRY_THRESHOLD,
        min_momentum_pct=MIN_MOMENTUM_PCT,
        volume_confidence=VOLUME_CONFIDENCE,
        fee_rate=fee_rate,
        has_yes_token=bool(yes_token_id),
        has_no_token=bool(no_token_id),
    )

    heuristic_signal = consensus["heuristic"]
    model_signal = consensus["model"]
    momentum_pct = abs(momentum["momentum_pct"])
    direction = heuristic_signal.get("signal_direction") or momentum["direction"]
    side = heuristic_signal.get("proposed_side")
    divergence = heuristic_signal.get("divergence")
    trade_rationale = heuristic_signal.get("trade_rationale")
    token_id = yes_token_id if side == "yes" else no_token_id if side == "no" else None
    vol_note = ""
    if heuristic_signal.get("volume_note"):
        vol_note = f" ({heuristic_signal['volume_note']})"

    cycle["decision"] = {
        "consensus_approved": consensus["approved"],
        "consensus_reason": consensus["reason"],
        "signal_direction": direction,
        "proposed_side": side,
        "divergence": divergence,
        "trade_rationale": trade_rationale,
        "heuristic": heuristic_signal,
        "model": model_signal,
        "model_source": cycle["model"],
    }

    log(
        f"  Heuristic: {heuristic_signal.get('signal_direction')} -> "
        f"{heuristic_signal.get('proposed_side')} ({heuristic_signal.get('reason')})"
    )
    log(
        f"  Model:     {model_signal.get('signal_direction')} -> "
        f"{model_signal.get('proposed_side')} "
        f"(score {model_signal.get('prediction') if model_signal.get('prediction') is not None else 'n/a'})"
    )

    if heuristic_signal.get("fee_adjusted_breakeven") is not None:
        log(
            "  Breakeven:        "
            f"{heuristic_signal['fee_adjusted_breakeven']:.1%} win rate "
            f"(fee-adjusted, min divergence {heuristic_signal['min_divergence']:.3f})"
        )

    if not consensus["approved"]:
        reason = consensus["reason"]
        if reason == "momentum-too-weak":
            log(f"  ⏸️  Momentum {momentum_pct:.3f}% < minimum {MIN_MOMENTUM_PCT}% — skip")
            if not quiet:
                print(f"📊 Summary: No trade❌ (momentum too weak: {momentum_pct:.3f}%)")
            return finalize("ok", reason=reason)
        if reason == "low-volume":
            log(f"  ⏸️  Low volume ({momentum['volume_ratio']:.2f}x avg) — weak signal, skip")
            if not quiet:
                print("📊 Summary: No trade❌ (low volume)")
            return finalize("ok", reason=reason)
        if reason == "priced-in":
            log(f"  ⏸️  Market already priced in: divergence {divergence:.3f} ≤ 0 — skip")
            if not quiet:
                print("📊 Summary: No trade❌ (market already priced in)")
            return finalize("ok", reason=reason)
        if reason == "fees-eat-edge":
            log(
                f"  ⏸️  Divergence {divergence:.3f} < fee-adjusted minimum "
                f"{heuristic_signal['min_divergence']:.3f} — skip"
            )
            if not quiet:
                print("📊 Summary: No trade❌ (fees eat the edge)")
            return finalize("ok", reason=reason)
        if reason and reason.startswith("missing-token-id-"):
            log(f"  ❌ Missing CLOB token ID for {reason.split('-')[-1].upper()} outcome", force=True)
            return finalize("error", reason="missing-token-id", error=reason)
        if reason == "model-neutral":
            log(
                "  ⏸️  Heuristic qualified but model stayed neutral "
                f"(score {model_signal['prediction']:+.4f}, threshold {model_signal['threshold']:.4f})"
            )
            if not quiet:
                print("📊 Summary: No trade❌ (model neutral)")
            return finalize("ok", reason=reason)
        if reason == "missing-live-buffer":
            log("  ⏸️  Live feature buffer is not ready yet")
            if not quiet:
                print("📊 Summary: No trade❌ (feature buffer not ready)")
            return finalize("ok", reason=reason)
        if reason and reason.startswith("live-buffer-prediction-failed:"):
            log(f"  ⏸️  Live buffer prediction failed: {reason.split(':', 1)[1]}")
            if not quiet:
                print("📊 Summary: No trade❌ (feature buffer prediction failed)")
            return finalize("ok", reason=reason)
        if reason == "model-disagreement":
            log(
                "  ⏸️  Heuristic and model disagree "
                f"({heuristic_signal.get('proposed_side')} vs {model_signal.get('proposed_side')})"
            )
            if not quiet:
                print("📊 Summary: No trade❌ (model disagreement)")
            return finalize("ok", reason=reason)
        log(f"  ⏸️  Signal rejected: {reason}")
        return finalize("ok", reason=reason)

    if latest_open_trade is not None:
        open_trade_slug = latest_open_trade["market_slug"]
        open_trade_direction = (latest_open_trade["direction"] or "").lower()
        cycle["decision"]["open_trade_slug"] = open_trade_slug
        cycle["decision"]["open_trade_direction"] = open_trade_direction

        if open_trade_slug == best.get("slug") and open_trade_direction != side:
            if live_mode:
                log("  ⏸️  Open live trade exists with opposite side; live exit path is not implemented yet")
                cycle["trade"] = {
                    "mode": "live",
                    "action": "hold",
                    "reason": "open-live-trade-opposite-signal",
                }
                return finalize("ok", action="hold", reason="open-live-trade-opposite-signal")

            closed_trade = close_paper_trade(
                latest_open_trade,
                best,
                closed_at=datetime.now(timezone.utc),
                reason="signal-exit",
            )
            cycle["closed_trades"].append(closed_trade)
            cycle["portfolio"] = get_paper_portfolio()
            cycle["trade"] = {
                "mode": "paper",
                "action": "sell",
                "reason": "signal-exit",
                "closed_trade": closed_trade,
            }
            cycle["last_open_trade"] = None
            log(f"  ✅ Closed paper trade on opposite signal, PnL ${closed_trade['pnl']:+.2f}", force=True)
            return finalize("ok", action="sell", reason="signal-exit")

        log("  ⏸️  Open trade already exists; holding current position")
        cycle["trade"] = {
            "mode": execution_mode,
            "action": "hold",
            "reason": "open-trade-exists",
        }
        return finalize("ok", action="hold", reason="open-trade-exists")

    address = get_wallet_address() if live_mode and smart_sizing else None
    if not live_mode:
        ensure_paper_db()
        cycle["portfolio"] = get_paper_portfolio()
    position_size = calculate_position_size(MAX_POSITION_USD, smart_sizing, live_mode=live_mode, address=address)
    price = market_yes_price if side == "yes" else (1 - market_yes_price)

    if price > 0:
        min_cost = MIN_SHARES_PER_ORDER * price
        if min_cost > position_size:
            log(f"  ⚠️  Position ${position_size:.2f} too small for {MIN_SHARES_PER_ORDER} shares at ${price:.2f}")
            return finalize("ok", reason="position-too-small")
    if position_size <= 0:
        if live_mode:
            log("  ⏸️  No live portfolio value available for a new trade")
            return finalize("ok", reason="no-live-balance")
        log("  ⏸️  No paper cash available for a new trade")
        return finalize("ok", reason="no-paper-cash")

    _signal_to_display = side.upper() + ("⬆️" if side == "yes" else "⬇️")
    log(f"  ✅ Signal: {_signal_to_display} — {trade_rationale}{vol_note}", force=True)
    log(f"  Divergence: {divergence:.3f}", force=True)
    est_shares = position_size / price if price > 0 else 0

    recorded = False
    result = None
    if not live_mode:
        recorded, error = record_trade(
            best,
            side,
            price,
            est_shares,
            momentum["momentum_pct"],
            momentum["volume_ratio"],
            execution_mode="paper",
        )
        cycle["trade"] = {
            "mode": "paper",
            "side": side,
            "position_size_usd": round(position_size, 6),
            "estimated_shares": round(est_shares, 6),
            "price": round(price, 6),
            "recorded": recorded,
            "error": error,
        }
        if recorded:
            log(f"  📝 Paper trade recorded: {side.upper()} ${position_size:.2f} (~{est_shares:.1f} shares)", force=True)
            cycle["portfolio"] = get_paper_portfolio()
            latest_open_trade = get_latest_open_trade()
            trade_market = _resolve_open_trade_market(latest_open_trade, default_market=best)
            cycle["last_open_trade"] = _mark_to_market_snapshot(latest_open_trade, trade_market)
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
            cycle["action"] = "paper-recorded"
        else:
            log(f"  ❌ Paper trade not recorded: {error}", force=True)
            cycle["action"] = "paper-skipped"
    else:
        client = get_clob_client()
        log(f"  Executing {side.upper()} trade for ${position_size:.2f} via CLOB...", force=True)
        result = execute_live_trade(client, token_id, position_size)
        live_recorded = False
        live_record_error = None
        cycle["trade"] = {
            "mode": "live",
            "side": side,
            "position_size_usd": round(position_size, 6),
            "estimated_shares": round(est_shares, 6),
            "price": round(price, 6),
            "result": result,
        }
        if result and not (isinstance(result, dict) and result.get("error")):
            order_id = result.get("orderID") or result.get("id") if isinstance(result, dict) else None
            live_recorded, live_record_error = record_trade(
                best,
                side,
                price,
                est_shares,
                momentum["momentum_pct"],
                momentum["volume_ratio"],
                execution_mode="live",
            )
            cycle["trade"]["recorded"] = live_recorded
            cycle["trade"]["record_error"] = live_record_error
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
            cycle["action"] = "live-traded"
        else:
            error = result.get("error", "Unknown error") if isinstance(result, dict) else "No response"
            log(f"  ❌ Trade failed: {error}", force=True)
            return finalize("error", action="live-failed", reason="live-trade-failed", error=error)

    if not live_mode:
        total_trades = 1 if recorded else 0
        action = "PAPER RECORDED" if total_trades else "PAPER SKIPPED"
    else:
        total_trades = 1 if result and not (isinstance(result, dict) and result.get("error")) else 0
        action = "TRADED" if total_trades else "FAILED"

    show_summary = not quiet or total_trades > 0
    if show_summary:
        print("\n📊 Summary:")
        print(f"  Sprint: {best['question'][:50]}")
        print(f"  Slug:   {best.get('slug', '-')}")
        print(f"  Signal: {direction} {momentum_pct:.3f}% | YES ${market_yes_price:.3f}")
        if cycle.get("last_open_trade"):
            print(f"  Last buy PnL: {cycle['last_open_trade']['unrealized_pnl_ratio']:+.2%}")
        print(f"  Action: {action}")

    return finalize("ok", action=cycle.get("action") or action.lower().replace(" ", "-"))


def run_loop(
    session_started_at,
    live_mode=False,
    positions_only=False,
    paper_positions_only=False,
    show_config=False,
    smart_sizing=False,
    quiet=False,
    simple_display=False,
    loop_interval_seconds=DEFAULT_LOOP_INTERVAL_SECONDS,
    debug_model=False,
):
    """Run the strategy continuously and keep JSON session artifacts fresh."""

    _write_summary(
        session_started_at,
        build_trading_summary(
            session_started_at,
            loop_enabled=True,
            loop_interval_seconds=loop_interval_seconds,
        ),
    )

    while True:
        try:
            if simple_display:
                print("\033[2J\033[H", end="")
            cycle = run_fast_market_strategy(
                session_started_at_ts=session_started_at,
                live_mode=live_mode,
                positions_only=positions_only,
                paper_positions_only=paper_positions_only,
                show_config=show_config,
                smart_sizing=smart_sizing,
                quiet=quiet,
                simple_display=simple_display,
                debug_model=debug_model,
            )
        except KeyboardInterrupt:
            _write_summary(
                session_started_at,
                build_trading_summary(
                    session_started_at,
                    loop_enabled=False,
                    loop_interval_seconds=loop_interval_seconds,
                ),
            )
            raise
        except Exception as exc:
            cycle = {
                "cycle_started_at": datetime.now(timezone.utc).isoformat(),
                "cycle_completed_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "action": "exception",
                "reason": "uncaught-exception",
                "error": str(exc),
            }

        _append_history_event(session_started_at, cycle)
        _write_summary(
            session_started_at,
            build_trading_summary(
                session_started_at,
                loop_enabled=True,
                loop_interval_seconds=loop_interval_seconds,
                last_error=cycle.get("error"),
            ),
        )

        time.sleep(max(1, loop_interval_seconds))


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Polymarket Fast Market Trader")
    parser.add_argument("--live", action="store_true", help="Execute real trades")
    parser.add_argument("--positions", action="store_true", help="Show current live fast market positions")
    parser.add_argument("--paper-positions", action="store_true", help="Show current paper positions")
    parser.add_argument("--config", action="store_true", help="Show current config")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="Update config (e.g., --set entry_threshold=0.08)")
    parser.add_argument("--smart-sizing", action="store_true", help="Use portfolio-based position sizing")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only output on trades/errors (ideal for high-frequency runs)")
    parser.add_argument("--loop", action="store_true",
                        help="Run continuously and keep session JSON files updated")
    parser.add_argument("--simple-display", action="store_true",
                        help="Clear the terminal on each loop iteration for a simple dashboard-style display")
    parser.add_argument("--loop-interval-seconds", type=int, default=DEFAULT_LOOP_INTERVAL_SECONDS,
                        help="Sleep interval between long-running loop cycles")
    parser.add_argument("--debug-model", action="store_true",
                        help="Print live feature-window and normalized model-input diagnostics")
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

    session_started_at = int(datetime.now(timezone.utc).timestamp())

    if args.loop:
        try:
            run_loop(
                session_started_at=session_started_at,
                live_mode=args.live,
                positions_only=args.positions,
                paper_positions_only=args.paper_positions,
                show_config=args.config,
                smart_sizing=args.smart_sizing,
                quiet=args.quiet,
                simple_display=args.simple_display,
                loop_interval_seconds=args.loop_interval_seconds,
                debug_model=args.debug_model,
            )
        except KeyboardInterrupt:
            print("\nStopped long-running fast loop.")
        return

    cycle = run_fast_market_strategy(
        session_started_at_ts=session_started_at,
        live_mode=args.live,
        positions_only=args.positions,
        paper_positions_only=args.paper_positions,
        show_config=args.config,
        smart_sizing=args.smart_sizing,
        quiet=args.quiet,
        simple_display=args.simple_display,
        debug_model=args.debug_model,
    )
    _append_history_event(session_started_at, cycle)
    _write_summary(
        session_started_at,
        build_trading_summary(
            session_started_at,
            loop_enabled=False,
            last_error=cycle.get("error"),
        ),
    )


if __name__ == "__main__":
    main()

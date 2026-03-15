#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${POLYMARKET_PRIVATE_KEY:?POLYMARKET_PRIVATE_KEY is required}"
WALLET_TYPE="${WALLET_TYPE:-0}"
FUNDER_ADDRESS="${FUNDER_ADDRESS:-}"
PLACE_ORDER="${PLACE_ORDER:-0}"
WINDOW="${WINDOW:-15m}"
OUTCOME="${OUTCOME:-yes}"
ORDER_USD="${ORDER_USD:-1.0}"
MARKET_SLUG="${MARKET_SLUG:-}"
TOKEN_ID="${TOKEN_ID:-}"

echo "Polymarket order check"
echo "  repo:          $ROOT_DIR"
echo "  wallet_type:   $WALLET_TYPE"
if [[ -n "$FUNDER_ADDRESS" ]]; then
  echo "  funder:        $FUNDER_ADDRESS"
fi
echo "  place_order:   $PLACE_ORDER"
if [[ -n "$MARKET_SLUG" ]]; then
  echo "  market_slug:   $MARKET_SLUG"
fi
if [[ -n "$TOKEN_ID" ]]; then
  echo "  token_id:      $TOKEN_ID"
fi

uv run python - <<'PY'
import json
import os
import sys

from scripts.polymarket_fast_market import (
    execute_live_trade,
    fetch_market_by_slug,
    get_clob_client,
    get_token_midpoint,
    get_wallet_address,
)


def fail(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}")
    raise SystemExit(code)


place_order = os.environ.get("PLACE_ORDER", "0") == "1"
market_slug = os.environ.get("MARKET_SLUG", "").strip()
window = os.environ.get("WINDOW", "15m").strip()
outcome = os.environ.get("OUTCOME", "yes").strip().lower()
token_id = os.environ.get("TOKEN_ID", "").strip()
order_usd = float(os.environ.get("ORDER_USD", "1.0"))
wallet_type = os.environ.get("WALLET_TYPE", "0")
funder_address = os.environ.get("FUNDER_ADDRESS", "").strip()

if outcome not in {"yes", "no"}:
    fail(f"OUTCOME must be yes or no, got {outcome!r}")

print("\n[1/3] Creating authenticated CLOB client")
client = get_clob_client()
wallet_address = get_wallet_address()
print(f"  wallet_address: {wallet_address}")
print(f"  wallet_type:    {wallet_type}")
if funder_address:
    print(f"  funder:         {funder_address}")
print("  auth:           OK")

resolved_market = None
if not token_id and market_slug:
    print("\n[2/3] Resolving market and token")
    resolved_market = fetch_market_by_slug(market_slug, window)
    if not resolved_market:
        fail(f"Could not resolve market slug {market_slug!r} for window {window!r}")
    token_id = resolved_market["yes_token_id"] if outcome == "yes" else resolved_market["no_token_id"]
    if not token_id:
        fail(f"Missing {outcome.upper()} token id on resolved market")
    print(f"  question:       {resolved_market['question']}")
    print(f"  selected_side:  {outcome.upper()}")
    print(f"  token_id:       {token_id}")
    midpoint = get_token_midpoint(token_id)
    if midpoint is not None:
        print(f"  midpoint:       {midpoint:.4f}")

if not place_order:
    print("\n[3/3] Order placement skipped")
    print("  result:         auth and token resolution succeeded")
    print("  next step:      run with PLACE_ORDER=1 to post a live FOK market order")
    raise SystemExit(0)

if not token_id:
    fail("PLACE_ORDER=1 requires TOKEN_ID or MARKET_SLUG")

print("\n[3/3] Posting live FOK market order")
print(f"  token_id:       {token_id}")
print(f"  amount_usd:     {order_usd:.4f}")
result = execute_live_trade(client, token_id, order_usd)
print("  raw_result:")
print(json.dumps(result, indent=2, default=str))

if isinstance(result, dict) and result.get("error"):
    fail(f"Live order failed: {result['error']}", code=2)

print("  result:         live order request accepted")
PY

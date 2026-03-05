---
name: polymarket-fast-loop
description: Trade Polymarket BTC 5-minute and 15-minute fast markets using CEX price momentum signals via the official Polymarket CLOB client. Default signal is Binance BTC/USDT klines. Use when user wants to trade sprint/fast markets, automate short-term crypto trading, or use CEX momentum as a Polymarket signal.
metadata:
  {
    "openclaw":
      {
        "requires": { "bins": ["uv"], "env": ["POLYMARKET_PRIVATE_KEY"], "config": ["browser.enabled"] },
        "primaryEnv": "POLYMARKET_PRIVATE_KEY",
      },
  }
---

# Polymarket FastLoop Trader

Trade Polymarket's 5-minute BTC fast markets using real-time price momentum from Binance.

> **Polymarket only.** All trades execute on Polymarket via the official CLOB client with real USDC. Use `--live` for real trades, dry-run is the default.

**How it works:** Every cycle, the script finds the current live BTC fast market, checks BTC price momentum on Binance, and trades if momentum diverges from market odds.

**This is a template.** The default signal (Binance momentum) gets you started. Your agent's reasoning is the edge — layer on sentiment analysis, multi-exchange spreads, news feeds, or custom signals to improve it.

> ⚠️ Fast markets carry Polymarket's 10% fee (`is_paid: true`). Factor this into your edge calculations.

## When to Use This Skill

Use this skill when the user wants to:
- Trade BTC sprint/fast markets (5-minute or 15-minute)
- Automate short-term crypto prediction trading
- Use CEX price momentum as a Polymarket signal
- Monitor sprint market positions

## Setup Flow

When user asks to install or configure this skill:

1. **Install dependencies**
  Make sure uv is installed, run uv --version to verify. If not, following the instructions:
  #### **Direct Installation**
    Choose the command for your operating system:

    **macOS / Linux:**
    ```bash
    curl -LsSf [https://astral.sh/uv/install.sh](https://astral.sh/uv/install.sh) | sh
    ```

    **Windows (PowerShell):**
    ```powershell
    powershell -ExecutionPolicy ByPass -c "irm [https://astral.sh/uv/install.ps1](https://astral.sh/uv/install.ps1) | iex"
    ```

    [!TIP]
    After installation, restart your terminal to ensure uv is in your PATH. Run uv --version to verify.

  #### **Alternative (Via Pip)**
  If you already have Python installed and prefer pip:
  ```bash
  pip install uv
  ```

  Once uv is installed, use the following commands to set up the environment:

  ```bash
  uv sync
  ```

2. **Set your Polygon wallet private key**
   - Must be a funded Polygon wallet with USDC.e
   - Store in environment as `POLYMARKET_PRIVATE_KEY`
   - Ensure token approvals are set for Polymarket exchange contracts

3. **Ask about settings** (or confirm defaults)
   - Asset: BTC, ETH, or SOL (default BTC)
   - Entry threshold: Min divergence to trade (default 5¢)
   - Max position: Amount per trade (default $5.00)
   - Window: 5m or 15m (default 5m)

4. **Set up cron or loop** (user drives scheduling — see "How to Run on a Loop")

Ensure the environment variable POLYMARKET_PRIVATE_KEY is exported before running.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set your private key
export POLYMARKET_PRIVATE_KEY="0xyour-private-key-here"

# Dry run — see what would happen
# Dry run
uv run python scripts/fastloop_trader.py

# Go live
uv run python scripts/fastloop_trader.py --live

# Live + smart sizing (5% of portfolio per trade)
uv run python scripts/fastloop_trader.py --live --smart-sizing
```

## How to Run on a Loop

The script runs **one cycle** — your bot drives the loop. Set up a cron job or heartbeat:

**Every 5 minutes (one per fast market window):**
```
*/5 * * * * cd /path/to/skill && uv run python scripts/fastloop_trader.py --live 
```

**Every 1 minute (more aggressive, catches mid-window opportunities):**
```
* * * * * cd /path/to/skill && uv run python scripts/fastloop_trader.py --live 
```

**Via OpenClaw heartbeat:** Add to your HEARTBEAT.md:
```
Run: cd /path/to/fast market && uv run python scripts/fastloop_trader.py --live 
```

## Configuration

Configure via `config.json`, environment variables, or `--set`:

```bash
# Change entry threshold
uv run python scripts/fastloop_trader.py --set entry_threshold=0.08

# Trade ETH instead of BTC
uv run python scripts/fastloop_trader.py --set asset=ETH

# Multiple settings
uv run python scripts/fastloop_trader.py --set min_momentum_pct=0.3 --set max_position=10
```

### Settings

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| `entry_threshold` | 0.05 | `PM_FASTLOOP_ENTRY` | Min price divergence from 50¢ to trigger |
| `min_momentum_pct` | 0.5 | `PM_FASTLOOP_MOMENTUM` | Min BTC % move to trigger |
| `max_position` | 5.0 | `PM_FASTLOOP_MAX_POSITION` | Max $ per trade |
| `signal_source` | binance | `PM_FASTLOOP_SIGNAL` | Price feed (binance, coingecko) |
| `lookback_minutes` | 5 | `PM_FASTLOOP_LOOKBACK` | Minutes of price history |
| `min_time_remaining` | 60 | `PM_FASTLOOP_MIN_TIME` | Skip fast markets with less time left (seconds) |
| `asset` | BTC | `PM_FASTLOOP_ASSET` | Asset to trade (BTC, ETH, SOL) |
| `window` | 5m | `PM_FASTLOOP_WINDOW` | Market window duration (5m or 15m) |
| `volume_confidence` | true | `PM_FASTLOOP_VOL_CONF` | Weight signal by Binance volume |

### Example config.json

```json
{
  "entry_threshold": 0.08,
  "min_momentum_pct": 0.3,
  "max_position": 10.0,
  "asset": "BTC",
  "window": "5m",
  "signal_source": "binance"
}
```

## CLI Options

```bash
uv run python scripts/fastloop_trader.py                    # Dry run
uv run python scripts/fastloop_trader.py --live             # Real trades
uv run python scripts/fastloop_trader.py --live      # Silent except trades/errors
uv run python scripts/fastloop_trader.py --smart-sizing     # Portfolio-based sizing
uv run python scripts/fastloop_trader.py --positions        # Show open fast market positions
uv run python scripts/fastloop_trader.py --config           # Show current config
uv run python scripts/fastloop_trader.py --set KEY=VALUE    # Update config
```

## Signal Logic

Default signal (Binance momentum):

1. Fetch last 5 one-minute candles from Binance (`BTCUSDT`)
2. Calculate momentum: `(price_now - price_5min_ago) / price_5min_ago`
3. Compare momentum direction to current Polymarket odds
4. Trade when:
   - Momentum ≥ `min_momentum_pct` (default 0.5%)
   - Price diverges from 50¢ by ≥ `entry_threshold` (default 5¢)
   - Volume ratio > 0.5x average (filters out thin moves)

**Example:** BTC up 0.8% in last 5 min, but fast market YES price is only $0.52. The 3¢ divergence from the expected ~$0.55 → buy YES.

### Customizing Your Signal

The default momentum signal is a starting point. To add your own edge:

- **Multi-exchange:** Compare prices across Binance, Kraken, Bitfinex — divergence between exchanges can predict CLOB direction
- **Sentiment:** Layer in Twitter/social signals — a viral tweet can move fast markets
- **Technical indicators:** RSI, VWAP, order flow analysis
- **News:** Breaking news correlation — use your agent's reasoning to interpret headlines

The skill handles all the Polymarket CLOB plumbing (discovery, order signing, trade execution). Your agent provides the alpha.

## Example Output

```
⚡ Polymarket FastLoop Trader
==================================================

  [DRY RUN] No trades will be executed. Use --live to enable trading.

⚙️  Configuration:
  Asset:            BTC
  Entry threshold:  0.05 (min divergence from 50¢)
  Min momentum:     0.5% (min price move)
  Max position:     $5.00
  Signal source:    binance
  Lookback:         5 minutes
  Min time left:    60s
  Volume weighting: ✓

🔍 Discovering BTC fast markets...
  Found 3 active fast markets

🎯 Selected: Bitcoin Up or Down - February 15, 5:30AM-5:35AM ET
  Expires in: 185s
  Current YES price: $0.480

📈 Fetching BTC price signal (binance)...
  Price: $97,234.50 (was $96,812.30)
  Momentum: +0.436%
  Direction: up
  Volume ratio: 1.45x avg

🧠 Analyzing...
  ⏸️  Momentum 0.436% < minimum 0.500% — skip

📊 Summary: No trade (momentum too weak: 0.436%)
```

## Source Tagging

All trades are tagged with `source: "clob:fastloop"`. This helps track fast market P&L separately.

## Token Approvals

Before your first trade, your wallet needs to approve Polymarket's exchange contracts to spend your USDC.e and conditional tokens on Polygon. See the [Polymarket CLOB documentation](https://docs.polymarket.com/developers/CLOB/quickstart) for details.

## Troubleshooting

**"No active fast markets found"**
- Fast markets may not be running (off-hours, weekends)
- Check Polymarket directly for active BTC fast markets

**"No fast markets with >60s remaining"**
- Current window is about to expire, next one isn't live yet
- Reduce `min_time_remaining` if you want to trade closer to expiry

**"Failed to fetch price data"**
- Binance API may be down or rate limited
- Try `--set signal_source=coingecko` as fallback

**"Trade failed" / order errors**
- Check that your wallet has sufficient USDC.e balance on Polygon
- Ensure token approvals are set for Polymarket exchange contracts
- Fast market may have thin orderbook — try smaller position size

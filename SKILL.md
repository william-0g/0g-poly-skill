---
name: polymarket-fast-loop
description: Trade Polymarket BTC 5-minute and 15-minute fast markets using CEX price momentum signals, with support for dry-run, paper journaling, and live execution through the official CLOB client. Default signal is Binance BTC/USDT klines.
metadata:
  {
    "openclaw":
      {
        "requires": { "bins": ["uv"], "env": ["POLYMARKET_PRIVATE_KEY"] },
        "primaryEnv": "POLYMARKET_PRIVATE_KEY",
      },
  }
---

# Polymarket FastLoop Trader

Evaluate or trade Polymarket fast markets with a simple momentum signal.

> **Modes:** dry-run for inspection, `--record-paper` for SQLite paper journaling, and `--live` for real trades on Polymarket.

## When to Use This Skill

Use this skill when the user wants to:
- Trade or paper trade BTC/ETH/SOL fast markets
- Monitor short-term crypto momentum against Polymarket odds
- Keep a local SQLite journal of paper entries
- Run the strategy on a cron/heartbeat loop

## Setup Flow

1. Ensure `uv` is installed, then run:
   ```bash
   uv sync
   ```
2. Confirm or adjust settings:
   - `asset`: BTC, ETH, or SOL
   - `window`: `5m` or `15m`
   - `entry_threshold`: minimum divergence to act
   - `max_position`: paper size cap per entry
3. Choose the mode:
   - Dry run: inspect the current opportunity only
   - `--record-paper`: write a paper trade to SQLite when a signal qualifies
   - `--live`: place a real order through the Polymarket CLOB client
4. If the user wants automation, schedule the command with cron or heartbeat.

Live mode requires `POLYMARKET_PRIVATE_KEY`.

## OpenClaw Invocation Rules

When OpenClaw calls this skill from a prompt, map the requested mode directly to the CLI command:

- If the prompt says `dry-run`, run:
  ```bash
  uv run python scripts/fastloop_trader.py
  ```
- If the prompt says `paper trading`, `paper-trading`, or `paper`, run:
  ```bash
  uv run python scripts/fastloop_trader.py --record-paper
  ```
- If the prompt says `live`, run:
  ```bash
  uv run python scripts/fastloop_trader.py --live
  ```
- If the prompt does not specify a mode, default to `dry-run`.

Apply any requested configuration before running the command:

- Asset: `--set asset=BTC|ETH|SOL`
- Window: `--set window=5m|15m`
- Max position: `--set max_position=<amount>`
- Entry threshold: `--set entry_threshold=<amount>`
- Lookback: `--set lookback_minutes=<minutes>`

For timed or recurring OpenClaw calls, the prompt should explicitly include the mode. Good examples:

- `Run polymarket-fast-loop in dry-run mode for BTC 15m`
- `Run polymarket-fast-loop in paper trading mode for BTC 15m with max position 5`
- `Run polymarket-fast-loop in live mode for BTC 15m with max position 5`

Before running in `live` mode:

- Confirm `POLYMARKET_PRIVATE_KEY` is available
- Prefer a dry-run first if the user did not explicitly ask to skip it
- Do not silently downgrade `live` to paper mode; surface the missing prerequisite instead

## Recommended Prompt Templates

Use prompts in this shape for OpenClaw heartbeat, cron wrappers, or recurring automations.

Dry-run template:

```text
Run polymarket-fast-loop in dry-run mode.
Set asset to BTC.
Set window to 15m.
Set max position to 5.
Report the selected market, signal direction, divergence, and whether a trade would be taken.
```

Paper trading template:

```text
Run polymarket-fast-loop in paper trading mode.
Set asset to BTC.
Set window to 15m.
Set max position to 5.
If a qualifying signal exists, record it to the paper trading database.
Report whether a paper trade was recorded.
```

Live template:

```text
Run polymarket-fast-loop in live mode.
Set asset to BTC.
Set window to 15m.
Set max position to 5.
If a qualifying signal exists, execute the trade on Polymarket.
Report the selected market, signal details, and final order result.
```

Conservative live template:

```text
First run polymarket-fast-loop in dry-run mode for BTC 15m with max position 5.
If the signal qualifies and the market is tradeable, run polymarket-fast-loop in live mode with the same settings.
Report both the dry-run decision and the live execution result.
```

For recurring runs, keep the prompt explicit and stable. Avoid vague instructions like `run the trader normally`, because the skill defaults to `dry-run` when the mode is omitted.

## Quick Start

```bash
# Install dependencies
uv sync

# Dry run
uv run python scripts/fastloop_trader.py

# Record a qualifying paper trade
uv run python scripts/fastloop_trader.py --record-paper

# Execute a live trade
uv run python scripts/fastloop_trader.py --live

# Record using smart sizing (5% of remaining paper cash, capped by max_position)
uv run python scripts/fastloop_trader.py --record-paper --smart-sizing
```

## Looping

```bash
*/5 * * * * cd /path/to/skill && uv run python scripts/fastloop_trader.py --live
```

Or for signal-only monitoring:

```bash
* * * * * cd /path/to/skill && uv run python scripts/fastloop_trader.py
```

## Configuration

The script reads `config.json` from the skill root. You can update settings with `--set`:

```bash
uv run python scripts/fastloop_trader.py --set asset=ETH
uv run python scripts/fastloop_trader.py --set window=5m
uv run python scripts/fastloop_trader.py --set max_position=10
```

### Settings

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| `entry_threshold` | `0.05` | `PM_FASTLOOP_ENTRY` | Minimum divergence from 50c |
| `min_momentum_pct` | `0.5` | `PM_FASTLOOP_MOMENTUM` | Minimum move over lookback window |
| `max_position` | `5.0` | `PM_FASTLOOP_MAX_POSITION` | Maximum dollars per trade |
| `signal_source` | `binance` | `PM_FASTLOOP_SIGNAL` | Price feed source |
| `lookback_minutes` | `5` | `PM_FASTLOOP_LOOKBACK` | Lookback window in minutes |
| `min_time_remaining` | `60` | `PM_FASTLOOP_MIN_TIME` | Skip markets too close to expiry |
| `asset` | `BTC` | `PM_FASTLOOP_ASSET` | Asset to trade |
| `window` | `5m` | `PM_FASTLOOP_WINDOW` | Fast-market duration |
| `volume_confidence` | `true` | `PM_FASTLOOP_VOL_CONF` | Filter out thin volume moves |
| `paper_trade_db` | `fastloop_paper.db` | `PM_FASTLOOP_DB` | SQLite path for paper trades |

## CLI Options

```bash
uv run python scripts/fastloop_trader.py                 # Dry run
uv run python scripts/fastloop_trader.py --record-paper  # Record paper trade
uv run python scripts/fastloop_trader.py --live          # Execute real trade
uv run python scripts/fastloop_trader.py --positions     # Show live fast-market positions
uv run python scripts/fastloop_trader.py --paper-positions # Show paper positions
uv run python scripts/fastloop_trader.py --config        # Show resolved config path and DB path
uv run python scripts/fastloop_trader.py --set KEY=VALUE # Update config.json
```

## Signal Logic

Default signal:

1. Fetch recent 1-minute candles from Binance.
2. Measure momentum over the configured lookback.
3. Compare direction and fixed divergence thresholds against current Polymarket odds.
4. Reject low-volume, low-momentum, or fee-unfavorable entries.
5. On `--record-paper`, store the entry in SQLite instead of placing a live order.
6. On `--live`, place a real order through the Polymarket CLOB client.

This is still a template strategy. The current fair-value model is simple and should not be treated as production alpha.

## Database

The script can create a local SQLite file, `fastloop_paper.db`, with:
- `fastloop_trades`: recorded paper entries
- `portfolio`: a lightweight snapshot of remaining paper cash and open positions

## Troubleshooting

**"No active fast markets found"**
- Fast markets may not be running
- Gamma API results may have changed format

**"No tradeable fast_markets"**
- Current window is near expiry
- Market metadata may be missing token IDs

**"Failed to fetch price data"**
- Binance may be unavailable or rate-limiting
- `coingecko` is only a weak fallback because it has no candle history here

**"Paper trade not recorded"**
- The same market/direction was already recorded
- Remaining paper cash is too low for the configured size

**"Trade failed" / order errors**
- Check `POLYMARKET_PRIVATE_KEY`
- Confirm the wallet has funds and approvals set
- Fast markets may have thin books or have moved before the FOK order landed

# Report Payloads

Use this reference only when the user wants to define, adjust, or standardize the contents of the periodic `polymarket-fast-market` status/PnL push.

## Default Report Sections

The reporting payload should be compact and operationally useful. Default sections:

1. Execution status
2. Profit and loss summary
3. Open positions
4. Recent trade activity
5. Latest update timestamp

## Recommended Payload Shape

```text
polymarket-fast-market status

Execution:
- mode: paper or live
- loop status: running or stopped
- latest update: <timestamp>

PnL:
- paper_pnl
- live_pnl
- combined_trade_stats

Open positions:
- market slug
- direction
- entry price
- quantity

Recent activity:
- latest entries
- latest exits
- realized pnl for closed trades
```

## Formatting Guidance

- Prefer concise summaries over raw dumps.
- Keep the report readable in chat or push notifications.
- Use the same field names as `market_summary_<session_started_at>.json` when possible.
- If a source is missing a field, omit it rather than inventing a placeholder.

## Data Mapping

Map report sections from sources in this order:

- `polymarket_fast_market_paper.db`
- nearest `market_summary_<session_started_at>.json`
- nearest `market_history_<session_started_at>.json`

Recommended field mapping:

- execution status:
  - `runtime.loop_enabled`
  - `runtime.updated_at`
  - `session.mode`
- PnL:
  - `paper_pnl`
  - `live_pnl`
  - `combined_trade_stats`
- open positions:
  - `open_positions`
- recent activity:
  - `recent_trades`

## Minimal Push Template

```text
polymarket-fast-market update
- status: running
- updated_at: 2026-03-10T09:30:00Z
- paper realized pnl: 1.25
- live realized pnl: 0.00
- combined realized pnl: 1.25
- open positions: 1
- latest trade: btc-updown-15m-1773135000 YES pnl +0.42
```

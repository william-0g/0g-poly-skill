# Automation Reporting

Use this reference only when the user wants OpenClaw cron to push periodic execution or PnL updates for `polymarket-fast-market`.

## Reporting Data Source Priority

1. Prefer querying `polymarket_fast_market_paper.db` in the skill workspace.
2. If `polymarket_fast_market_paper.db` does not exist, fall back to:
   - `market_history_<session_started_at>.json`
   - `market_summary_<session_started_at>.json`
3. If multiple matching JSON files exist, choose the session whose timestamp is closest to the current time.

## Reporting Contents

The reporting task should summarize:

- current execution status
- `paper_pnl`
- `live_pnl`
- `combined_trade_stats`
- open positions
- recent entries and exits
- latest session or update timestamp

## Automation Rules

- use OpenClaw cron, not system cron
- keep unrelated existing automations untouched
- if an equivalent reporting task already exists, update it instead of creating a duplicate
- the reporting task is read-only and must not place trades

## Questions To Ask Before Creating The Automation

1. What reporting interval should be used?
2. Where should the push be sent?
3. Which workspace should be monitored?

## Direct Automation Prompt Template

```text
Create an OpenClaw cron task for polymarket-fast-market reporting.

Task:
- Report current execution status and profit/loss on a fixed interval.
- Prefer querying polymarket_fast_market_paper.db in the skill workspace.
- If polymarket_fast_market_paper.db does not exist, find market_history_<session_started_at>.json and market_summary_<session_started_at>.json in the workspace and use the session whose timestamp is closest to the current time.

Report contents:
- current execution status
- paper_pnl
- live_pnl
- combined_trade_stats
- open positions
- recent entries and exits
- latest session/update timestamp

Rules:
- use OpenClaw cron, not system cron
- keep unrelated existing automations untouched
- if an equivalent reporting task already exists, update it instead of creating a duplicate
- the reporting task is read-only and must not place trades

Inputs to confirm before creating:
- reporting interval
- destination/channel for the push
- workspace path for the running trader
```

## Filled Example

```text
Create an OpenClaw cron task for polymarket-fast-market reporting every 30 minutes.

Workspace:
/path/to/polymarket-fast-market

Task:
- Report current execution status and profit/loss for the running fast loop.
- Prefer querying polymarket_fast_market_paper.db in the workspace.
- If polymarket_fast_market_paper.db does not exist, use the nearest market_history_<session_started_at>.json and market_summary_<session_started_at>.json files instead.

Report contents:
- current execution status
- paper_pnl
- live_pnl
- combined_trade_stats
- open positions
- recent entries and exits
- latest session/update timestamp

Rules:
- use OpenClaw cron, not system cron
- update an equivalent existing reporting task instead of creating a duplicate
- this reporting task is read-only and must not place trades
```

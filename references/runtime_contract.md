# Runtime Contract

Use this reference only when the task depends on the session artifact format or on how the long-running loop resolves fast-market slugs.

## Session Model

- One Python process lifetime is one run session.
- Capture one Unix timestamp at process start and keep it fixed for the session.
- Use that timestamp in all session output filenames.
- Do not rotate to a new timestamp until the process restarts.

## Session Artifacts

- `market_history_<unix_time>.json`
  - append-friendly chronological records of key trade events
- `market_summary_<unix_time>.json`
  - compact portfolio, position, and PnL summary for the same session

Store these files in the skill workspace root unless the user explicitly requests a different output directory.

## Slug Resolution

- Resolve the market slug directly from asset, window, and the current bucket start timestamp.
- Use the current bucket start time, not market discovery heuristics, to choose the slug.
- Slug format:
  - BTC 5m: `btc-updown-5m-<unix_time>`
  - BTC 15m: `btc-updown-15m-<unix_time>`
  - ETH 5m: `eth-updown-5m-<unix_time>`
  - ETH 15m: `eth-updown-15m-<unix_time>`
  - SOL follows the same pattern with `sol`

## Loop Behavior

- Long-running loop cadence is 5 seconds by default.
- During each cycle, the process should settle expired markets and then evaluate the current slug for a new entry.
- When paper positions exist, record the latest open buy's mark-to-market PnL ratio in `market_history_<unix_time>.json`.

## Expected JSON Shape

Recommended top-level fields:

- `schema_version`
- `session`
- `runtime`

`market_history_<unix_time>.json` should additionally contain:

- `events`: append-only array of key trade events
- Event types:
  - `observation`
  - `entry`
  - `exit`
  - `error`

`market_summary_<unix_time>.json` should additionally contain:

- `paper_pnl`
- `live_pnl`
- `combined_trade_stats`
- `open_positions`
- `recent_trades`

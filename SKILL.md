---
name: polymarket-fast-market
description: Run or inspect the Polymarket fast-market trader for BTC, ETH, or SOL in paper or live mode, with optional reporting automation and local JSON/SQLite artifacts.
metadata:
  {
    "openclaw":
      {
        "requires": { "bins": ["uv"] },
        "primaryEnv": "POLYMARKET_PRIVATE_KEY",
      },
  }
---

# Polymarket Fast Market Trader

Use this skill to run, inspect, or automate the Polymarket fast-market trader in this workspace.

## When To Use

Use this skill when the user wants to:
- start the fast-market trader in paper or live mode
- change trader settings in `config.json`
- inspect paper trades, open positions, or current config
- review per-session JSON outputs or the local SQLite journal
- create periodic status or PnL reporting for a running trader

## Core Rules

- Default to paper trading unless the user explicitly asks for live trading.
- Paper and inspect flows must remain usable even when live-trading credentials are absent.
- Live flows depend on `POLYMARKET_PRIVATE_KEY`, `WALLET_TYPE`, and `FUNDER_ADDRESS`.
- If the user wants live trading and any of those values are missing, stop and ask the user to provide or configure them before running anything live.
- If `WALLET_TYPE=0`, `FUNDER_ADDRESS` may be ignored by the script, but still ask for the configured value unless the live setup is clearly documented otherwise.
- Use `--loop` only for the main long-lived trading process.
- Do not use system cron to repeatedly start `--loop`.
- If the user wants periodic status or PnL pushes, create a separate OpenClaw automation that reads trader outputs and reports them. That reporting task must be read-only and must not place trades.
- Before changing live-trading settings or launching live mode, confirm the user explicitly wants live trading.

## Minimal Flow

### Starting The Trader

1. Ensure dependencies are installed:
   ```bash
   uv sync
   ```
2. Determine the mode:
   - paper mode: default
   - live mode: only if the user explicitly requests it
3. If live mode is requested, verify `POLYMARKET_PRIVATE_KEY`, `WALLET_TYPE`, and `FUNDER_ADDRESS` are available before running anything.
   - If any are missing, ask the user to provide or configure the missing values and do not start live mode yet.
4. Apply requested settings with `--set` before launch when needed:
   ```bash
   uv run python -m scripts.polymarket_fast_market --set asset=BTC
   uv run python -m scripts.polymarket_fast_market --set window=15m
   uv run python -m scripts.polymarket_fast_market --set max_position=5
   uv run python -m scripts.polymarket_fast_market --set entry_threshold=0.05
   ```
5. Start the main process:
   - paper:
     ```bash
     uv run python -m scripts.polymarket_fast_market --loop
     ```
   - live:
     ```bash
     uv run python -m scripts.polymarket_fast_market --live --loop
     ```
6. Use `--simple-display` with `--loop` only when the user wants a cleaner dashboard-style terminal view.

### Inspecting The Trader

- Show live fast-market positions:
  ```bash
  uv run python -m scripts.polymarket_fast_market --positions
  ```
- Show paper positions:
  ```bash
  uv run python -m scripts.polymarket_fast_market --paper-positions
  ```
- Show resolved config and paths:
  ```bash
  uv run python -m scripts.polymarket_fast_market --config
  ```

### Reporting Automation

If the user wants periodic execution or PnL updates:

1. Treat the trader process and the reporting automation as separate concerns.
2. Keep the trader as one long-lived `--loop` process.
3. Create a separate OpenClaw automation for reporting.
4. Ask for:
   - reporting interval
   - destination or channel
   - workspace path if it is not already obvious
5. Read [references/automation_reporting.md](references/automation_reporting.md).
6. Read [references/report_payloads.md](references/report_payloads.md) for the reporting format.

## Current Workspace Notes

- The current checked-in runtime config lives in `config.json`.
- Do not assume code defaults are the active settings; inspect `config.json` or run `--config` if the current values matter.
- Supported assets are `BTC`, `ETH`, and `SOL`.
- Supported windows are `5m` and `15m`.

## References

Load only the file needed for the task:

- [references/automation_reporting.md](references/automation_reporting.md)
  - For periodic reporting automation, scheduling, data-source priority, and safety rules.
- [references/report_payloads.md](references/report_payloads.md)
  - For message structure and payload formatting.
- [references/runtime_contract.md](references/runtime_contract.md)
  - For session JSON artifacts, slug resolution, and summary/history expectations.
- [references/database_inspection.md](references/database_inspection.md)
  - For SQLite schema details and inspection queries.
- [references/troubleshooting.md](references/troubleshooting.md)
  - For common runtime failures and likely causes.

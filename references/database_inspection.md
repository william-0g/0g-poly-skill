# Database Inspection

Use this reference only when the user wants to inspect paper-trade records or the paper portfolio stored in SQLite.

## Database File

- Default path: `polymarket_fast_market_paper.db`

## Tables

- `polymarket_fast_market_trades`
- `portfolio`

## Trade Columns

When querying trades, do not invent column names. Use the schema created by this skill.

`polymarket_fast_market_trades` columns:

- `id`
- `market_id`
- `market_name`
- `strategy`
- `direction`
- `entry_price`
- `exit_price`
- `quantity`
- `pnl`
- `signal_momentum_pct`
- `volume_ratio`
- `status`
- `created_at`
- `closed_at`

Do not assume columns such as:

- `market_slug`
- `side`
- `size`

## Example Queries

Recent paper trades:

```bash
sqlite3 polymarket_fast_market_paper.db '
select
  id,
  market_id,
  market_name,
  direction,
  entry_price,
  quantity,
  status,
  created_at
from polymarket_fast_market_trades
order by id desc
limit 10;
'
```

Paper portfolio snapshot:

```bash
sqlite3 polymarket_fast_market_paper.db '
select
  total_value,
  cash,
  positions,
  updated_at
from portfolio
where id = 1;
'
```

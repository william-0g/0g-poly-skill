# Polymarket FastLoop Trader

一个支持 `dry-run`、`paper trading` 和 `live trading` 的 Polymarket fast market 信号脚本。它读取 Binance 的短周期动量，筛选 Polymarket 的 `5m` / `15m` fast market，在满足阈值时可以只打印、写入本地 SQLite，或者真实下单。

## 当前行为

- 默认是 dry run，只打印当前市场和信号判断
- `--record-paper` 会把合格信号写入 SQLite
- `--live` 会通过 Polymarket CLOB 发真实订单
- `--positions` 查看 live 仓位，`--paper-positions` 查看本地 paper 仓位

## 主要文件

- `scripts/fastloop_trader.py`: 核心策略与 SQLite 记录逻辑
- `config.json`: 默认配置
- `fastloop_paper.db`: 默认生成的 paper trading 数据库

## 安装

```bash
uv sync
```

## 用法

```bash
# 仅检查当前信号
uv run python scripts/fastloop_trader.py

# 记录一笔 paper trade
uv run python scripts/fastloop_trader.py --record-paper

# 真实下单
uv run python scripts/fastloop_trader.py --live

# 查看当前 paper positions / portfolio
uv run python scripts/fastloop_trader.py --paper-positions

# 查看 live fast market positions
uv run python scripts/fastloop_trader.py --positions

# 查看配置路径和数据库路径
uv run python scripts/fastloop_trader.py --config
```

## 配置

默认从项目根目录的 `config.json` 读取，也支持环境变量和 `--set` 覆盖。

```bash
uv run python scripts/fastloop_trader.py --set asset=BTC
uv run python scripts/fastloop_trader.py --set window=15m
uv run python scripts/fastloop_trader.py --set max_position=5
```

默认配置示例：

```json
{
  "entry_threshold": 0.05,
  "min_momentum_pct": 0.2,
  "max_position": 5.0,
  "lookback_minutes": 15,
  "min_time_remaining": 60,
  "asset": "BTC",
  "window": "15m",
  "signal_source": "binance",
  "volume_confidence": true,
  "paper_trade_db": "fastloop_paper.db"
}
```

## SQLite 结构

脚本会自动初始化两个表：

- `fastloop_trades`
  - 记录 market、方向、入场价、数量、信号强度等信息
- `portfolio`
  - 保存当前 paper cash、总价值和 open positions JSON

这是 SQLite 方言，不再使用之前 README 里混入的 `SERIAL`、`TIMESTAMPTZ`、`JSONB`。

## 调度

脚本本身只跑一轮，不内置 cron 或 Telegram。要自动化可以自己挂 cron：

```bash
*/5 * * * * cd /path/to/skill && uv run python scripts/fastloop_trader.py --live
```

如果只想观察信号，不写库：

```bash
* * * * * cd /path/to/skill && uv run python scripts/fastloop_trader.py
```

## 限制

- 现在只是一个单次扫描 + 记录 entry 的原型，没有自动平仓逻辑
- `coingecko` fallback 只能给当前价，不能提供真正的动量
- 公平价格模型仍然比较粗糙，只是固定阈值启发式，不是完整概率模型
- 没有内置 Telegram 汇总；如果要做日报，需要额外脚本读取 SQLite
- live 模式需要你自己保证私钥、资金和 Polygon/Polymarket approvals 都已配置

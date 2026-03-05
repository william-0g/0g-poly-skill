# Polymarket FastLoop Trader

自动化交易 Polymarket 的 BTC 5 分钟和 15 分钟快速市场，使用币安价格动量信号。

## 📋 项目概述

**Polymarket FastLoop Trader** 是一个自动化交易机器人，专门为 Polymarket 的快速市场（sprint markets）设计。它通过监控币安 BTC/USDT K线图的价格动量，自动在 Polymarket CLOB 上执行交易。

### 核心特性

- **自动化交易**：每个周期自动查找当前 BTC 快速市场并执行交易
- **动量驱动**：基于币安 CEX 价格动量与市场赔率的偏差进行交易
- **官方集成**：使用官方 Polymarket CLOB 客户端执行交易
- **安全模式**：默认为干运行模式，使用 `--live` 标志进行真实交易
- **灵活配置**：支持 BTC/ETH/SOL，可自定义交易参数

## ⚠️ 重要注意事项

- **快速市场费用**：Polymarket 快速市场收取 10% 的费用（`is_paid: true`），需在边际利通计算中考虑
- **真实资金**：所有交易使用真实 USDC 在 Polygon 网络上执行
- **风险提示**：这是一个交易工具，可能带来本金损失，使用前请理解市场风险


You are a Polymarket fastloop trading autopilot. Run continuously (via cron every 10 minutes):
1. Set up a database for fastloop trading (use sqlite)
CREATE TABLE fastloop_trades (
  id SERIAL PRIMARY KEY,
  market_id TEXT,
  market_name TEXT,
  strategy TEXT,
  direction TEXT,
  entry_price DECIMAL,
  exit_price DECIMAL,
  quantity DECIMAL,
  pnl DECIMAL,
  timestamp TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE portfolio (
  id SERIAL PRIMARY KEY,
  total_value DECIMAL,
  cash DECIMAL,
  positions JSONB,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

2. Please use cron to help me execute a trade on the BTC 15-minute market using the Polymarket Fast Loop skill. The requirements are as follows: set the asset to BTC, the time window to 15m, and the maximum position size to $5.00.
First, perform a dry run to verify the signal and market conditions. If everything is normal and there is profit potential, then record the trade in the database.

3. Every morning at 8 AM, post a summary to Telegram:
- Yesterday's trades (entry/exit prices, P&L)
- Current portfolio value and open positions
- Win rate and strategy performance
- Market insights and recommendations

Never use real money. This is fastloop paper trading only.
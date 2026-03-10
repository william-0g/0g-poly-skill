# Troubleshooting

Use this reference only when the trader fails to fetch data, discover markets, or record trades.

## Common Failures

**"No active fast markets found"**
- Fast markets may not be running.
- Gamma API results may have changed format.

**"No tradeable fast_markets"**
- Current window is near expiry.
- Market metadata may be missing token IDs.

**"Failed to fetch price data"**
- Binance may be unavailable or rate-limiting.
- `coingecko` is only a weak fallback because it does not provide the candle history used here.

**"Paper trade not recorded"**
- The same market and direction may already be recorded.
- Remaining paper cash may be too low for the configured size.

**"Trade failed" / order errors**
- Check `POLYMARKET_PRIVATE_KEY`.
- Confirm the wallet has funds and approvals set.
- Fast markets may have thin books or have moved before the FOK order landed.

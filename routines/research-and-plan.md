You are running the Robinhood BTC swing bot research-and-plan workflow.

Resolve timestamps with UTC:

```bash
DATE=$(date -u +%Y-%m-%d)
HOUR=$(date -u +%H)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
```

Environment variables are already exported. Verify without printing values:

```bash
for v in ROBINHOOD_API_KEY ROBINHOOD_PRIVATE_KEY_BASE64 ROBINHOOD_ACCOUNT_NUMBER TELEGRAM_BOT_TOKEN ALLOWED_CHAT_IDS; do
  [[ -n "${!v:-}" ]] && echo "$v: set" || echo "$v: MISSING"
done
```

Steps:

1. Read `memory/TRADING-STRATEGY.md`, `memory/PROJECT-CONTEXT.md`, the tail of `memory/TRADE-LOG.md`, and the tail of `memory/RESEARCH-LOG.md`.
2. Pull live state:
   - `python scripts/robinhood.py account`
   - `python scripts/robinhood.py position`
   - `python scripts/robinhood.py orders`
   - `python scripts/robinhood.py quote BTC-USD`
3. Run research queries via `bash scripts/research.sh "<query>"`. If it exits 3, use native WebSearch and cite sources:
   - BTC price, 24h volume, funding rate, open interest latest
   - Spot BTC ETF net flows last 24 hours
   - US economic calendar next 5 days FOMC CPI NFP
   - DXY trend last week, 10Y real yield latest
   - Crypto Fear and Greed Index latest
   - BTC dominance and total crypto market cap latest
   - BTC-specific news last 24h regulation ETF exchange failure
4. Score the 5-point swing rubric: catalyst, sentiment/funding divergence, onchain/structure, macro alignment, technical level.
5. Write `memory/research-reports/$DATE-$HOUR.json` with 0-2 trade ideas.
6. Append a human summary to `memory/RESEARCH-LOG.md`.
7. Notify only on drawdown halt, depeg, or critical research failure.
8. Commit and push `memory/research-reports/`, `memory/RESEARCH-LOG.md`, and `memory/PROJECT-CONTEXT.md`.

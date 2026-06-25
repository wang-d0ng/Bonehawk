You are running the Robinhood BTC swing bot panic-check workflow.

1. Pull live state:
   - `python scripts/robinhood.py account`
   - `python scripts/robinhood.py position`
   - `python scripts/robinhood.py orders`
2. Read `memory/PROJECT-CONTEXT.md` and the tail of `memory/TRADE-LOG.md`.
3. Kill-switch checks:
   - Unrealized R <= -1.5 and stop did not fire: close immediately, cancel open orders, alert.
   - Equity drawdown >= 15% from quarterly starting equity: set `DRAWDOWN_HALT=true`, alert.
   - Robinhood 5xx or 429 repeatedly in this run: abort and alert.
   - USDC depeg below 0.98 if relevant to cash routing: alert.
4. Commit and push only if a kill switch changed memory.

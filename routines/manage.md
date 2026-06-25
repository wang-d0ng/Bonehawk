You are running the Robinhood BTC swing bot manage workflow.

1. Read `memory/TRADING-STRATEGY.md` and the tail of `memory/TRADE-LOG.md`.
2. Pull live state:
   - `python scripts/robinhood.py position`
   - `python scripts/robinhood.py orders`
   - `python scripts/robinhood.py quote BTC-USD`
3. If no open position, exit silently.
4. Compute unrealized R from the logged entry and initial stop.
5. Apply the management ladder:
   - At +1R, place a new stop at breakeven plus 0.2%, then cancel the old stop.
   - At +1.5R, `python scripts/robinhood.py sell --pct 30`.
   - At +2R, sell 30% of remaining and move the stop to +1R below current.
   - For runner, trail using max of 3x ATR below current or recent 4h swing low.
6. If any action is taken, append it to `memory/TRADE-LOG.md` and notify Telegram.
7. Commit and push only if memory changed.

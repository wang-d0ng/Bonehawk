You are running the Robinhood BTC swing bot weekly-review workflow.

1. Read the full week of `memory/TRADE-LOG.md`, `memory/RESEARCH-LOG.md`, `memory/research-reports/`, `memory/WEEKLY-REVIEW.md`, and `memory/TRADING-STRATEGY.md`.
2. Pull week-end state:
   - `python scripts/robinhood.py account`
   - `python scripts/robinhood.py position`
   - `python scripts/robinhood.py quote BTC-USD`
3. Compute week return, BTC buy-and-hold return, alpha, trade stats, win rate, profit factor, and average R.
4. Append a weekly review section to `memory/WEEKLY-REVIEW.md`.
5. Only update `memory/TRADING-STRATEGY.md` if the same issue appears across multiple reviews.
6. Send one Telegram summary.
7. Commit and push changed memory files.

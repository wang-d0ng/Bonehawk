You are running the Robinhood BTC swing bot execute workflow.

BTC-USD spot only. No leverage, options, margin, or altcoins.

1. Read `memory/TRADING-STRATEGY.md`, `memory/PROJECT-CONTEXT.md`, latest `memory/research-reports/*.json`, and the tail of `memory/TRADE-LOG.md`.
2. Abort if the latest research report is older than 45 minutes.
3. Pull live state:
   - `python scripts/robinhood.py account`
   - `python scripts/robinhood.py position`
   - `python scripts/robinhood.py orders`
   - `python scripts/robinhood.py quote BTC-USD`
4. Check cooldown, drawdown halt, existing BTC position, and rolling 7-day entry count.
5. Run the buy-side gate from `memory/TRADING-STRATEGY.md`. Log every pass/fail check.
6. If all checks pass, compute size:
   - A grade: risk 1.0% of equity
   - B grade: risk 0.5% of equity
   - `size_usd = risk_usd / ((entry - stop) / entry)`, rounded down to nearest $10
7. Before any live order, verify `TRADING_MODE=live`; otherwise stop and log paper-mode skip.
8. Atomic buy + stop:
   - `python scripts/robinhood.py buy --usd <size_usd>`
   - Poll `python scripts/robinhood.py orders` until the buy is filled or timeout.
   - Compute filled BTC size from executions.
   - `python scripts/robinhood.py stop --base <base_size> --stop-price <stop> --limit <stop_minus_buffer>`
   - If stop placement fails, run `python scripts/robinhood.py close` and alert.
9. Append trade details to `memory/TRADE-LOG.md`.
10. Send Telegram only if a trade was placed or a critical failure occurred.
11. Commit and push only if memory changed.

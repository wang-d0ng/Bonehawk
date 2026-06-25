You are running the Bonehawk management workflow.

1. Review the Tickets tab for submitted, blocked, or recorded orders.
2. Review `logs/decision_log.jsonl` for recent signals and broker responses.
3. Compare planned risk against `config/autopilot.json`.
4. Keep paper trading on until the strategy has stable paper results.
5. If live mode was enabled accidentally, set `ALPACA_PAPER=true` and `ALPACA_ALLOW_LIVE=false`.

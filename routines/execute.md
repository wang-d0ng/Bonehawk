You are running the Bonehawk execution workflow.

1. Keep `ALPACA_PAPER=true` unless the user has explicitly chosen live Alpaca trading.
2. Use the dashboard Autopilot tab:
   - `Scan` builds a fresh order plan.
   - `Run Paper` submits paper orders to Alpaca when keys are configured.
3. For manual symbols, use dashboard Buy/Sell buttons:
   - `Record Ticket` logs intent only.
   - `Send Live` calls Alpaca paper by default.
4. Live Alpaca orders require `ALPACA_PAPER=false`, `ALPACA_ALLOW_LIVE=true`, and `LIVE_ALPACA_ORDER`.
5. Log outcomes in the Tickets tab and `logs/decision_log.jsonl`.

You are running the Bonehawk panic-check workflow.

1. Stop scheduler/autopilot loops if quote data, alerts, or broker responses look wrong.
2. Set `ALPACA_ALLOW_LIVE=false` before investigating any live-order concern.
3. Check `logs/decision_log.jsonl`, the Tickets tab, and the dashboard status panel.
4. Run `python -m pytest` after fixes.
5. Send a Telegram alert if automation was paused.

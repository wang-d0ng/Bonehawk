# Routines

These prompts adapt the Opus 4.7 Coinbase setup guide to Robinhood Crypto.
Configure them as scheduled agent runs if you use a cloud routine system.

Required routine environment variables:

- `ROBINHOOD_API_KEY`
- `ROBINHOOD_PRIVATE_KEY_BASE64`
- `ROBINHOOD_ACCOUNT_NUMBER`
- `ROBINHOOD_API_VERSION=v2`
- `TRADING_MODE=paper` or `live`
- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_CHAT_IDS`

Keep `TRADING_MODE=paper` until read-only smoke tests and one manual dry review pass succeed.

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/.env"
FALLBACK="$ROOT/DAILY-SUMMARY.md"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ $# -gt 0 ]]; then
  msg="$*"
else
  msg="$(cat)"
fi

if [[ -z "${msg// /}" ]]; then
  echo "usage: bash scripts/telegram.sh \"<message>\"" >&2
  exit 1
fi

stamp="$(date -u '+%Y-%m-%d %H:%M UTC')"

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${ALLOWED_CHAT_IDS:-}" ]]; then
  printf "\n---\n## %s (fallback - Telegram not configured)\n%s\n" "$stamp" "$msg" >> "$FALLBACK"
  echo "[telegram fallback] appended to DAILY-SUMMARY.md"
  echo "$msg"
  exit 0
fi

if [[ ${#msg} -gt 4000 ]]; then
  msg="${msg:0:3990}..."
fi

IFS=',' read -ra CHAT_IDS <<< "${ALLOWED_CHAT_IDS}"
for raw_id in "${CHAT_IDS[@]}"; do
  chat_id="${raw_id// /}"
  [[ -z "$chat_id" ]] && continue
  curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${chat_id}" \
    --data-urlencode "text=${msg}" \
    --data-urlencode "disable_web_page_preview=true"
  echo
done

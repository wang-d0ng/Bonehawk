#!/usr/bin/env bash
set -euo pipefail

query="${1:-}"
if [[ -z "$query" ]]; then
  echo "usage: bash scripts/research.sh \"<query>\"" >&2
  exit 1
fi

echo "WARNING: research backend not configured. Fall back to WebSearch." >&2
echo "QUERY_FOR_WEBSEARCH: $query"
exit 3

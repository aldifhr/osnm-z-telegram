#!/usr/bin/env bash
# osnm-z launcher — pinned interpreter, env check, no surprises.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "[!] uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

[ -f .env ] || { cp .env.example .env; echo "[!] created .env — fill WALLET_KEY + RPC_URL"; exit 1; }

# .env.example is all-or-nothing: config.py rejects partial overrides.
missing=""
for k in WALLET_KEY RPC_URL RPC_REQUEST_TIMEOUT_MS FEE_AUTOMATIC GAS_LIMIT \
         PUBLIC_MINT_BROADCAST_OFFSET_MS PENDING_TIMEOUT_SECONDS \
         RECEIPT_POLL_INTERVAL_MS OPENSEA_REQUEST_TIMEOUT_MS \
         ELIGIBILITY_REQUEST_TIMEOUT_MS OPENSEA_ACTION_REQUEST_TIMEOUT_MS \
         OPENSEA_ATTEMPTS OPENSEA_RETRY_INTERVAL_MS OPENSEA_CALLDATA_ATTEMPTS; do
  grep -qE "^${k}=" .env || missing="$missing $k"
done
[ -z "$missing" ] || { echo "[!] missing in .env:$missing" >&2; exit 1; }

exec uv run --frozen osnm-z "$@"

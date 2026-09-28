#!/usr/bin/env sh
# osnm-z Telegram bot launcher.
#
# Two env files, deliberately separate: osnm_z.config._validate_known_settings
# rejects any key it does not recognise, so Telegram secrets must NOT live in the
# app .env next to WALLET_KEY. bot/.env is sourced into the process environment
# only and never read by the mint library.
set -eu
# This script must live at <osnm-z checkout>/bot/run-bot.sh: the app root is
# one level up, and bot.py imports the upstream package from ../src.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$APP_DIR"

[ -f "$APP_DIR/.env" ] || { echo "[!] missing $APP_DIR/.env (app config)" >&2; exit 1; }
[ -f "$SCRIPT_DIR/.env" ] || { echo "[!] missing $SCRIPT_DIR/.env (telegram config)" >&2; exit 1; }

set -a
. "$SCRIPT_DIR/.env"
set +a

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN not set}"
: "${TELEGRAM_ALLOWED_CHAT_ID:?TELEGRAM_ALLOWED_CHAT_ID not set}"

# systemd does not inherit the interactive shell PATH, so resolve uv explicitly.
UV=$(command -v uv || echo /root/.hermes/bin/uv)
[ -x "$UV" ] || { echo "[!] uv not found (PATH and /root/.hermes/bin)" >&2; exit 1; }

# Keep uv's cache inside the app tree: the unit runs with ProtectHome=read-only,
# so the default /root/.cache/uv is not writable.
export UV_CACHE_DIR="$APP_DIR/.uv-cache"
export TMPDIR="$APP_DIR/.tmp"
mkdir -p "$UV_CACHE_DIR" "$TMPDIR"

exec "$UV" run --frozen --no-sync python bot/bot.py

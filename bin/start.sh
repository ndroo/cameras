#!/bin/sh
# Runs go2rtc (local only, port 1984) and the web app side by side.
set -e
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }
export INTERNAL_SECRET="${INTERNAL_SECRET:-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
export DATA_DIR="${DATA_DIR:-$PWD/data}"
export GO2RTC_CONFIG="${GO2RTC_CONFIG:-$DATA_DIR/go2rtc.yaml}"
mkdir -p "$DATA_DIR"
[ -f "$GO2RTC_CONFIG" ] || cp go2rtc.yaml "$GO2RTC_CONFIG"
GO2RTC_BIN="${GO2RTC_BIN:-go2rtc}"
"$GO2RTC_BIN" -config "$GO2RTC_CONFIG" &
G=$!
trap 'kill $G 2>/dev/null' EXIT INT TERM
exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*' --ws-ping-timeout 60

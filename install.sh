#!/usr/bin/env bash
# Stock Radar one-step installer for an Ubuntu/Debian server.
# Run from inside the unzipped folder:   bash install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
say() { printf "\n\033[1;33m==> %s\033[0m\n" "$*"; }

say "1/6 Installing Python (needs sudo)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv curl >/dev/null
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

say "2/6 Telegram bot"
echo "In Telegram: open @BotFather, send /newbot, pick a name, copy the token it gives you."
while true; do
  read -rp "Paste the bot token: " TOKEN
  NAME=$(curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | python3 -c \
    'import sys,json; d=json.load(sys.stdin); print(d["result"]["username"] if d.get("ok") else "")' 2>/dev/null || true)
  [ -n "$NAME" ] && { echo "Token OK: @$NAME"; break; }
  echo "That token didn't work. Copy it again from BotFather (it looks like 123456:ABC-...)."
done

say "3/6 Linking your chat"
echo "Now open Telegram, search @$NAME, press Start (or send 'hi'). Waiting up to 3 minutes..."
CHAT=""
for _ in $(seq 1 60); do
  CHAT=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates" | python3 -c \
    'import sys,json; r=json.load(sys.stdin).get("result",[]); print(r[-1]["message"]["chat"]["id"] if r and "message" in r[-1] else "")' 2>/dev/null || true)
  [ -n "$CHAT" ] && break
  sleep 3
done
if [ -z "$CHAT" ]; then
  read -rp "Didn't see your message. Type your chat id manually: " CHAT
fi
echo "Chat linked: $CHAT"

say "4/6 Your portfolio"
read -rp "NSE symbols you own, comma separated (e.g. TCS,HAL,IRFC). Enter to skip, add later with /add: " PORT
PORT=$(echo "$PORT" | tr '[:lower:]' '[:upper:]' | tr -d ' ')

say "5/6 Writing settings and checking every data source"
[ -f .env ] && cp .env ".env.backup.$(date +%s)"
sed -e "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=${TOKEN}|" \
    -e "s|^TELEGRAM_CHAT_ID=.*|TELEGRAM_CHAT_ID=${CHAT}|" \
    -e "s|^PORTFOLIO=.*|PORTFOLIO=${PORT}|" .env.example > .env
chmod 600 .env
.venv/bin/python -m radar.check || true
echo "(A FAIL on NSE with 403 means NSE blocks this server's IP: see README 'Things to know'.)"

say "6/6 Starting it as a service that survives reboots"
sudo tee /etc/systemd/system/stockradar.service >/dev/null <<EOF
[Unit]
Description=Stock Radar (NSE/BSE news, filings and price alerts)
After=network-online.target
Wants=network-online.target

[Service]
User=$(whoami)
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python -m radar.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo timedatectl set-timezone Asia/Kolkata || true
sudo systemctl daemon-reload
sudo systemctl enable --now stockradar
sleep 3
sudo systemctl --no-pager --lines=5 status stockradar || true

say "Done. You should get '🛰️ Stock Radar online' in Telegram now."
echo "Live logs:  journalctl -u stockradar -f"
echo "Restart after editing .env:  sudo systemctl restart stockradar"

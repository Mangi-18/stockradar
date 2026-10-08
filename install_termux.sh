#!/data/data/com.termux/files/usr/bin/bash
# Stock Radar installer for an Android phone running Termux (install Termux from F-Droid,
# not the Play Store). Run from inside the unzipped folder:   bash install_termux.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
say() { printf "\n\033[1;33m==> %s\033[0m\n" "$*"; }

say "1/5 Installing Python"
pkg update -y >/dev/null
pkg install -y python curl >/dev/null
pip install -q -r requirements.txt

say "2/5 Telegram bot"
echo "In Telegram: open @BotFather, send /newbot, pick a name, copy the token it gives you."
while true; do
  read -rp "Paste the bot token: " TOKEN
  NAME=$(curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | python -c \
    'import sys,json; d=json.load(sys.stdin); print(d["result"]["username"] if d.get("ok") else "")' 2>/dev/null || true)
  [ -n "$NAME" ] && { echo "Token OK: @$NAME"; break; }
  echo "That token didn't work. Copy it again from BotFather."
done
echo "Now open Telegram, search @$NAME and press Start. Waiting up to 3 minutes..."
CHAT=""
for _ in $(seq 1 60); do
  CHAT=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates" | python -c \
    'import sys,json; r=json.load(sys.stdin).get("result",[]); print(r[-1]["message"]["chat"]["id"] if r and "message" in r[-1] else "")' 2>/dev/null || true)
  [ -n "$CHAT" ] && break
  sleep 3
done
[ -z "$CHAT" ] && read -rp "Didn't see your message. Type your chat id manually: " CHAT
echo "Chat linked: $CHAT"

say "3/5 Your portfolio"
read -rp "NSE symbols you own, comma separated (e.g. TCS,HAL,IRFC). Enter to skip: " PORT
PORT=$(echo "$PORT" | tr '[:lower:]' '[:upper:]' | tr -d ' ')

say "4/5 Writing settings and checking every data source"
sed -e "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=${TOKEN}|" \
    -e "s|^TELEGRAM_CHAT_ID=.*|TELEGRAM_CHAT_ID=${CHAT}|" \
    -e "s|^PORTFOLIO=.*|PORTFOLIO=${PORT}|" .env.example > .env
chmod 600 .env
python -m radar.check || true

say "5/5 Starting it (and on every phone restart, if Termux:Boot is installed)"
cat > run.sh <<EOF
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock 2>/dev/null || true
cd "${APP_DIR}"
while true; do
  python -m radar.main >> radar.log 2>&1
  echo "restarting in 10 s" >> radar.log; sleep 10
done
EOF
chmod +x run.sh
mkdir -p ~/.termux/boot
printf '#!/data/data/com.termux/files/usr/bin/bash\nnohup %s/run.sh >/dev/null 2>&1 &\n' "$APP_DIR" > ~/.termux/boot/start-radar.sh
chmod +x ~/.termux/boot/start-radar.sh
pkill -f "radar.main" 2>/dev/null || true
nohup ./run.sh >/dev/null 2>&1 &

say "Done. You should get '🛰️ Stock Radar online' in Telegram now."
echo "Keep the phone on charger and Wi-Fi. In Android settings, set Termux battery to 'Unrestricted'."
echo "Logs: tail -f ${APP_DIR}/radar.log"

# Stock Radar — real-time NSE/BSE alerts on Telegram

Watches exchange filings, insider trades, big deals, pre-open orders and live
news, and messages you on Telegram within seconds, so you can act before most
investors have even seen the news.

## What you get, ordered by how EARLY it is

Nobody legally knows news before it's public. The edge is acting in the
first seconds and minutes after it goes public, before most investors notice.

| When | Alert | Why it's early |
|---|---|---|
| Days ahead | **Catalyst calendar** (8:30am) and instant alerts when a board meeting for **bonus / split / buyback / fund raising** is announced | The event is known in advance; stocks often run up between announcement and record date |
| Hours to days ahead | **Promoter buying** (SEBI insider filings, every 2 min) | Promoters know their business best; buying with their own money is one of the most reliable signals |
| Hours ahead | **Bulk / block deals** (big funds taking stakes, every 5 min) | Large investors positioning |
| 9 min before open | **Pre-open gaps** at ~9:06am with the filing/news behind each gap | You see the opening price before trading starts |
| Seconds after release | **Exchange filings** (NSE + BSE every 15 s), **results with numbers + score** (every 30 s) | The exchange is the first public source; news sites follow minutes later |
| Seconds after release, **24x7** | **Live news every 30 s, day and night** from 15 feeds (ET, Moneycontrol, Business Standard, Mint, Financial Express, BusinessLine, NDTV Profit, govt press releases, and Google News, which aggregates hundreds of Indian outlets). Headlines are matched to **every NSE company** (~2,000). Your portfolio stocks alert; everything else goes through the opportunity score below | News is the biggest driver of moves; it arrives at any hour |
| Seconds after release | **Sector / policy news** (defence, railways, crude, RBI, steel duty, USFDA, IT/visa, power, gold, telecom, autos, rural, infra) with the most exposed stocks | Policy news moves whole sectors before any company files |
| Before the open | **Pre-market brief at 8:30am**: everything since yesterday's 3:30pm close (overnight and weekend news), ranked: global cues, your stocks, big news on other stocks, sectors. Then the catalyst calendar. Send `/brief` any time for the last 12 h (`/brief 4` = last 4 h) | You start the day knowing what happened while you slept |
| Start of a move | **Early momentum** (every 30 s): +/-1.5% within 5 min; 🔥 when volume is also 3x its normal pace | Catches a move at +2%, not +8% |
| After (confirmation) | 5% / 10% crossings, 52-week highs, each with the likely reason | For context, not for entry |

News headlines that only report a move that already happened ("shares jump 8%")
are tagged ⏱️ *already moving*, so you don't chase them blindly.

**Results score (−4 to +4)** comes from `radar/results.py`: profit growth YoY,
revenue growth YoY, net-margin change, and loss↔profit swings. Every point is
explained in the alert. It compares with last year, not analyst estimates, and
it does not predict the price.

## Understanding indirect news (AI reasoning)

Headlines rarely say "this stock will rise". "Government raises steel import
duty" means domestic steel makers can charge more (up) and steel buyers like
carmakers pay more (down). With a free Gemini or Groq key (`/setkey`), the
engine sends important headlines (your stocks, policy, sector, high-impact
news) to an AI model that answers: which NSE companies are affected, including
second-order effects, which direction, how big, how confident, and why. Your
portfolio alerts then show a 🤖 line with that reasoning, and the result feeds
the evidence score. Invented symbols are discarded; only real NSE symbols are
kept. `/ai` shows how many headlines it read today.

## ⚡ Breaking alerts (any stock, first report)

The fastest alert. When a single headline, or an NSE/BSE filing, is judged by
the AI to mean a LARGE move (>5%) with 80%+ confidence, and the price hasn't
reacted yet (<3% so far), you get it immediately, day or night, without
waiting for other outlets. Filings are read by the AI within ~15 seconds of
being posted, so this often beats every news site. Once per stock per day, at
most 10 a day. Tune with `/set breakconf 0.75` and `/set maxbreak 15`. It is
single-source, so sometimes wrong; the 📣 story alert is the confirmed version.

## 📣 Story alerts (any stock, not just yours)

When 3 or more different outlets cover the same stock within 3 hours, the story
is spreading. You get one 📣 alert for that stock that day, with the AI's read
(which way, how big, why), the headlines and their sources, and today's price
change so far, flagged ⏱️ if the price has already moved a lot. If the AI sees
no price effect (a CEO interview, generic commentary), it stays quiet. Change
the trigger with `/set outlets 2` and the daily cap with `/set maxstory 15`.

## Learning from what actually happened

Every directional signal (filing, results, AI read, promoter buy, block deal,
high-impact news) is recorded with the price when it fired, 1 hour later and 1
trading day later. `/learn` shows, per kind of signal, how often the price
moved the predicted way and by how much on average. Once a kind of signal has
10+ measured cases, it nudges the evidence score: ±10 for signals that have
worked (≥60%) or failed (<45%) so far.

## How it avoids spamming you

| Who | What reaches you |
|---|---|
| **Your portfolio** (`PORTFOLIO` in `.env`, or `/add` in Telegram) | Every relevant filing, news item, results, promoter trade, deal, price burst and 5%/10% move. Urgent items (HIGH-impact news, results, big moves) arrive instantly; routine ones are limited to one ping per stock per hour, the rest wait for the digest |
| **Everything else** (~2,000 stocks) | Watched silently. A stock pings you only as a 🎯 **Opportunity**, when independent signals agree: evidence score ≥ 70/100, at most **5 per day**, once per stock per day |
| Sector / policy news | Only pings if it affects a stock you own; otherwise it goes into the digest |
| Quiet hours (11pm–7am) | Only urgent portfolio alerts; everything else is in the 8:30am brief |

**Evidence score (0–100)**, from `radar/scoring.py`. One headline is weak
evidence; agreement between independent signals is strong:

- strongest single item: exchange filing or results far from last year +40, promoter buying +35, high-impact news +30, bulk/block deal +25
- +10 per extra outlet covering it (max +30)
- +12 per extra *kind* of signal (filing + news + promoter buy + deal + price burst; max +24)
- +15 if the price is moving on heavy volume
- −25 if the price has already moved a lot, −15 for mixed signals, −10 if the direction is unclear

Example: an order-win filing reported by 3 outlets scores 72 and pings you. The
same filing with only 1 outlet scores 40 and goes to the digest. It ranks the
strength of evidence; it is not a probability.

Typical day: a handful of portfolio alerts, up to 5 opportunities, 3 digests
(12:30, 3:45pm, 8:30pm), the 8:30am brief and the 9:06 pre-open. All limits are
in `.env`.

## Setup (about 30 minutes, free)

### 1. Telegram bot
1. In Telegram, open **@BotFather** → `/newbot` → copy the token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   `"chat":{"id": ...}` — that's your chat id.

### 2. Free cloud VM (always on)
Oracle Cloud "Always Free" is the most generous. Create an Ubuntu VM in the
**Mumbai or Hyderabad** region (Indian IPs are less likely to be blocked by NSE).
Google Cloud's free e2-micro also works.

### 3. Install
```bash
sudo apt update && sudo apt install -y python3-venv git
cd ~ && mkdir stockradar   # then upload this folder's files into it (scp, or git)
cd ~/stockradar
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env      # paste token + chat id
.venv/bin/python -m radar.check        # every line should say OK
```

### 4. Keep it running 24/7
```bash
sudo cp deploy/stockradar.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now stockradar
journalctl -u stockradar -f            # live logs
```
You'll get "🛰️ Stock Radar online" in Telegram.

## Family: everyone gets their own

Anyone who finds the bot in Telegram and presses **Start** can use it right away,
with **their own** portfolio, mutes, settings, digests and briefs. Nobody sees
anyone else's stocks. The market is scanned once for everyone, so it costs no
more to run.

- Portfolio alerts go only to the people who hold that stock
- ⚡ / 🎯 / 📣 market alerts go to everyone who doesn't hold it, within each person's own limits
- The owner (`TELEGRAM_CHAT_ID` in `.env`) is told when someone new starts, and has
  ☰ More → 👥 Users to see who's using it and 🚫 remove anyone
- Only the owner can ⬆️ update and 🔑 set the AI key; the AI's daily budget is shared

## 📊 Overall verdict (no more mixed signals)

Every alert about one of your stocks now carries the stock's **overall** verdict
from all of the last 24 hours' signals, e.g. "📊 Overall (24h): 🔴🔴 Strongly
negative · 1 positive vs 3 negative · Biggest factor: DGCA grounds 40 aircraft".
Signals are weighted (filing > results > promoter trade > AI read > deal > news >
price burst; AI by its confidence), older ones count less (half every 8 h), and
positives and negatives are netted. If the verdict flips (positive → negative or
back), you get a 🔄 alert right away. Logic: `radar/outlook.py`.

## ✅ High certainty

One tap shows only the stocks where the evidence clearly points one way: at
least 75% of the signals agree, the combined weight is large, and either the
evidence score is 70+ or the AI is 80%+ confident. No count limit; it can be 0
stocks or 20. Each shows the agreement %, evidence score, AI confidence, today's
move and the biggest factor, with ⏱️ if it has already moved a lot.

## 🔎 Type any stock

Type a stock's symbol or name (`tcs`, `indigo`, `tata steel`) as a message to get:
price and today's move, the **overall verdict** for 24 hours (and 3 days if
different) combining positive and negative news, the **AI's net reading** with
the main positives ➕ and negatives ➖, and the latest 10 news items with each
one's tone. News comes from the engine's own history plus a fresh Google News
search, so it works for any NSE stock, not only ones you track. If the name
matches several companies, you get buttons to pick.

## 📡 Telegram channels

The owner can add **public** Telegram channels (☰ More → 📡 Channels → ➕ Add
channel, then send `@channelname` or a `t.me/...` link). The engine reads each
channel's public web preview every minute. A post that mentions someone's
stock, or that the AI judges will affect it, is forwarded to the people who
hold it, with the AI's reading. Channel posts never trigger market-wide ⚡ / 🎯
/ 📣 alerts, because tip channels can be used to pump stocks. Private channels
and groups can't be read this way.

## Using it: buttons, no typing

- **Button panel** below the typing box: ✅ High certainty · ⭐ Portfolio · 🗞️ Brief · 🎯 Top · 🤖 Ask AI · 📥 Digest · ⚙️ Settings · 📊 Learn · ☰ More
- **⭐ Portfolio** shows each stock's overall verdict, busiest first, with quiet stocks on one line; ✏️ Edit portfolio to add or remove
- **☰ Menu** button next to the typing box lists every function with a short description
- **Settings** are ➕ / ➖ buttons; **Portfolio** has ❌ buttons to remove and ➕ Add stocks
- Every ⚡ / 🎯 / 📣 alert has **➕ Add to portfolio** and **🔇 Mute** buttons
- **Paste any headline** as a message and the AI tells you which stocks it likely moves

## Telegram commands (still work if you prefer typing)
- `/add TCS HAL IRFC` — add stocks to your portfolio
- `/remove IRFC`
- `/list` — your portfolio and muted stocks
- `/mute XYZ` / `/unmute XYZ` — never hear about a stock
- `/top` — strongest candidates right now, with scores
- `/brief` — news summary of the last 12 hours (`/brief 4` for the last 4)
- `/digest` — send the held-back items now
- `/status` — is it alive, opportunity alerts used today, failing sources
- `/settings` — see all limits; `/set maxopp 15`, `/set score 60`, `/set cooldown 0`, `/set quiet off` change them instantly (no server login needed)
- `/update` — install the latest version from GitHub and restart (after the one-time setup below)
- `/setkey YOUR_KEY` — turn on AI reasoning (free key from aistudio.google.com/apikey or console.groq.com/keys); `/ai` shows its status
- `/ask HEADLINE` — paste any news headline; the AI tells you which stocks it likely moves, which way and why
- `/learn` — how each kind of signal actually played out so far

## One-time setup for one-tap updates

1. The code lives in a GitHub repo (e.g. `yourname/stockradar`).
2. On the server, once:
   ```
   cd ~/stockradar
   git init -b main && git remote add origin https://github.com/YOURNAME/stockradar.git
   git fetch origin && git reset --hard origin/main
   sudo systemctl restart stockradar
   ```
   Your `.env` settings, portfolio and history are kept (they're not in the repo).
3. From then on, send `/update` in Telegram whenever there's a new version.

## Things to know
- **NSE blocks some cloud IPs.** If `radar.check` shows NSE `FAIL ... 403`, try
  another region or provider. BSE usually still works, so filings keep coming.
  If a source fails 5 times in a row you get a ⚠️ alert, so you're never
  silently blind.
- These are NSE's website endpoints, not an official paid feed. They can change
  without notice. If something breaks, the fix is usually a field name in
  `nse.py`.
- First run on an empty database stays quiet about old filings. After a
  restart it alerts anything filed while it was down.
- Want tick-level prices later? Add Zerodha Kite Connect's WebSocket; for
  swing/long-term trading, filings are the bigger edge.
- Nothing here is investment advice. A filing alert means "look now",
  not "buy now".

## Tests
`python tests/test_radar.py` — offline tests for classification, XBRL parsing
and scoring, alert de-duplication and price-level logic.

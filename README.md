# flight-steal-deals

Hourly scanner for **steal deals on Indian domestic flights**, with priority
watch on **Lakshadweep (AGX)** direct / 1-stop fares. Runs entirely on
**GitHub Actions** (not your laptop) and messages a Telegram bot every hour —
a 🔥 alert when a steal is found, a short ✅ heartbeat otherwise.

## How it decides a fare is a "steal"

For every route × date it takes the **minimum across all enabled sources** and
alerts when any of these hit:

1. price ≤ the route's `steal_below_inr` hard threshold (config.json)
2. price ≤ 70% of the rolling 45-day median for that route + booking window
   (needs ≥ 25 samples, accumulates automatically in `price_history.json`,
   committed back by the workflow each run)
3. Google marks the route "low" **and** price ≤ 85% of median

Same route+date won't re-alert for 24 h unless it drops another 5%.

## Sources

| Source | Key needed | Notes |
|---|---|---|
| Google Flights | none | aggregates airlines + most OTAs; primary source |
| Amadeus (test) | free — [developers.amadeus.com](https://developers.amadeus.com) | **needed for HYD→AGX** (Alliance Air/GDS fares Google can't price); runs every 8 h on priority routes to stay in free quota |
| Aviasales | free — [travelpayouts.com](https://www.travelpayouts.com/) token | cached OTA prices, cheap extra signal |

Sources are optional & independent — the scanner uses whichever keys exist.
Direct scraping of MakeMyTrip/ixigo/Cleartrip is deliberately out: they
bot-block datacenter IPs, so a CI scraper dies within days.

## Setup (one time, ~5 min)

1. **New Telegram bot**: message [@BotFather](https://t.me/BotFather) →
   `/newbot` → name it (e.g. `keus_flight_deals_bot`) → copy the token.
   Then open your new bot and press **Start** (required before it can message you).
2. **Create the GitHub repo** (public = unlimited Actions minutes) and push
   this folder.
3. Repo → Settings → Secrets and variables → Actions → add:
   - `TG_BOT_TOKEN` — from BotFather
   - `TG_CHAT_IDS` — your chat id (comma-separate for more)
   - optional: `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET`, `TRAVELPAYOUTS_TOKEN`
4. Actions tab → `flight-deal-scan` → **Run workflow** to test; the hourly
   cron takes over from there.

## Local test

```powershell
# .env with TG_BOT_TOKEN / TG_CHAT_IDS (see .env.example), then:
pip install -r requirements.txt
$env:SCAN_LIMIT='10'; $env:DRY_RUN='1'; python scanner.py
```

## Date-window watches (every day of a month, both ways)

`config.json` → `watches` tracks the **lowest fare for every single day** of a
fixed window, in each direction — currently Hyderabad ⇄ Goa for all of
January 2027 (Goa = GOI Dabolim + GOX Mopa, cheaper airport wins per day).

- **Every hourly message** gets a short block: the three lowest price levels
  per direction and the days that have them (`summary_levels`).
- **Twice a day** (`calendar_hours_ist`, default 08:00 and 20:00 IST) the full
  day-by-day list goes out, one message per direction: price, airport and the
  flights at that price; tap a day to open it in Google Flights. 🟢 marks the
  `top_n` cheapest days.
- A fare at or below a direction's `steal_below_inr` joins the 🔥 steal alert.
- Each run re-checks half the window (`refresh_slices: 2` → every day
  refreshed every 2 h) plus any day it has no fare for; the latest fare per
  day is kept in `price_history.json` → `watch`. Code: `watch.py`.
- The watch switches itself off after `end_date`. To watch another month or
  city pair, add another entry to `watches` with its own `id`.

## Tuning

- Routes / stops / thresholds: `config.json` → `routes`
- Scan window: `scan_window` — days 2–92 ahead, split into 12 rotating
  hourly slices, so the full next-90-days window is covered every 12 h
  (each departure date re-checked twice a day)
- Ratio & baseline knobs: `deal` block
- Skip night heartbeats: `heartbeat.quiet_hours_ist`, e.g. `[0,1,2,3,4,5,6]`
  (steal alerts always send)
- If Google starts blocking GitHub's IPs: set `FF_FETCH_MODE=local` in the
  workflow env and add `pip install playwright && playwright install chromium`
  as a step.

## Notes

- GitHub cron is best-effort; runs can drift 5–30 min. `[skip ci]` history
  commits also keep the repo "active" so GitHub never auto-disables the schedule.
- Google is queried as desktop Chrome with `curr=INR`, so fares are exact
  rupee prices and always carry departure time + airline. If a page still
  comes back in USD it is converted with a live rate (open.er-api.com).

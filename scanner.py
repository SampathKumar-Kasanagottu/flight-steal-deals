"""Indian domestic flight steal-deal scanner.

Runs hourly on GitHub Actions. Pulls one-way fares from every enabled source
(Google Flights always; Amadeus + Aviasales when API keys are configured),
builds a rolling per-route price baseline, and Telegrams an alert when a fare
is meaningfully below usual. Sends a short heartbeat when nothing is found.

Env:
  TG_BOT_TOKEN, TG_CHAT_IDS            required (Telegram)
  AMADEUS_CLIENT_ID/_CLIENT_SECRET     optional extra source
  TRAVELPAYOUTS_TOKEN                  optional extra source
  SCAN_LIMIT                           optional: cap route*date pairs (testing)
  DRY_RUN=1                            optional: print instead of sending
"""

import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import providers
from providers import Fare  # noqa: F401

ROOT = Path(__file__).parent
IST = timezone(timedelta(hours=5, minutes=30))

HISTORY_CAP = 400          # samples kept per route+bucket
HISTORY_MAX_AGE_DAYS = 45  # baseline window


def load_env_file():
    """Tiny .env loader for local runs (no python-dotenv dependency)."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def bucket_for(days_ahead):
    if days_ahead <= 6:
        return "0-6"
    if days_ahead <= 13:
        return "7-13"
    if days_ahead <= 29:
        return "14-29"
    if days_ahead <= 59:
        return "30-59"
    return "60-92"


def dates_for_run(config, run_hour):
    """Rotating slice of the scan window: the full window is covered every
    `slices` runs (12 => twice a day on hourly cron), keeping each run short."""
    w = config.get("scan_window", {})
    start = w.get("start_days_ahead", 2)
    end = w.get("end_days_ahead", 92)
    slices = max(1, w.get("slices", 12))
    return [d for d in range(start, end + 1) if d % slices == run_hour % slices]


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def fmt_inr(n):
    return f"₹{n:,}"


def fmt_when(date_str):
    """'2026-09-24' -> '24 - Sep - 2026 (WED)'."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.strftime('%d - %b - %Y')} ({d.strftime('%a').upper()})"


def fare_meta(f):
    """'🛫 6:25 AM · ✈ 4h 5m · layover 1h 20m' from whatever the source gave."""
    parts = []
    if f.dep_time:
        parts.append(f"\U0001f6eb {f.dep_time}")
    if f.duration:
        parts.append(f"✈ {f.duration}")
    if f.stops > 0:
        parts.append(f"layover {f.layover}" if f.layover and f.layover != "—"
                     else "layover incl. in total")
    return " · ".join(parts)


def tg_send(text):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_ids = [c.strip() for c in os.environ.get("TG_CHAT_IDS", "").split(",") if c.strip()]
    if os.environ.get("DRY_RUN") == "1" or not token or not chat_ids:
        print("---- TELEGRAM (not sent) ----")
        print(text)
        print("-----------------------------")
        return
    for cid in chat_ids:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": cid, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=30,
            )
            if r.status_code != 200:
                print(f"telegram send failed ({cid}): {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"telegram send failed ({cid}): {e}")


def main():
    load_env_file()
    config = load_json(ROOT / "config.json", None)
    if not config:
        sys.exit("config.json missing or invalid")
    history = load_json(ROOT / "price_history.json", {})
    history.setdefault("routes", {})
    history.setdefault("alerts", {})

    now = datetime.now(IST)
    today = now.date()
    usd_inr = providers.get_usd_inr(history)
    deal_cfg = config["deal"]

    use_amadeus = bool(os.environ.get("AMADEUS_CLIENT_ID"))
    am_cfg = config.get("amadeus", {})
    amadeus_this_run = use_amadeus and (now.hour % max(1, am_cfg.get("every_n_hours", 8)) == 0)
    use_aviasales = bool(os.environ.get("TRAVELPAYOUTS_TOKEN"))

    scan_limit = int(os.environ.get("SCAN_LIMIT", "0"))
    pairs_scanned = 0
    all_best = {}    # (route, date) -> best Fare across sources
    deals = []
    google_levels = {}

    for route in config["routes"]:
        origin, dest = route["from"], route["to"]
        rkey = f"{origin}-{dest}"
        max_stops = route.get("max_stops", 1)
        dates_ahead = dates_for_run(config, now.hour)
        if amadeus_this_run and am_cfg.get("priority_only") and not route.get("priority"):
            am_dates = []
        else:
            am_dates = dates_ahead[: am_cfg.get("max_dates", 6)]

        for da in dates_ahead:
            if scan_limit and pairs_scanned >= scan_limit:
                break
            pairs_scanned += 1
            date = (today + timedelta(days=da)).isoformat()

            fares = providers.fetch_google(origin, dest, date, max_stops, usd_inr)
            if amadeus_this_run and da in am_dates:
                fares += providers.fetch_amadeus(origin, dest, date, max_stops)
            if use_aviasales:
                fares += providers.fetch_aviasales(origin, dest, date, max_stops)
            time.sleep(1.0 + random.random())

            if not fares:
                continue
            best = min(fares, key=lambda f: f.price_inr)
            all_best[(rkey, date)] = best
            if best.google_price_level:
                google_levels[(rkey, date)] = best.google_price_level

            # -- update rolling baseline
            bkey = bucket_for(da)
            rh = history["routes"].setdefault(rkey, {})
            samples = rh.setdefault(bkey, [])
            samples.append([int(time.time()), best.price_inr])
            cutoff = time.time() - HISTORY_MAX_AGE_DAYS * 86400
            samples[:] = [s for s in samples if s[0] >= cutoff][-HISTORY_CAP:]

            # -- steal detection
            prices = [s[1] for s in samples]
            median = statistics.median(prices) if prices else None
            enough = len(prices) >= deal_cfg["min_samples_for_baseline"]
            is_deal, why = False, ""
            abs_thr = route.get("steal_below_inr")
            if abs_thr and best.price_inr <= abs_thr:
                is_deal, why = True, f"below steal threshold {fmt_inr(abs_thr)}"
            elif enough and best.price_inr <= deal_cfg["ratio_vs_median"] * median:
                is_deal, why = True, f"{round(100 - 100 * best.price_inr / median)}% below usual ({fmt_inr(int(median))})"
            elif (enough and best.google_price_level == "low"
                  and best.price_inr <= deal_cfg["google_low_ratio"] * median):
                is_deal, why = True, f"Google says LOW, {round(100 - 100 * best.price_inr / median)}% below usual"

            if is_deal:
                akey = f"{rkey}|{date}"
                prev = history["alerts"].get(akey)
                fresh_enough = (
                    not prev
                    or time.time() - prev["ts"] > deal_cfg["realert_hours"] * 3600
                    or best.price_inr <= prev["price"] * (1 - deal_cfg["realert_drop_pct"] / 100)
                )
                if fresh_enough:
                    deals.append((route, best, why))
                    history["alerts"][akey] = {"price": best.price_inr, "ts": int(time.time())}

    # prune stale alert-dedupe entries
    history["alerts"] = {k: v for k, v in history["alerts"].items()
                         if time.time() - v["ts"] < 7 * 86400}

    # ---------------------------------------------------------- messaging
    stamp = now.strftime("%d %b, %H:%M IST")
    sources = ["Google Flights"] + (["Amadeus"] if amadeus_this_run else []) \
        + (["Aviasales"] if use_aviasales else [])

    # Lakshadweep watch: always show best AGX fare seen this run
    agx_lines = []
    for (rkey, date), f in sorted(all_best.items(), key=lambda kv: kv[1].price_inr):
        if "AGX" in rkey:
            stops_txt = "direct" if f.stops == 0 else f"{f.stops} stop"
            meta = fare_meta(f)
            detail = " · ".join(x for x in
                                [f"<b>{fmt_inr(f.price_inr)}</b>", stops_txt,
                                 f.airline.strip(), f.source] if x)
            agx_lines.append(f"  {rkey} {fmt_when(date)}\n    {detail}"
                             + (f"\n    {meta}" if meta else ""))
    agx_block = "\n".join(agx_lines[:4]) if agx_lines else "  no AGX fares returned this run"

    if deals:
        origin_rank = config.get("origin_rank", {})
        deals.sort(key=lambda d: (not d[0].get("priority", False),
                                  origin_rank.get(d[0]["from"], 9),
                                  d[1].price_inr))
        lines = [f"\U0001f525 <b>STEAL DEALS</b> — {stamp}\n"]
        for route, f, why in deals[:12]:
            stops_txt = "direct" if f.stops == 0 else f"{f.stops} stop"
            tag = " \U0001f3dd️" if route.get("priority") else ""
            link = providers.google_flights_link(route["from"], route["to"], f.date)
            meta = fare_meta(f)
            detail = " · ".join(x for x in
                                [f"<b>{fmt_inr(f.price_inr)}</b>", stops_txt,
                                 f.airline.strip(), f"via {f.source}"] if x)
            lines.append(
                f"{tag}<b>{f.route}</b> {fmt_when(f.date)}\n  {detail}\n"
                + (f"  {meta}\n" if meta else "")
                + f"  {why} — <a href=\"{link}\">book</a>\n")
        tg_send("\n".join(lines))
    else:
        hb = config.get("heartbeat", {})
        quiet = now.hour in hb.get("quiet_hours_ist", [])
        if hb.get("enabled", True) and not quiet:
            cheapest = sorted(all_best.values(), key=lambda f: f.price_inr)[:3]
            cheap_txt = "\n".join(
                f"  {f.route} {fmt_when(f.date)}: {fmt_inr(f.price_inr)} ({f.source})"
                for f in cheapest
            ) or "  (no fares returned — check logs)"
            tg_send(
                f"✅ <b>No steal deals</b> — {stamp}\n"
                f"Scanned {pairs_scanned} route-dates via {', '.join(sources)} "
                f"(rotating slice of next 90 days).\n\n"
                f"\U0001f3dd️ Lakshadweep watch:\n{agx_block}\n\n"
                f"Cheapest overall:\n{cheap_txt}"
            )

    (ROOT / "price_history.json").write_text(
        json.dumps(history, separators=(",", ":")), encoding="utf-8")
    print(f"done: {pairs_scanned} pairs, {len(all_best)} priced, {len(deals)} deals")


if __name__ == "__main__":
    main()

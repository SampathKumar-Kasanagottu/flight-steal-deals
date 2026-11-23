"""Date-window fare watches: the cheapest fare for EVERY day of a fixed
window, in each direction — e.g. Hyderabad <-> Goa for the whole of January.

Configured under "watches" in config.json. A direction can span several
airport pairs (Goa = GOI Dabolim + GOX Mopa); the day's fare is the cheapest
across them. Each run refreshes a rotating slice of the window plus any day
it holds no fare for yet, and keeps the latest fare per day in
price_history.json["watch"], so every message covers the whole window.

Output:
  * block()     short "cheapest days" summary for the hourly message
  * calendars() the full day-by-day list, sent at calendar_hours_ist
  * scan()      also returns fares at/below a direction's steal_below_inr
"""

import html
import random
import re
import time
from datetime import datetime, timedelta

import providers

STALE_HOURS = 8   # a day's fare not re-confirmed for this long is dropped
MAX_VISIBLE = 3500  # visible characters of day lines per Telegram message
_TAG = re.compile(r"<[^>]+>")

AIRLINE_SHORT = {"Air India Express": "AI Express", "Akasa Air": "Akasa",
                 "Alliance Air": "Alliance"}


def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def _inr(n):
    return f"₹{n:,}"


def _clock(dep):
    """'6:25 AM' -> minutes after midnight (for sorting)."""
    try:
        t = datetime.strptime(dep.strip(), "%I:%M %p")
        return t.hour * 60 + t.minute
    except ValueError:
        return 0


def _days(w, today):
    """Window days still ahead of today (empty once the window has passed)."""
    start = max(_d(w["start_date"]), today + timedelta(days=1))
    end = _d(w["end_date"])
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def active(w, today):
    return bool(_days(w, today))


def _store(history, w):
    return history.setdefault("watch", {}).setdefault(w["id"], {"fares": {}})


def scan(w, history, now, usd_inr, deal_cfg):
    """Refresh this run's share of the window. Returns (queries_made, steals)
    where steals is [(route_dict, Fare, why)] ready for the deals message."""
    today = now.date()
    days = _days(w, today)
    fares = _store(history, w)["fares"]
    max_stops = w.get("max_stops", 0)
    slices = max(1, w.get("refresh_slices", 2))

    # forget days that have passed, and fares nobody has re-confirmed lately
    keep = {d.isoformat() for d in days}
    cutoff = time.time() - STALE_HOURS * 3600
    for k in [k for k, v in fares.items()
              if k.split("|")[1] not in keep or v["ts"] < cutoff]:
        del fares[k]

    queries, steals = 0, []
    for direction in w["directions"]:
        thr = direction.get("steal_below_inr")
        for origin, dest in direction["pairs"]:
            for d in days:
                key = f"{origin}-{dest}|{d.isoformat()}"
                due = (d.toordinal() + now.hour) % slices == 0
                if key in fares and not due:
                    continue
                queries += 1
                got = providers.fetch_google(origin, dest, d.isoformat(),
                                             max_stops, usd_inr, limit=None)
                time.sleep(1.0 + random.random())
                if not got:
                    continue  # keep the last known fare until it goes stale
                best = min(got, key=lambda f: f.price_inr)
                # every flight at the day's lowest price: [[dep, airline], ...]
                flights = sorted({(f.dep_time, f.airline.strip()) for f in got
                                  if f.price_inr == best.price_inr and f.dep_time},
                                 key=lambda x: _clock(x[0]))
                prev = fares.get(key)
                entry = {"ts": int(time.time()), "p": best.price_inr,
                         "fl": [list(x) for x in flights]}
                if not flights and prev and prev["p"] == entry["p"]:
                    # same fare, but this page came without flight details
                    entry["fl"] = prev.get("fl", [])
                fares[key] = entry

                # same alert key + cooldown as the regular route scan, so a
                # fare both of them catch is announced once
                if thr and best.price_inr <= thr:
                    seen = history["alerts"].get(key)
                    if (not seen
                            or time.time() - seen["ts"] > deal_cfg["realert_hours"] * 3600
                            or best.price_inr <= seen["price"]
                            * (1 - deal_cfg["realert_drop_pct"] / 100)):
                        steals.append(({"from": origin, "to": dest}, best,
                                       f"{w.get('short_name', w['name'])}: "
                                       f"below {_inr(thr)}"))
                        history["alerts"][key] = {"price": best.price_inr,
                                                  "ts": int(time.time())}
    return queries, steals


def _cheapest_per_day(w, history, direction, today):
    """[(date, pair 'HYD-GOX', entry)] — cheapest across the direction's
    airport pairs, one row per day that has a fare."""
    fares = _store(history, w)["fares"]
    rows = []
    for d in _days(w, today):
        opts = [(fares[k], f"{o}-{dst}") for o, dst in direction["pairs"]
                for k in [f"{o}-{dst}|{d.isoformat()}"] if k in fares]
        if opts:
            # same price at both airports: show the one with more flights
            entry, pair = min(opts, key=lambda x: (x[0]["p"], -len(x[0].get("fl", []))))
            rows.append((d, pair, entry))
    return rows


def _which_airport(pair, direction):
    """The airport that varies within a direction (GOI vs GOX), or ''."""
    origins = {o for o, _ in direction["pairs"]}
    dests = {x for _, x in direction["pairs"]}
    o, dst = pair.split("-")
    if len(dests) > 1:
        return dst
    if len(origins) > 1:
        return o
    return ""


def _flights_txt(entry, max_times=3):
    """'IndiGo 7:15 AM, 8:05 PM' — the flights at the day's lowest price,
    grouped by airline; '+2' when there are more than max_times."""
    flights = entry.get("fl", [])
    shown, extra = flights[:max_times], len(flights) - max_times
    by_airline = {}
    for dep, airline in shown:
        by_airline.setdefault(AIRLINE_SHORT.get(airline, airline), []).append(dep)
    txt = ", ".join(f"{html.escape(a)} {', '.join(deps)}".strip()
                    for a, deps in by_airline.items())
    return txt + (f" +{extra}" if extra > 0 else "")


def _one_month(w):
    a, b = _d(w["start_date"]), _d(w["end_date"])
    return (a.year, a.month) == (b.year, b.month)


def _day_runs(days, one_month):
    """Sorted dates -> '1–13, 14 Thu, 16–21': runs of 3+ consecutive days
    collapse to a range, the rest keep their weekday."""
    def label(d, weekday):
        if one_month:
            return f"{d.day} {d.strftime('%a')}" if weekday else str(d.day)
        return f"{d.day} {d.strftime('%b')}"

    out, i = [], 0
    while i < len(days):
        j = i
        while j + 1 < len(days) and (days[j + 1] - days[j]).days == 1:
            j += 1
        if j - i >= 2:
            out.append(f"{label(days[i], False)}–{label(days[j], False)}")
        else:
            out += [label(d, True) for d in days[i:j + 1]]
        i = j + 1
    return ", ".join(out)


def block(w, history, now):
    """Hourly summary: the lowest price levels and the days that have them."""
    today = now.date()
    levels_n = w.get("summary_levels", 3)
    lines = [f"\U0001f3d6️ <b>{html.escape(w['name'])}</b> — cheapest days"]
    any_rows = False
    for direction in w["directions"]:
        rows = _cheapest_per_day(w, history, direction, today)
        lines.append(f"<b>{html.escape(direction['label'])}</b>")
        if not rows:
            lines.append("  no fares yet")
            continue
        any_rows = True
        for price in sorted({e["p"] for _, _, e in rows})[:levels_n]:
            days = [d for d, _, e in rows if e["p"] == price]
            count = f" ({len(days)} days)" if len(days) > 3 else ""
            lines.append(f"  <b>{_inr(price)}</b> — "
                         f"{_day_runs(days, _one_month(w))}{count}")
    if not any_rows:
        return ""
    return "\n".join(lines)


def _calendar_due(w, store, now):
    """True when a scheduled send time (calendar_hours_ist) has come up since
    the last calendar went out — so a missed run catches up on the next one."""
    hours = sorted(w.get("calendar_hours_ist", [8, 20]))
    if not hours:
        return False
    slots = [now.replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(days=dd)
             for dd in (-1, 0) for h in hours]
    latest = max(s for s in slots if s <= now)
    return store.get("last_calendar_ts", 0) < latest.timestamp()


def calendars(w, history, now, force=False):
    """Full day-by-day messages (one per direction), or [] when not due."""
    today = now.date()
    store = _store(history, w)
    if not force and not _calendar_due(w, store, now):
        return []
    top_n = w.get("top_n", 5)
    one_month = _one_month(w)
    stamp = now.strftime("%d %b, %H:%M IST")
    stops_txt = "non-stop" if w.get("max_stops", 0) == 0 else "cheapest"
    msgs = []
    for direction in w["directions"]:
        rows = _cheapest_per_day(w, history, direction, today)
        if not rows:
            continue
        # the top_n cheapest days, plus any day tied with the last of them
        ranked = sorted(e["p"] for _, _, e in rows)
        cut = ranked[min(top_n, len(ranked)) - 1]
        lo, hi = ranked[0], ranked[-1]
        title = (f"\U0001f3d6️ <b>{html.escape(direction['label'])} · "
                 f"{html.escape(w.get('window_label', w['name']))}</b>")
        head = [
            title,
            f"lowest {stops_txt} fare of each day, and the flights at that "
            f"price · {stamp}",
            f"lowest <b>{_inr(lo)}</b> · highest {_inr(hi)} · "
            f"\U0001f7e2 = cheapest days",
            "",
        ]
        lines = []
        for d, pair, e in rows:
            o, dst = pair.split("-")
            link = providers.google_flights_link(o, dst, d.isoformat())
            day = d.strftime("%a %d") if one_month else d.strftime("%a %d %b")
            cheap = e["p"] <= cut
            price = f"<b>{_inr(e['p'])}</b>" if cheap else _inr(e["p"])
            parts = [f"<a href=\"{html.escape(link)}\">{day}</a>", price]
            ap = _which_airport(pair, direction)
            if ap:
                parts.append(ap)
            flights = _flights_txt(e)
            if flights:
                parts.append(flights)
            lines.append(("\U0001f7e2 " if cheap else "▫️ ") + " · ".join(parts))
        foot = []
        missing = len(_days(w, today)) - len(rows)
        if missing:
            foot.append(f"\n<i>{missing} day(s) not priced yet</i>")
        foot.append("\n<i>tap a day to open it in Google Flights</i>")
        # Telegram allows 4096 visible characters per message: a long window
        # goes out in parts
        parts, part, size = [], [], 0
        for ln in lines:
            n = len(_TAG.sub("", ln)) + 1
            if part and size + n > MAX_VISIBLE:
                parts.append(part)
                part, size = [], 0
            part.append(ln)
            size += n
        parts.append(part)
        for i, part in enumerate(parts):
            top = head if i == 0 else [f"{title} (cont.)", ""]
            msgs.append("\n".join(top + part + (foot if i == len(parts) - 1 else [])))
    if msgs:
        store["last_calendar_ts"] = int(now.timestamp())
    return msgs

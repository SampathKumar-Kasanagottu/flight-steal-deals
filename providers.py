"""Fare providers. Each returns a list of Fare tuples and must never raise —
a broken source should not kill the whole scan."""

import json
import os
import re
import time
import types
import urllib.parse
from dataclasses import dataclass

import requests

USD_INR_FALLBACK = 84.0


@dataclass
class Fare:
    route: str          # "HYD-AGX"
    date: str           # "2026-09-20"
    price_inr: int
    stops: int
    airline: str
    source: str         # "google" | "amadeus" | "aviasales"
    google_price_level: str = ""  # "low"/"typical"/"high" (google only)
    dep_time: str = ""  # "6:25 AM"
    duration: str = ""  # total journey time, "4h 5m"
    layover: str = ""   # "—" direct, "1h 20m" (amadeus), "" unknown


def _short_dur(s):
    return (str(s).replace(" hr ", "h ").replace(" hr", "h")
            .replace(" min", "m").strip())


def _hm(seconds):
    h, m = int(seconds // 3600), int(seconds % 3600 // 60)
    return f"{h}h {m}m" if h else f"{m}m"


def _parse_price(text, usd_inr):
    """'₹4,320' / 'INR 4320' / '$52' -> int INR (or None)."""
    if not text:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)", str(text))
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    t = str(text)
    if "₹" in t or "INR" in t.upper():
        return int(round(val))
    if "$" in t or "USD" in t.upper():
        return int(round(val * usd_inr))
    if "€" in t or "EUR" in t.upper():
        return int(round(val * usd_inr * 1.08))
    # unknown currency: assume INR if the number is large, USD if small
    return int(round(val if val > 900 else val * usd_inr))


def get_usd_inr(cache):
    """Cached (12h) free FX rate, no API key."""
    now = time.time()
    c = cache.get("fx_usd_inr") or {}
    if c.get("rate") and now - c.get("ts", 0) < 12 * 3600:
        return c["rate"]
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=20)
        rate = float(r.json()["rates"]["INR"])
        cache["fx_usd_inr"] = {"rate": rate, "ts": now}
        return rate
    except Exception:
        return c.get("rate") or USD_INR_FALLBACK


# ---------------------------------------------------------------- google
_GOOGLE_URL = "https://www.google.com/travel/flights"
# per-run counters, printed in the Actions log by google_stats()
_gstats = {"html": 0, "data": 0, "empty": 0, "errors": 0, "retried": 0,
           "failed": 0, "stock": 0, "logged": False, "client_ok": True}
_DS1 = re.compile(r"AF_initDataCallback\(\{key: 'ds:1', hash: '\d+', "
                  r"data:(.*?), sideChannel: \{\}\}\);</script>", re.S)
# Google's backend failing one request: the same page, but the flight data
# block is a travel.frontend.flights.ErrorResponse (status 13 = internal).
# From GitHub's runners 1 request in 4-8 gets this; the next one usually works.
_DS1_ERROR = re.compile(r"AF_initDataCallback\(\{key: 'ds:1',[^<]*?errorHasStatus: true")


# browsers to present as, in order, until a page carries the flights (the
# error pages above come per request, so each retry has its own odds)
_PROFILES = [("chrome_146", "windows"), ("chrome_146", "android"),
             ("chrome_146", "linux")]


class _NoResults(Exception):
    """No attempt returned flight data: Google error pages every time, or a
    page this code can't read."""


def _clock_txt(hm):
    """[15] / [7, 5] / [None, 30] -> '3:00 PM' / '7:05 AM' / '12:30 AM'."""
    h = (hm[0] if hm else 0) or 0
    m = (hm[1] if hm and len(hm) > 1 else 0) or 0
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _js_flights(page, origin, dest):
    """Flights from the page's embedded data block (ds:1).

    Returns a fast-flights-like Result, an empty Result when Google answered
    "no flights", or None when the block is missing or doesn't look like
    this search (Google changed the layout) — so a misread can never become
    a wrong price."""
    m = _DS1.search(page)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return None
    try:
        flights, other_route = [], 0
        for block in (d[2], d[3]):  # "best" flights, then all other flights
            for item in ((block or [None])[0] or []):
                info, price_box = item[0], item[1]
                price = (price_box[0][1] if price_box and price_box[0]
                         and len(price_box[0]) > 1 else None)
                if not isinstance(price, int) or price <= 0:
                    continue  # "price unavailable"
                if info[3] != origin or info[6] != dest:
                    other_route += 1  # e.g. a nearby airport
                    continue
                legs = info[2] or []
                mins = info[9] if isinstance(info[9], int) else 0
                flights.append(types.SimpleNamespace(
                    is_best=block is d[2],
                    name=", ".join(info[1] or []) or str(info[0] or "?"),
                    departure=_clock_txt(info[5]),
                    duration=f"{mins // 60} hr {mins % 60} min" if mins else "",
                    stops=max(0, len(legs) - 1),
                    price=f"₹{price}",
                ))
        if other_route and not flights:
            return None  # nothing matches this search: the layout has moved
        level = ""
        ins = d[5] if len(d) > 5 and isinstance(d[5], list) else None
        if flights and ins and len(ins) > 5:
            lo = ins[4][1] if ins[4] and len(ins[4]) > 1 else None
            hi = ins[5][1] if ins[5] and len(ins[5]) > 1 else None
            cheapest = min(int(f.price[1:]) for f in flights)
            if isinstance(lo, int) and isinstance(hi, int):
                level = "low" if cheapest < lo else "high" if cheapest > hi else "typical"
    except (IndexError, TypeError, KeyError, ValueError):
        return None
    return types.SimpleNamespace(current_price=level, flights=flights)


def _google_query(origin, dest, date, max_stops):
    """One Google Flights search -> fast-flights-like Result.

    Fetched as desktop Chrome with INR asked for (fast-flights' own fetch
    asks primp for a profile it no longer has, gets a random one, and from
    GitHub's US runners Google then prices in whole dollars). Flights are
    read from the data block embedded in the page, else from the HTML list.
    A Google error page is retried as the next browser in _PROFILES.
    """
    from fast_flights import FlightData, Passengers, get_flights
    kwargs = dict(
        flight_data=[FlightData(date=date, from_airport=origin, to_airport=dest)],
        trip="one-way",
        seat="economy",
        passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
    )
    if max_stops is not None:
        kwargs["max_stops"] = max_stops
    # "fallback" retries via a hosted service that now 401s; stick to
    # direct fetch and treat a no-results page as an empty answer.
    mode = os.environ.get("FF_FETCH_MODE", "common")

    if _gstats["client_ok"] and mode == "common":
        try:
            import primp
            from fast_flights.core import parse_response
            from fast_flights.filter import TFSData
            tfs = TFSData.from_interface(**kwargs).as_b64().decode("utf-8")
            client = primp.Client(impersonate=_PROFILES[0][0],
                                  impersonate_os=_PROFILES[0][1], verify=False)
        except Exception as e:
            _gstats["client_ok"] = False
            print(f"  google: own fetch unavailable ({type(e).__name__}: "
                  f"{str(e)[:100]}) — using fast-flights' fetch")
        else:
            for n, (browser, os_) in enumerate(_PROFILES):
                if n:
                    _gstats["retried"] += 1
                    time.sleep(1.5)
                    # a fresh client per attempt: which page Google serves
                    # varies per request, so a retry has its own odds
                    client = primp.Client(impersonate=browser, impersonate_os=os_,
                                          verify=False)
                res = client.get(_GOOGLE_URL, params={
                    "tfs": tfs, "hl": "en", "tfu": "EgQIABABIgA", "curr": "INR"})
                if res.status_code != 200:
                    raise ConnectionError(f"HTTP {res.status_code}")
                page = res.text
                if "Verifying your request" in page[:20000]:
                    raise RuntimeError("No flights found: Verifying your request")
                if _DS1_ERROR.search(page):
                    _gstats["errors"] += 1
                    continue  # Google's side failed this request: ask again
                # the data block first: it carries times + airline on every
                # page type, and matched the HTML list exactly where both exist
                result = _js_flights(page, origin, dest)
                if result is not None:
                    _gstats["data" if result.flights else "empty"] += 1
                    if not result.flights:
                        raise RuntimeError("No flights found")
                    return result
                try:
                    result = parse_response(res)
                    _gstats["html"] += 1
                    return result
                except RuntimeError:
                    pass
                # neither data nor list: ask again, as the next browser
            _gstats["failed"] += 1
            raise _NoResults(f"no flight data after {len(_PROFILES)} tries")
    _gstats["stock"] += 1
    return get_flights(fetch_mode=mode, **kwargs)


def google_stats():
    s = _gstats
    return (f"google: {s['data']} searches read from page data, {s['html']} from "
            f"the HTML list, {s['empty']} with no flights, {s['failed']} unanswered "
            f"after {len(_PROFILES)} tries | {s['errors']} Google error pages, "
            f"{s['retried']} retries, {s['stock']} via fast-flights' fetch")


def fetch_google(origin, dest, date, max_stops, usd_inr, limit=10):
    """Google Flights via fast-flights (no API key). Google already
    aggregates airline sites + most OTAs, so this is the widest net.
    `limit` caps the fares returned (None = every flight on the page)."""
    try:
        import fast_flights  # noqa: F401
    except ImportError:
        return []
    result = None
    for attempt in (1, 2):
        try:
            result = _google_query(origin, dest, date, max_stops)
            break
        except RuntimeError as e:
            msg = str(e)
            # Google's "Verifying your request..." interstitial is not an
            # empty answer — it usually clears on a second try
            if "Verifying your request" in msg and attempt == 1:
                time.sleep(4)
                continue
            if "Verifying your request" in msg:
                print(f"  google {origin}-{dest} {date}: blocked by verification page")
            elif "No flights found" in msg:
                print(f"  google {origin}-{dest} {date}: no itineraries")
            else:
                print(f"  google {origin}-{dest} {date}: RuntimeError: {msg[:120]}")
            return []
        except _NoResults as e:
            print(f"  google {origin}-{dest} {date}: {e}")
            return []
        except Exception as e:
            if attempt == 1:
                time.sleep(3)  # one retry for transient network errors
                continue
            print(f"  google {origin}-{dest} {date}: {type(e).__name__}: {str(e)[:120]}")
            return []

    level = getattr(result, "current_price", "") or ""
    fares, seen = [], set()
    for f in getattr(result, "flights", []):
        raw_price = getattr(f, "price", "")
        price = _parse_price(raw_price, usd_inr)
        if not price:
            continue
        if not _gstats["logged"]:
            # one sample per run so the Actions log shows the currency in use
            _gstats["logged"] = True
            # (ascii(): the rupee sign prints as ₹, safe on any console)
            print(f"  google sample {origin}-{dest} {date}: price text "
                  f"{ascii(raw_price)}, departure {str(getattr(f, 'departure', ''))!r}")
        stops = getattr(f, "stops", 0)
        try:
            stops = int(stops)
        except (TypeError, ValueError):
            stops = 0
        if max_stops is not None and stops > max_stops:
            continue
        dep_raw = str(getattr(f, "departure", ""))
        fare = Fare(
            route=f"{origin}-{dest}", date=date, price_inr=price, stops=stops,
            airline=str(getattr(f, "name", "?")), source="google",
            google_price_level=level,
            dep_time=dep_raw.split(" on ")[0].strip(),
            duration=_short_dur(getattr(f, "duration", "")),
            layover="—" if stops == 0 else "",  # google only reports totals
        )
        # the page lists its "best" flights a second time among the rest
        ident = (fare.price_inr, fare.dep_time, fare.airline, fare.stops)
        if fare.dep_time and ident in seen:
            continue
        seen.add(ident)
        fares.append(fare)
    return fares if limit is None else fares[:limit]


# --------------------------------------------------------------- amadeus
_amadeus_token = {"value": None, "exp": 0}


def _amadeus_auth():
    cid = os.environ.get("AMADEUS_CLIENT_ID")
    sec = os.environ.get("AMADEUS_CLIENT_SECRET")
    if not cid or not sec:
        return None
    if _amadeus_token["value"] and time.time() < _amadeus_token["exp"] - 60:
        return _amadeus_token["value"]
    r = requests.post(
        "https://test.api.amadeus.com/v1/security/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    _amadeus_token["value"] = j["access_token"]
    _amadeus_token["exp"] = time.time() + int(j.get("expires_in", 1700))
    return _amadeus_token["value"]


def fetch_amadeus(origin, dest, date, max_stops):
    """Amadeus Self-Service (free tier). Real GDS fares in INR."""
    try:
        token = _amadeus_auth()
        if not token:
            return []
        r = requests.get(
            "https://test.api.amadeus.com/v2/shopping/flight-offers",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "originLocationCode": origin, "destinationLocationCode": dest,
                "departureDate": date, "adults": 1, "currencyCode": "INR",
                "nonStop": "true" if max_stops == 0 else "false", "max": 5,
            },
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  amadeus {origin}-{dest} {date}: HTTP {r.status_code}")
            return []
        fares = []
        for offer in r.json().get("data", []):
            try:
                price = int(round(float(offer["price"]["grandTotal"])))
                itin = offer["itineraries"][0]
                segs = itin["segments"]
                stops = len(segs) - 1
                if max_stops is not None and stops > max_stops:
                    continue
                from datetime import datetime as _dt
                dep = _dt.fromisoformat(segs[0]["departure"]["at"])
                gap = sum(
                    (_dt.fromisoformat(b["departure"]["at"])
                     - _dt.fromisoformat(a["arrival"]["at"])).total_seconds()
                    for a, b in zip(segs, segs[1:]))
                m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", itin.get("duration", ""))
                dur = _hm((int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60)) if m else ""
                fares.append(Fare(
                    route=f"{origin}-{dest}", date=date, price_inr=price, stops=stops,
                    airline=segs[0].get("carrierCode", "?"), source="amadeus",
                    dep_time=dep.strftime("%I:%M %p").lstrip("0"),
                    duration=dur,
                    layover="—" if stops == 0 else _hm(gap),
                ))
            except (KeyError, IndexError, ValueError):
                continue
        return fares
    except Exception as e:
        print(f"  amadeus {origin}-{dest} {date}: {type(e).__name__}: {e}")
        return []


# -------------------------------------------------------------- aviasales
def fetch_aviasales(origin, dest, date, max_stops):
    """Travelpayouts/Aviasales cached-price API (free token). Prices are
    cached from recent user searches across OTAs — great extra signal."""
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        return []
    try:
        r = requests.get(
            "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
            params={
                "origin": origin, "destination": dest, "departure_at": date,
                "one_way": "true", "currency": "inr", "limit": 10,
                "sorting": "price", "token": token,
            },
            timeout=30,
        )
        if r.status_code != 200:
            return []
        fares = []
        for d in r.json().get("data", []):
            stops = int(d.get("transfers", 0))
            if max_stops is not None and stops > max_stops:
                continue
            dep_time = ""
            try:
                from datetime import datetime as _dt
                dep_time = _dt.fromisoformat(d["departure_at"]).strftime("%I:%M %p").lstrip("0")
            except (KeyError, ValueError):
                pass
            dur_min = int(d.get("duration", 0) or 0)
            fares.append(Fare(
                route=f"{origin}-{dest}", date=date,
                price_inr=int(round(float(d["price"]))), stops=stops,
                airline=str(d.get("airline", "?")), source="aviasales",
                dep_time=dep_time,
                duration=_hm(dur_min * 60) if dur_min else "",
                layover="—" if stops == 0 else "",
            ))
        return fares
    except Exception as e:
        print(f"  aviasales {origin}-{dest} {date}: {type(e).__name__}: {e}")
        return []


def google_flights_link(origin, dest, date):
    q = urllib.parse.quote(f"Flights from {origin} to {dest} on {date}")
    return f"https://www.google.com/travel/flights?q={q}&curr=INR"

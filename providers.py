"""Fare providers. Each returns a list of Fare tuples and must never raise —
a broken source should not kill the whole scan."""

import json
import os
import re
import time
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
def fetch_google(origin, dest, date, max_stops, usd_inr):
    """Google Flights via fast-flights (no API key). Google already
    aggregates airline sites + most OTAs, so this is the widest net."""
    try:
        from fast_flights import FlightData, Passengers, get_flights
    except ImportError:
        return []
    try:
        kwargs = dict(
            flight_data=[FlightData(date=date, from_airport=origin, to_airport=dest)],
            trip="one-way",
            seat="economy",
            passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
            # "fallback" retries via a hosted service that now 401s; stick to
            # direct fetch and treat a no-results page as an empty answer.
            fetch_mode=os.environ.get("FF_FETCH_MODE", "common"),
        )
        if max_stops is not None:
            kwargs["max_stops"] = max_stops
        try:
            result = get_flights(**kwargs)
        except RuntimeError:
            raise
        except Exception:
            time.sleep(3)  # one retry for transient network errors
            result = get_flights(**kwargs)
    except RuntimeError as e:
        if "No flights found" in str(e):
            print(f"  google {origin}-{dest} {date}: no itineraries")
        else:
            print(f"  google {origin}-{dest} {date}: RuntimeError: {str(e)[:120]}")
        return []
    except Exception as e:
        print(f"  google {origin}-{dest} {date}: {type(e).__name__}: {str(e)[:120]}")
        return []

    level = getattr(result, "current_price", "") or ""
    fares = []
    for f in getattr(result, "flights", [])[:10]:
        price = _parse_price(getattr(f, "price", ""), usd_inr)
        if not price:
            continue
        stops = getattr(f, "stops", 0)
        try:
            stops = int(stops)
        except (TypeError, ValueError):
            stops = 0
        if max_stops is not None and stops > max_stops:
            continue
        dep_raw = str(getattr(f, "departure", ""))
        fares.append(Fare(
            route=f"{origin}-{dest}", date=date, price_inr=price, stops=stops,
            airline=str(getattr(f, "name", "?")), source="google",
            google_price_level=level,
            dep_time=dep_raw.split(" on ")[0].strip(),
            duration=_short_dur(getattr(f, "duration", "")),
            layover="—" if stops == 0 else "",  # google only reports totals
        ))
    return fares


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

"""Live aircraft (OpenSky or a local dump1090/readsb feed) joined to the FAA registry."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..http import SourceError
from . import csl, faa
from .base import Ctx

OPENSKY = "https://opensky-network.org/api/states/all"
GEOCODER = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
EMERGENCY = {"7500": "hijack", "7600": "radio failure", "7700": "emergency"}
CORPORATE = {"Corporation", "LLC", "Non-Citizen Corporation", "Partnership"}
_AIRLINE_CALLSIGN = re.compile(r"^[A-Z]{3}\d")


@dataclass
class Plane:
    hex: str
    callsign: str
    lat: float | None
    lon: float | None
    alt_ft: int | None
    speed_kt: int | None
    heading: int | None
    squawk: str | None
    on_ground: bool
    reg: dict | None = None  # FAA registry row
    flags: list[str] = field(default_factory=list)

    @property
    def tail(self) -> str:
        return (self.reg or {}).get("n_number") or ""

    @property
    def owner(self) -> str:
        return (self.reg or {}).get("name") or ""

    @property
    def model(self) -> str:
        r = self.reg or {}
        return " ".join(x for x in ((r.get("mfr") or "").strip(), (r.get("model") or "").strip()) if x)


def place(ctx: Ctx, where: str) -> tuple[float, float]:
    """'lat,lon' or a US street address (Census geocoder)."""
    if m := re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", where):
        return float(m.group(1)), float(m.group(2))
    data = ctx.http.get(GEOCODER, params={"address": where, "benchmark": "Public_AR_Current", "format": "json"},
                        ttl=30 * 86400)
    matches = data.get("result", {}).get("addressMatches", [])
    if not matches:
        raise SourceError(f"Census geocoder couldn't place {where!r}; give a street address or 'lat,lon'")
    c = matches[0]["coordinates"]
    return c["y"], c["x"]


def bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _from_opensky(s: list) -> Plane:
    alt = s[7] if s[7] is not None else s[13]
    return Plane(
        hex=s[0].upper(), callsign=(s[1] or "").strip(), lat=s[6], lon=s[5],
        alt_ft=round(alt * 3.28084) if alt is not None else None,
        speed_kt=round(s[9] * 1.94384) if s[9] is not None else None,
        heading=round(s[10]) if s[10] is not None else None, squawk=s[14], on_ground=bool(s[8]),
    )


def opensky(ctx: Ctx, box: tuple[float, float, float, float] | None = None,
            icao24: list[str] | None = None) -> list[Plane]:
    params: dict = {}
    if box:
        params.update(lamin=box[0], lomin=box[1], lamax=box[2], lomax=box[3])
    if icao24:
        params["icao24"] = [h.lower() for h in icao24]
    data = ctx.http.get(OPENSKY, params=params, ttl=0)
    return [_from_opensky(s) for s in data.get("states") or []]


def local_feed(ctx: Ctx, url: str) -> list[Plane]:
    """dump1090-fa / readsb aircraft.json, e.g. http://pi.local:8080/data/aircraft.json"""
    data = ctx.http.get(url, ttl=0)
    out = []
    for a in data.get("aircraft", []):
        alt = a.get("alt_baro")
        out.append(Plane(
            hex=a["hex"].upper().lstrip("~"), callsign=(a.get("flight") or "").strip(),
            lat=a.get("lat"), lon=a.get("lon"),
            alt_ft=alt if isinstance(alt, int) else None,
            speed_kt=round(a["gs"]) if a.get("gs") is not None else None,
            heading=round(a["track"]) if a.get("track") is not None else None,
            squawk=a.get("squawk"), on_ground=alt == "ground",
        ))
    return out


def annotate(ctx: Ctx, planes: list[Plane], screen: bool = True) -> list[Plane]:
    have_faa = faa.available(ctx)
    have_csl = screen and csl.available(ctx)
    for p in planes:
        if have_faa:
            rows = faa.lookup(ctx, p.hex)
            p.reg = rows[0] if rows else None
        if p.squawk in EMERGENCY:
            p.flags.append(f"squawk {p.squawk} ({EMERGENCY[p.squawk]})")
        r = p.reg or {}
        if r.get("registrant_type") == "Government":
            p.flags.append("government")
        elif r.get("registrant_type") in CORPORATE and not _AIRLINE_CALLSIGN.match(p.callsign or ""):
            p.flags.append("corporate")
        if r.get("type_registrant") == "8" or r.get("registrant_type") == "Non-Citizen Corporation":
            p.flags.append("foreign-owned")
        if have_csl and p.owner:
            hits = csl.screen(ctx, p.owner)
            if hits:
                p.flags.append(f"owner on {hits[0].record['source'].split(' - ')[0]}")
    return planes


def owner_hexes(ctx: Ctx, owner: str) -> list[dict]:
    return [r for r in faa.by_owner(ctx, owner) if r.get("mode_s_hex")]

"""Everything public records say about a place: who represents it, who pollutes it, who's paid
to work there, what's registered there, and what's shaking or flying overhead."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..http import SourceError
from . import congress, faa, usaspending
from .base import Ctx

CENSUS_GEO = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
ECHO = "https://echodata.epa.gov/echo/echo_rest_services"
ECHO_REPORT = "https://echo.epa.gov/detailed-facility-report?fid={rid}"
USGS = "https://earthquake.usgs.gov/fdsnws/event/1/query"
NWS = "https://api.weather.gov/alerts/active"

FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP",
    "72": "PR", "78": "VI",
}


@dataclass
class Place:
    lat: float
    lon: float
    state: str = ""
    county: str = ""
    county_fips: str = ""  # 3-digit
    tract: str = ""
    district: int | None = None  # congressional district (0 = at-large)
    city: str = ""


@dataclass
class Report:
    place: Place
    reps: list[dict] = field(default_factory=list)
    facilities: list[dict] = field(default_factory=list)
    facility_count: int = 0
    contracts: list[dict] = field(default_factory=list)
    aircraft: list[dict] = field(default_factory=list)
    aircraft_count: int = 0
    quakes: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def locate(ctx: Ctx, lat: float, lon: float) -> Place:
    data = ctx.http.get(CENSUS_GEO, params={"x": lon, "y": lat, "benchmark": "Public_AR_Current",
                                            "vintage": "Current_Current", "format": "json"}, ttl=30 * 86400)
    g = data.get("result", {}).get("geographies", {})

    def first(prefix: str) -> dict:
        for k, v in g.items():
            if k.startswith(prefix) and v:
                return v[0]
        return {}

    p = Place(lat, lon)
    st = first("States")
    p.state = FIPS.get(st.get("STATE", ""), "")
    c = first("Counties")
    p.county, p.county_fips = c.get("NAME", ""), c.get("COUNTY", "")
    p.tract = first("Census Tracts").get("NAME", "")
    p.city = first("Incorporated Places").get("NAME", "") or first("County Subdivisions").get("NAME", "")
    cd = next((v[0] for k, v in g.items() if "Congressional Districts" in k and v), {})
    for key in ("CD119", "CD120", "CD118", "BASENAME"):
        if (val := cd.get(key)) and str(val).isdigit():
            p.district = int(val)
            break
    if cd and p.district is None and "at Large" in (cd.get("NAME") or ""):
        p.district = 0
    if p.district == 98:  # delegate districts (DC, territories) are coded 98
        p.district = 0
    return p


def representatives(ctx: Ctx, p: Place) -> list[dict]:
    ms = congress.members(ctx)
    out = []
    for m in ms.values():
        if m.state != p.state:
            continue
        if m.chamber == "house" and p.district is not None and (m.district or 0) != p.district:
            continue
        trades = congress.trades(ctx, member=m.name, limit=10_000) if m.chamber == "house" and congress.available(ctx) else []
        out.append({"name": m.name, "chamber": m.chamber, "party": m.party,
                    "district": m.district, "committees": [c["name"] for c in m.committees if ":" not in c["name"]],
                    "trades": len(trades)})
    return sorted(out, key=lambda r: r["chamber"])


def _money(s: str | None) -> float:
    try:
        return float((s or "0").replace("$", "").replace(",", ""))
    except ValueError:
        return 0.0


def facilities(ctx: Ctx, p: Place, radius_mi: float) -> tuple[int, list[dict]]:
    q = ctx.http.get(f"{ECHO}.get_facilities", params={"output": "JSON", "p_lat": p.lat, "p_long": p.lon,
                                                         "p_radius": radius_mi}, ttl=86400)
    res = q.get("Results", {})
    if "QueryID" not in res:
        raise SourceError(f"EPA ECHO: {res.get('Message', 'no results')}")
    total = int(res.get("QueryRows") or 0)
    rows = ctx.http.get(f"{ECHO}.get_qid", params={"qid": res["QueryID"], "output": "JSON", "pageno": 1},
                        ttl=86400).get("Results", {}).get("Facilities", [])
    out = []
    for f in rows:
        penalties = _money(f.get("FacTotalPenalties"))
        nc = int(f.get("FacQtrsWithNC") or 0)
        snc = f.get("FacSNCFlg") == "Y"
        if not (snc or penalties or nc >= 4):
            continue
        out.append({"name": f["FacName"], "address": f"{f.get('FacStreet', '')}, {f.get('FacCity', '')}",
                    "significant_violator": snc, "quarters_in_violation": nc, "penalties": penalties,
                    "last_penalty": f.get("FacDateLastPenalty"), "status": f.get("FacComplianceStatus"),
                    "industry": (f.get("FacSICDescs") or f.get("CAASICDescs") or "")[:60],
                    "federal": f.get("FacFederalFlg") == "Y", "url": ECHO_REPORT.format(rid=f["RegistryID"])})
    out.sort(key=lambda f: (not f["significant_violator"], -f["penalties"], -f["quarters_in_violation"]))
    return total, out


def contracts(ctx: Ctx, p: Place, limit: int = 10) -> list[dict]:
    if not (p.state and p.county_fips):
        return []
    today = date.today()
    body = {
        "filters": {
            "place_of_performance_locations": [{"country": "USA", "state": p.state, "county": p.county_fips}],
            "award_type_codes": usaspending.CONTRACTS,
            "time_period": [{"start_date": (today - timedelta(days=365)).isoformat(), "end_date": today.isoformat()}],
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Sub Agency", "Description",
                   "generated_internal_id"],
        "limit": limit, "sort": "Award Amount", "order": "desc",
    }
    data = ctx.http.post(f"{usaspending.API}/search/spending_by_award/", body, ttl=86400)
    return [{"recipient": a["Recipient Name"], "amount": a["Award Amount"], "agency": a["Awarding Sub Agency"],
             "description": (a["Description"] or "")[:80], "url": usaspending.award_url(a["generated_internal_id"])}
            for a in data.get("results", [])]


def aircraft(ctx: Ctx, p: Place, limit: int = 15) -> tuple[int, list[dict]]:
    if not (faa.available(ctx) and p.state and p.county_fips):
        return 0, []
    db = ctx.db()
    cols = {r[1] for r in db.execute("pragma table_info(faa_master)")}
    if "county" not in cols:
        return 0, []  # registry loaded by an older govint; re-run `govint sync faa`
    where = "m.state=? and m.county=?"
    args = (p.state, p.county_fips)
    total = db.execute(f"select count(*) from faa_master m where {where}", args).fetchone()[0]
    # Who operates the most multi-engine aircraft and helicopters here.
    rows = db.execute(f"""select m.name, m.city, count(*) as n, group_concat(m.n_number, ' ') as tails,
                                 group_concat(distinct trim(r.model)) as models
                          from faa_master m left join faa_ref r on r.code = m.mfr_mdl_code
                          where {where} and r.type_acft in ('5', '6')
                          group by m.name order by n desc, m.name limit ?""", (*args, limit)).fetchall()
    return total, [dict(r) for r in rows]


def quakes(ctx: Ctx, p: Place, radius_km: float = 100, years: int = 10) -> list[dict]:
    data = ctx.http.get(USGS, params={"format": "geojson", "latitude": p.lat, "longitude": p.lon,
                                      "maxradiuskm": radius_km, "minmagnitude": 2.5, "orderby": "magnitude",
                                      "starttime": (date.today() - timedelta(days=365 * years)).isoformat(),
                                      "limit": 5}, ttl=86400)
    return [{"mag": f["properties"]["mag"], "place": f["properties"]["place"],
             "when": date.fromtimestamp(f["properties"]["time"] / 1000).isoformat(),
             "url": f["properties"]["url"]} for f in data.get("features", [])]


def alerts(ctx: Ctx, p: Place) -> list[dict]:
    data = ctx.http.get(NWS, params={"point": f"{p.lat:.4f},{p.lon:.4f}"}, ttl=600)
    return [{"event": f["properties"]["event"], "headline": f["properties"].get("headline"),
             "severity": f["properties"].get("severity")} for f in data.get("features", [])]


def report(ctx: Ctx, lat: float, lon: float, radius_mi: float = 1.0) -> Report:
    p = locate(ctx, lat, lon)
    rep = Report(place=p)
    steps = [
        ("representatives", lambda: setattr(rep, "reps", representatives(ctx, p))),
        ("EPA ECHO", lambda: _set2(rep, "facility_count", "facilities", facilities(ctx, p, radius_mi))),
        ("USAspending", lambda: setattr(rep, "contracts", contracts(ctx, p))),
        ("FAA", lambda: _set2(rep, "aircraft_count", "aircraft", aircraft(ctx, p))),
        ("USGS", lambda: setattr(rep, "quakes", quakes(ctx, p))),
        ("NWS", lambda: setattr(rep, "alerts", alerts(ctx, p))),
    ]
    for label, fn in steps:
        try:
            fn()
        except (SourceError, KeyError, ValueError) as e:
            rep.notes.append(f"{label}: {e}")
    return rep


def _set2(obj, a: str, b: str, pair: tuple) -> None:
    setattr(obj, a, pair[0])
    setattr(obj, b, pair[1])

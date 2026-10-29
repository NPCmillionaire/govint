import json
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from govint import names
from govint.brief import render
from govint.config import Settings
from govint.graph import COMPANY, Edge, Graph, Node, Result
from govint.http import Http, SourceError
from govint.sources import csl, edgar, faa, fec, usaspending
from govint.sources.base import Ctx


@pytest.fixture
def ctx(tmp_path: Path) -> Ctx:
    s = Settings(contact="test@example.com", data_gov_key="DEMO_KEY", sam_key=None, data_dir=tmp_path)
    return Ctx(settings=s, http=Http(s.user_agent, s.cache_path))


# ---- names

def test_normalize_strips_suffixes_and_punctuation():
    assert names.normalize("Lockheed Martin Corp.") == "lockheed martin"
    assert names.normalize("AT&T Inc") == "at and t"


def test_score_keeps_parenthetical_qualifiers():
    # "(Yangon)" is a different legal entity, not noise
    assert names.score("Huawei Technologies", "Huawei Technologies (Yangon) Co., Ltd.") < 100
    assert names.score("Huawei Technologies", "HUAWEI TECHNOLOGIES CO., LTD.") == 100


def test_short_name_inside_long_name_is_not_a_strong_match():
    assert names.score("Acme", "Acme Rocket Fuel Trading of Tehran") < 88


# ---- graph

def test_graph_merges_props_sources_and_flags():
    g = Graph()
    g.merge(Result(nodes=[Node("company:x", COMPANY, "X", props={"a": 1}, sources=[{"name": "s1", "url": "u1"}])]))
    new = g.merge(Result(nodes=[Node("company:x", COMPANY, "X", props={"a": 2, "b": 3},
                                     sources=[{"name": "s2", "url": "u2"}], flags=["f"])]))
    n = g.nodes["company:x"]
    assert new == []
    assert n.props == {"a": 1, "b": 3}
    assert len(n.sources) == 2 and n.flags == ["f"]


def test_graph_drops_edges_to_unknown_nodes():
    g = Graph()
    g.merge(Result(nodes=[Node("a", COMPANY, "A")], edges=[Edge("a", "missing", "rel")]))
    assert not g.edges


# ---- EDGAR

FORM4 = """<?xml version="1.0"?><ownershipDocument>
<reportingOwner><reportingOwnerId><rptOwnerCik>0000000042</rptOwnerCik><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CFO</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction><transactionDate><value>2026-01-02</value></transactionDate>
<transactionCoding><transactionCode>S</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>100</value></transactionShares><transactionPricePerShare><value>10.5</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument>"""


def test_parse_form4():
    owners, txns = edgar.parse_ownership_xml(FORM4)
    assert owners == [{"cik": 42, "name": "DOE JANE", "roles": {"officer"}, "title": "CFO"}]
    assert txns[0]["code"] == "S" and txns[0]["value"] == 1050.0


SUBS = {
    "name": "ACME CORP", "tickers": ["ACME"], "exchanges": ["NYSE"], "sicDescription": "Widgets",
    "stateOfIncorporation": "DE", "ein": "1", "lei": None, "phone": "1", "website": "", "entityType": "operating",
    "formerNames": [{"name": "ACME WIDGETS"}],
    "addresses": {"business": {"street1": "1 MAIN ST", "city": "AUSTIN", "stateOrCountry": "TX", "zipCode": "78701"}},
    "filings": {"recent": {"form": ["4", "10-K"], "filingDate": ["2026-01-03", "2026-01-01"],
                           "accessionNumber": ["0001-26-000001", "0001-26-000002"],
                           "primaryDocument": ["xslF345X05/form4.xml", "acme.htm"]}},
}


@respx.mock
def test_edgar_profile_and_insiders(ctx):
    respx.get("https://data.sec.gov/submissions/CIK0000000007.json").mock(return_value=httpx.Response(200, json=SUBS))
    respx.get("https://www.sec.gov/Archives/edgar/data/7/000126000001/form4.xml").mock(
        return_value=httpx.Response(200, text=FORM4))
    r = edgar.profile(ctx, 7)
    assert r.nodes[0].label == "ACME CORP" and r.nodes[0].props["former_names"] == ["ACME WIDGETS"]
    assert r.nodes[1].label == "1 MAIN ST, AUSTIN, TX, 78701"
    people, notes = edgar.insiders(ctx, 7)
    assert notes == []
    assert people[0].name == "DOE JANE" and people[0].sold == 1050.0 and people[0].title == "CFO"


@respx.mock
def test_sec_403_explains_contact(ctx):
    respx.get("https://data.sec.gov/submissions/CIK0000000007.json").mock(return_value=httpx.Response(403))
    with pytest.raises(SourceError, match="contact"):
        edgar.submissions(ctx, 7)


@respx.mock
def test_cache_avoids_second_request(ctx):
    route = respx.get("https://example.gov/x").mock(return_value=httpx.Response(200, json={"ok": 1}))
    assert ctx.http.get("https://example.gov/x") == {"ok": 1}
    assert ctx.http.get("https://example.gov/x") == {"ok": 1}
    assert route.call_count == 1


@respx.mock
def test_long_retry_after_fails_fast(ctx):
    respx.get("https://example.gov/slow").mock(
        return_value=httpx.Response(429, headers={"retry-after": "3600"}))
    with pytest.raises(SourceError, match="429"):
        ctx.http.get("https://example.gov/slow")


# ---- CSL

def _write_csl(ctx, records):
    csl.path(ctx).write_text(json.dumps(records))
    csl._load.cache_clear()


def test_csl_screen_matches_alt_names(ctx):
    _write_csl(ctx, [
        {"id": "1", "source": "SDN", "name": "BAD ACTOR TRADING LLC", "alt_names": ["Shady Shipping Co"]},
        {"id": "2", "source": "EL", "name": "Unrelated Name"},
    ])
    hits = csl.screen(ctx, "Shady Shipping Company")
    assert [h.record["id"] for h in hits] == ["1"]
    assert csl.screen(ctx, "Lockheed Martin") == []


def test_csl_screen_result_flags_subject(ctx):
    _write_csl(ctx, [{"id": "9", "source": "SDN", "name": "Bad Actor Trading LLC"}])
    subj = Node("q", COMPANY, "Bad Actor Trading")
    r = csl.screen_result(ctx, subj)
    assert "sanctions-match" in subj.flags and r.edges[0].rel == "possible_match"


# ---- FAA

def _faa_zip(path: Path) -> Path:
    master = ("N-NUMBER,SERIAL NUMBER,MFR MDL CODE,ENG MFR MDL,YEAR MFR,TYPE REGISTRANT,NAME,STREET,STREET2,CITY,"
              "STATE,ZIP CODE,REGION,COUNTY,COUNTRY,LAST ACTION DATE,CERT ISSUE DATE,CERTIFICATION,TYPE AIRCRAFT,"
              "TYPE ENGINE,STATUS CODE,MODE S CODE,FRACT OWNER,AIR WORTH DATE,OTHER NAMES(1),OTHER NAMES(2),"
              "OTHER NAMES(3),OTHER NAMES(4),OTHER NAMES(5),EXPIRATION DATE,UNIQUE ID,KIT MFR, KIT MODEL,MODE S CODE HEX,\n"
              "123AB ,5555,1234567,,2019,7,ACME AVIATION LLC,1 MAIN ST,,AUSTIN,TX,78701,4,453,US,20240101,20190505,"
              ",5,5,V,50000000,,20190505,,,,,,20290531,1,,,A0B1C2      ,\n")
    ref = "CODE,MFR,MODEL,TYPE-ACFT,TYPE-ENG,AC-CAT,BUILD-CERT-IND,NO-ENG,NO-SEATS,AC-WEIGHT,SPEED,\n1234567,GULFSTREAM,G650,5,5,1,0,2,19,CLASS 3,0,\n"
    z = path / "ReleasableAircraft.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("MASTER.txt", "﻿" + master)
        zf.writestr("ACFTREF.txt", ref)
    return z


def test_faa_load_lookup_and_owner_search(ctx, tmp_path):
    assert faa.load_zip(ctx, _faa_zip(tmp_path)) == 1
    assert faa.meta(ctx)["data_date"]
    [a] = faa.lookup(ctx, "123ab")
    assert a["n_number"] == "N123AB" and a["model"].strip() == "G650" and a["registrant_type"] == "LLC"
    assert faa.lookup(ctx, "A0B1C2")[0]["n_number"] == "N123AB"
    assert faa.by_owner(ctx, "Acme Aviation")[0]["n_number"] == "N123AB"
    assert faa.by_owner(ctx, "Zenith Air") == []


# ---- USAspending / FEC helpers

def test_award_family_filter():
    assert usaspending._same_family("Palantir Technologies", "PALANTIR USG INC")
    assert not usaspending._same_family("Palantir Technologies", "LOCKHEED MARTIN CORP")


@respx.mock
def test_fec_employer_giving_merges_spellings(ctx):
    respx.get(url__startswith="https://api.open.fec.gov/v1/schedules/schedule_a/by_employer/").mock(
        return_value=httpx.Response(200, json={"results": [
            {"committee_id": "C1", "employer": "ACME", "total": 10, "count": 1, "cycle": 2026},
            {"committee_id": "C1", "employer": "ACME INC", "total": 5, "count": 2, "cycle": 2026},
            {"committee_id": "C2", "employer": "ACME", "total": 50, "count": 1, "cycle": 2026},
        ]}))
    rows = fec.employer_giving(ctx, "acme", 2026)
    assert [(r["committee_id"], r["total"], r["count"]) for r in rows] == [("C2", 50, 1), ("C1", 15, 3)]


# ---- brief

def test_brief_renders_flags_and_insiders():
    g = Graph()
    seed = g.add_node(Node("query:acme", COMPANY, "Acme", props={"query": True}))
    co = Node("company:cik:7", COMPANY, "ACME CORP", props={"cik": 7}, sources=[{"name": "SEC", "url": "https://sec"}],
              flags=["name-churn"])
    p = Node("person:cik:42", "person", "DOE JANE")
    g.merge(Result(nodes=[co, p], edges=[
        Edge(seed.id, co.id, "same_as"),
        Edge(p.id, co.id, "insider_of", props={"role": "CFO", "last_filing": "2026-01-03", "bought_usd": 0, "sold_usd": 1050}),
    ]))
    md = render(g, seed)
    assert "Three or more former names" in md
    assert "| DOE JANE | CFO | 2026-01-03 |  | $1,050 |" in md
    assert "[ACME CORP](https://sec)" in md


def test_generic_words_do_not_carry_a_match():
    assert names.score("Palantir Technologies", "Planet Technology") < 88
    assert names.score("Palantir Technologies Inc", "PALANTIR TECHNOLOGIES INC.") == 100


def test_address_key_unifies_abbreviations():
    assert names.address_key("19505 BISCAYNE BOULEVARD, SUITE 2350, AVENTURA, FL, 33180") == \
        names.address_key("19505 BISCAYNE BLVD STE 2350, AVENTURA, FL, 33180")


def test_person_names_match_across_order_and_patronymic():
    assert names.score("Viktor Vekselberg", "VEKSELBERG, Viktor Feliksovich", person=True) >= 88
    assert names.score("Oleg Deripaska", "DERIPASKA, Oleg Vladimirovich", person=True) >= 88


def test_shared_first_name_or_single_word_alias_is_not_a_match():
    assert names.score("Alexander Karp", "ALEXANDER KOVAR", person=True) < 88
    assert names.score("Mohammed Ali", "Ali") < 88
    assert names.score("Rosneft", "Open Joint-Stock Company Rosneft Oil Company") < 100


def test_international_subsidiary_is_not_the_parent():
    assert names.score("Lockheed Martin", "LOCKHEED MARTIN INTERNATIONAL SA") < 100


@respx.mock
def test_leaks_new_only_and_routine_filter(ctx):
    from govint.sources import leaks
    respx.get(url__startswith="https://api.fda.gov/device/510k.json").mock(return_value=httpx.Response(200, json={
        "meta": {"results": {"total": 1}},
        "results": [{"k_number": "K261111", "decision_date": "2026-09-01", "device_name": "Widget Sensor",
                     "applicant": "Acme Inc.", "decision_description": "Substantially Equivalent",
                     "product_code": "ABC", "date_received": "2026-05-01"}]}))
    respx.get(url__startswith="https://api.fda.gov/device/pma.json").mock(return_value=httpx.Response(200, json={
        "meta": {"results": {"total": 1}},
        "results": [{"pma_number": "P1", "supplement_number": "S2", "decision_date": "2026-09-02", "applicant": "ACME INC",
                     "trade_name": "Thing", "supplement_type": "30-Day Notice"}]}))
    respx.get(url__startswith="https://api.fda.gov/device/registrationlisting.json").mock(
        return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}))
    respx.get(url__startswith="https://api.fda.gov/drug/drugsfda.json").mock(
        return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}))
    items, notes = leaks.scan(ctx, "Acme", days=36500)
    assert notes == []
    assert [(i.id, i.signal) for i in items] == [("P1S2", False), ("K261111", True)]
    assert len(leaks.only_new(ctx, items)) == 2
    assert leaks.only_new(ctx, items) == []


def test_sky_annotate_joins_faa_and_flags(ctx, tmp_path):
    from govint.sources import sky
    faa.load_zip(ctx, _faa_zip(tmp_path))
    planes = [sky.Plane("A0B1C2", "N123AB", 38.9, -77.0, 41000, 450, 90, "1200", False),
              sky.Plane("ABCDEF", "DAL850", 38.9, -77.0, 3000, 200, 90, "7700", False)]
    sky.annotate(ctx, planes)
    assert planes[0].tail == "N123AB" and planes[0].owner == "ACME AVIATION LLC"
    assert planes[0].flags == ["corporate"]
    assert planes[1].reg is None and planes[1].flags == ["squawk 7700 (emergency)"]


def test_sky_bbox_and_distance():
    from govint.sources import sky
    lamin, lomin, lamax, lomax = sky.bbox(38.9, -77.0, 50)
    assert lamin < 38.9 < lamax and lomin < -77.0 < lomax
    assert 40 < sky.distance_km(38.9, -77.0, 38.9 + 50 / 111, -77.0) < 60


def test_faa_mirror_build_and_install_roundtrip(ctx, tmp_path):
    m = faa.build_mirror(_faa_zip(tmp_path), tmp_path / "out")
    assert m["rows"] == 1 and len(m["sha256"]) == 64
    assert faa.install_mirror(ctx, tmp_path / "out" / faa.MIRROR_FILE, m) == 1
    assert faa.lookup(ctx, "N123AB")[0]["model"].strip() == "G650"
    assert faa.meta(ctx)["source"] == "mirror"


def test_faa_mirror_rejects_bad_checksum(ctx, tmp_path):
    m = faa.build_mirror(_faa_zip(tmp_path), tmp_path / "out")
    with pytest.raises(SourceError, match="checksum"):
        faa.install_mirror(ctx, tmp_path / "out" / faa.MIRROR_FILE, {**m, "sha256": "0" * 64})


@respx.mock
def test_faa_sync_falls_back_to_mirror(ctx, tmp_path):
    m = faa.build_mirror(_faa_zip(tmp_path), tmp_path / "out")
    gz = (tmp_path / "out" / faa.MIRROR_FILE).read_bytes()
    respx.get(faa.URL).mock(return_value=httpx.Response(403))
    base = ctx.settings.mirror
    respx.get(f"{base}/faa/faa.manifest.json").mock(return_value=httpx.Response(200, json=m))
    respx.get(f"{base}/faa/{faa.MIRROR_FILE}").mock(return_value=httpx.Response(200, content=gz))
    n, where = faa.sync(ctx)
    assert n == 1 and where.startswith("mirror")
    assert faa.lookup(ctx, "A0B1C2")[0]["n_number"] == "N123AB"


def test_inserted_word_is_a_different_company():
    assert names.score("UNITED AIRLINES INC", "UNITED AFRICAN AIRLINES") < 88
    assert names.score("Oleg Deripaska", "DERIPASKA, Oleg Vladimirovich", person=True) >= 88


@pytest.mark.parametrize("query,candidate", [
    ("PARIS AIR INC", "PARS AIR"), ("SIERRA ALPHA GROUP LLC", "ALPHA GROUP"),
    ("WINGS AVIATION INC", "GOLDEN WINGS AVIATION"), ("ESM AVIATION LLC", "QESHM AVIATION"),
    ("HUNTER AVIATION INTERNATIONAL INC", "AVIATION INTERNATIONAL"), ("ROTORX LLC", "ROTOR"),
    ("NORTH STAR EQUIPMENT LLC", "NORTH STAR"), ("GREENE CO", "GREEN"),
])
def test_common_word_company_names_do_not_collide(query, candidate):
    assert names.score(query, candidate) < 88


@pytest.mark.parametrize("query,candidate", [
    ("Hikvision", "Hangzhou Hikvision Digital Technology Co., Ltd."),
    ("Sberbank", "JOINT STOCK COMPANY SBERBANK"), ("Kaspersky Lab", "Kaspersky Labs Limited"),
    ("Rosneft", "OPEN JOINT-STOCK COMPANY ROSNEFT OIL COMPANY"),
    ("Huawei Technologies", "HUAWEI TECHNOLOGIES CO., LTD."),
])
def test_real_sanctioned_companies_still_match(query, candidate):
    assert names.score(query, candidate) >= 88

import io
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from govint import names
from govint.config import Settings
from govint.graph import COMPANY, Node
from govint.http import Http
from govint.sources import edgar, lobby, offshore
from govint.sources.base import Ctx


@pytest.fixture
def ctx(tmp_path: Path) -> Ctx:
    s = Settings(contact="t@example.com", data_gov_key="DEMO_KEY", sam_key=None, data_dir=tmp_path)
    return Ctx(settings=s, http=Http(s.user_agent, s.cache_path))


def test_address_key_states_zip4_and_numbers():
    a = names.address_key("1209 Orange Street, Wilmington, Delaware 19801-1120")
    b = names.address_key("1209 ORANGE ST, WILMINGTON, DE 19801")
    assert a == b
    assert names.address_key("One Bowerman Drive, Beaverton, Oregon") == "1 bowerman dr beaverton or"


# ---- ICIJ

def _icij_zip(tmp: Path) -> Path:
    files = {
        "nodes-entities.csv": "node_id,name,jurisdiction_description,countries,sourceID,status,incorporation_date,"
                              "service_provider\n1,ACME OFFSHORE HOLDINGS LIMITED,British Virgin Islands,,"
                              "Panama Papers,Active,01-JAN-2010,Mossack Fonseca\n"
                              "2,ORANGE TREE LIMITED,Seychelles,,Panama Papers,Active,,\n",
        "nodes-officers.csv": "node_id,name,countries,sourceID\n10,John Q Example,United States,Panama Papers\n",
        "nodes-intermediaries.csv": "node_id,name,countries,sourceID\n20,FIXER LAW LLP,Panama,Panama Papers\n",
        "nodes-addresses.csv": "node_id,address,name,countries,sourceID\n"
                               "30,\"1209 Orange Street, Wilmington, Delaware 19801\",,United States,Panama Papers\n",
        "nodes-others.csv": "node_id,name,sourceID\n",
        "relationships.csv": "node_id_start,node_id_end,rel_type,link,start_date,end_date\n"
                             "10,1,officer_of,director of,2010,\n20,1,intermediary_of,intermediary of,,\n"
                             "1,30,registered_address,registered address,,\n2,30,registered_address,registered address,,\n",
        "GENERATED_ON_20260909.txt": "",
    }
    p = tmp / "oldb.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for k, v in files.items():
            zf.writestr(k, v)
    return p


def test_offshore_load_search_links_and_address(ctx, tmp_path):
    counts = offshore.load(_icij_zip(tmp_path), offshore.path(ctx))
    assert counts["entity"] == 2 and counts["relationships"] == 4
    assert offshore.meta(ctx)["generated"] == "20260909"
    hits = offshore.search(ctx, "Acme Offshore Holdings")
    assert hits and hits[0].node["id"] == 1 and hits[0].score == 100
    assert not offshore.search(ctx, "Acme Rocket Fuel")
    rels = {(ln["rel"], ln["name"]) for ln in offshore.links(ctx, 1)}
    assert ("officer_of", "John Q Example") in rels and ("intermediary_of", "FIXER LAW LLP") in rels
    at = {r["name"] for r in offshore.at_address(ctx, "1209 Orange St, Wilmington, DE 19801")}
    assert at == {"ACME OFFSHORE HOLDINGS LIMITED", "ORANGE TREE LIMITED"}


def test_offshore_result_flags_company_and_namesake(ctx, tmp_path):
    offshore.load(_icij_zip(tmp_path), offshore.path(ctx))
    co = Node("company:q", COMPANY, "Acme Offshore Holdings Ltd")
    r = offshore.result(ctx, co)
    assert "offshore-leaks" in co.flags
    assert any(e.rel == "icij:officer_of" for e in r.edges)
    person = Node("person:q", "person", "John Q Example")
    offshore.result(ctx, person)
    assert person.flags == ["offshore-leaks-namesake"]


# ---- lobbying

def _filing(uuid, period, posted, registrant, amount, covered=None, foreign=None):
    return {
        "filing_uuid": uuid, "filing_year": 2026, "filing_period": period, "dt_posted": posted,
        "filing_document_url": f"https://lda.gov/f/{uuid}", "income": amount, "expenses": None,
        "registrant": {"id": hash(registrant) % 1000, "name": registrant},
        "client": {"id": 7, "name": "ACME DEFENSE CORP"},
        "lobbying_activities": [{"general_issue_code_display": "Defense", "description": "NDAA",
                                 "government_entities": [{"name": "SENATE"}],
                                 "lobbyists": [{"lobbyist": {"first_name": "JANE", "last_name": "DOE"},
                                                "covered_position": covered}]}],
        "foreign_entities": foreign or [],
    }


def test_lobby_dedupes_amendments_and_finds_revolving_door(ctx):
    fs = [
        _filing("a", "first_quarter", "2026-04-01", "K STREET LLC", "50000"),
        _filing("b", "first_quarter", "2026-05-01", "K STREET LLC", "60000", covered="Chief of Staff, Sen. X"),
        _filing("c", "second_quarter", "2026-07-01", "ACME DEFENSE CORP", "400000"),
        _filing("d", "first_quarter", "2026-04-01", "ACME ROCKETRY OF TEHRAN", "1"),  # client mismatch below
    ]
    fs[3]["client"] = {"id": 9, "name": "ACME ROCKETRY OF TEHRAN"}
    with respx.mock:
        respx.get(f"{lobby.API}/filings/").mock(return_value=httpx.Response(200, json={"results": fs, "next": None}))
        got = lobby.filings(ctx, client="Acme Defense", years=1)
    assert {f["filing_uuid"] for f in got} == {"b", "c"}  # amendment wins, other client dropped
    s = lobby.summarize("Acme Defense", got)
    assert s.by_year == {2026: 460000.0}
    assert [p["self"] for p in s.parties] == [True, False]
    assert s.revolving[0]["name"] == "Jane Doe"
    subj = Node("company:q", COMPANY, "Acme Defense")
    with respx.mock:
        respx.get(f"{lobby.API}/filings/").mock(return_value=httpx.Response(200, json={"results": fs, "next": None}))
        r = lobby.result(ctx, subj)
    assert "revolving-door:1" in subj.flags
    assert any(e.rel == "pays_lobbyist" for e in r.edges)
    assert all(n.props.get("screen") is False for n in r.nodes)


# ---- SEC full text

def test_edgar_fulltext_builds_document_urls(ctx):
    body = {"hits": {"total": {"value": 1}, "hits": [{"_id": "0000067716-24-000017:a2023ex21.htm", "_source": {
        "ciks": ["0000067716"], "display_names": ["MDU RESOURCES GROUP INC  (MDU)  (CIK 0000067716)"],
        "form": "10-K", "file_date": "2024-02-22", "file_type": "EX-21", "file_description": "SUBSIDIARIES",
        "biz_locations": ["Bismarck, ND"]}}]},
        "aggregations": {"entity_filter": {"buckets": [{"key": "MDU RESOURCES GROUP INC  (MDU)  (CIK 0000067716)",
                                                        "doc_count": 3}]}}}
    with respx.mock:
        route = respx.get(edgar.SEARCH).mock(return_value=httpx.Response(200, json=body))
        total, hits, top = edgar.fulltext(ctx, '"Wagner Group"', forms=["10-K"], since="2023-01-01")
    assert route.calls[0].request.url.params["forms"] == "10-K"
    assert total == 1 and top == [("MDU RESOURCES GROUP INC  (MDU)", 3)]
    assert hits[0]["url"] == "https://www.sec.gov/Archives/edgar/data/67716/000006771624000017/a2023ex21.htm"

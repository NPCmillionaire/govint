from govint.sources import congress

NUL = "\x00"
PTR = (
    f"P{NUL*4} T{NUL*5} R{NUL*3}\nClerk of the House of Representatives • Legislative Resource Center\n"
    "Name: Hon. Jane Doe\nStatus: Member\nState/District: MD06\n"
    "ID Owner Asset Transaction\nType\nDate Notification\nDate\nAmount Cap.\nGains >\n$200?\n"
    "DC Brown & Brown, Inc. Common Stock\n(BRO) [ST]\nS 08/25/2026 09/01/2026 $100,001 -\n$250,000\n"
    f"F{NUL*3} S{NUL*3}: New\n"
    "SP C.H. Robinson Worldwide, Inc. - P 08/05/2026 09/01/2026 $1,001 - $15,000\n"
    "Filing ID #20035408\nID Owner Asset Transaction\nType\nDate Notification\nDate\nAmount Cap.\nGains >\n$200?\n"
    "Common Stock (CHRW) [ST]\n"
    f"F{NUL*3} S{NUL*3}: New\n"
    "Corpay, Inc. Common Stock (CPAY)\n[ST]\nS (partial) 08/06/2026 09/01/2026 $15,001 -\n$50,000\n"
    "RSP ETF [OT] P 08/13/2026 08/13/2026 Over $50,000,000\n"
    f"D{NUL*5}: RSP ETF\n"
    " Yes  No\n I CERTIFY that the statements (FAKE) [ST] P 01/01/2026 01/01/2026 $1,001 - $15,000\n"
)


def test_parse_ptr_rows_owners_and_page_break():
    rows = congress.parse_ptr_text(PTR)
    assert [(r.owner, r.ticker, r.type, r.date) for r in rows] == [
        ("DC", "BRO", "sale", "2026-08-25"),
        ("SP", "CHRW", "purchase", "2026-08-05"),
        ("self", "CPAY", "partial sale", "2026-08-06"),
        ("self", None, "purchase", "2026-08-13"),
    ]
    assert rows[0].amount_min == 100_001 and rows[0].amount_max == 250_000
    assert rows[1].asset == "C.H. Robinson Worldwide, Inc. - Common Stock (CHRW)"
    assert rows[3].asset_type == "OT" and rows[3].amount_min == 50_000_001 and rows[3].amount_max is None


def test_committee_and_sic_sectors_overlap():
    assert "defense" in congress.committee_sectors("House Committee on Armed Services")
    assert "finance" in congress.committee_sectors("Senate Committee on Banking, Housing, and Urban Affairs")
    assert "defense" in congress.sic_sectors(3760)  # guided missiles
    assert congress.sic_sectors(5812) == []  # restaurants


def test_find_member_by_state_district():
    ms = {
        "A": congress.Member("A", "Jane Doe", "house", "MD", 6, "D"),
        "B": congress.Member("B", "John Roe", "house", "MD", 7, "R"),
        "C": congress.Member("C", "Jane Doe", "senate", "VA", None, "D"),
    }
    assert congress.find_member(ms, "Jane Doe", "MD06").bioguide == "A"
    assert congress.find_member(ms, "Nobody Here", "TX01") is None


def test_committee_mapping_avoids_false_overlaps():
    assert congress.committee_sectors("House Committee on Small Business: Contracting and Infrastructure") == []
    assert "intel-tech" not in congress.committee_sectors("House Committee on Armed Services")
    assert "transport" in congress.committee_sectors("House Committee on Transportation and Infrastructure")


def test_contract_spike_vs_prior_year():
    from datetime import date
    monthly = {(2025, m): 1e6 for m in range(1, 13)}
    monthly.update({(2026, m): 1e6 for m in range(1, 13)})
    monthly[(2026, 3)] = 50e6
    window, norm = congress.contract_spike(monthly, date(2026, 3, 10))
    assert window == 51e6 and norm == 2e6

# govint

Public-records OSINT from the command line. Give it a company or person name and it searches US government databases, links the results into one entity graph, and writes a sourced dossier.

```
govint brief "Palantir Technologies" -o palantir.md
```

## Sources

| Source | What it adds | Key? |
|---|---|---|
| SEC EDGAR | Company profile, addresses, former names, filings, insiders and their trades (Forms 3/4/5) | No (needs a contact email) |
| Consolidated Screening List | 11 US sanctions and export lists (OFAC SDN, BIS Entity List, ITAR debarred, etc.), screened offline | No |
| USAspending | Recipient IDs (UEI), parent company, lifetime federal dollars, top contracts and grants | No |
| GLEIF | LEI, jurisdiction, parent, ultimate parent, subsidiaries | No |
| FEC | Corporate PACs, who the PACs pay, donations by employees | Free api.data.gov key (DEMO_KEY works but is heavily rate limited) |
| SAM.gov | Registration, CAGE code, exclusions (debarments) | Free SAM.gov key |
| FAA registry | Aircraft by tail number, Mode S hex, or owner name, offline | No |
| House Clerk PTRs | Every House member's disclosed stock trade, parsed from the PDFs | No |
| congress-legislators | Current members and committee assignments (mirrors official rosters) | No |
| openFDA | Device clearances (510k, De Novo, PMA), new device listings, drug approvals | No |
| OpenSky / your receiver | Live ADS-B positions, joined to the FAA registry | No |
| Census geocoder | Street address to coordinates for `sky now` | No |
| FARA (DOJ) | Registered foreign agents, the countries and companies they represent, and their staff | No |
| EPA ECHO | Regulated facilities near a location, with violations and penalties | No |
| USGS / NWS | Earthquake history and active weather alerts for a location | No |
| Lobbying Disclosure Act (lda.gov) | Lobbying spend, firms hired, issues, agencies contacted, lobbyists' prior government jobs | No (optional key raises the limit from 15 to 120 req/min) |
| ICIJ Offshore Leaks | Panama, Paradise and Pandora Papers, Bahamas and Offshore Leaks: 800k offshore entities, officers, intermediaries, addresses, offline | No |
| SEC full-text search | Every word of every EDGAR filing since 2001, exhibits included | No |

## Setup

```
uv tool install --editable ~/code/govint
govint config contact you@example.com     # SEC requires a contact in the User-Agent
govint config data_gov_key <key>          # https://api.data.gov/signup/
govint config sam_key <key>               # optional, sam.gov > Account Details
govint sync csl                           # sanctions list, ~30 MB, refresh weekly
govint sync fara                          # foreign agents, ~2 MB
govint sync faa                           # tries faa.gov, falls back to the govint-data mirror
govint sync offshore                      # ICIJ Offshore Leaks, 70 MB download, ~750 MB on disk
govint config lda_key <key>               # optional, https://lda.gov/api/register/
govint status
```

The FAA often blocks scripted downloads. When it does, `govint sync faa` pulls a prebuilt copy from [govint-data](https://github.com/NPCmillionaire/govint-data/releases/tag/faa), which is refreshed weekly and verified by checksum. To use a file you downloaded yourself, run `govint sync faa --file ~/Downloads/ReleasableAircraft.zip`.

Settings live in `~/.config/govint/config.toml` (mode 600). Env vars override them: `GOVINT_CONTACT`, `GOVINT_DATA_GOV_KEY`, `GOVINT_SAM_KEY`, `GOVINT_LDA_KEY`, `GOVINT_HOME` (data dir, default `~/.local/share/govint`).

## Commands

```
govint pivot NAME [--depth 2] [--dot g.dot] [--json-out g.json]   # linked graph across all sources
govint brief NAME [-o file.md] [--depth 2]                        # markdown dossier with flags and citations
govint screen NAME | --csv names.csv [--column name] [-t 88]      # sanctions / export screening
govint edgar NAME|TICKER|CIK                                      # SEC profile, filings, insiders
govint spend NAME [--grants] [--years 10]                         # federal contracts / grants
govint fec NAME [--person]                                        # PACs, employee giving, or a donor
govint lei NAME                                                   # corporate parent/child chain
govint tail N12345 | --owner NAME                                 # FAA aircraft registry
govint sam NAME                                                   # SAM registration + exclusions

govint congress sync [YEARS...]                                   # parse House PTRs (default: this year + last)
govint congress trades [-m MEMBER] [-t TICKER] [--days N]
govint congress member NAME                                       # committees + trading summary
govint congress flags [--days 120] [--contracts] [--min-score 2]  # ranked: committee overlap, late filing,
                                                                  #   size, contract spikes vs. the company's norm
govint fara "Huawei Technologies" | "Mercury Public Affairs" | Qatar   # who represents whom
govint near "1600 Pennsylvania Ave NW, Washington DC" [-r 1]     # representatives, EPA violators, local
                                                                  #   federal contracts, aircraft operators, quakes
govint lobby "Palantir Technologies" [--years 2]                # spend, firms, issues, ex-officials lobbying
govint lobby "Ballard Partners" --firm                            # a lobbying firm's clients
govint offshore "Mossack Fonseca" | NODE_ID                       # ICIJ Offshore Leaks entity and its links
govint grep "Wagner Group" [--forms 10-K,8-K] [--since 2024-01-01]  # SEC full-text search
govint shells "Nike Inc" | "1209 Orange St, Wilmington, DE 19801"  # who else is registered at the same address
govint shells --top [--state DE]                                  # aircraft registration mail drops
govint watch add company|ticker|tail|owner|keyword|phrase|lobbying VALUE   # then: govint watch run
govint leaks "Apple Inc" "Dexcom" [--days 90] [--all] [--new]     # FDA filings that reveal products
govint sky now "1600 Pennsylvania Ave NW, Washington DC" [-r 25] [--feed http://pi:8080/data/aircraft.json] [--all]
govint sky watch --owner "ACME AVIATION LLC" [--tail N123AB] [--interval 120] [--once]
```

### Notes

- **congress**: covers the House only. The Senate's eFD site puts its data behind a click-through agreement, so it isn't scraped. 2025–2026 is about 11k trades and syncs in roughly 4 minutes. Paper filings (scanned images) are counted and skipped. Committee overlap is matched by SEC industry code (SIC), so treat it as a lead and check the linked PTR before concluding anything.
- **leaks**: `--new` remembers what it has already shown, so `govint leaks --new "Apple Inc"` works in cron. The FCC equipment-authorization site blocks scripted access, so `--fcc-grantee BCG` only prints links to open in a browser.
- **watch**: the first run for each item records what already exists without alerting; later runs report only new items. Set `govint config ntfy <topic>` to get phone notifications through [ntfy](https://ntfy.sh). `contrib/` has a systemd timer for running it every 30 minutes on a Pi.
- **lobby**: amounts are what each report discloses (outside firms report income from the client, in-house filers report total expenses), with amendments replacing the original. Without a key, lda.gov allows 15 requests a minute, so a large firm's client list takes a while.
- **offshore**: ICIJ's data is published under the ODbL. Appearing in it doesn't imply wrongdoing, and a person sharing a name with an officer is often someone else, so briefs label those as namesakes.
- **shells**: pulls a name's addresses from the FAA registry, ICIJ and SEC, then lists everything else registered at each. Addresses with 10+ unrelated registrants are marked as mass-registration addresses (registered agents, aircraft trust companies, mail drops).
- **watch phrase** alerts on new SEC filings containing a phrase; **watch lobbying** alerts on new lobbying reports for a client.
- **sky**: needs the FAA registry synced to name owners. Anonymous OpenSky requests are rate-limited, so keep `watch --interval` at 60s or more, or point `--feed` at a local dump1090/readsb receiver.

`--json` before any command gives machine-readable output. `--no-cache` bypasses the 24h HTTP cache.

## How matching works

Names are normalized (legal suffixes and punctuation removed) and scored 0–100. A match also needs most query words to have a close counterpart, and generic words like "Technologies" or "Services" can't carry a match alone. Screening hits at 95+ are flagged as matches and 88–95 as possible matches. Every line in a brief links to its source record, so check before relying on it.

## Tests

```
uv run pytest
```

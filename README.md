# govint

Public-records OSINT from the command line. Give it a company or person name and it searches US government databases, links the results into one entity graph, and writes a sourced dossier.

```
govint brief "Palantir Technologies" -o palantir.md
```

## Sources (phase 1)

| Source | What it adds | Key? |
|---|---|---|
| SEC EDGAR | Company profile, addresses, former names, filings, insiders and their trades (Forms 3/4/5) | No (needs a contact email) |
| Consolidated Screening List | 11 US sanctions and export lists (OFAC SDN, BIS Entity List, ITAR debarred, etc.), screened offline | No |
| USAspending | Recipient IDs (UEI), parent company, lifetime federal dollars, top contracts and grants | No |
| GLEIF | LEI, jurisdiction, parent, ultimate parent, subsidiaries | No |
| FEC | Corporate PACs, who the PACs pay, donations by employees | Free api.data.gov key (DEMO_KEY works but is heavily rate limited) |
| SAM.gov | Registration, CAGE code, exclusions (debarments) | Free SAM.gov key |
| FAA registry | Aircraft by tail number, Mode S hex, or owner name, offline | No |

## Setup

```
uv tool install --editable ~/code/govint
govint config contact you@example.com     # SEC requires a contact in the User-Agent
govint config data_gov_key <key>          # https://api.data.gov/signup/
govint config sam_key <key>               # optional, sam.gov > Account Details
govint sync csl                           # ~30 MB, refresh weekly
govint sync faa --file ~/Downloads/ReleasableAircraft.zip
govint status
```

The FAA blocks scripted downloads. Download https://registry.faa.gov/database/ReleasableAircraft.zip in a browser and pass it with `--file`.

Settings live in `~/.config/govint/config.toml` (mode 600). Env vars override them: `GOVINT_CONTACT`, `GOVINT_DATA_GOV_KEY`, `GOVINT_SAM_KEY`, `GOVINT_HOME` (data dir, default `~/.local/share/govint`).

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
```

`--json` before any command gives machine-readable output. `--no-cache` bypasses the 24h HTTP cache.

## How matching works

Names are normalized (legal suffixes and punctuation removed) and scored 0–100. A match also needs most query words to have a close counterpart, and generic words like "Technologies" or "Services" can't carry a match alone. Screening hits at 95+ are flagged as matches and 88–95 as possible matches. Every line in a brief links to its source record, so check before relying on it.

## Tests

```
uv run pytest
```

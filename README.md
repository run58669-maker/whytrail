# WhyTrail

**An agent that explains why a metric changed, and shows the query behind every number.**

Open Agent Hackathon 2026, **Track 02: The Agent That Can Explain Why**.

An operations analyst asks: *"Why did Manhattan taxi trips drop in the week of 2025-01-20 compared with the week of 2025-01-13?"*
WhyTrail does not guess. It lists the competing explanations, tries to **falsify** each one with a query against
federated data in Zetaris, has a Skeptic attack the survivors, has a Verifier re-derive every cited number through an
independent query, and only then writes the answer. Every claim in the answer links to a fact with its SQL, source
tables, row count and result hash.

> Status (2026-10-22): the full loop runs end to end against a live local Zetaris instance (Docker) with live Claude,
> on real data, and offline in sample mode. See [Status](#status).

## Architecture

```
python -m whytrail "<question>"   or   POST /run
              |
              v
        Orchestrator (bounded state machine, structured errors, logs/trace-<run_id>.jsonl)
              |
  H-MEM hydrate (Meterless) -- schema facts + earlier verdicts --> Planner
              |
  Planner ........ picks competing hypotheses from a catalog, each with a test that can falsify it
              |      (weather / holiday / price / data gap / local-vs-citywide)
              v
  Retriever ...... restricted SQL templates --> Zetaris (REST /api/v1.0/sql-editor/sqls/run)
              |      NY_TAXI.taxi_daily   NOAA_GHCN.central_park_daily   REF.us_holidays
              |      results --> World Model IngestPipeline (idempotent, provenance per fact)
              v
  Analyst ........ deterministic verdict per hypothesis: supported / refuted (+ edges in World Model)
              v
  Skeptic ........ for each supported hypothesis: strongest alternative + follow-up test
              |      --> Retriever: federated join NY_TAXI x NOAA_GHCN x REF, weekday-matched attribution
              |      (a hypothesis that does not survive is downgraded: needs_revision)
              v
  Verifier ....... re-derives each cited number from raw rows via a second Zetaris query;
              |      a mismatch blocks the Writer and replaces the number
              v
  Writer ......... answer + why-trail, built only from World Model ContextBuilder payloads
              |
  H-MEM mine ..... verdicts + run outcome persisted for the next run's Planner
```

Planner, Skeptic and Writer call the model through one **injected generator**. Retriever, Analyst and Verifier are
plain code, so numbers never come from model text.

## Sponsor technology

### Zetaris (data harness, load-bearing)
- Every piece of evidence is a Lightning SQL query sent through the **direct REST API** documented in
  `zetaris/getting-started` (`POST {ZETARIS_REST_URL}/sql-editor/sqls/run`, bearer API key, `X-Org-ID`,
  UUID `X-Request-ID` and `queryId`, one statement per call). Client: [`whytrail/zetaris.py`](whytrail/zetaris.py).
- Three sources are addressed with qualified `SOURCE.table` names, and the Skeptic's follow-up is a **federated
  join across all three** (`daily_joined` in [`whytrail/queries.py`](whytrail/queries.py)).
- The Verifier queries Zetaris a second time with a different formulation (raw rows, summed client-side) to check
  every number the Writer cites.
- REST values arrive as strings; they are converted before comparison. Timeout 20 s, one retry, structured error.
- Sample mode (`--mock`) serves the same qualified names from SQLite databases attached under the source names, so
  the identical SQL text runs offline.

### Meterless (agent engines)
Implemented from the Meterless reference architectures (minimum sets from the Meterless README §3):

| Engine | Services implemented | Written by | Read by |
|---|---|---|---|
| **World Model** ([`world_model.py`](whytrail/meterless/world_model.py)) | `WorldStore`, `IngestPipeline` (idempotent, provenance), `ContextBuilder` (structured payload + consumer manifest) | Retriever, Analyst, Verifier | Skeptic, Writer |
| **H-MEM** ([`hmem.py`](whytrail/meterless/hmem.py)) | `MemoryStore` (layers, supersede, persist), `MemoryRetrieval` (hybrid score, `retrievalReason`, strategy label) | Retriever (schema), Analyst (verdicts), Orchestrator (outcome) | Planner |

- Engines never call a model; the injected generator is in [`whytrail/generator.py`](whytrail/generator.py).
- Evidence JSONL: `logs/trace-<run_id>.jsonl` and `logs/world-<run_id>.jsonl`.
- Measured effect of H-MEM (sample mode, same question asked twice): **20 Zetaris queries on a cold run, 10 on the
  warm run** (schema discovery skipped, already-refuted hypotheses cited from memory instead of re-queried).
  See [`examples/run1_cold_memory.txt`](examples/run1_cold_memory.txt) and [`examples/run2_warm_memory.txt`](examples/run2_warm_memory.txt).

### Cursor
Cursor is the development environment for this repository. Project rules for Cursor Agent are in
[`.cursor/rules/whytrail.mdc`](.cursor/rules/whytrail.mdc). See [How we built it](#how-we-built-it).

### NVIDIA
Not used yet. Planned: route the Skeptic role to NVIDIA Nemotron through the same injected generator.

## Setup

Requirements: Python 3.11+.

```bash
pip install -r requirements.txt
cp .env.example .env     # only needed for live mode
```

## Usage

```bash
# Offline, no credentials (same as SAMPLE_MODE=true); synthetic sample data is January 2024
python -m whytrail --mock "Why did Manhattan taxi trips drop in the week of 2024-01-15 compared with the week of 2024-01-08?"

# Live model (Claude, default claude-opus-5-5), sample data
ANTHROPIC_API_KEY=... python -m whytrail --mock-data "<question>"

# Fully live: Zetaris + Claude (needs ZETARIS_REST_URL / ZETARIS_ORG_ID / ZETARIS_API_KEY)
python -m whytrail "Why did Manhattan taxi trips drop in the week of 2025-01-20 compared with the week of 2025-01-13?"

# With no question, the CLI picks the right default per mode (2024-01 sample question in --mock; 2025-01 live default otherwise).

# HTTP API
python run.py
# Live (real data)
curl -X POST localhost:8000/run -H 'content-type: application/json' \
  -d '{"run_id":"demo-001","input":{"query":"Why did Manhattan taxi trips drop in the week of 2025-01-20 compared with the week of 2025-01-13?"},"options":{"sample_mode":false}}'
# Sample (synthetic January 2024)
curl -X POST localhost:8000/run -H 'content-type: application/json' \
  -d '{"run_id":"demo-001","input":{"query":"Why did Manhattan taxi trips drop in the week of 2024-01-15 compared with the week of 2024-01-08?"},"options":{"sample_mode":true}}'

# Tests
python -m pytest
```

Flags: `--json` (full result), `--reset-memory`, `--memory PATH`, `--max-cost USD`, `--quiet`.

## Example output (live: Zetaris + Claude, real data)

One recorded run (`run-b5402d4e`, 2026-10-22). Full transcript:
[`examples/run5_live_zetaris_claude.txt`](examples/run5_live_zetaris_claude.txt).

```
Manhattan trips changed -1.42% (758,672 -> 747,921) from 2025-01-13..2025-01-19 to 2025-01-20..2025-01-26.
Why: H_holiday (confidence 0.95). MLK Day (2025-01-20) had 16,783 fewer trips than the matched weekday
(100% of the gross loss); the other six days were +6,032 combined.
Ruled out: weather, data gap, price, Manhattan-only (other boroughs +8.5%).
Verifier re-derived both totals from raw rows: match.
20 Zetaris queries, 3 Claude calls, 34.3 s, $0.057.
```

The same transcript shows a question outside the data range returning `NoDataError` with the available range.

## Example output (sample mode)

```
Manhattan trips changed -7.5% (599,892 -> 554,691) from 2024-01-08..2024-01-14 to 2024-01-15..2024-01-21.
Why: holiday days (2024-01-15) account for 24,380 fewer trips (50% of the gross loss); snow days (2024-01-16,
2024-01-19) account for 24,333 fewer trips (50% of the gross loss). Ordinary days changed by +3,512 trips ...
Ruled out: H_price, H_data_gap, H_local ...

=== why-trail ===
- H_weather -> supported: ... snow 0mm -> 46mm ...; Skeptic follow-up: snow days explain 50% of the drop  [evidence f_6b9639dc2b, f_594c44c18b]
- H_price -> refuted: ... fare per trip $19.48 -> $19.55  [evidence f_6a7f0691dd, f_fbb4fef5e0]
- verify:metric_b: 554,691 | re-derived 554,691 from raw rows -> ok  [evidence f_3e4ec4f56f]
```

Sample inputs and full outputs in [`examples/`](examples/): cold memory, warm memory, a different borough and
direction (Brooklyn, rise), a live-Claude run on sample data, and a live Zetaris + Claude run on real data.

## Data

| Mode | Data |
|---|---|
| Sample (`--mock`) | **Synthetic** January 2024 data generated deterministically in [`whytrail/sample_data.py`](whytrail/sample_data.py), shaped like NY_TAXI daily trip aggregates, NOAA GHCN-Daily Central Park weather and US federal holidays. Not real TLC or NOAA data. |
| Live | Real data: `NY_TAXI.taxi_daily` (2024-04-01 .. 2025-02-28), `NOAA_GHCN.central_park_daily` (through 2025-02-05), `REF.us_holidays`. See [Live table mapping](#live-table-mapping). |

### Live table mapping

- `NY_TAXI.taxi_daily`: daily x borough aggregate built inside Zetaris from the built-in trip-level
  `NY_TAXI.yellow_taxi` sample (pickup zone -> borough), materialised with `RUN EXPORT TO STORAGE` (one full scan,
  about 32 minutes on a laptop Docker instance) to `/home/zetaris/customer-data/shared/whytrail/taxi_daily`; rows with
  out-of-range timestamps were dropped (`taxi_daily_clean`) and the table is registered on the clean copy.
  2,002 rows, 2024-04-01 .. 2025-02-28, 38,539,715 trips, 6 boroughs.
- `NOAA_GHCN.central_park_daily`: NOAA GHCN-Daily station USW00094728 (Central Park), official CSV from
  `s3://noaa-ghcn-pds/csv/by_station/` (CC0), pivoted to one row per day (prcp_mm, snow_mm, tmax_c, tmin_c);
  402 rows, through 2025-02-05. Registered as a new Lightning database `NOAA_GHCN`.
- `REF.us_holidays`: US federal holidays (OPM list), 22 rows, new Lightning database `REF`.
- Built-in sample tables were not modified. Each template query runs in 0.4-2.3 s on the aggregate tables.
- Questions outside the data range return a `NoDataError` that names the available range (no crash).

## Status

Done: orchestrator, five agents, Zetaris REST client + mock, World Model, H-MEM, injected generator (Claude + mock),
CLI, `POST /run`, JSONL traces, live Zetaris instance, live table mapping, NoDataError handling, 18 unit tests,
live Claude run on sample data.
- Live end-to-end run on local Zetaris (Docker) + Claude — see logs/trace-run-b5402d4e.jsonl

Next: web UI, NVIDIA Nemotron for the Skeptic, Docker image test, demo video, slide deck.

## How we built it

- **Cursor** is the development environment ([`.cursor/rules/whytrail.mdc`](.cursor/rules/whytrail.mdc)); commits tagged
  `[cursor]` were made by Cursor Agent, except the skeleton commit and a7c2a96, which Claude Code wrote from the terminal.
- Zetaris integration follows `zetaris/getting-started` (`docs/connections/client-setup.md`, `scripts/zetaris_sql.py`);
  Meterless engines follow the Meterless README and the World Model / H-MEM engine documents.

## Pre-existing components

None. All code was written after the build window opened (2026-10-22 00:00 UTC).

## License

Apache-2.0

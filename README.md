# WhyTrail

**An agent that explains why a metric changed, and shows the query behind every number.**

Open Agent Hackathon 2026, **Track 02: The Agent That Can Explain Why**.

An operations analyst asks: *"Why did Manhattan taxi trips drop in the week of 2025-01-20 compared with the week of 2025-01-13?"*
WhyTrail does not guess. It lists the competing explanations, tries to **falsify** each one with a query against
federated data in Zetaris, has a Skeptic attack the survivors and choose the follow-up test that gets run, has a
Verifier re-derive the two headline totals (period A and period B) through an independent query, and only then writes
the answer. Every number in the answer comes from a Zetaris result stored in the World Model with its SQL, source
tables, row count and result hash; the Verifier's second, independent derivation covers the two totals.

Judges: `pip install -r requirements.txt && python -m whytrail --mock` runs the whole loop offline in a few seconds,
no keys ([Path A](#path-a--offline-judge-run-no-keys)).

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
  Skeptic ........ for each supported hypothesis: strongest alternative + the follow-up test to run,
              |      chosen from a fixed catalog (queries.FOLLOWUP_TESTS); unknown names are logged and rejected
              |      --> Retriever runs exactly the requested tests, each traced as Skeptic.followup_executed:
              |          isolate_daytype   : federated join NY_TAXI x NOAA_GHCN x REF, weekday-matched attribution
              |          baseline_variation: total of the period before A; is the change within ordinary variation?
              |      (a hypothesis that does not survive is downgraded: needs_revision; "ordinary variation" cuts confidence)
              v
  Verifier ....... re-derives the two headline totals (period A, period B) from raw rows via a second Zetaris query;
              |      a mismatch blocks the Writer and replaces the number. Other numbers are not re-derived.
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
- Three sources are addressed with qualified `SOURCE.table` names, and the Skeptic's `isolate_daytype` follow-up is a
  **federated join across all three** (`daily_joined` in [`whytrail/queries.py`](whytrail/queries.py)).
- The Verifier queries Zetaris a second time with a different formulation (raw rows, summed client-side) to check
  the two headline totals the Writer cites (period A and period B). Per-hypothesis, attribution and follow-up numbers
  are not re-derived; each carries its SQL, source tables, row count and result hash in the why-trail.
- The dataset behind the live run is reproducible from [`scripts/zetaris/`](scripts/zetaris/), see
  [Reproducing the Zetaris dataset](#reproducing-the-zetaris-dataset).
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

## Quickstart

Requirements: Python 3.11+ (tested on 3.14). Both paths start the same way:

```bash
git clone https://github.com/run58669-maker/whytrail && cd whytrail
pip install -r requirements.txt
```

### Path A — offline judge run (no keys)

```bash
python -m whytrail --mock
```

`--mock` (same as `SAMPLE_MODE=true`) replaces Zetaris with SQLite databases attached under the same source names
(synthetic January 2024 data, [`whytrail/sample_data.py`](whytrail/sample_data.py)) and the model with a deterministic
offline generator. No `.env`, no network. Expected output (trace lines go to stderr, the answer to stdout; fact ids are
deterministic, so yours should match):

```
[INFO] run_id=run-... Orchestrator.start: H-MEM hydrated 0 memories
[INFO] run_id=run-... Retriever.zetaris_query: 1 rows via mock-sqlite -> f_166341bb4c
...
[INFO] run_id=run-... Skeptic.request_followup: 2 challenges
[INFO] run_id=run-... Skeptic.followup_executed: day-type attribution: holiday, snow, other
[INFO] run_id=run-... Verifier.check: metric_a: cited 599,892 vs re-derived 599,892 -> ok

=== WhyTrail answer ===
Manhattan trips changed -7.5% (599,892 -> 554,691) from 2024-01-08..2024-01-14 to 2024-01-15..2024-01-21.
Why: holiday days (2024-01-15) account for 24,380 fewer trips (50% of the gross loss); snow days (2024-01-16,
2024-01-19) account for 24,333 fewer trips (50% of the gross loss). Ordinary days changed by +3,512 trips ...
Ruled out: H_price (...), H_data_gap (...), H_local (...). This conclusion would be overturned if ...

confidence=0.95  supported=['H_weather', 'H_holiday']

=== why-trail ===
- H_weather -> supported: ... snow 0mm -> 46mm ...; Skeptic follow-up: snow days explain 50% of the drop  [evidence f_6b9639dc2b, f_594c44c18b]
- H_price -> refuted: ... fare per trip $19.48 -> $19.55  [evidence f_6a7f0691dd, f_fbb4fef5e0]
...
- verify:metric_b: 554,691 | re-derived 554,691 from raw rows -> ok  [evidence f_3e4ec4f56f]

zetaris_queries=20 model_calls=3 cost=$0.0 world_facts=20 hmem_records=7 seconds=0.08
trace: logs/trace-run-....jsonl
world: logs/world-run-....jsonl
```

Run the same command a second time: H-MEM now holds the schema and the refuted verdicts, so the run needs
**10 queries instead of 20** (`Planner.hmem_retrieve: schema from memory ...; skipped 4 discovery queries`).
Other offline checks: `python -m whytrail --mock --json` (full JSON result), a different borough and direction
(`python -m whytrail --mock "Why did Brooklyn taxi trips rise in the week of 2024-01-22 compared with the week of 2024-01-15?"`),
and the test suite, `python -m pytest` (21 tests, offline, about 2 s).

### Path B — live (Zetaris + Claude)

Needs a Zetaris instance that has the three tables (local Docker platform plus
[Reproducing the Zetaris dataset](#reproducing-the-zetaris-dataset)) and an Anthropic API key.

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY, ZETARIS_REST_URL, ZETARIS_ORG_ID, ZETARIS_API_KEY; leave SAMPLE_MODE=false
python -m whytrail
# or with your own question:
python -m whytrail "Why did Manhattan taxi trips drop in the week of 2025-01-20 compared with the week of 2025-01-13?"
```

The CLI and `run.py` read `.env` themselves (stdlib loader in [`whytrail/env.py`](whytrail/env.py): `.env` in the
current directory or the repo root; variables already exported in the shell win). Expected output, abridged from the
recorded run [`examples/run5_live_zetaris_claude.txt`](examples/run5_live_zetaris_claude.txt):

```
[INFO] run_id=run-b5402d4e Retriever.zetaris_query: 1 rows via zetaris-rest -> f_4c6f8c5710
[INFO] run_id=run-b5402d4e Planner.generate: model=claude-opus-5-5 ok
...
[INFO] run_id=run-b5402d4e Verifier.check: metric_b: cited 747,921 vs re-derived 747,921 -> ok

=== WhyTrail answer ===
Manhattan taxi trips fell 1.42%, from 758,672 in the week of 2025-01-13 to 747,921 in the week of 2025-01-20.
The drop is explained by Martin Luther King Jr. Day on 2025-01-20 ...

zetaris_queries=20 model_calls=3 cost=$0.0565 world_facts=20 hmem_records=7 seconds=34.28
```

A question outside the data range answers
`ERROR NoDataError: no NY_TAXI.taxi_daily rows for Manhattan in ...; available data: 2024-04-01..2025-02-28.`
Half-way option: `python -m whytrail --mock-data "<question>"` runs live Claude on the synthetic data (only
`ANTHROPIC_API_KEY` needed).

### HTTP API

```bash
python run.py                      # reads .env too; PORT (default 8000)
# Sample (synthetic January 2024), no keys
curl -X POST localhost:8000/run -H 'content-type: application/json' \
  -d '{"run_id":"demo-001","input":{"query":"Why did Manhattan taxi trips drop in the week of 2024-01-15 compared with the week of 2024-01-08?"},"options":{"sample_mode":true}}'
# Live (real data)
curl -X POST localhost:8000/run -H 'content-type: application/json' \
  -d '{"run_id":"demo-002","input":{"query":"Why did Manhattan taxi trips drop in the week of 2025-01-20 compared with the week of 2025-01-13?"},"options":{"sample_mode":false}}'
```

CLI flags: `--mock`, `--mock-data`, `--json` (full result), `--reset-memory`, `--memory PATH`, `--log-dir DIR`,
`--max-cost USD`, `--quiet`. With no question the CLI picks the default for its mode (2024-01 sample question with
`--mock` / `--mock-data`; the 2025-01 live question otherwise).

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

### Reproducing the Zetaris dataset

Source dataset: the built-in `NY_TAXI` sample that ships with the local Zetaris platform (`zetaris-platform.zip`,
registered on first start from `s3a://zetaris-public-dataset`): trip-level `NY_TAXI.yellow_taxi` and
`NY_TAXI.taxi_zones`. Resulting range of `NY_TAXI.taxi_daily`: **2024-04-01 .. 2025-02-28**. The scripts are in
[`scripts/zetaris/`](scripts/zetaris/); every SQL statement goes through
[`scripts/zetaris/zsql.py`](scripts/zetaris/zsql.py), the same REST endpoint the agent uses (key from `.env`,
never printed).

1. **Daily aggregate (the slow step).**
   `python scripts/zetaris/zsql.py --file scripts/zetaris/01_taxi_daily_export.sql 3600` runs one
   `RUN EXPORT TO STORAGE` that groups `yellow_taxi` by pickup date x borough (pickup zone -> borough via
   `taxi_zones`) and writes CSV to `/home/zetaris/customer-data/shared/whytrail/taxi_daily` inside the driver
   container. One full scan: **1,943 s (32 min)** on a laptop Docker instance with 8 GB / 4 CPUs.
2. **Clean copy.** `docker exec zetaris-driver-1 sh -s < scripts/zetaris/02_clean_taxi_daily.sh` keeps the rows
   dated 2024-04-01..2025-02-28 (the sample has a few trips with timestamps outside its real range) as
   `taxi_daily_clean/taxi_daily.csv`: header + 2,002 rows, 38,539,715 trips, 6 boroughs.
3. **Reference tables.** `curl -O https://noaa-ghcn-pds.s3.amazonaws.com/csv/by_station/USW00094728.csv`
   (NOAA GHCN-Daily, Central Park, CC0; the file grows daily) then
   `python scripts/zetaris/prep_reference_tables.py --noaa USW00094728.csv --out out/` pivots it to one row per
   day (402 rows, 2024-01-01..2025-02-05 on 2026-10-22) and writes the 22-row US federal holiday table (OPM list,
   2024-2025). `docker cp` both folders into the driver container (commands at the top of `03_register_tables.sql`).
4. **Register and check.** `python scripts/zetaris/zsql.py --file scripts/zetaris/03_register_tables.sql 300`
   creates the `NOAA_GHCN` and `REF` Lightning databases, registers the three CSV file-store tables
   (`taxi_daily` inside the existing `NY_TAXI` database) and runs three readiness counts; the expected values are
   in the file. The first query on a fresh table takes 1-2 min (schema inference), later queries 0.4-2.3 s.

Not in the repository: the exported CSVs (they live in the Docker volume and are rebuilt by steps 1-3) and the API
key (create it in the Zetaris UI, Management -> API Tokens, then put it in `.env`). Steps 2-4 take about five minutes;
step 1 is the only slow one. The holiday list is typed into `prep_reference_tables.py`; the NOAA pivot was
re-run offline against the same source file on 2026-10-24 and reproduced the loaded CSV byte for byte.

## Status

Done: orchestrator, six agents (Planner, Retriever, Analyst, Skeptic, Verifier, Writer), Zetaris REST client + mock,
World Model, H-MEM, injected generator (Claude + mock), CLI, `POST /run`, JSONL traces, `.env` loading, live Zetaris
instance, live table mapping, dataset reproduction scripts, Skeptic follow-up catalog (the Skeptic's requested test is
executed and traced), NoDataError handling, 21 unit tests, live Claude run on sample data.
- Live end-to-end run on local Zetaris (Docker) + Claude — see logs/trace-run-b5402d4e.jsonl (recorded before the
  follow-up catalog existed: its Skeptic requested `isolate_daytype`, the only test then available).

Known limits: the Verifier re-derives the two headline totals only; the question parser is fixed to taxi trips, a
borough and two ISO-dated periods; a model failure falls back to the offline generator and is marked `fallback` in the
trace rather than failing the run.

Next: web UI, NVIDIA Nemotron for the Skeptic, Docker image test, demo video. Slide deck: see [Submission](#submission).

## How we built it

- **Cursor** is the development environment ([`.cursor/rules/whytrail.mdc`](.cursor/rules/whytrail.mdc)); commits tagged
  `[cursor]` were made by Cursor Agent, except the skeleton commit and c5b5ebe, which Claude Code wrote from the terminal.
- Zetaris integration follows `zetaris/getting-started` (`docs/connections/client-setup.md`, `scripts/zetaris_sql.py`);
  Meterless engines follow the Meterless README and the World Model / H-MEM engine documents.

## Pre-existing components

None. All code was written after the build window opened (2026-10-22 00:00 UTC).

## Submission

- Slide deck: `docs/deck/WhyTrail-deck.pdf` (source `docs/deck/index.html`, 9 slides, 16:9; export with
  `chrome --headless=new --disable-gpu --no-pdf-header-footer --print-to-pdf=docs/deck/WhyTrail-deck.pdf file:///<repo>/docs/deck/index.html`)
- Repo: https://github.com/run58669-maker/whytrail
- Metadata: [`metadata.json`](metadata.json)

## License

Apache-2.0

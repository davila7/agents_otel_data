# dataset/ — synthetic OTLP corpus for query-engine benchmarking

The existing benchmark cohorts (`evaluation/results/*.json`) query projects that
hold only the three demo traces, so every latency number is dominated by HTTP
round-trip, not by the platform's query engine. This directory fixes that: it
generates a **deterministic synthetic corpus of OTLP trace data**, sends the
**identical bytes** to every platform, and gates on parity — so a future query
benchmark over this corpus measures the query engine on a non-trivial dataset,
with every platform holding exactly the same data.

> **Platform quirk (verified live 2026-08-13):** LangSmith's OTLP ingest rejects
> spans whose `start_time` falls outside ±24 hours of ingest time (HTTP 422), so
> it cannot receive the shared multi-day corpus. It gets a same-seed corpus
> regenerated with `--days 1` and a fresh `--anchor` (identical ids/attributes,
> compressed timestamps), sent immediately after generation:
> `generate.py --spans 10000 --seed 42 --days 1 --anchor <now> --out ./out_langsmith`
> then `send.py --platforms langsmith --manifest out_langsmith/manifest.json`.
> This is itself a data-portability finding: "record once, replay later" does
> not work against LangSmith.

## Pipeline

```bash
cd dataset && uv sync

# 1. generate — deterministic corpus + manifest (no network)
#    --anchor is REQUIRED for reproducibility: without it the anchor defaults
#    to midnight UTC of "today", so the same seed gives different bytes on
#    different days.
uv run python generate.py --seed 42 --anchor 2026-08-12T00:00:00Z --out ./out
#    writes ./out/chunk_*.pb (serialized ExportTraceServiceRequest batches;
#    --format json emits a chunk_*.json debug dump instead)
#    and ./out/manifest.json (span/trace counts, seed, time anchor)

# 2. send — replay the same bytes to each platform's OTLP endpoint
#    (endpoints/headers from platforms.json; creds resolved from
#    ../<platform>/.env, gitignored — use --dry-run to validate first)
uv run --with requests python send.py \
    --platforms logfire,braintrust,langsmith,langfuse,phoenix \
    --manifest ./out/manifest.json

# 3. verify — parity gate: does each platform actually hold the corpus?
uv run --with requests python verify_parity.py \
    --platforms logfire,braintrust,langsmith,langfuse,phoenix \
    --manifest ./out/manifest.json --tolerance 0.01
# exit 0 only if every platform is within tolerance; writes ./out/parity_report.json
```

## Determinism

Generation is fully reproducible from two inputs recorded in the manifest:

- **seed** — a single PRNG seed drives trace shapes, attribute values, token
  counts, durations, and error injection. Same seed → byte-identical corpus.
- **anchor** — all span timestamps are laid out relative to a fixed anchor
  timestamp, not `now()`. Regenerating with the same seed and anchor gives the
  same timestamps, so time-window queries are reproducible too. If `--anchor`
  is omitted it defaults to midnight UTC of the current day — the one
  nondeterministic input — so always pass it explicitly when byte-identical
  output matters.

Two expectations to calibrate before asserting on a run:

- **`--spans` is a soft budget** — the last trace that would exceed it is
  dropped, so runs produce slightly fewer spans than requested (e.g. 399/400,
  9994/10000). Always read `manifest.total_spans`; never hardcode the
  requested number in a parity check.
- **the 60/25/15 chat/tools/mcp scenario mix holds only in distribution** —
  it converges at corpus scale (within ~1pp at 10k spans) but small pilot runs
  can land well outside a ±5pp band from sampling variance alone (a 400-span
  run measured 51.7/32.6/15.7). Don't assert the mix on small corpora;
  `manifest.scenario_counts` records the actual mix of each run.

`out/manifest.json` is the ground truth for everything downstream: total span
count, total trace count, seed, anchor time bounds, and per-platform project
names. `verify_parity.py` reads its bounds and counts from there — nothing is
hardcoded twice.

## Fairness: identical bytes

`send.py` serializes each batch **once** and replays the same
`ExportTraceServiceRequest` bytes to every platform's OTLP/HTTP endpoint. No
per-platform SDK, no per-platform re-instrumentation, no attribute mapping
differences at the source. If a platform stores something different, that is
the platform's ingest pipeline — which is exactly what the parity gate exists
to surface.

Endpoints and headers live once in `platforms.json` (secrets only as
`${env:VAR}` placeholders, resolved from the gitignored per-platform `.env`
files). `collector.yaml` mirrors the same endpoints/headers for an
OpenTelemetry Collector-based send path; the two files must stay in sync.
`send.py --dry-run` validates config and credential presence (printing only
env var names, never values) without any network I/O.

## Project isolation

The corpus must not pollute the demo projects used by the frozen evaluations,
and the demo traffic must not pollute the corpus counts:

| Platform   | Isolation mechanism |
|------------|---------------------|
| Logfire    | Same project, separate `service_name = dataset-pilot` (queries filter on it) |
| Braintrust | Separate project `otel-dataset-pilot` |
| LangSmith  | Separate project (session) `otel-dataset-pilot` |
| Langfuse   | Project is fixed by API key pair; isolation via the manifest time window (and marker attributes on every span) |
| Phoenix    | Separate project `otel-dataset-pilot` |

## Parity gate (`verify_parity.py`)

Counts what each platform's **read API** holds and compares against the
manifest. Exit 0 only if every checked platform is within `--tolerance`
(default 1%) of the manifest span count. Reuses the verified read-API
mechanics from `evaluation/eval_*.py` (Arrow-vs-JSON Accept header on Logfire,
per-platform rate limits, cursor pagination, Braintrust fetch dedupe).

What one counted row equals, per platform:

| Platform   | Counted unit | Notes |
|------------|--------------|-------|
| Logfire    | span (`records` row) | single SQL `count(*)`, no pagination needed |
| Braintrust | span-level `/fetch` event | deduped by `id` (pagination can re-return rows); trace ≈ distinct `root_span_id` |
| LangSmith  | run | OTLP ingest maps spans 1:1 to runs; the report includes root-run count vs manifest trace count as the audit of that assumption |
| Langfuse   | observation | trace count reported as distinct `traceId` |
| Phoenix    | span | direct |

Big-dataset safe: page sizes 500–1000 (LangSmith capped at 100 by its
`/runs/query` API, so budget ~10× more pages and `--max-pages` headroom for
it: a 100k-span corpus needs 1000 LangSmith pages at 1.5 s pacing, ~25 min,
which exceeds the default `--max-pages 500`), progress every 10 pages, per-platform
pacing (Logfire ~10 q/min, LangSmith 10 req/10 s), and a `--max-pages` hard
cap. Output: `out/parity_report.json` with
`{platform: {expected, found, pct, method, wall_seconds}}` plus a table on
stdout.

## Free-tier ingest quotas (TODO: verify before scaling up)

Corpus size must fit the *smallest* free-tier ingest quota or parity is
structurally impossible. Verify these against current pricing pages before
generating a large corpus — all values below are unverified placeholders:

| Platform   | Free-tier ingest limit | Status |
|------------|------------------------|--------|
| Logfire    | TODO-verify (metered by spans/month) | unverified |
| Braintrust | TODO-verify (metered by processed data/rows) | unverified |
| LangSmith  | TODO-verify (metered by traces/month, base vs extended retention) | unverified |
| Langfuse   | TODO-verify (metered by units/observations per month) | unverified |
| Phoenix    | TODO-verify (Phoenix Cloud metered by spans and retention) | unverified |

## Hard rule: frozen evidence stays frozen

Nothing in `dataset/` reads from or writes to `evaluation/results/*.json` or
any frozen cohort. The July and August evidence files and
`final_scores*.json` are untouched by this pipeline. Query-performance
benchmarking over this corpus is a **future, separately-cohorted eval** — it
will follow the rubric's cohort rules (all platforms re-run back-to-back, new
result files, never mixed into an existing cohort).

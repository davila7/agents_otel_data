# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this repo is

An AI-agent observability benchmark: the same three traced scenarios are written into five OTEL-based platforms (Logfire, Braintrust, LangSmith, Langfuse, Arize Phoenix), then each platform's **read API** is benchmarked with executable scripts and scored by a judge panel. A separate synthetic 10k-span corpus (`dataset/`) feeds a query-engine benchmark. `README.md` has the verdict; `evaluation/RESULTS.md` has the full methodology and caveats; the dashboard lives at https://davila7.github.io/agents_otel_data/.

## Layout and conventions

- `logfire/`, `braintrust/`, `langsmith/`, `langfuse/`, `phoenix/` — self-contained `uv` projects, each with the same three demos: `01_messages.py` (direct Anthropic call), `02_tools.py` (Pydantic AI agent + tools), `03_mcp.py` (Pydantic AI + `mcp-server-time` over stdio).
- `evaluation/` — one `eval_<platform>.py` per read API, `rubric.md` (9 weighted criteria, weights sum to 100), `RESULTS.md`, `results/*.json` (raw evidence), `rescore_continuous.py` (sensitivity check), `presentation.html` (self-contained report).
- `dataset/` — synthetic OTLP corpus pipeline: `generate.py` (deterministic, seeded), `send.py` + `platforms.json` (byte-identical fan-out), `collector.yaml` (OTel Collector alternative), `verify_parity.py` (count gate via each read API), `query_bench.py` (query-engine benchmark). `dataset/out*/` is gitignored evidence.
- `docs/` — the GitHub Pages site (served from `main:/docs`): `index.html` (Arena: cohort scores, score anatomy, heatmap) and `query_bench.html`. Chart data is transcribed into the HTML — every number must be audited against `evaluation/results/*.json` / `dataset/out_v2/query_bench.json` before committing.
- All docs and deliverables in **English**.
- `cryptography` is pinned `<45` in every folder: newer versions need a rust toolchain to build on Intel macOS. Avoid adding `temporalio` (no Intel-macOS wheels).

## Running things

```bash
# demos
cd <platform> && uv sync && set -a && source .env && set +a
uv run 01_messages.py

# benchmark (each eval sources its platform's .env)
cd evaluation
set -a && source ../logfire/.env && set +a
uv run --with requests python eval_logfire.py   # writes results/logfire.json
# EVAL_RESULTS_PATH=<path> redirects output (cohort runs; never overwrite frozen files)

# sensitivity re-scoring (pure recompute, no API calls)
python3 rescore_continuous.py                   # --cohort jul2026|aug2026|aug12_2026|aug13_2026

# synthetic corpus (see dataset/README.md; sends are the user's call)
cd dataset
uv run python generate.py --spans 10000 --seed 43 --days 0.5 --anchor <now-UTC> --out ./out_vN
uv run python send.py --platforms ... --manifest out_vN/manifest.json --dry-run
uv run python verify_parity.py --platforms ... --manifest out_vN/manifest.json
uv run python query_bench.py --manifest out_vN/manifest.json

# local dashboard preview
cd docs && python3 -m http.server 8765
```

## Credentials — critical rules

- All secrets live in per-folder `.env` files, **gitignored**. Never commit keys, never print secret values from scripts (mask if needed).
- Logfire: the real project is **davila7/ant-agent**. Read access requires a **pylf_v2 API key with read scope** (`LOGFIRE_READ_TOKEN` in `logfire/.env`); write tokens are rejected by the query API. `LOGFIRE_TOKEN` in `logfire/.env` is a write token for OTLP ingest (minted 2026-08-12 via `logfire projects use ant-agent`, which also re-linked `.logfire/logfire_credentials.json` to ant-agent — it previously pointed at a deleted project). The `logfire read-tokens create` CLI subcommand is broken (AttributeError); read tokens come from the UI.
- Braintrust: project `agents-otel-data`, id `5d169ed6-af7e-4dbd-a8ca-458253acbfe8`. Traces appear in that project, not "My Project".
- Credentials decide benchmarks: the first eval run scored Logfire 6.67/100 purely because the wrong credential type was provisioned.

## Read-API gotchas (verified live, July–August 2026)

- **Logfire** `POST https://logfire-us.pydantic.dev/v2/query`: returns **Arrow binary by default** despite docs — always send `Accept: application/json`. Params go in a **JSON body** (`{"sql": ..., "min_timestamp": ...}`); URL params get HTTP 415. `min_timestamp` is required (422 otherwise). Responses cap at 100 rows unless a `limit` body param is set. Rate limit ~10 queries/min. Management API: `https://api-us.pydantic.dev/api/v1/`.
- **LangSmith** `POST /v2/runs/query` and `POST /v2/traces/query` (base `api.smith.langchain.com`, no `/api` prefix on v2): auth via `X-Api-Key` + `X-Tenant-Id`; resolve project UUID and tenant id via `GET /api/v1/sessions?name=...`; body uses `project_ids`/`selects`/`page_size`, filter DSL like `eq(name, "...")`, cursor via `next_cursor`; responses key on `items` (v1 used `runs`). **The deprecated v1 runs query is ~7–15x slower on identical scenarios — never benchmark through it.** `/api/v1/runs/stats` remains the only server-side aggregate (fixed shape). Rate limit: v1 measured ~10 req/10 s; the v2 export sustained ~3.3 req/s without 429s (docs claim higher, unverified).
- **Braintrust** `GET /v1/project_logs/{id}/fetch` + `POST /btql`: Bearer auth; fetch pagination can re-return rows — dedupe by `id`; `/fetch` has **no server-side time filter** — separate coexisting corpora client-side via `metrics.start`. Resource attributes are NOT visible in fetch rows — isolation must be by project.
- **Langfuse** `GET /api/public/v2/observations` (v1 `/api/public/traces` is deprecated): HTTP Basic (pk-lf / sk-lf); cursor pagination via `meta.cursor`; selective field groups via `?fields=` (I/O needs explicit opt-in); rows are observations — group by `traceId` to reconstruct traces, but `traceName` is **null on non-root rows** (name-based filtering of whole traces is unreliable). Ingest lag fluctuates: 46.5 s (v1, Jul) → 10.7 / 6.4 / 43.5 s across 2026 cohorts.
- **Phoenix** (Cloud space `dan-avila7`, project `agents-otel-data`): Bearer auth with `PHOENIX_API_KEY`; base `https://app.phoenix.arize.com/s/dan-avila7`; the space serves its own OpenAPI spec at `/openapi.json`. `GET /v1/projects/{id}/spans` has time/name/attribute/span_kind filters + cursor pagination (limit ≤ 1000) but no aggregation or SQL/DSL; `/spans/otlpv1` returns OTLP-JSON. Traces reconstruct by grouping on `context.trace_id`.
- **mcp-server-time** requires `--with 'mcp<2'` in the uvx invocation (imports `McpError`, removed in mcp 2.x) — already pinned in the five `03_mcp.py`.

## OTLP ingest gotchas (raw OTLP/HTTP, verified live Aug 2026)

- Endpoints/headers per platform are codified in `dataset/platforms.json` (env-var placeholders only).
- **LangSmith rejects spans with `start_time` outside ±24 h of ingest** (HTTP 422) — no historical backfill; generate `--days ≤ 1` corpora anchored at send time.
- **Logfire also enforces a ~24 h ingest window, but it is NOT silent** (issue pydantic/logfire#2242 was wrong — verified live 2026-08-13): mixed batches return HTTP 200 with `partial_success.rejected_spans` + a detailed error message, all-old batches return HTTP 422, and an error span is injected into the project per rejected payload. Our tooling missed all three signals: `send.py` originally checked only status codes, and `OTLPSpanExporter` returns `SpanExportResult.SUCCESS` on any 200 without surfacing `partial_success`. **Always parse OTLP response bodies before claiming a backend is silent.**
- **Langfuse OTLP ingest needs `x-langfuse-ingestion-version: 4`** or data lands in the legacy path, invisible to the v2 observations API. Phoenix ingest is protobuf-only (JSON → 415) and routes projects via the `openinference.project.name` resource attribute.

## Synthetic corpus — state and invariants

- Corpus data is marked: `service.name='dataset-pilot'`, `dataset.synthetic=true`, root spans named `synthetic-*` (deliberately NOT the demo names). Braintrust/LangSmith/Phoenix hold it in separate `otel-dataset-pilot` projects; **Logfire and Langfuse hold it in the same project as the benchmark demos** (credentials are project-bound).
- Because of that, `eval_logfire.py` (SQL `SYNTH_EXCL` predicate + deterministic tool-marker fetch) and `eval_langfuse.py` (deterministic fetch-by-name; **no recency fallback** — a synthetic trace can imitate a demo) carry contamination guards that must be preserved. Never let completeness evidence come from a synthetic trace.
- `generate.py` is deterministic (seed + explicit `--anchor`): same args → byte-identical chunks. Integer-day output must stay stable across code changes.
- `verify_parity.py` is a gate: every platform within 1% of the manifest before any query benchmarking.

## Scoring architecture — keep these invariants

- `results/final_scores.json` is the frozen judge-panel output (banded rubric). **Never edit it**; sensitivity analyses live alongside it (`final_scores_continuous.json`), regenerated by `rescore_continuous.py`.
- July raw evidence (`results/{platform}.json`) is frozen too. Re-runs go to new files: `results/*_aug2026.json` + `results/langfuse_v2.json` (Aug 3 cohort); `results/*_aug12_2026.json` + `results/phoenix.json` (Aug 12, Phoenix joins; its categorical scores from `results/phoenix_judges.json`, a fresh 3-judge panel); `results/*_aug13_2026.json` (Aug 13, LangSmith v2 migration). If anything changes measurement conditions (vendor improvement, endpoint migration, new platform), re-run **all five** evals back-to-back — rubric rule 2 forbids mixing sessions in one cohort.
- Categorical criteria keep judge averages across cohorts; verifiable API changes get **mechanical rubric-anchor overrides** declared in the cohort config (langfuse v2: pagination 10 / otel-fidelity 9 / query-flex 8; langsmith v2: dx-friction 7) — flagged as provisional, never silently.
- `rescore_continuous.py` re-scores only latency and freshness (log-scale, cohort min-max, lower is better). Full-precision arithmetic, rounding **only at display** — intermediate rounding caused ±0.01 drift once.
- Latency methodology: 3 timed identical calls, median, timer wraps the full HTTP round trip, 429s/pacing outside the timed window. Freshness readings of "0.1 s" are bounded by the poll loop's first iteration — always carry that caveat.
- Honest-framing rules (learned via adversarial review): continuous totals are cohort-normalized, so the leader on both measured metrics banks two 10s and can flip the podium (Phoenix leads Aug 12/13 this way while trailing Logfire by ~11 points on judge-scored criteria — present podiums as profiles/clusters, with the categorical-vs-measured decomposition, never as coronations). Cohort membership changes the normalization. The query bench and the arena measure different things (engine under load vs round-trip) — don't blend their claims.
- Every number in RESULTS.md/README/docs must trace to a value in `results/*.json` or `dataset/out*/query_bench.json` — no doc-only claims, and no citing measurements whose evidence file was overwritten.

## Publishing

- Repo: `davila7/agents_otel_data`. Use `gh` for PRs (`gh pr edit` lacks scopes here — PATCH via `gh api` instead). The repo runs the "cubic · AI code reviewer" check on PRs; read its comments — its evidence-integrity catches have been valid.
- The dashboard deploys via GitHub Pages from `main:/docs` — merging to main IS the deploy. Preview locally with `python3 -m http.server 8765` in `docs/`.
- The older results presentation is a Claude artifact from `evaluation/presentation.html` — republish to the same URL rather than creating a new artifact.

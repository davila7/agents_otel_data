# Agents OTEL Data

AI agent observability, tested from both sides: **write identical traces into five OpenTelemetry-based platforms, then benchmark how well each one gives the data back through its read API.**

Every platform folder is a self-contained `uv` project running the same three scenarios (a direct Anthropic call, a tool-calling Pydantic AI agent, and an MCP-powered agent). The `evaluation/` folder holds an executable, evidence-only benchmark of the five read APIs, scored by a multi-agent judge panel.

📊 **[Live dashboard](https://davila7.github.io/agents_otel_data/)** — all cohorts, criteria heatmap, and raw measurements.

## The verdict

Latest results (August 12, 2026 — Arize Phoenix joins, so all five read APIs were measured back-to-back in the same session, continuous scoring):

| Rank | Platform | Score / 100 | What decided it |
|------|----------|------------:|-----------------|
| 🥇 | [Phoenix](./phoenix) (Arize) | **87.27** | Fastest read path (87.5 ms) and write-to-read lag (0.1 s) measured in any cohort, verbatim `gen_ai.*` + OTLP-JSON export, one self-served OpenAPI spec; weakest query surface of the top three (no server-side aggregation or SQL) |
| 🥈 | [Logfire](./logfire) (Pydantic) | 87.11 | Arbitrary SQL over raw spans, `gen_ai.*` returned verbatim, 4 export formats actually delivered (JSON/NDJSON/CSV/Arrow) |
| 🥉 | [Braintrust](./braintrust) | 84.53 | BTQL free query language, 0.5 s write-to-read, best error messages |
| 4 | [Langfuse](./langfuse) | 79.66 | Sub-140 ms retrieval on its observations v2 API and cursor pagination; held back by 6.4 s ingest lag and JSON-only export |
| 5 | [LangSmith](./langsmith) | 67.25 | Full trace completeness and cursor pagination, but no free query language and the slowest retrieval of the session (1.9 s) |

Key facts behind the numbers:

- **The podium is a cluster, not a coronation.** Phoenix edges Logfire by 0.16 points — far inside this benchmark's method dependence — and its lead comes exactly where cohort normalization is most sensitive (it holds the best value on both continuous metrics, so it banks two perfect 10s). The durable reading: Phoenix wins on speed and standards fidelity, Logfire on analysis power and export openness, Braintrust is the all-rounder.
- **Langfuse made the biggest jump of any incumbent** — from a distant 4th (65.63) to a stable 3rd–4th after shipping its [observations v2 API](./evaluation/RESULTS.md#august-2026-re-run-langfuse-observations-v2): retrieval latency 1293 → ~140–200 ms, ingest lag 46.5 → ~6–10 s, offset → cursor pagination.
- The original July judge-panel scores (Logfire 96.40, Braintrust 89.40, LangSmith 80.93, Langfuse 72.03) remain the frozen record in [`evaluation/RESULTS.md`](./evaluation/RESULTS.md), together with the full methodology, rubric, per-judge scores, [sensitivity checks](./evaluation/RESULTS.md#sensitivity-check-continuous-scoring-for-measured-metrics), and caveats. Phoenix's categorical criteria were scored by [its own 3-judge panel](./evaluation/results/phoenix_judges.json) against captured evidence.
- One caveat worth repeating: in the first run Logfire scored dead last (6.67) simply because the wrong credential type was provisioned — its query API requires a read-scope key. **Credentials decide benchmarks.**

## How the benchmark works

1. **Instrument** — each platform folder runs the same three demo scripts, so all five backends hold equivalent traces:
   - `01_messages.py` — direct Anthropic API call (native SDK integration per platform)
   - `02_tools.py` — Pydantic AI agent with tool calling (`Agent.instrument_all()` → OTEL)
   - `03_mcp.py` — Pydantic AI agent driving an MCP server (`mcp-server-time` over stdio)
2. **Retrieve** — one standalone script per platform (`evaluation/eval_*.py`) exercises the live read API: a 10-field trace-completeness checklist, filters, server-side aggregation, pagination (a real second page), export formats, retrieval latency, and write-to-read lag.
3. **Verify** — an adversarial pass re-runs every script from scratch and curl-checks sampled claims; only reproduced numbers count.
4. **Judge** — three independent judges (data-engineer, platform-operator, and AI-engineer lenses) score the evidence against a 9-criterion weighted rubric ([`evaluation/rubric.md`](./evaluation/rubric.md)). Documented-but-untested capabilities are capped: the benchmark measures what an engineer actually gets, from code, with ordinary credentials.

## Repository layout

```
logfire/      Pydantic Logfire     — native SDK; read via SQL Query API (/v2/query)
langfuse/     Langfuse             — SDK + AnthropicInstrumentor; read via /api/public REST
langsmith/    LangSmith            — wrap_anthropic + OTLP; read via /runs/query DSL
braintrust/   Braintrust           — wrap_anthropic + OTLP; read via fetch + BTQL
phoenix/      Arize Phoenix        — arize-phoenix-otel + OpenInference; read via REST /v1
evaluation/   Executable benchmark — eval_*.py, rubric.md, RESULTS.md, results/*.json,
              presentation.html (self-contained HTML report)
```

## Running the demos

Each folder is independent. The pattern is always:

```bash
cd <platform>
uv sync
# put credentials in .env (see the folder's README for the exact variables)
set -a && source .env && set +a
uv run 01_messages.py
uv run 02_tools.py
uv run 03_mcp.py
```

Then check the platform's dashboard — each folder README says exactly where the traces appear.

## Running the benchmark

```bash
cd evaluation
set -a && source ../logfire/.env && set +a     # each eval sources its platform's .env
uv run --with requests python eval_logfire.py  # writes results/logfire.json
# same pattern: eval_braintrust.py, eval_langsmith.py, eval_langfuse.py, eval_phoenix.py
```

Every metric in `results/*.json` maps to an actual API call in the script — nothing is taken from docs. `evaluation/presentation.html` is a self-contained report of the final scores.

## Read-API cheat sheet

Verified against live APIs, July–August 2026:

| Platform | Read endpoint | Auth | Query language | Formats received |
|----------|--------------|------|----------------|------------------|
| Logfire | `POST logfire-us.pydantic.dev/v2/query` | Bearer read-scope API key | Arbitrary SQL over `records` | JSON, NDJSON, CSV, Arrow |
| Braintrust | `/v1/project_logs/{id}/fetch` + `POST /btql` | Bearer API key | BTQL (SQL-like + pipe syntax) | JSON |
| LangSmith | `POST /api/v1/runs/query` | `X-Api-Key` header | Function-style filter DSL | JSON |
| Langfuse | `GET /api/public/v2/observations` | HTTP Basic (public/secret key) | Filters + advanced JSON `filter` + Metrics API | JSON |
| Phoenix | `GET app.phoenix.arize.com/s/{space}/v1/projects/{id}/spans` | Bearer API key | Simple filters only (time/name/attribute/span_kind) | JSON, OTLP-JSON |

Gotchas discovered along the way:

- **Logfire** returns Arrow binary unless you send `Accept: application/json` — despite docs saying JSON is the default. `min_timestamp` is required (422 otherwise). Read access needs a read-scope API key; write tokens are rejected.
- **LangSmith** addresses projects by UUID — resolve names via `GET /api/v1/sessions?name=...`. Rate limit: 10 req/10 s on query endpoints.
- **Braintrust** fetch pagination can re-return rows across pages — dedupe by `id` when exporting.
- **Langfuse** legacy v1 endpoints (`/api/public/traces`) are offset-paginated and deprecated; the v2 observations API uses cursor pagination and selective field groups (`?fields=` — I/O payloads require explicit opt-in), and returns observation rows you group by `traceId` to reconstruct traces. Real-time ingestion needs a recent SDK (or `x-langfuse-ingestion-version: 4` on raw OTLP exports).
- **Phoenix** serves its own OpenAPI spec from the space URL (`/s/{space}/openapi.json`) — the read surface is fully discoverable, but deliberately simple: no server-side aggregation and no SQL/DSL, so analysis happens client-side. Traces are reconstructed by grouping spans on `context.trace_id` (or `include_spans=true` on `/traces`).
- **mcp-server-time** breaks with `mcp>=2` (imports the removed `McpError` name) — the demos pin `--with 'mcp<2'` in the uvx invocation.
- `cryptography` is pinned `<45` in all folders because newer versions need a rust toolchain to build from source on Intel macOS.

## Credentials

All secrets live in per-folder `.env` files, which are gitignored. No keys are committed. Each folder's README documents the exact variables needed and where to create them.

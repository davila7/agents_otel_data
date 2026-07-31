# Agents OTEL Data

AI agent observability, tested from both sides: **write identical traces into four OpenTelemetry-based platforms, then benchmark how well each one gives the data back through its read API.**

Every platform folder is a self-contained `uv` project running the same three scenarios (a direct Anthropic call, a tool-calling Pydantic AI agent, and an MCP-powered agent). The `evaluation/` folder holds an executable, evidence-only benchmark of the four read APIs, scored by a multi-agent judge panel.

## The verdict

| Rank | Platform | Score / 100 | What decided it |
|------|----------|------------:|-----------------|
| 🥇 | [Logfire](./logfire) (Pydantic) | **96.40** | Arbitrary SQL over raw spans, `gen_ai.*` semantic conventions returned verbatim, 4 export formats actually delivered (JSON/NDJSON/CSV/Arrow) |
| 🥈 | [Braintrust](./braintrust) | 89.40 | BTQL free query language, fastest ingest (0.4 s write-to-read), best error messages |
| 🥉 | [LangSmith](./langsmith) | 80.93 | Full trace completeness, cursor pagination, but no free SQL and strict rate limits |
| 4 | [Langfuse](./langfuse) | 72.03 | Simplest auth and full completeness, but 46.5 s ingest lag and offset-only pagination |

Full methodology, rubric, per-judge scores, and caveats: [`evaluation/RESULTS.md`](./evaluation/RESULTS.md). Note the [continuous-scoring sensitivity check](./evaluation/RESULTS.md#sensitivity-check-continuous-scoring-for-measured-metrics): the banded rubric flattened Braintrust's measured wins (0.4 s vs 5.0 s ingest). Logfire ranks first under every scoring scheme tested, but the size of its lead over Braintrust is method-dependent — from 7.0 points (banded) down to 2.1 (log-continuous) — with Logfire ahead on data openness and Braintrust ahead on speed. One caveat worth repeating: in the first run Logfire scored dead last (6.67) simply because the wrong credential type was provisioned — its query API requires a read-scope key. **Credentials decide benchmarks.**

## How the benchmark works

1. **Instrument** — each platform folder runs the same three demo scripts, so all four backends hold equivalent traces:
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
# same pattern: eval_braintrust.py, eval_langsmith.py, eval_langfuse.py
```

Every metric in `results/*.json` maps to an actual API call in the script — nothing is taken from docs. `evaluation/presentation.html` is a self-contained report of the final scores.

## Read-API cheat sheet

Verified against live APIs, July 2026:

| Platform | Read endpoint | Auth | Query language | Formats received |
|----------|--------------|------|----------------|------------------|
| Logfire | `POST logfire-us.pydantic.dev/v2/query` | Bearer read-scope API key | Arbitrary SQL over `records` | JSON, NDJSON, CSV, Arrow |
| Braintrust | `/v1/project_logs/{id}/fetch` + `POST /btql` | Bearer API key | BTQL (SQL-like + pipe syntax) | JSON |
| LangSmith | `POST /api/v1/runs/query` | `X-Api-Key` header | Function-style filter DSL | JSON |
| Langfuse | `GET /api/public/traces` et al. | HTTP Basic (public/secret key) | Fixed filters + Metrics API | JSON |

Gotchas discovered along the way:

- **Logfire** returns Arrow binary unless you send `Accept: application/json` — despite docs saying JSON is the default. `min_timestamp` is required (422 otherwise). Read access needs a read-scope API key; write tokens are rejected.
- **LangSmith** addresses projects by UUID — resolve names via `GET /api/v1/sessions?name=...`. Rate limit: 10 req/10 s on query endpoints.
- **Braintrust** fetch pagination can re-return rows across pages — dedupe by `id` when exporting.
- **Langfuse** legacy offset endpoints degrade on large projects; prefer the cursor-based v2/v3 endpoints.
- `cryptography` is pinned `<45` in all folders because newer versions need a rust toolchain to build from source on Intel macOS.

## Credentials

All secrets live in per-folder `.env` files, which are gitignored. No keys are committed. Each folder's README documents the exact variables needed and where to create them.

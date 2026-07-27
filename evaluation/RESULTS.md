# Agent Observability Platforms — Final Evaluation Results

**Date:** 2026-07-27
**Platforms evaluated:** Logfire (Pydantic), Langfuse, LangSmith, Braintrust
**Verdict: Logfire wins with 96.4 / 100.**

## Final Ranking

| Rank | Platform | Total (weighted, /100) |
|------|-----------|------------------------|
| 1 | **Logfire** | **96.40** |
| 2 | Braintrust | 89.40 |
| 3 | LangSmith | 80.93 |
| 4 | Langfuse | 72.03 |

## Methodology

1. **Live testing, not doc reading.** Each platform ingested the same three demo traces (chat-hello, travel-assistant with tools, time MCP agent) and its read API was exercised with real HTTP calls. Doc claims without a captured response were counted as false.
2. **Three independent judges** scored the captured evidence, each from a distinct lens:
   - **Data-engineer lens** — completeness of retrievable data, query power, export formats.
   - **Platform-operator lens** — reliability, rate limits, pagination robustness, auth friction, error quality.
   - **AI-engineer lens** — agent-specific fidelity (tool calls, span trees, token/cost tracking) and suitability for building evals.
3. **Aggregation.** For each platform and criterion, the three judges' scores (0–10) were averaged. The weighted total is `sum(weight × avg_score / 10)` over the nine criteria; weights sum to 100.
4. **Duplicate handling.** Judge 2's payload contained a duplicate `langsmith / otel-fidelity` entry explicitly marked as a duplicate guard; it was ignored (its value was identical to the primary entry, so the result is unaffected).

Raw combined data (per-judge scores, per-criterion averages, weighted contributions) is in [`results/final_scores.json`](results/final_scores.json).

## Rubric

| Criterion | Weight | What it measures |
|-----------|-------:|------------------|
| Trace data completeness / fidelity | 25 | Full agent story retrievable: llm input/output, model, tokens, cost, per-span latency, tool call args/results, intact span tree, plausible span count. |
| Server-side query flexibility | 20 | Ad-hoc analysis answerable server-side in one call; free SQL/DSL is the strongest signal. |
| Developer experience and error quality | 10 | Auth setup friction, docs-as-experienced, quality of the error for a deliberately malformed query. |
| Auth and API accessibility | 8 | Read API reachable with available plan/credentials; API parity across plans. |
| Retrieval latency | 8 | Median wall time of 3 identical "list recent traces" calls. |
| Pagination mechanism and scalability | 8 | Cursor / SQL-window / offset, verified by actually fetching a second page. |
| Export formats obtained | 8 | Formats actually received in responses (json/ndjson/csv/arrow/parquet); doc promises don't count. |
| Time to queryable (ingest lag) | 8 | Emit a fresh trace, poll every 2 s until visible (120 s timeout). |
| OTel/GenAI attribute fidelity | 5 | gen_ai.* semconv attributes and W3C-style IDs visible on the read path vs opaque proprietary schema. |

## Per-Platform Scores (judge-averaged, 0–10)

| Criterion (weight) | Logfire | Braintrust | LangSmith | Langfuse |
|--------------------|--------:|-----------:|----------:|---------:|
| Completeness (25) | 10.00 | 10.00 | 10.00 | 10.00 |
| Query flexibility (20) | 10.00 | 10.00 | 7.33 | 7.00 |
| DX / error quality (10) | 8.00 | 8.00 | 5.67 | 7.33 |
| Auth / accessibility (8) | 10.00 | 10.00 | 9.00 | 10.00 |
| Latency (8) | 8.00 | 8.00 | 8.00 | 4.00 |
| Pagination (8) | 10.00 | 10.00 | 10.00 | 6.00 |
| Export formats (8) | 10.00 | 5.00 | 5.00 | 5.00 |
| Freshness (8) | 10.00 | 10.00 | 10.00 | 4.00 |
| OTel fidelity (5) | 10.00 | 4.00 | 4.00 | 5.00 |
| **Weighted total** | **96.40** | **89.40** | **80.93** | **72.03** |

Judge agreement was very high: 33 of 36 platform×criterion cells were unanimous; the only splits were Langfuse dx-friction (7/8/7), LangSmith query-flex (7/8/7), and LangSmith dx-friction (5/6/6).

## Platform Summaries

### 1. Logfire — 96.40 (winner)

The only platform to score 10 on query flexibility, export formats, and OTel fidelity simultaneously.

**Strengths**
- Free SQL over the `records` table with server-side aggregation captured live (`{'calls': 10, 'in_tok': 3653, 'avg_out': 74.6}`) — the strongest analysis and anti-lock-in signal in the field.
- Four export formats actually received: JSON, NDJSON, CSV (curl-confirmed with real rows), and Arrow (columnar).
- gen_ai.* semconv attributes returned verbatim with W3C-style 32-hex trace IDs and lossless custom attributes — the only platform with standards-shaped read-path telemetry.
- Perfect completeness (11-span tools trace, intact tree, tool args/results, tokens, cost), single bearer read token, no plan gating.
- Freshness 5.0 s, exactly at the top-band threshold (the builder's 1.6 s outlier was discarded by all judges).
- High-quality errors: malformed SQL → 400 with parser line/column; missing `min_timestamp` → specific 422.

**Weaknesses**
- Footguns: Arrow-binary is the response default despite what the docs suggest, and `min_timestamp` is effectively required.
- Per-minute rate limiting (429s after ~10 queries).
- Retrieval latency 372 ms — good but not sub-300 ms.

### 2. Braintrust — 89.40

**Strengths**
- BTQL free query DSL with grouped dimensions/measures aggregation, independently curl-confirmed (HTTP 200 with grouped rows).
- Fastest ingest-to-queryable by far: 0.4 s — ideal for write-then-read agent loops.
- Cursor pagination with second page verified; single Bearer key, no plan gating.
- Good errors: malformed BTQL → 400 with parser position and expected-token list.

**Weaknesses**
- JSON-only export (ndjson/csv/parquet all tested false; `Accept: text/csv` ignored).
- Proprietary task/llm/tool span schema — no gen_ai.* keys or W3C IDs evidenced, so OTel-shape recovery needs custom mapping.
- Unhandled 429 rate limit actually broke the eval script until a retry patch was added; BTQL has a learning curve.

### 3. LangSmith — 80.93

**Strengths**
- Fully complete trace data (model, 981/183 tokens, $0.005688 cost, tool args/results, intact 7-span tree).
- Cursor pagination (`cursors.next`) with second page fetched; freshness 4.7 s.
- Expressive function-style filter DSL plus real `/runs/stats` server-side aggregation.

**Weaknesses**
- No free query language (SQL in filters → 400); aggregates are fixed-shape.
- Worst error quality tested: malformed filter → generic "Unable to parse filter" naming nothing.
- Friction: project-name-to-UUID resolution step; observed 10 req/10 s rate limit forcing client throttling.
- JSON-only in real responses; **Parquet bulk export is Plus/Enterprise plan-gated and was therefore untestable on this plan** — the only genuine access gap observed in the evaluation (reflected in auth-access 9 and export-formats 5).
- Proprietary run schema with zero-padded UUID trace IDs instead of native W3C IDs; no gen_ai.* passthrough.

### 4. Langfuse — 72.03

**Strengths**
- Fully complete trace data (666/161 tokens, $0.010347 cost, intact 8-span tree), reproduced exactly.
- Simple Basic auth, predictable REST, all evaluated read endpoints accessible on the plan.
- Structured 400 on malformed timestamp identifying the invalid field (though as a raw regex dump).

**Weaknesses**
- Slowest read path by ~3x: median 1293 ms (1.2–3 s band).
- Slowest freshness: 46.5 s ingest-to-queryable — long enough to break tight write-then-read agent loops.
- Metrics API limited to a fixed metric whitelist (free-expression probe `count(*)+1` rejected); no free query language.
- Offset/limit pagination (degrades at scale); the v2 observations cursor was seen in metadata but not exercised.
- JSON-only export; proprietary trace/observation schema with no gen_ai.* keys evidenced (W3C-style trace IDs are preserved).

## Winner Rationale

Logfire wins on the two heaviest criteria plus every openness signal: it ties everyone on completeness (all four platforms returned the full agent story), but it is one of only two platforms with a free query language (weight 20), and it is the **only** platform with real multi-format export (4 formats including columnar Arrow) and verbatim gen_ai.*/W3C-ID passthrough on the read path. Braintrust is a strong runner-up — matching Logfire on query power and beating it on freshness (0.4 s vs 5.0 s) — but loses ground on export formats (JSON-only) and its proprietary span schema. LangSmith is held back by no free query language, the weakest malformed-query error, and plan-gated bulk export. Langfuse, despite perfect completeness, is penalized by the slowest latency, a 46.5 s ingest lag, offset pagination, and a whitelisted metrics API.

## Caveats

- **This report supersedes an earlier draft** in which Logfire was largely untestable; the final judge scores below are based on a successful fresh run where every Logfire read-path capability was exercised and reproduced.
- **LangSmith Parquet bulk export was untestable**: it is documented as Plus/Enterprise-only, so it was scored as unavailable per the "only formats actually received count" rule. On a higher plan LangSmith's export score could improve.
- **Braintrust and LangSmith OTel-fidelity scores are conservative**: no gen_ai.* keys or W3C IDs appeared in captured payloads, but absence of evidence in the fetched responses is not absolute proof the platforms cannot surface them via other endpoints.
- **Rate limits affected measurement on three platforms**: Logfire (~10 queries/min 429), LangSmith (10 req/10 s), and Braintrust (429 requiring a retry patch mid-run). Sustained-throughput behavior was not benchmarked.
- **Logfire's freshness sits exactly on the 5 s band boundary** (5.0 s after discarding a 1.6 s outlier); a re-run could plausibly land it one band lower (score 8), which would still leave it first (94.8).
- **Langfuse's tested metrics endpoint is flagged deprecated**, so its query-flexibility standing may shift as the replacement API matures.
- All measurements are single-session, single-machine, small-data (3 demo traces); production-scale behavior (pagination depth, query performance on large datasets) was not exercised.

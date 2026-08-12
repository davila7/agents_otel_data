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

## Sensitivity Check: Continuous Scoring for Measured Metrics

A fair critique of the ranking above: latency and freshness were scored in **bands** ("300–600 ms = 8 pts", "< 10 s = 10 pts"), which collapsed real quantitative gaps into ties — Braintrust's 0.4 s write-to-read ingest is 12.5× faster than Logfire's 5.0 s, yet both scored 10/10. Under banded scoring, the entire 7.0-point Logfire–Braintrust gap came from just two categorical criteria (export-formats and otel-fidelity).

[`rescore_continuous.py`](./rescore_continuous.py) re-scores those two metrics continuously — log-scale, normalized within the cohort (best observed = 10, worst = 0) — while the other 7 criteria keep the judge averages. Output: [`results/final_scores_continuous.json`](./results/final_scores_continuous.json).

| Platform | Banded total | Continuous total | Retrieval latency | Write-to-read |
|----------|-------------:|-----------------:|------------------:|--------------:|
| Logfire | 96.40 | **93.14** | 372.1 ms | 5.0 s |
| Braintrust | 89.40 | **91.00** | 335.5 ms | 0.4 s |
| LangSmith | 80.93 | **76.46** | 464.3 ms | 4.7 s |
| Langfuse | 72.03 | **65.63** | 1293.1 ms | 46.5 s |

**What this shows — and what it doesn't.** Logfire ranks first under every scoring scheme tested; what changes with the method is the *size* of its lead over Braintrust:

| Scoring scheme | Logfire–Braintrust gap |
|----------------|-----------------------:|
| Banded rubric (original) | 7.00 |
| Continuous, linear min-max | 5.90 |
| Continuous, log-scale (this table) | 2.14 |

The log transform is the most Braintrust-favorable reasonable choice (it stretches the scale near Braintrust's 0.4 s best), so the honest summary is: **the ranking is transform-robust; the gap magnitude is method-dependent (~2–7 points)**. Logfire leads on data openness (export formats, verbatim OTEL attributes); Braintrust leads on speed (ingest freshness, retrieval latency). LangSmith and Langfuse drop under any continuous scheme because the bands were cushioning their slower measurements.

Two disclosures that bound the precision of the continuous numbers: Logfire's 5.0 s freshness is a **single sample** (an earlier 1.6 s reading was discarded by the verifier; using it instead would widen the gap to ~4 points), and cohort min-max normalization is **cohort-dependent** — the scale is stretched by Langfuse's 46.5 s outlier, and the worst platform is forced to 0 by construction.

## August 2026 Re-run: Langfuse Observations v2

After the July results were published, the Langfuse team reached out: they had shipped major read-path improvements (the [observations v2 API](https://langfuse.com/docs/api-and-data-platform/features/observations-api) with cursor pagination and selective field groups, plus real-time ingestion in the v4 data model) and asked for a re-run. Since our demos use the Python SDK (v4.14.2), no ingestion header changes were needed. `eval_langfuse.py` was rewritten against `GET /api/public/v2/observations`; the July v1-API evidence stays frozen in `results/langfuse.json`, and the re-run writes `results/langfuse_v2.json`.

**What changed for Langfuse** (July v1 API → August v2 API, both live-measured):

| Metric | July (v1) | August (v2) | Rubric effect |
|--------|-----------|-------------|---------------|
| Retrieval latency | 1293.1 ms | **202.5 ms** — fastest in cohort | band 4 → 10 |
| Write-to-read lag | 46.5 s | **10.7 s** | band 4 → 8 |
| Pagination | offset (page/limit) | **cursor** (`meta.cursor`, second page verified, limit 1000) | band 6 → 10 |
| OTEL fidelity | proprietary schema | **verbatim `attributes.gen_ai.*` keys + W3C 32-hex trace IDs** in metadata | 5 → 9 |
| Query filters | fixed params | fixed params + **advanced JSON `filter` param (verified honored)**; still no free SQL | 7 → 8 |
| Completeness / export | 10/10 checklist; JSON only | unchanged (10/10; JSON only) | — |

**Same-session cohort (rubric rule 2).** Comparing August Langfuse numbers against July numbers for the other platforms would break the same-machine/same-session rule, so all four evals were re-run back-to-back on 2026-08-03 (`results/*_aug2026.json`):

| Platform | Latency (median of 3) | Write-to-read |
|----------|----------------------:|--------------:|
| Langfuse (v2) | **202.5 ms** | 10.7 s |
| Braintrust | 374.6 ms | **0.4 s** |
| Logfire | 413.9 ms | 5.1 s |
| LangSmith | 1305.2 ms | 0.5 s |

Note the session-to-session variance this exposes: with identical scripts, LangSmith's median latency went 464 → 1305 ms while its ingest lag improved 4.7 → 0.5 s. Single-session medians of three samples are weather, not climate.

**August continuous scores** (`rescore_continuous.py --cohort aug2026` → `results/final_scores_continuous_aug2026.json`; Langfuse's changed categorical criteria re-scored by mechanical application of the rubric anchors to captured v2 responses — pagination 10, otel-fidelity 9, query-flex 8 — all other judge averages retained):

| Platform | July continuous | August continuous |
|----------|----------------:|------------------:|
| Logfire | 93.14 | **88.73** |
| Braintrust | 91.00 | **88.36** |
| Langfuse | 65.63 | **80.83** |
| LangSmith | 76.46 | 73.99 |

**What the re-run changes.** Langfuse moves from a distant 4th to 3rd, ~8 points behind a Logfire–Braintrust top pair that is now separated by 0.37 points — the vendor's improvements are real and large. In banded terms Langfuse's provisional re-score is ≈87.2 (from 72.03). Two honesty notes: (1) the categorical re-scores are single-scorer rubric-anchor applications, not a fresh three-judge panel — treated as provisional; (2) Langfuse's 10.7 s freshness, though 4× better than July, is the worst in the August cohort, so cohort min-max assigns it 0 — the banded view (8/10) is kinder and arguably fairer here. A follow-up probe (3 extra emit+poll cycles, 1 s polling; see `notes.freshness_followup_samples_s` in `langfuse_v2.json`) measured 20.7 / 7.0 / 6.3 s — the lag genuinely fluctuates in the ~6–21 s range, consistent with queue-and-batch ingestion rather than a synchronous write path. The July tables above remain the frozen record of the original evaluation.

## August 12, 2026: Phoenix Joins the Arena

Arize Phoenix (Phoenix Cloud space, project `agents-otel-data`) was added as a fifth platform: the same three demos were instrumented via `arize-phoenix-otel` + OpenInference (`phoenix/`), and a new `eval_phoenix.py` exercises the Phoenix REST API (`/v1/projects/{id}/spans`, `/traces`, `/spans/otlpv1`; Bearer auth; the whole surface is one OpenAPI spec served from the space itself). Evidence: [`results/phoenix.json`](results/phoenix.json).

**Same-session cohort (rubric rule 2).** Adding a platform re-opens the cohort, so all five evals were re-run back-to-back on 2026-08-12 (`results/*_aug12_2026.json` + `results/phoenix.json`):

| Platform | Latency (median of 3) | Write-to-read |
|----------|----------------------:|--------------:|
| Phoenix | **87.5 ms** | **0.1 s** |
| Langfuse (v2) | 137.9 ms | 6.4 s |
| Braintrust | 322.9 ms | 0.5 s |
| Logfire | 326.1 ms | 4.9 s |
| LangSmith | 1935.5 ms | 4.4 s |

**Phoenix judge panel.** Its seven categorical criteria were scored by a fresh 3-judge panel (same lenses as July) against the captured evidence — [`results/phoenix_judges.json`](results/phoenix_judges.json). 20 of 21 cells were unanimous: completeness 10 (full checklist on the 9-span MCP trace, including `operation.cost` and tool args/results), dx-friction 9 (single Bearer token, self-served OpenAPI spec, structured 422 naming the bad parameter), auth-access 10, pagination 10 (cursor, second page fetched), otel-fidelity 10 (verbatim `gen_ai.*` keys alongside OpenInference `llm.*`, W3C 32-hex ids, OTLP-JSON export path), export-formats 7 (json + otlp-json; CSV Accept header ignored), query-flex 5.33 (the split cell, 6/5/5: time/name/attribute/span_kind filters all honored, but no server-side aggregation and no SQL/DSL — the spans endpoint self-describes as "simple filters, no DSL").

**August 12 continuous scores** (`rescore_continuous.py --cohort aug12_2026` → [`results/final_scores_continuous_aug12_2026.json`](results/final_scores_continuous_aug12_2026.json); incumbents keep their judge averages plus the Langfuse v2 overrides):

| Rank | Platform | Total /100 |
|------|----------|-----------:|
| 1 | Phoenix | **87.27** |
| 2 | Logfire | 87.11 |
| 3 | Braintrust | 84.53 |
| 4 | Langfuse | 79.66 |
| 5 | LangSmith | 67.25 |

**Read the top of that table as a cluster, not a coronation.** Phoenix edges Logfire by 0.16 points — far inside this benchmark's known method-dependence — and its lead is manufactured exactly where cohort min-max normalization is most sensitive: Phoenix is the best observed value on *both* continuous criteria, so it banks a perfect 10 on each, while its 0.1 s freshness stretches the log scale and compresses everyone else toward 0 (Logfire's 4.9 s, band 10/10 in July terms, becomes 0.64/10 here). The durable, method-independent readings are: Phoenix delivers the fastest read path and write-to-read lag measured in any cohort of this benchmark, ties Logfire on OTel fidelity (the only two platforms returning `gen_ai.*` verbatim), and trades that against the weakest query surface of the top three (no server-side aggregation, no SQL/DSL, weight-20 criterion at 5.33). Logfire remains the strongest analysis/export platform; Braintrust remains the all-rounder. Session variance is also visible again: LangSmith's median latency was 464 → 1305 → 1936 ms across the three cohorts on identical scripts.

Two Phoenix-specific disclosures: its 0.1 s freshness is a single measurement whose granularity is bounded by the poll loop's first iteration (the true lag is somewhere in 0–2 s; SimpleSpanProcessor exports synchronously, so near-zero lag is plausible but the precision isn't); and dropping Phoenix from the cohort returns the remaining four platforms to essentially their August 3 ordering — cohort membership still moves the continuous numbers, as disclosed since July.

## Caveats

- **This report supersedes an earlier draft** in which Logfire was largely untestable; the final judge scores below are based on a successful fresh run where every Logfire read-path capability was exercised and reproduced.
- **LangSmith Parquet bulk export was untestable**: it is documented as Plus/Enterprise-only, so it was scored as unavailable per the "only formats actually received count" rule. On a higher plan LangSmith's export score could improve.
- **Braintrust and LangSmith OTel-fidelity scores are conservative**: no gen_ai.* keys or W3C IDs appeared in captured payloads, but absence of evidence in the fetched responses is not absolute proof the platforms cannot surface them via other endpoints.
- **Rate limits affected measurement on three platforms**: Logfire (~10 queries/min 429), LangSmith (10 req/10 s), and Braintrust (429 requiring a retry patch mid-run). Sustained-throughput behavior was not benchmarked.
- **Logfire's freshness sits exactly on the 5 s band boundary** (5.0 s after discarding a 1.6 s outlier); a re-run could plausibly land it one band lower (score 8), which would still leave it first (94.8).
- **Langfuse's tested metrics endpoint is flagged deprecated**, so its query-flexibility standing may shift as the replacement API matures.
- All measurements are single-session, single-machine, small-data (3 demo traces); production-scale behavior (pagination depth, query performance on large datasets) was not exercised.

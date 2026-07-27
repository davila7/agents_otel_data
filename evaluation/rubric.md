# Scoring Rubric: Agent-Observability Read APIs

Empirical comparison of **Logfire, Langfuse, LangSmith, Braintrust**, evaluated from Claude Code exclusively through each platform's **read API**. Every score must be justified by an actual API response captured during testing (stored in each platform's `metrics` JSON object). Documentation may explain a behavior but never substitutes for an observed response.

Each criterion is scored **0–10**. Weighted total = Σ (score × weight) / 10, on a 0–100 scale. Weights sum to 100.

| # | ID | Criterion | Weight |
|---|-----|-----------|--------|
| 1 | `completeness` | Trace data completeness / fidelity | 25 |
| 2 | `query-flex` | Server-side query flexibility | 20 |
| 3 | `dx-friction` | Developer experience and error quality | 10 |
| 4 | `auth-access` | Auth and API accessibility | 8 |
| 5 | `latency` | Retrieval latency | 8 |
| 6 | `pagination` | Pagination mechanism and scalability | 8 |
| 7 | `export-formats` | Export formats obtained | 8 |
| 8 | `freshness` | Time to queryable (ingest lag) | 8 |
| 9 | `otel-fidelity` | OTel/GenAI attribute fidelity through the read path | 5 |
| | | **Total** | **100** |

---

## 1. `completeness` — Trace data completeness / fidelity (25)

**What it captures.** Whether the read API returns the full agent story for the three demo traces (01 chat-hello, 02 travel-assistant/tools, 03 time MCP agent): prompts/messages in, completions out, model name, token usage, cost, per-span latency, tool-call arguments and results, and an intact parent-child span tree. This is the core job of an agent-observability read API — truncated or missing fields make root-cause analysis impossible (Brief 1 §2, Brief 2 B2).

**Measured from.** `metrics.completeness`: presence/non-emptiness of `llm_input`, `llm_output`, `model_name`, `token_usage` (input+output), `cost_usd`, `latency_per_span`, `tool_call_args`, `tool_call_results`, `span_tree`, plus `span_count` of the tools-example trace.

**Scoring.** 10 checklist fields, 1 point each:
- 1 point if the field is present AND non-empty AND untruncated in a real read-API response for the relevant demo trace.
- 0.5 points if present but partial (e.g. truncated payload, tokens without breakdown, tool args without results).
- The `span_tree` point requires verifiable parent-child links (parent IDs resolving to spans in the same trace) and a plausible `span_count` for the tools example.
- Round to the nearest integer for the final 0–10 score.

## 2. `query-flex` — Server-side query flexibility (20)

**What it captures.** Whether ad-hoc analysis ("which tool failed?", "total output tokens per model last 7 days") can be answered server-side in one call rather than by client-side post-processing of bulk downloads (Brief 1 §3, Brief 2 B3). Free SQL/DSL access is the strongest anti-lock-in and analysis signal.

**Measured from.** `metrics.query_flexibility` booleans, each verified live: `filter_by_time`, `filter_by_name_or_attribute`, `aggregation`, `free_sql_or_dsl`.

**Scoring anchors.**
- **10** — all four true; arbitrary expressions (SQL or equivalent DSL) demonstrated against real spans.
- **7–8** — time + attribute filters + server-side aggregation work, but no free-form query language.
- **4–6** — time and name/attribute filtering only; aggregation must be done client-side.
- **1–3** — only time-range filtering (or only a fixed "recent" listing).
- **0** — no server-side filtering demonstrated.

## 3. `dx-friction` — Developer experience and error quality (10)

**What it captures.** How much friction an engineer hits driving the API from code: auth setup steps, docs-as-experienced clarity, and — critically — the quality of the error returned for a deliberately malformed query (a malformed request MUST have been sent). Time-to-value and debuggability are recurring differentiators (Brief 1 §7).

**Measured from.** `metrics.dx_friction` free-text notes, grounded in actual requests/responses (including the intentional malformed call).

**Scoring anchors.**
- **9–10** — single env var/token auth; endpoints discoverable without trial-and-error; malformed query returns a specific, actionable message (names the bad field/parameter).
- **6–8** — minor friction (extra headers, region/base-URL discovery, one docs gap) but errors are still informative.
- **3–5** — meaningful trial-and-error needed; errors are generic (bare 400/422, opaque message).
- **0–2** — auth or endpoint discovery required guessing; malformed input yields 5xx, HTML, or silent empty results.

## 4. `auth-access` — Auth and API accessibility (8)

**What it captures.** Whether the read API is actually reachable with the plan/credentials at hand — API parity across plans is an openness precondition (Brief 2 B6). A read API that doesn't authenticate scores the whole platform down.

**Measured from.** `metrics.auth_works` (200 on a real read call) plus any observed scope/plan gating encountered during testing.

**Scoring anchors.**
- **10** — 200 on first correctly-formed call; all read endpoints used in this evaluation accessible with one credential on the current (non-enterprise) plan.
- **6–9** — auth works but some read endpoints/entities were gated, needed a second credential type, or required non-obvious scoping (org/project IDs discovered by trial).
- **1–5** — auth works only for a subset of read surface relevant to the tests.
- **0** — `auth_works` false: no authenticated read response obtained.

## 5. `latency` — Retrieval latency (8)

**What it captures.** Interactive analysis responsiveness: the median wall-time of three identical "list recent traces" calls. Matters for agent-driven workflows (Claude Code polling/iterating against the API).

**Measured from.** `metrics.retrieval_latency_ms` (median of 3, measured in Python).

**Scoring anchors** (same network conditions across platforms; compare relatively if absolute bands are unfair):
- **10** — < 300 ms
- **8** — 300–600 ms
- **6** — 600–1200 ms
- **4** — 1.2–3 s
- **2** — 3–10 s
- **0** — > 10 s or timeouts/instability across the three calls.

## 6. `pagination` — Pagination mechanism and scalability (8)

**What it captures.** Whether full-dataset export is mechanically possible: cursor-based pagination scales; offset pagination degrades; no pagination caps the retrievable dataset (Brief 2 B4).

**Measured from.** `metrics.pagination`: mechanism (`cursor` / `offset` / `sql-window`), verified by fetching a second page when enough data exists, else by the mechanism field observed in a real first-page response.

**Scoring anchors.**
- **10** — cursor (or SQL windowing) pagination, second page actually fetched, documented/observed page-size limit, no evident total-results cap.
- **7–8** — cursor/sql-window mechanism observed in a real response but second page not exercised (insufficient data).
- **4–6** — offset/limit pagination verified working.
- **1–3** — pagination fields present but ambiguous or unverifiable behavior.
- **0** — no pagination mechanism in responses; result set hard-capped.

## 7. `export-formats` — Export formats obtained (8)

**What it captures.** Getting raw data out in useful shapes is the anti-lock-in test (Brief 1 §3, Brief 2 C1). Only formats **actually received** count — not formats a docs page promises.

**Measured from.** `metrics.export_formats`: list among json / ndjson / csv / arrow / parquet actually obtained in responses.

**Scoring anchors.**
- **10** — ≥ 3 formats obtained, including at least one bulk/columnar or streaming-friendly format (ndjson, csv, arrow, or parquet).
- **7–8** — 2 formats obtained (e.g. json + one of ndjson/csv).
- **5** — json only, but full-fidelity (raw spans with all attributes, not summaries).
- **2–4** — json only, and responses are summary-shaped (lossy relative to what the UI shows).
- **0** — no machine-readable export obtained.

## 8. `freshness` — Time to queryable (8)

**What it captures.** Ingest-to-readable lag: emit a fresh trace via the platform's `01_messages.py` and poll the read API every 2 s until it appears (timeout 120 s). Long lag breaks agent loops that write then immediately read.

**Measured from.** `metrics.time_to_queryable_s` (null allowed if the emit script failed for environment reasons — note why).

**Scoring anchors.**
- **10** — ≤ 5 s
- **8** — 5–15 s
- **6** — 15–40 s
- **4** — 40–90 s
- **2** — 90–120 s
- **0** — not visible within 120 s.
- **null** — score 5 (neutral) and flag as unmeasured in the writeup; do not reward or punish an environment failure.

## 9. `otel-fidelity` — OTel/GenAI attribute fidelity through the read path (5)

**What it captures.** Openness of the data model as observed from the read side: do responses carry standards-shaped telemetry (`gen_ai.*` attributes, span kind/operation names, intact OTel span/trace/parent IDs) rather than an opaque proprietary schema? This is the read-API-visible slice of Brief 1 §1 and Brief 2 A2/A5/D5 — scored only from fields seen in actual responses during the completeness fetches, never from docs claims.

**Measured from.** Attribute keys and ID fields observed in the trace/span payloads already fetched for `completeness` (no extra test run required).

**Scoring anchors.**
- **9–10** — raw `gen_ai.*` (or equivalent OTel semconv) attributes retrievable verbatim; W3C-style trace/span/parent IDs exposed; custom attributes round-trip without being renamed or dropped.
- **6–8** — OTel IDs and most attributes retrievable, but some semconv fields are remapped into proprietary fields with the originals discarded.
- **3–5** — proprietary schema with partial attribute passthrough; reconstructing OTel shape would need custom mapping code.
- **0–2** — opaque proprietary payloads; original attributes/IDs not recoverable via the read API.

---

## Scoring rules

1. **Evidence-only.** Every number and boolean in `metrics` must trace to a captured API response. If a capability could not be exercised, score what was observed — do not extrapolate from docs.
2. **Same conditions.** Run latency and freshness measurements from the same machine/network in the same session for all four platforms.
3. **Nulls.** For optional `time_to_queryable_s`, a null (env failure) scores 5 with an explicit note; any other missing measurement scores 0 for the portion it covers.
4. **Deliberate malformed query.** `dx-friction` cannot be scored above 5 unless a malformed query was actually sent and its error response recorded.
5. **Reporting.** Alongside the weighted table, report per-platform strengths/weaknesses and a "best for" mapping (industry-standard comparison pattern), citing the metrics values.

## Out of scope (noted, not scored)

Cost modeling at scale, self-hosting/licensing, evals integration, retention windows, and contractual data ownership (Brief 1 §4–6, Brief 2 C3/D1–D6) require pricing pages, ToS analysis, or long-horizon tests that cannot be established from this session's read-API responses. They belong in the qualitative writeup, not this empirical rubric.

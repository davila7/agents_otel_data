"""Empirical evaluation of the Arize Phoenix read API.

Phoenix Cloud space `dan-avila7`, project `agents-otel-data`. The read path
is the Phoenix REST API (`/v1/projects/{id}/spans`, `/traces`, `/spans/otlpv1`)
with Bearer auth, documented at
https://app.phoenix.arize.com/s/dan-avila7/openapi.json

Run:
    cd evaluation && set -a && source ../phoenix/.env && set +a \
        && uv run --with requests python eval_phoenix.py

Prints a single JSON object to stdout and writes it to results/phoenix.json
(override with EVAL_RESULTS_PATH). Never prints secret values.
"""

import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "").rstrip("/")
KEY = os.environ.get("PHOENIX_API_KEY", "")
PROJECT = os.environ.get("PHOENIX_PROJECT_NAME", "agents-otel-data")

REPO = "/Users/danipower/Proyectos/Github/agents_otel_data"
RESULTS = os.environ.get("EVAL_RESULTS_PATH") or os.path.join(
    REPO, "evaluation", "results", "phoenix.json"
)

SPANS = f"/v1/projects/{PROJECT}/spans"

metrics = {"platform": "phoenix", "api": "Phoenix REST v1", "notes": {}}


def emit(blocked=None):
    if blocked:
        metrics["blocked"] = blocked
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    out = json.dumps(metrics, indent=2, default=str)
    with open(RESULTS, "w") as f:
        f.write(out + "\n")
    print(out)


if not (BASE and KEY):
    metrics["auth_works"] = None
    emit("missing PHOENIX_COLLECTOR_ENDPOINT/PHOENIX_API_KEY in env")
    sys.exit(0)

S = requests.Session()
S.headers["Authorization"] = f"Bearer {KEY}"


def get(path, **params):
    return S.get(BASE + path, params=params, timeout=30)


# ---------- 1. auth ----------
r = get("/v1/projects")
metrics["auth_works"] = r.status_code == 200
if r.status_code != 200:
    metrics["notes"]["auth"] = f"HTTP {r.status_code}"
    emit("credentials rejected by read API")
    sys.exit(0)

# ---------- 2. retrieval latency ----------
# Same probe shape as the other platforms: list 25 recent items.
lat = []
for _ in range(3):
    t0 = time.perf_counter()
    rr = get(SPANS, limit=25)
    rr.raise_for_status()
    lat.append((time.perf_counter() - t0) * 1000)
metrics["retrieval_latency_ms"] = round(statistics.median(lat), 1)
metrics["notes"]["latency_samples_ms"] = [round(x, 1) for x in lat]

# ---------- 3. completeness ----------
since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
rows = get(SPANS, limit=200, start_time=since).json().get("data", [])
by_trace = {}
for s in rows:
    by_trace.setdefault(s["context"]["trace_id"], []).append(s)
metrics["notes"]["recent_root_span_names"] = sorted(
    {s["name"] for s in rows if s.get("parent_id") is None}
)

tools_trace = next(
    (sp for sp in by_trace.values() if any(s.get("span_kind") == "TOOL" for s in sp)),
    None,
)
if tools_trace is None:
    metrics["completeness"] = None
    metrics["notes"]["completeness"] = "no trace with TOOL spans in last 200 rows"
else:
    spans = tools_trace
    llm = [s for s in spans if s.get("span_kind") == "LLM"]
    tools = [s for s in spans if s.get("span_kind") == "TOOL"]
    a = (llm[0].get("attributes") or {}) if llm else {}
    in_tok = a.get("gen_ai.usage.input_tokens") or a.get("llm.token_count.prompt")
    out_tok = a.get("gen_ai.usage.output_tokens") or a.get("llm.token_count.completion")
    cost = a.get("operation.cost")
    lat_ok = all(s.get("start_time") and s.get("end_time") for s in spans) and bool(spans)
    ids = {s["context"]["span_id"] for s in spans}
    parents = [s.get("parent_id") for s in spans if s.get("parent_id")]
    tree_ok = bool(parents) and all(p in ids for p in parents)
    t_attrs = [(t.get("attributes") or {}) for t in tools]
    t_args = next((ta.get("gen_ai.tool.call.arguments") for ta in t_attrs if ta.get("gen_ai.tool.call.arguments")), None)
    t_res = next((ta.get("gen_ai.tool.call.result") for ta in t_attrs if ta.get("gen_ai.tool.call.result") is not None), None)
    llm_input = bool(a.get("gen_ai.input.messages") or a.get("llm.input_messages.0.message.content"))
    llm_output = bool(a.get("gen_ai.output.messages") or a.get("llm.output_messages.0.message.content"))
    metrics["completeness"] = {
        "trace_id": spans[0]["context"]["trace_id"],
        "root_span_name": next((s["name"] for s in spans if s.get("parent_id") is None), None),
        "llm_input": llm_input,
        "llm_output": llm_output,
        "model_name": a.get("llm.model_name") or a.get("gen_ai.request.model"),
        "token_usage": {"input": in_tok, "output": out_tok, "present": bool(in_tok and out_tok)},
        "cost_usd": cost,
        "latency_per_span": lat_ok,
        "tool_call_args": bool(t_args),
        "tool_call_results": t_res is not None,
        "span_tree": tree_ok,
        "span_count": len(spans),
    }
    # otel-fidelity evidence: attribute keys visible through the read path
    metrics["notes"]["llm_span_attribute_keys"] = sorted(a.keys())[:40]
    tid = spans[0]["context"]["trace_id"]
    metrics["notes"]["trace_id_format"] = (
        "32-hex (W3C-style)" if len(tid) == 32 else tid[:8]
    )

# ---------- 4. query flexibility ----------
qf = {}
rt = get(SPANS, start_time=since, limit=5)
qf["filter_by_time"] = rt.status_code == 200 and "data" in rt.json()

rn = get(SPANS, name="chat-hello", limit=5)
rn_data = rn.json().get("data", []) if rn.status_code == 200 else []
qf["filter_by_name_or_attribute"] = (
    rn.status_code == 200
    and len(rn_data) > 0
    and all(s.get("name") == "chat-hello" for s in rn_data)
)
metrics["notes"]["chat_hello_spans_found"] = len(rn_data)

# attribute key:value filter (documented on /spans)
ra = get(SPANS, attribute="llm.model_name:claude-sonnet-4-5", limit=5)
ra_data = ra.json().get("data", []) if ra.status_code == 200 else []
attr_ok = bool(ra_data) and all(
    (s.get("attributes") or {}).get("llm.model_name") == "claude-sonnet-4-5" for s in ra_data
)
metrics["notes"]["attribute_filter_probe"] = (
    "attribute key:value filter accepted and honored"
    if attr_ok
    else f"HTTP {ra.status_code}: {ra.text[:200]}"
)
qf["filter_by_name_or_attribute"] = qf["filter_by_name_or_attribute"] and attr_ok

# server-side aggregation: the REST surface has no metrics/aggregation endpoint.
# Trace rows do carry pre-aggregated per-trace token totals — record as evidence.
rtr = get(f"/v1/projects/{PROJECT}/traces", limit=2)
trace_row = (rtr.json().get("data") or [{}])[0] if rtr.status_code == 200 else {}
metrics["notes"]["trace_level_rollups"] = {
    k: trace_row.get(k)
    for k in ("token_count_prompt", "token_count_completion", "token_count_total")
}
qf["aggregation"] = False
metrics["notes"]["aggregation_probe"] = (
    "no server-side aggregation endpoint in the REST API (no metrics/GROUP BY); "
    "only fixed per-trace token rollups on the traces listing"
)
qf["free_sql_or_dsl"] = False
metrics["notes"]["free_expression_probe"] = (
    "spans endpoint documents 'simple filters (no DSL)'; no SQL/DSL surface exists "
    "in the REST API (GraphQL powers the UI but is undocumented/unsupported for users)"
)
metrics["query_flexibility"] = qf

# ---------- 5. pagination ----------
pag = {"mechanism": None, "second_page_fetched": False}
p1 = get(SPANS, limit=2)
cursor = p1.json().get("next_cursor")
pag["mechanism"] = "cursor (span Global ID in next_cursor)" if cursor else "cursor documented; no next_cursor on this page"
if cursor:
    p2 = get(SPANS, limit=2, cursor=cursor)
    d1 = {s["id"] for s in p1.json().get("data", [])}
    d2 = p2.json().get("data", []) if p2.status_code == 200 else []
    pag["second_page_fetched"] = bool(d2) and all(s["id"] not in d1 for s in d2)
pag["page_size_limit_documented"] = 1000
metrics["pagination"] = pag

# ---------- 6. export formats ----------
fmts = []
if p1.headers.get("content-type", "").startswith("application/json"):
    fmts.append("json")
# OTLP-shaped span export (protobuf-JSON Span objects) via the otlpv1 endpoint
ro = get(f"/v1/projects/{PROJECT}/spans/otlpv1", limit=1)
otlp_rows = ro.json().get("data", []) if ro.status_code == 200 else []
otlp_ok = bool(otlp_rows) and isinstance(otlp_rows[0].get("attributes"), list) and all(
    "key" in kv and "value" in kv for kv in otlp_rows[0]["attributes"][:5]
)
if otlp_ok:
    fmts.append("otlp-json")
metrics["notes"]["otlpv1_probe"] = (
    "OTLP/JSON Span payload returned (typed key/value attribute pairs)"
    if otlp_ok
    else f"HTTP {ro.status_code}"
)
rc = S.get(BASE + SPANS, params={"limit": 1}, headers={"Accept": "text/csv"}, timeout=30)
metrics["notes"]["csv_accept_header_result"] = (
    f"HTTP {rc.status_code}, content-type={rc.headers.get('content-type')}"
)
if rc.status_code == 200 and "csv" in (rc.headers.get("content-type") or ""):
    fmts.append("csv")
metrics["export_formats"] = fmts

# ---------- 7. time_to_queryable ----------
ttq = None
ttq_note = None
try:
    start = datetime.now(timezone.utc)
    proc = subprocess.run(
        ["uv", "run", "python", "01_messages.py"],
        cwd=os.path.join(REPO, "phoenix"),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        ttq_note = "01_messages.py failed: " + proc.stderr.strip().splitlines()[-1][:200]
    else:
        t_emit = time.perf_counter()
        deadline = t_emit + 120
        frm = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        while time.perf_counter() < deadline:
            rr = get(SPANS, name="chat-hello", start_time=frm, limit=5)
            if rr.status_code == 200 and rr.json().get("data"):
                ttq = round(time.perf_counter() - t_emit, 1)
                break
            time.sleep(2)
        if ttq is None:
            ttq_note = "fresh span not visible within 120s of script completion"
except Exception as e:  # noqa: BLE001
    ttq_note = f"could not run demo script: {type(e).__name__}: {e}"[:200]
metrics["time_to_queryable_s"] = ttq
if ttq_note:
    metrics["notes"]["time_to_queryable"] = ttq_note

# ---------- 8. dx friction (malformed query on purpose) ----------
bad = get(SPANS, start_time="not-a-date")
metrics["notes"]["malformed_query_response"] = f"HTTP {bad.status_code}: {bad.text[:300]}"
metrics["dx_friction"] = (
    "Auth is a single Bearer token; the whole read surface is one documented "
    "OpenAPI spec served from the space itself, so endpoints are discoverable "
    "without trial-and-error. Spans come back as flat OpenInference/OTel "
    "attribute maps with W3C ids — traces are reconstructed client-side by "
    "grouping on context.trace_id (an include_spans=true option exists on the "
    "traces endpoint). Filters are deliberately simple (time, name, span_kind, "
    "attribute key:value); anything analytical must be done client-side. "
    f"Malformed start_time returned HTTP {bad.status_code} with "
    + (
        "a structured validation body naming the bad parameter."
        if bad.status_code == 422
        else "see notes.malformed_query_response."
    )
)

emit()

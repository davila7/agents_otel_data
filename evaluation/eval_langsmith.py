"""Empirical evaluation of the LangSmith read API.

Run:
    cd evaluation && set -a && source ../langsmith/.env && set +a \
        && uv run --with requests python eval_langsmith.py

Prints a single JSON object to stdout and writes it to results/langsmith.json.
Never prints secret values.
"""

import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

API_V1 = "https://api.smith.langchain.com/api/v1"
API_V2 = "https://api.smith.langchain.com/v2"
KEY = os.environ.get("LANGSMITH_API_KEY", "")
PROJECT = os.environ.get("LANGSMITH_PROJECT", "agents-otel-data")

REPO = "/Users/danipower/Proyectos/Github/agents_otel_data"
RESULTS = os.environ.get("EVAL_RESULTS_PATH") or os.path.join(
    REPO, "evaluation", "results", "langsmith.json"
)

metrics = {"platform": "langsmith", "notes": {}}


def emit(blocked=None):
    if blocked:
        metrics["blocked"] = blocked
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    out = json.dumps(metrics, indent=2, default=str)
    with open(RESULTS, "w") as f:
        f.write(out + "\n")
    print(out)


if not KEY:
    metrics["auth_works"] = None
    emit("missing LANGSMITH_API_KEY in env")
    sys.exit(0)

S = requests.Session()
S.headers["X-Api-Key"] = KEY

# LangSmith rate limit: 10 requests / 10 s -> throttle to ~1 req/1.5 s and
# retry on HTTP 429 with backoff (observed 429s even at 1.05 s spacing).
_last_req = [0.0]


def _throttle():
    wait = _last_req[0] + 1.5 - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_req[0] = time.monotonic()


def _req(method, base, path, **kw):
    for attempt in range(5):
        _throttle()
        r = S.request(method, base + path, timeout=30, **kw)
        if r.status_code != 429:
            return r
        retry_after = r.headers.get("Retry-After")
        time.sleep(float(retry_after) if retry_after else 5 * (attempt + 1))
    return r


def get_v1(path, **params):
    return _req("GET", API_V1, path, params=params)


def post_v1(path, body, **kw):
    return _req("POST", API_V1, path, json=body, **kw)


def post_v2(path, body, **kw):
    return _req("POST", API_V2, path, json=body, **kw)


def raise_for_status(response):
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise requests.HTTPError(
            f"{error}; response body: {response.text[:1000]}",
            response=response,
        ) from error


def run_type(run):
    return (run.get("run_type") or "").lower()


# ---------- 1. auth + project uuid ----------
# Project discovery remains on the current tracer-sessions API. The deprecated
# v1 runs query is not used.
r = get_v1("/sessions", name=PROJECT)
metrics["auth_works"] = r.status_code == 200
if r.status_code != 200:
    metrics["notes"]["auth"] = f"HTTP {r.status_code}: {r.text[:200]}"
    emit("credentials rejected by read API")
    sys.exit(0)

sessions = r.json()
if not sessions:
    emit(f"project '{PROJECT}' not found via GET /sessions?name=")
    sys.exit(0)
project_id = sessions[0]["id"]
tenant_id = os.environ.get("LANGSMITH_WORKSPACE_ID") or sessions[0]["tenant_id"]
S.headers["X-Tenant-Id"] = tenant_id
metrics["notes"]["project"] = {"name": PROJECT, "id": project_id}

EARLIEST_START = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
    "%Y-%m-%dT%H:%M:%SZ"
)
RUN_SELECTS = [
    "ID",
    "NAME",
    "RUN_TYPE",
    "STATUS",
    "START_TIME",
    "END_TIME",
    "ERROR",
    "EXTRA",
    "METADATA",
    "INPUTS",
    "OUTPUTS",
    "PARENT_RUN_IDS",
    "TRACE_ID",
    "IS_ROOT",
    "TOTAL_TOKENS",
    "PROMPT_TOKENS",
    "COMPLETION_TOKENS",
    "TOTAL_COST",
]
TRACE_SELECTS = [
    "ID",
    "NAME",
    "RUN_TYPE",
    "STATUS",
    "START_TIME",
    "END_TIME",
    "TRACE_ID",
    "TOTAL_TOKENS",
    "TOTAL_COST",
    "FIRST_TOKEN_TIME",
]
RUN_QUERY_BASE = {
    "project_ids": [project_id],
    "min_start_time": EARLIEST_START,
    "selects": RUN_SELECTS,
}
TRACE_QUERY_BASE = {
    "project_id": project_id,
    "min_start_time": EARLIEST_START,
    "selects": TRACE_SELECTS,
}

# ---------- 2. retrieval latency (median of 3 identical list calls) ----------
lat = []
for _ in range(3):
    while True:
        _throttle()
        t0 = time.perf_counter()
        rr = S.post(
            API_V2 + "/traces/query",
            json={**TRACE_QUERY_BASE, "page_size": 25},
            timeout=30,
        )
        if rr.status_code != 429:
            break
        time.sleep(5)
    raise_for_status(rr)
    lat.append((time.perf_counter() - t0) * 1000)
metrics["retrieval_latency_ms"] = round(statistics.median(lat), 1)
metrics["notes"]["latency_samples_ms"] = [round(x, 1) for x in lat]

roots = [item.get("root_run", {}) for item in rr.json().get("items", [])]
metrics["notes"]["recent_root_run_names"] = sorted(
    {x.get("name") or "<none>" for x in roots}
)

# ---------- 3. completeness (tools-example trace: travel assistant) ----------
comp = None
# Prefer the 02_tools travel-assistant trace (get_weather tool runs).
rt = post_v2(
    "/runs/query",
    {
        **RUN_QUERY_BASE,
        "run_type": "TOOL",
        "filter": 'eq(name, "get_weather")',
        "page_size": 10,
    },
)
tool_runs = rt.json().get("items", []) if rt.status_code == 200 else []
if not tool_runs:
    rt = post_v2(
        "/runs/query",
        {**RUN_QUERY_BASE, "run_type": "TOOL", "page_size": 10},
    )
    tool_runs = rt.json().get("items", []) if rt.status_code == 200 else []
if not tool_runs:
    metrics["notes"]["completeness"] = "no run with run_type=tool found in project"
else:
    trace_id = tool_runs[0]["trace_id"]
    ra = post_v2(
        "/runs/query",
        {**RUN_QUERY_BASE, "trace_id": trace_id, "page_size": 100},
    )
    trace_runs = ra.json().get("items", [])
    llm_runs = [x for x in trace_runs if run_type(x) == "llm"]
    t_runs = [x for x in trace_runs if run_type(x) == "tool"]

    g = llm_runs[0] if llm_runs else {}
    t = t_runs[0] if t_runs else {}

    extra = g.get("extra") or {}
    model = (
        (g.get("metadata") or {}).get("ls_model_name")
        or (extra.get("metadata") or {}).get("ls_model_name")
        or (extra.get("invocation_params") or {}).get("model")
        or (g.get("inputs") or {}).get("model")
    )
    in_tok = g.get("prompt_tokens")
    out_tok = g.get("completion_tokens")
    cost = g.get("total_cost")
    lat_ok = bool(trace_runs) and all(
        x.get("start_time") and x.get("end_time") for x in trace_runs
    )
    tree_ok = any(x.get("parent_run_ids") for x in trace_runs)
    comp = {
        "trace_id": trace_id,
        "root_run_name": next(
            (x.get("name") for x in trace_runs if x.get("is_root")), None
        ),
        "llm_input": bool(g.get("inputs")),
        "llm_output": bool(g.get("outputs")),
        "model_name": model,
        "token_usage": {
            "input": in_tok,
            "output": out_tok,
            "present": bool(in_tok and out_tok),
        },
        "cost_usd": float(cost) if cost is not None else None,
        "latency_per_span": lat_ok,
        "tool_call_args": bool(t.get("inputs")),
        "tool_call_results": bool(t.get("outputs")),
        "span_tree": tree_ok,
        "span_count": len(trace_runs),
    }
metrics["completeness"] = comp

# ---------- 4. query flexibility ----------
qf = {}
since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

rtm = post_v2(
    "/traces/query",
    {**TRACE_QUERY_BASE, "min_start_time": since, "page_size": 5},
)
qf["filter_by_time"] = rtm.status_code == 200 and "items" in rtm.json()

rn = post_v2(
    "/traces/query",
    {**TRACE_QUERY_BASE, "trace_filter": 'eq(name, "chat-hello")', "page_size": 5},
)
rn_runs = (
    [item.get("root_run", {}) for item in rn.json().get("items", [])]
    if rn.status_code == 200
    else []
)
qf["filter_by_name_or_attribute"] = bool(rn_runs) and all(
    x.get("name") == "chat-hello" for x in rn_runs
)
metrics["notes"]["chat_hello_runs_found"] = len(rn_runs)

# Server-side aggregation: /runs/stats remains current and returns
# counts/latency/token aggregates across runs.
rs = post_v1("/runs/stats", {"session": [project_id], "filter": "eq(is_root, true)"})
stats = rs.json() if rs.status_code == 200 else {}
qf["aggregation"] = rs.status_code == 200 and stats.get("run_count") is not None
metrics["notes"]["runs_stats_sample"] = (
    {
        k: stats.get(k)
        for k in ("run_count", "total_tokens", "median_tokens", "total_cost")
    }
    if rs.status_code == 200
    else f"HTTP {rs.status_code}: {rs.text[:200]}"
)

# Arbitrary expressions? The filter language is a fixed function DSL
# (eq/and/or/gt/has/search) -- probe with SQL to confirm rejection.
rsql = post_v2(
    "/runs/query",
    {**RUN_QUERY_BASE, "filter": "select * from runs", "page_size": 1},
)
qf["free_sql_or_dsl"] = False
metrics["notes"]["free_expression_probe"] = (
    f"HTTP {rsql.status_code}: {rsql.text[:200]}"
)
metrics["query_flexibility"] = qf

# ---------- 5. pagination ----------
pag = {"mechanism": None, "second_page_fetched": False}
p1r = post_v2("/runs/query", {**RUN_QUERY_BASE, "page_size": 2})
p1 = p1r.json()
nxt = p1.get("next_cursor")
pag["first_page_has_next_cursor"] = "next_cursor" in p1
pag["mechanism"] = (
    "cursor (next_cursor token in POST /api/v2/runs/query response)"
    if "next_cursor" in p1
    else "unknown"
)
if nxt:
    p2r = post_v2(
        "/runs/query",
        {**RUN_QUERY_BASE, "page_size": 2, "cursor": nxt},
    )
    p2_items = p2r.json().get("items", []) if p2r.status_code == 200 else []
    ids1 = {item["id"] for item in p1.get("items", [])}
    pag["second_page_fetched"] = bool(p2_items) and p2_items[0]["id"] not in ids1
metrics["pagination"] = pag

# ---------- 6. export formats ----------
fmts = []
if (p1r.headers.get("content-type") or "").startswith("application/json"):
    fmts.append("json")
rc = post_v2(
    "/runs/query",
    {**RUN_QUERY_BASE, "page_size": 1},
    headers={"Accept": "text/csv"},
)
metrics["notes"]["csv_accept_header_result"] = (
    f"HTTP {rc.status_code}, content-type={rc.headers.get('content-type')}"
)
if rc.status_code == 200 and "csv" in (rc.headers.get("content-type") or ""):
    fmts.append("csv")
metrics["export_formats"] = fmts
metrics["notes"]["bulk_export"] = (
    "LangSmith bulk export (Parquet to S3) is Plus/Enterprise only per docs; not tested."
)

# ---------- 7. time_to_queryable ----------
ttq = None
ttq_note = None
try:
    start = datetime.now(timezone.utc)
    frm = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    proc = subprocess.run(
        ["uv", "run", "python", "01_messages.py"],
        cwd=os.path.join(REPO, "langsmith"),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        err = (proc.stderr.strip().splitlines() or ["<no stderr>"])[-1]
        ttq_note = "01_messages.py failed: " + err[:200]
    else:
        t_emit = time.perf_counter()
        deadline = t_emit + 120
        while time.perf_counter() < deadline:
            rr = post_v2(
                "/traces/query",
                {
                    **TRACE_QUERY_BASE,
                    "min_start_time": frm,
                    "trace_filter": 'eq(name, "chat-hello")',
                    "page_size": 5,
                },
            )
            if rr.status_code == 200 and rr.json().get("items"):
                ttq = round(time.perf_counter() - t_emit, 1)
                break
            time.sleep(2)
        if ttq is None:
            ttq_note = "fresh trace not visible within 120s of script completion"
except Exception as e:  # noqa: BLE001
    ttq_note = f"could not run demo script: {type(e).__name__}: {e}"[:200]
metrics["time_to_queryable_s"] = ttq
if ttq_note:
    metrics["notes"]["time_to_queryable"] = ttq_note

# ---------- 8. dx friction (malformed query on purpose) ----------
bad = post_v2(
    "/runs/query",
    {**RUN_QUERY_BASE, "filter": "eq(", "page_size": 1},
)
metrics["notes"]["malformed_query_response"] = (
    f"HTTP {bad.status_code}: {bad.text[:300]}"
)
metrics["dx_friction"] = (
    "Auth is a single X-Api-Key header; only extra step is resolving the project name "
    "to a UUID via the tracer-sessions API. Traces and runs use the v2 query endpoints "
    "with a small function-style filter DSL (eq/and/or/gt/has/search) -- expressive for "
    "filtering but no server-side arbitrary expressions; aggregates come from the "
    "separate fixed-shape /runs/stats endpoint. Cursor pagination uses next_cursor. "
    "Rate limit of 10 req/10s forces client-side throttling in any script. "
    f"Malformed filter 'eq(' returned HTTP {bad.status_code} "
    + (
        "with a structured error body."
        if bad.status_code in (400, 422)
        else "-- see notes.malformed_query_response."
    )
)

emit()

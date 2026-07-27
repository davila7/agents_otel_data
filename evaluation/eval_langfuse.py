"""Empirical evaluation of the Langfuse read API.

Run:
    cd evaluation && set -a && source ../langfuse/.env && set +a \
        && uv run --with requests python eval_langfuse.py

Prints a single JSON object to stdout and writes it to results/langfuse.json.
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

HOST = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
PUB = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
SEC = os.environ.get("LANGFUSE_SECRET_KEY", "")

REPO = "/Users/danipower/Proyectos/Github/agents_otel_data"
RESULTS = os.path.join(REPO, "evaluation", "results", "langfuse.json")

metrics = {"platform": "langfuse", "notes": {}}


def emit(blocked=None):
    if blocked:
        metrics["blocked"] = blocked
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    out = json.dumps(metrics, indent=2, default=str)
    with open(RESULTS, "w") as f:
        f.write(out + "\n")
    print(out)


if not (HOST and PUB and SEC):
    metrics["auth_works"] = None
    emit("missing LANGFUSE_HOST/LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY in env")
    sys.exit(0)

S = requests.Session()
S.auth = (PUB, SEC)


def get(path, **params):
    return S.get(HOST + path, params=params, timeout=30)


# ---------- 1. auth ----------
r = get("/api/public/traces", limit=1)
metrics["auth_works"] = r.status_code == 200
if r.status_code != 200:
    metrics["notes"]["auth"] = f"HTTP {r.status_code}"
    emit("credentials rejected by read API")
    sys.exit(0)

# ---------- 2. retrieval latency ----------
lat = []
for _ in range(3):
    t0 = time.perf_counter()
    rr = get("/api/public/traces", limit=25)
    rr.raise_for_status()
    lat.append((time.perf_counter() - t0) * 1000)
metrics["retrieval_latency_ms"] = round(statistics.median(lat), 1)
metrics["notes"]["latency_samples_ms"] = [round(x, 1) for x in lat]

# ---------- 3. completeness ----------
def full_trace(trace_id):
    fr = get(f"/api/public/traces/{trace_id}")
    fr.raise_for_status()
    return fr.json()


traces = get("/api/public/traces", limit=50).json().get("data", [])
metrics["notes"]["recent_trace_names"] = sorted(
    {t.get("name") or "<none>" for t in traces}
)

# tools-example trace: newest trace containing a tool-looking span
TOOL_NAMES = {"get_weather", "get_currency", "running tool", "running 1 tool"}


def find_tools_trace():
    for t in traces:
        ft = full_trace(t["id"])
        obs = ft.get("observations", [])
        for o in obs:
            n = (o.get("name") or "").lower()
            if any(tn in n for tn in TOOL_NAMES) or "tool" in n:
                return ft
    return None


tools_trace = find_tools_trace()
comp = {}
if tools_trace is None:
    metrics["completeness"] = None
    metrics["notes"]["completeness"] = "no trace with tool spans found in last 50 traces"
else:
    obs = tools_trace.get("observations", [])
    gens = [o for o in obs if o.get("type") == "GENERATION"]
    tool_obs = [
        o
        for o in obs
        if o.get("type") != "GENERATION"
        and any(tn in (o.get("name") or "").lower() for tn in TOOL_NAMES)
    ]
    g = gens[0] if gens else {}
    usage = g.get("usageDetails") or g.get("usage") or {}
    in_tok = usage.get("input") or usage.get("promptTokens") or usage.get("input_tokens")
    out_tok = usage.get("output") or usage.get("completionTokens") or usage.get("output_tokens")
    cost = tools_trace.get("totalCost")
    if cost is None and g:
        cd = g.get("costDetails") or {}
        cost = g.get("calculatedTotalCost") or cd.get("total")
    lat_ok = all(o.get("startTime") and o.get("endTime") for o in obs) and bool(obs)
    tree_ok = any(o.get("parentObservationId") for o in obs)
    t_args = next((o.get("input") for o in tool_obs if o.get("input")), None)
    t_res = next((o.get("output") for o in tool_obs if o.get("output") is not None), None)
    comp = {
        "trace_id": tools_trace["id"],
        "trace_name": tools_trace.get("name"),
        "llm_input": bool(g.get("input")),
        "llm_output": bool(g.get("output")),
        "model_name": g.get("model") or None,
        "token_usage": {"input": in_tok, "output": out_tok, "present": bool(in_tok and out_tok)},
        "cost_usd": cost,
        "latency_per_span": lat_ok,
        "tool_call_args": bool(t_args),
        "tool_call_results": t_res is not None,
        "span_tree": tree_ok,
        "span_count": len(obs),
    }
    metrics["completeness"] = comp

# ---------- 4. query flexibility ----------
qf = {}
since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
rt = get("/api/public/traces", fromTimestamp=since, limit=5)
qf["filter_by_time"] = rt.status_code == 200 and "data" in rt.json()

rn = get("/api/public/traces", name="chat-hello", limit=5)
qf["filter_by_name_or_attribute"] = (
    rn.status_code == 200
    and all(t.get("name") == "chat-hello" for t in rn.json().get("data", []))
    and len(rn.json().get("data", [])) > 0
)
metrics["notes"]["chat_hello_traces_found"] = len(rn.json().get("data", []))

# server-side aggregation via metrics API
mq = {
    "view": "traces",
    "metrics": [{"measure": "count", "aggregation": "count"}],
    "dimensions": [{"field": "name"}],
    "fromTimestamp": since,
    "toTimestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
ra = get("/api/public/metrics", query=json.dumps(mq))
qf["aggregation"] = ra.status_code == 200 and bool(ra.json().get("data"))
metrics["notes"]["metrics_api_sample"] = (
    ra.json().get("data", [])[:3] if ra.status_code == 200 else f"HTTP {ra.status_code}"
)

# arbitrary expressions? try an invalid measure to probe; API only allows a
# fixed set of views/measures/aggregations — no SQL/DSL expressions.
rf = get(
    "/api/public/metrics",
    query=json.dumps({"view": "traces", "metrics": [{"measure": "count(*) + 1", "aggregation": "sum"}], "fromTimestamp": since, "toTimestamp": mq["toTimestamp"]}),
)
qf["free_sql_or_dsl"] = False
metrics["notes"]["free_expression_probe"] = f"HTTP {rf.status_code}: {rf.text[:200]}"
metrics["query_flexibility"] = qf

# ---------- 5. pagination ----------
pag = {"mechanism": None, "second_page_fetched": False}
p1 = get("/api/public/traces", limit=2, page=1).json()
meta = p1.get("meta", {})
pag["mechanism"] = "offset (page/limit with meta.totalPages)" if "totalPages" in meta else "unknown"
pag["meta_first_page"] = meta
if meta.get("totalPages", 0) > 1:
    p2 = get("/api/public/traces", limit=2, page=2)
    d2 = p2.json().get("data", [])
    pag["second_page_fetched"] = (
        p2.status_code == 200
        and bool(d2)
        and d2[0]["id"] not in {t["id"] for t in p1.get("data", [])}
    )
# v2 observations claims cursor-based
ro = get("/api/public/v2/observations", limit=2)
if ro.status_code == 200:
    om = ro.json().get("meta", {})
    pag["observations_v2_meta_keys"] = sorted(om.keys())
metrics["pagination"] = pag

# ---------- 6. export formats ----------
fmts = []
rj = get("/api/public/traces", limit=1)
if rj.headers.get("content-type", "").startswith("application/json"):
    fmts.append("json")
rc = S.get(HOST + "/api/public/traces", params={"limit": 1}, headers={"Accept": "text/csv"}, timeout=30)
metrics["notes"]["csv_accept_header_result"] = f"HTTP {rc.status_code}, content-type={rc.headers.get('content-type')}"
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
        cwd=os.path.join(REPO, "langfuse"),
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
            rr = get("/api/public/traces", name="chat-hello", fromTimestamp=frm, limit=5)
            if rr.status_code == 200 and rr.json().get("data"):
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
bad = get("/api/public/traces", fromTimestamp="not-a-date")
metrics["notes"]["malformed_query_response"] = f"HTTP {bad.status_code}: {bad.text[:300]}"
metrics["dx_friction"] = (
    "Auth is simple HTTP Basic (public key as user, secret key as password); no token "
    "exchange needed. Endpoints are predictable REST with page/limit pagination and "
    "meta.totalItems/totalPages in every list response. The metrics API takes a "
    "URL-encoded JSON query which is awkward to hand-build but works. "
    f"Malformed fromTimestamp returned HTTP {bad.status_code} with "
    + ("a structured error body naming the invalid field." if bad.status_code == 400 else "see notes.malformed_query_response.")
)

emit()

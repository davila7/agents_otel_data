"""Empirical evaluation of the Langfuse read API — observations v2 edition.

August 2026 re-run requested by the Langfuse team after their real-time
ingestion + observations v2 improvements. The primary read path is now
GET /api/public/v2/observations (cursor pagination, selective field groups);
the metrics API is still used for server-side aggregation.

Run:
    cd evaluation && set -a && source ../langfuse/.env && set +a \
        && uv run --with requests python eval_langfuse.py

Prints a single JSON object to stdout and writes it to results/langfuse_v2.json.
The July 2026 v1-API evidence stays frozen in results/langfuse.json.
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
RESULTS = os.environ.get("EVAL_RESULTS_PATH") or os.path.join(
    REPO, "evaluation", "results", "langfuse_v2.json"
)

OBS = "/api/public/v2/observations"
ALL_FIELDS = "core,basic,time,io,metadata,model,usage,trace_context"

metrics = {"platform": "langfuse", "api": "observations v2", "notes": {}}


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
r = get(OBS, limit=1)
metrics["auth_works"] = r.status_code == 200
if r.status_code != 200:
    metrics["notes"]["auth"] = f"HTTP {r.status_code}"
    emit("credentials rejected by read API")
    sys.exit(0)

# ---------- 2. retrieval latency ----------
# Same probe shape as the other platforms: list 25 recent items, default fields.
lat = []
for _ in range(3):
    t0 = time.perf_counter()
    rr = get(OBS, limit=25)
    rr.raise_for_status()
    lat.append((time.perf_counter() - t0) * 1000)
metrics["retrieval_latency_ms"] = round(statistics.median(lat), 1)
metrics["notes"]["latency_samples_ms"] = [round(x, 1) for x in lat]

# ---------- 3. completeness ----------
# v2 returns observation rows; a trace is reconstructed by grouping on traceId.
since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
rows = get(OBS, limit=200, fields=ALL_FIELDS, fromStartTime=since).json().get("data", [])
by_trace = {}
for o in rows:
    by_trace.setdefault(o["traceId"], []).append(o)
metrics["notes"]["recent_trace_names"] = sorted(
    {o.get("traceName") or o.get("name") or "<none>" for o in rows if o.get("isRootObservation")}
)

TOOL_NAMES = {"get_weather", "get_currency", "running tool", "running 1 tool"}


def is_tool_obs(o):
    n = (o.get("name") or "").lower()
    return o.get("type") != "GENERATION" and (
        any(tn in n for tn in TOOL_NAMES) or o.get("type") == "TOOL"
    )


tools_trace = next(
    (obs for obs in by_trace.values() if any(is_tool_obs(o) for o in obs)), None
)
if tools_trace is None:
    metrics["completeness"] = None
    metrics["notes"]["completeness"] = "no trace with tool observations in last 200 rows"
else:
    obs = tools_trace
    gens = [o for o in obs if o.get("type") == "GENERATION"]
    tool_obs = [o for o in obs if is_tool_obs(o)]
    g = gens[0] if gens else {}
    usage = g.get("usageDetails") or {}
    in_tok = usage.get("input") or g.get("inputUsage")
    out_tok = usage.get("output") or g.get("outputUsage")
    cost = g.get("totalCost") or (g.get("costDetails") or {}).get("total")
    lat_ok = all(o.get("startTime") and o.get("endTime") for o in obs) and bool(obs)
    ids = {o["id"] for o in obs}
    parents = [o.get("parentObservationId") for o in obs if o.get("parentObservationId")]
    tree_ok = bool(parents) and all(p in ids for p in parents)
    t_args = next((o.get("input") for o in tool_obs if o.get("input")), None)
    t_res = next((o.get("output") for o in tool_obs if o.get("output") is not None), None)
    metrics["completeness"] = {
        "trace_id": obs[0]["traceId"],
        "trace_name": obs[0].get("traceName"),
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
    # otel-fidelity evidence: attribute keys visible through the read path
    md = g.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except ValueError:
            md = {}
    metrics["notes"]["generation_metadata_keys"] = sorted(md.keys())[:40]
    metrics["notes"]["trace_id_format"] = (
        "32-hex (W3C-style)" if len(obs[0]["traceId"]) == 32 else obs[0]["traceId"][:8]
    )

# ---------- 4. query flexibility ----------
qf = {}
rt = get(OBS, fromStartTime=since, limit=5)
qf["filter_by_time"] = rt.status_code == 200 and "data" in rt.json()

rn = get(OBS, name="chat-hello", limit=5)
rn_data = rn.json().get("data", []) if rn.status_code == 200 else []
qf["filter_by_name_or_attribute"] = (
    rn.status_code == 200
    and len(rn_data) > 0
    and all(o.get("name") == "chat-hello" for o in rn_data)
)
metrics["notes"]["chat_hello_observations_found"] = len(rn_data)

# advanced JSON filter param (documented on v2; structured, not free SQL)
adv = json.dumps([{"column": "type", "operator": "=", "value": "GENERATION", "type": "string"}])
ra2 = get(OBS, filter=adv, limit=5)
adv_ok = ra2.status_code == 200 and all(
    o.get("type") == "GENERATION" for o in ra2.json().get("data", [])
) and bool(ra2.json().get("data"))
qf["advanced_json_filter"] = adv_ok
metrics["notes"]["advanced_filter_probe"] = (
    "filter param accepted and honored" if adv_ok else f"HTTP {ra2.status_code}: {ra2.text[:200]}"
)

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

# fixed set of views/measures — still no free SQL/DSL expressions
rf = get(
    "/api/public/metrics",
    query=json.dumps({"view": "traces", "metrics": [{"measure": "count(*) + 1", "aggregation": "sum"}], "fromTimestamp": since, "toTimestamp": mq["toTimestamp"]}),
)
qf["free_sql_or_dsl"] = False
metrics["notes"]["free_expression_probe"] = f"HTTP {rf.status_code}: {rf.text[:200]}"
metrics["query_flexibility"] = qf

# ---------- 5. pagination ----------
pag = {"mechanism": None, "second_page_fetched": False}
p1 = get(OBS, limit=2)
m1 = p1.json().get("meta", {})
cursor = m1.get("cursor")
pag["mechanism"] = "cursor (base64 in meta.cursor)" if cursor else "unknown"
if cursor:
    p2 = get(OBS, limit=2, cursor=cursor)
    d1 = {o["id"] for o in p1.json().get("data", [])}
    d2 = p2.json().get("data", []) if p2.status_code == 200 else []
    pag["second_page_fetched"] = bool(d2) and all(o["id"] not in d1 for o in d2)
    pag["page_size_limit_documented"] = 1000
metrics["pagination"] = pag

# ---------- 6. export formats ----------
fmts = []
if p1.headers.get("content-type", "").startswith("application/json"):
    fmts.append("json")
rc = S.get(HOST + OBS, params={"limit": 1}, headers={"Accept": "text/csv"}, timeout=30)
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
            rr = get(OBS, name="chat-hello", fromStartTime=frm, limit=5)
            if rr.status_code == 200 and rr.json().get("data"):
                ttq = round(time.perf_counter() - t_emit, 1)
                break
            time.sleep(2)
        if ttq is None:
            ttq_note = "fresh observation not visible within 120s of script completion"
except Exception as e:  # noqa: BLE001
    ttq_note = f"could not run demo script: {type(e).__name__}: {e}"[:200]
metrics["time_to_queryable_s"] = ttq
if ttq_note:
    metrics["notes"]["time_to_queryable"] = ttq_note

# ---------- 8. dx friction (malformed query on purpose) ----------
bad = get(OBS, fromStartTime="not-a-date")
metrics["notes"]["malformed_query_response"] = f"HTTP {bad.status_code}: {bad.text[:300]}"
metrics["dx_friction"] = (
    "Auth is simple HTTP Basic (public key as user, secret key as password); no token "
    "exchange needed. The v2 observations endpoint uses cursor pagination "
    "(meta.cursor) and selective field groups via ?fields=, which keeps default "
    "responses lean but means I/O payloads require explicit opt-in and traces must "
    "be reconstructed client-side by grouping rows on traceId. The metrics API "
    "still takes a URL-encoded JSON query which is awkward to hand-build. "
    f"Malformed fromStartTime returned HTTP {bad.status_code} with "
    + ("a structured error body naming the invalid field." if bad.status_code == 400 else "see notes.malformed_query_response.")
)

emit()

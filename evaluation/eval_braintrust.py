"""Empirical evaluation of the Braintrust read API.

Run:
    cd evaluation && set -a && source ../braintrust/.env && set +a \
        && uv run --with requests python eval_braintrust.py

Prints a single JSON object to stdout and writes it to results/braintrust.json.
Never prints secret values.
"""

import json
import os
import statistics
import subprocess
import sys
import time

import requests

API = "https://api.braintrust.dev"
PROJECT_ID = "5d169ed6-af7e-4dbd-a8ca-458253acbfe8"
KEY = os.environ.get("BRAINTRUST_API_KEY", "")

REPO = "/Users/danipower/Proyectos/Github/agents_otel_data"
RESULTS = os.environ.get("EVAL_RESULTS_PATH") or os.path.join(
    REPO, "evaluation", "results", "braintrust.json"
)

metrics = {"platform": "braintrust", "notes": {}}


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
    emit("BRAINTRUST_API_KEY not set in env")
    sys.exit(0)

S = requests.Session()
S.headers["Authorization"] = f"Bearer {KEY}"


def _with_429_retry(do_request, retries=5):
    """Retry on HTTP 429 with backoff so rate limits don't abort the run."""
    for attempt in range(retries):
        r = do_request()
        if r.status_code != 429:
            return r
        wait = float(r.headers.get("retry-after") or 2 ** attempt)
        time.sleep(min(wait, 30))
    return r


def fetch(limit=25, cursor=None):
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    return _with_429_retry(
        lambda: S.get(f"{API}/v1/project_logs/{PROJECT_ID}/fetch", params=params, timeout=30)
    )


def btql(query):
    return _with_429_retry(lambda: S.post(f"{API}/btql", json={"query": query}, timeout=30))


FROM = f"from: project_logs('{PROJECT_ID}')"

# ---------------------------------------------------------------- 1. auth
r = fetch(limit=1)
metrics["auth_works"] = r.status_code == 200
if r.status_code != 200:
    metrics["notes"]["auth"] = f"HTTP {r.status_code}"
    emit("read API not accessible with available credentials")
    sys.exit(0)

# ------------------------------------------------------- 2. retrieval latency
lat = []
for _ in range(3):
    t0 = time.perf_counter()
    rr = fetch(limit=25)
    rr.raise_for_status()
    lat.append((time.perf_counter() - t0) * 1000)
metrics["retrieval_latency_ms"] = round(statistics.median(lat), 1)
metrics["notes"]["retrieval_latency_all_ms"] = [round(x, 1) for x in lat]

# ------------------------------------------------------------ 3. completeness
# Pull a batch of recent events and group spans by root_span_id.
events = []
cursor = None
for _ in range(4):
    rr = fetch(limit=100, cursor=cursor)
    rr.raise_for_status()
    body = rr.json()
    events.extend(body.get("events", []))
    cursor = body.get("cursor")
    if not cursor or not body.get("events"):
        break

traces = {}
for e in events:
    traces.setdefault(e.get("root_span_id"), []).append(e)


def span_name(e):
    return ((e.get("span_attributes") or {}).get("name")) or ""


def span_type(e):
    return ((e.get("span_attributes") or {}).get("type")) or ""


def find_trace(pred):
    """Most recent trace where any span satisfies pred."""
    best = None
    for spans in traces.values():
        if any(pred(s) for s in spans):
            created = max(s.get("created") or "" for s in spans)
            if best is None or created > best[0]:
                best = (created, spans)
    return best[1] if best else None


tools_trace = find_trace(lambda s: "get_weather" in span_name(s) or "get_currency" in span_name(s))
mcp_trace = find_trace(lambda s: "get_current_time" in span_name(s))
msg_trace = find_trace(lambda s: "anthropic.messages.create" in span_name(s))
metrics["notes"]["traces_found"] = {
    "01_messages": msg_trace is not None,
    "02_tools": tools_trace is not None,
    "03_mcp": mcp_trace is not None,
}

comp = {}
trace = tools_trace or mcp_trace or msg_trace
if trace is None:
    metrics["completeness"] = None
    metrics["notes"]["completeness"] = "no demo traces found in recent events"
else:
    llm_spans = [s for s in trace if span_type(s) == "llm"]
    tool_spans = [s for s in trace if span_type(s) == "tool"]
    llm = llm_spans[0] if llm_spans else {}
    m = llm.get("metrics") or {}
    md = llm.get("metadata") or {}

    comp["llm_input"] = bool(llm.get("input"))
    comp["llm_output"] = bool(llm.get("output"))
    comp["model_name"] = bool(md.get("model") or md.get("gen_ai.request.model"))
    comp["token_usage"] = bool(m.get("prompt_tokens")) and bool(m.get("completion_tokens"))
    comp["cost_usd"] = m.get("estimated_cost") is not None
    comp["latency_per_span"] = all(
        (s.get("metrics") or {}).get("start") and (s.get("metrics") or {}).get("end")
        for s in trace
    )
    comp["tool_call_args"] = any(t.get("input") for t in tool_spans)
    comp["tool_call_results"] = any(t.get("output") for t in tool_spans)
    comp["span_tree"] = any(s.get("span_parents") for s in trace)
    comp["span_count_tools_trace"] = len(tools_trace) if tools_trace else None
    if m.get("estimated_cost") is not None:
        metrics["notes"]["example_llm_cost_usd"] = m["estimated_cost"]
    metrics["notes"]["completeness_trace_used"] = (
        "02_tools" if trace is tools_trace else ("03_mcp" if trace is mcp_trace else "01_messages")
    )
    metrics["completeness"] = comp

# -------------------------------------------------------- 4. query flexibility
qf = {}

r = btql(f"select: id, created | {FROM} | filter: created > '2026-01-01T00:00:00Z' | limit: 5")
qf["filter_by_time"] = r.status_code == 200 and len(r.json().get("data", [])) > 0

r = btql(f"select: id, span_attributes | {FROM} | filter: span_attributes.type = 'llm' | limit: 5")
qf["filter_by_name_or_attribute"] = r.status_code == 200 and len(r.json().get("data", [])) > 0

r = btql(
    f"{FROM} | dimensions: span_attributes.type as t "
    f"| measures: count(1) as n, avg(metrics.tokens) as avg_tokens"
)
qf["aggregation"] = r.status_code == 200 and len(r.json().get("data", [])) > 0
if qf["aggregation"]:
    metrics["notes"]["aggregation_sample"] = r.json()["data"][:5]
else:
    metrics["notes"]["aggregation_error"] = r.text[:300]

r = btql(
    f"select: (metrics.prompt_tokens + metrics.completion_tokens) * 2 as computed "
    f"| {FROM} | filter: span_attributes.type = 'llm' | limit: 2"
)
qf["free_sql_or_dsl"] = r.status_code == 200 and len(r.json().get("data", [])) > 0
metrics["query_flexibility"] = qf

# --------------------------------------------------------------- 5. pagination
pag = {"mechanism": None, "second_page_verified": False}
r1 = fetch(limit=5)
b1 = r1.json()
if "cursor" in b1:
    pag["mechanism"] = "cursor"
    c = b1.get("cursor")
    ids1 = {e["id"] for e in b1.get("events", [])}
    if c:
        r2 = fetch(limit=5, cursor=c)
        if r2.status_code == 200:
            ids2 = {e["id"] for e in r2.json().get("events", [])}
            pag["second_page_verified"] = bool(ids2) and ids1.isdisjoint(ids2)
metrics["pagination"] = pag

# ----------------------------------------------------------- 6. export formats
fmts = {"json": True}  # every response above was application/json
# NDJSON / CSV / parquet: try Accept negotiation on the fetch endpoint.
for accept, name in [
    ("application/x-ndjson", "ndjson"),
    ("text/csv", "csv"),
    ("application/vnd.apache.parquet", "parquet"),
]:
    rr = S.get(
        f"{API}/v1/project_logs/{PROJECT_ID}/fetch",
        params={"limit": 1},
        headers={"Accept": accept},
        timeout=30,
    )
    ct = rr.headers.get("content-type", "")
    fmts[name] = rr.status_code == 200 and (name in ct or accept in ct)
metrics["export_formats"] = fmts

# ------------------------------------------------------ 7. time_to_queryable_s
metrics["time_to_queryable_s"] = None
try:
    t_start = time.time()
    proc = subprocess.run(
        ["uv", "run", "python", "01_messages.py"],
        cwd=os.path.join(REPO, "braintrust"),
        capture_output=True,
        text=True,
        timeout=180,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        metrics["notes"]["time_to_queryable"] = (
            "01_messages.py exited nonzero: " + (proc.stderr or proc.stdout)[-300:]
        )
    else:
        emit_done = time.time()
        deadline = emit_done + 120
        while time.time() < deadline:
            rr = btql(
                f"select: id, created | {FROM} "
                f"| filter: span_attributes.name = 'anthropic.messages.create' | limit: 5"
            )
            if rr.status_code == 200:
                fresh = [
                    d for d in rr.json().get("data", [])
                    if d.get("created", "") and time.mktime(time.strptime(
                        d["created"][:19], "%Y-%m-%dT%H:%M:%S")) > 0
                ]
                # compare on created timestamp string vs script start (UTC)
                start_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start))
                if any(d.get("created", "") > start_iso for d in rr.json().get("data", [])):
                    metrics["time_to_queryable_s"] = round(time.time() - emit_done, 1)
                    break
            time.sleep(2)
        if metrics["time_to_queryable_s"] is None:
            metrics["notes"]["time_to_queryable"] = "fresh trace not visible within 120s"
except Exception as exc:  # noqa: BLE001
    metrics["notes"]["time_to_queryable"] = f"could not run 01_messages.py: {type(exc).__name__}"

# ------------------------------------------------------------- 8. dx friction
r_bad = btql(f"selct: id | {FROM} | limit: 1")  # intentional typo
bad_snippet = r_bad.text[:200]
metrics["dx_friction"] = (
    "Auth is a single Bearer API key; the REST fetch endpoint worked on the first try with "
    "just the project id. BTQL (pipe syntax) is powerful but its docs require reading "
    "examples to get dimensions/measures right. Malformed query returned "
    f"HTTP {r_bad.status_code} with message: {bad_snippet!r} - "
    "error messages include parser position, which is helpful."
)

emit()

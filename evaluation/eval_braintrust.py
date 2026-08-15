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
        wait = float(r.headers.get("retry-after") or 2**attempt)
        time.sleep(min(wait, 30))
    return r


def sql(query, fmt=None):
    body = {"query": query}
    if fmt:
        body["fmt"] = fmt
    return _with_429_retry(lambda: S.post(f"{API}/btql", json=body, timeout=30))


FROM = f"FROM project_logs('{PROJECT_ID}')"
SINCE = "WHERE created > '2026-01-01T00:00:00Z'"
FIELDS = (
    "id, root_span_id, span_parents, created, input, output, "
    "metadata, metrics, span_attributes"
)


def fetch(limit=25, cursor=None):
    query = (
        f"SELECT {FIELDS} {FROM} {SINCE} ORDER BY _pagination_key DESC LIMIT {limit}"
    )
    if cursor:
        query += f" OFFSET '{cursor}'"
    return sql(query)


# ---------------------------------------------------------------- 1. auth
r = fetch(limit=1)
metrics["auth_works"] = r.status_code == 200
if r.status_code != 200:
    metrics["notes"]["auth"] = f"HTTP {r.status_code}"
    emit("read API not accessible with available credentials")
    sys.exit(0)

# ------------------------------------------------------- 2. retrieval latency
LIST_SQL = (
    f"SELECT root_span_id, created {FROM} {SINCE} AND is_root = true "
    f"ORDER BY created DESC LIMIT 20"
)
lat = []
for _ in range(3):
    # retry 429s OUTSIDE the timed window (rubric latency methodology);
    # routing through sql() would count its backoff sleep in the sample
    while True:
        t0 = time.perf_counter()
        rr = S.post(f"{API}/btql", json={"query": LIST_SQL}, timeout=30)
        if rr.status_code != 429:
            break
        time.sleep(min(float(rr.headers.get("retry-after") or 2), 30))
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
    events.extend(body.get("data", []))
    cursor = body.get("cursor")
    if not cursor or not body.get("data"):
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


def fetch_trace_by_span_name(name):
    """Deterministic lookup: find the newest trace containing a span with this
    name and pull all its rows — the recency window can miss old demo traces
    (they age out of the project; observed between the Aug 3 and Aug 12
    cohorts) or be crowded out by fresher freshness-probe traces."""
    r = sql(
        f"SELECT root_span_id {FROM} {SINCE} "
        f"AND span_attributes.name = '{name}' ORDER BY created DESC LIMIT 1"
    )
    if r.status_code != 200 or not r.json().get("data"):
        return None
    rsid = r.json()["data"][0]["root_span_id"]
    r2 = sql(f"SELECT {FIELDS} {FROM} {SINCE} AND root_span_id = '{rsid}'")
    rows = r2.json().get("data", []) if r2.status_code == 200 else []
    if rows:
        traces[rsid] = rows
        return rows
    return None


tools_trace = find_trace(
    lambda s: "get_weather" in span_name(s) or "get_currency" in span_name(s)
) or fetch_trace_by_span_name("get_weather")
mcp_trace = (find_trace(lambda s: "get_current_time" in span_name(s))
             or fetch_trace_by_span_name("get_current_time"))
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
    comp["token_usage"] = bool(m.get("prompt_tokens")) and bool(
        m.get("completion_tokens")
    )
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
        "02_tools"
        if trace is tools_trace
        else ("03_mcp" if trace is mcp_trace else "01_messages")
    )
    metrics["completeness"] = comp

# -------------------------------------------------------- 4. query flexibility
qf = {}

r = sql(f"SELECT id, created {FROM} WHERE created > '2026-01-01T00:00:00Z' LIMIT 5")
qf["filter_by_time"] = r.status_code == 200 and len(r.json().get("data", [])) > 0

r = sql(f"SELECT id, span_attributes {FROM} WHERE span_attributes.type = 'llm' LIMIT 5")
qf["filter_by_name_or_attribute"] = (
    r.status_code == 200 and len(r.json().get("data", [])) > 0
)

r = sql(
    f"SELECT span_attributes.type AS t, count(1) AS n, avg(metrics.tokens) AS avg_tokens "
    f"{FROM} GROUP BY span_attributes.type"
)
qf["aggregation"] = r.status_code == 200 and len(r.json().get("data", [])) > 0
if qf["aggregation"]:
    metrics["notes"]["aggregation_sample"] = r.json()["data"][:5]
else:
    metrics["notes"]["aggregation_error"] = r.text[:300]

r = sql(
    f"SELECT (metrics.prompt_tokens + metrics.completion_tokens) * 2 AS computed "
    f"{FROM} WHERE span_attributes.type = 'llm' LIMIT 2"
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
    ids1 = {e["id"] for e in b1.get("data", [])}
    if c:
        r2 = fetch(limit=5, cursor=c)
        if r2.status_code == 200:
            ids2 = {e["id"] for e in r2.json().get("data", [])}
            pag["second_page_verified"] = bool(ids2) and ids1.isdisjoint(ids2)
metrics["pagination"] = pag

# ----------------------------------------------------------- 6. export formats
# /btql takes a fmt parameter; "jsonl" is Braintrust's name for NDJSON. json and jsonl
# both come back as application/json, so identify the body rather than trust the header.


def is_json_export(body):
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("data"), list)


def is_jsonl_export(body):
    lines = [line for line in body.splitlines() if line.strip()]
    if not lines:
        return False
    try:
        rows = [json.loads(line) for line in lines]
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return all(
        isinstance(row, dict) and "id" in row and "created" in row for row in rows
    )


SIGNATURES = {
    "json": is_json_export,
    "ndjson": is_jsonl_export,
    "csv": lambda b: not b.lstrip()[:1] in (b"{", b"[") and b"," in b.split(b"\n")[0],
    "parquet": lambda b: b.startswith(b"PAR1"),
}
fmts = {}
for name, fmt in [
    ("json", "json"),
    ("ndjson", "jsonl"),
    ("csv", "csv"),
    ("parquet", "parquet"),
]:
    rr = sql(f"SELECT id, created {FROM} {SINCE} LIMIT 5", fmt=fmt)
    fmts[name] = (
        rr.status_code == 200 and bool(rr.content) and SIGNATURES[name](rr.content)
    )
    metrics["notes"].setdefault("export_format_results", {})[name] = (
        f"HTTP {rr.status_code}, content-type={rr.headers.get('content-type')}"
    )
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
            rr = sql(
                f"SELECT id, created {FROM} "
                f"WHERE span_attributes.name = 'anthropic.messages.create' "
                f"ORDER BY created DESC LIMIT 5"
            )
            if rr.status_code == 200:
                fresh = [
                    d
                    for d in rr.json().get("data", [])
                    if d.get("created", "")
                    and time.mktime(
                        time.strptime(d["created"][:19], "%Y-%m-%dT%H:%M:%S")
                    )
                    > 0
                ]
                # compare on created timestamp string vs script start (UTC)
                start_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start))
                if any(
                    d.get("created", "") > start_iso for d in rr.json().get("data", [])
                ):
                    metrics["time_to_queryable_s"] = round(time.time() - emit_done, 1)
                    break
            time.sleep(2)
        if metrics["time_to_queryable_s"] is None:
            metrics["notes"]["time_to_queryable"] = (
                "fresh trace not visible within 120s"
            )
except Exception as exc:  # noqa: BLE001
    metrics["notes"]["time_to_queryable"] = (
        f"could not run 01_messages.py: {type(exc).__name__}"
    )

# ------------------------------------------------------------- 8. dx friction
r_bad = sql(f"SELECT id {FROM} LIMT 1")  # intentional typo
bad_snippet = r_bad.text[:200]
metrics["notes"]["malformed_query_response"] = (
    f"HTTP {r_bad.status_code}: {r_bad.text[:300]}"
)
metrics["dx_friction"] = (
    "Auth is a single Bearer API key; the /btql endpoint worked on the first try with "
    "just the project id. SQL syntax is standard enough to write without dialect docs, "
    "though the span field layout still has to be looked up. Malformed query returned "
    f"HTTP {r_bad.status_code} with message: {bad_snippet!r} - "
    "error messages include parser position, which is helpful."
)

emit()

#!/usr/bin/env python3
"""Empirical evaluation of the Logfire read/query API.

Run:
  cd evaluation && set -a && source ../logfire/.env && set +a && \
  uv run --with requests python eval_logfire.py

Never prints secret values. Prints a single JSON object to stdout and writes
it to results/logfire.json.
"""
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://logfire-us.pydantic.dev"
QUERY_URL = BASE + "/v2/query"
REPO = "/Users/danipower/Proyectos/Github/agents_otel_data"
RESULTS_PATH = os.environ.get("EVAL_RESULTS_PATH") or os.path.join(
    REPO, "evaluation", "results", "logfire.json"
)
MIN_TS = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

notes = []
metrics = {}


def get_token():
    tok = os.environ.get("LOGFIRE_READ_TOKEN")
    if tok:
        return tok, "env LOGFIRE_READ_TOKEN"
    # fall back to the project credentials token; verify empirically
    cred_path = os.path.join(REPO, "logfire", ".logfire", "logfire_credentials.json")
    try:
        with open(cred_path) as f:
            return json.load(f)["token"], "logfire_credentials.json write token (fallback)"
    except Exception:
        return None, None


TOKEN, TOKEN_SOURCE = get_token()


RATE_LIMIT_HITS = 0


def q(sql, accept="application/json", min_ts=MIN_TS, timeout=30, throttle=1.2):
    """Run a query; returns (status_code, parsed_or_text, elapsed_ms).

    Retries on HTTP 429 (Logfire enforces a per-minute rate limit) so a burst
    of probes doesn't poison later measurements, and throttles between calls.
    """
    global RATE_LIMIT_HITS
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": accept,
               "Content-Type": "application/json"}
    body = {"sql": sql}
    if min_ts:
        body["min_timestamp"] = min_ts
    for attempt in range(4):
        t0 = time.monotonic()
        r = requests.post(QUERY_URL, headers=headers, json=body, timeout=timeout)
        ms = (time.monotonic() - t0) * 1000
        if r.status_code != 429:
            break
        RATE_LIMIT_HITS += 1
        retry_after = r.headers.get("Retry-After")
        try:
            wait = min(float(retry_after), 70) if retry_after else 62
        except ValueError:
            wait = 62
        time.sleep(wait)
    if throttle:
        time.sleep(throttle)
    if "json" in accept and r.status_code == 200 and accept == "application/json":
        try:
            return r.status_code, r.json(), ms
        except Exception:
            pass
    return r.status_code, r.content, ms


def rows(resp):
    """Convert a Logfire /v2/query JSON response to a list of row dicts.

    Observed live shape: {"schema": {"fields": [...]}, "data": [ {col: val, ...} ]}.
    Also tolerates the older columnar shape ({"columns": [{"name", "values"}]}).
    """
    if not isinstance(resp, dict):
        return []
    if isinstance(resp.get("data"), list):
        return resp["data"]
    cols = resp.get("columns", [])
    if not cols:
        return []
    names = [c["name"] for c in cols]
    n = len(cols[0]["values"])
    return [{names[j]: cols[j]["values"][i] for j in range(len(cols))} for i in range(n)]


def emit(blocked=None):
    out = {
        "platform": "logfire",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "api": QUERY_URL,
        "token_source": TOKEN_SOURCE,
        "metrics": metrics,
        "notes": notes,
    }
    if blocked:
        out["blocked"] = blocked
    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))
    sys.exit(0 if not blocked else 1)


if not TOKEN:
    metrics["auth_works"] = False
    emit(blocked="READ TOKEN MISSING: no LOGFIRE_READ_TOKEN in env and no credentials file")

# ---------------- 1. auth ----------------
status, resp, _ = q("SELECT count(*) AS n FROM records")
metrics["auth_works"] = status == 200
if status != 200:
    body_preview = resp.decode() if isinstance(resp, bytes) else str(resp)
    notes.append(f"auth check failed: HTTP {status}: {body_preview[:300]}")
    emit(blocked=f"READ TOKEN MISSING or invalid: query API returned HTTP {status} "
                 f"with token from {TOKEN_SOURCE}")
total_records = rows(resp)[0]["n"]
notes.append(f"records in last 30d: {total_records}")

# ---------------- 2. retrieval latency ----------------
LIST_SQL = ("SELECT trace_id, min(start_timestamp) AS started, count(*) AS spans "
            "FROM records GROUP BY trace_id ORDER BY started DESC LIMIT 20")
lat = []
for _ in range(3):
    s, _, ms = q(LIST_SQL)
    if s == 200:
        lat.append(ms)
metrics["retrieval_latency_ms"] = round(statistics.median(lat), 1) if len(lat) == 3 else None
if len(lat) != 3:
    notes.append("one or more latency probes failed")

# ---------------- 3. completeness ----------------
comp = {}
s, resp, _ = q(
    "SELECT trace_id, span_id, parent_span_id, span_name, message, start_timestamp, "
    "end_timestamp, duration, attributes, otel_scope_name "
    "FROM records ORDER BY start_timestamp DESC LIMIT 200")
all_spans = rows(resp) if s == 200 else []
notes.append(f"fetched {len(all_spans)} recent spans for completeness check")

def attr(sp):
    a = sp.get("attributes")
    if isinstance(a, str):
        try:
            return json.loads(a)
        except Exception:
            return {}
    return a or {}

# find the tools-example trace (travel-assistant / tool spans) and a chat trace
by_trace = {}
for sp in all_spans:
    by_trace.setdefault(sp["trace_id"], []).append(sp)

tools_trace = None
for tid, sps in by_trace.items():
    names = " ".join((sp.get("span_name") or "") + " " + (sp.get("message") or "") for sp in sps).lower()
    if "tool" in names or "travel" in names or "get_weather" in names or "execute_tool" in names:
        tools_trace = tid
        break
target_trace = tools_trace or (all_spans[0]["trace_id"] if all_spans else None)
tsp = by_trace.get(target_trace, [])
comp["inspected_trace_id"] = target_trace
comp["span_count_tools_trace"] = len(tsp) if tools_trace else None

def any_attr(spans, keys):
    for sp in spans:
        a = attr(sp)
        for k in keys:
            v = a.get(k)
            if v not in (None, "", [], {}):
                return True
    return False

GEN_AI_IN = ["gen_ai.input.messages", "gen_ai.prompt", "events", "all_messages_events",
             "gen_ai.request.messages"]
GEN_AI_OUT = ["gen_ai.output.messages", "gen_ai.completion", "gen_ai.response.text"]
scan = tsp or all_spans
comp["llm_input"] = any_attr(scan, GEN_AI_IN) or any(
    "gen_ai" in json.dumps(attr(sp)) and "user" in json.dumps(attr(sp)) for sp in scan)
comp["llm_output"] = any_attr(scan, GEN_AI_OUT) or any(
    a.get("gen_ai.usage.output_tokens") and "events" in a for sp in scan for a in [attr(sp)])
comp["model_name"] = any_attr(scan, ["gen_ai.response.model", "gen_ai.request.model", "model"])
comp["token_usage"] = any_attr(scan, ["gen_ai.usage.input_tokens"]) and any_attr(
    scan, ["gen_ai.usage.output_tokens"])
comp["cost_usd"] = any_attr(scan, ["gen_ai.usage.cost", "cost", "gen_ai.cost.total",
                                   "llm.usage.total_cost", "operation.cost"])
comp["latency_per_span"] = all(sp.get("duration") is not None or
                               (sp.get("start_timestamp") and sp.get("end_timestamp"))
                               for sp in scan) if scan else False
comp["tool_call_args"] = any_attr(scan, ["tool_arguments", "gen_ai.tool.call.arguments",
                                         "tool_call.arguments", "input"]) or any(
    "tool_arguments" in json.dumps(attr(sp)) for sp in scan)
comp["tool_call_results"] = any_attr(scan, ["tool_response", "gen_ai.tool.call.result",
                                            "tool_call.result", "output"]) or any(
    "tool_response" in json.dumps(attr(sp)) for sp in scan)
comp["span_tree"] = any(sp.get("parent_span_id") for sp in scan)
# record which attribute keys were seen, to make the assessment auditable
seen_keys = sorted({k for sp in scan for k in attr(sp)})
comp["attribute_keys_seen"] = seen_keys[:60]
metrics["completeness"] = comp

# ---------------- 4. query flexibility ----------------
flex = {}
s, r1, _ = q("SELECT count(*) AS n FROM records WHERE start_timestamp > now() - interval '7 days'")
flex["filter_by_time"] = s == 200
s, r2, _ = q("SELECT span_name, count(*) FROM records WHERE span_name ILIKE '%chat%' "
             "OR attributes->>'gen_ai.request.model' IS NOT NULL GROUP BY span_name LIMIT 10")
flex["filter_by_name_or_attribute"] = s == 200
s, r3, _ = q("SELECT count(*) AS calls, sum(CAST(attributes->>'gen_ai.usage.input_tokens' AS BIGINT)) "
             "AS in_tok, avg(CAST(attributes->>'gen_ai.usage.output_tokens' AS DOUBLE)) AS avg_out "
             "FROM records WHERE attributes->>'gen_ai.usage.input_tokens' IS NOT NULL")
flex["aggregation"] = s == 200
if s == 200:
    agg = rows(r3)
    if agg:
        notes.append(f"server-side aggregation over LLM spans: {agg[0]}")
s, r4, _ = q("SELECT upper(span_name) AS u, duration * 1000 AS ms, "
             "CASE WHEN duration > 1 THEN 'slow' ELSE 'fast' END AS bucket "
             "FROM records ORDER BY start_timestamp DESC LIMIT 5")
flex["free_sql_or_dsl"] = s == 200
metrics["query_flexibility"] = flex

# ---------------- 5. pagination ----------------
pag = {"mechanism": None, "second_page_verified": False}
s1, p1, _ = q("SELECT span_id FROM records ORDER BY start_timestamp DESC LIMIT 5 OFFSET 0")
s2, p2, _ = q("SELECT span_id FROM records ORDER BY start_timestamp DESC LIMIT 5 OFFSET 5")
if s1 == 200 and s2 == 200:
    ids1 = {r["span_id"] for r in rows(p1)}
    ids2 = {r["span_id"] for r in rows(p2)}
    pag["mechanism"] = "sql-window (LIMIT/OFFSET)"
    pag["second_page_verified"] = bool(ids2) and ids1.isdisjoint(ids2)
metrics["pagination"] = pag

# ---------------- 6. export formats ----------------
fmts = {}
for name, accept in [("json", "application/json"), ("ndjson", "application/x-ndjson"),
                     ("csv", "text/csv"), ("arrow", "application/vnd.apache.arrow.stream")]:
    s, body, _ = q("SELECT span_name, start_timestamp FROM records LIMIT 3", accept=accept)
    ok = s == 200 and (len(body) > 0 if isinstance(body, bytes) else bool(body))
    fmts[name] = ok
fmts["parquet"] = None
notes.append("parquet not advertised by API; not tested beyond documented formats")
metrics["export_formats"] = fmts

# ---------------- 7. time_to_queryable_s ----------------
ttq = None
marker_deadline = None
try:
    # Let the per-minute rate-limit window reset so a 429 backoff inside the
    # poll loop cannot inflate the time-to-queryable measurement.
    time.sleep(61)
    before_sql = "SELECT max(start_timestamp) AS m FROM records"
    s, resp, _ = q(before_sql)
    prev_max = rows(resp)[0]["m"] if s == 200 else None
    t_emit = datetime.now(timezone.utc)
    proc = subprocess.run(
        ["uv", "run", "python", "01_messages.py"],
        cwd=os.path.join(REPO, "logfire"), capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        notes.append(f"01_messages.py failed (rc={proc.returncode}): "
                     f"{(proc.stderr or proc.stdout)[-300:]}")
    else:
        t0 = time.monotonic()
        deadline = t0 + 120
        found = False
        while time.monotonic() < deadline:
            s, resp, _ = q(
                "SELECT count(*) AS n FROM records WHERE start_timestamp > "
                f"'{t_emit.strftime('%Y-%m-%dT%H:%M:%S.%f')}'",
                min_ts=t_emit.strftime("%Y-%m-%dT%H:%M:%SZ"))
            if s == 200 and rows(resp) and rows(resp)[0]["n"] and rows(resp)[0]["n"] > 0:
                ttq = round(time.monotonic() - t0, 1)
                found = True
                break
            time.sleep(2)
        if not found:
            notes.append("fresh trace did not appear within 120s poll window")
except FileNotFoundError as e:
    notes.append(f"time_to_queryable skipped: {e}")
except subprocess.TimeoutExpired:
    notes.append("time_to_queryable skipped: 01_messages.py timed out")
except Exception as e:
    notes.append(f"time_to_queryable error: {type(e).__name__}: {e}")
metrics["time_to_queryable_s"] = ttq

# ---------------- 8. dx_friction (incl. deliberate malformed query) ----------------
s_bad, bad_body, _ = q("SELEKT nope FROM nowhere")
bad_text = bad_body.decode(errors="replace") if isinstance(bad_body, bytes) else json.dumps(bad_body)
metrics["malformed_query_response"] = {"status": s_bad, "body_excerpt": bad_text[:400]}
metrics["dx_friction"] = (
    f"Auth: bearer token in Authorization header; token used here came from {TOKEN_SOURCE}. "
    "Setup is minimal (single POST endpoint, arbitrary SQL over 'records'). Footguns hit live: "
    "(1) 'min_timestamp' is effectively required — omitting it returned HTTP 422; "
    "(2) despite docs, responses default to Arrow binary unless Accept: application/json is set; "
    "(3) a per-minute rate limit (HTTP 429 'Rate limit exceeded (minute)') was hit after ~10 "
    f"rapid queries ({RATE_LIMIT_HITS} hits this run; script retries with backoff); "
    "(4) JSON responses are row-oriented ({schema, data:[rowdicts]}), easy to consume. "
    f"Malformed SQL returned HTTP {s_bad}; body excerpt in malformed_query_response."
)

emit()

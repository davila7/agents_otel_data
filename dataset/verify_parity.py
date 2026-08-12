#!/usr/bin/env python3
"""Parity gate: verify each platform holds the synthetic dataset described by
out/manifest.json.

Run AFTER send.py. For every requested platform this script counts what the
platform's read API actually returns for the pilot dataset and compares the
count against the manifest's span count. It is a GATE:

  exit 0  — every checked platform is within --tolerance of the manifest
  exit 1  — any platform out of tolerance, unreachable, or missing creds

Usage:
    cd dataset
    uv run --with requests python verify_parity.py \
        --platforms logfire,braintrust,langsmith,langfuse,phoenix \
        --manifest ./out/manifest.json --tolerance 0.01

Credentials are read from each platform's gitignored .env file
(../<platform>/.env). Secret VALUES are never printed — only env var NAMES
appear in output.

What one row equals, per platform (verified against the eval scripts):
  logfire    SQL count over `records` — one row per OTel span.
  braintrust /fetch events are span-level rows (one event per span; a trace is
             the set of rows sharing root_span_id). Deduped by id because
             fetch pagination can re-return rows.
  langsmith  runs. For OTLP ingest spans map 1:1 to runs, so run count is
             span count; the report also includes the root-run count so the
             1:1 assumption is auditable against manifest trace count.
  langfuse   v2 observations — one observation per span. Trace count is
             reported as distinct traceId.
  phoenix    spans, directly.

Writes parity_report.json next to the manifest and prints a table on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALL_PLATFORMS = ["logfire", "braintrust", "langsmith", "langfuse", "phoenix"]

# Defaults; the manifest can override via manifest["projects"][platform] and
# manifest["service_name"].
DEFAULT_PROJECT = "otel-dataset-pilot"
DEFAULT_SERVICE_NAME = "dataset-pilot"

TIME_PAD = timedelta(minutes=5)  # pad manifest bounds against clock skew


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_env(platform: str) -> dict:
    """Parse ../<platform>/.env into a dict. Never print values."""
    env = {}
    path = os.path.join(REPO, platform, ".env")
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = re.sub(r"^export\s+", "", line)
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            env[k.strip()] = v
    return env


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def first_key(d: dict, keys: list[str]):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def read_manifest(path: str) -> dict:
    with open(path) as f:
        m = json.load(f)
    expected_spans = first_key(m, ["total_spans", "span_count", "spans", "n_spans"])
    if isinstance(expected_spans, list):
        expected_spans = len(expected_spans)
    expected_traces = first_key(m, ["total_traces", "trace_count", "traces", "n_traces"])
    if isinstance(expected_traces, list):
        expected_traces = len(expected_traces)
    # generate.py writes nested time_range.start/.end and args.anchor (the
    # END of the window); flat keys are kept as fallbacks for older manifests.
    time_range = m.get("time_range") or {}
    t_start = parse_ts(time_range.get("start")) or parse_ts(first_key(
        m, ["time_start", "anchor_start", "min_start_time", "start_time"]))
    t_end = (parse_ts(time_range.get("end"))
             or parse_ts(first_key(
                 m, ["time_end", "anchor_end", "max_end_time", "end_time"]))
             # anchor is defined as the end of the window, never the start
             or parse_ts((m.get("args") or {}).get("anchor"))
             or parse_ts(m.get("anchor")))
    if expected_spans is None:
        raise SystemExit(f"manifest {path} has no span count "
                         "(looked for total_spans/span_count/spans/n_spans)")
    return {
        "raw": m,
        "expected_spans": int(expected_spans),
        "expected_traces": int(expected_traces) if expected_traces is not None else None,
        "t_start": t_start,
        "t_end": t_end,
        "service_name": m.get("service_name", DEFAULT_SERVICE_NAME),
        "projects": m.get("projects", {}),
    }


class Http:
    """Session wrapper with 429 retry and optional fixed pacing."""

    def __init__(self, session: requests.Session, min_interval: float = 0.0):
        self.s = session
        self.min_interval = min_interval
        self._last = 0.0

    def request(self, method: str, url: str, **kw) -> requests.Response:
        for attempt in range(6):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            r = self.s.request(method, url, timeout=60, **kw)
            self._last = time.monotonic()
            if r.status_code != 429:
                return r
            ra = r.headers.get("Retry-After")
            try:
                backoff = min(float(ra), 90) if ra else min(5 * 2 ** attempt, 90)
            except ValueError:
                backoff = min(5 * 2 ** attempt, 90)
            log(f"    429 rate-limited; sleeping {backoff:.0f}s")
            time.sleep(backoff)
        return r

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


# --------------------------------------------------------------------------
# per-platform counters
# Each returns: {"found": int|None, "method": str, "notes": [...], "error": str|None}
# --------------------------------------------------------------------------

def count_logfire(env, mf, args) -> dict:
    tok = env.get("LOGFIRE_READ_TOKEN")
    if not tok:
        return {"found": None, "method": "logfire /v2/query SQL count",
                "error": "LOGFIRE_READ_TOKEN missing in logfire/.env", "notes": []}
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {tok}",
                      "Accept": "application/json",  # Arrow binary by default!
                      "Content-Type": "application/json"})
    # Logfire rate limit is ~10 queries/min; we need a single query, pace anyway.
    http = Http(s, min_interval=6.5)
    svc = mf["service_name"]
    where = f"service_name = '{svc}'"
    if mf["t_end"] is not None:
        where += f" AND start_timestamp <= '{iso(mf['t_end'] + TIME_PAD)}'"
    sql = f"SELECT count(*) AS n FROM records WHERE {where}"
    body = {"sql": sql,
            # min_timestamp is REQUIRED (422 otherwise) and doubles as lower bound
            "min_timestamp": iso((mf["t_start"] - TIME_PAD) if mf["t_start"]
                                 else datetime.now(timezone.utc) - timedelta(days=30))}
    r = http.post("https://logfire-us.pydantic.dev/v2/query", json=body)
    if r.status_code != 200:
        return {"found": None, "method": "logfire /v2/query SQL count",
                "error": f"HTTP {r.status_code}: {r.text[:200]}", "notes": []}
    data = r.json().get("data", [])
    n = int(data[0]["n"]) if data else 0
    return {"found": n,
            "method": f"SQL count(*) over records WHERE service_name='{svc}' "
                      "(one row per span) via POST /v2/query",
            "notes": [], "error": None}


def count_braintrust(env, mf, args) -> dict:
    key = env.get("BRAINTRUST_API_KEY")
    method = "braintrust /v1/project_logs/{id}/fetch, cursor pages, dedupe by id"
    if not key:
        return {"found": None, "method": method,
                "error": "BRAINTRUST_API_KEY missing in braintrust/.env", "notes": []}
    api = "https://api.braintrust.dev"
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {key}"
    http = Http(s, min_interval=0.3)
    project = mf["projects"].get("braintrust", DEFAULT_PROJECT)
    r = http.get(f"{api}/v1/project", params={"project_name": project})
    if r.status_code != 200:
        return {"found": None, "method": method,
                "error": f"project lookup HTTP {r.status_code}: {r.text[:200]}", "notes": []}
    objs = r.json().get("objects", [])
    if not objs:
        return {"found": None, "method": method,
                "error": f"braintrust project '{project}' not found", "notes": []}
    pid = objs[0]["id"]
    notes = [f"project '{project}' id={pid}",
             "one /fetch event == one span-level row (trace = rows sharing "
             "root_span_id); pagination can re-return rows, so ids are deduped"]
    ids: set[str] = set()
    root_ids: set[str] = set()
    cursor = None
    pages = 0
    while pages < args.max_pages:
        params = {"limit": args.page_size}
        if cursor:
            params["cursor"] = cursor
        r = http.get(f"{api}/v1/project_logs/{pid}/fetch", params=params)
        if r.status_code != 200:
            return {"found": None, "method": method, "notes": notes,
                    "error": f"fetch HTTP {r.status_code}: {r.text[:200]}"}
        body = r.json()
        events = body.get("events", [])
        for e in events:
            ids.add(e["id"])
            if e.get("root_span_id"):
                root_ids.add(e["root_span_id"])
        pages += 1
        if pages % 10 == 0:
            log(f"    braintrust: page {pages}, {len(ids)} unique rows so far")
        cursor = body.get("cursor")
        if not cursor or not events:
            break
    else:
        notes.append(f"HIT --max-pages={args.max_pages}; count is a lower bound")
    notes.append(f"distinct root_span_id (~traces): {len(root_ids)}")
    return {"found": len(ids), "method": method, "notes": notes, "error": None}


def count_langsmith(env, mf, args) -> dict:
    key = env.get("LANGSMITH_API_KEY")
    method = "langsmith POST /runs/query, cursor pages, count runs (1 run == 1 span for OTLP ingest)"
    if not key:
        return {"found": None, "method": method,
                "error": "LANGSMITH_API_KEY missing in langsmith/.env", "notes": []}
    api = "https://api.smith.langchain.com/api/v1"
    s = requests.Session()
    s.headers["X-Api-Key"] = key
    http = Http(s, min_interval=1.5)  # 10 req / 10 s rate limit
    project = mf["projects"].get("langsmith", DEFAULT_PROJECT)
    r = http.get(f"{api}/sessions", params={"name": project})
    if r.status_code != 200:
        return {"found": None, "method": method,
                "error": f"sessions lookup HTTP {r.status_code}: {r.text[:200]}", "notes": []}
    sessions = r.json()
    if not sessions:
        return {"found": None, "method": method,
                "error": f"langsmith project '{project}' not found", "notes": []}
    pid = sessions[0]["id"]
    notes = [f"project '{project}' id={pid}",
             "LangSmith counts RUNS; OTLP ingest maps spans 1:1 to runs — the "
             "root-run count below vs manifest trace count is the audit for that"]
    flt_parts = []
    if mf["t_start"]:
        flt_parts.append(f'gt(start_time, "{iso(mf["t_start"] - TIME_PAD)}")')
    if mf["t_end"]:
        flt_parts.append(f'lt(start_time, "{iso(mf["t_end"] + TIME_PAD)}")')
    flt = f"and({', '.join(flt_parts)})" if len(flt_parts) > 1 else (
        flt_parts[0] if flt_parts else None)
    page_size = min(args.page_size, 100)  # /runs/query caps at 100
    total = 0
    roots = 0
    cursor = None
    pages = 0
    while pages < args.max_pages:
        body = {"session": [pid], "limit": page_size}
        if flt:
            body["filter"] = flt
        if cursor:
            body["cursor"] = cursor
        r = http.post(f"{api}/runs/query", json=body)
        if r.status_code != 200:
            return {"found": None, "method": method, "notes": notes,
                    "error": f"runs/query HTTP {r.status_code}: {r.text[:200]}"}
        j = r.json()
        runs = j.get("runs", [])
        total += len(runs)
        roots += sum(1 for x in runs if not x.get("parent_run_id"))
        pages += 1
        if pages % 10 == 0:
            log(f"    langsmith: page {pages}, {total} runs so far")
        cursor = (j.get("cursors") or {}).get("next")
        if not cursor or not runs:
            break
    else:
        notes.append(f"HIT --max-pages={args.max_pages}; count is a lower bound")
    notes.append(f"root runs (~traces): {roots}")
    return {"found": total, "method": method, "notes": notes, "error": None}


def count_langfuse(env, mf, args) -> dict:
    host = env.get("LANGFUSE_HOST", "").rstrip("/")
    pub, sec = env.get("LANGFUSE_PUBLIC_KEY"), env.get("LANGFUSE_SECRET_KEY")
    method = "langfuse GET /api/public/v2/observations, fromStartTime/toStartTime, cursor pages"
    if mf["t_start"] is None or mf["t_end"] is None:
        # The time window is Langfuse's ONLY isolation mechanism (the project
        # is fixed by the API key pair) — counting without bounds would count
        # every trace in the project, so this is a hard failure, not a
        # silent unbounded count.
        return {"found": None, "method": method, "notes": [],
                "error": "manifest has no time bounds (time_range.start/end); "
                         "refusing to count Langfuse without a time window — "
                         "it is the only isolation boundary for this platform"}
    if not (host and pub and sec):
        return {"found": None, "method": method, "notes": [],
                "error": "LANGFUSE_HOST/LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY "
                         "missing in langfuse/.env"}
    s = requests.Session()
    s.auth = (pub, sec)
    http = Http(s, min_interval=0.3)
    params_base = {"limit": min(args.page_size, 1000)}
    if mf["t_start"]:
        params_base["fromStartTime"] = iso(mf["t_start"] - TIME_PAD)
    if mf["t_end"]:
        params_base["toStartTime"] = iso(mf["t_end"] + TIME_PAD)
    notes = ["one observation == one span; keys are project-scoped so the time "
             "window is the dataset boundary"]
    total = 0
    trace_ids: set[str] = set()
    cursor = None
    pages = 0
    while pages < args.max_pages:
        params = dict(params_base)
        if cursor:
            params["cursor"] = cursor
        r = http.get(f"{host}/api/public/v2/observations", params=params)
        if r.status_code != 200:
            return {"found": None, "method": method, "notes": notes,
                    "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        j = r.json()
        data = j.get("data", [])
        total += len(data)
        for o in data:
            if o.get("traceId"):
                trace_ids.add(o["traceId"])
        pages += 1
        if pages % 10 == 0:
            log(f"    langfuse: page {pages}, {total} observations so far")
        cursor = (j.get("meta") or {}).get("cursor")
        if not cursor or not data:
            break
    else:
        notes.append(f"HIT --max-pages={args.max_pages}; count is a lower bound")
    notes.append(f"distinct traceId (traces): {len(trace_ids)}")
    return {"found": total, "method": method, "notes": notes, "error": None}


def count_phoenix(env, mf, args) -> dict:
    base = env.get("PHOENIX_COLLECTOR_ENDPOINT", "").rstrip("/")
    key = env.get("PHOENIX_API_KEY")
    project = mf["projects"].get("phoenix", DEFAULT_PROJECT)
    method = f"phoenix GET /v1/projects/{project}/spans, cursor pages"
    if not (base and key):
        return {"found": None, "method": method, "notes": [],
                "error": "PHOENIX_COLLECTOR_ENDPOINT/PHOENIX_API_KEY missing in phoenix/.env"}
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {key}"
    http = Http(s, min_interval=0.3)
    params_base = {"limit": min(args.page_size, 1000)}
    if mf["t_start"]:
        params_base["start_time"] = iso(mf["t_start"] - TIME_PAD)
    if mf["t_end"]:
        params_base["end_time"] = iso(mf["t_end"] + TIME_PAD)
    notes = ["rows are spans directly"]
    total = 0
    cursor = None
    pages = 0
    while pages < args.max_pages:
        params = dict(params_base)
        if cursor:
            params["cursor"] = cursor
        r = http.get(f"{base}/v1/projects/{project}/spans", params=params)
        if r.status_code == 404:
            return {"found": None, "method": method, "notes": notes,
                    "error": f"phoenix project '{project}' not found (HTTP 404)"}
        if r.status_code != 200:
            return {"found": None, "method": method, "notes": notes,
                    "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        j = r.json()
        data = j.get("data", [])
        total += len(data)
        pages += 1
        if pages % 10 == 0:
            log(f"    phoenix: page {pages}, {total} spans so far")
        cursor = j.get("next_cursor")
        if not cursor or not data:
            break
    else:
        notes.append(f"HIT --max-pages={args.max_pages}; count is a lower bound")
    return {"found": total, "method": method, "notes": notes, "error": None}


COUNTERS = {
    "logfire": count_logfire,
    "braintrust": count_braintrust,
    "langsmith": count_langsmith,
    "langfuse": count_langfuse,
    "phoenix": count_phoenix,
}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Parity gate after send.py")
    ap.add_argument("--platforms", default=",".join(ALL_PLATFORMS),
                    help="comma-separated subset of: " + ",".join(ALL_PLATFORMS))
    ap.add_argument("--manifest", default="./out/manifest.json")
    ap.add_argument("--tolerance", type=float, default=0.01,
                    help="allowed relative deviation from manifest span count (0.01 = 1%%)")
    ap.add_argument("--window", type=float, default=None,
                    help="hours: override time bounds as [now - window, now] "
                         "instead of the manifest's anchor bounds")
    ap.add_argument("--page-size", type=int, default=500)
    ap.add_argument("--max-pages", type=int, default=500,
                    help="hard cap on pages per platform")
    ap.add_argument("--report", default=None,
                    help="output path (default: parity_report.json next to the manifest)")
    args = ap.parse_args()

    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    unknown = [p for p in platforms if p not in COUNTERS]
    if unknown:
        ap.error(f"unknown platform(s): {unknown}; choose from {ALL_PLATFORMS}")

    mf = read_manifest(args.manifest)
    if args.window is not None:
        mf["t_end"] = datetime.now(timezone.utc)
        mf["t_start"] = mf["t_end"] - timedelta(hours=args.window)
    expected = mf["expected_spans"]
    log(f"manifest: {expected} spans"
        + (f", {mf['expected_traces']} traces" if mf["expected_traces"] else "")
        + (f", window {iso(mf['t_start'])} .. {iso(mf['t_end'])}"
           if mf["t_start"] and mf["t_end"] else ", no time bounds"))

    report = {}
    all_ok = True
    for p in platforms:
        log(f"[{p}] counting ...")
        env = load_env(p)
        t0 = time.monotonic()
        try:
            res = COUNTERS[p](env, mf, args)
        except requests.RequestException as e:
            res = {"found": None, "method": COUNTERS[p].__name__,
                   "notes": [], "error": f"{type(e).__name__}: {e}"}
        wall = round(time.monotonic() - t0, 1)
        found = res["found"]
        pct = round(found / expected * 100, 2) if (found is not None and expected) else None
        ok = (found is not None
              and abs(found - expected) <= args.tolerance * expected)
        all_ok = all_ok and ok
        report[p] = {
            "expected": expected,
            "found": found,
            "pct": pct,
            "ok": ok,
            "method": res["method"],
            "wall_seconds": wall,
            "notes": res.get("notes", []),
            "error": res.get("error"),
        }
        log(f"[{p}] found={found} expected={expected} "
            f"({pct if pct is not None else '—'}%) in {wall}s -> "
            f"{'OK' if ok else 'FAIL'}")

    out_path = args.report or os.path.join(
        os.path.dirname(os.path.abspath(args.manifest)), "parity_report.json")
    payload = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "manifest": os.path.abspath(args.manifest),
        "tolerance": args.tolerance,
        "expected_spans": expected,
        "expected_traces": mf["expected_traces"],
        "platforms": report,
        "pass": all_ok,
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    # human table
    w = max(len(p) for p in platforms) + 2
    print(f"\n{'platform':<{w}}{'expected':>10}{'found':>10}{'pct':>9}"
          f"{'wall_s':>9}  status")
    print("-" * (w + 44))
    for p in platforms:
        r = report[p]
        found = r["found"] if r["found"] is not None else "—"
        pct = f"{r['pct']}%" if r["pct"] is not None else "—"
        status = "OK" if r["ok"] else ("ERROR: " + r["error"][:60] if r["error"] else "FAIL")
        print(f"{p:<{w}}{r['expected']:>10}{found:>10}{pct:>9}"
              f"{r['wall_seconds']:>9}  {status}")
    print(f"\nreport: {out_path}")
    print("PARITY GATE:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Query-performance benchmark over the synthetic corpus (read-only).

Runs three probes per platform against the project holding the corpus,
bounded by the manifest time window:

  1. filtered_scan  — server-side filter for one model's LLM spans
                      ("chat claude-haiku-4-5"), first 100 rows.
                      Timed, median of 3.
  2. aggregation    — server-side "span count + token sum per model"
                      where the platform supports it. Timed, median of 3.
                      Platforms without server-side aggregation are
                      reported unsupported (computed client-side from the
                      export instead, cost folded into that probe).
  3. full_export    — page through every corpus row in the window.
                      Timed once; reports rows/s. Rate limits are part of
                      the number: export throughput as actually experienced.

Usage:
    cd dataset && uv run python query_bench.py --manifest out_v2/manifest.json

Writes <manifest_dir>/query_bench.json. Never prints secret values.
Known asymmetry (documented in the report): the Braintrust and Langfuse
projects also hold the earlier 7-day corpus (~10k extra rows), so their
whole-project scans cover ~2x the rows of the others.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import time

import requests

from verify_parity import TIME_PAD, Http, iso, load_env, log, read_manifest

MODEL = "claude-haiku-4-5"
LLM_SPAN_NAME = f"chat {MODEL}"


def timed(fn, n=3):
    """Run fn n times; return (median_ms, last_result)."""
    samples, out = [], None
    for _ in range(n):
        t0 = time.perf_counter()
        out = fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(samples), 1), out


# --------------------------------------------------------------------------
# per-platform benches: each returns the probe dict
# --------------------------------------------------------------------------

def bench_logfire(env, mf):
    # No Http pacing wrapper here: the ~10 q/min budget is paid with explicit
    # sleeps OUTSIDE the timed window, so timings measure the query engine,
    # not our client-side rate limiting.
    s = _sess({"Authorization": f"Bearer {env['LOGFIRE_READ_TOKEN']}",
               "Content-Type": "application/json",
               "Accept": "application/json"})
    svc = mf["service_name"]
    lo, hi = iso(mf["t_start"] - TIME_PAD), iso(mf["t_end"] + TIME_PAD)
    base_where = (f"service_name = '{svc}' AND start_timestamp >= '{lo}' "
                  f"AND start_timestamp <= '{hi}'")
    PACE = 7.0

    def q(sql, limit=None):
        # /v2/query returns at most 100 rows unless a `limit` body param is
        # set (the SQL LIMIT alone is not enough) — verified live 2026-08-13
        body = {"sql": sql, "min_timestamp": lo}
        if limit:
            body["limit"] = limit
        for attempt in range(4):
            r = s.post("https://logfire-us.pydantic.dev/v2/query",
                       json=body, timeout=60)
            if r.status_code != 429:
                break
            time.sleep(min(float(r.headers.get("Retry-After", 15)), 90))
        r.raise_for_status()
        return r.json()["data"]

    def timed_paced(fn, n=3):
        samples, out = [], None
        for _ in range(n):
            time.sleep(PACE)  # rate-limit budget, untimed
            t0 = time.perf_counter()
            out = fn()
            samples.append((time.perf_counter() - t0) * 1000)
        return round(statistics.median(samples), 1), out

    scan_ms, rows = timed_paced(lambda: q(
        f"SELECT span_name, trace_id, attributes FROM records WHERE {base_where} "
        f"AND attributes->>'gen_ai.request.model' = '{MODEL}' LIMIT 100"))
    agg_ms, agg = timed_paced(lambda: q(
        f"SELECT attributes->>'gen_ai.request.model' AS model, count(*) AS n, "
        f"sum(CAST(attributes->>'gen_ai.usage.input_tokens' AS BIGINT)) AS in_tok "
        f"FROM records WHERE {base_where} "
        f"AND attributes->>'gen_ai.request.model' IS NOT NULL GROUP BY model"))

    total, offset, query_s = 0, 0, 0.0
    t_wall = time.perf_counter()
    while True:
        time.sleep(PACE)  # untimed pacing between export pages
        t0 = time.perf_counter()
        page = q(f"SELECT span_name, trace_id, span_id, start_timestamp, attributes "
                 f"FROM records WHERE {base_where} LIMIT 5000 OFFSET {offset}",
                 limit=5000)
        query_s += time.perf_counter() - t0
        total += len(page)
        offset += 5000
        if len(page) < 5000:
            break
    export_s = round(query_s, 1)
    wall_s = round(time.perf_counter() - t_wall, 1)
    return {
        "filtered_scan_ms": scan_ms, "filtered_rows": len(rows),
        "aggregation_ms": agg_ms, "aggregation_supported": True,
        "aggregation_sample": agg[:5],
        "export_rows": total, "export_wall_s": export_s,
        "export_wall_incl_pacing_s": wall_s,
        "export_rows_per_s": round(total / export_s, 1),
        "method": "SQL over records via POST /v2/query (pages of 5000). "
                  "Client-side 7s pacing for the ~10 q/min limit is excluded "
                  "from all timings (export_wall_incl_pacing_s has it included); "
                  "429 retries happen outside the timed window",
    }


def bench_braintrust(env, mf):
    http = Http(_sess({"Authorization": f"Bearer {env['BRAINTRUST_API_KEY']}"}),
                min_interval=0.3)
    api = "https://api.braintrust.dev"
    project = mf["projects"]["braintrust"]
    pid = http.request("GET", f"{api}/v1/project",
                       params={"project_name": project}).json()["objects"][0]["id"]
    lo_ts = (mf["t_start"] - TIME_PAD).timestamp()
    hi_ts = (mf["t_end"] + TIME_PAD).timestamp()

    def btql(query):
        r = http.request("POST", f"{api}/btql", json={"query": query})
        r.raise_for_status()
        return r.json()

    # SQL over /btql is the canonical read path since PR #7 (the BTQL pipe
    # syntax remains accepted; /fetch is the legacy export route)
    frm = f"FROM project_logs('{pid}')"
    win = f"metrics.start > {lo_ts} AND metrics.start < {hi_ts}"
    scan_ms, scan = timed(lambda: btql(
        f"SELECT id, metadata, metrics {frm} "
        f"WHERE metadata.model = '{MODEL}' AND {win} LIMIT 100"))
    agg_ms, agg = timed(lambda: btql(
        f"SELECT metadata.model AS model, count(1) AS n, "
        f"sum(metrics.prompt_tokens) AS in_tok {frm} WHERE {win} "
        f"GROUP BY metadata.model"))

    t0 = time.perf_counter()
    total, cursor = 0, None
    while True:
        q = (f"SELECT id, metadata, metrics {frm} WHERE {win} "
             f"ORDER BY _pagination_key DESC LIMIT 1000")
        if cursor:
            q += f" OFFSET '{cursor}'"
        body = btql(q)
        rows = body.get("data", [])
        total += len(rows)
        cursor = body.get("cursor")
        if not cursor or not rows:
            break
    export_s = round(time.perf_counter() - t0, 1)
    return {
        "filtered_scan_ms": scan_ms, "filtered_rows": len(scan.get("data", [])),
        "aggregation_ms": agg_ms, "aggregation_supported": True,
        "aggregation_sample": [a for a in agg.get("data", []) if a.get("model")][:5],
        "export_rows": total, "export_wall_s": export_s,
        "export_rows_per_s": round(total / export_s, 1),
        "method": "SQL over /btql for scan/agg/export (cursor OFFSET pages of "
                  "1000); project also holds the 7-day corpus — the time "
                  "window separates them server-side now",
    }


def bench_langsmith(env, mf):
    # v2 runs/query APIs (the deprecated v1 path measured ~15x slower on the
    # eval side and was retired from this repo in PR #6); 429s are handled by
    # Http, so pacing is light and the API's own limits show up in the numbers
    http = Http(_sess({"X-Api-Key": env["LANGSMITH_API_KEY"]}),
                min_interval=0.2)
    api_v1 = "https://api.smith.langchain.com/api/v1"
    api_v2 = "https://api.smith.langchain.com/v2"
    project = mf["projects"]["langsmith"]
    session = http.request("GET", f"{api_v1}/sessions",
                           params={"name": project}).json()[0]
    sid = session["id"]
    http.s.headers["X-Tenant-Id"] = session["tenant_id"]
    lo, hi = iso(mf["t_start"] - TIME_PAD), iso(mf["t_end"] + TIME_PAD)
    base = {"project_ids": [sid], "min_start_time": lo,
            "filter": f'lt(start_time, "{hi}")',
            "selects": ["ID", "NAME", "RUN_TYPE", "START_TIME", "TRACE_ID"]}

    def q(body):
        r = http.request("POST", f"{api_v2}/runs/query", json=body)
        r.raise_for_status()
        return r.json()

    scan_ms, res = timed(lambda: q({
        **base, "page_size": 100,
        "filter": f'and(eq(name, "{LLM_SPAN_NAME}"), lt(start_time, "{hi}"))'}))
    # /runs/stats is the only server-side aggregate: fixed shape, no group-by
    # /runs/stats remains v1 and is still the only server-side aggregate
    win = f'and(gt(start_time, "{lo}"), lt(start_time, "{hi}"))'
    agg_ms, stats = timed(lambda: http.request(
        "POST", f"{api_v1}/runs/stats",
        json={"session": [sid], "filter": win}).json())

    t0 = time.perf_counter()
    total, cursor = 0, None
    while True:
        body = {**base, "page_size": 100}
        if cursor:
            body["cursor"] = cursor
        page = q(body)
        total += len(page.get("items", []))
        cursor = page.get("next_cursor")
        if not cursor:
            break
    export_s = round(time.perf_counter() - t0, 1)
    return {
        "filtered_scan_ms": scan_ms, "filtered_rows": len(res.get("items", [])),
        "aggregation_ms": agg_ms, "aggregation_supported": "fixed-shape only",
        "aggregation_sample": {k: stats.get(k) for k in
                               ("run_count", "total_tokens", "prompt_tokens")},
        "export_rows": total, "export_wall_s": export_s,
        "export_rows_per_s": round(total / export_s, 1),
        "method": "v2 runs/query filter DSL (X-Tenant-Id, next_cursor), export "
                  "pages of 100; stats via v1 /runs/stats (no group-by)",
    }


def bench_langfuse(env, mf):
    auth = base64.b64encode(
        f"{env['LANGFUSE_PUBLIC_KEY']}:{env['LANGFUSE_SECRET_KEY']}".encode()
    ).decode()
    http = Http(_sess({"Authorization": f"Basic {auth}"}), min_interval=0.15)
    host = env["LANGFUSE_HOST"].rstrip("/")
    lo, hi = iso(mf["t_start"] - TIME_PAD), iso(mf["t_end"] + TIME_PAD)
    obs = f"{host}/api/public/v2/observations"

    def get(url, **params):
        r = http.request("GET", url, params=params)
        r.raise_for_status()
        return r.json()

    scan_ms, page = timed(lambda: get(
        obs, name=LLM_SPAN_NAME, fromStartTime=lo, toStartTime=hi, limit=100))
    mq = {"view": "observations",
          "metrics": [{"measure": "count", "aggregation": "count"},
                      {"measure": "inputTokens", "aggregation": "sum"}],
          "dimensions": [{"field": "providedModelName"}],
          "fromTimestamp": lo, "toTimestamp": hi}
    try:
        agg_ms, agg = timed(lambda: get(f"{host}/api/public/metrics",
                                        query=json.dumps(mq)))
        agg_supported, agg_sample = True, agg.get("data", [])[:5]
    except requests.HTTPError as e:  # metrics field names are a moving target
        agg_ms, agg_supported = None, f"metrics API error: {e.response.status_code} {e.response.text[:150]}"
        agg_sample = None

    t0 = time.perf_counter()
    total, cursor = 0, None
    while True:
        params = {"fromStartTime": lo, "toStartTime": hi, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        body = get(obs, **params)
        total += len(body.get("data", []))
        cursor = (body.get("meta") or {}).get("cursor")
        if not cursor or not body.get("data"):
            break
    export_s = round(time.perf_counter() - t0, 1)
    return {
        "filtered_scan_ms": scan_ms, "filtered_rows": len(page.get("data", [])),
        "aggregation_ms": agg_ms, "aggregation_supported": agg_supported,
        "aggregation_sample": agg_sample,
        "export_rows": total, "export_wall_s": export_s,
        "export_rows_per_s": round(total / export_s, 1),
        "method": "v2 observations (name filter, cursor pages of 1000); "
                  "metrics API for aggregation (project also holds the 7-day "
                  "corpus + demo traces; time window separates them)",
    }


def bench_phoenix(env, mf):
    http = Http(_sess({"Authorization": f"Bearer {env['PHOENIX_API_KEY']}"}),
                min_interval=0.15)
    base = env["PHOENIX_COLLECTOR_ENDPOINT"].rstrip("/")
    project = mf["projects"]["phoenix"]
    spans = f"{base}/v1/projects/{project}/spans"
    lo, hi = iso(mf["t_start"] - TIME_PAD), iso(mf["t_end"] + TIME_PAD)

    def get(**params):
        r = http.request("GET", spans, params=params)
        r.raise_for_status()
        return r.json()

    scan_ms, page = timed(lambda: get(
        attribute=f"gen_ai.request.model:{MODEL}",
        start_time=lo, end_time=hi, limit=100))

    t0 = time.perf_counter()
    total, cursor, by_model = 0, None, {}
    while True:
        params = {"start_time": lo, "end_time": hi, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        body = get(**params)
        rows = body.get("data", [])
        total += len(rows)
        for s in rows:  # client-side aggregation: phoenix has no server-side agg
            a = s.get("attributes") or {}
            m = a.get("gen_ai.request.model")
            if m:
                agg = by_model.setdefault(m, {"n": 0, "in_tok": 0})
                agg["n"] += 1
                agg["in_tok"] += a.get("gen_ai.usage.input_tokens") or 0
        cursor = body.get("next_cursor")
        if not cursor or not rows:
            break
    export_s = round(time.perf_counter() - t0, 1)
    return {
        "filtered_scan_ms": scan_ms, "filtered_rows": len(page.get("data", [])),
        "aggregation_ms": None, "aggregation_supported": False,
        "aggregation_sample": [{"model": m, **v} for m, v in sorted(by_model.items())],
        "export_rows": total, "export_wall_s": export_s,
        "export_rows_per_s": round(total / export_s, 1),
        "method": "REST /spans (attribute filter, cursor pages of 1000); no "
                  "server-side aggregation — per-model rollup computed "
                  "client-side during the export pass",
    }


# --------------------------------------------------------------------------

def _sess(headers):
    s = requests.Session()
    s.headers.update(headers)
    return s


BENCHES = {
    "logfire": bench_logfire,
    "braintrust": bench_braintrust,
    "langsmith": bench_langsmith,
    "langfuse": bench_langfuse,
    "phoenix": bench_phoenix,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", default="./out/manifest.json")
    ap.add_argument("--platforms", default=",".join(BENCHES))
    args = ap.parse_args()

    mf = read_manifest(args.manifest)
    report = {"manifest": os.path.abspath(args.manifest),
              "corpus_spans": mf["expected_spans"],
              "model_filter": MODEL, "platforms": {}}

    for name in args.platforms.split(","):
        name = name.strip()
        log(f"[{name}] benching ...")
        env = load_env(name)
        try:
            report["platforms"][name] = BENCHES[name](env, mf)
        except Exception as e:  # noqa: BLE001 — keep going, record the failure
            report["platforms"][name] = {"error": f"{type(e).__name__}: {e}"[:300]}
        log(f"[{name}] done")

    out_path = os.path.join(os.path.dirname(os.path.abspath(args.manifest)),
                            "query_bench.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\ncorpus: {report['corpus_spans']} spans · filter model: {MODEL}\n")
    hdr = f"{'platform':<11} {'scan p50':>9} {'agg p50':>9} {'export':>8} {'rows/s':>8}"
    print(hdr + "\n" + "-" * len(hdr))
    for p, d in report["platforms"].items():
        if "error" in d:
            print(f"{p:<11} ERROR: {d['error'][:60]}")
            continue
        agg = f"{d['aggregation_ms']:.0f}ms" if d.get("aggregation_ms") else "client"
        print(f"{p:<11} {d['filtered_scan_ms']:>7.0f}ms {agg:>9} "
              f"{d['export_wall_s']:>7.1f}s {d['export_rows_per_s']:>8}")
    print(f"\nreport: {out_path}")


if __name__ == "__main__":
    main()

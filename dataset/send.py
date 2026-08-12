#!/usr/bin/env python3
"""Replay the generated OTLP corpus — the identical bytes — to each platform.

Reads out/manifest.json, verifies every chunk's sha256 against the manifest,
then POSTs each chunk's raw ExportTraceServiceRequest bytes to every requested
platform's OTLP/HTTP endpoint, exactly as generated. No per-platform SDK, no
re-serialization: one platform receiving different data can only be that
platform's ingest pipeline (which verify_parity.py then surfaces).

Endpoints and headers come from platforms.json; credentials are ${env:VAR}
placeholders resolved from the platform's gitignored .env file (env_file in
platforms.json) plus the process environment. Secret VALUES are never printed
— output shows only placeholder/env-var NAMES, and any secret that would leak
through an HTTP error body is masked.

Usage:
    cd dataset
    uv run --with requests python send.py \
        --platforms logfire,braintrust,langsmith,langfuse,phoenix \
        --manifest ./out/manifest.json
    # add --dry-run to validate config/credentials and print the plan
    # without any network I/O

Retry policy: 429 and 5xx are retried with exponential backoff (Retry-After
honored, capped); any other 4xx is a configuration/auth error and fails that
platform immediately. Writes send_report.json next to the manifest; exit 0
only if every requested platform received every chunk.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PLACEHOLDER = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
MAX_BACKOFF = 90.0


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_env_file(path: str) -> dict:
    """Parse a .env file into a dict. Never print values."""
    env: dict[str, str] = {}
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
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def resolve_platform_env(platform: str, cfg: dict) -> dict:
    """Env for placeholder resolution: platform .env overlaid on os.environ."""
    env = dict(os.environ)
    env_file = cfg.get("env_file")
    if env_file:
        env.update(load_env_file(os.path.join(HERE, env_file)))
    if platform == "langfuse" and not env.get("LANGFUSE_BASIC_AUTH"):
        pub, sec = env.get("LANGFUSE_PUBLIC_KEY"), env.get("LANGFUSE_SECRET_KEY")
        if pub and sec:
            env["LANGFUSE_BASIC_AUTH"] = base64.b64encode(
                f"{pub}:{sec}".encode()).decode()
    return env


def substitute(template: str, env: dict, missing: set[str]) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        value = env.get(name)
        if not value:
            missing.add(name)
            return ""
        return value
    return PLACEHOLDER.sub(repl, template)


def secret_values(cfg: dict, env: dict) -> list[str]:
    """All resolved secret values referenced by this platform's config."""
    names = set()
    for tpl in [cfg.get("endpoint", "")] + list(cfg.get("headers", {}).values()):
        names.update(PLACEHOLDER.findall(tpl))
    return [env[n] for n in names if env.get(n)]


def mask(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "<masked>")
    return text


def send_platform(platform: str, cfg: dict, chunks: list[dict],
                  out_dir: str, args) -> dict:
    started = time.monotonic()
    result = {
        "endpoint_template": cfg.get("endpoint"),
        "header_names": sorted(cfg.get("headers", {})),
        "chunks_total": len(chunks),
        "chunks_sent": 0,
        "spans_sent": 0,
        "bytes_sent": 0,
        "attempts": 0,
        "retries": 0,
        "statuses": {},
        "ok": False,
        "error": None,
    }

    env = resolve_platform_env(platform, cfg)
    missing: set[str] = set()
    endpoint = substitute(cfg["endpoint"], env, missing)
    headers = {k: substitute(v, env, missing)
               for k, v in cfg.get("headers", {}).items()}
    if missing:
        result["error"] = ("missing env var(s): " + ", ".join(sorted(missing))
                           + f" (looked in {cfg.get('env_file')} and process env)")
        result["wall_seconds"] = round(time.monotonic() - started, 1)
        return result

    if args.dry_run:
        log(f"[{platform}] DRY RUN — endpoint template: {cfg['endpoint']}")
        for k, v in cfg.get("headers", {}).items():
            log(f"[{platform}] DRY RUN — header {k}: {v}")  # template, not resolved
        log(f"[{platform}] DRY RUN — all env vars resolve; would send "
            f"{len(chunks)} chunk(s), "
            f"{sum(c['spans'] for c in chunks)} spans, "
            f"{sum(c['bytes'] for c in chunks)} bytes, "
            f"pacing {cfg.get('min_interval_seconds', 0)}s between POSTs")
        result.update(ok=True, dry_run=True)
        result["wall_seconds"] = round(time.monotonic() - started, 1)
        return result

    secrets = secret_values(cfg, env)
    headers["Content-Type"] = "application/x-protobuf"
    session = requests.Session()
    min_interval = float(cfg.get("min_interval_seconds", 0))
    last_post = 0.0

    for i, chunk in enumerate(chunks, 1):
        payload = chunk["payload"]
        sent = False
        for attempt in range(args.max_attempts):
            wait = last_post + min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            result["attempts"] += 1
            if attempt:
                result["retries"] += 1
            try:
                r = session.post(endpoint, data=payload, headers=headers,
                                 timeout=60)
            except requests.RequestException as e:
                result["error"] = mask(
                    f"chunk {chunk['file']}: {type(e).__name__}: {e}", secrets)
                result["wall_seconds"] = round(time.monotonic() - started, 1)
                return result
            last_post = time.monotonic()
            key = str(r.status_code)
            result["statuses"][key] = result["statuses"].get(key, 0) + 1
            if r.status_code in (200, 202):
                sent = True
                break
            retryable = r.status_code == 429 or r.status_code >= 500
            if not retryable:
                result["error"] = mask(
                    f"chunk {chunk['file']}: HTTP {r.status_code}: "
                    f"{r.text[:200]}", secrets)
                result["wall_seconds"] = round(time.monotonic() - started, 1)
                return result
            ra = r.headers.get("Retry-After")
            try:
                backoff = min(float(ra), MAX_BACKOFF) if ra else min(
                    2.0 * 2 ** attempt, MAX_BACKOFF)
            except ValueError:
                backoff = min(2.0 * 2 ** attempt, MAX_BACKOFF)
            log(f"[{platform}] chunk {i}/{len(chunks)} HTTP {r.status_code}; "
                f"retrying in {backoff:.0f}s "
                f"(attempt {attempt + 1}/{args.max_attempts})")
            time.sleep(backoff)
        if not sent:
            result["error"] = (f"chunk {chunk['file']}: gave up after "
                               f"{args.max_attempts} attempts (429/5xx)")
            result["wall_seconds"] = round(time.monotonic() - started, 1)
            return result
        result["chunks_sent"] += 1
        result["spans_sent"] += chunk["spans"]
        result["bytes_sent"] += chunk["bytes"]
        if i % 10 == 0 or i == len(chunks):
            log(f"[{platform}] {i}/{len(chunks)} chunks sent "
                f"({result['spans_sent']} spans)")

    result["ok"] = result["chunks_sent"] == len(chunks)
    result["wall_seconds"] = round(time.monotonic() - started, 1)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Replay generated OTLP chunks byte-identically to each platform")
    ap.add_argument("--platforms", required=True,
                    help="comma-separated platform names from platforms.json")
    ap.add_argument("--manifest", default="./out/manifest.json")
    ap.add_argument("--config", default=os.path.join(HERE, "platforms.json"))
    ap.add_argument("--dry-run", action="store_true",
                    help="validate config and credentials, print the send plan "
                         "(templates and env var NAMES only — never secret "
                         "values), no network I/O")
    ap.add_argument("--max-attempts", type=int, default=5,
                    help="attempts per chunk for 429/5xx (other 4xx fail fast)")
    ap.add_argument("--report", default=None,
                    help="output path (default: send_report.json next to the manifest)")
    args = ap.parse_args()

    with open(args.config) as f:
        config = json.load(f)["platforms"]
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    unknown = [p for p in platforms if p not in config]
    if unknown:
        ap.error(f"unknown platform(s) {unknown}; platforms.json defines "
                 f"{sorted(config)}")

    with open(args.manifest) as f:
        manifest = json.load(f)
    if manifest.get("args", {}).get("format", "pb") != "pb":
        ap.error("manifest was generated with --format json; send.py replays "
                 "raw protobuf — regenerate with --format pb")
    out_dir = os.path.dirname(os.path.abspath(args.manifest))

    # Load every chunk once and verify integrity against the manifest so all
    # platforms are guaranteed to receive the exact generated bytes.
    chunks = []
    for c in manifest["chunks"]:
        path = os.path.join(out_dir, c["file"])
        payload = open(path, "rb").read()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != c["sha256"]:
            log(f"FATAL: {c['file']} sha256 mismatch vs manifest "
                f"(corpus modified after generation?) — refusing to send")
            return 1
        chunks.append({"file": c["file"], "spans": c["spans"],
                       "bytes": len(payload), "payload": payload})
    log(f"manifest: {manifest['total_spans']} spans in {len(chunks)} chunk(s), "
        f"sha256 verified" + (" [DRY RUN]" if args.dry_run else ""))

    report = {}
    all_ok = True
    for p in platforms:
        log(f"[{p}] sending ..." if not args.dry_run else f"[{p}] dry run ...")
        res = send_platform(p, config[p], chunks, out_dir, args)
        all_ok = all_ok and res["ok"]
        report[p] = res
        log(f"[{p}] {'OK' if res['ok'] else 'FAIL'}"
            + (f" — {res['error']}" if res.get("error") else ""))

    out_path = args.report or os.path.join(out_dir, "send_report.json")
    payload = {
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "manifest": os.path.abspath(args.manifest),
        "dry_run": args.dry_run,
        "total_spans": manifest["total_spans"],
        "total_chunks": len(chunks),
        "platforms": report,
        "pass": all_ok,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    log(f"report: {out_path}")
    print("SEND:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

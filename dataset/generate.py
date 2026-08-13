#!/usr/bin/env python3
"""Deterministic synthetic OTLP trace generator for the observability benchmark.

Emits opentelemetry.proto ExportTraceServiceRequest messages serialized to
chunked files (out/chunk_00001.pb, max ~1000 spans per chunk) plus
out/manifest.json with run metadata and per-chunk sha256 hashes.

Determinism contract: with identical CLI args (including --anchor) the .pb
output is byte-identical. All randomness flows through a single
random.Random(seed); no time.time()/uuid4 anywhere in the data path.

The ONE nondeterministic default: if --anchor is omitted, the anchor defaults
to midnight UTC of "today" (datetime.now). Callers who need reproducibility
MUST pass --anchor explicitly (ISO8601, e.g. 2026-08-12T00:00:00Z).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import (
    ResourceSpans,
    ScopeSpans,
    Span,
    Status,
)

NS = 1_000_000_000
MAX_SPANS_PER_CHUNK = 1000

# --- fixed vocabularies -----------------------------------------------------

MODELS = ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-5"]
MODEL_WEIGHTS = [0.65, 0.25, 0.10]

# USD per 1M tokens (input, output) — fixed synthetic price table.
PRICE_TABLE = {
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (15.00, 75.00),
}

TOOLS_SCENARIO_TOOLS = ["get_weather", "get_currency", "search_flights", "book_hotel"]
MCP_TOOLS = ["get_current_time", "convert_time"]

ERROR_TYPES = ["rate_limit_error", "timeout", "invalid_tool_args"]
ERROR_MESSAGES = {
    "rate_limit_error": "429 rate_limit_error: request rate exceeded, retry after backoff",
    "timeout": "deadline exceeded: upstream did not respond within the configured timeout",
    "invalid_tool_args": "invalid_tool_args: schema validation failed for tool arguments",
}

# Seeded word material for plausible questions/answers (not lorem ipsum).
Q_OPENERS = [
    "Can you tell me", "I need to know", "What is", "How do I find",
    "Could you check", "Please explain", "I'm trying to figure out",
    "Help me understand", "Quick question about", "What would you recommend for",
]
Q_TOPICS = [
    "the weather in Lisbon this weekend", "the exchange rate from USD to JPY",
    "the cheapest flight from Madrid to Berlin in October",
    "a good hotel near the conference center in Amsterdam",
    "the current time in Tokyo compared to San Francisco",
    "how timezone conversion works for a meeting across three offices",
    "whether I should book refundable fares for a business trip",
    "the typical rainfall in Singapore during monsoon season",
    "converting 250 euros into British pounds today",
    "which day next week has the best weather for hiking near Oslo",
    "the layover rules when connecting through Frankfurt",
    "how much a mid-range hotel costs per night in Copenhagen",
]
Q_TAILS = [
    "I have a trip coming up and want to plan ahead.",
    "My team is distributed so timing really matters.",
    "Budget is a concern, so cheaper options are preferred.",
    "Ideally with sources so I can double check.",
    "A short summary is fine, no need for a long answer.",
    "This is for a report I am putting together today.",
]
A_OPENERS = [
    "Sure — here's what I found.", "Good question.", "Here's a quick summary.",
    "Based on the latest data,", "I checked the relevant sources.",
    "Short answer first, details below.",
]
A_BODIES = [
    "The forecast shows mild temperatures with a small chance of rain late in the day.",
    "The mid-market rate is roughly stable this week, with minor intraday movement.",
    "The lowest fare I found departs early morning with one short layover.",
    "There are three well-reviewed hotels within walking distance of the venue.",
    "The time difference is significant, so scheduling before noon UTC works best.",
    "Conversion at today's rate gives a slightly better result than yesterday.",
    "Booking two to three weeks ahead usually gives the best balance of price and flexibility.",
    "Expect afternoon showers most days, so plan indoor activities as a backup.",
]
A_TAILS = [
    "Let me know if you want me to compare more options.",
    "I can set up an alert if the numbers change.",
    "Happy to break this down further if useful.",
    "Prices and rates can shift, so treat this as an estimate.",
    "Tell me your exact dates and I will refine the answer.",
]

RESOURCE_ATTR_SEED_KEY = "dataset.seed"

SCENARIOS = ["chat", "tools", "mcp"]
SCENARIO_WEIGHTS = [0.60, 0.25, 0.15]

# Diurnal weighting per UTC hour (heavier 9:00-19:00).
HOUR_WEIGHTS = [
    1, 1, 1, 1, 1, 1,        # 00-05
    2, 3, 5, 8, 9, 9,        # 06-11
    8, 9, 9, 9, 8, 8,        # 12-17
    7, 6, 4, 3, 2, 1,        # 18-23
]


# --- small helpers ----------------------------------------------------------

def kv_str(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def kv_int(key: str, value: int) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(int_value=value))


def kv_double(key: str, value: float) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(double_value=value))


def kv_bool(key: str, value: bool) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(bool_value=value))


def make_question(rng: random.Random) -> str:
    parts = [f"{rng.choice(Q_OPENERS)} {rng.choice(Q_TOPICS)}?"]
    if rng.random() < 0.7:
        parts.append(rng.choice(Q_TAILS))
    text = " ".join(parts)
    return text[:400]


def make_answer(rng: random.Random) -> str:
    parts = [rng.choice(A_OPENERS), rng.choice(A_BODIES)]
    if rng.random() < 0.6:
        parts.append(rng.choice(A_BODIES))
    if rng.random() < 0.7:
        parts.append(rng.choice(A_TAILS))
    text = " ".join(parts)
    return text[:400]


def lognormal_clamped(rng: random.Random, median: float, sigma: float,
                      lo: float, hi: float) -> float:
    value = rng.lognormvariate(math.log(median), sigma)
    return max(lo, min(hi, value))


class IdFactory:
    """Hex-unique 16/8 byte ids drawn from the seeded RNG."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.seen_traces: set[bytes] = set()
        self.seen_spans: set[bytes] = set()

    def trace_id(self) -> bytes:
        while True:
            tid = self.rng.randbytes(16)
            if tid not in self.seen_traces and any(tid):
                self.seen_traces.add(tid)
                return tid

    def span_id(self) -> bytes:
        while True:
            sid = self.rng.randbytes(8)
            if sid not in self.seen_spans and any(sid):
                self.seen_spans.add(sid)
                return sid


# --- span builders ----------------------------------------------------------

def build_llm_span(rng: random.Random, ids: IdFactory, trace_id: bytes,
                   parent_span_id: bytes, start_ns: int,
                   common_attrs: list[KeyValue]) -> tuple[Span, int]:
    model = rng.choices(MODELS, weights=MODEL_WEIGHTS, k=1)[0]
    input_tokens = int(lognormal_clamped(rng, 800, 0.9, 50, 20000))
    output_tokens = int(lognormal_clamped(rng, 150, 0.9, 10, 4000))
    price_in, price_out = PRICE_TABLE[model]
    cost = round(
        input_tokens * price_in / 1_000_000 + output_tokens * price_out / 1_000_000, 8
    )
    duration_ns = int(lognormal_clamped(rng, 2.2, 0.7, 0.8, 8.0) * NS)
    end_ns = start_ns + duration_ns

    question = make_question(rng)
    answer = make_answer(rng)

    attrs = [
        kv_str("gen_ai.operation.name", "chat"),
        kv_str("gen_ai.request.model", model),
        kv_str("gen_ai.response.model", model),
        kv_int("gen_ai.usage.input_tokens", input_tokens),
        kv_int("gen_ai.usage.output_tokens", output_tokens),
        kv_int("llm.token_count.prompt", input_tokens),
        kv_int("llm.token_count.completion", output_tokens),
        kv_str("openinference.span.kind", "LLM"),
        kv_str("input.value", question),
        kv_str("output.value", answer),
        kv_double("operation.cost", cost),
    ] + common_attrs

    status = Status(code=Status.STATUS_CODE_OK)
    if rng.random() < 0.03:
        err = rng.choice(ERROR_TYPES)
        attrs.append(kv_str("error.type", err))
        status = Status(code=Status.STATUS_CODE_ERROR, message=ERROR_MESSAGES[err])

    span = Span(
        trace_id=trace_id,
        span_id=ids.span_id(),
        parent_span_id=parent_span_id,
        name=f"chat {model}",
        kind=Span.SPAN_KIND_CLIENT,
        start_time_unix_nano=start_ns,
        end_time_unix_nano=end_ns,
        attributes=attrs,
        status=status,
    )
    return span, end_ns


def build_tool_span(rng: random.Random, ids: IdFactory, trace_id: bytes,
                    parent_span_id: bytes, start_ns: int, name: str,
                    tool_name: str | None, arguments: dict, result: str,
                    common_attrs: list[KeyValue]) -> tuple[Span, int]:
    duration_ns = int(lognormal_clamped(rng, 0.045, 0.9, 0.005, 0.300) * NS)
    end_ns = start_ns + duration_ns

    attrs = [
        kv_str("openinference.span.kind", "TOOL"),
        kv_str("gen_ai.tool.call.arguments", json.dumps(arguments, sort_keys=True)),
        kv_str("gen_ai.tool.call.result", result),
    ] + common_attrs
    if tool_name is not None:
        attrs.insert(1, kv_str("gen_ai.tool.name", tool_name))

    status = Status(code=Status.STATUS_CODE_OK)
    if rng.random() < 0.02:
        err = rng.choice(ERROR_TYPES)
        attrs.append(kv_str("error.type", err))
        status = Status(code=Status.STATUS_CODE_ERROR, message=ERROR_MESSAGES[err])

    span = Span(
        trace_id=trace_id,
        span_id=ids.span_id(),
        parent_span_id=parent_span_id,
        name=name,
        kind=Span.SPAN_KIND_INTERNAL,
        start_time_unix_nano=start_ns,
        end_time_unix_nano=end_ns,
        attributes=attrs,
        status=status,
    )
    return span, end_ns


TOOL_ARGS = {
    "get_weather": lambda rng: {"city": rng.choice(
        ["Lisbon", "Oslo", "Tokyo", "Berlin", "Singapore", "Copenhagen"])},
    "get_currency": lambda rng: {"from": rng.choice(["USD", "EUR", "GBP"]),
                                 "to": rng.choice(["JPY", "CHF", "SEK"]),
                                 "amount": rng.randrange(50, 5000, 10)},
    "search_flights": lambda rng: {"origin": rng.choice(["MAD", "LIS", "BCN"]),
                                   "destination": rng.choice(["BER", "AMS", "CPH"]),
                                   "pax": rng.randint(1, 3)},
    "book_hotel": lambda rng: {"city": rng.choice(["Amsterdam", "Berlin", "Madrid"]),
                               "nights": rng.randint(1, 7)},
    "get_current_time": lambda rng: {"timezone": rng.choice(
        ["Asia/Tokyo", "Europe/Madrid", "America/Los_Angeles", "UTC"])},
    "convert_time": lambda rng: {"time": f"{rng.randint(0, 23):02d}:00",
                                 "from_tz": "UTC",
                                 "to_tz": rng.choice(["Asia/Tokyo", "Europe/Madrid"])},
}

TOOL_RESULTS = {
    "get_weather": lambda rng: json.dumps(
        {"temp_c": rng.randint(-5, 35), "condition": rng.choice(
            ["clear", "cloudy", "rain", "windy"])}, sort_keys=True),
    "get_currency": lambda rng: json.dumps(
        {"rate": round(rng.uniform(0.5, 160.0), 4)}, sort_keys=True),
    "search_flights": lambda rng: json.dumps(
        {"results": rng.randint(3, 25), "cheapest_eur": rng.randint(39, 480)},
        sort_keys=True),
    "book_hotel": lambda rng: json.dumps(
        {"confirmation": f"HB-{rng.randint(100000, 999999)}"}, sort_keys=True),
    "get_current_time": lambda rng: json.dumps(
        {"time": f"2026-08-{rng.randint(1, 12):02d}T{rng.randint(0, 23):02d}:"
                 f"{rng.randint(0, 59):02d}:00"}, sort_keys=True),
    "convert_time": lambda rng: json.dumps(
        {"converted": f"{rng.randint(0, 23):02d}:00"}, sort_keys=True),
}


def build_trace(rng: random.Random, ids: IdFactory, scenario: str,
                trace_start_ns: int, session_id: str, user_id: str) -> list[Span]:
    """Build one full trace; returns spans with root first."""
    trace_id = ids.trace_id()
    root_span_id = ids.span_id()
    common_attrs = [
        kv_str("session.id", session_id),
        kv_str("user.id", user_id),
    ]

    children: list[Span] = []
    cursor = trace_start_ns + int(rng.uniform(0.002, 0.030) * NS)  # root overhead in

    def gap() -> int:
        return int(rng.uniform(0.001, 0.020) * NS)

    if scenario == "chat":
        root_name, root_kind_attr = "synthetic-chat-hello", "CHAIN"
        span, cursor = build_llm_span(rng, ids, trace_id, root_span_id, cursor,
                                      common_attrs)
        children.append(span)
    elif scenario == "tools":
        root_name, root_kind_attr = "synthetic-travel-assistant", "AGENT"
        n_llm = rng.randint(2, 4)
        n_tool = rng.randint(2, 4)
        steps = ["llm"] * n_llm + ["tool"] * n_tool
        rng.shuffle(steps)
        # Always lead with an LLM turn so the agent flow looks natural.
        if steps[0] != "llm":
            first_llm = steps.index("llm")
            steps[0], steps[first_llm] = steps[first_llm], steps[0]
        for step in steps:
            cursor += gap()
            if step == "llm":
                span, cursor = build_llm_span(rng, ids, trace_id, root_span_id,
                                              cursor, common_attrs)
            else:
                tool = rng.choice(TOOLS_SCENARIO_TOOLS)
                span, cursor = build_tool_span(
                    rng, ids, trace_id, root_span_id, cursor,
                    f"execute_tool {tool}", tool,
                    TOOL_ARGS[tool](rng), TOOL_RESULTS[tool](rng), common_attrs)
            children.append(span)
    elif scenario == "mcp":
        root_name, root_kind_attr = "synthetic-time-assistant-mcp", "AGENT"
        span, cursor = build_tool_span(
            rng, ids, trace_id, root_span_id, cursor, "tools/list", "tools/list",
            {"method": "tools/list"},
            json.dumps({"tools": MCP_TOOLS}, sort_keys=True), common_attrs)
        children.append(span)
        n_llm = rng.randint(2, 3)
        n_call = rng.randint(2, 3)
        steps = ["llm"] * n_llm + ["call"] * n_call
        rng.shuffle(steps)
        if steps[0] != "llm":
            first_llm = steps.index("llm")
            steps[0], steps[first_llm] = steps[first_llm], steps[0]
        for step in steps:
            cursor += gap()
            if step == "llm":
                span, cursor = build_llm_span(rng, ids, trace_id, root_span_id,
                                              cursor, common_attrs)
            else:
                tool = rng.choice(MCP_TOOLS)
                span, cursor = build_tool_span(
                    rng, ids, trace_id, root_span_id, cursor,
                    f"tools/call {tool}", tool,
                    TOOL_ARGS[tool](rng), TOOL_RESULTS[tool](rng), common_attrs)
            children.append(span)
    else:  # pragma: no cover
        raise ValueError(f"unknown scenario {scenario}")

    root_end_ns = cursor + int(rng.uniform(0.002, 0.040) * NS)  # root overhead out
    root_attrs = [
        kv_str("openinference.span.kind", root_kind_attr),
        kv_str("input.value", make_question(rng)),
        kv_str("output.value", make_answer(rng)),
    ] + common_attrs
    has_error_child = any(
        c.status.code == Status.STATUS_CODE_ERROR for c in children)
    root = Span(
        trace_id=trace_id,
        span_id=root_span_id,
        name=root_name,
        kind=Span.SPAN_KIND_INTERNAL,
        start_time_unix_nano=trace_start_ns,
        end_time_unix_nano=root_end_ns,
        attributes=root_attrs,
        status=Status(code=Status.STATUS_CODE_ERROR,
                      message="child operation failed")
        if has_error_child else Status(code=Status.STATUS_CODE_OK),
    )
    return [root] + children


# --- top-level generation ---------------------------------------------------

# Worst-case trace duration is well under a minute (a tools trace tops out
# around ~35 s: four 8 s LLM spans plus gaps/overheads), so clamping trace
# starts to anchor - MAX_TRACE_DURATION_NS guarantees no span's end time
# spills past the anchor.
MAX_TRACE_DURATION_NS = 60 * NS


def pick_trace_start(rng: random.Random, window_start_ns: int, days: float) -> int:
    if float(days).is_integer():
        # integer-day path unchanged: keeps byte-identical output for the
        # corpora already generated with --days 7 / --days 1
        day = rng.randrange(int(days))
        hour = rng.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]
    else:
        # fractional window (e.g. --days 0.5 for platforms with ±24h ingest
        # limits): draw an hour offset across the whole window, diurnal
        # weights tiled and truncated to the hours that actually fit
        total_hours = max(1, int(days * 24))
        weights = (HOUR_WEIGHTS * (total_hours // 24 + 1))[:total_hours]
        offset = rng.choices(range(total_hours), weights=weights, k=1)[0]
        day, hour = offset // 24, offset % 24
    second_of_hour = rng.randrange(3600)
    micro = rng.randrange(1_000_000)
    start = (window_start_ns
             + day * 86400 * NS
             + hour * 3600 * NS
             + second_of_hour * NS
             + micro * 1000)
    latest = window_start_ns + int(days * 86400) * NS - MAX_TRACE_DURATION_NS
    return min(start, latest)


def build_resource(seed: int) -> Resource:
    return Resource(attributes=[
        kv_str("service.name", "dataset-pilot"),
        kv_str("service.namespace", "synthetic"),
        kv_bool("dataset.synthetic", True),
        kv_int(RESOURCE_ATTR_SEED_KEY, seed),
        kv_str("openinference.project.name", "otel-dataset-pilot"),
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spans", type=int, default=10000,
                        help="total span budget (default 10000). This is a "
                             "SOFT budget with a hard cap: the last trace "
                             "that would exceed it is dropped, so runs "
                             "typically produce slightly fewer spans than "
                             "requested — always read manifest.total_spans, "
                             "never assume the requested value")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--days", type=float, default=7,
                        help="spread window in days (default 7)")
    parser.add_argument("--out", default="./out")
    parser.add_argument("--format", choices=["pb", "json"], default="pb",
                        help="json is a MessageToDict debug dump of the same messages")
    parser.add_argument(
        "--anchor", default=None,
        help="ISO8601 end of the time window, e.g. 2026-08-12T00:00:00Z. "
             "Pass explicitly for reproducible output; the default (midnight "
             "UTC today) is the ONE nondeterministic default.")
    args = parser.parse_args()

    if args.anchor:
        anchor = datetime.fromisoformat(args.anchor.replace("Z", "+00:00"))
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
    else:
        # Documented nondeterministic default: midnight UTC of today.
        anchor = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
    anchor_ns = int(anchor.timestamp()) * NS
    window_start_ns = anchor_ns - int(args.days * 86400) * NS

    rng = random.Random(args.seed)
    ids = IdFactory(rng)
    resource = build_resource(args.seed)

    traces: list[list[Span]] = []
    scenario_counts = {s: 0 for s in SCENARIOS}
    total_spans = 0

    # Session/user state: sessions hold 1-8 traces; users drawn zipf-ish.
    user_pool = [f"user-{i:04d}" for i in range(1, 201)]
    user_weights = [1.0 / (i ** 1.1) for i in range(1, 201)]
    session_counter = 0
    session_remaining = 0
    session_id = ""
    session_user = ""

    while total_spans < args.spans:
        if session_remaining == 0:
            session_counter += 1
            session_remaining = rng.randint(1, 8)
            session_id = f"session-{args.seed}-{session_counter:06d}"
            session_user = rng.choices(user_pool, weights=user_weights, k=1)[0]
        scenario = rng.choices(SCENARIOS, weights=SCENARIO_WEIGHTS, k=1)[0]
        start_ns = pick_trace_start(rng, window_start_ns, args.days)
        spans = build_trace(rng, ids, scenario, start_ns, session_id, session_user)
        if total_spans + len(spans) > args.spans and total_spans > 0:
            break  # keep the budget as a hard cap once at least one trace exists
        traces.append(spans)
        scenario_counts[scenario] += 1
        total_spans += len(spans)
        session_remaining -= 1

    # Chunking: keep traces whole, flush at ~MAX_SPANS_PER_CHUNK spans.
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[dict] = []
    buffer: list[Span] = []
    chunk_index = 0

    def flush() -> None:
        nonlocal buffer, chunk_index
        if not buffer:
            return
        chunk_index += 1
        request = ExportTraceServiceRequest(resource_spans=[
            ResourceSpans(
                resource=resource,
                scope_spans=[ScopeSpans(spans=buffer)],
            )
        ])
        payload = request.SerializeToString(deterministic=True)
        if args.format == "pb":
            path = out_dir / f"chunk_{chunk_index:05d}.pb"
            path.write_bytes(payload)
        else:
            path = out_dir / f"chunk_{chunk_index:05d}.json"
            path.write_text(json.dumps(MessageToDict(request), indent=2,
                                       sort_keys=True) + "\n")
        chunks.append({
            "file": path.name,
            "spans": len(buffer),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
        buffer = []

    for spans in traces:
        if buffer and len(buffer) + len(spans) > MAX_SPANS_PER_CHUNK:
            flush()
        buffer.extend(spans)
    flush()

    all_spans = [s for t in traces for s in t]
    max_end_ns = max(s.end_time_unix_nano for s in all_spans)
    assert max_end_ns <= anchor_ns, (
        "generator invariant violated: a span ends after --anchor "
        f"({max_end_ns} > {anchor_ns})")
    time_range = {
        "start": datetime.fromtimestamp(
            min(s.start_time_unix_nano for s in all_spans) / NS,
            tz=timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(
            max(s.end_time_unix_nano for s in all_spans) / NS,
            tz=timezone.utc).isoformat(),
    }
    manifest = {
        "generator": "dataset/generate.py",
        "args": {
            "spans": args.spans,
            "seed": args.seed,
            "days": args.days,
            "format": args.format,
            "anchor": anchor.isoformat(),
        },
        "seed": args.seed,
        "total_spans": total_spans,
        "total_traces": len(traces),
        "scenario_counts": scenario_counts,
        "sessions": session_counter,
        "time_range": time_range,
        # Single source of truth for names that also appear in the OTLP
        # resource attributes above (build_resource) — consumers such as
        # verify_parity.py and send.py must read them from here.
        "service_name": "dataset-pilot",
        "projects": {
            "logfire": "dataset-pilot",  # same project; isolated by service_name
            "braintrust": "otel-dataset-pilot",
            "langsmith": "otel-dataset-pilot",
            # Langfuse project is fixed by the API key pair; isolation is
            # the manifest time window, so no name to record here.
            "langfuse": None,
            "phoenix": "otel-dataset-pilot",
        },
        "chunks": chunks,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(chunks)} chunk(s), {total_spans} spans, "
          f"{len(traces)} traces -> {out_dir}")


if __name__ == "__main__":
    main()

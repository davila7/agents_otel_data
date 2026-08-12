"""Re-score the benchmark with continuous scoring for the measured metrics.

Addresses the banded-scoring critique: latency and freshness were scored in
bands ("300-600 ms = 8 pts", "< 10 s = 10 pts"), so real quantitative gaps
(Braintrust's 0.4 s ingest vs Logfire's 5.0 s) collapsed into ties and the
ranking was decided entirely by categorical criteria.

Here latency and freshness are re-scored on a log scale normalized within the
cohort: best observed value = 10, worst = 0, log-linear in between. Log scale
because both metrics span an order of magnitude (335 ms - 1293 ms, 0.4 s -
46.5 s) and perception of "how much worse" is multiplicative, not additive.
The other 7 criteria keep the judge-panel averages from final_scores.json.

Reads  results/*.json + results/final_scores.json
Writes results/final_scores_continuous.json

With --cohort aug2026 it instead scores the August 2026 same-session re-run
(triggered by Langfuse shipping observations v2 + real-time ingestion):
measured values come from results/*_aug2026.json / langfuse_v2.json, and
Langfuse's categorical criteria that verifiably changed on the v2 API are
overridden (pagination: cursor second-page fetched -> 10; otel-fidelity:
verbatim gen_ai.* keys + W3C 32-hex trace ids -> 9; query-flex: advanced
JSON filter honored, still no free SQL -> 8; export unchanged at 5). These
overrides are mechanical applications of the rubric anchors to captured
responses, not a fresh judge panel — flagged as provisional in RESULTS.md.
Writes results/final_scores_continuous_aug2026.json
"""

import json
import math
import sys
from pathlib import Path

RESULTS = Path(__file__).parent / "results"
CONTINUOUS = ["latency", "freshness"]

COHORTS = {
    "jul2026": {
        "files": {p: f"{p}.json" for p in ["logfire", "braintrust", "langsmith", "langfuse"]},
        "overrides": {},
        "out": "final_scores_continuous.json",
    },
    "aug2026": {
        "files": {
            "logfire": "logfire_aug2026.json",
            "braintrust": "braintrust_aug2026.json",
            "langsmith": "langsmith_aug2026.json",
            "langfuse": "langfuse_v2.json",
        },
        # rubric-anchor re-scores for langfuse capabilities that changed on v2,
        # each traceable to a captured response in langfuse_v2.json
        "overrides": {"langfuse": {"pagination": 10, "otel-fidelity": 9, "query-flex": 8}},
        "out": "final_scores_continuous_aug2026.json",
    },
    # August 12, 2026: Phoenix joins the benchmark, so all five evals were
    # re-run back-to-back in one session (rubric rule 2). The four incumbents
    # keep their judge-panel categorical averages (plus the langfuse v2
    # overrides); Phoenix's categorical criteria come from its own 3-judge
    # panel in phoenix_judges.json, scored against evidence in phoenix.json.
    "aug12_2026": {
        "files": {
            "logfire": "logfire_aug12_2026.json",
            "braintrust": "braintrust_aug12_2026.json",
            "langsmith": "langsmith_aug12_2026.json",
            "langfuse": "langfuse_aug12_2026.json",
            "phoenix": "phoenix.json",
        },
        "overrides": {"langfuse": {"pagination": 10, "otel-fidelity": 9, "query-flex": 8}},
        "extra_judges": {"phoenix": "phoenix_judges.json"},
        "out": "final_scores_continuous_aug12_2026.json",
    },
}

cohort_name = "jul2026"
if len(sys.argv) == 3 and sys.argv[1] == "--cohort":
    cohort_name = sys.argv[2]
elif len(sys.argv) != 1:
    sys.exit("usage: rescore_continuous.py [--cohort jul2026|aug2026|aug12_2026]")
cohort = COHORTS[cohort_name]

final = json.loads((RESULTS / "final_scores.json").read_text())
weights = final["weights"]

# judge-panel categorical averages per platform: incumbents from the frozen
# final_scores.json, additions from their own judge files (never merged back)
judge_averages = {n: d["criterion_averages"] for n, d in final["platforms"].items()}
banded_totals = {n: d["total_score"] for n, d in final["platforms"].items()}
for name, jf in cohort.get("extra_judges", {}).items():
    j = json.loads((RESULTS / jf).read_text())
    judge_averages[name] = j["criterion_averages"]
    banded_totals[name] = None  # not part of the frozen banded record

raw = {}
for name in judge_averages:
    d = json.loads((RESULTS / cohort["files"][name]).read_text())
    m = d.get("metrics", d)  # logfire nests under "metrics", others are flat
    raw[name] = {
        "latency": m["retrieval_latency_ms"],
        "freshness": m["time_to_queryable_s"],
    }


def log_scores(values):
    """Map values (lower = better) to 0-10, log-linear between best and worst."""
    best, worst = min(values.values()), max(values.values())
    if best == worst:
        return {k: 10.0 for k in values}
    span = math.log(worst / best)
    return {k: 10 * math.log(worst / v) / span for k, v in values.items()}


cont = {c: log_scores({p: raw[p][c] for p in raw}) for c in CONTINUOUS}

out = {
    "method": {
        "cohort": cohort_name,
        "measurement_files": cohort["files"],
        "continuous_criteria": CONTINUOUS,
        "scoring": "10 * log(worst/value) / log(worst/best); other criteria keep judge averages",
        "categorical_overrides": cohort["overrides"],
        "raw_inputs": raw,
    },
    "weights": weights,
    "platforms": {},
}

for name, averages in judge_averages.items():
    avgs = dict(averages)
    avgs.update(cohort["overrides"].get(name, {}))
    for c in CONTINUOUS:
        avgs[c] = cont[c][name]
    contrib = {c: weights[c] * avgs[c] / 10 for c in avgs}
    out["platforms"][name] = {
        "criterion_scores": {c: round(v, 2) for c, v in avgs.items()},
        "weighted_contributions": {c: round(v, 2) for c, v in contrib.items()},
        "total_score": round(sum(contrib.values()), 2),  # round only at display
        "banded_total_score": banded_totals[name],
    }

ranking = sorted(out["platforms"].items(), key=lambda kv: -kv[1]["total_score"])
out["ranking"] = [
    {"rank": i + 1, "platform": p, "total_score": d["total_score"]}
    for i, (p, d) in enumerate(ranking)
]
out["winner"] = ranking[0][0]

(RESULTS / cohort["out"]).write_text(json.dumps(out, indent=2) + "\n")

print(f"{'platform':<12} {'banded':>7} {'continuous':>11}  latency(ms)  freshness(s)")
for p, d in ranking:
    banded = d["banded_total_score"] if d["banded_total_score"] is not None else "—"
    print(
        f"{p:<12} {banded:>7} {d['total_score']:>11}"
        f"  {raw[p]['latency']:>10}  {raw[p]['freshness']:>11}"
    )

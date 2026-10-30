#!/usr/bin/env python3
"""
telemetry.py — TPM aggregation and cost analysis from JSONL telemetry logs.

Reads telemetry JSONL files produced by bedrock_client.py, buckets by minute,
and computes tokens-per-minute metrics, cost estimates, and violation reports.

Usage (<dir> is a run output directory or its telemetry/ folder):
    python3 telemetry.py aggregate <dir> [--threshold 1000000]
    python3 telemetry.py cost <dir>
    python3 telemetry.py summary <dir>
The CLI exits 1 with an error when <dir> holds no telemetry records (instead of reporting $0).
"""

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional


# Pricing per 1M tokens (on-demand list price, us-east-1). Cache write = 1.25x input, cache read =
# 0.1x input. Matched by substring of model_id; PRICING (Sonnet 4.6) is the default.
PRICING = {
    "input_per_1m": 3.00,
    "output_per_1m": 15.00,
    "cache_write_per_1m": 3.75,
    "cache_read_per_1m": 0.30,
}
MODEL_PRICING = {
    "sonnet": PRICING,
    "haiku": {"input_per_1m": 1.00, "output_per_1m": 5.00,
              "cache_write_per_1m": 1.25, "cache_read_per_1m": 0.10},
    "opus": {"input_per_1m": 5.00, "output_per_1m": 25.00,
             "cache_write_per_1m": 6.25, "cache_read_per_1m": 0.50},
}

# A labelled caller (reduce/format/synthesis) that uses bedrock_client.call_llm logs the same
# call twice: once unlabelled from call_llm (still="" phase="" -> `_.jsonl`) and once with its
# still/phase. An unlabelled record is a duplicate when a labelled record has the same token
# counts and model within this many seconds.
DUP_WINDOW_S = 5.0


def pricing_for(model_id: str) -> dict:
    m = (model_id or "").lower()
    for key, price in MODEL_PRICING.items():
        if key in m:
            return price
    return PRICING


def _ts(rec: dict) -> float:
    try:
        return datetime.strptime(rec.get("timestamp", "")[:23], "%Y-%m-%dT%H:%M:%S.%f").timestamp()
    except ValueError:
        return 0.0


def _key(rec: dict) -> tuple:
    return (rec.get("input_tokens", 0), rec.get("output_tokens", 0), rec.get("model_id", ""))


def dedupe_records(records: list[dict]) -> list[dict]:
    """Drop unlabelled records that duplicate a labelled record of the same call (one-to-one).
    (call_llm logs without cache tokens, so cache fields are not part of the match key.)"""
    labelled: dict[tuple, list[float]] = defaultdict(list)
    for r in records:
        if r.get("still") or r.get("phase"):
            labelled[_key(r)].append(_ts(r))
    out = []
    for r in records:
        if not (r.get("still") or r.get("phase")):
            cands = labelled.get(_key(r))
            if cands:
                t = _ts(r)
                j = min(range(len(cands)), key=lambda k: abs(cands[k] - t))
                if abs(cands[j] - t) <= DUP_WINDOW_S:
                    cands.pop(j)          # consume: each labelled record absorbs one duplicate
                    continue
        out.append(r)
    return out


def record_cost(rec: dict) -> float:
    """USD cost of one call: input + output + cache write + cache read at the model's price."""
    p = pricing_for(rec.get("model_id", ""))
    return ((rec.get("input_tokens", 0) or 0) * p["input_per_1m"]
            + (rec.get("output_tokens", 0) or 0) * p["output_per_1m"]
            + (rec.get("cache_write_tokens", 0) or 0) * p["cache_write_per_1m"]
            + (rec.get("cache_read_tokens", 0) or 0) * p["cache_read_per_1m"]) / 1_000_000


def load_records(telemetry_dir: str, dedupe: bool = True) -> list[dict]:
    """Load all JSONL records from a telemetry directory (double-logged calls removed unless
    dedupe=False)."""
    records = []
    tel_path = Path(telemetry_dir)
    if not tel_path.exists():
        return records
    for jsonl_file in tel_path.glob("*.jsonl"):
        for line in jsonl_file.read_text(encoding="utf-8").strip().split("\n"):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return dedupe_records(records) if dedupe else records


def aggregate_tpm(telemetry_dir: str, threshold_tpm: int = 1_000_000) -> dict:
    """Read telemetry JSONL and compute TPM metrics, split by input/output.
    
    Returns:
        {
            "timeseries": [{"minute": str, "input_tokens": int, "output_tokens": int, "over_threshold": bool}],
            "peak_input_tpm": int,
            "peak_output_tpm": int,
            "total_input_tokens": int,
            "total_output_tokens": int,
            "total_calls": int,
            "input_violations": int,
            "output_violations": int,
            "by_still": {still: {"input": int, "output": int, "calls": int}},
            "by_phase": {phase: {"input": int, "output": int, "calls": int}},
        }
    """
    records = load_records(telemetry_dir)
    if not records:
        return {"timeseries": [], "peak_input_tpm": 0, "peak_output_tpm": 0,
                "total_input_tokens": 0, "total_output_tokens": 0,
                "total_calls": 0, "input_violations": 0, "output_violations": 0,
                "by_still": {}, "by_phase": {}}

    # Bucket by minute
    input_buckets: dict[str, int] = defaultdict(int)
    output_buckets: dict[str, int] = defaultdict(int)
    by_still: dict[str, dict] = defaultdict(lambda: {"input": 0, "output": 0, "calls": 0})
    by_phase: dict[str, dict] = defaultdict(lambda: {"input": 0, "output": 0, "calls": 0})
    total_input = 0
    total_output = 0
    total_calls = 0

    for rec in records:
        ts = rec.get("timestamp", "")
        minute_key = ts[:16] if len(ts) >= 16 else ts
        input_t = rec.get("input_tokens", 0)
        output_t = rec.get("output_tokens", 0)

        input_buckets[minute_key] += input_t
        output_buckets[minute_key] += output_t
        total_input += input_t
        total_output += output_t
        total_calls += 1

        still = rec.get("still", "unknown")
        phase = rec.get("phase", "unknown")
        by_still[still]["input"] += input_t
        by_still[still]["output"] += output_t
        by_still[still]["calls"] += 1
        by_phase[phase]["input"] += input_t
        by_phase[phase]["output"] += output_t
        by_phase[phase]["calls"] += 1

    # Build timeseries
    all_minutes = sorted(set(list(input_buckets.keys()) + list(output_buckets.keys())))
    timeseries = []
    input_violations = 0
    output_violations = 0
    peak_input_tpm = 0
    peak_output_tpm = 0

    for minute_key in all_minutes:
        inp = input_buckets.get(minute_key, 0)
        out = output_buckets.get(minute_key, 0)
        over = inp > threshold_tpm or out > threshold_tpm
        timeseries.append({
            "minute": minute_key,
            "input_tokens": inp,
            "output_tokens": out,
            "over_threshold": over,
        })
        peak_input_tpm = max(peak_input_tpm, inp)
        peak_output_tpm = max(peak_output_tpm, out)
        if inp > threshold_tpm:
            input_violations += 1
        if out > threshold_tpm:
            output_violations += 1

    return {
        "timeseries": timeseries,
        "peak_input_tpm": peak_input_tpm,
        "peak_output_tpm": peak_output_tpm,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_calls": total_calls,
        "input_violations": input_violations,
        "output_violations": output_violations,
        "by_still": dict(by_still),
        "by_phase": dict(by_phase),
    }


def compute_cost(telemetry_dir: str) -> dict:
    """Compute estimated cost from telemetry records: double-logged calls removed, cache
    write/read tokens priced, each record priced at its own model's rate (Haiku != Sonnet)."""
    records = load_records(telemetry_dir)
    fields = ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")
    tot = {k: sum(r.get(k, 0) or 0 for r in records) for k in fields}
    cost = {k: 0.0 for k in fields}
    by_phase: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    for r in records:
        p = pricing_for(r.get("model_id", ""))
        rate = {"input_tokens": p["input_per_1m"], "output_tokens": p["output_per_1m"],
                "cache_write_tokens": p["cache_write_per_1m"],
                "cache_read_tokens": p["cache_read_per_1m"]}
        for k in fields:
            cost[k] += (r.get(k, 0) or 0) * rate[k] / 1_000_000
        ph = f"{r.get('still') or '-'}/{r.get('phase') or '-'}"
        by_phase[ph]["calls"] += 1
        by_phase[ph]["cost_usd"] += record_cost(r)

    return {
        "total_input_tokens": tot["input_tokens"],
        "total_output_tokens": tot["output_tokens"],
        "total_cache_write_tokens": tot["cache_write_tokens"],
        "total_cache_read_tokens": tot["cache_read_tokens"],
        "input_cost_usd": round(cost["input_tokens"], 4),
        "output_cost_usd": round(cost["output_tokens"], 4),
        "cache_write_cost_usd": round(cost["cache_write_tokens"], 4),
        "cache_read_cost_usd": round(cost["cache_read_tokens"], 4),
        "total_cost_usd": round(sum(cost.values()), 4),
        "total_calls": len(records),
        "by_phase": {k: {"calls": v["calls"], "cost_usd": round(v["cost_usd"], 4)}
                     for k, v in sorted(by_phase.items())},
    }


def retry_analysis(telemetry_dir: str) -> dict:
    """Analyze format retry costs from telemetry.
    
    Parses format-phase records to compute:
    - First-attempt success rate
    - Token waste on failed attempts
    - Effective cost multiplier vs theoretical minimum
    
    Returns:
        {
            "total_format_calls": int,
            "first_attempt_calls": int,
            "first_attempt_passes": int,
            "first_attempt_fails": int,
            "first_attempt_success_rate": float,
            "retry_calls": int,
            "tokens_on_passes": {"input": int, "output": int},
            "tokens_on_fails": {"input": int, "output": int},
            "tokens_total": {"input": int, "output": int},
            "theoretical_minimum_input": int,
            "effective_cost_multiplier": float,
            "waste_cost_usd": float,
        }
    """
    records = load_records(telemetry_dir)
    
    # Filter to format phase records
    format_records = [r for r in records if r.get("phase", "").startswith("format")]
    
    if not format_records:
        return {"error": "No format-phase telemetry found"}
    
    first_attempt = [r for r in format_records if r.get("phase") == "format_r0"]
    retry_records = [r for r in format_records if r.get("phase") != "format_r0"]
    
    # Parse pass/fail from job_id field: "pass_25in_25out" or "fail_25in_20out"
    first_passes = [r for r in first_attempt if r.get("job_id", "").startswith("pass")]
    first_fails = [r for r in first_attempt if r.get("job_id", "").startswith("fail")]
    
    tokens_on_passes = {
        "input": sum(r.get("input_tokens", 0) for r in format_records if r.get("job_id", "").startswith("pass")),
        "output": sum(r.get("output_tokens", 0) for r in format_records if r.get("job_id", "").startswith("pass")),
    }
    tokens_on_fails = {
        "input": sum(r.get("input_tokens", 0) for r in format_records if r.get("job_id", "").startswith("fail")),
        "output": sum(r.get("output_tokens", 0) for r in format_records if r.get("job_id", "").startswith("fail")),
    }
    tokens_total = {
        "input": tokens_on_passes["input"] + tokens_on_fails["input"],
        "output": tokens_on_passes["output"] + tokens_on_fails["output"],
    }
    
    # Theoretical minimum: only the successful calls' tokens
    theoretical_min_input = tokens_on_passes["input"]
    
    # Cost multiplier
    multiplier = tokens_total["input"] / theoretical_min_input if theoretical_min_input > 0 else 1.0
    
    # Waste cost (tokens spent on failed attempts)
    waste_input_cost = (tokens_on_fails["input"] / 1_000_000) * PRICING["input_per_1m"]
    waste_output_cost = (tokens_on_fails["output"] / 1_000_000) * PRICING["output_per_1m"]
    
    first_attempt_rate = len(first_passes) / len(first_attempt) if first_attempt else 0
    
    # Per-split-round breakdown
    by_round = defaultdict(lambda: {"calls": 0, "passes": 0, "fails": 0, "input_tokens": 0, "output_tokens": 0})
    for r in format_records:
        phase = r.get("phase", "format_r0")
        round_key = phase.replace("format_", "")
        by_round[round_key]["calls"] += 1
        by_round[round_key]["input_tokens"] += r.get("input_tokens", 0)
        by_round[round_key]["output_tokens"] += r.get("output_tokens", 0)
        if r.get("job_id", "").startswith("pass"):
            by_round[round_key]["passes"] += 1
        else:
            by_round[round_key]["fails"] += 1
    
    return {
        "total_format_calls": len(format_records),
        "first_attempt_calls": len(first_attempt),
        "first_attempt_passes": len(first_passes),
        "first_attempt_fails": len(first_fails),
        "first_attempt_success_rate": round(first_attempt_rate, 3),
        "retry_calls": len(retry_records),
        "tokens_on_passes": tokens_on_passes,
        "tokens_on_fails": tokens_on_fails,
        "tokens_total": tokens_total,
        "theoretical_minimum_input": theoretical_min_input,
        "effective_cost_multiplier": round(multiplier, 2),
        "waste_cost_usd": round(waste_input_cost + waste_output_cost, 2),
        "by_round": dict(by_round),
    }


def summary(telemetry_dir: str) -> dict:
    """Quick summary: calls, tokens, cost, errors."""
    records = load_records(telemetry_dir)
    statuses = defaultdict(int)
    for r in records:
        statuses[r.get("status", "unknown")] += 1
    
    cost = compute_cost(telemetry_dir)
    return {
        **cost,
        "status_breakdown": dict(statuses),
    }


def resolve_telemetry_dir(path: str) -> Path:
    """Accept either a run output directory (holding telemetry/) or the telemetry dir itself."""
    p = Path(path)
    if (p / "telemetry").is_dir():
        return p / "telemetry"
    return p


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print("Usage (<dir> = a run output directory or its telemetry/ folder):")
        print("  python3 telemetry.py aggregate <dir> [--threshold N]")
        print("  python3 telemetry.py cost <dir>")
        print("  python3 telemetry.py summary <dir>")
        print("  python3 telemetry.py retries <dir>")
        return 1

    cmd = argv[0]
    if cmd not in ("aggregate", "cost", "retries", "summary"):
        print(f"Unknown command: {cmd}")
        return 1
    given = argv[1]
    tel = resolve_telemetry_dir(given)
    if not tel.is_dir():
        print(f"telemetry.py: ERROR {given} is not a directory (expected a run output directory "
              f"or its telemetry/ folder)", file=sys.stderr)
        return 1
    if not load_records(str(tel), dedupe=False):
        n_files = len(list(tel.glob("*.jsonl")))
        print(f"telemetry.py: ERROR no telemetry records found in {tel} ({n_files} .jsonl file(s)). "
              f"Pass a run output directory (e.g. output/<run>) or its telemetry/ folder.",
              file=sys.stderr)
        return 1
    tel_dir = str(tel)

    if cmd == "aggregate":
        threshold = 1_000_000
        if "--threshold" in argv:
            idx = argv.index("--threshold")
            threshold = int(argv[idx + 1])
        result = aggregate_tpm(tel_dir, threshold)
        print(json.dumps(result, indent=2))
    elif cmd == "cost":
        print(json.dumps(compute_cost(tel_dir), indent=2))
    elif cmd == "retries":
        print(json.dumps(retry_analysis(tel_dir), indent=2))
    else:
        print(json.dumps(summary(tel_dir), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

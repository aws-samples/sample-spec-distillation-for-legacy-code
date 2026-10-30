#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test double for a capped, plannable LLM evaluator (the shape of score.py / benefits/*).

--dry-run       write result.json {"dry_run": true, "plan": {"est_cost_usd": --plan-usd}}, spend nothing
--max-usd X     the cap the suite passes down; recorded in result.json, and exit 3 if --usd > X
--usd Y         what a real run spends (written to the stage's token log)
--status S      measurement status to report (measured | partial | not_measured)
--null-metric   report summary.correct_pct as null
--judge-runs N  recorded, so tests can see what the suite passed
It never calls Bedrock.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--plan-usd", type=float, default=0.5)
    ap.add_argument("--max-usd", type=float)
    ap.add_argument("--usd", type=float, default=0.1)
    ap.add_argument("--status", default="measured")
    ap.add_argument("--null-metric", action="store_true")
    ap.add_argument("--judge-runs", type=int)
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    if a.dry_run:
        (a.out_dir / "result.json").write_text(json.dumps(
            {"dry_run": True, "plan": {"est_cost_usd": a.plan_usd, "calls": 3,
                                       "judge_runs": a.judge_runs}}))
        return 0
    if a.max_usd is not None and a.usd > a.max_usd:
        print(f"STOPPED by cap {a.max_usd}")
        return 3
    log = Path(os.environ.get("EVAL_BEDROCK_LOG") or a.out_dir / "bedrock_calls.jsonl")
    with open(log, "a") as f:
        f.write(json.dumps({"ts": f"t-{a.out_dir.name}", "tag": f"fake:{a.out_dir.name}",
                            "input_tokens": 1, "output_tokens": 1, "cost_usd": a.usd}) + "\n")
    (a.out_dir / "result.json").write_text(json.dumps({
        "measurement": {"status": a.status, "reason": None if a.status == "measured" else "test"},
        "summary": {"correct_pct": None if a.null_metric else 80.0,
                    "measurement_status": a.status},
        "cap_received": a.max_usd, "judge_runs_received": a.judge_runs,
        "log_used": str(log)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

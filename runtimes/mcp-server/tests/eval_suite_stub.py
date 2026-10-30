#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Stand-in for eval/ci/run_suite.py in the launcher's evaluation tests. No AWS, no evaluator.

Accepts run_suite.py's flags, appends its argv to $STUB_ARGV_LOG, sleeps $STUB_SLEEP seconds, and
writes a suite.json in run_suite.py's shape under --out:

  --plan    budget.planned_usd = $STUB_PLAN_USD (default 2.5); exit 3 if over --budget-usd
  --smoke   cap $0, one deterministic score.json of the run's bottled spec (method "deterministic")
  (full)    spent_usd = $STUB_SPEND_USD (default 1.25), a bedrock_calls.jsonl with that cost
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True)
ap.add_argument("--out", required=True, type=Path)
ap.add_argument("--var", action="append", default=[])
ap.add_argument("--smoke", action="store_true")
ap.add_argument("--plan", action="store_true")
ap.add_argument("--budget-usd", type=float)
ap.add_argument("--judge-runs", type=int)
a = ap.parse_args()

log = os.environ.get("STUB_ARGV_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
time.sleep(float(os.environ.get("STUB_SLEEP", "0")))

run = dict(v.split("=", 1) for v in a.var).get("run", "")
a.out.mkdir(parents=True, exist_ok=True)
cap = 0.0 if a.smoke else (a.budget_usd if a.budget_usd is not None else 3.0)
planned = float(os.environ.get("STUB_PLAN_USD", "2.5"))
spend = 0.0 if (a.smoke or a.plan) else float(os.environ.get("STUB_SPEND_USD", "1.25"))
stages = [{"id": "graph-eval", "evaluator": "graph_eval", "status": "ok", "estimated_usd": 0.0,
           "metrics": {}, "spend": {"stage_usd": 0.0}}]
if a.plan:
    stages.append({"id": "score-bottled", "evaluator": "score", "estimated_usd": planned,
                   "status": "planned" if planned <= cap else "budget_exceeded",
                   "estimate": {"source": "evaluator_plan"}, "cap_passed_usd": cap, "metrics": {},
                   "spend": {}})
elif a.smoke:
    sd = a.out / "score-bottled"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "score.json").write_text(json.dumps({
        "scorer_version": "stub", "method": "deterministic",
        "spec": f"{run}/bottled_spec/bottled_spec.md", "key": "synthetic-corpus/ap-invoices/ANSWER_KEY.yaml",
        "counts": {"key_items": 48, "findings": 10, "citations": 20},
        "recall": {"any_match_pct": 93.8, "strict_full_pct": 90.0},
        "precision": {"keyed_pct": 50.0, "incorrect": 0},
        "citations": {"validity_pct": 100.0, "valid": 20, "invalid": 0},
        "key_coverage": {}}))
    stages.append({"id": "score-bottled", "evaluator": "score", "status": "ok", "estimated_usd": 0.0,
                   "result_file": "score-bottled/score.json", "metrics": {}, "spend": {"stage_usd": 0.0}})
else:
    sd = a.out / "score-bottled"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "bedrock_calls.jsonl").write_text(json.dumps({"ts": "t", "tag": "stub", "cost_usd": spend}) + "\n")
    if os.environ.get("STUB_WRITE_SCORE"):      # a judged score.json, as score.py writes one
        lo, hi = (float(x) for x in os.environ.get("STUB_CI", "75,88").split(","))
        key_ref = os.environ.get("STUB_SCORE_KEY", "synthetic-corpus/ap-invoices/ANSWER_KEY.yaml")
        cov = {}
        try:
            import yaml
            items = (yaml.safe_load(Path(key_ref).read_text()) or {}).get("items") or []
            cov = {it.get("id"): ("full" if i % 2 == 0 else "missed") for i, it in enumerate(items)}
        except Exception:
            pass
        (sd / "score.json").write_text(json.dumps({
            "scorer_version": "stub", "method": "llm", "spec": f"{run}/bottled_spec/bottled_spec.md",
            "key": key_ref, "counts": {"key_items": len(cov) or 48, "findings": 30, "citations": 40},
            "recall": {"any_match_pct": 81.2, "strict_full_pct": 70.0},
            "precision": {"keyed_pct": 60.0, "incorrect": 1},
            "citations": {"validity_pct": 100.0, "valid": 40, "invalid": 0},
            "judge": {"runs": a.judge_runs or 1, "model": "stub",
                      "bootstrap_ci95": {"recall_any_match_pct": {"lo": lo, "hi": hi}}},
            "key_coverage": cov}))
    stages.append({"id": "score-bottled", "evaluator": "score", "status": "ok", "estimated_usd": planned,
                   "judge_runs": a.judge_runs, "metrics": {}, "spend": {"stage_usd": spend}})
over = a.plan and planned > cap
report = {
    "suite_schema_version": 1,
    "provenance": {"suite": Path(a.config).stem, "plan": a.plan, "smoke": a.smoke,
                   "git": {"commit": "0" * 40, "branch": "stub", "dirty": False},
                   "model_id": "us.anthropic.claude-sonnet-4-6", "region": "us-east-1",
                   "judge_runs_default": a.judge_runs or 1, "corpora": {}, "vars": {"run": run}},
    "started_at": "2026-10-08T00:00:00+00:00", "duration_s": 0.1, "out_dir": str(a.out),
    "budget": {"cap_usd": cap, "spent_usd": spend, "calls": 1 if spend else 0,
               "remaining_usd": cap - spend, "aborted": "plan exceeds the budget" if over else None,
               **({"planned_usd": planned} if a.plan else {})},
    "stages": stages, "thresholds": [],
    "verdict": "plan exceeds the budget" if over else ("plan fits the budget" if a.plan else "pass"),
    "exit_code": 3 if over else 0,
}
(a.out / "suite.json").write_text(json.dumps(report, indent=1))
print(f"stub suite: {report['verdict']}")
sys.exit(report["exit_code"])

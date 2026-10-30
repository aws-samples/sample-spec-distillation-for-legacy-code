#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run the whole spec-distillation evaluation suite and emit one machine-readable report.

    # every commit: deterministic layers only, no Bedrock calls, well under a minute
    python3 eval/ci/run_suite.py --config eval/ci/suites/ap-invoices.yaml --smoke \
        --out eval/ci/runs/smoke

    # full run against a pipeline output, with a hard budget cap
    python3 eval/ci/run_suite.py --config eval/ci/suites/ap-invoices.yaml \
        --var run=output/ap-invoices --budget-usd 3 --out eval/ci/runs/2026-10-06

    # regression gate against an earlier suite
    python3 eval/ci/run_suite.py --config ... --baseline eval/ci/runs/2026-10-06/suite.json

    # plan only: per-stage cost estimates and the caps that would be passed down, no spend
    python3 eval/ci/run_suite.py --config ... --var run=output/ap-invoices --plan

Outputs under ``--out``
-----------------------
``suite.json``   the full machine-readable report: provenance, per-stage status, metrics,
                 spend, threshold verdicts, baseline comparison.
``suite.md``     the same thing for humans.
``<stage-id>/``  each evaluator's own output files, untouched.

What it guarantees
------------------
**Budget.** Every stage runs with ``EVAL_BEDROCK_LOG=<stage out>/bedrock_calls.jsonl``, so
its calls are logged where only this suite reads them; spend is read from those logs (plus
any ``tokens.jsonl``, plus the per-machine ``eval/logs/local/bedrock_calls.jsonl`` as a
backstop), keyed per record so concurrent calls from another session are never charged here.
``eval/logs/bedrock_calls.jsonl`` (a frozen historical record, if present) is ignored.

Before an LLM stage starts, its cost is ESTIMATED, in this order:
  1. the evaluator's own plan (``score.py --dry-run-judge``, ``benefits/* --dry-run``), run
     with the stage's real arguments; judge-cache hits count as $0;
  2. otherwise ``suite_lib.CALIBRATED_COSTS`` (measured per-pass / per-item costs);
  3. a stage's ``est_usd`` is a floor, never a substitute: max(estimate, est_usd).
If ``spent + estimate > cap`` the stage is not started: it is recorded ``budget_exceeded``,
the remaining stages ``not_run``, and the suite exits 3. Every LLM stage is also handed the
remaining budget as its own hard cap (``--max-judge-cost-usd`` for score.py, ``--max-usd`` for
benefits/*); an evaluator that declares a cap flag but no longer advertises it is refused
(``error``) rather than run uncapped, and one that exits 3 (its own cap tripped) is recorded
``budget_exceeded``.

**Judge passes.** ``judge_runs`` (suite level, overridable per stage) is passed to score.py
as ``--judge-runs``. It defaults to 1 in the CI suites (score.py's own default is 3), so the
estimate, the cap and the spend all describe the same number of passes.

**Unmeasured is not zero.** A metric reported as null is kept as null; a threshold on it is
``unmeasured`` and fails the run. An evaluator that reports ``measurement.status:
not_measured`` (e.g. the budget ran out before grading) puts its stage in status
``not_measured``, which fails the run; ``partial`` is reported but only fails through
thresholds. A key that loads zero items makes the evaluator exit non-zero (stage ``failed``).

**Plan mode.** ``--plan`` runs the deterministic stages (they are free and later stages may
need their outputs), and for every LLM stage records the estimate, its source and the exact
command with the cap that would be passed down, without calling Bedrock. Exit 3 if the plan
does not fit the cap.

**Provenance.** Every number can be traced to its inputs: repo commit and dirty flag,
model id, region, per-corpus content hash (with the answer key excluded from the hash
input list so the hash describes what the tool actually saw), the sha256 and the
``--help`` fingerprint of every evaluator, the suite config hash, seeds and timestamps.

**Black-box evaluators.** No evaluator is imported. Each one's flags are discovered from
``--help`` at runtime; unavailable flags are dropped and recorded; a missing tool or a
missing required flag becomes a stage status, not a crash.

**Smoke mode.** ``--smoke`` forces every stage onto its deterministic path, redirects the
Bedrock log to a per-run sentinel file and fails the run (exit 4) if a single call was
logged. Stages marked ``smoke: false`` are skipped.

Exit codes
----------
0 everything passed; 1 a threshold was violated, a threshold's metric was null or absent, a
stage measured nothing, or a baseline regression was found; 2 configuration or
infrastructure error (bad config, missing non-optional input, evaluator failed); 3 budget
exhausted (or, with --plan, would be); 4 an LLM call happened during ``--smoke``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from suite_lib import (                                        # noqa: E402
    DEFAULT_SUITE_JUDGE_RUNS, EVALUATORS, SHARED_BEDROCK_LOG, SUITE_SCHEMA_VERSION, Budget,
    ConfigError, build_argv, calibrated_estimate, canonical, compare_to_baseline, dotted,
    flatten, load_config, measurement_of, probe_flags, provenance, sha256_file, tolerance_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

EXIT_OK, EXIT_THRESHOLD, EXIT_CONFIG, EXIT_BUDGET, EXIT_SMOKE_LLM = 0, 1, 2, 3, 4


def rp(p: str | Path) -> Path:
    """Resolve a config path: absolute stays, relative is taken from the repo root."""
    q = Path(p)
    return q if q.is_absolute() else REPO_ROOT / q


# --------------------------------------------------------------------------- stage run
NON_PATH_KEYS = {"adapter", "tool_name", "categories", "segment", "seed", "line_slack",
                 "judge_runs", "avoid_key_margin", "avoid_key_scope",
                 "cache_dir"}   # created on first write; not an input that must exist
#: an evaluator's exit code when its own spend cap stopped it (score.py, see EXIT_BUDGET there)
EVALUATOR_EXIT_BUDGET = 3


def missing_inputs(ev, stage: dict, cfg: dict) -> list[str]:
    """Input paths the stage names that do not exist (ints and bare words are not paths)."""
    out = []
    for key in [k for k, _ in ev.maps] + list(ev.positional):
        if key in NON_PATH_KEYS:
            continue
        v = stage.get(key)
        if isinstance(v, str) and ("/" in v or v.endswith((".md", ".json", ".yaml", ".yml"))):
            if not rp(v).exists():
                out.append(f"{key}={v}")
    return out


def resolve_stage_refs(stage: dict, out_root: Path) -> dict:
    """Expand ``${stage:<id>}`` to that stage's output directory, so a later stage can
    consume an earlier one's artifacts without hard-coding the suite output path."""
    import re as _re

    def sub(v):
        if isinstance(v, str):
            return _re.sub(r"\$\{stage:([A-Za-z0-9_.-]+)\}", lambda m: str(out_root / m.group(1)), v)
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        return v
    return {k: sub(v) for k, v in stage.items()}


def stage_env(out_dir: Path, smoke: bool) -> dict:
    """Environment for one evaluator: its Bedrock calls go to its own log."""
    env = dict(os.environ)
    env["EVAL_BEDROCK_LOG"] = str(out_dir / "bedrock_calls.jsonl")
    if smoke:
        env["EVAL_SMOKE"] = "1"
    return env


def estimate_stage(ev, st: dict, flags: set[str], out_dir: Path, ename: str) -> dict:
    """Cost estimate for one LLM stage: the evaluator's own plan, else the calibrated table;
    a configured ``est_usd`` acts as a floor. Never calls Bedrock."""
    out: dict = {"usd": None, "source": None, "detail": None}
    if ev.plan_flags and all(f in flags for f in ev.plan_flags) and ev.plan_cost_key:
        plan_dir = out_dir / "_plan"
        plan_dir.mkdir(parents=True, exist_ok=True)
        argv, _ = build_argv(REPO_ROOT, ev, st, plan_dir, flags, smoke=False, budget_left=None,
                             plan=True)
        out["plan_command"] = " ".join(argv)
        try:
            p = subprocess.run(argv, capture_output=True, text=True, cwd=str(REPO_ROOT),
                               timeout=int(st.get("plan_timeout_s", 900)),
                               env=stage_env(plan_dir, smoke=False))
            res = plan_dir / ev.result
            data = json.loads(res.read_text()) if p.returncode == 0 and res.is_file() else None
            v = dotted(data, ev.plan_cost_key) if data else None
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.update(usd=float(v), source="evaluator_plan",
                           detail=f"{ev.script} {' '.join(ev.plan_flags)} -> {ev.plan_cost_key}")
                plan = dotted(data, ev.plan_cost_key.rsplit(".", 1)[0]) or {}
                out["plan"] = {k: plan.get(k) for k in
                               ("calls", "runs", "findings", "batches", "expected_cache_hits",
                                "expected_bedrock_calls", "est_cost_usd_uncached", "questions",
                                "changes", "key_items", "context") if k in plan}
            else:
                out["plan_error"] = (f"plan run exited {p.returncode}: "
                                     f"{(p.stderr or p.stdout or '')[-300:]}")
        except Exception as exc:                               # pragma: no cover
            out["plan_error"] = f"plan run failed: {exc}"
    if out["usd"] is None:
        usd, basis = calibrated_estimate(ename, st, REPO_ROOT)
        if usd is not None:
            out.update(usd=usd, source="calibrated", detail=basis)
    floor = st.get("est_usd")
    if isinstance(floor, (int, float)) and not isinstance(floor, bool):
        if out["usd"] is None or floor > out["usd"]:
            out.update(usd=float(floor), source="stage_est_usd" if out["usd"] is None
                       else f"stage_est_usd (floor over {out['source']} ${out['usd']:.4f})")
    if out["usd"] is None:
        out.update(usd=0.0, source="none",
                   detail="no plan, no calibrated cost, no est_usd: the cap passed down is the "
                          "only protection")
    out["usd"] = round(out["usd"], 4)
    return out


def run_stage(stage: dict, cfg: dict, out_root: Path, budget: Budget,  # noqa: C901
              smoke: bool, plan: bool = False, estimate_only: bool = False) -> dict:
    sid, ename = stage["id"], stage["evaluator"]
    rec: dict = {"id": sid, "evaluator": ename, "status": "pending", "notes": [],
                 "metrics": {}, "spend": {}, "started_at": None, "duration_s": None}
    ev = EVALUATORS.get(ename)
    if ev is None:
        rec.update(status="error", reason=f"unknown evaluator '{ename}'; known: "
                                         f"{', '.join(sorted(EVALUATORS))}")
        return rec

    # ---- corpus defaults
    st = resolve_stage_refs(stage, out_root)
    if st.get("corpus"):
        c = cfg["corpora"][st["corpus"]]
        st.setdefault("source_root", c["source_root"])
        st.setdefault("key", c["key"])
        st.setdefault("exclude", c.get("exclude") or [])
    for k, v in (cfg.get("seeds") or {}).items():
        if k == ename or k == sid:
            st.setdefault("seed", v)
    if any(k == "judge_runs" for k, _ in ev.maps):
        st.setdefault("judge_runs", cfg.get("judge_runs", DEFAULT_SUITE_JUDGE_RUNS))
        rec["judge_runs"] = st["judge_runs"]

    # ---- evaluator availability and flag probe
    exists, flags, raw_help = probe_flags(REPO_ROOT, ev.script)
    rec["evaluator_path"] = ev.script
    rec["evaluator_sha256"] = sha256_file(REPO_ROOT / ev.script)
    rec["evaluator_help_sha256"] = __import__("hashlib").sha256(raw_help.encode()).hexdigest()[:16]
    if not exists:
        rec.update(status="skipped" if st.get("optional") else "error",
                   reason=f"evaluator not present: {ev.script}")
        return rec
    if raw_help.startswith("__probe_failed__"):
        rec.update(status="skipped" if st.get("optional") else "error",
                   reason=f"could not probe {ev.script} --help: {raw_help}")
        return rec
    lacking = [f for f in ev.required if f not in flags]
    if lacking:
        rec.update(status="skipped" if st.get("optional") else "error",
                   reason=f"{ev.script} does not advertise required flag(s) {lacking}; "
                          f"its interface changed")
        return rec

    # ---- inputs
    miss = missing_inputs(ev, st, cfg)
    if miss:
        rec.update(status="skipped" if st.get("optional") else "error",
                   reason=f"input(s) not found: {', '.join(miss)}")
        return rec

    # ---- smoke gating
    if smoke and st.get("smoke") is False:
        rec.update(status="skipped", reason="stage is not part of --smoke")
        return rec
    if smoke and ev.uses_llm and not (ev.smoke_flags or st.get("smoke_args")):
        rec.update(status="skipped",
                   reason=f"{ev.script} has no deterministic mode, so it cannot run in --smoke")
        return rec

    out_dir = out_root / sid
    llm_stage = ev.uses_llm and not smoke

    # ---- a capped tool must still advertise its cap: never run an LLM stage uncapped
    if llm_stage and ev.budget_flag and ev.budget_flag not in flags:
        rec.update(status="error",
                   reason=f"{ev.script} no longer advertises its spend cap {ev.budget_flag}; "
                          f"refusing to run it uncapped")
        return rec

    # ---- budget guard: a realistic estimate, checked before the stage starts
    if llm_stage:
        e = estimate_stage(ev, st, flags, out_dir, ename)
        est = e["usd"]
        rec["estimate"] = e
    else:
        est = 0.0
    rec["estimated_usd"] = est
    if estimate_only:
        rec.update(status="not_run",
                   reason=f"a real run stops before this stage (budget exhausted earlier); "
                          f"estimated ${est:.4f}")
        return rec
    ok, why = budget.allows(est)
    if not ok:
        rec.update(status="budget_exceeded", reason=why)
        if plan:
            rec["cap_passed_usd"] = round(budget.left, 4) if ev.budget_flag else None
        return rec

    out_dir.mkdir(parents=True, exist_ok=True)
    # the remaining budget is passed down as the tool's own hard cap; in --smoke it is $0
    cap_left = 0.0 if smoke else budget.left
    argv, notes = build_argv(REPO_ROOT, ev, st, out_dir, flags, smoke, cap_left)
    rec["notes"] += notes
    rec["command"] = " ".join(argv)
    if ev.budget_flag and ev.budget_flag in flags:
        rec["cap_passed_usd"] = round(max(0.0, cap_left), 4)
    if plan and llm_stage:
        rec.update(status="planned",
                   reason=f"would run: est ${est:.4f} ({rec['estimate']['source']}), "
                          f"cap passed down ${rec.get('cap_passed_usd', 0):.4f}")
        budget.reserve(est)
        return rec
    rec["started_at"] = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, cwd=str(REPO_ROOT),
                           timeout=int(st.get("timeout_s", 3600)),
                           env=stage_env(out_dir, smoke))
        rc, stdout, stderr = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        rc, stdout, stderr = -1, "", f"timed out after {st.get('timeout_s', 3600)}s"
    except Exception as exc:                                   # pragma: no cover
        rc, stdout, stderr = -1, "", str(exc)
    rec["duration_s"] = round(time.time() - t0, 2)
    rec["returncode"] = rc
    (out_dir / "stdout.txt").write_text(stdout or "")
    (out_dir / "stderr.txt").write_text(stderr or "")

    rec["spend"] = budget.collect([out_dir / "bedrock_calls.jsonl", out_dir / "tokens.jsonl"])

    result = out_dir / ev.result if ev.result else None
    if rc == EVALUATOR_EXIT_BUDGET and ev.budget_flag:
        rec.update(status="budget_exceeded",
                   reason=f"{ev.script} stopped at its own cap ({ev.budget_flag} "
                          f"{rec.get('cap_passed_usd')}): {(stderr or stdout or '')[-300:]}")
        return rec
    if rc != 0:
        rec.update(status="failed",
                   reason=f"{ev.script} exited {rc}: {(stderr or stdout or '')[-500:]}")
        return rec
    if result is not None and not result.exists():
        rec.update(status="failed",
                   reason=f"{ev.script} exited 0 but wrote no {ev.result}")
        return rec
    if result is not None:
        try:
            data = json.loads(result.read_text())
        except Exception as exc:
            rec.update(status="failed", reason=f"{ev.result} is not valid JSON: {exc}")
            return rec
        # relative to the suite out dir, so the path does not vary with --out
        rec["result_file"] = str(result.relative_to(out_root))
        rec["result_sha256"] = sha256_file(result)
        # None is kept: a null metric means "not measured" and must stay visible as such
        rec["metrics"] = {k: v for k, v in flatten(data).items()
                          if (v is None or isinstance(v, (int, float, str, list))) and len(k) < 120}
        mstatus, mreason = measurement_of(data)
        if mstatus:
            rec["measurement"] = {"status": mstatus, "reason": mreason}
        if mstatus == "not_measured":
            rec.update(status="not_measured",
                       reason=f"{ev.script} measured nothing: {mreason or 'no reason given'}")
            return rec
        if mstatus == "partial":
            rec.update(status="partial", reason=f"partial measurement: {mreason}")
            return rec
    rec["status"] = "ok"
    return rec


# --------------------------------------------------------------------------- thresholds
def check_thresholds(cfg: dict, stages: list[dict], smoke: bool) -> list[dict]:
    by_id = {s["id"]: s for s in stages}
    out = []
    for t in cfg.get("thresholds") or []:
        rec = {"stage": t["stage"], "metric": t["metric"],
               "min": t.get("min"), "max": t.get("max"), "equals": t.get("equals")}
        if smoke and not t.get("applies_in_smoke"):
            rec.update(verdict="skipped", reason="threshold does not apply in --smoke")
            out.append(rec)
            continue
        st = by_id.get(t["stage"])
        if st is None or st["status"] not in ("ok", "partial", "not_measured"):
            rec.update(verdict="skipped",
                       reason=f"stage {t['stage']} status "
                              f"{(st or {}).get('status', 'absent')}")
            out.append(rec)
            continue
        if t["metric"] not in st["metrics"]:
            rec.update(verdict="unmeasured", value=None,
                       reason=f"stage {t['stage']} produced no metric '{t['metric']}'; "
                              f"the evaluator's output schema does not contain it")
            out.append(rec)
            continue
        v = st["metrics"][t["metric"]]
        rec["value"] = v
        if v is None:
            m = st.get("measurement") or {}
            rec.update(verdict="unmeasured",
                       reason=f"metric is null: {t['stage']} did not measure it "
                              f"(measurement {m.get('status', '?')}: {m.get('reason') or '-'})")
            out.append(rec)
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            rec.update(verdict="unmeasured", reason=f"metric value {v!r} is not numeric")
            out.append(rec)
            continue
        bad = []
        if t.get("min") is not None and v < t["min"]:
            bad.append(f"{v} < min {t['min']}")
        if t.get("max") is not None and v > t["max"]:
            bad.append(f"{v} > max {t['max']}")
        if t.get("equals") is not None and v != t["equals"]:
            bad.append(f"{v} != {t['equals']}")
        rec.update(verdict="fail" if bad else "pass", reason="; ".join(bad))
        out.append(rec)
    return out


# --------------------------------------------------------------------------- report
def to_markdown(r: dict) -> str:
    p = r["provenance"]
    L = [f"# Suite `{p['suite']}`{' (smoke)' if p['smoke'] else ''}", "",
         f"- Verdict: **{r['verdict']}** (exit {r['exit_code']})",
         f"- Config: `{p['suite_config']['path']}` sha256 `{p['suite_config']['sha256'][:12]}`",
         f"- Repo: `{p['git']['commit'][:12]}` on `{p['git']['branch']}`"
         f"{' (dirty worktree)' if p['git']['dirty'] else ''}",
         f"- Model: `{p['model_id']}` in `{p['region']}`; seeds {p['seeds'] or '{}'}",
         f"- Started {r['started_at']}, {r['duration_s']}s, python {p['python']}",
         f"- Spend: **${r['budget']['spent_usd']:.4f}** of ${r['budget']['cap_usd']:.2f} cap "
         f"({r['budget']['calls']} logged Bedrock calls)"
         + (f"; planned **${r['budget'].get('planned_usd', 0):.4f}**" if p.get("plan") else ""),
         f"- Judge passes (score.py stages): {p.get('judge_runs_default')}", ""]
    L += ["## Corpora", "", "| Corpus | Root | Files | Content sha256 | Key sha256 |",
          "|---|---|---|---|---|"]
    for name, c in p["corpora"].items():
        L.append(f"| {name} | `{c['source_root']}`{'' if c['exists'] else ' **(missing)**'} | "
                 f"{c['content']['n_files']} | `{c['content']['sha256'][:12]}` | "
                 f"`{(c['key_sha256'] or '-')[:12]}` |")
    L += ["", "## Stages", "",
          "| Stage | Evaluator | Status | Wall s | Est $ | Estimate source | Cap passed $ | Spend $ | Notes |",
          "|---|---|---|---|---|---|---|---|---|"]
    for s in r["stages"]:
        note = s.get("reason", "") or "; ".join(s.get("notes", []))
        e = s.get("estimate") or {}
        cap = s.get("cap_passed_usd")
        L.append(f"| {s['id']} | `{s['evaluator']}` | **{s['status']}** | "
                 f"{s.get('duration_s') or '-'} | "
                 f"{s.get('estimated_usd', 0) or 0:.4f} | {e.get('source') or '-'} | "
                 f"{'-' if cap is None else f'{cap:.4f}'} | "
                 f"{s.get('spend', {}).get('stage_usd', 0):.4f} | {note[:140]} |")
    th = r["thresholds"]
    if th:
        L += ["", "## Thresholds", "", "| Stage | Metric | Bound | Value | Verdict | Why |",
              "|---|---|---|---|---|---|"]
        for t in th:
            bound = ", ".join(f"{k}={t[k]}" for k in ("min", "max", "equals")
                              if t.get(k) is not None) or "-"
            L.append(f"| {t['stage']} | `{t['metric']}` | {bound} | {t.get('value', '-')} | "
                     f"**{t['verdict']}** | {t.get('reason', '')[:100]} |")
    b = r.get("baseline_comparison")
    if b:
        L += ["", "## Baseline comparison", ""]
        if not b["compared"]:
            L.append(f"Not compared: {b['reason']}")
        else:
            L.append(f"{b['checked']} metric(s) compared; {len(b['regressions'])} regression(s), "
                     f"{len(b['improvements'])} improvement(s).")
            if b["regressions"]:
                L += ["", "| Stage | Metric | Baseline | Current | Delta | Band |",
                      "|---|---|---|---|---|---|"]
                for x in b["regressions"]:
                    L.append(f"| {x['stage']} | `{x['metric']}` | {x['baseline']} | "
                             f"{x['current']} | {x['delta']:+g} | {x['band']} |")
    L += ["", "## Headline metrics", ""]
    for s in r["stages"]:
        if s["status"] != "ok" or not s["metrics"]:
            continue
        keys = [k for k in s["metrics"]
                if tolerance_for(k)[1] != "informational"
                and isinstance(s["metrics"][k], (int, float))
                and not isinstance(s["metrics"][k], bool)][:14]
        if not keys:
            continue
        L += [f"**{s['id']}**", "", "| Metric | Value |", "|---|---|"]
        L += [f"| `{k}` | {s['metrics'][k]} |" for k in keys] + [""]
    if p["smoke"]:
        L += ["", "> Smoke mode: deterministic layers only. No LLM judge ran, so recall and "
              "precision of meaning are **not** measured here. See the "
              "\"What these numbers mean\" section of `eval/README.md`.", ""]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- main
def run(cfg_path: Path, out: Path, smoke: bool, budget_cap: float | None,
        baseline: Path | None, only: list[str] | None, overrides: dict[str, str],
        plan: bool = False, judge_runs: int | None = None) -> dict:
    cfg = load_config(cfg_path, overrides)
    if judge_runs is not None:
        if judge_runs < 1:
            raise ConfigError(f"--judge-runs must be >= 1 (got {judge_runs})")
        cfg["judge_runs"] = judge_runs     # stages that pin their own judge_runs keep it
    out.mkdir(parents=True, exist_ok=True)

    cap = budget_cap if budget_cap is not None else float(cfg["budget_usd"])
    if smoke:
        cap = 0.0
    shared_log = REPO_ROOT / SHARED_BEDROCK_LOG
    # every stage logs into its own directory (stage_env); the per-machine log and the
    # smoke sentinel are watched only as backstops for a tool that ignores EVAL_BEDROCK_LOG
    sentinel = out / "smoke_bedrock_calls.jsonl"
    budget = Budget(cap_usd=cap, logs=[shared_log, sentinel])
    budget.prime()

    prov = provenance(REPO_ROOT, cfg_path, cfg, smoke)
    prov["plan"] = plan
    started = datetime.now(timezone.utc)
    t0 = time.time()

    stages: list[dict] = []
    aborted = None
    for stage in cfg["stages"]:
        if only and stage["id"] not in only:
            stages.append({"id": stage["id"], "evaluator": stage["evaluator"],
                           "status": "skipped", "reason": "not selected by --stage",
                           "metrics": {}, "notes": [], "spend": {}})
            continue
        rec = run_stage(stage, cfg, out, budget, smoke, plan,
                        estimate_only=bool(plan and aborted))
        stages.append(rec)
        est_txt = (f"est ${rec['estimated_usd']:.4f} ({(rec.get('estimate') or {}).get('source')})  "
                   if rec.get("estimate") else "")
        cap_txt = (f"cap->${rec['cap_passed_usd']:.4f}  " if rec.get("cap_passed_usd") is not None
                   and rec.get("estimate") else "")
        print(f"[{rec['status']:16s}] {rec['id']:28s} "
              f"{rec.get('duration_s') or '-':>7} s  "
              f"${rec.get('spend', {}).get('stage_usd', 0):.4f}  {est_txt}{cap_txt}"
              f"{rec.get('reason', '')[:90]}", flush=True)
        if rec["status"] == "budget_exceeded":
            if plan:
                # a real run stops here; keep going only to PRICE the remaining stages
                # (estimate_only: they are recorded not_run, with their estimate)
                aborted = f"plan exceeds the budget at stage '{rec['id']}': {rec['reason']}"
                continue
            aborted = f"budget exhausted at stage '{rec['id']}': {rec['reason']}"
            for later in cfg["stages"][len(stages):]:
                stages.append({"id": later["id"], "evaluator": later["evaluator"],
                               "status": "not_run", "reason": "suite aborted on budget",
                               "metrics": {}, "notes": [], "spend": {}})
            break

    thresholds = check_thresholds(cfg, stages, smoke)
    report = {
        "suite_schema_version": SUITE_SCHEMA_VERSION,
        "provenance": prov,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": round(time.time() - t0, 2),
        "out_dir": str(out),
        "budget": {"cap_usd": cap, "spent_usd": budget.spent, "calls": budget.calls,
                   "remaining_usd": budget.left, "aborted": aborted,
                   **({"planned_usd": round(budget.planned, 4)} if plan else {})},
        "stages": stages,
        "thresholds": thresholds,
    }
    if baseline:
        if not baseline.is_file():
            report["baseline_comparison"] = {"compared": False,
                                             "reason": f"baseline not found: {baseline}",
                                             "regressions": [], "improvements": [], "checked": 0}
        else:
            try:
                bl = json.loads(baseline.read_text())
            except Exception as exc:
                bl = None
                report["baseline_comparison"] = {
                    "compared": False, "reason": f"baseline is not valid JSON: {exc}",
                    "regressions": [], "improvements": [], "checked": 0}
            if bl is not None:
                report["baseline_comparison"] = compare_to_baseline(report, bl)
        report["baseline"] = str(baseline)

    # ---- verdict
    smoke_calls = budget.calls if smoke else 0
    failures = [t for t in thresholds if t["verdict"] == "fail"]
    unmeasured = [t for t in thresholds if t["verdict"] == "unmeasured"]
    errors = [s for s in stages if s["status"] in ("error", "failed")]
    not_measured = [s for s in stages if s["status"] == "not_measured"]
    partial = [s for s in stages if s["status"] == "partial"]
    regressions = (report.get("baseline_comparison") or {}).get("regressions") or []

    if smoke and smoke_calls:
        code, verdict = EXIT_SMOKE_LLM, "smoke mode made LLM calls"
    elif aborted:
        code, verdict = EXIT_BUDGET, ("plan exceeds the budget" if plan else "budget exhausted")
    elif errors:
        code, verdict = EXIT_CONFIG, f"{len(errors)} stage(s) failed"
    elif not_measured:
        code, verdict = EXIT_THRESHOLD, (f"{len(not_measured)} stage(s) measured nothing: "
                                         f"{', '.join(s['id'] for s in not_measured)}")
    elif failures or regressions:
        code, verdict = EXIT_THRESHOLD, (f"{len(failures)} threshold violation(s), "
                                         f"{len(regressions)} regression(s)")
    elif unmeasured:
        code, verdict = EXIT_THRESHOLD, f"{len(unmeasured)} threshold(s) had no metric to read"
    elif plan:
        code, verdict = EXIT_OK, "plan fits the budget"
    else:
        code, verdict = EXIT_OK, "pass"
    if partial and code in (EXIT_OK, EXIT_THRESHOLD):
        verdict += (f" ({len(partial)} stage(s) only partly measured: "
                    f"{', '.join(s['id'] for s in partial)})")
    report["smoke_llm_calls"] = smoke_calls
    report["verdict"] = verdict
    report["exit_code"] = code

    (out / "suite.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "suite.md").write_text(to_markdown(report))
    (out / "suite.canonical.json").write_text(
        json.dumps(canonical(report), indent=2, sort_keys=True) + "\n")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, type=Path, help="eval/ci/suites/*.yaml")
    ap.add_argument("--out", type=Path, help="output dir (default: eval/ci/runs/<suite>-<ts>)")
    ap.add_argument("--smoke", action="store_true",
                    help="deterministic layers only, no LLM calls, budget forced to $0")
    ap.add_argument("--budget-usd", type=float, help="override the config's budget cap")
    ap.add_argument("--judge-runs", type=int,
                    help="override the suite's judge_runs (score.py passes) for stages that do not "
                         "pin their own")
    ap.add_argument("--plan", action="store_true",
                    help="estimate every LLM stage (evaluator plan or calibrated table) and show "
                         "the cap each would be given, without calling Bedrock; deterministic "
                         "stages still run")
    ap.add_argument("--baseline", type=Path, help="an earlier suite.json to compare against")
    ap.add_argument("--stage", action="append", default=[], help="run only this stage; repeatable")
    ap.add_argument("--var", action="append", default=[],
                    help="override a config variable, KEY=VALUE; repeatable")
    a = ap.parse_args()

    overrides = {}
    for kv in a.var:
        if "=" not in kv:
            print(f"--var needs KEY=VALUE, got {kv!r}", file=sys.stderr)
            return EXIT_CONFIG
        k, v = kv.split("=", 1)
        overrides[k] = v

    out = a.out or (REPO_ROOT / "eval" / "ci" / "runs" /
                    f"{a.config.stem}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    try:
        r = run(a.config, out, a.smoke, a.budget_usd, a.baseline, a.stage or None, overrides,
                plan=a.plan, judge_runs=a.judge_runs)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"\n{r['verdict']} -- wrote {out}/suite.json and suite.md "
          f"(${r['budget']['spent_usd']:.4f} spent)")
    return int(r["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

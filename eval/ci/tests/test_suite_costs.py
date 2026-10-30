# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Suite cost estimates, caps, judge passes, plan mode and null handling (fixes 1 and 3).
No AWS calls: evaluators are local doubles, and the one real evaluator (score.py) is only
run with --dry-run-judge.

Regressions covered:
* the JC suite estimated $3.30 in total while score.py's default 3 judge passes cost ~$16
  per spec artifact, and ``score`` stages were given no spend cap at all;
* a null metric (Q&A graded nothing) and an evaluator that measured nothing both came
  back as "ok".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

CI_DIR = Path(__file__).resolve().parents[1]
REPO = CI_DIR.parents[1]
sys.path.insert(0, str(CI_DIR))

import run_suite                                              # noqa: E402
import suite_lib                                              # noqa: E402
from suite_lib import ConfigError, Evaluator                  # noqa: E402

FIX = "eval/ci/tests/fixtures"
PLANNED_MAPS = (("plan_usd", "--plan-usd"), ("usd", "--usd"), ("status", "--status"),
                ("judge_runs", "--judge-runs"))


@pytest.fixture(autouse=True)
def doubles(monkeypatch):
    monkeypatch.setitem(suite_lib.EVALUATORS, "fake_planned", Evaluator(
        "fake_planned", f"{FIX}/fake_planned.py", "result.json", required=("--out-dir",),
        maps=PLANNED_MAPS, out_flag="--out-dir", uses_llm=True, budget_flag="--max-usd",
        plan_flags=("--dry-run",), plan_cost_key="plan.est_cost_usd"))
    # same tool, but the suite is not told it can plan: the calibrated / est_usd route
    monkeypatch.setitem(suite_lib.EVALUATORS, "fake_unplanned", Evaluator(
        "fake_unplanned", f"{FIX}/fake_planned.py", "result.json", required=("--out-dir",),
        maps=PLANNED_MAPS, out_flag="--out-dir", uses_llm=True, budget_flag="--max-usd"))
    # declares a cap flag the tool does not advertise
    monkeypatch.setitem(suite_lib.EVALUATORS, "fake_uncapped", Evaluator(
        "fake_uncapped", f"{FIX}/fake_planned.py", "result.json", required=("--out-dir",),
        maps=PLANNED_MAPS, out_flag="--out-dir", uses_llm=True, budget_flag="--max-judge-cost-usd"))
    suite_lib._HELP_CACHE.clear()
    yield
    suite_lib._HELP_CACHE.clear()


def cfg(tmp_path: Path, stages, **over) -> Path:
    c = {"suite": "unit", "schema_version": 1, "budget_usd": 1.0,
         "vars": {"corpus": "synthetic-corpus/ap-invoices"},
         "corpora": {"c": {"source_root": "${corpus}", "key": "${corpus}/ANSWER_KEY.yaml",
                           "exclude": ["ANSWER_KEY.yaml"]}},
         "stages": stages, "thresholds": []}
    c.update(over)
    p = tmp_path / "suite.yaml"
    p.write_text(yaml.safe_dump(c, sort_keys=False))
    return p


def result(tmp_path, sid):
    return json.loads((tmp_path / "out" / sid / "result.json").read_text())


def go(tmp_path, p, **kw):
    return run_suite.run(p, tmp_path / "out", kw.pop("smoke", False), kw.pop("budget_cap", None),
                         None, None, {}, **kw)


# --------------------------------------------------------------------------- estimates
def test_estimate_comes_from_the_evaluators_own_plan(tmp_path):
    r = go(tmp_path, cfg(tmp_path, [{"id": "s", "evaluator": "fake_planned", "plan_usd": 0.7,
                                     "usd": 0.2}]))
    st = r["stages"][0]
    assert st["status"] == "ok"
    assert st["estimated_usd"] == 0.7 and st["estimate"]["source"] == "evaluator_plan"
    assert "--dry-run" in st["estimate"]["plan_command"]
    assert st["spend"]["stage_usd"] == pytest.approx(0.2)


def test_a_stage_whose_plan_exceeds_the_cap_never_starts(tmp_path):
    r = go(tmp_path, cfg(tmp_path, [
        {"id": "big", "evaluator": "fake_planned", "plan_usd": 5.4, "usd": 5.4},
        {"id": "later", "evaluator": "fake_planned", "plan_usd": 0.1}], budget_usd=3.0))
    st = {s["id"]: s for s in r["stages"]}
    assert st["big"]["status"] == "budget_exceeded" and st["big"]["estimated_usd"] == 5.4
    assert not (tmp_path / "out" / "big" / "result.json").exists()     # it was never run
    assert st["later"]["status"] == "not_run"
    assert r["budget"]["spent_usd"] == 0.0
    assert r["exit_code"] == run_suite.EXIT_BUDGET


def test_the_remaining_budget_is_passed_down_as_the_cap(tmp_path):
    go(tmp_path, cfg(tmp_path, [
        {"id": "a", "evaluator": "fake_planned", "plan_usd": 0.3, "usd": 0.4},
        {"id": "b", "evaluator": "fake_planned", "plan_usd": 0.3, "usd": 0.1}], budget_usd=1.0))
    assert result(tmp_path, "a")["cap_received"] == pytest.approx(1.0)
    assert result(tmp_path, "b")["cap_received"] == pytest.approx(0.6)


def test_est_usd_is_a_floor_not_a_substitute(tmp_path):
    r = go(tmp_path, cfg(tmp_path, [
        {"id": "lo", "evaluator": "fake_planned", "plan_usd": 0.1, "est_usd": 0.5},
        {"id": "hi", "evaluator": "fake_planned", "plan_usd": 0.3, "est_usd": 0.05}],
        budget_usd=5.0))
    st = {s["id"]: s for s in r["stages"]}
    assert st["lo"]["estimated_usd"] == 0.5 and st["lo"]["estimate"]["source"].startswith("stage_est_usd")
    assert st["hi"]["estimated_usd"] == 0.3 and st["hi"]["estimate"]["source"] == "evaluator_plan"


def test_without_a_plan_the_stage_est_usd_is_used(tmp_path):
    r = go(tmp_path, cfg(tmp_path, [{"id": "s", "evaluator": "fake_unplanned", "est_usd": 0.4}]))
    assert r["stages"][0]["estimate"]["source"] == "stage_est_usd"
    assert r["stages"][0]["estimated_usd"] == 0.4


def test_calibrated_table_reproduces_the_measured_costs(tmp_path):
    """The fallback table is the measured per-pass cost, times judge_runs."""
    jc = tmp_path / "jc_bottled.md"
    jc.write_bytes(b"x" * 555_601)                 # the JC business spec's size
    usd1, basis = suite_lib.calibrated_estimate("score", {"spec": str(jc), "judge_runs": 1}, REPO)
    usd3, _ = suite_lib.calibrated_estimate("score", {"spec": str(jc), "judge_runs": 3}, REPO)
    assert usd1 == pytest.approx(5.39, abs=0.05)   # measured: $5.39/pass
    assert usd3 == pytest.approx(3 * usd1, rel=1e-3)
    assert "100KB" in basis
    ap = tmp_path / "ap_bottled.md"
    ap.write_bytes(b"x" * 438_452)
    req, _ = suite_lib.calibrated_estimate("score", {"spec": str(ap), "segment": "requirements",
                                                      "judge_runs": 1}, REPO)
    assert req == pytest.approx(1.60, abs=0.05)   # measured: $1.60/pass requirements
    qa, _ = suite_lib.calibrated_estimate(
        "benefits_qa", {"qa": "eval/keys/jc-changeorders/qa.yaml"}, REPO)
    assert qa == pytest.approx(25 * 0.20)
    imp, _ = suite_lib.calibrated_estimate(
        "benefits_impact", {"impact": "eval/keys/jc-changeorders/impact.yaml"}, REPO)
    assert imp == pytest.approx(10 * 0.22)        # counts `scenarios:` too


def test_real_score_plan_route_on_the_mini_fixture(tmp_path):
    """The real score.py, planned through --dry-run-judge; no Bedrock call is possible."""
    p = cfg(tmp_path, [{"id": "score-mini", "evaluator": "score", "spec": "eval/fixtures/mini/spec.md",
                        "key": "eval/fixtures/mini/key.yaml", "source_root": "eval/fixtures/mini/src",
                        "judge_runs": 3, "cache_dir": str(tmp_path / "cache")}])
    r = go(tmp_path, p, plan=True, budget_cap=2.0)
    st = r["stages"][0]
    assert st["status"] == "planned", st
    assert st["estimate"]["source"] == "evaluator_plan"
    assert st["estimate"]["plan"]["runs"] == 3
    assert st["estimated_usd"] > 0
    assert "--judge-runs 3" in st["command"] and "--max-judge-cost-usd 2.0000" in st["command"]
    assert st["cap_passed_usd"] == 2.0


# --------------------------------------------------------------------------- caps
def test_real_score_stage_gets_judge_runs_and_a_cap():
    ev = suite_lib.EVALUATORS["score"]
    exists, flags, _ = suite_lib.probe_flags(REPO, ev.script)
    argv, notes = suite_lib.build_argv(REPO, ev, {"spec": "s.md", "key": "k.yaml",
                                                  "source_root": "src", "judge_runs": 1},
                                       Path("/tmp/o"), flags, smoke=False, budget_left=2.5)
    assert argv[argv.index("--judge-runs") + 1] == "1"
    assert argv[argv.index("--max-judge-cost-usd") + 1] == "2.5000"
    assert not notes


@pytest.mark.parametrize("ename", ["score", "benefits_insights", "benefits_qa", "benefits_impact",
                                   "benefits_fieldapp"])
def test_every_real_llm_evaluator_is_capped_and_plannable(ename):
    ev = suite_lib.EVALUATORS[ename]
    exists, flags, _ = suite_lib.probe_flags(REPO, ev.script)
    assert ev.budget_flag in flags, f"{ev.script} must advertise {ev.budget_flag}"
    assert ev.plan_flags and all(f in flags for f in ev.plan_flags)


def test_an_evaluator_that_drops_its_cap_flag_is_refused(tmp_path):
    r = go(tmp_path, cfg(tmp_path, [{"id": "s", "evaluator": "fake_uncapped"}]))
    assert r["stages"][0]["status"] == "error"
    assert "refusing to run it uncapped" in r["stages"][0]["reason"]


def test_an_evaluator_stopped_by_its_own_cap_is_budget_exceeded(tmp_path):
    r = go(tmp_path, cfg(tmp_path, [{"id": "s", "evaluator": "fake_planned", "plan_usd": 0.1,
                                     "usd": 9.0}]))
    assert r["stages"][0]["status"] == "budget_exceeded"
    assert r["exit_code"] == run_suite.EXIT_BUDGET


def test_each_stage_logs_into_its_own_directory(tmp_path):
    go(tmp_path, cfg(tmp_path, [{"id": "s", "evaluator": "fake_planned", "plan_usd": 0.1}]))
    assert result(tmp_path, "s")["log_used"] == str(tmp_path / "out" / "s" / "bedrock_calls.jsonl")


# --------------------------------------------------------------------------- judge passes
def test_suite_default_is_one_judge_pass(tmp_path):
    go(tmp_path, cfg(tmp_path, [{"id": "s", "evaluator": "fake_planned", "plan_usd": 0.1}]))
    assert result(tmp_path, "s")["judge_runs_received"] == 1


def test_suite_and_stage_can_pin_judge_runs(tmp_path):
    p = cfg(tmp_path, [{"id": "a", "evaluator": "fake_planned", "plan_usd": 0.1},
                       {"id": "b", "evaluator": "fake_planned", "plan_usd": 0.1, "judge_runs": 5}],
            judge_runs=3, budget_usd=5.0)
    go(tmp_path, p)
    assert result(tmp_path, "a")["judge_runs_received"] == 3
    assert result(tmp_path, "b")["judge_runs_received"] == 5
    # and the CLI override moves the suite default, not a stage that pinned its own
    run_suite.run(p, tmp_path / "out2", False, None, None, None, {}, judge_runs=2)
    assert json.loads((tmp_path / "out2" / "a" / "result.json").read_text())["judge_runs_received"] == 2
    assert json.loads((tmp_path / "out2" / "b" / "result.json").read_text())["judge_runs_received"] == 5


@pytest.mark.parametrize("bad", [0, -1, "3", True])
def test_judge_runs_must_be_a_positive_integer(tmp_path, bad):
    with pytest.raises(ConfigError, match="judge_runs"):
        suite_lib.load_config(cfg(tmp_path, [{"id": "s", "evaluator": "fake_planned"}],
                                  judge_runs=bad))


# --------------------------------------------------------------------------- plan mode
def test_plan_mode_runs_no_llm_stage_and_reports_caps(tmp_path):
    r = go(tmp_path, cfg(tmp_path, [
        {"id": "a", "evaluator": "fake_planned", "plan_usd": 0.6, "usd": 9.0},
        {"id": "b", "evaluator": "fake_planned", "plan_usd": 0.3, "usd": 9.0}], budget_usd=1.0),
        plan=True)
    st = {s["id"]: s for s in r["stages"]}
    assert st["a"]["status"] == st["b"]["status"] == "planned"
    assert st["a"]["cap_passed_usd"] == 1.0 and st["b"]["cap_passed_usd"] == pytest.approx(0.4)
    assert not (tmp_path / "out" / "a" / "result.json").exists()
    assert r["budget"]["planned_usd"] == pytest.approx(0.9) and r["budget"]["spent_usd"] == 0
    assert r["exit_code"] == 0 and r["verdict"] == "plan fits the budget"
    md = (tmp_path / "out" / "suite.md").read_text()
    assert "Estimate source" in md and "evaluator_plan" in md


def test_plan_mode_that_does_not_fit_exits_3_and_prices_the_rest(tmp_path):
    r = go(tmp_path, cfg(tmp_path, [
        {"id": "a", "evaluator": "fake_planned", "plan_usd": 0.8},
        {"id": "b", "evaluator": "fake_planned", "plan_usd": 0.5},
        {"id": "c", "evaluator": "fake_planned", "plan_usd": 0.1}], budget_usd=1.0), plan=True)
    st = {s["id"]: s for s in r["stages"]}
    assert st["a"]["status"] == "planned" and st["b"]["status"] == "budget_exceeded"
    assert st["c"]["status"] == "not_run" and st["c"]["estimated_usd"] == 0.1
    assert r["exit_code"] == run_suite.EXIT_BUDGET


# --------------------------------------------------------------------------- unmeasured
def test_a_null_metric_fails_its_threshold_as_unmeasured(tmp_path):
    p = cfg(tmp_path, [{"id": "qa", "evaluator": "fake_planned", "plan_usd": 0.1,
                        "args": {"null-metric": True}, "status": "partial"}],
            thresholds=[{"stage": "qa", "metric": "summary.correct_pct", "min": 50.0}])
    r = go(tmp_path, p)
    t = r["thresholds"][0]
    assert r["stages"][0]["metrics"]["summary.correct_pct"] is None   # kept as null, not dropped
    assert t["verdict"] == "unmeasured" and "metric is null" in t["reason"]
    assert r["exit_code"] == run_suite.EXIT_THRESHOLD


def test_a_stage_that_measured_nothing_fails_the_suite(tmp_path):
    r = go(tmp_path, cfg(tmp_path, [{"id": "qa", "evaluator": "fake_planned", "plan_usd": 0.1,
                                     "status": "not_measured"}]))
    assert r["stages"][0]["status"] == "not_measured"
    assert r["exit_code"] == run_suite.EXIT_THRESHOLD and "measured nothing" in r["verdict"]


def test_a_partly_measured_stage_is_flagged_but_judged_by_thresholds(tmp_path):
    p = cfg(tmp_path, [{"id": "qa", "evaluator": "fake_planned", "plan_usd": 0.1,
                        "status": "partial"}],
            thresholds=[{"stage": "qa", "metric": "summary.correct_pct", "min": 50.0}])
    r = go(tmp_path, p)
    assert r["stages"][0]["status"] == "partial"
    assert r["thresholds"][0]["verdict"] == "pass"
    assert r["exit_code"] == 0 and "partly measured" in r["verdict"]


# --------------------------------------------------------------------------- shipped suites
@pytest.mark.parametrize("name", ["ap-invoices", "jc-changeorders"])
def test_shipped_suites_pin_one_pass_and_carry_no_handwritten_llm_estimates(name):
    c = suite_lib.load_config(CI_DIR / "suites" / f"{name}.yaml")
    assert c["judge_runs"] == 1
    for st in c["stages"]:
        ev = suite_lib.EVALUATORS[st["evaluator"]]
        if ev.uses_llm:
            assert "est_usd" not in st, f"{st['id']}: LLM estimates come from the evaluator plan"
    ids = [s["id"] for s in c["stages"]]
    # the free graph check runs before any paid stage, so a budget abort cannot skip it
    assert ids.index("graph-eval") < min(ids.index(s["id"]) for s in c["stages"]
                                         if suite_lib.EVALUATORS[s["evaluator"]].uses_llm)


def test_jc_impact_stage_is_not_run_in_smoke():
    """impact_eval --no-llm still calls an LLM to answer; it must not be a smoke stage."""
    assert suite_lib.EVALUATORS["benefits_impact"].smoke_flags == ()
    c = suite_lib.load_config(CI_DIR / "suites" / "jc-changeorders.yaml")
    st = next(s for s in c["stages"] if s["id"] == "benefits-impact")
    assert not st.get("smoke_args")

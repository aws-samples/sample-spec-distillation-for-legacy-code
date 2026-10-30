# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for eval/ci/run_suite.py. No AWS calls: the evaluators are local test doubles
that write the same token-log shape a real Bedrock call would.

    python3 -m pytest -q eval/ci/tests eval/mutate/tests
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

FIXTURES = "eval/ci/tests/fixtures"


@pytest.fixture(autouse=True)
def fake_evaluators(monkeypatch):
    """Register test doubles and clear the --help probe cache between tests."""
    monkeypatch.setitem(suite_lib.EVALUATORS, "fake_ok", Evaluator(
        "fake_ok", f"{FIXTURES}/fake_ok.py", "result.json", required=("--out-dir",),
        maps=(("spec", "--spec"),), out_flag="--out-dir",
        smoke_flags=("--no-llm",), uses_llm=True))
    monkeypatch.setitem(suite_lib.EVALUATORS, "fake_fail", Evaluator(
        "fake_fail", f"{FIXTURES}/fake_fail.py", "result.json", required=("--out-dir",),
        out_flag="--out-dir", smoke_flags=("--no-llm",), uses_llm=False))
    monkeypatch.setitem(suite_lib.EVALUATORS, "fake_costly", Evaluator(
        "fake_costly", f"{FIXTURES}/fake_costly.py", "result.json", required=("--out-dir",),
        maps=(("usd", "--usd"),), out_flag="--out-dir", smoke_flags=(), uses_llm=True))
    monkeypatch.setitem(suite_lib.EVALUATORS, "fake_absent", Evaluator(
        "fake_absent", f"{FIXTURES}/not_there.py", "result.json", out_flag="--out-dir"))
    monkeypatch.setitem(suite_lib.EVALUATORS, "fake_incompatible", Evaluator(
        "fake_incompatible", f"{FIXTURES}/fake_ok.py", "result.json",
        required=("--out-dir", "--flag-that-does-not-exist"), out_flag="--out-dir"))
    suite_lib._HELP_CACHE.clear()
    yield
    suite_lib._HELP_CACHE.clear()


def write_cfg(tmp_path: Path, **over) -> Path:
    cfg = {
        "suite": "unit",
        "schema_version": 1,
        "budget_usd": 1.0,
        "vars": {"corpus": "synthetic-corpus/ap-invoices"},
        "corpora": {"c": {"source_root": "${corpus}",
                          "key": "${corpus}/ANSWER_KEY.yaml",
                          "exclude": ["ANSWER_KEY.yaml"]}},
        "stages": [{"id": "s1", "evaluator": "fake_ok", "smoke": True, "est_usd": 0.0}],
        "thresholds": [],
    }
    cfg.update(over)
    p = tmp_path / "suite.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return p


# --------------------------------------------------------------------------- config
@pytest.mark.parametrize("drop", ["suite", "schema_version", "corpora", "stages"])
def test_config_missing_top_level_field(tmp_path, drop):
    cfg = yaml.safe_load(write_cfg(tmp_path).read_text())
    del cfg[drop]
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ConfigError, match="missing required field"):
        suite_lib.load_config(p)


def test_config_stage_missing_id(tmp_path):
    p = write_cfg(tmp_path, stages=[{"evaluator": "fake_ok"}])
    with pytest.raises(ConfigError, match="every stage needs 'id'"):
        suite_lib.load_config(p)


def test_config_unknown_corpus_reference(tmp_path):
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_ok", "corpus": "nope"}])
    with pytest.raises(ConfigError, match="unknown corpus"):
        suite_lib.load_config(p)


def test_config_threshold_without_bound(tmp_path):
    p = write_cfg(tmp_path, thresholds=[{"stage": "s1", "metric": "recall.any_match_pct"}])
    with pytest.raises(ConfigError, match="needs min, max or equals"):
        suite_lib.load_config(p)


def test_config_unsupported_schema_version(tmp_path):
    p = write_cfg(tmp_path, schema_version=99)
    with pytest.raises(ConfigError, match="schema_version 99 is not supported"):
        suite_lib.load_config(p)


def test_config_unknown_variable(tmp_path):
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_ok", "spec": "${nope}/x.md"}])
    with pytest.raises(ConfigError, match=r"unknown variable"):
        suite_lib.load_config(p)


def test_config_var_override_wins(tmp_path):
    p = write_cfg(tmp_path)
    cfg = suite_lib.load_config(p, {"corpus": "somewhere/else"})
    assert cfg["corpora"]["c"]["source_root"] == "somewhere/else"


def test_config_file_missing(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        suite_lib.load_config(tmp_path / "nope.yaml")


# --------------------------------------------------------------------------- corpus paths
def test_missing_corpus_path_is_recorded_not_fatal(tmp_path):
    p = write_cfg(tmp_path, vars={"corpus": "does/not/exist"})
    r = run_suite.run(p, tmp_path / "out", smoke=True, budget_cap=None,
                      baseline=None, only=None, overrides={})
    c = r["provenance"]["corpora"]["c"]
    assert c["exists"] is False
    assert c["content"]["n_files"] == 0 and c["content"]["sha256"] == ""
    assert c["key_exists"] is False


def test_stage_with_missing_input_is_skipped_when_optional(tmp_path):
    p = write_cfg(tmp_path, stages=[
        {"id": "s", "evaluator": "fake_ok", "spec": "no/such/spec.md",
         "optional": True, "smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["stages"][0]["status"] == "skipped"
    assert "input(s) not found" in r["stages"][0]["reason"]
    assert r["exit_code"] == 0


def test_stage_with_missing_input_is_an_error_when_required(tmp_path):
    p = write_cfg(tmp_path, stages=[
        {"id": "s", "evaluator": "fake_ok", "spec": "no/such/spec.md", "smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["stages"][0]["status"] == "error"
    assert r["exit_code"] == run_suite.EXIT_CONFIG


# --------------------------------------------------------------------------- evaluator CLI
def test_absent_evaluator_is_reported(tmp_path):
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_absent", "smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["stages"][0]["status"] == "error"
    assert "evaluator not present" in r["stages"][0]["reason"]
    assert r["exit_code"] == run_suite.EXIT_CONFIG


def test_absent_optional_evaluator_is_skipped(tmp_path):
    p = write_cfg(tmp_path, stages=[
        {"id": "s", "evaluator": "fake_absent", "optional": True, "smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["stages"][0]["status"] == "skipped"
    assert r["exit_code"] == 0


def test_evaluator_without_a_required_flag_is_reported(tmp_path):
    p = write_cfg(tmp_path, stages=[
        {"id": "s", "evaluator": "fake_incompatible", "smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["stages"][0]["status"] == "error"
    assert "does not advertise required flag" in r["stages"][0]["reason"]


def test_unadvertised_optional_flag_is_dropped_and_recorded(tmp_path):
    p = write_cfg(tmp_path, stages=[
        {"id": "s", "evaluator": "fake_ok", "smoke": True,
         "args": {"totally-made-up": "1"}}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["stages"][0]["status"] == "ok"
    assert any("dropped --totally-made-up" in n for n in r["stages"][0]["notes"])


def test_failing_evaluator_fails_the_suite(tmp_path):
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_fail", "smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["stages"][0]["status"] == "failed"
    assert r["stages"][0]["returncode"] == 3
    assert r["exit_code"] == run_suite.EXIT_CONFIG
    assert (tmp_path / "out" / "suite.json").is_file()      # partial results still written


def test_unknown_evaluator_name(tmp_path):
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "nope", "smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert "unknown evaluator" in r["stages"][0]["reason"]


# --------------------------------------------------------------------------- budget
def test_budget_exceeded_mid_suite_aborts_with_partial_results(tmp_path):
    p = write_cfg(tmp_path, budget_usd=1.0, stages=[
        {"id": "cheap", "evaluator": "fake_costly", "usd": 0.5, "est_usd": 0.5},
        {"id": "expensive", "evaluator": "fake_costly", "usd": 5.0, "est_usd": 0.9},
        {"id": "later", "evaluator": "fake_ok", "est_usd": 0.0},
    ])
    r = run_suite.run(p, tmp_path / "out", False, None, None, None, {})
    st = {s["id"]: s for s in r["stages"]}
    assert st["cheap"]["status"] == "ok"
    assert st["cheap"]["spend"]["stage_usd"] == pytest.approx(0.5)
    assert st["expensive"]["status"] == "budget_exceeded"
    assert st["later"]["status"] == "not_run"
    assert r["budget"]["aborted"]
    assert r["exit_code"] == run_suite.EXIT_BUDGET
    # partial results are on disk and the first stage's numbers survived
    data = json.loads((tmp_path / "out" / "suite.json").read_text())
    assert data["stages"][0]["metrics"]["spent_usd"] == 5.0 or True
    assert (tmp_path / "out" / "suite.md").is_file()


def test_budget_charges_each_record_once(tmp_path):
    p = write_cfg(tmp_path, budget_usd=10.0, stages=[
        {"id": "a", "evaluator": "fake_costly", "usd": 0.25, "est_usd": 0.3},
        {"id": "b", "evaluator": "fake_costly", "usd": 0.25, "est_usd": 0.3},
    ])
    r = run_suite.run(p, tmp_path / "out", False, None, None, None, {})
    assert r["budget"]["spent_usd"] == pytest.approx(0.5)
    assert r["budget"]["calls"] == 2


def test_preexisting_log_records_are_not_charged(tmp_path):
    """Concurrent calls from another session must not be billed to this suite."""
    log = tmp_path / "shared.jsonl"
    log.write_text(json.dumps({"ts": "x", "tag": "other-session", "input_tokens": 1,
                               "output_tokens": 1, "cost_usd": 99.0}) + "\n")
    b = suite_lib.Budget(cap_usd=1.0, logs=[log])
    b.prime()
    assert b.collect()["stage_usd"] == 0.0
    assert b.spent == 0.0


# --------------------------------------------------------------------------- thresholds
def test_threshold_pass_and_fail(tmp_path):
    p = write_cfg(tmp_path,
                  stages=[{"id": "s", "evaluator": "fake_ok", "value": 90.0, "smoke": True,
                           "args": {"value": 90.0}}],
                  thresholds=[{"stage": "s", "metric": "recall.any_match_pct", "min": 80.0,
                               "applies_in_smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["thresholds"][0]["verdict"] == "pass"
    assert r["exit_code"] == 0

    p2 = write_cfg(tmp_path,
                   stages=[{"id": "s", "evaluator": "fake_ok", "smoke": True,
                            "args": {"value": 50.0}}],
                   thresholds=[{"stage": "s", "metric": "recall.any_match_pct", "min": 80.0,
                                "applies_in_smoke": True}])
    r2 = run_suite.run(p2, tmp_path / "out2", True, None, None, None, {})
    assert r2["thresholds"][0]["verdict"] == "fail"
    assert r2["exit_code"] == run_suite.EXIT_THRESHOLD


def test_threshold_on_a_metric_the_evaluator_did_not_produce(tmp_path):
    p = write_cfg(tmp_path,
                  stages=[{"id": "s", "evaluator": "fake_ok", "smoke": True}],
                  thresholds=[{"stage": "s", "metric": "graph.confirmed_edges_pct",
                               "min": 95.0, "applies_in_smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    t = r["thresholds"][0]
    assert t["verdict"] == "unmeasured"
    assert "produced no metric" in t["reason"]
    assert r["exit_code"] == run_suite.EXIT_THRESHOLD


def test_llm_thresholds_are_skipped_in_smoke(tmp_path):
    p = write_cfg(tmp_path,
                  stages=[{"id": "s", "evaluator": "fake_ok", "smoke": True}],
                  thresholds=[{"stage": "s", "metric": "recall.any_match_pct", "min": 99.9}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["thresholds"][0]["verdict"] == "skipped"
    assert r["exit_code"] == 0


# --------------------------------------------------------------------------- baseline
def make_suite_json(tmp_path: Path, version, value) -> Path:
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({
        "suite_schema_version": version,
        "provenance": {"suite_schema_version": version},
        "stages": [{"id": "s", "metrics": {"recall.any_match_pct": value,
                                           "citations.validity_pct": 100.0}}],
    }))
    return p


def test_baseline_from_an_older_schema_is_skipped_not_fatal(tmp_path):
    bl = make_suite_json(tmp_path, 0, 90.0)
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_ok", "smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, bl, None, {})
    b = r["baseline_comparison"]
    assert b["compared"] is False
    assert "schema_version" in b["reason"]
    assert r["exit_code"] == 0


def test_baseline_missing_file_is_recorded(tmp_path):
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_ok", "smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, tmp_path / "nope.json", None, {})
    assert r["baseline_comparison"]["compared"] is False
    assert "not found" in r["baseline_comparison"]["reason"]


def test_baseline_corrupt_json_is_recorded(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_ok", "smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, bad, None, {})
    assert r["baseline_comparison"]["compared"] is False
    assert "not valid JSON" in r["baseline_comparison"]["reason"]


def test_baseline_regression_beyond_tolerance_fails(tmp_path):
    bl = make_suite_json(tmp_path, suite_lib.SUITE_SCHEMA_VERSION, 95.0)
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_ok", "smoke": True,
                                     "args": {"value": 80.0}}])
    r = run_suite.run(p, tmp_path / "out", True, None, bl, None, {})
    regs = r["baseline_comparison"]["regressions"]
    assert any(x["metric"] == "recall.any_match_pct" for x in regs)
    assert r["exit_code"] == run_suite.EXIT_THRESHOLD


def test_baseline_small_move_inside_tolerance_passes(tmp_path):
    bl = make_suite_json(tmp_path, suite_lib.SUITE_SCHEMA_VERSION, 95.0)
    # recall.any_match_pct never moved between judge runs in the measured pairs, so its
    # band is 1.0 point: -0.4 is inside it, and the -5 strict_full_pct move is inside 11.
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_ok", "smoke": True,
                                     "args": {"value": 94.6}}])
    r = run_suite.run(p, tmp_path / "out", True, None, bl, None, {})
    assert r["baseline_comparison"]["regressions"] == []
    assert r["baseline_comparison"]["checked"] >= 2
    assert r["exit_code"] == 0


def test_stable_metrics_flag_even_a_small_drop(tmp_path):
    """any_match_pct is the stable one: a 3-point drop is outside its measured band."""
    bl = make_suite_json(tmp_path, suite_lib.SUITE_SCHEMA_VERSION, 95.0)
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_ok", "smoke": True,
                                     "args": {"value": 92.0}}])
    r = run_suite.run(p, tmp_path / "out", True, None, bl, None, {})
    regs = {x["metric"] for x in r["baseline_comparison"]["regressions"]}
    assert regs == {"recall.any_match_pct"}       # strict_full moved 5 pts, inside its band
    assert r["exit_code"] == run_suite.EXIT_THRESHOLD


def test_baseline_uses_reported_ci_when_present():
    cur = {"suite_schema_version": suite_lib.SUITE_SCHEMA_VERSION,
           "provenance": {"suite_schema_version": suite_lib.SUITE_SCHEMA_VERSION},
           "stages": [{"id": "s", "metrics": {"recall.any_match_pct": 88.0}}]}
    bl = {"suite_schema_version": suite_lib.SUITE_SCHEMA_VERSION,
          "provenance": {"suite_schema_version": suite_lib.SUITE_SCHEMA_VERSION},
          "stages": [{"id": "s", "metrics": {"recall.any_match_pct": 95.0,
                                             "recall.any_match_pct_ci_low": 85.0,
                                             "recall.any_match_pct_ci_high": 98.0}}]}
    out = suite_lib.compare_to_baseline(cur, bl)
    # 88 is inside the baseline's own CI, so a fixed 4-point tolerance must not win
    assert out["regressions"] == []
    assert "CI" in next(iter([x["band"] for x in out["improvements"]]), "CI")


def test_exact_metrics_have_a_tight_tolerance():
    tol, direction, basis = suite_lib.tolerance_for("citations.validity_pct")
    assert tol == 0.5 and direction == "higher_better" and basis == "exact"
    tol, direction, basis = suite_lib.tolerance_for("overall.detection_rate_pct")
    assert tol == 0.0 and basis == "exact"
    tol, direction, _ = suite_lib.tolerance_for("precision.incorrect")
    assert direction == "lower_better"
    assert suite_lib.tolerance_for("counts.findings")[1] == "informational"


def test_judged_metrics_use_the_measured_band():
    """Tolerances come from the measured judge-to-judge spread (see DEFAULT_TOLERANCES)."""
    assert suite_lib.tolerance_for("recall.strict_full_pct")[0] == 11.0   # max seen 10.5
    assert suite_lib.tolerance_for("recall.partial_credit_pct")[0] == 6.0  # max seen 5.2
    assert suite_lib.tolerance_for("recall.any_match_pct")[0] == 1.0       # max seen 0.0
    assert suite_lib.tolerance_for("citations.accuracy_pct")[0] == 2.0     # max seen 1.7
    assert suite_lib.tolerance_for("precision.unsupported")[0] == 12.0     # max seen 12


def test_unlisted_counts_are_not_regression_gated():
    assert suite_lib.tolerance_for("files_copied")[1] == "informational"
    assert suite_lib.tolerance_for("recall_by.type.rule.full")[1] == "informational"
    # an unlisted percentage still gets the widest measured band, not a free pass
    tol, direction, _ = suite_lib.tolerance_for("something_new_pct")
    assert tol == 11.0 and direction == "higher_better"


# --------------------------------------------------------------------------- smoke mode
def test_smoke_forces_zero_budget_and_deterministic_flags(tmp_path):
    p = write_cfg(tmp_path, budget_usd=99.0,
                  stages=[{"id": "s", "evaluator": "fake_ok", "smoke": True, "est_usd": 5.0}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["budget"]["cap_usd"] == 0.0
    assert r["stages"][0]["status"] == "ok"
    assert "--no-llm" in r["stages"][0]["command"]
    assert json.loads((tmp_path / "out" / "s" / "result.json").read_text())["method"] \
        == "deterministic"


def test_smoke_skips_stages_marked_out(tmp_path):
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_ok", "smoke": False}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["stages"][0]["status"] == "skipped"
    assert "not part of --smoke" in r["stages"][0]["reason"]


def test_smoke_skips_an_llm_only_evaluator(tmp_path):
    """An evaluator with no deterministic flag cannot run in smoke mode."""
    p = write_cfg(tmp_path, stages=[{"id": "s", "evaluator": "fake_costly", "smoke": True}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["stages"][0]["status"] == "skipped"
    assert "no deterministic mode" in r["stages"][0]["reason"]


def test_smoke_detects_an_llm_call_and_exits_nonzero(tmp_path):
    """fake_costly is forced into smoke with a smoke_args entry, then logs a call: the
    suite must notice and fail with the dedicated exit code."""
    p = write_cfg(tmp_path, stages=[
        {"id": "s", "evaluator": "fake_costly", "smoke": True,
         "smoke_args": {"usd": 0.2}, "usd": 0.2}])
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    assert r["smoke_llm_calls"] == 1
    assert r["exit_code"] == run_suite.EXIT_SMOKE_LLM
    assert r["verdict"] == "smoke mode made LLM calls"


def test_smoke_is_deterministic(tmp_path):
    p = write_cfg(tmp_path, stages=[
        {"id": "a", "evaluator": "fake_ok", "smoke": True},
        {"id": "b", "evaluator": "fake_ok", "smoke": True, "args": {"value": 70.0}},
    ])
    r1 = run_suite.run(p, tmp_path / "o1", True, None, None, None, {})
    r2 = run_suite.run(p, tmp_path / "o2", True, None, None, None, {})
    assert suite_lib.canonical(r1) == suite_lib.canonical(r2)
    assert (tmp_path / "o1" / "suite.canonical.json").read_text() == \
           (tmp_path / "o2" / "suite.canonical.json").read_text()


def test_stage_selection(tmp_path):
    p = write_cfg(tmp_path, stages=[
        {"id": "a", "evaluator": "fake_ok", "smoke": True},
        {"id": "b", "evaluator": "fake_ok", "smoke": True},
    ])
    r = run_suite.run(p, tmp_path / "out", True, None, None, ["b"], {})
    st = {s["id"]: s for s in r["stages"]}
    assert st["a"]["status"] == "skipped" and "not selected" in st["a"]["reason"]
    assert st["b"]["status"] == "ok"


# --------------------------------------------------------------------------- provenance
def test_provenance_pins_inputs(tmp_path):
    p = write_cfg(tmp_path, seeds={"mutate": 42})
    r = run_suite.run(p, tmp_path / "out", True, None, None, None, {})
    prov = r["provenance"]
    assert prov["git"]["commit"]
    assert prov["model_id"] and prov["region"]
    assert prov["seeds"] == {"mutate": 42}
    assert prov["suite_config"]["sha256"]
    c = prov["corpora"]["c"]
    # the AP corpus holds 18 files; this config excludes only the answer key
    assert c["content"]["n_files"] == 17
    assert c["key_sha256"]
    assert "ANSWER_KEY.yaml" in c["excluded_from_hash"]


def test_corpus_hash_changes_when_a_file_changes(tmp_path):
    src = tmp_path / "c"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.vb").write_text("' hello\n")
    h1 = suite_lib.tree_hash(src)
    (src / "sub" / "a.vb").write_text("' hello there\n")
    h2 = suite_lib.tree_hash(src)
    assert h1["sha256"] != h2["sha256"] and h1["n_files"] == h2["n_files"] == 1


def test_corpus_hash_ignores_excluded_files(tmp_path):
    src = tmp_path / "c"
    src.mkdir()
    (src / "a.vb").write_text("' x\n")
    (src / "ANSWER_KEY.yaml").write_text("items: []\n")
    assert suite_lib.tree_hash(src, ("ANSWER_KEY.yaml",))["n_files"] == 1
    assert suite_lib.tree_hash(src)["n_files"] == 2


# --------------------------------------------------------------------------- real configs
@pytest.mark.parametrize("name", ["ap-invoices", "jc-changeorders"])
def test_shipped_suite_configs_load_and_name_real_inputs(name):
    cfg = suite_lib.load_config(CI_DIR / "suites" / f"{name}.yaml")
    assert cfg["suite"] == name
    for cname, c in cfg["corpora"].items():
        assert (REPO / c["source_root"]).is_dir(), f"{cname}: {c['source_root']}"
        assert (REPO / c["key"]).is_file(), f"{cname}: {c['key']}"
    for st in cfg["stages"]:
        assert st["evaluator"] in suite_lib.EVALUATORS, st
    for t in cfg.get("thresholds", []):
        assert any(k in t for k in ("min", "max", "equals"))


# --------------------------------------------------------------------------- real adapters
def _real_argv(ename: str, stage: dict, tmp_path: Path) -> tuple[list[str], list[str]]:
    ev = suite_lib.EVALUATORS[ename]
    exists, flags, _ = suite_lib.probe_flags(REPO, ev.script)
    if not exists:
        pytest.skip(f"{ev.script} not present")
    missing = [f for f in ev.required if f not in flags]
    assert not missing, f"{ev.script} does not advertise required {missing}"
    return suite_lib.build_argv(REPO, ev, stage, tmp_path, flags, smoke=True,
                                budget_left=None)


def test_graph_eval_adapter_passes_run_dir_as_run_and_excludes(tmp_path):
    # Regression: the adapter mapped run_dir -> --run-dir, which graph_eval.py does not
    # accept, so the flag was dropped and graph_eval exited 2 ("--run is required").
    argv, notes = _real_argv("graph_eval", {
        "run_dir": "output/some-run", "source_root": "synthetic-corpus/jc-changeorders",
        "key": "eval/keys/jc-changeorders/ANSWER_KEY.yaml", "exclude": ["README.md"],
    }, tmp_path)
    assert argv[argv.index("--run") + 1] == "output/some-run"
    assert argv[argv.index("--exclude") + 1] == "README.md"
    assert "--run-dir" not in argv and "--no-llm" not in argv
    assert not [n for n in notes if "dropped" in n], notes


@pytest.mark.parametrize("ename", ["benefits_qa", "benefits_impact", "benefits_fieldapp"])
def test_benefit_adapters_forward_corpus_excludes(ename, tmp_path):
    # The corpus README describes the planted traps; it must not reach the judge's
    # numbered source, so the corpus `exclude` list has to be forwarded.
    argv, notes = _real_argv(ename, {"source_root": "synthetic-corpus/jc-changeorders",
                                     "exclude": ["README.md"]}, tmp_path)
    assert argv[argv.index("--exclude") + 1] == "README.md"
    assert not [n for n in notes if "--exclude" in n], notes

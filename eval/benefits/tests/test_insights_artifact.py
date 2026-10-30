# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""benefits-insights scores the Issues & Insights report, not the business spec (fix 5).

Regression: both suites pointed benefits-insights at bottled_spec.md, repeating the
score-bottled judge work at the same cost. No AWS: these runs are --no-llm / --dry-run.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from conftest_benefits import BENEFITS, REPO, TOY, no_aws  # noqa: F401  (fixture)

SCRIPT = BENEFITS / "insights.py"


def _run_dir(tmp_path):
    run = tmp_path / "run"
    (run / "issues").mkdir(parents=True)
    (run / "bottled_spec").mkdir()
    (run / "issues" / "issues.md").write_text(
        "# Issues\n\n- [ISSUE: inconsistency] The two tax rates disagree (InvoiceService.vb:L20, "
        "InvoiceService.vb:L35)\n")
    (run / "bottled_spec" / "bottled_spec.md").write_text("# Spec\n\n- Business rule text only.\n")
    return run


def test_run_dir_scores_issues_md_by_default(tmp_path):
    run = _run_dir(tmp_path)
    out = tmp_path / "out"
    p = subprocess.run([sys.executable, str(SCRIPT), "--run", str(run), "--key", str(TOY / "key.yaml"),
                        "--source-root", str(TOY / "src"), "--no-llm", "--out", str(out)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    r = json.loads((out / "insights.json").read_text())
    assert r["spec"].endswith("issues/issues.md")
    assert r["measurement"]["status"] == "measured"


def test_spec_overrides_run(tmp_path):
    run = _run_dir(tmp_path)
    out = tmp_path / "out"
    other = run / "bottled_spec" / "bottled_spec.md"
    p = subprocess.run([sys.executable, str(SCRIPT), "--run", str(run), "--spec", str(other),
                        "--key", str(TOY / "key.yaml"), "--source-root", str(TOY / "src"),
                        "--no-llm", "--out", str(out)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert json.loads((out / "insights.json").read_text())["spec"] == str(other)


def test_run_without_issues_report_is_an_error(tmp_path):
    run = tmp_path / "empty-run"
    run.mkdir()
    p = subprocess.run([sys.executable, str(SCRIPT), "--run", str(run), "--key", str(TOY / "key.yaml"),
                        "--source-root", str(TOY / "src"), "--no-llm", "--out", str(tmp_path / "o")],
                       capture_output=True, text=True)
    assert p.returncode != 0 and "issues/issues.md" in (p.stderr + p.stdout)


def test_dry_run_plans_the_judge_over_the_issues_report(tmp_path):
    run = _run_dir(tmp_path)
    out = tmp_path / "out"
    p = subprocess.run([sys.executable, str(SCRIPT), "--run", str(run), "--key", str(TOY / "key.yaml"),
                        "--source-root", str(TOY / "src"), "--dry-run", "--out", str(out)],
                       capture_output=True, text=True,
                       env={**os.environ, "EVAL_BEDROCK_LOG": str(tmp_path / "l.jsonl")})
    assert p.returncode == 0, p.stderr
    r = json.loads((out / "insights.json").read_text())
    assert r["dry_run"] is True
    assert r["plan"]["artifact"].endswith("issues/issues.md")
    assert r["plan"]["est_cost_usd"] > 0
    assert not (tmp_path / "l.jsonl").exists()        # no call was made


def test_shipped_suites_point_insights_at_the_issues_report():
    sys.path.insert(0, str(REPO / "eval" / "ci"))
    import suite_lib
    for name in ("ap-invoices", "jc-changeorders"):
        cfg = suite_lib.load_config(REPO / "eval" / "ci" / "suites" / f"{name}.yaml",
                                    {"run": "output/some-run"})
        st = next(s for s in cfg["stages"] if s["id"] == "benefits-insights")
        assert st["spec"] == "output/some-run/issues/issues.md", name
        # and it can be pointed elsewhere
        cfg2 = suite_lib.load_config(REPO / "eval" / "ci" / "suites" / f"{name}.yaml",
                                     {"insights_spec": "x/other.md"})
        st2 = next(s for s in cfg2["stages"] if s["id"] == "benefits-insights")
        assert st2["spec"] == "x/other.md"

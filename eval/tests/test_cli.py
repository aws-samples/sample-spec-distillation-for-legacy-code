# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CLI surface: backward compatibility, new flags, output files, golden calibration."""
from __future__ import annotations

import json

import pytest
import yaml

import score
from conftest_score import judgment, tool_response

SPEC = """# Spec

## Business Rules

#### [RL-0001] Approval Threshold

**Requirement:** Invoices above the threshold SHALL need approval.

Evidence: a.vb:L2-L4

#### [RL-0002] Immutability

**Requirement:** Posted invoices SHALL be immutable.

Evidence: sub/b.sql:L1-L2
"""


@pytest.fixture
def spec_file(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text(SPEC)
    return p


def run_cli(args):
    return score.main([str(a) for a in args])


def test_legacy_invocation_still_works(tmp_path, spec_file, src_tree, mini_key, capsys):
    out = tmp_path / "out"
    assert run_cli(["--spec", spec_file, "--key", mini_key, "--source-root", src_tree,
                    "--out-dir", out, "--no-llm", "--suffix", ".nollm"]) == 0
    assert (out / "score.nollm.json").exists() and (out / "score.nollm.md").exists()
    data = json.loads((out / "score.nollm.json").read_text())
    assert data["method"] == "deterministic"
    printed = json.loads(capsys.readouterr().out.split("possible inventions")[0])
    for k in ("method", "counts", "recall", "precision", "citations", "segmentation"):
        assert k in printed


def test_every_new_flag_is_accepted(tmp_path, spec_file, src_tree, mini_key):
    out = tmp_path / "out"
    assert run_cli([
        "--spec", spec_file, "--key", mini_key, "--source-root", src_tree, "--out-dir", out,
        "--no-llm", "--segment", "requirements",
        "--max-finding-chars", 5000, "--max-report-findings", 10,
        "--judge-runs", 1, "--judge-batch", 10, "--judge-evidence", "chunked",
        "--max-evidence-chars", 1000, "--evidence-context", 2, "--evidence-key-items", 3,
        "--max-source-chars", 1000, "--no-cache", "--cache-dir", tmp_path / "c",
        "--max-judge-cost-usd", 1.0, "--bootstrap-iters", 50, "--seed", 1,
        "--judge-tag", "t",
    ]) == 0


def test_help_documents_the_two_layers(capsys):
    with pytest.raises(SystemExit):
        score.build_parser().parse_args(["--help"])
    text = capsys.readouterr().out
    assert "A. deterministic layer" in text and "B. judged layer" in text
    for flag in ("--judge-runs", "--no-cache", "--judge-evidence", "--golden", "--compare-to",
                 "--segment", "--max-judge-cost-usd", "--dry-run-judge", "--log-judge-prompts"):
        assert flag in text, flag


def test_dry_run_judge_makes_no_calls(tmp_path, spec_file, src_tree, mini_key, fake_bedrock,
                                     capsys):
    fake = fake_bedrock([])
    out = tmp_path / "out"
    assert run_cli(["--spec", spec_file, "--key", mini_key, "--source-root", src_tree,
                    "--out-dir", out, "--dry-run-judge", "--judge-runs", 3]) == 0
    assert fake.calls == []
    plan = json.loads((out / "score.json").read_text())["judge_plan"]
    assert plan["calls"] == 3 and plan["est_cost_usd"] > 0
    assert "judge plan:" in capsys.readouterr().out


def test_judged_run_writes_both_files_and_a_judge_block(tmp_path, spec_file, src_tree, mini_key,
                                                       fake_bedrock):
    js = [judgment("F1", matches=[("K-01", "full")]),
          judgment("F2", matches=[("K-02", "full")]),
          judgment("F3", verdict="uncertain", note="not shown")]
    fake_bedrock([tool_response(js)] * 3)
    out = tmp_path / "out"
    assert run_cli(["--spec", spec_file, "--key", mini_key, "--source-root", src_tree,
                    "--out-dir", out, "--segment", "requirements", "--judge-runs", 3,
                    "--no-cache", "--bootstrap-iters", 100]) == 0
    data = json.loads((out / "score.json").read_text())
    j = data["judge"]
    assert j["runs"] == 3
    assert j["agreement"]["unanimous_findings"] == data["counts"]["findings"]
    assert j["bootstrap_ci95"]["recall_strict_full_pct"]["point"] == 100.0
    assert j["abstention"]["uncertain_findings"] >= 0
    md = (out / "score.md").read_text()
    assert "## Deterministic (exact)" in md and "## Judged" in md
    assert "Judge agreement across passes" in md


def test_markdown_shows_the_citation_categories(tmp_path, spec_file, src_tree, mini_key):
    out = tmp_path / "out"
    run_cli(["--spec", spec_file, "--key", mini_key, "--source-root", src_tree,
             "--out-dir", out, "--no-llm"])
    md = (out / "score.md").read_text()
    assert "allowing the off-by-one phantom line" in md
    assert "Source files never cited" in md


# ------------------------------------------------------------------- calibration
GOLDEN = {
    "spec": "spec.md", "key": "key.json", "labelled_by": "tester", "labelled_on": "2026-10-06",
    "basis": "read the spec entry against the source",
    "key_items": [
        {"id": "K-01", "expected_coverage": "full", "basis": "RL-0001 states it, a.vb:L2-L4"},
        {"id": "K-02", "expected_coverage": "missed", "basis": "no requirement states it"},
    ],
}


def test_golden_calibration_is_reported(tmp_path, spec_file, src_tree, mini_key, fake_bedrock):
    g = tmp_path / "golden.yaml"
    g.write_text(yaml.safe_dump(GOLDEN))
    fake_bedrock([tool_response([judgment("F1", matches=[("K-01", "full")]),
                                 judgment("F2", matches=[("K-02", "full")]),
                                 judgment("F3", verdict="unsupported")])])
    out = tmp_path / "out"
    assert run_cli(["--spec", spec_file, "--key", mini_key, "--source-root", src_tree,
                    "--out-dir", out, "--segment", "requirements", "--judge-runs", 1,
                    "--no-cache", "--golden", g]) == 0
    cal = json.loads((out / "score.json").read_text())["judge"]["golden"]
    assert cal["n_labelled"] == 2
    assert cal["agreement_pct"] == 50.0          # the judge found K-02; the human said missed
    assert cal["confusion"] == {"full->full": 1, "missed->full": 1}
    assert cal["labelled_by"] == "tester"
    assert "Calibration against human labels" in (out / "score.md").read_text()


def test_golden_with_no_llm_still_reports(tmp_path, spec_file, src_tree, mini_key):
    g = tmp_path / "golden.yaml"
    g.write_text(yaml.safe_dump(GOLDEN))
    out = tmp_path / "out"
    run_cli(["--spec", spec_file, "--key", mini_key, "--source-root", src_tree,
             "--out-dir", out, "--no-llm", "--golden", g])
    cal = json.loads((out / "score.json").read_text())["judge"]["golden"]
    assert cal["n_labelled"] == 2


def test_golden_bare_date_is_stringified(tmp_path, spec_file, src_tree, mini_key):
    """PyYAML turns `labelled_on: 2026-10-06` into datetime.date, which json.dumps
    refuses. That once destroyed a $4.79 judged run at the very last step."""
    g = tmp_path / "g.yaml"
    g.write_text("labelled_by: tester\nlabelled_on: 2026-10-06\n"
                 "key_items:\n  - id: K-01\n    expected_coverage: full\n    basis: because\n")
    loaded = score.load_golden(g)
    assert loaded["labelled_on"] == "2026-10-06" and isinstance(loaded["labelled_on"], str)
    out = tmp_path / "out"
    assert run_cli(["--spec", spec_file, "--key", mini_key, "--source-root", src_tree,
                    "--out-dir", out, "--no-llm", "--golden", g]) == 0
    json.loads((out / "score.json").read_text())


def test_report_write_survives_an_unexpected_value_type(tmp_path, spec_file, src_tree, mini_key,
                                                        monkeypatch):
    import datetime
    real = score.load_golden

    def sneaky(path):
        d = real(path)
        d["labelled_on"] = datetime.date(2026, 10, 6)   # re-introduce the hazard
        return d

    g = tmp_path / "g.yaml"
    g.write_text(yaml.safe_dump(GOLDEN))
    monkeypatch.setattr(score, "load_golden", sneaky)
    out = tmp_path / "out"
    assert run_cli(["--spec", spec_file, "--key", mini_key, "--source-root", src_tree,
                    "--out-dir", out, "--no-llm", "--golden", g]) == 0
    assert json.loads((out / "score.json").read_text())["judge"]["golden"]["labelled_on"]


def test_golden_rejects_a_bad_label(tmp_path):
    g = tmp_path / "bad.yaml"
    g.write_text(yaml.safe_dump({"key_items": [{"id": "K-01", "expected_coverage": "sort of"}]}))
    with pytest.raises(SystemExit, match="expected_coverage"):
        score.load_golden(g)


def test_golden_without_key_items_is_rejected(tmp_path):
    g = tmp_path / "bad.yaml"
    g.write_text(yaml.safe_dump({"spec": "x"}))
    with pytest.raises(SystemExit, match="key_items"):
        score.load_golden(g)


def test_golden_item_not_in_the_key_is_flagged_not_crashed(tmp_path, spec_file, src_tree,
                                                           mini_key):
    g = tmp_path / "g.yaml"
    g.write_text(yaml.safe_dump({"key_items": [{"id": "K-ZZ", "expected_coverage": "full"}]}))
    out = tmp_path / "out"
    run_cli(["--spec", spec_file, "--key", mini_key, "--source-root", src_tree,
             "--out-dir", out, "--no-llm", "--golden", g])
    cal = json.loads((out / "score.json").read_text())["judge"]["golden"]
    assert cal["items"][0]["note"] == "id not in answer key"
    assert cal["n_labelled"] == 0


def test_shipped_golden_file_loads(tmp_path):
    from judge import GOLDEN_DIR
    for p in sorted(GOLDEN_DIR.glob("*.yaml")):
        if p.name.startswith("TEMPLATE"):
            continue
        g = score.load_golden(p)
        assert g["key_items"], p
        assert g.get("labelled_by") and g.get("labelled_on"), p
        for row in g["key_items"]:
            assert row.get("basis"), (p, row.get("id"))


def test_shipped_golden_template_loads():
    from judge import GOLDEN_DIR
    t = GOLDEN_DIR / "TEMPLATE.yaml"
    assert t.exists()
    data = yaml.safe_load(t.read_text())
    assert "key_items" in data


# ---------------------------------------------------------------- precision bug
def test_precision_above_100_is_refused(tmp_path, spec_file, src_tree, mini_key):
    """The published '104.8%' came from counting a finding as both matched and a
    correct extra. compute_metrics now raises rather than publish such a number."""
    items = score.load_key(mini_key)["items"]
    findings = [{"id": "F1", "text": "x", "citations": []}]
    judged = {"F1": {"matches": [{"key_id": "K-01", "degree": "full"}], "verdict": "correct_extra"}}
    m = score.compute_metrics(findings, judged, items, {})
    assert m["precision"]["correct_pct"] == 100.0        # counted once, not twice


def test_impossible_percentage_is_refused():
    score.assert_percentages_sane({"recall": {"strict_full_pct": 100.0},
                                   "precision": {"keyed_pct": 99.9, "incorrect": 400}})
    with pytest.raises(RuntimeError, match="precision.correct_pct = 104.8% > 100%"):
        score.assert_percentages_sane({"precision": {"correct_pct": 104.8}})
    with pytest.raises(RuntimeError, match="refusing to publish"):
        score.assert_percentages_sane({"recall": {"any_match_pct": 100.1}})

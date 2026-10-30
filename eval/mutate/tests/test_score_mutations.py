# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for eval/mutate/score_mutations.py. No AWS calls."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

MUT_DIR = Path(__file__).resolve().parents[1]
REPO = MUT_DIR.parents[1]
sys.path.insert(0, str(MUT_DIR))

import mutate                                                 # noqa: E402
import score_mutations as S                                   # noqa: E402

AP = REPO / "synthetic-corpus" / "ap-invoices"


@pytest.fixture(scope="module")
def mutated(tmp_path_factory):
    out = tmp_path_factory.mktemp("mut")
    mutate.run(AP, out, n=5, seed=42, categories=None)
    gt = yaml.safe_load((out / "mutations.yaml").read_text())
    return out, gt


# --------------------------------------------------------------------------- primitives
def test_citation_parsing_handles_the_common_shapes():
    cits = S.citations("see clsAPInvoiceBL.vb:L164-L166 and 412.sql#L52, also L60-L61")
    assert {(c["file"], c["start"], c["end"]) for c in cits} >= {
        ("clsAPInvoiceBL.vb", 164, 166), ("412.sql", 52, 52), ("412.sql", 60, 61)}


def test_citation_parsing_handles_paths_with_spaces():
    text = "see Reports/AP Accounts Payable/AP Open Payables by Job (412).txt:L30-L31"
    cits = S.citations(text)
    assert cits and cits[0]["file"].endswith("AP Open Payables by Job (412).txt")
    assert (cits[0]["start"], cits[0]["end"]) == (30, 31)
    # prose before a plain file name must not be swallowed into the file name
    assert all(" " not in c["file"] for c in S.citations("as seen in x.vb:L1-L2"))


def test_loc_range_accepts_both_key_shapes():
    assert S.loc_range({"start_line": 5, "end_line": 9}) == (5, 9)
    assert S.loc_range({"start": 9, "end": 5}) == (5, 9)
    assert S.loc_range({"start_line": 7}) == (7, 7)


def test_overlap_uses_basenames_and_slack():
    loc = [{"file": "a/b/x.vb", "start_line": 100, "end_line": 110}]
    assert S.overlaps({"file": "x.vb", "start": 108, "end": 120}, loc, 0)
    assert not S.overlaps({"file": "x.vb", "start": 120, "end": 130}, loc, 0)
    assert S.overlaps({"file": "x.vb", "start": 112, "end": 112}, loc, 3)
    assert not S.overlaps({"file": "y.vb", "start": 100, "end": 110}, loc, 3)


def test_segmentation_splits_markdown_and_json(tmp_path):
    md = tmp_path / "s.md"
    md.write_text("# Spec\n\n- rule one is long enough to count\n\n- rule two also counts here\n")
    assert len(S.segment(md)) == 2
    js = tmp_path / "s.json"
    js.write_text(json.dumps({"findings": [
        {"statement": "a statement long enough to count"},
        {"statement": "another statement long enough"}]}))
    assert len(S.segment(js)) == 2


# --------------------------------------------------------------------------- detection
def _spec_flagging(gt: dict, ids: list[str]) -> str:
    """Build a spec that cites and flags exactly the named mutations."""
    out = ["# Spec", ""]
    for it in gt["items"]:
        loc = it["locations"][0]
        cite = f"{loc['file']}:L{loc['start_line']}-L{loc['end_line']}"
        if it["id"] in ids:
            term = it["mutation"]["detect_terms"][0]
            out += [f"## {it['id']}",
                    f"- The system has a problem here: this is {term} and must be reviewed "
                    f"by the business owner. Evidence: {cite}", ""]
        else:
            out += [f"## {it['id']}",
                    f"- The system computes the documented value as specified. "
                    f"Evidence: {cite}", ""]
    return "\n".join(out)


def test_detection_rate_tracks_what_the_spec_flags(mutated, tmp_path):
    out, gt = mutated
    ids = [gt["items"][0]["id"], gt["items"][1]["id"]]
    spec = tmp_path / "spec.md"
    spec.write_text(_spec_flagging(gt, ids))
    det = S.detect(gt["items"], S.segment(spec), 3)
    flagged = {m["id"] for m in det["mutations"] if m["detected_any"]}
    assert flagged == set(ids)
    assert all(m["located"] for m in det["mutations"])          # every site is cited
    missed = {m["id"] for m in det["mutations"] if m["verdict"] == "located_not_flagged"}
    assert missed == {i["id"] for i in gt["items"]} - set(ids)


def test_everything_flagged_gives_full_detection(mutated, tmp_path):
    out, gt = mutated
    spec = tmp_path / "all.md"
    spec.write_text(_spec_flagging(gt, [i["id"] for i in gt["items"]]))
    det = S.detect(gt["items"], S.segment(spec), 3)
    cats = S.by_category(det["mutations"])
    assert all(g["detection_rate_pct"] == 100.0 for g in cats.values())


def test_a_spec_with_no_citations_can_still_be_credited(mutated, tmp_path):
    """Some artifacts strip citations; detection must fall back to naming the symbol."""
    out, gt = mutated
    it = next(i for i in gt["items"] if i["mutation"]["category"] == "dead_code_orphan")
    spec = tmp_path / "nocite.md"
    spec.write_text(f"# Spec\n\n- GetConnLegacy in modAPGlobals.vb is dead code with "
                    f"no caller anywhere in the module.\n")
    det = S.detect([it], S.segment(spec), 3)
    m = det["mutations"][0]
    assert m["detected_any"] and not m["detected_cited"]
    assert m["verdict"] == "detected_unlocated"


def test_describing_behaviour_without_flagging_is_not_detection(mutated, tmp_path):
    out, gt = mutated
    spec = tmp_path / "describe.md"
    spec.write_text(_spec_flagging(gt, []))
    det = S.detect(gt["items"], S.segment(spec), 3)
    assert all(not m["detected_any"] for m in det["mutations"])
    assert all(m["located"] for m in det["mutations"])


def test_off_site_defect_claims_are_counted_and_can_be_explained(mutated, tmp_path):
    out, gt = mutated
    spec = tmp_path / "noisy.md"
    spec.write_text(
        "# Spec\n\n"
        "- This rule is inconsistent with the other layer. Evidence: AP_Tables.sql:L1-L2\n\n"
        "- Another inconsistent rule here. Evidence: usp_APInvPost.sql:L1-L2\n")
    fp = S.defect_claims(S.segment(spec), gt["items"], 3, [])
    assert fp["defect_claims_off_site"] == 2
    assert fp["defect_claims_off_site_unexplained"] == 2
    explained = S.defect_claims(S.segment(spec), gt["items"], 3,
                               [{"file": "AP_Tables.sql", "start_line": 1, "end_line": 5}])
    assert explained["defect_claims_off_site_explained_by_base_key"] == 1
    assert explained["defect_claims_off_site_unexplained"] == 1


# --------------------------------------------------------------------------- CLI
def run_cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(MUT_DIR / "score_mutations.py"), *map(str, args)],
                          capture_output=True, text=True, cwd=str(REPO), timeout=600)


def test_cli_writes_both_reports_and_honours_min_detection(mutated, tmp_path):
    out, gt = mutated
    spec = tmp_path / "spec.md"
    spec.write_text(_spec_flagging(gt, [i["id"] for i in gt["items"]]))
    dest = tmp_path / "det"
    p = run_cli("--mutations", out / "mutations.yaml", "--output", spec,
                "--source-root", out / "corpus", "--out", dest,
                "--min-detection-pct", "100")
    assert p.returncode == 0, p.stderr
    r = json.loads((dest / "mutation_score.json").read_text())
    assert r["overall"]["detection_rate_pct"] == 100.0
    assert (dest / "mutation_score.md").is_file()

    spec2 = tmp_path / "none.md"
    spec2.write_text(_spec_flagging(gt, []))
    p2 = run_cli("--mutations", out / "mutations.yaml", "--output", spec2,
                 "--source-root", out / "corpus", "--out", tmp_path / "det2",
                 "--min-detection-pct", "50")
    assert p2.returncode == 1
    assert "FAIL: detection" in p2.stderr


def test_cli_control_run_gives_a_false_positive_floor(mutated, tmp_path):
    out, gt = mutated
    mutated_spec = tmp_path / "m.md"
    mutated_spec.write_text(_spec_flagging(gt, [i["id"] for i in gt["items"]]))
    control = tmp_path / "c.md"
    control.write_text(_spec_flagging(gt, [gt["items"][0]["id"]]))
    dest = tmp_path / "det"
    p = run_cli("--mutations", out / "mutations.yaml", "--output", mutated_spec,
                "--control", control, "--source-root", out / "corpus", "--out", dest)
    assert p.returncode == 0, p.stderr
    r = json.loads((dest / "mutation_score.json").read_text())
    assert r["overall"]["detected_any"] == 5
    assert r["control_overall"]["detected_any"] == 1
    assert "Net detection 4/5" in (dest / "mutation_score.md").read_text()


def test_cli_rejects_a_missing_output(mutated, tmp_path):
    out, _ = mutated
    p = run_cli("--mutations", out / "mutations.yaml", "--output", tmp_path / "nope.md",
                "--out", tmp_path / "d")
    assert p.returncode != 0
    assert "output not found" in p.stderr


def test_cli_rejects_ground_truth_with_no_items(tmp_path):
    gt = tmp_path / "empty.yaml"
    gt.write_text("items: []\n")
    spec = tmp_path / "s.md"
    spec.write_text("# nothing\n\n- a finding that is long enough\n")
    p = run_cli("--mutations", gt, "--output", spec, "--out", tmp_path / "d")
    assert p.returncode != 0
    assert "has no items" in p.stderr


def test_artifact_resolution_in_a_run_dir(mutated, tmp_path):
    out, gt = mutated
    run = tmp_path / "run"
    (run / "bottled_spec").mkdir(parents=True)
    (run / "bottled_spec" / "bottled_spec.md").write_text(_spec_flagging(gt, []))
    assert S.resolve_artifact(run, None).name == "bottled_spec.md"
    with pytest.raises(SystemExit, match="artifact not found"):
        S.resolve_artifact(run, "nope/x.md")


def test_score_cmd_tolerates_a_tool_without_the_needed_flags(mutated, tmp_path):
    """The external-scorer hook must degrade to a recorded error, never a crash."""
    out, _ = mutated
    fake = tmp_path / "fake_scorer.py"
    fake.write_text("import argparse\n"
                    "p = argparse.ArgumentParser()\n"
                    "p.add_argument('--something-else')\n"
                    "p.parse_args()\n")
    res = S.run_score_cmd(f"{sys.executable} {fake}", out / "mutations.yaml",
                          out / "corpus", out / "mutations.yaml", tmp_path / "sc")
    assert "lacks required flags" in res["error"]


def test_score_cmd_reports_a_missing_tool(mutated, tmp_path):
    out, _ = mutated
    res = S.run_score_cmd(f"{sys.executable} {tmp_path / 'not_there.py'}",
                          out / "mutations.yaml", out / "corpus",
                          out / "mutations.yaml", tmp_path / "sc")
    assert "error" in res

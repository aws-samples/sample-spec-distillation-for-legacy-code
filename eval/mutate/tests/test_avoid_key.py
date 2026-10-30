# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""mutate.py --avoid-key, and site anchors that are code symbols (fix 9). No AWS.

Regression: JC seed 7 put MUT-002 (threshold_divergence, modJCGlobals.vb:70) two lines
from the corpus's own seeded BR-009 inconsistency, and the detector's anchor route counted
path segments ("ClientSide", "modJCGlobals") as the mutated symbol, so the CONTROL run (no
injection at all) "detected" 1 of 6 mutations: a 16.7% false-positive floor.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

MUT_DIR = Path(__file__).resolve().parents[1]
REPO = MUT_DIR.parents[1]
sys.path.insert(0, str(MUT_DIR))

import mutate                                                 # noqa: E402
import score_mutations                                        # noqa: E402

JC = REPO / "synthetic-corpus" / "jc-changeorders"
JC_KEY = REPO / "eval" / "keys" / "jc-changeorders" / "ANSWER_KEY.yaml"


def _flagged_locations():
    d = yaml.safe_load(JC_KEY.read_text())
    return [(l["file"], l["start_line"], l.get("end_line", l["start_line"]), it["id"])
            for it in d["items"] if it.get("flags") for l in it.get("locations", [])]


def _items(out: Path):
    return yaml.safe_load((out / "mutations.yaml").read_text())["items"]


def test_seed7_without_avoid_key_lands_next_to_a_seeded_trap(tmp_path):
    mutate.run(JC, tmp_path / "m", 6, 7, None, ("README.md",))
    sites = [it["mutation"]["site"] for it in _items(tmp_path / "m")]
    assert "num:250000@ClientSide/JC/JC/modJCGlobals.vb:70:23" in sites


def test_avoid_key_keeps_every_mutation_clear_of_seeded_traps(tmp_path):
    man = mutate.run(JC, tmp_path / "m", 6, 7, None, ("README.md",),
                     avoid_keys=[JC_KEY], avoid_margin=2)
    assert man["applied"] == 6 and man["sanity_ok"]
    assert man["command"]["avoid_key"] == [str(JC_KEY)]
    assert man["avoid_key_sha256"][str(JC_KEY)]
    assert any(r["reason"] == "overlaps_answer_key" for r in man["rejected"])
    traps = _flagged_locations()
    for it in _items(tmp_path / "m"):
        for e in it["mutation"]["edits"]:
            for f, a, b, kid in traps:
                if f == e["file"]:
                    assert not (e["original_start_line"] - 2 <= b and a <= e["original_end_line"] + 2), \
                        (it["id"], it["mutation"]["site"], kid)
    assert "num:250000@ClientSide/JC/JC/modJCGlobals.vb:70:23" not in \
        [it["mutation"]["site"] for it in _items(tmp_path / "m")]


def test_avoid_key_is_reproducible(tmp_path):
    a = mutate.run(JC, tmp_path / "a", 6, 7, None, ("README.md",), avoid_keys=[JC_KEY], avoid_margin=2)
    b = mutate.run(JC, tmp_path / "b", 6, 7, None, ("README.md",), avoid_keys=[JC_KEY], avoid_margin=2)
    assert a["mutations_yaml_sha256"] == b["mutations_yaml_sha256"]
    assert a["mutated_corpus_sha256"] == b["mutated_corpus_sha256"]


def test_scope_all_is_stricter_than_flagged():
    flagged = mutate.load_key_locations([JC_KEY], "flagged")
    every = mutate.load_key_locations([JC_KEY], "all")
    assert sum(map(len, every.values())) > sum(map(len, flagged.values())) > 0


def test_a_key_without_locations_is_refused(tmp_path):
    k = tmp_path / "k.yaml"
    k.write_text("items: [{id: X, statement: s}]\n")
    with pytest.raises(SystemExit, match="no flagged item locations"):
        mutate.load_key_locations([k])


def test_anchors_ignore_path_segments():
    item = {"locations": [{"file": "ClientSide/JC/JC/modJCGlobals.vb", "start_line": 71}],
            "mutation": {"site": "num:250000@ClientSide/JC/JC/modJCGlobals.vb:70:23",
                         "before": "Return 250000D", "after": "Return 375000D"}}
    anc = score_mutations.anchors_for(item)
    assert "ClientSide" not in anc["idents"] and "modJCGlobals" not in anc["idents"]
    assert "250000" in anc["nums"] and "modjcglobals.vb" in anc["files"]
    # a real symbol in the site id is still an anchor
    item2 = {"locations": [{"file": "a/clsX.vb", "start_line": 3}],
             "mutation": {"site": "const:MAX_BATCH@a/clsX.vb:27", "before": "", "after": ""}}
    assert "MAX_BATCH" in score_mutations.anchors_for(item2)["idents"]


def test_a_finding_that_only_names_the_file_does_not_detect(tmp_path):
    """The JC control-run false positive, reduced to its essentials."""
    muts = {"items": [{
        "id": "MUT-001", "type": "rule", "statement": "s", "flags": ["inconsistency"],
        "locations": [{"file": "ClientSide/JC/JC/modJCGlobals.vb", "start_line": 71, "end_line": 71}],
        "mutation": {"category": "threshold_divergence", "detection_flag": "inconsistency",
                     "site": "num:250000@ClientSide/JC/JC/modJCGlobals.vb:70:23",
                     "before": "Return 250000D", "after": "Return 375000D",
                     "detect_terms": ["conflict"]}}]}
    findings = [{"id": "F1", "text": "Naming conflict: markup fields differ by scope - Evidence: "
                                     "modJCGlobals.vb (ClientSide) MAX_MARKUP_PCT"}]
    res = score_mutations.detect(muts["items"], findings, slack=3)["mutations"][0]
    assert res["verdict"] == "missed", json.dumps(res)


def test_shipped_jc_suite_uses_avoid_key():
    sys.path.insert(0, str(REPO / "eval" / "ci"))
    import suite_lib
    cfg = suite_lib.load_config(REPO / "eval" / "ci" / "suites" / "jc-changeorders.yaml")
    st = next(s for s in cfg["stages"] if s["id"] == "mutate-jc")
    assert st["avoid_key"] == ["eval/keys/jc-changeorders/ANSWER_KEY.yaml"]
    assert st["avoid_key_margin"] == 2
    ev = suite_lib.EVALUATORS["mutate"]
    exists, flags, _ = suite_lib.probe_flags(REPO, ev.script)
    argv, notes = suite_lib.build_argv(REPO, ev, st, Path("/tmp/x"), flags, smoke=True,
                                       budget_left=None)
    assert argv[argv.index("--avoid-key") + 1] == "eval/keys/jc-changeorders/ANSWER_KEY.yaml"
    assert argv[argv.index("--avoid-key-margin") + 1] == "2"
    assert not [n for n in notes if "avoid" in n]

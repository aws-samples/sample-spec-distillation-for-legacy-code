# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the mutation harness. No AWS calls: nothing here touches Bedrock.

    python3 -m pytest -q eval/mutate/tests
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

MUT_DIR = Path(__file__).resolve().parents[1]
REPO = MUT_DIR.parents[1]
sys.path.insert(0, str(MUT_DIR))

import mutate                                                 # noqa: E402
import mutations as M                                         # noqa: E402
import sanity                                                 # noqa: E402

AP = REPO / "synthetic-corpus" / "ap-invoices"
JC = REPO / "synthetic-corpus" / "jc-changeorders"


def tree_sha(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


# --------------------------------------------------------------------------- sanity layer
@pytest.mark.parametrize("corpus", [AP, JC])
def test_shipped_corpora_pass_the_syntax_check(corpus):
    ok, problems = sanity.check_tree(corpus)
    assert ok, problems


def test_sanity_catches_an_unterminated_string(tmp_path):
    f = tmp_path / "a.vb"
    f.write_text('Public Module M\n    Dim s As String = "oops\nEnd Module\n')
    ok, problems = sanity.check(f)
    assert not ok and any("unterminated string" in p for p in problems)


def test_sanity_catches_a_dropped_block_terminator(tmp_path):
    f = tmp_path / "a.vb"
    f.write_text("Public Module M\n    Public Function F() As Integer\n"
                 "        Return 1\n    End Function\nEnd Module\n")
    assert sanity.check(f)[0]
    f.write_text("Public Module M\n    Public Function F() As Integer\n"
                 "        Return 1\nEnd Module\n")
    ok, problems = sanity.check(f)
    assert not ok and any("Function" in p for p in problems)


def test_sanity_catches_unbalanced_parens(tmp_path):
    f = tmp_path / "a.sql"
    f.write_text("SELECT COUNT(* FROM t;\n")
    assert not sanity.check(f)[0]


def test_sanity_ignores_comments_and_string_bodies(tmp_path):
    f = tmp_path / "a.vb"
    f.write_text('Public Module M\n'
                 "    ' End Function ((( unbalanced in a comment\n"
                 '    Dim s As String = "End Module ((("\n'
                 'End Module\n')
    ok, problems = sanity.check(f)
    assert ok, problems


# --------------------------------------------------------------------------- site discovery
def test_every_category_has_sites_on_at_least_one_shipped_corpus():
    found: set[str] = set()
    for corpus in (AP, JC):
        c = M.Corpus(corpus)
        found |= {s.category for s in M.all_sites(c)}
    assert found == set(M.CATEGORIES), set(M.CATEGORIES) - found


def test_site_discovery_is_order_stable():
    sa = M.all_sites(M.Corpus(AP))
    sb = M.all_sites(M.Corpus(AP))
    assert [s.key for s in sa] == [s.key for s in sb]
    # the canonical order is (category, file, line, key)
    assert sa == sorted(sa, key=lambda s: s.sort_key())
    keys = [s.key for s in sa]
    assert len(keys) == len(set(keys)), "site keys must be unique"


def test_zero_applicable_sites_is_recorded_not_fatal(tmp_path):
    """A corpus with nothing to mutate yields no mutations and a reason per category."""
    src = tmp_path / "bare"
    src.mkdir()
    (src / "notes.vb").write_text("Public Module Empty\nEnd Module\n")
    out = tmp_path / "out"
    man = mutate.run(src, out, n=4, seed=1, categories=None)
    assert man["applied"] == 0
    reasons = {r["category"]: r["reason"] for r in man["rejected"]}
    assert set(reasons) == set(M.CATEGORIES)
    assert set(reasons.values()) == {"no_applicable_sites"}
    gt = yaml.safe_load((out / "mutations.yaml").read_text())
    assert gt["items"] == []


def test_strict_mode_fails_when_fewer_mutations_apply(tmp_path):
    src = tmp_path / "bare"
    src.mkdir()
    (src / "notes.vb").write_text("Public Module Empty\nEnd Module\n")
    with pytest.raises(SystemExit, match="applied 0/3"):
        mutate.run(src, tmp_path / "out", n=3, seed=1, categories=None, strict=True)


def test_empty_corpus_after_exclude_is_an_error(tmp_path):
    src = tmp_path / "c"
    src.mkdir()
    (src / "README.md").write_text("# nothing else\n")
    with pytest.raises(SystemExit, match="empty after --exclude"):
        mutate.run(src, tmp_path / "out", n=1, seed=1, categories=None)


def test_missing_corpus_is_an_error(tmp_path):
    with pytest.raises(SystemExit, match="corpus not found"):
        mutate.run(tmp_path / "nope", tmp_path / "out", n=1, seed=1, categories=None)


# --------------------------------------------------------------------------- overlap
def _one_line_corpus(tmp_path: Path) -> Path:
    """One VB line is simultaneously a guard, a >= comparison and a threshold site."""
    src = tmp_path / "c"
    (src / "ClientSide").mkdir(parents=True)
    (src / "ClientSide" / "mod.vb").write_text(
        "Public Module modThing\n"
        "    Public Const APPR_LIMIT As Decimal = 5000D\n"
        "    Public Function Check(ByVal Amount As Decimal) As Boolean\n"
        "        If Amount >= APPR_LIMIT Then Return False\n"
        "        Return True\n"
        "    End Function\n"
        "End Module\n")
    (src / "Scripts").mkdir(parents=True)
    (src / "Scripts" / "p.sql").write_text(
        "CREATE PROCEDURE dbo.usp_Thing AS\nBEGIN\n"
        "    DECLARE @Lim MONEY = 5000\n"
        "    SELECT 1 WHERE 1 >= @Lim\nEND\n")
    return src


def test_two_mutations_on_the_same_line_are_rejected(tmp_path):
    src = _one_line_corpus(tmp_path)
    c = M.Corpus(src)
    cats = ["operator_relax", "client_check_removed", "rule_to_comment"]
    chosen, rejected = mutate.select_mutations(c, n=3, seed=5, categories=cats)
    # all three operators target line 4 of mod.vb, so only one can be applied
    assert len(chosen) == 1
    assert any(r["reason"] == "overlaps" for r in rejected), rejected
    # and the lines each accepted mutation touches are disjoint
    touched: set[tuple[str, int]] = set()
    for m in chosen:
        for e in m.edits:
            keys = {(e.file, ln) for ln in e.touched}
            assert not (keys & touched)
            touched |= keys


def test_overlap_rejection_is_deterministic(tmp_path):
    src = _one_line_corpus(tmp_path)
    cats = ["operator_relax", "client_check_removed", "rule_to_comment"]
    runs = []
    for _ in range(3):
        chosen, rejected = mutate.select_mutations(M.Corpus(src), n=3, seed=5, categories=cats)
        runs.append(([m.site.key for m in chosen],
                     [(r["category"], r["reason"]) for r in rejected]))
    assert runs[0] == runs[1] == runs[2]


# --------------------------------------------------------------------------- end to end
@pytest.fixture(scope="module")
def ap_mut(tmp_path_factory):
    out = tmp_path_factory.mktemp("apmut")
    man = mutate.run(AP, out, n=5, seed=42, categories=None)
    return out, man


def test_mutated_corpus_still_tokenises(ap_mut):
    out, man = ap_mut
    assert man["applied"] == 5
    assert man["sanity_ok"] is True, man["sanity_problems"]
    ok, problems = sanity.check_tree(out / "corpus")
    assert ok, problems


def test_mutated_corpus_differs_from_the_base_only_at_the_recorded_sites(ap_mut):
    out, _ = ap_mut
    gt = yaml.safe_load((out / "mutations.yaml").read_text())
    changed = set()
    for p in sorted((out / "corpus").rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(out / "corpus").as_posix()
        base = AP / rel
        if base.read_bytes() != p.read_bytes():
            changed.add(rel)
    recorded = {e["file"] for it in gt["items"] for e in it["mutation"]["edits"]}
    assert changed == recorded, (changed, recorded)


def test_answer_key_and_readme_are_not_copied(ap_mut):
    out, _ = ap_mut
    assert not (out / "corpus" / "ANSWER_KEY.yaml").exists()
    assert not (out / "corpus" / "README.md").exists()
    assert (out / "mutations.yaml").is_file()          # ground truth lives outside the corpus


def test_same_seed_gives_byte_identical_output(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    mutate.run(AP, a, n=5, seed=42, categories=None)
    mutate.run(AP, b, n=5, seed=42, categories=None)
    assert tree_sha(a / "corpus") == tree_sha(b / "corpus")
    assert (a / "mutations.yaml").read_bytes() == (b / "mutations.yaml").read_bytes()
    ma = json.loads((a / "mutate_manifest.json").read_text())
    mb = json.loads((b / "mutate_manifest.json").read_text())
    assert ma["mutated_corpus_sha256"] == mb["mutated_corpus_sha256"]
    assert ma["mutations_yaml_sha256"] == mb["mutations_yaml_sha256"]


def test_a_different_seed_changes_the_selection(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    mutate.run(AP, a, n=5, seed=42, categories=None)
    mutate.run(AP, b, n=5, seed=99, categories=None)
    ga = yaml.safe_load((a / "mutations.yaml").read_text())
    gb = yaml.safe_load((b / "mutations.yaml").read_text())
    assert [i["mutation"]["site"] for i in ga["items"]] != \
           [i["mutation"]["site"] for i in gb["items"]]


def test_ground_truth_is_in_answer_key_schema(ap_mut):
    out, _ = ap_mut
    gt = yaml.safe_load((out / "mutations.yaml").read_text())
    assert gt["mutation_schema_version"] == mutate.SCHEMA_VERSION
    assert gt["seed"] == 42
    assert gt["base_corpus_sha256"] and gt["mutated_corpus_sha256"]
    for it in gt["items"]:
        assert set(it) >= {"id", "type", "statement", "locations", "evidence_class",
                           "flags", "mutation"}
        assert it["type"] in ("rule", "state", "workflow", "exception")
        assert it["evidence_class"] in ("Verified", "Inferred", "Unknown",
                                       "Confirmation Required")
        assert it["flags"] and all(isinstance(f, str) for f in it["flags"])
        assert it["locations"]
        for loc in it["locations"]:
            assert set(loc) == {"file", "start_line", "end_line"}
            assert loc["start_line"] >= 1 and loc["end_line"] >= loc["start_line"]
        assert it["mutation"]["category"] in M.CATEGORIES
        assert it["mutation"]["detection_flag"] == M.CATEGORIES[it["mutation"]["category"]]
        assert it["mutation"]["detect_terms"]


def test_recorded_locations_point_at_the_mutated_lines(ap_mut):
    """The ground truth must cite coordinates in the MUTATED tree, not the original."""
    out, _ = ap_mut
    gt = yaml.safe_load((out / "mutations.yaml").read_text())
    for it in gt["items"]:
        cat = it["mutation"]["category"]
        after = it["mutation"]["after"]
        if cat == "dead_code_orphan":
            loc = it["locations"][0]
            lines = (out / "corpus" / loc["file"]).read_text().split("\n")
            block = "\n".join(lines[loc["start_line"] - 1: loc["end_line"]])
            assert "Legacy" in block, block
            continue
        if cat in ("client_check_removed",):
            continue                                   # the line is gone by design
        loc = it["locations"][0]
        lines = (out / "corpus" / loc["file"]).read_text().split("\n")
        window = "\n".join(lines[max(0, loc["start_line"] - 2): loc["end_line"] + 1])
        assert after.strip()[:40] in window, (cat, after, window)


def test_declared_structural_delta_is_enforced(tmp_path, monkeypatch):
    """A mutation that breaks the file must be rejected, not written out."""
    src = _one_line_corpus(tmp_path)

    def wrecking_build(site, corpus):
        return M.Mutation(
            category="operator_relax", site=site,
            edits=[M.Edit(site.file, site.line, site.line, ['        If Amount > ("'])],
            statement="deliberately broken", flags=["logic_flaw"])

    monkeypatch.setitem(M.OPERATORS, "operator_relax",
                        (M.sites_operator_relax, wrecking_build))
    out = tmp_path / "out"
    man = mutate.run(src, out, n=1, seed=1, categories=["operator_relax"])
    assert man["applied"] == 0
    assert any(r["reason"] == "not_syntax_preserving" for r in man["rejected"]), man["rejected"]
    assert man["sanity_ok"] is True
    # the file was rolled back
    assert (out / "corpus" / "ClientSide" / "mod.vb").read_text() == \
           (src / "ClientSide" / "mod.vb").read_text()


def test_dead_code_clone_adds_exactly_one_function():
    c = M.Corpus(AP)
    site = next(s for s in M.all_sites(c, ["dead_code_orphan"]))
    mut = M.build_dead_code_orphan(site, c)
    assert mut.expected_delta[site.file] == {"vb_open_function": 1, "vb_close_function": 1}


def test_category_filter_is_respected(tmp_path):
    out = tmp_path / "out"
    man = mutate.run(AP, out, n=3, seed=3, categories=["silent_cap", "date_off_by_one"])
    gt = yaml.safe_load((out / "mutations.yaml").read_text())
    assert {i["mutation"]["category"] for i in gt["items"]} <= {"silent_cap", "date_off_by_one"}
    assert man["command"]["categories"] == ["silent_cap", "date_off_by_one"]


def test_unknown_category_is_rejected():
    with pytest.raises(KeyError, match="unknown mutation category"):
        M.all_sites(M.Corpus(AP), ["no_such_operator"])

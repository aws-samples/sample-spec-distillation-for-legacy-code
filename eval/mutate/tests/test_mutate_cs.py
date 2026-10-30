# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""C# mutation operators, the C# scanner, syntax preservation and the optional Roslyn check.
No AWS, no dotnet (the Roslyn check is exercised with a stand-in tool)."""
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

import cs_scan                                                # noqa: E402
import mutate                                                 # noqa: E402
import mutations as M                                         # noqa: E402
import mutations_cs as MC                                     # noqa: E402
import sanity                                                 # noqa: E402

CS = REPO / "eval" / "keygen" / "tests" / "fixtures" / "mini-cs"


def tree_sha(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


# --------------------------------------------------------------------------- scanner
def test_scanner_blanks_comments_and_literal_bodies_but_keeps_columns():
    src = ('var a = "x { ( //";  // trailing ( {\n'
           'var b = @"multi\n line "" { (";\n'
           'var c = $"n={Count(x)} {{lit}}";\n'
           "var d = '{'; /* block ( \n still */ var e = 1;\n"
           'var f = """raw { ( """;')
    code, ok = cs_scan.scan_cs(src)
    assert all(ok)
    assert [len(c) for c in code] == [len(l) for l in src.split("\n")]
    joined = "\n".join(code)
    assert joined.count("{") == joined.count("}") == 0          # every brace was inside a literal
    assert "Count(x)" in code[3]                                  # interpolation hole stays code
    assert "var e = 1;" in code[5]                                # after the block comment ends


def test_scanner_flags_an_unterminated_string():
    code, ok = cs_scan.scan_cs('var s = "oops;\nvar t = 1;')
    assert ok == [False, True]


def test_sanity_catches_broken_csharp(tmp_path):
    f = tmp_path / "a.cs"
    f.write_text("class A {\n  void F() { if (x) { } }\n}\n")
    assert sanity.check(f)[0]
    f.write_text("class A {\n  void F() { if (x) { }\n}\n")
    ok, problems = sanity.check(f)
    assert not ok and any("braces" in p for p in problems)
    f.write_text('class A { string s = "x; }\n')
    assert not sanity.check(f)[0]


def test_sanity_accepts_the_csharp_fixture():
    ok, problems = sanity.check_tree(CS)
    assert ok, problems


# --------------------------------------------------------------------------- operators
def test_every_category_has_a_csharp_site_on_the_fixture():
    c = MC.CsCorpus(CS)
    found = {s.category for s in MC.all_sites(c)}
    assert found == set(M.CATEGORIES), set(M.CATEGORIES) - found


def test_test_projects_are_never_mutated():
    c = MC.CsCorpus(CS)
    assert c.files and not any(r.startswith("tests/") for r in c.files)


def test_lang_auto_detects_csharp():
    assert mutate.detect_lang(CS) == "cs"
    assert mutate.detect_lang(REPO / "synthetic-corpus" / "ap-invoices") == "vb"


@pytest.fixture(scope="module")
def eight(tmp_path_factory):
    out = tmp_path_factory.mktemp("cs8")
    man = mutate.run(CS, out / "a", 8, 3, None, exclude=(), roslyn=False)
    return out, man


def test_eight_mutations_one_per_category_and_syntax_preserved(eight):
    out, man = eight
    assert man["applied"] == 8 and man["lang"] == "cs"
    gt = yaml.safe_load((out / "a" / "mutations.yaml").read_text())
    assert sorted(i["mutation"]["category"] for i in gt["items"]) == sorted(M.CATEGORIES)
    ok, problems = sanity.check_tree(out / "a" / "corpus")
    assert ok and man["sanity_ok"], problems
    for it in gt["items"]:
        assert it["flags"] and it["locations"]
        for l in it["locations"]:
            p = out / "a" / "corpus" / l["file"]
            assert p.is_file() and l["end_line"] <= len(p.read_text().split("\n"))


def test_same_seed_gives_byte_identical_output(eight, tmp_path):
    out, man = eight
    man2 = mutate.run(CS, tmp_path / "b", 8, 3, None, exclude=(), roslyn=False)
    assert man2["mutated_corpus_sha256"] == man["mutated_corpus_sha256"]
    assert (tmp_path / "b" / "mutations.yaml").read_bytes() == (out / "a" / "mutations.yaml").read_bytes()
    assert tree_sha(tmp_path / "b" / "corpus") == tree_sha(out / "a" / "corpus")
    man3 = mutate.run(CS, tmp_path / "c", 8, 4, None, exclude=(), roslyn=False)
    assert man3["mutations_yaml_sha256"] != man["mutations_yaml_sha256"]


def test_dead_code_clone_declares_its_structural_delta(tmp_path):
    man = mutate.run(CS, tmp_path / "d", 1, 1, ["dead_code_orphan"], exclude=(), roslyn=False)
    assert man["applied"] == 1
    gt = yaml.safe_load((tmp_path / "d" / "mutations.yaml").read_text())
    m = gt["items"][0]
    clone = m["mutation"]["after"].split()[-1]
    f = tmp_path / "d" / "corpus" / m["locations"][0]["file"]
    assert clone.endswith("Legacy") and clone in f.read_text()


def test_guard_removal_names_where_the_rule_still_lives(tmp_path):
    man = mutate.run(CS, tmp_path / "g", 1, 1, ["client_check_removed"], exclude=(), roslyn=False)
    gt = yaml.safe_load((tmp_path / "g" / "mutations.yaml").read_text())
    it = gt["items"][0]
    assert "enforced only elsewhere" in it["statement"]
    assert any("OrderConfiguration.cs" in l["file"] for l in it["locations"])   # EF IsRequired


# --------------------------------------------------------------------------- Roslyn check
FAKE_TOOL = '''
import json, sys, pathlib
root = pathlib.Path(sys.argv[-1])
diags = []
for p in sorted(root.rglob("*.cs")):
    if "{marker}" in p.read_text():
        diags.append({{"file": p.relative_to(root).as_posix(), "line": 1, "id": "CS0000", "message": "x"}})
print(json.dumps({{"parse_diagnostics": diags}}))
'''


def _tool(tmp_path: Path, marker: str) -> str:
    t = tmp_path / f"fake_{abs(hash(marker))}.py"
    t.write_text(FAKE_TOOL.format(marker=marker))
    return f"{sys.executable} {t}"


def test_roslyn_check_rolls_back_a_file_that_gets_new_syntax_errors(tmp_path):
    cmd = _tool(tmp_path, "Legacy(")                     # "breaks" every dead-code clone
    man = mutate.run(CS, tmp_path / "r", 1, 1, ["dead_code_orphan"], exclude=(), roslyn_cmd=cmd)
    assert man["applied"] == 0
    assert man["roslyn_check"]["status"] == "failed"
    assert any(r["reason"] == "not_syntax_preserving" and "Roslyn" in r["detail"] for r in man["rejected"])
    assert tree_sha(tmp_path / "r" / "corpus") == tree_sha(CS)


def test_roslyn_check_passes_and_is_recorded(tmp_path):
    cmd = _tool(tmp_path, "@@never@@")
    man = mutate.run(CS, tmp_path / "ok", 3, 1, None, exclude=(), roslyn_cmd=cmd)
    assert man["applied"] == 3
    assert man["roslyn_check"]["status"] == "ok" and man["roslyn_check"]["files_checked"] >= 1


def test_roslyn_check_is_skipped_without_a_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(mutate, "resolve_roslyn", lambda cmd: (None, "dotnet not found on PATH"))
    man = mutate.run(CS, tmp_path / "s", 2, 1, None, exclude=())
    assert man["roslyn_check"] == {"status": "skipped", "reason": "dotnet not found on PATH",
                                   "files_checked": 0}
    assert man["applied"] == 2


def test_vb_corpora_are_unaffected_by_the_csharp_operators(tmp_path):
    ap = REPO / "synthetic-corpus" / "ap-invoices"
    man = mutate.run(ap, tmp_path / "ap", 5, 42, None)
    assert man["lang"] == "vb" and man["roslyn_check"]["status"] == "not_applicable"
    ex = REPO / "eval" / "mutate" / "examples" / "ap-invoices-seed42"
    ref = json.loads((ex / "mutate_manifest.json").read_text())
    if man["base_corpus_sha256"] != ref["base_corpus_sha256"]:
        pytest.skip("the AP corpus changed since the reference example was recorded")
    assert man["mutated_corpus_sha256"] == ref["mutated_corpus_sha256"]
    got = yaml.safe_load((tmp_path / "ap" / "mutations.yaml").read_text())["items"]
    assert got == yaml.safe_load((ex / "mutations.yaml").read_text())["items"]

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Segmentation: explicit, deterministic, and comparable."""
from __future__ import annotations

import json

import pytest

import score

SPEC_WITH_IDS = """# Widget Spec

This document specifies 2 requirements, each with an Evidence line.

## Requirements Index

| ID | Requirement | Evidence |
|---|---|---|
| RL-0001 | Approval threshold | a.vb:L2-L4 |
| RL-0002 | Immutability | sub/b.sql:L1-L2 |

## Business Rules

#### [RL-0001] Approval Threshold Gates Status

**Requirement:** Invoices above the threshold SHALL go to Pending Approval.
- Rationale: control over spend.

Evidence: a.vb:L2-L4

#### [RL-0002] Posted Invoices Are Immutable

**Requirement:** A posted invoice SHALL NOT be edited.
- Rationale: audit integrity.

Evidence: sub/b.sql:L1-L2
"""


def write(tmp_path, text, name="spec.md"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_empty_spec(tmp_path):
    for mode in score.SEGMENT_MODES:
        assert score.load_findings(write(tmp_path, "", f"e-{mode}.md"), mode) == []


def test_whitespace_only_spec(tmp_path):
    for mode in score.SEGMENT_MODES:
        assert score.load_findings(write(tmp_path, "\n\n   \n\t\n", f"w-{mode}.md"), mode) == []


def test_headings_only_spec_has_no_findings(tmp_path):
    p = write(tmp_path, "# A\n\n## B\n\n### C\n")
    assert score.load_findings(p, "auto") == []


def test_requirements_mode_folds_the_index(tmp_path):
    p = write(tmp_path, SPEC_WITH_IDS)
    reqs = score.load_findings(p, "requirements")
    kinds = [f["kind"] for f in reqs]
    assert kinds.count("requirement") == 2
    assert [f["req_id"] for f in reqs if f["kind"] == "requirement"] == ["RL-0001", "RL-0002"]
    # the index row for RL-0001 is attached to RL-0001, not a separate finding
    r1 = next(f for f in reqs if f["req_id"] == "RL-0001")
    assert "Approval threshold" in r1["text"] and r1.get("index_rows") == 1
    assert sum(1 for f in reqs if f["kind"] == "narrative") >= 1


def test_requirements_mode_is_coarser_than_auto_which_is_coarser_than_lines(tmp_path):
    p = write(tmp_path, SPEC_WITH_IDS)
    n = {m: len(score.load_findings(p, m)) for m in score.SEGMENT_MODES}
    assert n["requirements"] <= n["auto"] <= n["lines"], n
    assert n["requirements"] < n["lines"], n


def test_requirements_mode_falls_back_when_the_spec_has_no_ids(tmp_path):
    p = write(tmp_path, "## Rules\n\n- Invoices above the threshold need approval. a.vb:L2-L4\n")
    reqs = score.load_findings(p, "requirements")
    assert reqs and all(f["kind"] != "requirement" for f in reqs)


def test_requirements_strategy_is_recorded_with_the_fallback(tmp_path, mini_key, src_tree):
    p = write(tmp_path, "## Rules\n\n- Invoices above the threshold need approval. a.vb:L2-L4\n")
    report, _ = score.score(p, mini_key, src_tree, False, None, None, segment="requirements")
    assert report["segmentation"]["strategy_requested"] == "requirements"
    assert "defines no requirement ids" in report["segmentation"]["strategy"]


def test_segmentation_block_records_strategy_and_counts(tmp_path, mini_key, src_tree):
    p = write(tmp_path, SPEC_WITH_IDS)
    report, _ = score.score(p, mini_key, src_tree, False, None, None, segment="requirements")
    seg = report["segmentation"]
    assert seg["strategy"] == "requirements"
    assert seg["findings"] == report["counts"]["findings"]
    assert seg["by_kind"]["requirement"] == 2
    assert seg["requirement_ids_in_spec"] == 2
    assert seg["spec_bytes"] == p.stat().st_size
    assert seg["strategy_doc"]


def test_unknown_segment_mode_is_rejected(tmp_path):
    with pytest.raises(SystemExit):
        score.load_findings(write(tmp_path, "# x\n\ntext here now please\n"), "nonsense")


# ------------------------------------------------------------- comparability gate
def _score_json(tmp_path, n_findings, strategy="auto"):
    p = tmp_path / f"other-{n_findings}.json"
    p.write_text(json.dumps({"counts": {"findings": n_findings},
                             "segmentation": {"findings": n_findings, "strategy": strategy}}))
    return p


def test_compare_to_warns_above_2x(tmp_path, mini_key, src_tree, capsys):
    p = write(tmp_path, SPEC_WITH_IDS)
    other = _score_json(tmp_path, 400, "requirements")
    report, _ = score.score(p, mini_key, src_tree, False, None, None, segment="requirements",
                            compare_to=other)
    comp = report["segmentation"]["comparability"]
    assert comp["comparable"] is False
    assert comp["ratio"] > score.COMPARABILITY_RATIO
    assert "NOT comparable" in comp["warning"]
    assert "WARNING" in capsys.readouterr().err


def test_compare_to_accepts_similar_counts(tmp_path, mini_key, src_tree):
    p = write(tmp_path, SPEC_WITH_IDS)
    n = len(score.load_findings(p, "requirements"))
    other = _score_json(tmp_path, n + 1, "requirements")
    report, _ = score.score(p, mini_key, src_tree, False, None, None, segment="requirements",
                            compare_to=other)
    assert report["segmentation"]["comparability"]["comparable"] is True


def test_compare_to_flags_a_different_strategy(tmp_path, mini_key, src_tree):
    p = write(tmp_path, SPEC_WITH_IDS)
    n = len(score.load_findings(p, "requirements"))
    other = _score_json(tmp_path, n, "lines")
    report, _ = score.score(p, mini_key, src_tree, False, None, None, segment="requirements",
                            compare_to=other)
    comp = report["segmentation"]["comparability"]
    assert comp["comparable"] is False and "segmentation differs" in comp["warning"]


def test_compare_to_missing_file_fails_loudly(tmp_path, mini_key, src_tree):
    p = write(tmp_path, SPEC_WITH_IDS)
    with pytest.raises(SystemExit):
        score.score(p, mini_key, src_tree, False, None, None, compare_to=tmp_path / "nope.json")


# --------------------------------------------------------------- repeated content
def test_identical_findings_repeated_100x(tmp_path, mini_key, src_tree):
    body = "- Invoices above the threshold go to Pending Approval. a.vb:L2-L4\n"
    p = write(tmp_path, "## Rules\n\n" + body * 100)
    report, _ = score.score(p, mini_key, src_tree, False, None, None)
    assert report["counts"]["findings"] == 100
    assert report["counts"]["citations"] == 100
    # recall must not exceed 100% just because the same thing was said 100 times
    assert report["recall"]["any_match_pct"] <= 100.0
    assert report["precision"]["keyed_pct"] <= 100.0


def test_finding_text_is_capped_but_keeps_the_trailing_evidence_line(tmp_path):
    long_body = "\n".join(f"- filler sentence number {i} about invoices" for i in range(4000))
    p = write(tmp_path, f"#### [RL-0001] Big One\n\n**Requirement:** x y z w\n{long_body}\n"
                        f"\nEvidence: a.vb:L2-L4\n")
    f = score.load_findings(p, "requirements", 5000)[0]
    assert f.get("text_truncated_chars", 0) > 0
    assert "chars omitted" in f["text"]
    assert "a.vb:L2-L4" in f["text"]      # the tail survived, so the citation is still found
    assert len(f["text"]) < 7000


def test_json_spec_segmentation(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps({"requirements": [
        {"id": "RL-0001", "statement": "Invoices above the threshold need approval.",
         "citations": ["a.vb:L2-L4"]},
        {"id": "RL-0002", "statement": "A posted invoice must never be edited again.",
         "citations": ["sub/b.sql:L1-L2"]}]}))
    fs = score.load_findings(p, "requirements")
    assert [f["req_id"] for f in fs] == ["RL-0001", "RL-0002"]
    assert all(f["kind"] == "requirement" for f in fs)


def test_crlf_and_bom_do_not_change_segmentation(tmp_path):
    plain = write(tmp_path, SPEC_WITH_IDS, "plain.md")
    crlf = tmp_path / "crlf.md"
    crlf.write_bytes(b"\xef\xbb\xbf" + SPEC_WITH_IDS.replace("\n", "\r\n").encode())
    a = [(f["kind"], f["req_id"], f["text"]) for f in score.load_findings(plain, "requirements")]
    b = [(f["kind"], f["req_id"], f["text"]) for f in score.load_findings(crlf, "requirements")]
    assert a == b

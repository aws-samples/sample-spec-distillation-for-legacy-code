# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Source coverage and key-location coverage (exact layer), plus degenerate keys."""
from __future__ import annotations

import json

import pytest

import score
from conftest_score import write_key


def spec(tmp_path, text, name="spec.md"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_key_location_coverage(tmp_path, src_tree, mini_key):
    p = spec(tmp_path, "## Rules\n\n"
                       "- Invoices above the threshold need approval. a.vb:L2-L4\n")
    report, _ = score.score(p, mini_key, src_tree, False, None, None)
    kl = report["source_coverage"]["key_locations"]
    assert kl["total"] == 2 and kl["cited"] == 1 and kl["pct"] == 50.0
    detail = {d["key_id"]: d for d in report["source_coverage"]["key_location_detail"]}
    assert detail["K-01"]["fully_covered"] is True
    assert detail["K-01"]["locations"][0]["cited_by"] == ["F1"]
    assert detail["K-02"]["covered"] == 0


def test_files_never_cited_and_density(tmp_path, src_tree, mini_key):
    p = spec(tmp_path, "## Rules\n\n- A rule about approval thresholds. a.vb:L1-L5\n")
    report, _ = score.score(p, mini_key, src_tree, False, None, None)
    sc = report["source_coverage"]
    assert set(sc["files_never_cited"]) == {"sub/b.sql", "Reports/Odd Name (412).txt", "empty.sql"}
    per_file = {d["file"]: d for d in sc["per_file"]}
    assert per_file["a.vb"]["lines"] == 10
    assert per_file["a.vb"]["lines_cited"] == 5
    assert per_file["a.vb"]["coverage_pct"] == 50.0
    assert per_file["a.vb"]["citations_per_100_lines"] == 10.0
    assert per_file["a.vb"]["uncited_ranges"] == [[6, 10]]
    assert sc["source_lines"] == 18 and sc["source_lines_cited"] == 5


def test_overlapping_citations_do_not_double_count_covered_lines(tmp_path, src_tree, mini_key):
    p = spec(tmp_path, "## Rules\n\n- Rule one about approvals. a.vb:L1-L5, L3-L8\n")
    report, _ = score.score(p, mini_key, src_tree, False, None, None)
    per_file = {d["file"]: d for d in report["source_coverage"]["per_file"]}
    assert per_file["a.vb"]["lines_cited"] == 8          # 1-8, not 5 + 6
    assert per_file["a.vb"]["citations"] == 2


def test_empty_file_has_no_coverage_percentage(tmp_path, src_tree, mini_key):
    p = spec(tmp_path, "## Rules\n\n- A rule about approval thresholds. a.vb:L1-L2\n")
    report, _ = score.score(p, mini_key, src_tree, False, None, None)
    per_file = {d["file"]: d for d in report["source_coverage"]["per_file"]}
    assert per_file["empty.sql"]["lines"] == 0
    assert per_file["empty.sql"]["coverage_pct"] is None


def test_invalid_citations_do_not_count_as_coverage(tmp_path, src_tree, mini_key):
    p = spec(tmp_path, "## Rules\n\n- Approval threshold rule. a.vb:L50-L60 and ghost.vb:L1-L2\n")
    report, _ = score.score(p, mini_key, src_tree, False, None, None)
    assert report["source_coverage"]["source_lines_cited"] == 0
    assert report["source_coverage"]["key_locations"]["cited"] == 0


# --------------------------------------------------------------- degenerate keys
def test_key_with_zero_items(tmp_path, src_tree):
    key = write_key(tmp_path / "empty.json", [])
    p = spec(tmp_path, "## Rules\n\n- Some rule about invoice approval. a.vb:L1-L2\n")
    report, _ = score.score(p, key, src_tree, False, None, None)
    assert report["counts"]["key_items"] == 0
    assert report["recall"]["strict_full_pct"] is None
    assert report["recall"]["any_match_pct"] is None
    assert report["precision"]["keyed_pct"] == 0.0
    assert report["source_coverage"]["key_locations"]["pct"] is None
    assert report["missed_items"] == []


def test_key_whose_locations_are_wrong(tmp_path, src_tree):
    """A key that points at lines outside the file, or at a file that is not there.

    The scorer must still produce a report, and must never report those bogus
    locations as covered.
    """
    key = write_key(tmp_path / "bad.json", [
        {"id": "K-01", "type": "rule", "statement": "Invoices above the threshold need approval.",
         "locations": [{"file": "a.vb", "start_line": 500, "end_line": 600}],
         "evidence_class": "Verified"},
        {"id": "K-02", "type": "rule", "statement": "Posted invoices are immutable forever.",
         "locations": [{"file": "does-not-exist.vb", "start_line": 1, "end_line": 2}],
         "evidence_class": "Verified"},
    ])
    p = spec(tmp_path, "## Rules\n\n"
                       "- Invoices above the threshold need approval. a.vb:L2-L4\n"
                       "- Posted invoices are immutable forever. a.vb:L5-L6\n")
    report, _ = score.score(p, key, src_tree, False, None, None)
    assert report["source_coverage"]["key_locations"]["cited"] == 0
    assert report["citations"]["accuracy_pct"] in (0.0, None)
    assert report["citations"]["validity_pct"] == 100.0  # the spec's own citations are fine


def test_key_with_duplicate_item_ids_is_rejected(tmp_path):
    key = tmp_path / "dup.json"
    key.write_text(json.dumps({"items": [
        {"id": "K-01", "type": "rule", "statement": "one statement here"},
        {"id": "K-01", "type": "rule", "statement": "another statement here"}]}))
    with pytest.raises(SystemExit, match="duplicate item id"):
        score.load_key(key)


def test_key_item_without_an_id_is_rejected(tmp_path):
    key = tmp_path / "noid.json"
    key.write_text(json.dumps({"items": [{"type": "rule", "statement": "x"}]}))
    with pytest.raises(SystemExit, match="no id"):
        score.load_key(key)


def test_malformed_key_citation_is_rejected(tmp_path, src_tree):
    key = tmp_path / "badcit.json"
    key.write_text(json.dumps({"items": [
        {"id": "K-01", "type": "rule", "statement": "x y z", "citations": ["a.vb lines 1 to 2"]}]}))
    p = spec(tmp_path, "## Rules\n\n- Some rule here about invoices. a.vb:L1-L2\n")
    with pytest.raises(SystemExit, match="not 'file:start"):
        score.score(p, key, src_tree, False, None, None)


def test_key_locations_default_end_line(tmp_path):
    key = write_key(tmp_path / "k.json", [
        {"id": "K-01", "type": "rule", "statement": "s", "locations": [{"file": "a.vb", "start_line": 7}]}])
    k = score.load_key(key)
    assert k["items"][0]["citations"] == ["a.vb:7-7"]


def test_spec_with_no_citations_at_all(tmp_path, src_tree, mini_key):
    p = spec(tmp_path, "## Rules\n\n- Invoices above the threshold need approval.\n"
                       "- Posted invoices are immutable.\n")
    report, _ = score.score(p, mini_key, src_tree, False, None, None)
    assert report["counts"]["citations"] == 0
    assert report["citations"]["validity_pct"] is None
    assert report["citations"]["accuracy_pct"] == 0.0
    assert report["citations"]["findings_without_citation"] == report["counts"]["findings"]
    assert report["source_coverage"]["source_lines_cited"] == 0
    assert report["source_coverage"]["files_never_cited_n"] == 4


def test_exclude_keeps_files_out_of_the_source_view(tmp_path, src_tree, mini_key):
    p = spec(tmp_path, "## Rules\n\n- Rule about approvals. a.vb:L1-L2\n")
    report, _ = score.score(p, mini_key, src_tree, False, None, None,
                            exclude=["empty.sql", "*.txt"])
    files = {d["file"] for d in report["source_coverage"]["per_file"]}
    assert files == {"a.vb", "sub/b.sql"}


def test_a_finding_may_match_two_key_items(tmp_path, src_tree, mini_key):
    """One sentence that states both key items must credit both, once each."""
    p = spec(tmp_path, "## Rules\n\n- Invoices above the threshold need approval and "
                       "posted invoices are immutable. a.vb:L2-L4\n")
    findings = score.load_findings(p, "auto")
    judged = {"F1": {"matches": [{"key_id": "K-01", "degree": "full"},
                                {"key_id": "K-02", "degree": "full"}],
                     "verdict": "matched", "note": ""}}
    items = score.load_key(mini_key)["items"]
    files = score.source_files(src_tree)
    counts = {k: score.line_count(v) for k, v in files.items()}
    for f in findings:
        f["citations"] = score.extract_citations(f, files, counts, None, [])
    kc = {str(k["id"]): [score.parse_key_cit(c) for c in k["citations"]] for k in items}
    m = score.compute_metrics(findings, judged, items, kc)
    assert m["recall"]["strict_full_pct"] == 100.0
    assert m["precision"]["keyed_pct"] == 100.0          # one finding, not two
    assert m["citation_accuracy_den"] == 2               # but two match checks

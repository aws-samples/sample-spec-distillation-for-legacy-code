# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Citation parsing and verification: the part that must be exactly right."""
from __future__ import annotations

import pytest

import score
from conftest_score import cite, make_finding

FORMATS_RANGE = [
    "a.vb:L2-L4", "a.vb:2-4", "a.vb#L2-L4", "a.vb (lines 2-4)", "a.vb L2-4",
    "a.vb, 2-4", "a.vb:ll. 2-4", "a.vb:L2–L4", "a.vb:L2—L4",
    "a.vb:L2 to L4", "a.vb:L2..L4", "lines 2-4 of a.vb", "L2-L4 in a.vb",
]
FORMATS_SINGLE = ["a.vb:L7", "a.vb:7", "a.vb#L7", "a.vb (line 7)", "a.vb L7"]


@pytest.mark.parametrize("text", FORMATS_RANGE)
def test_every_supported_range_format_parses(text, files_and_counts):
    files, counts = files_and_counts
    cits = cite(text, files, counts)
    assert len(cits) == 1, cits
    c = cits[0]
    assert (c["resolved"], c["start"], c["end"]) == ("a.vb", 2, 4)
    assert c["valid"] and c["category"] == score.CIT_OK


@pytest.mark.parametrize("text", FORMATS_SINGLE)
def test_every_supported_single_line_format_parses(text, files_and_counts):
    files, counts = files_and_counts
    cits = cite(text, files, counts)
    assert len(cits) == 1, cits
    assert (cits[0]["resolved"], cits[0]["start"], cits[0]["end"]) == ("a.vb", 7, 7)


def test_path_prefixed_citation(files_and_counts):
    files, counts = files_and_counts
    c = cite("see sub/b.sql:L1-L3", files, counts)[0]
    assert (c["resolved"], c["start"], c["end"]) == ("sub/b.sql", 1, 3)


def test_basename_resolution_when_path_differs(files_and_counts):
    files, counts = files_and_counts
    c = cite("ClientSide/elsewhere/b.sql:L1-L2", files, counts)[0]
    assert c["resolved"] == "sub/b.sql" and c["valid"]


def test_literal_filename_with_spaces_and_parens(files_and_counts):
    files, counts = files_and_counts
    c = cite("Reports/Odd Name (412).txt:L1-L3", files, counts)[0]
    assert (c["resolved"], c["start"], c["end"]) == ("Reports/Odd Name (412).txt", 1, 3)
    assert c["valid"]


def test_literal_filename_basename_only(files_and_counts):
    files, counts = files_and_counts
    c = cite("Evidence: Odd Name (412).txt:L2", files, counts)[0]
    assert c["resolved"] == "Reports/Odd Name (412).txt"


def test_unicode_filename(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    name = "Rapport Général – Détail (412).txt"
    (root / name).write_text("x\ny\nz\n")
    files = score.source_files(root)
    counts = {k: score.line_count(p) for k, p in files.items()}
    c = cite(f"{name}:L1-L2", files, counts)[0]
    assert c["resolved"] == name and c["valid"]


def test_quote_in_filename(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    name = "o'brien's report (1).txt"
    (root / name).write_text("a\nb\n")
    files = score.source_files(root)
    counts = {k: score.line_count(p) for k, p in files.items()}
    c = cite(f"{name}:L1-L2", files, counts)[0]
    assert c["resolved"] == name and c["valid"]


def test_extra_ranges_inherit_the_file(files_and_counts):
    files, counts = files_and_counts
    cits = cite("a.vb:L1-L2, L5-L6, L9", files, counts)
    assert [(c["resolved"], c["start"], c["end"]) for c in cits] == [
        ("a.vb", 1, 2), ("a.vb", 5, 6), ("a.vb", 9, 9)]
    assert all(c["valid"] for c in cits)


def test_bare_range_uses_heading_file_context(files_and_counts):
    files, counts = files_and_counts
    f = make_finding("the rule lives at L3-L5", context_file="a.vb")
    cits = score.extract_citations(f, files, counts, None, score.literal_file_patterns(files))
    assert [(c["resolved"], c["start"], c["end"]) for c in cits] == [("a.vb", 3, 5)]


def test_bare_range_with_no_context_is_no_file_context(files_and_counts):
    files, counts = files_and_counts
    cits = cite("the rule lives at L3-L5", files, counts)
    assert cits[0]["category"] == score.CIT_NO_FILE
    assert not cits[0]["valid"]


def test_single_file_corpus_supplies_default_context(tmp_path):
    root = tmp_path / "one"
    root.mkdir()
    (root / "only.sql").write_text("a\nb\nc\n")
    files = score.source_files(root)
    counts = {k: score.line_count(p) for k, p in files.items()}
    cits = cite("rule at L2", files, counts, default_file="only.sql")
    assert cits[0]["resolved"] == "only.sql" and cits[0]["valid"]


# ------------------------------------------------------------------- bad ranges
def test_nonexistent_file(files_and_counts):
    files, counts = files_and_counts
    c = cite("ghost.vb:L1-L2", files, counts)[0]
    assert c["category"] == score.CIT_NOT_FOUND
    assert c["resolved"] is None and not c["valid"]


def test_out_of_range(files_and_counts):
    files, counts = files_and_counts
    c = cite("a.vb:L50-L60", files, counts)[0]
    assert c["category"] == score.CIT_OUT_OF_RANGE and "file has 10 lines" in c["reason"]


def test_off_by_one_at_end_of_file_is_its_own_category(files_and_counts):
    files, counts = files_and_counts
    c = cite("a.vb:L8-L11", files, counts)[0]  # a.vb has 10 lines; the pager shows 11
    assert c["category"] == score.CIT_OFF_BY_ONE
    assert c["off_by_one"] and not c["valid"]


def test_phantom_trailing_line_only(files_and_counts):
    files, counts = files_and_counts
    c = cite("a.vb:L11", files, counts)[0]
    assert c["category"] == score.CIT_PHANTOM and c["off_by_one"]


def test_two_lines_past_the_end_is_not_off_by_one(files_and_counts):
    files, counts = files_and_counts
    c = cite("a.vb:L8-L12", files, counts)[0]
    assert c["category"] == score.CIT_OUT_OF_RANGE and not c["off_by_one"]


def test_reversed_range_is_bad_range(files_and_counts):
    files, counts = files_and_counts
    assert score.classify_citation("a.vb", 7, 3, 10)[0] == score.CIT_BAD_RANGE


def test_zero_and_negative_start_are_bad_range():
    assert score.classify_citation("a.vb", 0, 3, 10)[0] == score.CIT_BAD_RANGE
    assert score.classify_citation("a.vb", -1, -1, 10)[0] == score.CIT_BAD_RANGE


def test_empty_file_any_citation_is_invalid(files_and_counts):
    files, counts = files_and_counts
    assert counts["empty.sql"] == 0
    c = cite("empty.sql:L1", files, counts)[0]
    assert not c["valid"] and c["category"] == score.CIT_OUT_OF_RANGE


def test_all_citations_invalid(files_and_counts):
    files, counts = files_and_counts
    cits = cite("ghost.vb:L1 and a.vb:L99-L120 and other.cob:L3-L4", files, counts)
    assert cits and not any(c["valid"] for c in cits)


def test_nested_and_overlapping_ranges_are_all_kept(files_and_counts):
    files, counts = files_and_counts
    cits = cite("a.vb:L1-L8 plus a.vb:L3-L5 plus a.vb:L5-L9", files, counts)
    assert [(c["start"], c["end"]) for c in cits] == [(1, 8), (3, 5), (5, 9)]


def test_citation_overlap_with_key_locations(files_and_counts):
    files, counts = files_and_counts
    kc = {"K-01": [("a.vb", 2, 4)], "K-02": [("sub/b.sql", 1, 2)]}
    inside = cite("a.vb:L3-L3", files, counts)[0]
    outside = cite("a.vb:L9-L10", files, counts)[0]
    touching = cite("a.vb:L4-L7", files, counts)[0]
    assert score.overlapping_key_ids(inside, kc) == ["K-01"]
    assert score.overlapping_key_ids(outside, kc) == []
    assert score.overlapping_key_ids(touching, kc) == ["K-01"]


def test_invalid_citation_never_counts_as_overlap(files_and_counts):
    files, counts = files_and_counts
    kc = {"K-01": [("a.vb", 2, 400)]}
    bad = cite("a.vb:L50-L60", files, counts)[0]
    assert score.overlapping_key_ids(bad, kc) == []


def test_key_citation_matched_by_basename_only():
    assert score.same_file("Scripts/x.sql", "other/dir/x.sql")
    assert not score.same_file("a.vb", "b.vb")
    assert not score.same_file(None, "a.vb")


def test_plus_n_suffix_in_an_index_is_not_a_citation(files_and_counts):
    """The demo spec writes 'a.vb:L2-L4 (+1)' meaning one more location."""
    files, counts = files_and_counts
    cits = cite("a.vb:L2-L4 (+1)", files, counts)
    assert len(cits) == 1 and (cits[0]["start"], cits[0]["end"]) == (2, 4)


def test_no_citation_at_all(files_and_counts):
    files, counts = files_and_counts
    assert cite("This requirement has no evidence whatsoever.", files, counts) == []


def test_version_numbers_are_not_citations(files_and_counts):
    files, counts = files_and_counts
    assert cite("SQL Server 2019 and bug 2231 and ROUND(x, 2)", files, counts) == []

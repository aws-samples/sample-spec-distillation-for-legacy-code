# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Structural checks on the spec document (exact layer)."""
from __future__ import annotations

import score


def write(tmp_path, text, name="spec.md"):
    p = tmp_path / name
    p.write_text(text)
    return p


def struct(tmp_path, text, key_ids=frozenset(), segment="requirements"):
    p = write(tmp_path, text)
    findings = score.load_findings(p, segment)
    files = {}
    counts = {}
    for f in findings:
        f["citations"] = score.extract_citations(f, files, counts, None, [])
    return score.structural_checks(p, findings, set(key_ids))


def test_duplicate_requirement_ids(tmp_path):
    text = ("#### [RL-0001] First definition\n\nSome requirement body text here.\n\n"
            "#### [RL-0001] Second definition of the same id\n\nMore body text here.\n")
    st = struct(tmp_path, text)
    assert st["duplicate_requirement_ids_n"] == 1
    dup = st["duplicate_requirement_ids"][0]
    assert dup["id"] == "RL-0001" and len(dup["defined_at_lines"]) == 2


def test_no_duplicates_when_ids_are_unique(tmp_path):
    text = ("#### [RL-0001] One\n\nBody text for the first requirement.\n\n"
            "#### [RL-0002] Two\n\nBody text for the second requirement.\n")
    assert struct(tmp_path, text)["duplicate_requirement_ids_n"] == 0


def test_unresolved_cross_reference(tmp_path):
    text = ("#### [RL-0001] One\n\nThis one depends on RL-0099, which does not exist.\n")
    st = struct(tmp_path, text)
    assert st["unresolved_cross_references_n"] == 1
    assert st["unresolved_cross_references"][0] == {"id": "RL-0099", "mentions": 1}


def test_resolved_cross_reference_is_not_flagged(tmp_path):
    text = ("#### [RL-0001] One\n\nSee also RL-0002 for the related rule.\n\n"
            "#### [RL-0002] Two\n\nBody text for the second requirement.\n")
    assert struct(tmp_path, text)["unresolved_cross_references_n"] == 0


def test_answer_key_ids_are_reported_separately_not_as_unresolved(tmp_path):
    text = "#### [RL-0001] One\n\nThis implements BR-018 from the key.\n"
    st = struct(tmp_path, text, key_ids={"BR-018"})
    assert st["unresolved_cross_references_n"] == 0
    assert st["answer_key_ids_mentioned"] == [{"id": "BR-018", "mentions": 1}]


def test_requirement_without_citation(tmp_path, src_tree):
    text = ("#### [RL-0001] Cited\n\nBody text here.\n\nEvidence: a.vb:L2-L4\n\n"
            "#### [RL-0002] Uncited\n\nBody text with no evidence line at all.\n")
    p = write(tmp_path, text)
    files = score.source_files(src_tree)
    counts = {k: score.line_count(v) for k, v in files.items()}
    findings = score.load_findings(p, "requirements")
    for f in findings:
        f["citations"] = score.extract_citations(f, files, counts, None,
                                                 score.literal_file_patterns(files))
    st = score.structural_checks(p, findings, set())
    assert st["requirements_without_citation"] == ["RL-0002"]
    assert st["requirements_without_citation_n"] == 1


def test_orphan_citations_are_counted(tmp_path, src_tree):
    text = ("Loose prose that cites a.vb:L2-L4 outside any requirement at all.\n\n"
            "#### [RL-0001] Proper\n\nBody text.\n\nEvidence: sub/b.sql:L1-L2\n")
    p = write(tmp_path, text)
    files = score.source_files(src_tree)
    counts = {k: score.line_count(v) for k, v in files.items()}
    findings = score.load_findings(p, "requirements")
    for f in findings:
        f["citations"] = score.extract_citations(f, files, counts, None,
                                                 score.literal_file_patterns(files))
    st = score.structural_checks(p, findings, set())
    assert st["orphan_citations"] == 1
    assert len(st["orphan_citation_findings"]) == 1


def test_heading_level_jump(tmp_path):
    st = struct(tmp_path, "# Top\n\nSome prose here now.\n\n#### Deep heading\n\nMore prose here.\n")
    assert st["malformed"]["heading_level_jumps_n"] == 1
    assert st["malformed"]["heading_level_jumps"][0]["from"] == 1
    assert st["malformed"]["heading_level_jumps"][0]["to"] == 4


def test_empty_section(tmp_path):
    st = struct(tmp_path, "## Alpha\n### Beta\n\nSome prose in beta here.\n")
    assert st["malformed"]["empty_sections_n"] == 1


def test_unclosed_code_fence(tmp_path):
    st = struct(tmp_path, "## A\n\n```\nunterminated code block here\n")
    assert st["malformed"]["unclosed_code_fence"] is True


def test_closed_code_fence(tmp_path):
    st = struct(tmp_path, "## A\n\n```\ncode here\n```\n\nprose after the block.\n")
    assert st["malformed"]["unclosed_code_fence"] is False


def test_duplicate_headings(tmp_path):
    st = struct(tmp_path, "## Rules\n\nprose one here.\n\n## Rules\n\nprose two here.\n")
    assert st["malformed"]["duplicate_headings_n"] == 1


def test_thin_requirement(tmp_path):
    text = "#### [RL-0001] Tiny\n\nToo short.\n\n#### [RL-0002] Fine\n\n" + \
           "This requirement has plenty of real words in its body text for sure.\n"
    st = struct(tmp_path, text)
    assert st["thin_requirements"] == ["RL-0001"]


def test_index_entry_without_a_definition(tmp_path):
    text = ("| ID | Requirement | Evidence |\n|---|---|---|\n"
            "| RL-0001 | Defined later | a.vb:L1-L2 |\n"
            "| RL-0404 | Never defined | a.vb:L3-L4 |\n\n"
            "#### [RL-0001] Defined later\n\nBody text here for real.\n")
    p = write(tmp_path, text)
    fs = score.load_findings(p, "requirements")
    for f in fs:
        f["citations"] = []
    st = score.structural_checks(p, fs, set())
    # RL-0404 appears only in the index: reported, and never silently dropped
    assert st["index_entries_without_definition"] == ["RL-0404"]
    assert any(f["kind"] == "index_only" and f["req_id"] == "RL-0404" for f in fs)


def test_code_fence_contents_are_not_scanned_for_ids(tmp_path):
    text = ("#### [RL-0001] One\n\nBody.\n\n```\nREQUIRE RL-0099 == 1\n```\n")
    st = struct(tmp_path, text)
    assert st["unresolved_cross_references_n"] == 0


def test_spec_with_no_ids_reports_zero_requirements(tmp_path):
    st = struct(tmp_path, "## Rules\n\n- A plain bullet with several real words.\n", segment="auto")
    assert st["requirement_ids_defined"] == 0
    assert st["requirement_units"] == 0
    assert st["orphan_citations"] is None  # not applicable without requirement units
    assert st["orphan_citations_applicable"] is False


def test_orphan_citations_are_not_counted_without_requirement_units(tmp_path, src_tree):
    """Under --segment auto nothing is a requirement unit, so every citation would look
    like an orphan. That is a measurement artifact, not a defect in the spec."""
    text = "#### [RL-0001] Proper\n\nBody text here for real.\n\nEvidence: a.vb:L2-L4\n"
    p = write(tmp_path, text)
    files = score.source_files(src_tree)
    counts = {k: score.line_count(v) for k, v in files.items()}
    out = {}
    for mode in ("auto", "requirements"):
        fs = score.load_findings(p, mode)
        for f in fs:
            f["citations"] = score.extract_citations(f, files, counts, None, [])
        out[mode] = score.structural_checks(p, fs, set())
    assert out["requirements"]["orphan_citations_applicable"] is True
    assert out["requirements"]["orphan_citations"] == 0
    assert out["auto"]["orphan_citations_applicable"] is False
    assert out["auto"]["orphan_citations"] is None

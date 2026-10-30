# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Encoding and line-ending exactness.

Line numbers are the unit of every citation, so the line counter has to agree
with what a human sees in an editor -- not with ``str.splitlines()``, which
breaks on form feeds and other control characters that no editor treats as a
line break.
"""
from __future__ import annotations

import score


def mk(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_lf_line_count(tmp_path):
    assert score.line_count(mk(tmp_path, "a.vb", b"1\n2\n3\n")) == 3


def test_no_trailing_newline(tmp_path):
    assert score.line_count(mk(tmp_path, "a.vb", b"1\n2\n3")) == 3


def test_crlf_line_count(tmp_path):
    assert score.line_count(mk(tmp_path, "a.vb", b"1\r\n2\r\n3\r\n")) == 3


def test_cr_only_line_count(tmp_path):
    assert score.line_count(mk(tmp_path, "a.vb", b"1\r2\r3\r")) == 3


def test_bom_does_not_add_a_line(tmp_path):
    assert score.line_count(mk(tmp_path, "a.vb", b"\xef\xbb\xbf1\n2\n3\n")) == 3


def test_bom_is_stripped_from_the_first_line(tmp_path):
    text = score.read_source_text(mk(tmp_path, "a.vb", b"\xef\xbb\xbfIf x Then\n"))
    assert text.startswith("If x Then")


def test_form_feed_is_not_a_line_break(tmp_path):
    """Legacy COBOL and VB listings use \\f as a page break. splitlines() would
    count 3 lines here and shift every citation after it by one."""
    p = mk(tmp_path, "a.cob", b"line one\n\x0cline two\nline three\n")
    assert score.line_count(p) == 3
    assert len(score.read_source_text(p).splitlines()) == 4  # the trap we avoid


def test_other_unicode_separators_are_not_line_breaks(tmp_path):
    body = "a b c\x0b d\x1c e\n second line\n"
    p = mk(tmp_path, "a.cob", body.encode())
    assert score.line_count(p) == 2


def test_multibyte_content_counts_lines_not_bytes(tmp_path):
    p = mk(tmp_path, "a.vb", "' Überprüfung\n' 日本語のコメント\n' emoji \U0001f600\n".encode())
    assert score.line_count(p) == 3


def test_invalid_utf8_is_replaced_not_fatal(tmp_path):
    p = mk(tmp_path, "a.vb", b"ok line\n\xff\xfe bad bytes\nthird\n")
    assert score.line_count(p) == 3
    assert "�" in score.read_source_text(p)


def test_numbered_source_uses_the_same_splitter(tmp_path):
    p = mk(tmp_path, "a.cob", b"alpha\n\x0cbeta\ngamma\n")
    files = {"a.cob": p}
    text = score.number_source(files, 10 ** 9)
    assert "    1  alpha" in text
    assert "    2  \x0cbeta" in text or "    2  \x0c" in text.replace("beta", "")
    assert "    3  gamma" in text
    assert "    4" not in text


def test_citation_bounds_use_the_exact_count(tmp_path):
    root = tmp_path / "src"
    mk(root, "a.cob", b"one\n\x0ctwo\nthree\n")
    files = score.source_files(root)
    counts = {k: score.line_count(v) for k, v in files.items()}
    assert counts["a.cob"] == 3
    good = score.extract_citations({"text": "a.cob:L1-L3", "context_file": None},
                                   files, counts, None, [])[0]
    bad = score.extract_citations({"text": "a.cob:L4", "context_file": None},
                                  files, counts, None, [])[0]
    assert good["valid"] and bad["category"] == score.CIT_PHANTOM


def test_crlf_spec_citations_parse(tmp_path, src_tree):
    spec = tmp_path / "spec.md"
    spec.write_bytes(b"\xef\xbb\xbf## Rules\r\n\r\n"
                     b"- A rule about posted invoices requiring approval. a.vb:L2-L4\r\n")
    files = score.source_files(src_tree)
    counts = {k: score.line_count(v) for k, v in files.items()}
    fs = score.load_findings(spec, "auto")
    cits = score.extract_citations(fs[0], files, counts, None, [])
    assert (cits[0]["resolved"], cits[0]["start"], cits[0]["end"]) == ("a.vb", 2, 4)


def test_multibyte_spec_text_is_segmented(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("## Règles\n\n- Les factures supérieures au seuil nécessitent approbation.\n")
    fs = score.load_findings(spec, "auto")
    assert len(fs) == 1 and "factures" in fs[0]["text"]

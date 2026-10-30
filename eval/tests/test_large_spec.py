# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Performance and memory behaviour on a 50 MB spec.

Marked ``slow``. Run with ``-m slow`` or plain (it is not deselected by default);
skip with ``-m 'not slow'``.
"""
from __future__ import annotations

import time

import pytest

import score

ENTRY = """#### [RL-{n:06d}] Requirement number {n}

**Requirement:** The system SHALL enforce condition {n} on every invoice before posting.
- Rationale: condition {n} protects the ledger from entry mistakes of class {n}.
- If violated: the vendor could be paid an amount unsupported by the job detail.
- Conditions: enforced client side and again in the stored procedure for safety.
- Acceptance: given an invoice, when it is validated, then condition {n} MUST hold.
```formal
REQUIRE condition_{n}(invoice) == TRUE
```

Evidence: a.vb:L{a}-L{b}

"""

TARGET_BYTES = 50 * 1024 * 1024


@pytest.fixture(scope="module")
def huge_spec(tmp_path_factory):
    p = tmp_path_factory.mktemp("huge") / "spec.md"
    written = 0
    n = 0
    with open(p, "w") as fh:
        fh.write("# Huge Spec\n\nThis document specifies very many requirements.\n\n"
                 "## Business Rules\n\n")
        while written < TARGET_BYTES:
            n += 1
            chunk = ENTRY.format(n=n, a=(n % 9) + 1, b=(n % 9) + 2)
            fh.write(chunk)
            written += len(chunk)
    return p, n


@pytest.mark.slow
def test_fifty_megabyte_spec_segments_in_reasonable_time(huge_spec):
    p, n = huge_spec
    assert p.stat().st_size >= TARGET_BYTES
    t0 = time.time()
    findings = score.load_findings(p, "requirements")
    elapsed = time.time() - t0
    reqs = [f for f in findings if f["kind"] == "requirement"]
    assert len(reqs) == n
    assert elapsed < 180, f"segmentation took {elapsed:.1f}s"


@pytest.mark.slow
def test_fifty_megabyte_spec_is_read_streamingly(huge_spec):
    """iter_spec_lines must not materialise the file: reading the first few lines
    of a 50 MB spec has to be instant."""
    p, _ = huge_spec
    t0 = time.time()
    it = score.iter_spec_lines(p)
    head = [next(it) for _ in range(5)]
    it.close()
    assert head[0] == "# Huge Spec"
    assert time.time() - t0 < 2.0


@pytest.mark.slow
def test_fifty_megabyte_spec_scores_end_to_end(huge_spec, src_tree, mini_key):
    p, n = huge_spec
    t0 = time.time()
    report, _ = score.score(p, mini_key, src_tree, False, None, None, segment="requirements",
                            max_report_findings=50)
    elapsed = time.time() - t0
    assert report["counts"]["findings"] >= n
    assert report["counts"]["citations"] >= n
    assert report["citations"]["validity_pct"] == 100.0
    # the JSON payload stays small even though every finding was measured
    assert report["findings_reported"] == 50
    assert report["findings_total"] == report["counts"]["findings"]
    assert elapsed < 600, f"full score took {elapsed:.1f}s"


@pytest.mark.slow
def test_finding_text_cap_bounds_memory(huge_spec):
    p, _ = huge_spec
    findings = score.load_findings(p, "requirements", 400)
    assert all(len(f["text"]) <= 400 + 120 for f in findings)
    # even at a tiny cap the trailing Evidence line survives, so citations are kept
    cited = [f for f in findings if "a.vb:L" in f["text"]]
    assert len(cited) == sum(1 for f in findings if f["kind"] == "requirement")


def test_wide_single_line_spec_does_not_blow_up(tmp_path, src_tree, mini_key):
    """One 8 MB line: the accumulator must not be O(n^2) string concatenation."""
    p = tmp_path / "wide.md"
    p.write_text("## Rules\n\n- " + ("invoice approval threshold detail " * 250_000) + " a.vb:L1-L2\n")
    t0 = time.time()
    report, _ = score.score(p, mini_key, src_tree, False, None, None)
    assert time.time() - t0 < 60
    assert report["counts"]["findings"] == 1


def test_many_tiny_findings(tmp_path, src_tree, mini_key):
    p = tmp_path / "many.md"
    p.write_text("## Rules\n\n" + "".join(
        f"- Invoice rule number {i} about approval thresholds. a.vb:L1-L2\n" for i in range(20_000)))
    t0 = time.time()
    report, _ = score.score(p, mini_key, src_tree, False, None, None, max_report_findings=10)
    assert time.time() - t0 < 120
    assert report["counts"]["findings"] == 20_000
    assert report["counts"]["citations"] == 20_000
    assert report["findings_reported"] == 10

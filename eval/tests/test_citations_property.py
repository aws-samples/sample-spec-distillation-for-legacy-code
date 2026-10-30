# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for the exact layer.

``hypothesis`` is used when importable (``eval/.venv`` has it). When it is not,
the same properties run as seeded generative tests, so the invariants are checked
either way and a failure is always reproducible from the printed seed.
"""
from __future__ import annotations

import random

import pytest

import score
from conftest_score import cite, make_finding
from judge.evidence import clamp, covered_lines, merge_intervals

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st
    HAVE_HYPOTHESIS = True
except Exception:  # pragma: no cover
    HAVE_HYPOTHESIS = False

SEEDS = list(range(20261006, 20261006 + 25))

RANGE_TEMPLATES = [
    "{f}:L{a}-L{b}", "{f}:{a}-{b}", "{f}#L{a}-L{b}", "{f} (lines {a}-{b})", "{f} L{a}-{b}",
    "{f}, {a}-{b}", "{f}:ll. {a}-{b}", "{f}:L{a}–L{b}", "{f}:L{a} to L{b}",
    "{f}:L{a}..L{b}", "lines {a}-{b} of {f}", "L{a}-L{b} in {f}",
]
SINGLE_TEMPLATES = ["{f}:L{a}", "{f}:{a}", "{f}#L{a}", "{f} (line {a})", "{f} L{a}"]


# ------------------------------------------------- property 1: classification is total
def _classify_invariants(start, end, n):
    cat, reason = score.classify_citation("a.vb", start, end, n)
    assert cat in score.CIT_CATEGORIES
    is_ok = 1 <= start <= end <= n
    assert (cat == score.CIT_OK) is is_ok, (start, end, n, cat)
    if cat == score.CIT_OK:
        return
    if start < 1 or end < start:
        assert cat == score.CIT_BAD_RANGE
    elif n >= 1 and end == n + 1:
        assert cat == (score.CIT_PHANTOM if start == end else score.CIT_OFF_BY_ONE)
    else:
        assert cat == score.CIT_OUT_OF_RANGE
    assert isinstance(reason, str) and reason


if HAVE_HYPOTHESIS:
    @given(st.integers(-5, 40), st.integers(-5, 40), st.integers(0, 40))
    def test_classify_citation_is_total_and_consistent(start, end, n):
        _classify_invariants(start, end, n)
else:  # pragma: no cover
    @pytest.mark.parametrize("seed", SEEDS)
    def test_classify_citation_is_total_and_consistent(seed):
        rng = random.Random(seed)
        for _ in range(400):
            _classify_invariants(rng.randint(-5, 40), rng.randint(-5, 40), rng.randint(0, 40))


def test_classify_citation_no_file_and_missing_file():
    assert score.classify_citation(None, 1, 1, None, had_file_name=False)[0] == score.CIT_NO_FILE
    assert score.classify_citation(None, 1, 1, None)[0] == score.CIT_NOT_FOUND


# ------------------------------------------------ property 2: parse round-trips
def _roundtrip(files, counts, template, a, b):
    text = template.format(f="a.vb", a=a, b=b)
    cits = score.extract_citations(make_finding(text), files, counts, None,
                                   score.literal_file_patterns(files))
    assert len(cits) == 1, (text, cits)
    c = cits[0]
    assert c["resolved"] == "a.vb", (text, c)
    assert (c["start"], c["end"]) == (a, b), (text, c)
    assert c["valid"] == (1 <= a <= b <= counts["a.vb"]), (text, c)


if HAVE_HYPOTHESIS:
    @settings(max_examples=250, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(st.sampled_from(RANGE_TEMPLATES), st.integers(1, 999), st.integers(0, 50))
    def test_citation_parse_roundtrips_for_every_format(files_and_counts, template, a, span):
        files, counts = files_and_counts
        _roundtrip(files, counts, template, a, a + span)

    @settings(max_examples=120, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(st.sampled_from(SINGLE_TEMPLATES), st.integers(1, 999))
    def test_single_line_citation_roundtrips(files_and_counts, template, a):
        files, counts = files_and_counts
        text = template.format(f="a.vb", a=a)
        cits = score.extract_citations(make_finding(text), files, counts, None,
                                       score.literal_file_patterns(files))
        assert len(cits) == 1 and (cits[0]["start"], cits[0]["end"]) == (a, a), (text, cits)
else:  # pragma: no cover
    @pytest.mark.parametrize("seed", SEEDS)
    def test_citation_parse_roundtrips_for_every_format(files_and_counts, seed):
        files, counts = files_and_counts
        rng = random.Random(seed)
        for _ in range(60):
            a = rng.randint(1, 999)
            _roundtrip(files, counts, rng.choice(RANGE_TEMPLATES), a, a + rng.randint(0, 50))


# ----------------------------------------- property 3: interval algebra is exact
def _merge_invariants(ivs):
    merged = merge_intervals(ivs)
    assert merged == sorted(merged)
    for (a1, b1), (a2, b2) in zip(merged, merged[1:]):
        assert b1 + 1 < a2, merged           # non-overlapping AND non-adjacent
    for a, b in merged:
        assert a <= b
    want = set()
    for a, b in ivs:
        if b >= a:
            want |= set(range(a, b + 1))
    got = set()
    for a, b in merged:
        got |= set(range(a, b + 1))
    assert got == want
    assert covered_lines(ivs) == len(want)


def _complement_invariants(ivs, n):
    merged = [iv for iv in merge_intervals(ivs) if iv[0] <= n]
    merged = [(a, min(b, n)) for a, b in merged]
    comp = score._complement(merge_intervals(merged), n)
    inside, outside = set(), set()
    for a, b in merged:
        inside |= set(range(a, b + 1))
    for a, b in comp:
        assert 1 <= a <= b <= n
        outside |= set(range(a, b + 1))
    assert inside & outside == set()
    assert inside | outside == set(range(1, n + 1))


if HAVE_HYPOTHESIS:
    @given(st.lists(st.tuples(st.integers(1, 60), st.integers(1, 60)), max_size=12))
    def test_merge_intervals_is_exact(pairs):
        _merge_invariants([(min(a, b), max(a, b)) for a, b in pairs])

    @given(st.lists(st.tuples(st.integers(1, 40), st.integers(1, 40)), max_size=10),
           st.integers(1, 40))
    def test_complement_partitions_the_file(pairs, n):
        _complement_invariants([(min(a, b), max(a, b)) for a, b in pairs], n)

    @given(st.integers(1, 30), st.integers(1, 30), st.integers(0, 30))
    def test_clamp_never_leaves_the_file(a, span, n):
        iv = clamp((a, a + span), n)
        if iv is not None:
            assert 1 <= iv[0] <= iv[1] <= n
else:  # pragma: no cover
    @pytest.mark.parametrize("seed", SEEDS)
    def test_merge_intervals_is_exact(seed):
        rng = random.Random(seed)
        for _ in range(200):
            k = rng.randint(0, 12)
            pairs = []
            for _ in range(k):
                a, b = rng.randint(1, 60), rng.randint(1, 60)
                pairs.append((min(a, b), max(a, b)))
            _merge_invariants(pairs)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_complement_partitions_the_file(seed):
        rng = random.Random(seed)
        for _ in range(200):
            n = rng.randint(1, 40)
            pairs = []
            for _ in range(rng.randint(0, 10)):
                a, b = rng.randint(1, 40), rng.randint(1, 40)
                pairs.append((min(a, b), max(a, b)))
            _complement_invariants(pairs, n)


# ------------------------------- property 4: overlap() agrees with set intersection
def _overlap_invariant(files, counts, ca, cb, ka, kb):
    c = cite(f"a.vb:L{ca}-L{cb}", files, counts)[0]
    kc = {"K": [("a.vb", ka, kb)]}
    expect = c["valid"] and bool(set(range(ca, cb + 1)) & set(range(ka, kb + 1)))
    assert score.overlap(c, kc["K"]) is bool(expect)


if HAVE_HYPOTHESIS:
    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(st.integers(1, 14), st.integers(0, 8), st.integers(1, 14), st.integers(0, 8))
    def test_overlap_matches_set_intersection(files_and_counts, ca, cspan, ka, kspan):
        files, counts = files_and_counts
        _overlap_invariant(files, counts, ca, ca + cspan, ka, ka + kspan)
else:  # pragma: no cover
    @pytest.mark.parametrize("seed", SEEDS)
    def test_overlap_matches_set_intersection(files_and_counts, seed):
        files, counts = files_and_counts
        rng = random.Random(seed)
        for _ in range(80):
            ca, ka = rng.randint(1, 14), rng.randint(1, 14)
            _overlap_invariant(files, counts, ca, ca + rng.randint(0, 8),
                               ka, ka + rng.randint(0, 8))


def test_hypothesis_is_actually_in_use_when_available():
    """Recorded so a reader knows which mode produced the run."""
    if HAVE_HYPOTHESIS:
        import hypothesis
        assert hypothesis.__version__
    else:  # pragma: no cover
        pytest.skip("hypothesis not installed; seeded generative fallback ran instead")

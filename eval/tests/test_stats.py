# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bootstrap CIs and kappa, checked against hand-computable cases."""
from __future__ import annotations

import pytest

from judge.stats import (bootstrap_proportion_ci, bootstrap_weighted_ci, cohen_kappa,
                         fleiss_kappa, kappa_label, mean_pairwise_cohen, median, spread)


def test_median():
    assert median([1, 2, 3]) == 2.0
    assert median([1, 2, 3, 4]) == 2.5
    assert median([]) is None
    assert median([5, None, 1]) == 3.0      # None values are dropped, not treated as 0


def test_bootstrap_on_all_true_is_a_point():
    ci = bootstrap_proportion_ci([True] * 20, iters=200)
    assert ci["point"] == ci["lo"] == ci["hi"] == 100.0


def test_bootstrap_on_all_false_is_a_point():
    ci = bootstrap_proportion_ci([False] * 20, iters=200)
    assert ci["point"] == ci["lo"] == ci["hi"] == 0.0


def test_bootstrap_brackets_the_point_estimate():
    flags = [True] * 43 + [False] * 5          # 89.6% of 48, the demo figure
    ci = bootstrap_proportion_ci(flags, iters=4000)
    assert ci["point"] == 89.6
    assert ci["lo"] < ci["point"] < ci["hi"]
    assert 70 < ci["lo"] and ci["hi"] <= 100.0


def test_bootstrap_is_seeded_and_reproducible():
    flags = [True] * 30 + [False] * 18
    a = bootstrap_proportion_ci(flags, iters=500, seed=7)
    b = bootstrap_proportion_ci(flags, iters=500, seed=7)
    c = bootstrap_proportion_ci(flags, iters=500, seed=8)
    assert a == b
    assert (a["lo"], a["hi"]) != (c["lo"], c["hi"]) or a["point"] == c["point"]


def test_bootstrap_narrows_with_more_items():
    small = bootstrap_proportion_ci([True] * 9 + [False], iters=3000)
    large = bootstrap_proportion_ci([True] * 900 + [False] * 100, iters=3000)
    assert (small["hi"] - small["lo"]) > (large["hi"] - large["lo"])


def test_bootstrap_empty():
    ci = bootstrap_proportion_ci([], iters=10)
    assert ci == {"point": None, "lo": None, "hi": None, "n": 0, "iters": 0}


def test_weighted_bootstrap_handles_partial_credit():
    ci = bootstrap_weighted_ci([1.0, 0.5, 0.0, 1.0], iters=500)
    assert ci["point"] == 62.5


# ----------------------------------------------------------------------- kappa
def test_cohen_kappa_perfect():
    assert cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"], ["a", "b"]) == 1.0


def test_cohen_kappa_total_disagreement_is_negative():
    k = cohen_kappa(["a", "a", "b", "b"], ["b", "b", "a", "a"], ["a", "b"])
    assert k == -1.0


def test_cohen_kappa_textbook_value():
    """2x2 table: both=yes 20, both=no 15, a-only 5, b-only 10.
    po = 35/50 = 0.70; pe = (25/50)(30/50) + (25/50)(20/50) = 0.30 + 0.20 = 0.50;
    kappa = 0.20/0.50 = 0.40."""
    a = ["y"] * 20 + ["n"] * 15 + ["y"] * 5 + ["n"] * 10
    b = ["y"] * 20 + ["n"] * 15 + ["n"] * 5 + ["y"] * 10
    assert cohen_kappa(a, b, ["y", "n"]) == pytest.approx(0.40, abs=1e-4)


def test_cohen_kappa_undefined_when_one_category():
    assert cohen_kappa(["a", "a"], ["a", "a"], ["a"]) is None


def test_cohen_kappa_length_mismatch():
    assert cohen_kappa(["a"], ["a", "b"]) is None


def test_fleiss_kappa_perfect():
    ratings = [["a"] * 3, ["b"] * 3, ["a"] * 3, ["b"] * 3]
    assert fleiss_kappa(ratings, ["a", "b"]) == 1.0


def test_fleiss_kappa_random_is_near_zero():
    # 4 subjects, 2 raters, maximal disagreement with balanced marginals
    ratings = [["a", "b"], ["b", "a"], ["a", "b"], ["b", "a"]]
    assert fleiss_kappa(ratings, ["a", "b"]) == -1.0


def test_fleiss_kappa_matches_cohen_for_two_raters():
    ratings = [["y", "y"]] * 20 + [["n", "n"]] * 15 + [["y", "n"]] * 5 + [["n", "y"]] * 10
    f = fleiss_kappa(ratings, ["y", "n"])
    c = cohen_kappa([r[0] for r in ratings], [r[1] for r in ratings], ["y", "n"])
    assert f == pytest.approx(c, abs=0.02)


def test_fleiss_kappa_undefined_cases():
    assert fleiss_kappa([]) is None
    assert fleiss_kappa([["a"], ["b"]]) is None                 # one rater
    assert fleiss_kappa([["a", "a"], ["a", "a"]], ["a"]) is None  # one category
    assert fleiss_kappa([["a", "b"], ["a"]]) is None            # ragged


def test_mean_pairwise_cohen():
    ratings = [["a", "a", "a"], ["b", "b", "b"], ["a", "a", "b"]]
    k = mean_pairwise_cohen(ratings, ["a", "b"])
    assert k is not None and -1.0 <= k <= 1.0


def test_kappa_labels():
    assert kappa_label(None) == "undefined"
    assert kappa_label(-0.1) == "worse than chance"
    assert kappa_label(0.1) == "slight"
    assert kappa_label(0.5) == "moderate"
    assert kappa_label(0.9) == "almost perfect"
    assert kappa_label(1.0) == "almost perfect"


def test_spread():
    s = spread([70.8, 72.9, 70.8])
    assert s["min"] == 70.8 and s["max"] == 72.9 and s["median"] == 70.8
    assert spread([None, None])["median"] is None

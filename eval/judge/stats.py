# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Uncertainty arithmetic for the judged layer.

Three separate things get reported, and they are not interchangeable:

* **across-run spread** -- min / median / max of a metric over N judge passes.
  This is judge noise, measured, not assumed.
* **bootstrap 95% CI over key items / findings** -- how much the metric would
  move on a different sample of the same size. This is the "48 items is a small
  key" uncertainty, and it does not shrink by judging more times.
* **kappa** -- chance-corrected agreement between judge passes. Fleiss for
  N >= 2 raters, Cohen pairwise.

All randomness is seeded, so two runs of the scorer print the same CI.
"""
from __future__ import annotations

import random
from collections import Counter

DEFAULT_SEED = 20261006
DEFAULT_ITERS = 2000


def median(xs):
    """Median of a numeric sequence; None when empty. Even length averages the middle two."""
    vals = sorted(v for v in xs if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def percentile(sorted_vals, q: float):
    """Nearest-rank percentile of an already-sorted list; q in [0, 1]."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return float(sorted_vals[idx])


def bootstrap_proportion_ci(flags, *, iters: int = DEFAULT_ITERS, seed: int = DEFAULT_SEED,
                            alpha: float = 0.05, scale: float = 100.0):
    """Percentile bootstrap CI for the share of truthy values in ``flags``.

    ``flags`` is the per-unit outcome (one entry per key item for recall, one per
    finding for precision). Resampling units -- not runs -- so this answers
    "how stable is this number on a key of this size".
    """
    n = len(flags)
    if n == 0:
        return {"point": None, "lo": None, "hi": None, "n": 0, "iters": 0}
    vals = [1.0 if f else 0.0 for f in flags]
    point = scale * sum(vals) / n
    rng = random.Random(seed)
    samples = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            s += vals[rng.randrange(n)]
        samples.append(scale * s / n)
    samples.sort()
    return {"point": round(point, 1),
            "lo": round(percentile(samples, alpha / 2), 1),
            "hi": round(percentile(samples, 1 - alpha / 2), 1),
            "n": n, "iters": iters}


def bootstrap_weighted_ci(weights, *, iters: int = DEFAULT_ITERS, seed: int = DEFAULT_SEED,
                          alpha: float = 0.05, scale: float = 100.0):
    """As above but for a mean of per-unit weights (e.g. partial credit = 0.5)."""
    n = len(weights)
    if n == 0:
        return {"point": None, "lo": None, "hi": None, "n": 0, "iters": 0}
    vals = [float(w) for w in weights]
    point = scale * sum(vals) / n
    rng = random.Random(seed)
    samples = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            s += vals[rng.randrange(n)]
        samples.append(scale * s / n)
    samples.sort()
    return {"point": round(point, 1),
            "lo": round(percentile(samples, alpha / 2), 1),
            "hi": round(percentile(samples, 1 - alpha / 2), 1),
            "n": n, "iters": iters}


# ------------------------------------------------------------------------ kappa
def fleiss_kappa(ratings, categories=None):
    """Fleiss' kappa over ``ratings`` = list of per-subject label lists.

    Every subject must have the same number of raters (here: judge passes).
    Returns None when kappa is undefined: fewer than 2 raters, no subjects, or
    perfect agreement on a single category (expected agreement == 1).
    """
    rows = [list(r) for r in ratings if r]
    if not rows:
        return None
    n_raters = len(rows[0])
    if n_raters < 2 or any(len(r) != n_raters for r in rows):
        return None
    cats = sorted(categories) if categories else sorted({c for r in rows for c in r})
    if len(cats) < 2:
        return None  # one category used everywhere: agreement is trivially total
    n_subjects = len(rows)
    idx = {c: i for i, c in enumerate(cats)}
    counts = []
    for r in rows:
        row = [0] * len(cats)
        for c in r:
            row[idx[c]] += 1
        counts.append(row)
    # observed agreement
    p_i = [(sum(x * x for x in row) - n_raters) / (n_raters * (n_raters - 1)) for row in counts]
    p_bar = sum(p_i) / n_subjects
    # expected agreement
    p_j = [sum(row[j] for row in counts) / (n_subjects * n_raters) for j in range(len(cats))]
    p_e = sum(x * x for x in p_j)
    if abs(1.0 - p_e) < 1e-12:
        return None
    return round((p_bar - p_e) / (1.0 - p_e), 4)


def cohen_kappa(a, b, categories=None):
    """Cohen's kappa for two raters over paired labels. None when undefined."""
    if len(a) != len(b) or not a:
        return None
    cats = sorted(categories) if categories else sorted(set(a) | set(b))
    if len(cats) < 2:
        return None
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[c] / n) * (cb[c] / n) for c in cats)
    if abs(1.0 - pe) < 1e-12:
        return None
    return round((po - pe) / (1.0 - pe), 4)


def mean_pairwise_cohen(ratings, categories=None):
    """Mean Cohen's kappa over every pair of raters. ``ratings`` as for Fleiss."""
    rows = [list(r) for r in ratings if r]
    if not rows:
        return None
    n_raters = len(rows[0])
    if n_raters < 2 or any(len(r) != n_raters for r in rows):
        return None
    ks = []
    for i in range(n_raters):
        for j in range(i + 1, n_raters):
            k = cohen_kappa([r[i] for r in rows], [r[j] for r in rows], categories)
            if k is not None:
                ks.append(k)
    return round(sum(ks) / len(ks), 4) if ks else None


def kappa_label(k) -> str:
    """Landis & Koch bands, for a reader who does not carry them in their head."""
    if k is None:
        return "undefined"
    if k < 0:
        return "worse than chance"
    for bound, name in ((0.20, "slight"), (0.40, "fair"), (0.60, "moderate"),
                        (0.80, "substantial"), (1.01, "almost perfect")):
        if k < bound:
            return name
    return "almost perfect"


def spread(values):
    """min / median / max of a metric across judge passes."""
    vals = [v for v in values if v is not None]
    if not vals:
        return {"min": None, "median": None, "max": None, "runs": list(values)}
    return {"min": round(min(vals), 1), "median": round(median(vals), 1),
            "max": round(max(vals), 1), "runs": list(values)}

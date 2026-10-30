# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bounded, cited source excerpts for the judge.

The old judge saw one ~200K-character dump of every source file on every call.
That is expensive, it pushes the findings to the far end of a huge context, and
it makes the prompt sensitive to unrelated files. Here each batch of findings
gets only:

  * the lines its own findings cite, plus a few lines of context  (priority 0)
  * the key locations of the key items lexically closest to the batch (priority 1)

Everything is merged, deduplicated, ordered deterministically and truncated to a
character budget. The exact intervals sent are recorded in a manifest so a
verdict can be traced back to what the judge could actually see.
"""
from __future__ import annotations

from pathlib import Path

Interval = tuple[int, int]


# --------------------------------------------------------------------- intervals
def merge_intervals(ivs) -> list[Interval]:
    """Merge inclusive 1-based intervals. Adjacent ranges (b, b+1) merge too.

    Exact and total: the output is sorted, non-overlapping, non-adjacent, and
    covers exactly the same set of line numbers as the input.
    """
    cleaned = [(int(a), int(b)) for a, b in ivs if int(b) >= int(a)]
    if not cleaned:
        return []
    cleaned.sort()
    out = [list(cleaned[0])]
    for a, b in cleaned[1:]:
        if a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def clamp(iv: Interval, n_lines: int) -> Interval | None:
    """Clip an interval to a file of ``n_lines`` lines; None if nothing is left."""
    a, b = max(1, int(iv[0])), min(int(iv[1]), n_lines)
    return (a, b) if n_lines >= 1 and a <= b else None


def expand(iv: Interval, context: int, n_lines: int) -> Interval | None:
    return clamp((iv[0] - context, iv[1] + context), n_lines)


def covered_lines(ivs) -> int:
    return sum(b - a + 1 for a, b in merge_intervals(ivs))


# ------------------------------------------------------------------- selection
def select_key_items(findings, key_items, tokenize, *, top_k: int = 12,
                     min_score: float = 0.15) -> list[str]:
    """Key items whose statements are lexically closest to a batch of findings.

    Every key *statement* is always in the prompt (they are short); this picks
    whose *source lines* are worth shipping as well, so the judge can tell
    "true but not in the key" from "contradicts the code".

    Deterministic: ties break on key id.
    """
    ftoks = [tokenize(f.get("text", "")) for f in findings]
    scored = []
    for k in key_items:
        kt = tokenize(k.get("statement", ""))
        if not kt:
            continue
        best = max((len(kt & ft) / len(kt) for ft in ftoks), default=0.0)
        if best >= min_score:
            scored.append((-best, str(k["id"])))
    scored.sort()
    return [kid for _, kid in scored[:top_k]]


# --------------------------------------------------------------------- rendering
def _read_lines(path: Path, split) -> list[str]:
    return split(path.read_text(errors="replace"))


def render(files: dict[str, Path], counts: dict[str, int], requests, *,
           max_chars: int = 40000, split=None, cache: dict | None = None) -> tuple[str, dict]:
    """Render ``requests`` as a numbered source excerpt within ``max_chars``.

    ``requests`` is an iterable of ``(priority, rel_path, start, end)`` with
    1-based inclusive lines; lower priority ships first. ``split`` splits file
    text into lines and MUST be the same splitter the citation checker uses,
    otherwise the numbers the judge sees would not be the numbers that were
    verified.

    Returns ``(text, manifest)``. The manifest lists, per file, the intervals
    actually included and the ones dropped for budget, plus total characters.
    """
    if split is None:
        raise ValueError("render() needs the scorer's line splitter; see score.split_lines_exact")
    cache = cache if cache is not None else {}

    # priority -> file -> intervals, clipped to the file and merged
    per_prio: dict[int, dict[str, list[Interval]]] = {}
    for prio, rel, a, b in requests:
        if rel not in files:
            continue
        iv = clamp((a, b), counts.get(rel, 0))
        if iv:
            per_prio.setdefault(int(prio), {}).setdefault(rel, []).append(iv)

    # Drop from a lower-priority band anything a higher-priority band already has,
    # so the same lines are never paid for twice.
    taken: dict[str, list[Interval]] = {}
    ordered: list[tuple[int, str, Interval]] = []
    for prio in sorted(per_prio):
        for rel in sorted(per_prio[prio]):
            merged = merge_intervals(per_prio[prio][rel])
            have = merge_intervals(taken.get(rel, []))
            for a, b in merged:
                for sa, sb in _subtract((a, b), have):
                    ordered.append((prio, rel, (sa, sb)))
            taken.setdefault(rel, []).extend(merged)

    included: dict[str, list[Interval]] = {}
    dropped: dict[str, list[Interval]] = {}
    budget = max_chars
    for prio, rel, (a, b) in ordered:
        if rel not in cache:
            cache[rel] = _read_lines(files[rel], split)
        lines = cache[rel]
        size = sum(len(lines[i - 1]) + 8 for i in range(a, min(b, len(lines)) + 1)) + 48
        if size <= budget:
            budget -= size
            included.setdefault(rel, []).append((a, b))
        else:
            dropped.setdefault(rel, []).append((a, b))

    parts, manifest_files = [], []
    for rel in sorted(included):
        ivs = merge_intervals(included[rel])
        lines = cache[rel]
        chunk = [f"=== {rel} (of {counts.get(rel, 0)} lines) ==="]
        prev_end = None
        for a, b in ivs:
            if prev_end is not None and a > prev_end + 1:
                chunk.append(f"     ... lines {prev_end + 1}-{a - 1} not shown ...")
            for i in range(a, min(b, len(lines)) + 1):
                chunk.append(f"{i:5d}  {lines[i - 1]}")
            prev_end = b
        parts.append("\n".join(chunk))
        manifest_files.append({"file": rel, "intervals": [list(x) for x in ivs],
                               "lines": covered_lines(ivs)})
    text = "\n\n".join(parts)
    manifest = {
        "chars": len(text),
        "max_chars": max_chars,
        "files": manifest_files,
        "dropped_for_budget": [{"file": r, "intervals": [list(x) for x in merge_intervals(v)]}
                               for r, v in sorted(dropped.items())],
        "truncated": bool(dropped),
    }
    return text, manifest


def _subtract(iv: Interval, blocks) -> list[Interval]:
    """``iv`` minus a merged, sorted list of intervals."""
    a, b = iv
    out = []
    for sa, sb in blocks:
        if sb < a:
            continue
        if sa > b:
            break
        if sa > a:
            out.append((a, sa - 1))
        a = max(a, sb + 1)
        if a > b:
            return out
    if a <= b:
        out.append((a, b))
    return out


def full_dump(files: dict[str, Path], max_chars: int, split) -> tuple[str, dict]:
    """Legacy behaviour: every file, whole, in path order, until the budget runs out.

    Kept behind ``--judge-evidence full`` so the chunked numbers can be compared
    against the published ones.
    """
    parts, total, used, omitted = [], 0, [], []
    for rel, p in sorted(files.items()):
        lines = split(p.read_text(errors="replace"))
        body = "\n".join(f"{i:5d}  {ln}" for i, ln in enumerate(lines, 1))
        chunk = f"=== {rel} ===\n{body}\n"
        total += len(chunk)
        if total > max_chars:
            parts.append(f"=== {rel} === (omitted: size limit)\n")
            omitted.append(rel)
            continue
        parts.append(chunk)
        used.append({"file": rel, "intervals": [[1, len(lines)]], "lines": len(lines)})
    text = "\n".join(parts)
    return text, {"chars": len(text), "max_chars": max_chars, "files": used,
                  "dropped_for_budget": [{"file": r, "intervals": []} for r in omitted],
                  "truncated": bool(omitted)}

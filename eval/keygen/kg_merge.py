# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MERGE / DEDUPE across sources, keeping the provenance of every contributor.

Two candidates are the same item when

* **harvest + harvest**: their locations overlap and their normalised statements are near
  identical (token overlap >= 0.85), i.e. two detectors (or the facts and regex paths) found the
  same thing;
* **tests -> harvest**: a test candidate joins the single best harvest item whose location lies
  inside the code the test reaches (the test, the system under test and two call hops from it)
  and whose statement shares the test's *behaviour* words (the test name minus its class name,
  stemmed): at least two of them, or half of a short name;
* **docs / llm-draft -> anything**: joins the best item with statement overlap >= 0.6 (docs cite
  markdown, so location overlap cannot be required).

A test or docs candidate never merges with more than one item, so a large test cannot chain
unrelated rules together. Merged items carry ``source`` = every contributor's tag,
``provenance`` = every contributor's statement and locations, and the ``corroborated`` flag when
two or more *different* sources (harvest / tests / docs / llm-draft) agree. Pure Python, no AI.
"""
from __future__ import annotations

import re

from kg_source import TYPE_PREFIX, Candidate, stable_hash

STOP = set("""a an the of to in on for by with and or is are be must not no at as from it its this that
these those when then than into via per any all each every has have had was were will can may only
should does do done using used use uses value values item items""".split())
GENERIC = {"guard", "against", "harvest", "protected", "behaviour", "asserts", "exercises", "assert",
           "equal", "throws", "database", "ef", "core", "configuration", "required", "provided", "null",
           "true", "false", "test", "tests", "returns", "method", "methods", "class"}
SOURCE_RANK = {"harvest": 0, "planted": 0, "tests": 1, "docs": 2, "llm-draft": 3}


def tokens(s: str) -> set[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    toks = re.findall(r"[a-z0-9]+", s.lower())
    return {t for t in toks if t not in STOP and len(t) > 1}


def content(s: str) -> set[str]:
    return {t for t in tokens(s) if t not in GENERIC}


def stem(t: str) -> str:
    for suf in ("ing", "es", "ed", "s"):
        if len(t) > len(suf) + 2 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def stems(s: str) -> set[str]:
    return {stem(t) for t in content(s)} - {"more", "new", "get", "set", "return", "one"}


def test_match(behaviour: set[str], harvest_stmt: str) -> float:
    """Share of a test's behaviour words (test name minus its class name) found in a harvest
    statement; 0 unless at least 2 words, or half of a short name, are shared."""
    if not behaviour:
        return 0.0
    shared = behaviour & stems(harvest_stmt)
    if len(shared) >= 2 or (shared and len(shared) / len(behaviour) >= 0.5):
        return len(shared) / len(behaviour)
    return 0.0


def overlap_coeff(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _span_overlap(a: dict, b: dict, slack: int = 0) -> bool:
    return (a["file"] == b["file"] and a["start_line"] - slack <= b["end_line"]
            and b["start_line"] - slack <= a["end_line"])


def locs_overlap(la: list[dict], lb: list[dict], slack: int = 0) -> bool:
    return any(_span_overlap(x, y, slack) for x in la for y in lb)


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def merge(cands: list[Candidate], doc_threshold: float = 0.6,
          harvest_threshold: float = 0.85) -> list[list[Candidate]]:
    """Clusters of candidates, each cluster one key item. Deterministic in input order."""
    n = len(cands)
    uf = _UF(n)
    toks = [content(c.statement) | {k.split(":", 1)[-1] for k in c.keys} for c in cands]
    harvest = [i for i, c in enumerate(cands) if c.source in ("harvest", "planted")]
    # harvest <-> harvest: overlapping locations + near-identical statements
    for x in range(len(harvest)):
        i = harvest[x]
        for y in range(x + 1, len(harvest)):
            j = harvest[y]
            if cands[i].source != cands[j].source:
                continue
            if locs_overlap(cands[i].locations, cands[j].locations) and \
                    overlap_coeff(tokens(cands[i].statement), tokens(cands[j].statement)) >= harvest_threshold:
                uf.union(i, j)
    for i, c in enumerate(cands):
        if c.source in ("harvest", "planted"):
            continue
        best, best_s = None, 0.0
        for j in harvest:
            h = cands[j]
            if c.source == "tests":
                if not locs_overlap(c.extra.get("reach") or c.locations, h.locations):
                    continue
                beh = stems(" ".join(c.extra.get("behaviour", []))) or stems(c.statement)
                s = test_match(beh, h.statement)
                if s <= 0 and locs_overlap(c.extra.get("direct") or [], h.locations):
                    # the rule sits in the method under test and the test class names it
                    subj = stems(" ".join(c.extra.get("subject", [])))
                    if len(subj & stems(h.statement)) >= 2:
                        s = 0.4
                if s <= 0:
                    continue
            else:
                s = overlap_coeff(toks[i], toks[j])
                if c.source == "llm-draft" and locs_overlap(c.locations, h.locations, 1):
                    s += 0.2
                if s < doc_threshold:
                    continue
            if s > best_s:
                best, best_s = j, s
        if best is not None:
            uf.union(best, i)
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(uf.find(i), []).append(i)
    return [[cands[i] for i in sorted(ix)] for _, ix in sorted(clusters.items())]


def _norm_locs(locs: list[dict], cap: int = 12) -> list[dict]:
    seen, out = set(), []
    for l in locs:
        k = (l["file"], int(l["start_line"]), int(l["end_line"]))
        if k not in seen:
            seen.add(k)
            out.append({"file": k[0], "start_line": k[1], "end_line": k[2]})
    return out[:cap]


def to_items(clusters: list[list[Candidate]]) -> list[dict]:
    """Key items (answer-key schema + provenance) with stable ids."""
    items, used = [], set()
    for cl in clusters:
        primary = sorted(cl, key=lambda c: (SOURCE_RANK.get(c.source, 9), cl.index(c)))[0]
        sources = sorted({c.source_tag for c in cl})
        kinds = {c.source for c in cl}
        flags = sorted({f for c in cl for f in c.flags} | ({"corroborated"} if len(kinds) >= 2 else set()))
        ev = "Verified" if any(c.evidence_class == "Verified" for c in cl) else primary.evidence_class
        locs = _norm_locs([l for c in [primary] + [c for c in cl if c is not primary] for l in c.locations])
        base = f"{primary.detector}|{locs[0]['file'] if locs else ''}|{' '.join(sorted(tokens(primary.statement)))}"
        prefix = TYPE_PREFIX.get(primary.type, "KI")
        n = 6
        iid = f"{prefix}-{stable_hash(base, n=n)}"
        while iid in used:
            n += 2
            iid = f"{prefix}-{stable_hash(base, n=n)}"
        used.add(iid)
        item = {
            "id": iid,
            "type": primary.type,
            "statement": primary.statement,
            "locations": locs,
            "evidence_class": ev,
            "flags": flags,
            "status": "unreviewed",
            "source": sources,
            "provenance": [{"source": c.source_tag, "statement": c.statement,
                            "locations": _norm_locs(c.locations, 6)} for c in cl],
        }
        snip = primary.extra.get("snippet")
        if snip:
            item["snippet"] = snip
        for k in ("test", "sut", "occurrences", "template"):
            if k in primary.extra:
                item[k] = primary.extra[k]
        items.append(item)
    order = {"harvest": 0, "tests": 1, "docs": 2, "llm-draft": 3}
    items.sort(key=lambda it: (order.get(it["source"][0].split(":")[0], 9), it["type"],
                               it["locations"][0]["file"] if it["locations"] else "",
                               it["locations"][0]["start_line"] if it["locations"] else 0, it["id"]))
    return items

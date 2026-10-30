#!/usr/bin/env python3
"""
story_module.py — deterministic story→business-module signal for the epic tier (Option 2:
module-cohesion as a STEERING PRIOR, not a hard partition).

Root finding (measured, not assumed): the epic anneal clustered on embedding-centroid text
semantics ALONE, so cross-cutting TECHNICAL patterns (format validation, immutability, sequence
numbering, access control) fused stories across unrelated business domains — because those
stories ARE textually similar. ~1/3 of epics are module-scattered (dominant-module share < 0.4,
>= 5 modules). The missing signal is the business DOMAIN.

The domain signal is often present, deterministically, when a source tree is organized by
module/domain codes (e.g. a typical ERP uses short codes like PR payroll, AP accounts-payable,
PO purchase-order, AR, … as path segments). These codes appear in each concept's `Evidence:`
file paths (e.g. `<root>/ClientSide/<MOD>/<MOD>/frm*.vb`, and report-folder prefixes like
`.../<MOD> <human name>/...`). A story's module = the DOMINANT module across its member concepts'
evidence paths. The module vocabulary is LEARNED from statistical mass at runtime, never
hardcoded, so this generalizes to any source tree (the codes above are illustrative only).

This is a PRIOR, not a gate (owner steering: "preseed the vector space to steer the outcome
without being overly influential"). It is folded into the anneal similarity as a weighted axis
(see ontology_epic_anneal augmented vectors), so same-module stories attract and accidental
cross-module fusion is penalized — but a genuinely cross-module capability (one spanning several
modules) can still hold if the semantic signal is strong enough. λ=0 recovers the
pure-semantic behavior; higher λ steers harder.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict

# module code appears as a path segment. Two common shapes:
#   client-side:  ClientSide/<MOD>/<MOD>/frm... .vb
#   reports/sql:  ".../<... folders ...>/<MOD> <human name>/....sql"  and  "Scripts/Reports/NNN.sql"
_CLIENT_RE = re.compile(r"ClientSide/([A-Z]{2,4})/")
# a 2-4 upper token immediately before a space inside a path folder (report module prefix)
_REPORT_RE = re.compile(r"/([A-Z]{2,4})\s")


def module_counts_for_body(body: str) -> Counter:
    c = Counter()
    if not body:
        return c
    for m in _CLIENT_RE.findall(body):
        c[m] += 2                      # client-side path is the strongest domain signal
    for m in _REPORT_RE.findall(body):
        c[m] += 1
    return c


def learn_module_vocab(raw_counts: Counter, min_mass: int = 10) -> set:
    """Derive the REAL module vocabulary from statistical mass, NOT a hardcoded list — so this
    generalizes to any source tree, never a specific target's codes. A path-segment token is a genuine
    module only if it recurs as a dominant domain across >= min_mass stories; the long tail
    (file abbreviations, one-off report codes, protocol names like SOAP/HTTP/TLS) appears once
    or twice and is dropped as noise. `raw_counts` = Counter of dominant-module per story."""
    return {m for m, c in raw_counts.items() if c >= min_mass}


def build_story_modules(conn: sqlite3.Connection, criterion: str = "assembled_backbone",
                        min_mass: int = 10):
    """Return (story_module: {story_id: MODULE|None}, module_vocab: set, coverage: float).
    Deterministic; no LLM. Two passes: (1) provisional dominant module per story from evidence
    paths; (2) learn the real vocabulary by mass (min_mass) and NULL-out any story whose
    provisional module is long-tail noise (kept, just unsteered — L3c). This makes the domain
    signal generalize to any corpus (mass-learned, not a hardcoded module list)."""
    # story -> member concept ids
    s2c = defaultdict(list)
    for src, dst in conn.execute(
        "SELECT src, dst FROM ontology_edges WHERE relation='concept_in_story' AND criterion=?",
        (criterion,),
    ):
        s2c[dst].append(src)
    # concept -> body (only the ones we need)
    need = {c for cs in s2c.values() for c in cs}
    body = {}
    need_l = list(need)
    for i in range(0, len(need_l), 900):
        chunk = need_l[i:i + 900]
        q = "SELECT concept_id, body_raw FROM unified_concepts WHERE concept_id IN (%s)" % ",".join("?" * len(chunk))
        for cid, b in conn.execute(q, chunk):
            body[cid] = b

    # pass 1 — provisional dominant module per story
    provisional = {}
    raw_counts = Counter()
    for st, cids in s2c.items():
        agg = Counter()
        for c in cids:
            agg += module_counts_for_body(body.get(c, ""))
        if agg:
            mod = agg.most_common(1)[0][0]
            provisional[st] = mod
            raw_counts[mod] += 1
        else:
            provisional[st] = None
    # pass 2 — keep only mass-backed modules; long-tail noise → None (unsteered, kept)
    vocab = learn_module_vocab(raw_counts, min_mass=min_mass)
    story_module = {st: (m if (m in vocab) else None) for st, m in provisional.items()}
    covered = sum(1 for v in story_module.values() if v)
    coverage = covered / len(story_module) if story_module else 0.0
    return story_module, vocab, coverage


if __name__ == "__main__":
    import json
    import sys
    conn = sqlite3.connect(sys.argv[1])
    sm, vocab, cov = build_story_modules(conn)
    print(json.dumps({
        "stories": len(sm),
        "with_module": sum(1 for v in sm.values() if v),
        "coverage": round(cov, 3),
        "module_vocab_size": len(vocab),
        "module_vocab": sorted(vocab),
    }, indent=2))

#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""concept_key.py — THE canonical key for one business concept. One definition, used by both ends.

Why this module exists
----------------------
A concept is written down twice, in two different spellings:

  * the concept GRAPH (knowledge.py) keeps the RAW map-stage `### ` header, e.g.
    `Data Invariant: Approval Limited by Role's Maximum Authorized Amount`
  * the FORMATTED still (what concept_symbols.py reads) keeps the rewritten, id-stamped title,
    e.g. `[DT-0021] Approval Limited by Role's Maximum Authorized Amount`

Neither is wrong, but they never match verbatim, so every consumer (export_graph's bridge,
graph_eval's resolver) grew its own ladder of "try the raw title, then without the id, then
without the type prefix, then casefolded". Ladders hide drift: a key that only matches on the
loosest rung is indistinguishable from a key that matches exactly.

So the id prefix and the `"<Type>: "` prefix are stripped EXACTLY ONCE, here, and both producers
emit the result as the concept's key. Consumers then compare keys with `==`.

    canonical_title("[DT-0021] Approval Limited by Role's Maximum Authorized Amount")
    canonical_title("Data Invariant: Approval Limited by Role's Maximum Authorized Amount")
      -> "approval limited by role's maximum authorized amount"   (both)

    concept_key("data", "[DT-0021] Approval ...") -> "data::approval ..."

Shape of a key: `"{still}::{canonical_title}"`. The still is part of the identity because dedup
is documented as within-still, so two stills may legitimately carry the same title.
"""
from __future__ import annotations

import re

# An assigned concept id: "[DT-0021] ", "[RL-1] ", "[EXC-0003] ".
ID_PREFIX = re.compile(r"^\[[A-Za-z]{2,4}-\d+\]\s*")
# A leading block-type label the formatter adds or drops: "Rule: ", "Entity: ",
# "Data Invariant: ", "Edge Case: ", "Obligation: ", "Relationship: ".
# Bounded to 3 words so a title that merely CONTAINS a colon ("Note: see also") is not eaten
# past its first clause, and anchored to an uppercase start so "foo: bar" is left alone.
TYPE_PREFIX = re.compile(r"^[A-Z][A-Za-z]*(?: [A-Za-z]+){0,2}:\s+")
# An HTML comment on the heading line. format_still.py stamps `<!-- ref:xxxxxxxx -->` traceability
# tags and asks the model to preserve them; the model sometimes keeps the tag on the `### ` line
# itself, so the formatted title carries it while the raw map header never does.
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)

KEY_SEP = "::"


def strip_id(title: str) -> str:
    """The title without its assigned `[XX-NNNN] ` id prefix (idempotent)."""
    return ID_PREFIX.sub("", (title or "").strip()).strip()


def strip_type(title: str) -> str:
    """The title without its leading `"<Type>: "` block-type label (idempotent)."""
    return TYPE_PREFIX.sub("", (title or "").strip()).strip()


def canonical_title(title: str) -> str:
    """The canonical spelling of a concept title: no HTML comment (`<!-- ref:… -->`), no id
    prefix, no `"<Type>: "` prefix, whitespace collapsed, casefolded. Idempotent —
    canonical_title(canonical_title(x)) == x."""
    t = strip_type(strip_id(HTML_COMMENT.sub(" ", title or "")))
    return " ".join(t.casefold().split())


def concept_key(still: str, title: str) -> str:
    """The canonical key for one concept: `"{still}::{canonical_title}"`.

    `still` may be empty (then the key is just the canonical title). A `still` that is itself a
    key (`"data::..."`) is NOT re-split — pass the still name, not a key."""
    ct = canonical_title(title)
    s = (still or "").strip()
    return f"{s}{KEY_SEP}{ct}" if s else ct


def split_key(key: str) -> tuple[str, str]:
    """(still, title) for a `"still::title"` key; ("", key) when there is no still part."""
    k = (key or "").strip()
    if KEY_SEP in k:
        still, _, title = k.partition(KEY_SEP)
        return still.strip(), title.strip()
    return "", k


def canonicalize_key(key: str) -> str:
    """Canonicalise a key that may already be in any spelling (raw, id-stamped, canonical).

    This is what a CONSUMER calls on a key read from an artifact that may predate this module:
    it is a no-op on an already-canonical key and converts a legacy key in one step."""
    still, title = split_key(key)
    return concept_key(still, title)

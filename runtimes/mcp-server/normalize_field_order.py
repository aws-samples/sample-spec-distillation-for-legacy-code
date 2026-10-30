#!/usr/bin/env python3
"""
normalize_field_order.py — Importable canonical field-order normalization.

Reorders concept fields into a business-first canonical order:

  1. Orientation:      Definition
  2. Why it matters:   Business rationale, What breaks if violated, Scope, Exceptions
  3. How it works:     Preconditions, Steps, Effects, Trigger, outcomes, Actor, ...
  4. Technical detail: Statement, Enforcement, Aliases, Evidence
  5. Confidence footer: Business Relevance, Evidence Diversity, Confidence

Rationale: business readers want what -> why -> stakes -> how it works ->
implementation detail -> confidence. Scores sit at the bottom as a trust
footer (relevant to both business and technical audiences).

Safety (zero signal loss): fields are segmented using a known-field-label
whitelist (sub-bullets, formal predicates, multi-line values stay attached to
their field). Segments are stable-sorted by canonical priority. A concept is
only rewritten if the transformed text contains exactly the same lines
(reordered). Unknown fields keep their relative order in the "how it works"
tier and never fall after the scores.

Used by format_still.py (post-LLM) so future runs emit canonical order
regardless of LLM compliance, and by the normalize-field-order.py migration CLI.
"""

import re

# Canonical priority (lower = earlier). Unknown known-fields default to 50
# (the "how it works" tier), preserving relative order via stable sort.
FIELD_PRIORITY = {
    # 1. Orientation
    "Definition": 0,
    # 2. Why it matters (business stakes)
    "Business rationale": 10,
    "What breaks if violated": 11,
    "Scope": 12,
    "Exceptions": 13,
    "Legitimate exceptions": 14,
    # 3. How it works (behavioral)
    "Preconditions": 30,
    "Steps": 31,
    "Effects": 32,
    "Trigger": 33,
    "Success outcome": 34,
    "Failure outcome": 35,
    "Primary actor": 36,
    "Actor": 37,
    "Handling": 38,
    "Frequency": 39,
    "Durability": 40,
    "Relationships": 41,
    "Attributes": 42,
    "Type": 43,
    "Classification": 44,
    "Domain concept": 45,
    "Domain area": 46,
    "Discovery trigger": 47,
    "Discovery origin": 48,
    "Resulting rule": 49,
    # (unknown known-fields default to 50)
    "Design vs. reality gap": 51,
    "Design vs reality gap": 51,
    "Inherits from": 52,
    "Hierarchy level": 53,
    "Capabilities": 54,
    "Time period": 55,
    "Previous state": 56,
    "Chosen path": 57,
    "Alternatives considered": 58,
    "Affected entity": 59,
    "Affected entities": 59,
    "Affected workflows": 60,
    "Affected interfaces": 61,
    "Test type": 62,
    "Risk level": 63,
    "Stability commitment": 64,
    "Recommendation": 65,
    "Note": 66,
    "Complexity": 67,
    # 4. Technical detail
    "Statement": 80,
    "Enforcement": 81,
    "Aliases": 82,
    "Evidence": 83,
    # 5. Confidence footer (bottom — both audiences)
    "Business Relevance": 90,
    "Evidence Diversity": 91,
    "Confidence": 92,
}

DEFAULT_PRIORITY = 50  # unknown-but-recognized fields land in "how it works"

KNOWN_FIELDS = set(FIELD_PRIORITY.keys())

_field_re = re.compile(r'^- ([A-Z][^:]*?):')


def _split_concepts(content):
    lines = content.split("\n")
    preamble = []
    concepts = []
    current = None
    for line in lines:
        if line.startswith("### "):
            if current is not None:
                concepts.append("\n".join(current))
            current = [line]
        elif current is None:
            preamble.append(line)
        else:
            current.append(line)
    if current is not None:
        concepts.append("\n".join(current))
    return "\n".join(preamble), concepts


def _segment_fields(concept_text):
    """Return (header_lines, segments). Each segment = [label, [lines]].

    A new segment starts only at a "- Label:" line whose label is a known
    field. All other lines (sub-bullets, formal fences, wrapped values, blanks)
    attach to the current segment, preserving multi-line field values.
    """
    lines = concept_text.split("\n")
    header = []
    segments = []
    current = None
    for line in lines:
        m = _field_re.match(line)
        label = m.group(1) if m else None
        if label in KNOWN_FIELDS:
            if current is not None:
                segments.append(current)
            current = [label, [line]]
        else:
            if current is None:
                header.append(line)
            else:
                current[1].append(line)
    if current is not None:
        segments.append(current)
    return header, segments


def reorder_concept(concept_text):
    """Reorder a concept's fields into canonical order. Safe no-op if the
    line multiset would change."""
    header, segments = _segment_fields(concept_text)
    if len(segments) < 2:
        return concept_text

    # Stable-sort by canonical priority (original index breaks ties)
    indexed = list(enumerate(segments))
    indexed.sort(key=lambda pair: (FIELD_PRIORITY.get(pair[1][0], DEFAULT_PRIORITY), pair[0]))
    ordered = [seg for _, seg in indexed]

    new_lines = list(header)
    for _label, seg_lines in ordered:
        new_lines.extend(seg_lines)
    new_text = "\n".join(new_lines)

    # SAFETY: multiset of lines must be identical
    if sorted(new_text.split("\n")) != sorted(concept_text.split("\n")):
        return concept_text
    return new_text


def normalize_text(content):
    """Normalize field order across all concepts. Returns (new_content, changed).

    Preserves the whole-file line multiset; returns input unchanged if not.
    """
    preamble, concepts = _split_concepts(content)
    if not concepts:
        return content, 0
    changed = 0
    new_concepts = []
    for c in concepts:
        nc = reorder_concept(c)
        if nc != c:
            changed += 1
        new_concepts.append(nc)
    if changed == 0:
        return content, 0
    if preamble:
        new_content = preamble + "\n" + "\n".join(new_concepts)
    else:
        new_content = "\n".join(new_concepts)
    if sorted(new_content.split("\n")) != sorted(content.split("\n")):
        return content, 0
    return new_content, changed

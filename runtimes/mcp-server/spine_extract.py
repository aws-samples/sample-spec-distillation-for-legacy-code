#!/usr/bin/env python3
"""
spine_extract.py — deterministic parser for the FORMAL SPINE of a concept block (System law
L14: projection-fidelity). The concept bodies are highly structured ("- Definition: …",
"- Statement:\n  ```…```", "- Aliases: …", "- Evidence: …", "- Business Relevance: N", …).
L14 says the formal representation (mechanism + limit/boundary conditions + business schema) is
the NORMATIVE PAYLOAD and must project VERBATIM — so we extract it MECHANICALLY here (no LLM,
L5), and the renderer emits it inline. The LLM never touches the spine; it can only add WHY
prose around it, so the spine cannot be paraphrased away.

Field taxonomy (mapped to the L14 layering):
  SPINE (mechanism)        : Statement, Handling, Resulting rule, Formula  → verbatim, fenced
  LIMITS (steering/bounds) : Trigger, Precondition(s), Enforcement, Exceptions, Scope,
                             Negative assertion, Effects, Driver, Type, Durability
  SCHEMA (business schema)  : Aliases, Affected entities/workflows
  EVIDENCE (provenance)     : Evidence (file:line)
  CALIBRATION (recovered)   : Business Relevance, Confidence, Evidence Diversity
  WHY (rationale)           : Definition, Business rationale, What breaks if violated

Everything rendered is bounded by what was recovered — a missing field is simply absent
(honestly partial, never fabricated: L4).
"""
from __future__ import annotations

import re

# field label → canonical key. Order matters only for display grouping, not parsing.
_SPINE_LABELS = ["Statement", "Handling", "Resulting rule", "Formula", "Resulting Rule"]
_LIMIT_LABELS = ["Trigger", "Preconditions", "Precondition", "Enforcement", "Exceptions",
                 "Exception", "Legitimate exceptions", "Scope", "Negative assertion", "Effects",
                 "Effect", "Driver", "Type", "Durability", "Expected outcome", "Action",
                 "Part of workflow", "Affected workflows"]
_SCHEMA_LABELS = ["Aliases", "Affected entities", "Affected workflows", "Affected"]
_EVIDENCE_LABELS = ["Evidence"]
_CALIB_LABELS = ["Business Relevance", "Confidence", "Evidence Diversity"]
_WHY_LABELS = ["Definition", "Business rationale", "What breaks if violated", "Discovery origin",
               "Discovery trigger", "Actor"]

_ALL_LABELS = (_SPINE_LABELS + _LIMIT_LABELS + _SCHEMA_LABELS + _EVIDENCE_LABELS
               + _CALIB_LABELS + _WHY_LABELS)
# longest-first so "Precondition" doesn't shadow "Preconditions", etc.
_ALL_LABELS_SORTED = sorted(set(_ALL_LABELS), key=len, reverse=True)

# a field starts at "- <Label>:" (markdown bullet) at line start (after optional ws)
_FIELD_RE = re.compile(
    r"^\s*[-*]\s*(" + "|".join(re.escape(l) for l in _ALL_LABELS_SORTED) + r")\s*:\s*(.*)$",
    re.IGNORECASE,
)


def _canon(label: str) -> str:
    lo = label.lower()
    for canon in _ALL_LABELS_SORTED:
        if canon.lower() == lo:
            return canon
    return label


def parse_block(body: str) -> dict:
    """Parse one concept body into {canonical_label: verbatim_value}. Multi-line values
    (including fenced ``` blocks) are captured verbatim until the next recognized field bullet.
    A concept body may contain multiple contributor sections (delimited by '<!-- contributor:'
    or '---'); we parse the WHOLE body and let repeated labels accumulate (list) so nothing is
    lost (L2/L14). Fenced code blocks are never split on."""
    fields: dict[str, list[str]] = {}
    title = None
    lines = body.splitlines()
    i = 0
    cur_key = None
    cur_val: list[str] = []
    in_fence = False

    def _flush():
        nonlocal cur_key, cur_val
        if cur_key is not None:
            fields.setdefault(cur_key, []).append("\n".join(cur_val).strip())
        cur_key, cur_val = None, []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # HARD contributor-section break: '---' or '<!-- contributor: -->'. This terminates the
        # current field AND force-closes any dangling fence — REGARDLESS of fence state. A source
        # value with an UNCLOSED ``` fence must NEVER be allowed to swallow the next contributor's
        # section (that is the bug: an unbalanced fence in one section ate the following sections).
        # A contributor boundary is authoritative over a malformed fence. (Field- and
        # fence-agnostic: it does not matter which field or whether a fence is open.)
        if stripped == "---" or stripped.startswith("<!-- contributor:"):
            _flush()
            in_fence = False
            i += 1
            continue

        # track fenced code so a "- Foo:" INSIDE a fence is not mistaken for a field
        if stripped.startswith("```"):
            in_fence = not in_fence
            if cur_key is not None:
                cur_val.append(line)
            i += 1
            continue
        if not in_fence:
            # a new '### heading' (after the title) is also a section break
            is_break = (title is not None and re.match(r"^\s*#{1,6}\s+\S", line) is not None)
            if is_break:
                _flush()
                i += 1
                continue
            if title is None:
                m_title = re.match(r"^\s*#{1,6}\s+(.*\S)\s*$", line)
                if m_title:
                    title = m_title.group(1).strip()
                    i += 1
                    continue
            m = _FIELD_RE.match(line)
            if m:
                _flush()
                cur_key = _canon(m.group(1))
                first = m.group(2).strip()
                cur_val = [first] if first else []
                i += 1
                continue
        if cur_key is not None:
            cur_val.append(line)
        i += 1
    _flush()

    # join repeated occurrences; keep them all (a concept unified across contributors may
    # carry several Statements/Evidence — L14: keep every distinct formal clause)
    out = {"_title": title} if title else {}
    for k, vals in fields.items():
        seen = []
        for v in vals:
            v = v.strip()
            if v and v not in seen:
                seen.append(v)
        if seen:
            out[k] = seen
    return out


def spine_of(body: str) -> dict:
    """Group a parsed block into the L14 layers. Values are verbatim strings (lists where a
    field recurred). Returns {'mechanism','limits','schema','evidence','calibration','why'} —
    each a dict of {label: [values]}. Absent layers are empty (honestly partial, L4)."""
    p = parse_block(body)
    def pick(labels):
        return {_canon(l): p[_canon(l)] for l in labels if _canon(l) in p}
    return {
        "title": p.get("_title"),
        "mechanism": pick(_SPINE_LABELS),
        "limits": pick(_LIMIT_LABELS),
        "schema": pick(_SCHEMA_LABELS),
        "evidence": pick(_EVIDENCE_LABELS),
        "calibration": pick(_CALIB_LABELS),
        "why": pick(_WHY_LABELS),
    }


if __name__ == "__main__":
    # tiny self-test on a representative block
    sample = """### Rule: Auto-Truncation of PO Number When Prefix Grows
- Definition: When a user increases the PO prefix length, the system automatically truncates.
- Aliases: MyFields.POPrefix, StdFieldValueChanged, PONumber.Remove
- Evidence: src/<module>/<Form>.ext#L943-L955
- Statement:
  ```
  REQUIRE (LENGTH(POPrefix) + LENGTH(PONumber) > 30) IMPLIES
          PONumber := PONumber.Remove(0, LENGTH(POPrefix) + LENGTH(PONumber) - 30)
  ```
- Enforcement: StdFieldValueChanged event handler for the POPrefix field.
- Business Relevance: 2
- Confidence: 4
- What breaks if violated: oversized PO numbers could reach the database layer.
"""
    import json
    print(json.dumps(spine_of(sample), indent=2))

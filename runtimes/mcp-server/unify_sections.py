#!/usr/bin/env python3
"""
unify_sections.py — Importable H2 section unification.

Problem: The format phase processes each still-file in independent chunks.
Each chunk groups its blocks under its own ## headers with no knowledge of
other chunks, so the same logical section (e.g. "Equipment Management")
appears as multiple ## headers scattered through the file. In the viewer,
identical H2 text slugifies to identical ids, colliding during TOC navigation
and scroll highlighting.

Fix: Merge all ### blocks under exact-match ## headers so each section appears
once, with all its blocks grouped (first-occurrence order preserved).

Safety (zero signal loss): the only lines removed are duplicate ## header
lines. If any non-header line would be removed, or any line added, the
transform aborts and returns the input unchanged.

Used by format_still.py (before renumbering) so future runs emit unified
sections natively, and by the unify-sections.py migration CLI for existing data.
"""

from collections import Counter, OrderedDict


def _is_h2(line):
    return line.startswith("## ") and not line.startswith("### ")


def parse_sections(content):
    """Split into (preamble_lines, [[h2_line, [body_lines]], ...])."""
    lines = content.split("\n")
    preamble = []
    sections = []
    current = None
    for line in lines:
        if _is_h2(line):
            if current is not None:
                sections.append(current)
            current = [line, []]
        elif current is None:
            preamble.append(line)
        else:
            current[1].append(line)
    if current is not None:
        sections.append(current)
    return preamble, sections


def unify_text(content):
    """Unify duplicate H2 sections. Returns (new_content, duplicate_headers_removed).

    Safe: aborts (returns input, 0) if the transform would remove any
    non-header line or introduce any new line.
    """
    preamble, sections = parse_sections(content)

    grouped = OrderedDict()
    for h2_line, body in sections:
        key = h2_line.strip()
        if key not in grouped:
            grouped[key] = [h2_line, []]
        grouped[key][1].extend(body)

    dup_removed = len(sections) - len(grouped)
    if dup_removed <= 0:
        return content, 0

    out = list(preamble)
    for _key, (h2_line, body) in grouped.items():
        out.append(h2_line)
        out.extend(body)
    new_content = "\n".join(out)

    # SAFETY: only duplicate ## header lines may be removed; nothing added.
    old_c = Counter(content.split("\n"))
    new_c = Counter(new_content.split("\n"))
    added = new_c - old_c
    removed = old_c - new_c
    if added:
        return content, 0
    for line in removed:
        if not _is_h2(line):
            return content, 0

    return new_content, dup_removed

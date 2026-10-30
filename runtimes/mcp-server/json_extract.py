#!/usr/bin/env python3
"""
json_extract.py — Single source of truth for extracting a JSON value from an LLM
response that may be fenced and/or followed by trailing prose.

WHY THIS EXISTS
---------------
LLMs reliably emit a valid JSON value and then keep talking, e.g.:

    ```json
    [ {"index": 0, "observed": "CMDT.StmtDate"} ]
    ```
    Wait, let me reconsider. ...

A naive first-'['..last-']' (or '{'..'}') slice includes that trailing prose (and
any stray brackets in it), yielding json 'Extra data' errors. We instead scan from
the first opening delimiter and walk a depth counter — respecting quoted strings and
escapes — returning exactly the first COMPLETE, BALANCED value and ignoring
everything after. This is the hardened behavior first proven out in the symbol
back-map residue pass; extracted here so every LLM caller shares one implementation
(no divergent copies that can silently rot).

USAGE
-----
    from json_extract import strip_json
    strip_json(text)                 # default: first balanced JSON array  [...]
    strip_json(text, kind="object")  # first balanced JSON object          {...}

Returns the extracted substring (caller still json.loads it). If no opening
delimiter is found, returns the fence-stripped text unchanged (caller's json.loads
will raise, which the caller handles / records).
"""
from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^```(?:json)?\s*")

_DELIMS = {
    "array": ("[", "]"),
    "object": ("{", "}"),
}


def strip_json(text: str, kind: str = "array") -> str:
    """Extract the first complete, balanced top-level JSON value of `kind`
    ('array' -> [...], 'object' -> {...}) from a possibly-fenced,
    possibly-trailing-prose LLM response.

    String/escape aware: brackets inside "quoted strings" (with backslash escapes)
    do not affect depth, so stray delimiters in string values or trailing prose are
    handled correctly.
    """
    try:
        open_ch, close_ch = _DELIMS[kind]
    except KeyError:
        raise ValueError(f"kind must be 'array' or 'object', got {kind!r}")

    t = _FENCE_RE.sub("", text.strip())
    start = t.find(open_ch)
    if start == -1:
        return t  # no value found; let the caller's json.loads raise/record

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return t[start:i + 1]  # first complete value; ignore trailing prose
    return t[start:]  # unbalanced (truncated) — caller records as unparseable

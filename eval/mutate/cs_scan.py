# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tokeniser-level C# scanner shared by ``sanity.py``, ``mutations_cs.py`` and ``eval/keygen``.

``scan_cs(text)`` returns ``(code_lines, line_ok)`` with exactly the same line and column
layout as the input:

* ``//`` and ``/* */`` comments are blanked (block comments may span lines);
* the body of every string and char literal is replaced by spaces, the delimiting quotes are
  kept, so later regexes never match inside a literal;
* regular ``"..."`` (backslash escapes), verbatim ``@"..."`` (``""`` escape, may span lines),
  interpolated ``$"..{expr}.."`` / ``$@"..."`` / ``@$"..."`` (the hole expression stays code, so
  its parentheses and nested strings are scanned too; the hole's own ``{`` ``}`` are blanked;
  ``{{`` / ``}}`` are literal), raw ``\"\"\"...\"\"\"`` literals (C# 11, body blanked) and char
  literals.

A line is not ok when a regular string or char literal is still open at its end. The last line
is not ok when a verbatim/raw string or a block comment is still open at end of file.

This is not a parser. Generic angle brackets, preprocessor directives and attributes are left
alone; only what a mechanical edit usually breaks (literals, comments, bracket balance) matters.
"""
from __future__ import annotations

import re

_STR_START = re.compile(r'(\$+@?|@\$+|@)?("{3,}|")')
_CHAR_LIT = re.compile(r"'(?:\\(?:u[0-9a-fA-F]{4}|x[0-9a-fA-F]{1,4}|.)|[^'\\])'")


def scan_cs(text: str) -> tuple[list[str], list[bool]]:
    lines = text.split("\n")
    code: list[str] = []
    ok: list[bool] = []
    # stack frames: ["code", depth] (top level), ["hole", depth] (interpolation hole),
    # ["str", verbatim, interpolated, raw_quote_count]
    stack: list[list] = [["code", 0]]
    in_block = False
    for raw in lines:
        out: list[str] = []
        good = True
        i, n = 0, len(raw)
        while i < n:
            c = raw[i]
            two = raw[i:i + 2]
            top = stack[-1]
            if in_block:
                if two == "*/":
                    in_block = False
                    out.append("  ")
                    i += 2
                else:
                    out.append(" ")
                    i += 1
                continue
            if top[0] == "str":
                _, verb, interp, rawq = top
                if rawq:
                    if raw.startswith('"' * rawq, i):
                        out.append('"' * rawq)
                        i += rawq
                        stack.pop()
                    else:
                        out.append(" ")
                        i += 1
                    continue
                if not verb and c == "\\":
                    w = len(raw[i:i + 2])
                    out.append(" " * w)
                    i += w
                    continue
                if verb and two == '""':
                    out.append("  ")
                    i += 2
                    continue
                if interp and two in ("{{", "}}"):
                    out.append("  ")
                    i += 2
                    continue
                if interp and c == "{":
                    out.append(" ")
                    stack.append(["hole", 0])
                    i += 1
                    continue
                if c == '"':
                    out.append('"')
                    stack.pop()
                    i += 1
                    continue
                out.append(" ")
                i += 1
                continue
            # code or interpolation hole
            if two == "//":
                out.append(" " * (n - i))           # keep the column layout
                break
            if two == "/*":
                in_block = True
                out.append("  ")
                i += 2
                continue
            if c in '$@"':
                m = _STR_START.match(raw, i)
                if m and (m.group(1) or c == '"'):
                    pre, q = m.group(1) or "", m.group(2)
                    if len(q) >= 3:
                        out.append(" " * len(pre) + q)
                        stack.append(["str", True, False, len(q)])
                        i += len(pre) + len(q)
                        continue
                    out.append(" " * len(pre) + '"')
                    stack.append(["str", "@" in pre, "$" in pre, 0])
                    i += len(pre) + 1
                    continue
            if c == "'":
                mm = _CHAR_LIT.match(raw, i)
                if mm:
                    w = len(mm.group(0))
                    out.append("'" + " " * (w - 2) + "'")
                    i += w
                    continue
                good = False
                out.append(c)
                i += 1
                continue
            if top[0] == "hole":
                if c == "{":
                    top[1] += 1
                elif c == "}":
                    if top[1] == 0:
                        stack.pop()
                        out.append(" ")
                        i += 1
                        continue
                    top[1] -= 1
            out.append(c)
            i += 1
        # a regular string cannot span lines: report it and resynchronise at top level
        if any(f[0] == "str" and not f[1] and not f[3] for f in stack):
            good = False
            del stack[1:]
        code.append("".join(out))
        ok.append(good)
    if (len(stack) > 1 or in_block) and ok:
        ok[-1] = False
    return code, ok


def cs_code_text(text: str) -> str:
    """Comment- and string-blanked C# text with the same length and line layout as ``text``."""
    return "\n".join(scan_cs(text)[0])


CS_TYPE_DECL = re.compile(
    r"(?<![\w.@])(?:class|struct|interface|enum|record(?:\s+(?:class|struct))?)\s+[A-Za-z_]\w*")
CS_MODS = r"(?:public|private|protected|internal|static|async|virtual|override|sealed|abstract|" \
          r"extern|unsafe|new|partial|readonly)"
#: a method / constructor / local-function head: modifiers, optional return type, name, ``(``
CS_METHOD_DECL = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)*(?:" + CS_MODS + r"\s+)+(?:[\w<>\[\],.?()\s]*?[\w>\]?)]\s+)?"
    r"(?P<name>[A-Za-z_]\w*)\s*(?:<[^()]*>)?\s*\(")
_NOT_METHOD = re.compile(r"^\s*(?:return|new|await|throw|using|var|if|while|for|foreach|switch)\b")


def is_method_head(code_line: str) -> bool:
    """True when a (blanked) code line starts a method, constructor or local function."""
    if _NOT_METHOD.match(code_line):
        return False
    m = CS_METHOD_DECL.match(code_line)
    if not m:
        return False
    # property/field initialisers like `public int X = Foo(` are not methods
    head = code_line[: m.start("name")]
    return "=" not in head

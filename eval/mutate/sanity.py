# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lightweight syntax sanity checks for VB.NET / C# / T-SQL corpus files.

This is not a parser. It is a tokeniser-level guard that catches the ways a
mechanical source edit usually breaks a file:

* an unterminated string literal,
* unbalanced parentheses / brackets,
* a dropped or duplicated block terminator (``End Function``, ``END``, ...).

Two entry points:

``check(path)``
    Absolute checks on one file. Returns ``(ok, [problem, ...])``.

``profile(path)``
    A dict of structural token counts. ``mutate.py`` compares the profile of a
    file before and after mutation against the delta each mutation *declares*.
    A mutation whose actual delta differs from its declared delta is rejected.
    This is how "syntax-preserving" is enforced in practice: an edit may only
    change the structure it says it changes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cs_scan import CS_TYPE_DECL, is_method_head, scan_cs  # noqa: E402

VB_EXT = {".vb", ".bas", ".cls"}
SQL_EXT = {".sql"}
CS_EXT = {".cs"}

# VB block openers -> the "End <x>" word that closes them.
VB_PAIRS = [
    ("Function", "Function"), ("Sub", "Sub"), ("Property", "Property"),
    ("Class", "Class"), ("Module", "Module"), ("Structure", "Structure"),
    ("Enum", "Enum"), ("Interface", "Interface"), ("Namespace", "Namespace"),
    ("Select Case", "Select"), ("Try", "Try"), ("With", "With"),
    ("SyncLock", "SyncLock"), ("Using", "Using"), ("While", "While"),
]
# Pairs whose counts are reliable enough to assert absolutely on real VB.
# ("Property" is excluded: auto-properties have no End Property. "With"/"Using"/
# "SyncLock" are rare and can appear as identifiers.)
VB_ENFORCED = {"Function", "Sub", "Class", "Module", "Structure", "Enum",
               "Interface", "Namespace", "Select Case", "Try", "While"}
# A block-opening keyword is not an opener when the word in front of it is one of these
# (``Exit Sub``, ``End Function``, ``Do While``, ``Declare Function``, ...).
NOT_OPENER_AFTER = {"end", "exit", "declare", "delegate", "do", "loop", "continue",
                    "resume", "addressof", "goto"}


def lang_of(path: Path) -> str:
    s = path.suffix.lower()
    if s in VB_EXT:
        return "vb"
    if s in SQL_EXT:
        return "sql"
    if s in CS_EXT:
        return "cs"
    return "text"


# --------------------------------------------------------------------------- scanning
def scan_vb(text: str) -> tuple[list[str], list[bool]]:
    """Return (code_lines, line_ok). code_lines have comments blanked and the body of
    every string literal replaced by spaces, so later regexes never see them."""
    code, ok = [], []
    for raw in text.split("\n"):
        out, i, in_str, good = [], 0, False, True
        while i < len(raw):
            c = raw[i]
            if in_str:
                if c == '"':
                    if i + 1 < len(raw) and raw[i + 1] == '"':  # "" escape
                        out.append("  ")
                        i += 2
                        continue
                    in_str = False
                    out.append('"')
                else:
                    out.append(" ")
                i += 1
                continue
            if c == '"':
                in_str = True
                out.append('"')
                i += 1
                continue
            if c == "'":  # comment to end of line
                break
            out.append(c)
            i += 1
        if in_str:
            good = False
        line = "".join(out)
        # REM-style comment
        if re.match(r"^\s*REM\b", line, re.I):
            line = ""
        code.append(line)
        ok.append(good)
    return code, ok


def scan_sql(text: str) -> tuple[list[str], list[bool]]:
    code, ok = [], []
    in_block = False
    for raw in text.split("\n"):
        out, i, in_str, good = [], 0, False, True
        while i < len(raw):
            c = raw[i]
            two = raw[i:i + 2]
            if in_block:
                # blank the comment out but keep column positions stable
                if two == "*/":
                    in_block = False
                    out.append("  ")
                    i += 2
                else:
                    out.append(" ")
                    i += 1
                continue
            if in_str:
                if c == "'":
                    if two == "''":
                        out.append("  ")
                        i += 2
                        continue
                    in_str = False
                    out.append("'")
                else:
                    out.append(" ")
                i += 1
                continue
            if two == "--":
                break
            if two == "/*":
                in_block = True
                out.append("  ")
                i += 2
                continue
            if c == "'":
                in_str = True
                out.append("'")
                i += 1
                continue
            out.append(c)
            i += 1
        if in_str:
            good = False
        code.append("".join(out))
        ok.append(good)
    if in_block:
        ok[-1] = False
    return code, ok


def scan(text: str, lang: str) -> tuple[list[str], list[bool]]:
    if lang == "vb":
        return scan_vb(text)
    if lang == "sql":
        return scan_sql(text)
    if lang == "cs":
        return scan_cs(text)
    lines = text.split("\n")
    return lines, [True] * len(lines)


def join_vb_continuations(code: list[str]) -> list[str]:
    """Collapse ``_``-continued VB lines into one logical line each (keeping the count
    stable by emitting empty strings for the consumed lines)."""
    out = [""] * len(code)
    i = 0
    while i < len(code):
        buf, j = code[i].rstrip(), i
        while buf.endswith("_") and j + 1 < len(code):
            j += 1
            buf = buf[:-1].rstrip() + " " + code[j].strip()
        out[i] = buf
        i = j + 1
    return out


# --------------------------------------------------------------------------- profile
def _count_word(code: list[str], word: str) -> int:
    pat = re.compile(r"(?<![\w.])" + word.replace(" ", r"\s+") + r"(?![\w])", re.I)
    return sum(len(pat.findall(l)) for l in code)


def _count_opener(code: list[str], word: str) -> int:
    """Count ``word`` as a block opener: skip hits whose preceding word disqualifies them."""
    pat = re.compile(r"(?:(?P<prev>[A-Za-z_]\w*)\s+)?(?<![\w.])(?P<kw>" +
                     word.replace(" ", r"\s+") + r")(?![\w])", re.I)
    n = 0
    for line in code:
        for m in pat.finditer(line):
            prev = (m.group("prev") or "").lower()
            if prev in NOT_OPENER_AFTER:
                continue
            n += 1
    return n


def profile(path: Path) -> dict[str, int]:
    """Structural token counts for one file (comments and string bodies ignored)."""
    text = path.read_text(errors="replace")
    lang = lang_of(path)
    code, _ = scan(text, lang)
    p: dict[str, int] = {
        "lines": len(text.split("\n")),
        "paren_open": sum(l.count("(") for l in code),
        "paren_close": sum(l.count(")") for l in code),
        "bracket_open": sum(l.count("[") for l in code),
        "bracket_close": sum(l.count("]") for l in code),
    }
    if lang == "vb":
        logical = join_vb_continuations(code)
        for opener, closer in VB_PAIRS:
            p[f"vb_open_{opener.replace(' ', '_').lower()}"] = _count_opener(logical, opener)
            p[f"vb_close_{closer.lower()}"] = _count_word(logical, "End\\s+" + closer)
        p["vb_open_if"] = sum(
            1 for l in logical
            if re.search(r"(?<![\w.])(?:If|ElseIf)\b", l, re.I) and re.search(r"\bThen\s*$", l, re.I)
            and not re.match(r"^\s*#", l) and not re.search(r"(?<![\w.])ElseIf\b", l, re.I))
        p["vb_close_if"] = _count_word(logical, "End\\s+If")
        p["vb_open_for"] = _count_opener(logical, "For")
        p["vb_close_next"] = _count_word(logical, "Next")
    elif lang == "cs":
        p["brace_open"] = sum(l.count("{") for l in code)
        p["brace_close"] = sum(l.count("}") for l in code)
        p["cs_type_decl"] = sum(len(CS_TYPE_DECL.findall(l)) for l in code)
        p["cs_method_decl"] = sum(1 for l in code if is_method_head(l))
    elif lang == "sql":
        p["sql_begin"] = _count_word(code, "BEGIN")
        p["sql_end"] = _count_word(code, "END")
        p["sql_case"] = _count_word(code, "CASE")
    return p


def check(path: Path) -> tuple[bool, list[str]]:
    """Absolute, language-aware sanity checks on one file."""
    text = path.read_text(errors="replace")
    lang = lang_of(path)
    code, ok = scan(text, lang)
    problems: list[str] = []
    for n, good in enumerate(ok, 1):
        if not good:
            problems.append(f"{path}:{n}: unterminated string literal")
    p = profile(path)
    if p["paren_open"] != p["paren_close"]:
        problems.append(f"{path}: unbalanced parentheses ({p['paren_open']} open, {p['paren_close']} close)")
    if p["bracket_open"] != p["bracket_close"]:
        problems.append(f"{path}: unbalanced brackets")
    if lang == "vb":
        for opener, closer in VB_PAIRS:
            if opener not in VB_ENFORCED:
                continue
            o = p.get(f"vb_open_{opener.replace(' ', '_').lower()}", 0)
            c = p.get(f"vb_close_{closer.lower()}", 0)
            if o != c:
                problems.append(f"{path}: {opener} / End {closer} counts differ ({o} vs {c})")
        if p["vb_open_if"] != p["vb_close_if"]:
            problems.append(f"{path}: block If / End If counts differ "
                            f"({p['vb_open_if']} vs {p['vb_close_if']})")
    if lang == "cs" and p["brace_open"] != p["brace_close"]:
        problems.append(f"{path}: unbalanced braces ({p['brace_open']} open, "
                        f"{p['brace_close']} close)")
    if lang == "sql":
        # END closes BEGIN and CASE; BEGIN TRANSACTION/TRY are matched by COMMIT/END TRY,
        # so only flag a clear deficit of ENDs.
        if p["sql_end"] < p["sql_begin"] - p["sql_case"] - _count_word(code, "BEGIN\\s+TRAN\\w*"):
            problems.append(f"{path}: fewer END than BEGIN ({p['sql_end']} vs {p['sql_begin']})")
    return (not problems), problems


def check_tree(root: Path, exts: set[str] | None = None) -> tuple[bool, list[str]]:
    exts = exts or (VB_EXT | SQL_EXT | CS_EXT)
    problems: list[str] = []
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.suffix.lower() in exts:
            _, pr = check(f)
            problems += pr
    return (not problems), problems


def profile_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    keys = set(before) | set(after)
    return {k: after.get(k, 0) - before.get(k, 0) for k in sorted(keys)
            if after.get(k, 0) != before.get(k, 0)}


def main() -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Syntax sanity check for a corpus tree or file")
    ap.add_argument("path", type=Path)
    ap.add_argument("--json", type=Path, help="write a machine-readable result here")
    ap.add_argument("--allow-problems", type=int, default=0,
                    help="exit 0 while the problem count is at or below this (default 0)")
    a = ap.parse_args()
    ok, problems = (check(a.path) if a.path.is_file() else check_tree(a.path))
    for p in problems:
        print(p)
    n_files = 1 if a.path.is_file() else sum(
        1 for f in a.path.rglob("*") if f.is_file() and f.suffix.lower() in (VB_EXT | SQL_EXT | CS_EXT))
    print("OK" if ok else f"{len(problems)} problem(s)")
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"path": str(a.path), "ok": ok,
                                      "n_problems": len(problems), "n_files": n_files,
                                      "problems": problems}, indent=2) + "\n")
    return 0 if len(problems) <= a.allow_problems else 1


if __name__ == "__main__":
    raise SystemExit(main())

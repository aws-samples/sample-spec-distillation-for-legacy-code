# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""issues.py — customer-facing "Issues & Insights" report (pipeline Phase 2.98).

WHY: the customer's main reason for distilling a legacy system is to surface, BEFORE
modernization, duplicated logic that should be centralized, undocumented behaviour, logic flaws
and inconsistencies. The stills describe the system; nothing in the pipeline was dedicated to
finding problems in it, and the full spec is too large to read for that purpose.

HOW (two passes):
  1. Deterministic candidate scan over the numbered source pages (.pages/*/concat.txt), with the
     code graph (knowledge_graph/code_graph.json) as a supplement:
       literal_mismatch   same named limit/size with different values, or the same literal
                          compared with different boundary operators across files
       repeated_literal   a non-trivial literal hard-coded in several files
       duplicate_expr     identical normalized expressions/blocks in more than one place
       uncalled           procedures/functions/subs (and tables) with no reference anywhere
       missing_object     referenced procedures/tables that no file in the corpus defines
       commented_code     comment runs that look like code; obsolete-marked code; `If False`
       comment_rule       comments that state a rule (never/only/must/nothing enforces/keep in sync)
       db_check           database-side checks (RAISERROR/THROW/CHECK/ROLLBACK)
  2. A few bounded LLM calls (Bedrock, default us.anthropic.claude-sonnet-4-6) confirm/classify
     each candidate and add issues found in the stills/source. Prompt: still_specs/issues/system_prompt.md.
  Every cited location is verified against the source pages (file exists, range in bounds, quoted
  fragment present — relocated to the line where it is found if the model's range was off).
  Locations that cannot be verified are dropped; an issue with no verified location is dropped.

OUTPUT: <output>/issues/issues.json and issues.md (ranked, grouped by category, summary table first).
EXIT:   0 ok (incl. "no candidates, no issues"); 3 = candidates existed but no issue survived
        (the stage must fail, not silently ship an empty report); 2 = bad input / LLM failure.

Optional input: <output>/facts/facts.json (produced by a separate facts phase when present).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PROMPT_PATH = REPO / "still_specs" / "issues" / "system_prompt.md"

CATEGORIES = [
    "Inconsistency",
    "Possible logic flaw",
    "Duplicated logic",
    "Undocumented behaviour",
    "DB-only rule",
    "Comment-only rule",
    "Dead/obsolete code",
]
EVIDENCE = ["Verified", "Inferred", "Confirmation Required", "Unknown"]
SEVERITY = ["High", "Medium", "Low"]

# Sonnet 4.x on-demand list price (USD / token) — used only for the cost estimate + budget guard.
PRICE_IN = 3.0e-6
PRICE_OUT = 15.0e-6

# ── Source loading ───────────────────────────────────────────────────────────

# Citation parsing, source indexing and location verification are shared with the MCP
# trace tools, so they live in citations.py. Imported under their original names to
# keep this module's behaviour and output identical.
from citations import (END_MARKER, FILE_MARKER, LOC_STR, NUMBERED,  # noqa: F401
                       SourceIndex, excerpt, load_sources, render_numbered,
                       verify_location)
from citations import marker_path as _marker_path  # noqa: F401


def lang_of(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in (".vb", ".bas", ".cls", ".frm", ".vbs"):
        return "vb"
    if ext in (".sql", ".prc", ".tab", ".trg", ".vw", ".fnc"):
        return "sql"
    if ext in (".cs", ".java", ".js", ".ts", ".tsx", ".jsx", ".c", ".cpp", ".h", ".go", ".rs",
               ".kt", ".swift", ".scala", ".php"):
        return "c"
    if ext in (".py", ".sh", ".rb", ".pl", ".ps1", ".yaml", ".yml", ".r"):
        return "hash"
    return "text"


def _split_vb(line: str) -> tuple[str, str]:
    in_str = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_str = not in_str
        elif ch == "'" and not in_str:
            return line[:i], line[i + 1:]
    s = line.lstrip()
    if s[:4].upper() == "REM " or s.upper() == "REM":
        return "", s[3:]
    return line, ""


def _split_marker(line: str, marker: str) -> tuple[str, str]:
    in_s = in_d = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d and line.startswith(marker, i):
            return line[:i], line[i + len(marker):]
        i += 1
    return line, ""


@dataclass
class SrcLine:
    n: int
    raw: str
    code: str
    comment: str
    full_comment: bool


def analyze(path: str, lines: dict[int, str]) -> list[SrcLine]:
    """Split every line into (code, comment). Block comments (/* */) handled for sql/c."""
    lang = lang_of(path)
    out: list[SrcLine] = []
    in_block = False
    for n in sorted(lines):
        raw = lines[n]
        code, comment = raw, ""
        if lang == "text":
            out.append(SrcLine(n, raw, raw, "", False))
            continue
        if lang in ("sql", "c"):
            if in_block:
                end = raw.find("*/")
                if end == -1:
                    out.append(SrcLine(n, raw, "", raw.strip(), True))
                    continue
                comment, raw_rest = raw[:end], raw[end + 2:]
                in_block = False
                code = raw_rest
            else:
                code = raw
            start = code.find("/*")
            if start != -1 and "*/" not in code[start:]:
                comment = (comment + " " + code[start + 2:]).strip()
                code = code[:start]
                in_block = True
            else:
                code = re.sub(r"/\*.*?\*/", " ", code)
            marker = "--" if lang == "sql" else "//"
            code, c2 = _split_marker(code, marker)
            comment = (comment + " " + c2).strip()
        elif lang == "vb":
            code, comment = _split_vb(raw)
        elif lang == "hash":
            code, comment = _split_marker(raw, "#")
        comment = comment.strip()
        out.append(SrcLine(n, raw, code, comment, bool(comment) and not code.strip()))
    return out


# ── Candidates ───────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    detector: str
    hint: str  # suggested category
    summary: str
    locations: list[tuple[str, int, int]]
    priority: int = 5  # lower = more important
    id: str = ""
    extra: dict = field(default_factory=dict)

    def key(self) -> tuple:
        return (self.detector, tuple(sorted(self.locations)))


TRIVIAL_NUMBERS = {"0", "1", "2", "-1", "100", "0.0", "1.0", "0.5", "3", "4", "10"}
LAYOUT_KEYS = {
    "tabindex", "width", "height", "left", "top", "x", "y", "size", "location", "font", "fontsize",
    "columns", "rows", "index", "i", "j", "k", "n", "count", "len", "length", "ubound", "lbound",
    "rc", "ret", "result", "retval", "step", "selectedindex", "rowcount", "rowindex", "columnindex",
    "padding", "margin", "autoscaledimensions", "clientsize", "point", "zorder", "id", "nocount",
    "error", "@@error", "@@rowcount", "@@trancount", "return", "exitcode", "attempt", "retry",
}
UI_PREFIX = re.compile(
    r"^(txt|cbo|cmb|lbl|chk|dtp|num|btn|cmd|grd|dgv|lst|opt|rdo|frm|m_|g_|p_|s_|str|dec|int|lng|dbl|cur|bln|b|n)(?=[A-Z_])")
NUM = r"-?\d+(?:\.\d+)?"
NUM_TOKEN = re.compile(rf"(?<![\w.]){NUM}[DdFfMmRrLl@]?(?![\w.])")
CMP_OPS = r"<=|>=|<>|!=|==|=|<|>"
CMP_RIGHT = re.compile(rf"([@\w.\[\]]+(?:\([^()]*\))?)\s*({CMP_OPS})\s*({NUM})[DdFfMmRrLl@]?(?![\w.])")
CMP_LEFT = re.compile(rf"(?<![\w.])({NUM})[DdFfMmRrLl@]?\s*({CMP_OPS})\s*([@\w.\[\]]+)")
COND_CTX = re.compile(r"\b(if|elseif|while|until|where|and|or|when|case|having|on|andalso|orelse|iif|check|return)\b|[?&|!]", re.I)
SIZE_DECL = re.compile(r"([@\w\[\]]+)\s+n?(?:var)?char\s*\(\s*(\d+)\s*\)", re.I)
MAXLEN_ASSIGN = re.compile(r"([\w.]+)\.MaxLength\s*=\s*(\d+)", re.I)
CONST_ASSIGN = re.compile(rf"(?:\bConst\s+|\bfinal\s+\w+\s+|\bconst\s+|DECLARE\s+)?([@\w]+)(?:\s+As\s+\w+|\s+[a-z]+(?:\(\d+(?:,\s*\d+)?\))?)?\s*=\s*({NUM})[DdFfMmRrLl@]?\s*;?\s*$", re.I)
OPPOSITE = {">": "gt", ">=": "gt", "<": "lt", "<=": "lt"}


def norm_key(token: str) -> str:
    t = token.strip("[]()")
    t = re.sub(r"\(.*$", "", t)
    parts = [p for p in re.split(r"[.]", t) if p and p.lower() not in ("me", "this", "dbo", "self")]
    if not parts:
        return ""
    if parts[-1].lower() in ("text", "value", "maxlength", "length", "count") and len(parts) > 1:
        parts = parts[:-1]
    name = parts[-1].lstrip("@")
    name = UI_PREFIX.sub("", name)
    return name.lower().strip("_")


def norm_num(v: str) -> str:
    try:
        f = float(v)
    except ValueError:
        return v
    return str(int(f)) if f == int(f) else repr(f)


def scan_literals(files: dict[str, list[SrcLine]]) -> list[Candidate]:
    cmps = []   # (key, op, value, file, line)
    named = []  # (key, value, file, line, kind)
    lit_uses = defaultdict(list)  # value -> [(file, line)]
    for path, lines in files.items():
        if lang_of(path) == "text":
            continue
        for sl in lines:
            code = re.sub(r'"[^"]*"', '""', sl.code)
            if not code.strip():
                continue
            for m in SIZE_DECL.finditer(code):
                named.append((norm_key(m.group(1)), m.group(2), path, sl.n, "size"))
            for m in MAXLEN_ASSIGN.finditer(code):
                named.append((norm_key(m.group(1)), m.group(2), path, sl.n, "size"))
            ctx = bool(COND_CTX.search(code))
            for m in CMP_RIGHT.finditer(code):
                key, op, val = norm_key(m.group(1)), m.group(2), norm_num(m.group(3))
                if op in ("=", "==") and not ctx:
                    continue
                if key and key not in LAYOUT_KEYS and val not in TRIVIAL_NUMBERS:
                    cmps.append((key, op, val, path, sl.n))
            for m in CMP_LEFT.finditer(code):
                val, op, key = norm_num(m.group(1)), m.group(2), norm_key(m.group(3))
                if not ctx:
                    continue
                flip = {">": "<", "<": ">", ">=": "<=", "<=": ">="}.get(op, op)
                if key and key not in LAYOUT_KEYS and val not in TRIVIAL_NUMBERS:
                    cmps.append((key, flip, val, path, sl.n))
            m = CONST_ASSIGN.search(code.strip())
            if m and not ctx:
                key, val = norm_key(m.group(1)), norm_num(m.group(2))
                if key and key not in LAYOUT_KEYS and val not in TRIVIAL_NUMBERS:
                    named.append((key, val, path, sl.n, "const"))
            for m in NUM_TOKEN.finditer(code):
                val = norm_num(re.sub(r"[DdFfMmRrLl@]$", "", m.group(0)))
                if val not in TRIVIAL_NUMBERS and not _is_layout_line(code):
                    lit_uses[val].append((path, sl.n))
    out: list[Candidate] = []
    # (1) same key, different value across places (sizes/limits/consts/comparisons)
    by_key = defaultdict(list)
    for key, val, path, n, kind in named:
        by_key[key].append((val, path, n, kind))
    for key, op, val, path, n in cmps:
        by_key[key].append((val, path, n, "cmp " + op))
    for key, occ in by_key.items():
        vals = {o[0] for o in occ}
        files_ = {o[1] for o in occ}
        if len(vals) >= 2 and len(files_) >= 2 and len(key) >= 3:
            desc = "; ".join(f"{v} ({k}) at {Path(p).name}:L{n}" for v, p, n, k in occ[:8])
            out.append(Candidate("literal_mismatch", "Inconsistency",
                                 f"'{key}' is given different values in different places: {desc}",
                                 [(p, n, n) for _, p, n, _ in occ[:8]], priority=1))
    # (2) same value, different boundary operator across files
    by_val = defaultdict(list)
    for key, op, val, path, n in cmps:
        if op in OPPOSITE:
            by_val[val].append((key, op, path, n))
    for val, occ in by_val.items():
        files_ = {o[2] for o in occ}
        for direction in ("gt", "lt"):
            ops = {o[1] for o in occ if OPPOSITE[o[1]] == direction}
            if len(ops) == 2 and len(files_) >= 2:
                sel = [o for o in occ if OPPOSITE[o[1]] == direction][:8]
                desc = "; ".join(f"{k} {op} {val} at {Path(p).name}:L{n}" for k, op, p, n in sel)
                out.append(Candidate("literal_mismatch", "Inconsistency",
                                     f"The value {val} is compared with both strict and inclusive operators: {desc}",
                                     [(p, n, n) for _, _, p, n in sel], priority=1))
    # (3) same non-trivial literal hard-coded in several files
    for val, uses in lit_uses.items():
        files_ = sorted({p for p, _ in uses})
        try:
            significant = abs(float(val)) >= 5 or "." in val
        except ValueError:
            significant = False
        if len(files_) >= 2 and len(uses) >= 2 and significant and len(uses) <= 12:
            sel = []
            for p in files_:
                sel.extend([u for u in uses if u[0] == p][:2])
            out.append(Candidate("repeated_literal", "Duplicated logic",
                                 f"The literal {val} is hard-coded in {len(files_)} files ({len(uses)} uses).",
                                 [(p, n, n) for p, n in sel[:8]], priority=3))
    return out


def _is_layout_line(code: str) -> bool:
    return bool(re.search(r"\b(Point|Size|SizeF|Padding|Font|Color|Location|TabIndex|Width|Height|"
                          r"AutoScale\w*|ClientSize|Margin)\b", code))


DECL_SKIP = re.compile(r"^\s*(dim|declare|private|public|protected|friend|const|var|let|imports|using|import|"
                       r"option\b|#|end |return\b|exit |next\b|else\b|begin\b|go\b|set nocount|print\b)", re.I)


def norm_expr(code: str) -> str:
    c = code.strip().lower()
    c = re.sub(r"\s+", " ", c)
    c = re.sub(r"\b(then|do|begin)$", "", c).strip().rstrip(";{").strip()
    c = c.replace("andalso", "and").replace("orelse", "or").replace("<>", "!=")
    return c


def scan_duplicates(files: dict[str, list[SrcLine]]) -> list[Candidate]:
    singles = defaultdict(list)
    windows = defaultdict(list)
    for path, lines in files.items():
        if lang_of(path) == "text":
            continue
        code_lines = [(sl.n, norm_expr(sl.code)) for sl in lines if sl.code.strip()]
        for n, e in code_lines:
            if len(e) >= 24 and re.search(r"[<>=+\-*/]|\b(if|where|when|case|and|or)\b", e) \
                    and not DECL_SKIP.match(e) and not re.fullmatch(r"[\w.\s]+=\s*[\w.\"']+", e):
                singles[e].append((path, n))
        for i in range(len(code_lines) - 2):
            trio = code_lines[i:i + 3]
            text = " ⏎ ".join(e for _, e in trio)
            if sum(len(e) for _, e in trio) >= 70 and not all(DECL_SKIP.match(e) for _, e in trio):
                windows[text].append((path, trio[0][0], trio[-1][0]))
    out: list[Candidate] = []
    covered = set()
    for text, occ in windows.items():
        distinct = _distinct(occ)
        if len(distinct) >= 2:
            key = tuple(sorted((p, a) for p, a, _ in distinct))
            if any((p, a) in covered for p, a in key):
                continue
            covered.update(key)
            out.append(Candidate("duplicate_expr", "Duplicated logic",
                                 f"An identical {len(text.split(' ⏎ '))}-line block appears in {len(distinct)} places.",
                                 [(p, a, b) for p, a, b in distinct[:6]], priority=_dup_priority(distinct)))
    for e, occ in singles.items():
        distinct = _distinct([(p, n, n) for p, n in occ])
        if len(distinct) >= 2 and not any((p, a) in covered for p, a, _ in distinct):
            out.append(Candidate("duplicate_expr", "Duplicated logic",
                                 f"The expression `{e[:120]}` appears in {len(distinct)} places.",
                                 [(p, a, b) for p, a, b in distinct[:6]], priority=_dup_priority(distinct) + 1))
    return _merge_adjacent_dups(out)


def _dup_priority(distinct) -> int:
    """Cross-file copies (e.g. client + database) matter more than repetition inside one file."""
    return 2 if len({p for p, _, _ in distinct}) >= 2 else 4


def _distinct(occ):
    """Occurrences in different files, or > 10 lines apart in the same file."""
    res = []
    for p, a, b in sorted(occ):
        if all(p != q or abs(a - c) > 10 for q, c, _ in res):
            res.append((p, a, b))
    return res


def _merge_adjacent_dups(cands: list[Candidate]) -> list[Candidate]:
    """Collapse runs of single-line duplicate candidates that are adjacent in every location."""
    cands.sort(key=lambda c: c.locations)
    merged: list[Candidate] = []
    for c in cands:
        if merged:
            prev = merged[-1]
            if len(prev.locations) == len(c.locations) and all(
                    p == q and pa < a <= pb + 2 for (p, pa, pb), (q, a, _) in zip(prev.locations, c.locations)):
                prev.locations = [(p, pa, max(pb, b)) for (p, pa, pb), (_, _, b) in zip(prev.locations, c.locations)]
                prev.summary = f"A similar multi-line block appears in {len(prev.locations)} places."
                continue
        merged.append(c)
    return merged


VB_DEF = re.compile(r"^\s*(?:(?:Public|Private|Friend|Protected|Shared|Overrides|Overridable|Overloads|"
                    r"Static|Async|Partial|NotOverridable|MustOverride|ReadOnly|WriteOnly)\s+)*"
                    r"(Sub|Function|Property)\s+(\w+)", re.I)
VB_END = re.compile(r"^\s*End\s+(Sub|Function|Property)\b", re.I)
SQL_DEF = re.compile(r"\bCREATE\s+(?:OR\s+(?:ALTER|REPLACE)\s+)?(PROC|PROCEDURE|FUNCTION|VIEW|TABLE|TRIGGER)\s+"
                     r"([\[\]\w.]+)", re.I)
PY_DEF = re.compile(r"^\s*def\s+(\w+)\s*\(")
C_DEF = re.compile(r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|async|final|sealed)\s+)+"
                   r"[\w<>\[\],.?]+\s+(\w+)\s*\(")
SKIP_NAMES = {"new", "main", "dispose", "initializecomponent", "finalize", "tostring", "equals",
              "gethashcode", "__init__", "__main__", "item"}
EVENT_NAME = re.compile(r"_(click|load|unload|changed|change|validating|validated|keypress|keydown|keyup|"
                        r"closing|closed|shown|activated|enter|leave|tick|selectedindexchanged|doubleclick|"
                        r"cellclick|cellvalidating|paint|resize)$", re.I)


def bare(name: str) -> str:
    return name.replace("[", "").replace("]", "").split(".")[-1].lower()


def find_definitions(files: dict[str, list[SrcLine]]) -> list[dict]:
    defs = []
    for path, lines in files.items():
        lang = lang_of(path)
        for i, sl in enumerate(lines):
            code = sl.code
            kind = name = None
            if lang == "vb":
                m = VB_DEF.match(code)
                if m and not re.search(r"\bDeclare\b", code, re.I):
                    kind, name = m.group(1).lower(), m.group(2)
                    if re.search(r"\bHandles\b", code, re.I) or EVENT_NAME.search(name):
                        kind = None
            elif lang == "sql":
                m = SQL_DEF.search(code)
                if m:
                    kind = {"proc": "procedure"}.get(m.group(1).lower(), m.group(1).lower())
                    name = m.group(2)
            elif lang == "hash":
                m = PY_DEF.match(code)
                if m:
                    kind, name = "function", m.group(1)
            elif lang == "c":
                m = C_DEF.match(code)
                if m and m.group(1) not in ("if", "while", "for", "switch", "catch"):
                    kind, name = "method", m.group(1)
            if not kind or bare(name) in SKIP_NAMES:
                continue
            end = _def_end(lines, i, lang)
            defs.append({"file": path, "kind": kind, "name": name, "bare": bare(name),
                         "start": sl.n, "end": end})
    return defs


def _def_end(lines: list[SrcLine], i: int, lang: str) -> int:
    if lang == "vb":
        for sl in lines[i + 1:]:
            if VB_END.match(sl.code):
                return sl.n
            if VB_DEF.match(sl.code):
                break
    if lang == "sql":
        prev = lines[i].n
        for sl in lines[i + 1:]:
            if re.match(r"^\s*GO\s*$", sl.code, re.I) or SQL_DEF.search(sl.code):
                return prev
            prev = sl.n
        return prev
    return min(lines[-1].n, lines[i].n + 30)


def scan_uncalled(files: dict[str, list[SrcLine]], defs: list[dict], graph_called: set[str]) -> list[Candidate]:
    corpus = []
    for path, lines in files.items():
        for sl in lines:
            if sl.code.strip():
                corpus.append((path, sl.n, sl.code.lower()))
    out = []
    for d in defs:
        if d["kind"] == "trigger":
            continue
        if d["bare"] in graph_called:
            continue
        pat = re.compile(rf"(?<![\w]){re.escape(d['bare'])}(?![\w])")
        refs = [(p, n) for p, n, c in corpus if pat.search(c) and not (p == d["file"] and n == d["start"])]
        if d["kind"] == "table":
            refs = [r for r in refs if not (r[0] == d["file"] and d["start"] <= r[1] <= d["end"])]
        if not refs:
            hint = "Dead/obsolete code"
            out.append(Candidate("uncalled", hint,
                                 f"{d['kind'].title()} `{d['name']}` is defined but nothing in the corpus references it.",
                                 [(d["file"], d["start"], d["end"])], priority=3,
                                 extra={"name": d["name"], "kind": d["kind"]}))
    return out


EXEC_REF = re.compile(r"\bEXEC(?:UTE)?\s+(?:@\w+\s*=\s*)?([\[\]\w.]+)", re.I)
TABLE_REF = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+([#@\[\]\w.]+)(?:\s+(?:AS\s+)?(\w+))?", re.I)
STR_IDENT = re.compile(r'"([A-Za-z_][\w.]*\w)"')
PROC_CTX = re.compile(r"stored\s*proc|storedprocedure|commandtype|\bexec\b|\bproc\b|sproc|callproc|procedure", re.I)
SQL_RESERVED = {"select", "set", "where", "values", "inserted", "deleted", "sys", "information_schema",
                "openjson", "openquery", "openrowset", "dual", "table", "the", "a", "an", "to", "with",
                "statistics", "top", "distinct"}


def scan_missing(files: dict[str, list[SrcLine]], defs: list[dict], graph: dict | None) -> list[Candidate]:
    defined = {d["bare"] for d in defs if d["kind"] in ("procedure", "function", "view", "table", "trigger")}
    if not defined:
        return []  # no schema in the corpus: cannot judge what is missing
    refs = defaultdict(list)  # name -> [(file, line, kind)]
    aliases = set()
    ctes = set()
    for path, lines in files.items():
        lang = lang_of(path)
        for idx, sl in enumerate(lines):
            code = sl.code
            for m in re.finditer(r"\b(\w+)\s+AS\s*\(", code, re.I):
                ctes.add(m.group(1).lower())
            for m in EXEC_REF.finditer(code):
                refs[bare(m.group(1))].append((path, sl.n, "procedure"))
            for m in TABLE_REF.finditer(code):
                t = m.group(1)
                if m.group(2):
                    aliases.add(m.group(2).lower())
                if t[0] in "#@(" or bare(t) in SQL_RESERVED:
                    continue
                if lang != "sql" and '"' not in code:
                    continue  # client code: only SQL inside string literals
                refs[bare(t)].append((path, sl.n, "table"))
            if lang != "sql":
                window = " ".join(x.code for x in lines[max(0, idx - 3): idx + 4])
                if PROC_CTX.search(window):
                    for m in STR_IDENT.finditer(code):
                        name = m.group(1)
                        if re.match(r"^(usp|sp|proc|p|fn|up)_", name, re.I) or name.lower() in defined:
                            refs[bare(name)].append((path, sl.n, "procedure"))
    if graph:
        objs = {bare(o.get("object", "")) for o in graph.get("code_objects", [])}
        defined |= objs
        for e in graph.get("code_edges", []):
            if e.get("type") == "REFERENCES" and e.get("kind") in ("table", "stored_procedure", "view", "function"):
                name = bare(e.get("dst", ""))
                if name and name not in defined and name not in refs:
                    refs[name].append((e.get("src", ""), 0, e.get("kind")))
    out = []
    for name, occ in refs.items():
        if name in defined or name in aliases or name in ctes or name in SQL_RESERVED or len(name) < 3:
            continue
        if name.isdigit():
            continue
        locs = [(p, n, n) for p, n, _ in occ if n > 0][:6]
        if not locs:
            continue
        kind = occ[0][2]
        out.append(Candidate("missing_object", "Possible logic flaw",
                             f"{kind.title()} `{name}` is referenced but not defined anywhere in the corpus "
                             f"({len(occ)} reference(s)).", locs, priority=2, extra={"name": name}))
    return out


CODE_LIKE = re.compile(
    r"^\s*(if|else|elseif|end\b|dim|return|set|select|update|insert|delete|exec|for|next|while|call|private|"
    r"public|sub|function|declare|begin|var|let|const|where|and|or|from|case|throw|raiserror)\b"
    r"|[;{}]\s*$|\w+\s*\(.*\)\s*$|\w+(\.\w+)*\s*[+\-*/]?=\s*\S", re.I)
OBSOLETE = re.compile(r"\b(obsolete|deprecated|no longer (used|supported|needed|called)|not used|unused|left in|"
                      r"legacy|replaced by|superseded|removed|retired|disabled|kept for reference|for reference|"
                      r"turned off|dead code|do not use)\b", re.I)
DISABLED_GUARD = re.compile(r"^\s*(#if\s+(0|false)\b|if\s*\(?\s*false\s*\)?\s*(then)?\s*$|while\s*\(?\s*false)|"
                            r"\bwhere\s+1\s*=\s*0\b|\band\s+1\s*=\s*0\b", re.I)


def comment_runs(lines: list[SrcLine]) -> list[list[SrcLine]]:
    runs, cur = [], []
    for sl in lines:
        if sl.full_comment:
            if cur and sl.n != cur[-1].n + 1:
                runs.append(cur)
                cur = []
            cur.append(sl)
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def _is_banner(text: str) -> bool:
    t = text.strip()
    return not t or bool(re.fullmatch(r"[=\-*#_~/'\s]+", t)) or "copyright" in t.lower() or "spdx" in t.lower()


def scan_comments(files: dict[str, list[SrcLine]], defs: list[dict]) -> list[Candidate]:
    out = []
    defs_by_file = defaultdict(list)
    for d in defs:
        defs_by_file[d["file"]].append(d)
    rule_re = re.compile(
        r"\b(must|never|always|only|cannot|can't|can not|not allowed|required|do not|don't|shall|"
        r"per\s+.{0,40}(memo|policy|request|decision)|auditors?|compliance|by hand|manually|"
        r"nothing enforces|not enforced|isn't enforced|no check|not checked|keep in sync|mirror of|"
        r"must match|same as|copy of|duplicated?|todo|fixme|hack|workaround|kludge|bug\s*#?\d+|\?\?\?)", re.I)
    for path, lines in files.items():
        if lang_of(path) == "text":
            continue
        for run in comment_runs(lines):
            texts = [sl.comment for sl in run if not _is_banner(sl.comment)]
            if not texts:
                continue
            a, b = run[0].n, run[-1].n
            joined = " ".join(texts)
            codey = [t for t in texts if CODE_LIKE.search(t) and not re.match(r"^[A-Z][a-z]+(\s+[a-z]+){3,}", t)]
            if len(codey) >= 2 and len(codey) / len(texts) >= 0.5:
                out.append(Candidate("commented_code", "Dead/obsolete code",
                                     f"{len(codey)} commented-out line(s) that look like code.",
                                     [(path, a, b)], priority=3))
            if OBSOLETE.search(joined):
                follow = [d for d in defs_by_file[path] if b < d["start"] <= b + 3]
                locs = [(path, a, b)] + [(path, d["start"], d["end"]) for d in follow]
                what = f" followed by {follow[0]['kind']} `{follow[0]['name']}`" if follow else ""
                out.append(Candidate("commented_code", "Dead/obsolete code",
                                     f"Comment marks code as obsolete/replaced{what}: \"{joined[:160]}\"",
                                     locs, priority=1))
                continue
            m = rule_re.search(joined)
            if m and len(joined) >= 15:
                word = m.group(1).lower()
                if re.search(r"by hand|manually|nothing enforces|not enforced|isn't enforced|no check|not checked", word):
                    hint, pr = "Comment-only rule", 1
                elif re.search(r"sync|mirror|match|same as|copy of|duplicat", word):
                    hint, pr = "Duplicated logic", 1
                elif re.search(r"todo|fixme|hack|workaround|kludge|bug|\?\?\?", word):
                    hint, pr = "Possible logic flaw", 2
                else:
                    hint, pr = "Comment-only rule", 4
                end = b
                nxt = [sl.n for sl in lines if sl.n > b and sl.code.strip()][:3]
                if nxt:
                    end = nxt[-1]
                out.append(Candidate("comment_rule", hint, f"Comment states a rule: \"{joined[:200]}\"",
                                     [(path, a, end)], priority=pr))
        for sl in lines:
            if DISABLED_GUARD.search(sl.code):
                out.append(Candidate("commented_code", "Dead/obsolete code",
                                     f"Code disabled by a constant-false guard: `{sl.code.strip()[:100]}`",
                                     [(path, sl.n, min(sl.n + 5, lines[-1].n))], priority=2))
            elif sl.comment and not sl.full_comment and rule_re.search(sl.comment) and len(sl.comment) >= 15 \
                    and re.search(r"by hand|manually|nothing enforces|not enforced|keep in sync|mirror|must match|"
                                  r"todo|fixme|hack|\?\?\?", sl.comment, re.I):
                out.append(Candidate("comment_rule", "Comment-only rule",
                                     f"Inline comment states a rule: \"{sl.comment[:160]}\"",
                                     [(path, sl.n, sl.n)], priority=2))
    return out


DB_CHECK = re.compile(r"\b(RAISERROR|THROW|ROLLBACK\s+TRAN|CHECK\s*\(|CONSTRAINT\s+\w+\s+CHECK)\b", re.I)


def scan_db_checks(files: dict[str, list[SrcLine]]) -> list[Candidate]:
    out = []
    for path, lines in files.items():
        if lang_of(path) != "sql":
            continue
        for i, sl in enumerate(lines):
            if DB_CHECK.search(sl.code):
                start = sl.n
                for prev in reversed(lines[max(0, i - 4):i]):
                    if re.match(r"^\s*(IF|ELSE\s+IF|WHEN)\b", prev.code, re.I):
                        start = prev.n
                        break
                out.append(Candidate("db_check", "DB-only rule",
                                     f"Database-side check: `{sl.code.strip()[:120]}` — is it also enforced before "
                                     "the database is reached?", [(path, start, sl.n)], priority=4))
    # one candidate per enclosing range is enough
    seen, uniq = set(), []
    for c in out:
        if c.locations[0] not in seen:
            seen.add(c.locations[0])
            uniq.append(c)
    return uniq


def collect_candidates(output_dir: Path, sources: dict[str, dict[int, str]], max_candidates: int = 120) -> list[Candidate]:
    files = {p: analyze(p, lines) for p, lines in sources.items()}
    graph = _load_json(output_dir / "knowledge_graph" / "code_graph.json")
    graph_called = set()
    if graph:
        for e in graph.get("code_edges", []):
            if e.get("type") in ("REFERENCES", "CALLS") and e.get("kind") in ("stored_procedure", "function", None) \
                    and e.get("operation") in ("exec", "call", None) and e.get("type") != "IMPORTS":
                if e.get("kind") == "stored_procedure" or e.get("type") == "CALLS":
                    graph_called.add(bare(e.get("dst", "")))
    defs = find_definitions(files)
    cands = (scan_literals(files) + scan_duplicates(files) + scan_uncalled(files, defs, graph_called)
             + scan_missing(files, defs, graph) + scan_comments(files, defs) + scan_db_checks(files))
    seen, uniq = set(), []
    for c in cands:
        if c.key() not in seen:
            seen.add(c.key())
            uniq.append(c)
    uniq.sort(key=lambda c: (c.priority, c.detector, c.locations))
    uniq = uniq[:max_candidates]
    for i, c in enumerate(uniq, 1):
        c.id = f"C{i}"
    return uniq


# ── Context: stills digest + facts ───────────────────────────────────────────

SIGNAL = re.compile(r"inconsisten|differ|mismatch|client[- ]side only|server[- ]side only|only (client|server)|"
                    r"not (re-?)?(verified|enforced|checked)|bypass|dead|unused|obsolete|legacy|retired|no longer|"
                    r"commented|duplicat|mirror|keep in sync|hard-?coded|undocumented|hidden|silent|todo|bug|flaw|"
                    r"never called|not called|missing|by hand|manual|gap|contradict|risk", re.I)


def stills_digest(output_dir: Path, limit: int = 60000) -> str:
    stills = output_dir / "stills"
    blocks = []
    for md in sorted(stills.glob("*/final/*.md")) if stills.is_dir() else []:
        if md.name == "README.md":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        for blk in re.split(r"\n(?=### )", text):
            if not blk.startswith("### "):
                continue
            hits = [ln.strip() for ln in blk.splitlines()[1:] if SIGNAL.search(ln)]
            if not hits:
                continue
            ev = [ln.strip() for ln in blk.splitlines() if ln.strip().lower().startswith("- evidence")]
            body = "\n".join(dict.fromkeys(ev[:1] + hits))[:900]
            blocks.append(f"[{md.parent.parent.name}] {blk.splitlines()[0][4:].strip()}\n{body}")
    out, total = [], 0
    for b in blocks:
        if total + len(b) > limit:
            break
        out.append(b)
        total += len(b) + 2
    return "\n\n".join(out)


def facts_digest(output_dir: Path, limit: int = 30000) -> str:
    data = _load_json(output_dir / "facts" / "facts.json")
    if data is None:
        return ""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text[:limit] + (" …(truncated)" if len(text) > limit else "")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── LLM pass ─────────────────────────────────────────────────────────────────

def default_llm(model_id: str, region: str, profile: str):
    sys.path.insert(0, str(HERE))
    import bedrock_client  # noqa: WPS433 (lazy: tests and --no-llm runs need no boto3)
    client = bedrock_client.create_client(profile, region)

    def call(system: str, user: str, max_tokens: int):
        t0 = time.time()
        r = bedrock_client.call_converse_with_retry(client, model_id, system, user, max_tokens)
        bedrock_client.log_llm_call(status="ok", input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                                    latency_ms=int((time.time() - t0) * 1000), model_id=model_id,
                                    phase="issues")
        return r.output_text, r.stop_reason, r.input_tokens, r.output_tokens
    return call


NON_ISSUE = re.compile(r"\b(not (an? )?(actionable|issue|problem|business rule|rule|defect)s?|informational only|"
                       r"accurately describ\w*|implementation noise|no (issue|problem|action needed)|"
                       r"(is|are) (just |only )?(boilerplate|documentation)|hygiene items?)\b", re.I)


def build_user_message(cands: list[Candidate], sources, stills: str, facts: str, full_source: bool,
                       discovery: bool) -> str:
    parts = ["# CANDIDATES\n"]
    for c in cands:
        parts.append(f"## {c.id} [{c.detector} → hint: {c.hint}]\n{c.summary}")
        for p, a, b in c.locations[:4]:
            if not full_source:
                parts.append(render_numbered(p, excerpt(sources, p, a, b, pad=3, max_lines=25)))
            else:
                parts.append(f"  at {p}:L{a}-L{b}")
        parts.append("")
    if stills:
        parts.append("# SPEC NOTES (from the distilled specification)\n" + stills)
    if facts:
        parts.append("# FACTS\n" + facts)
    if full_source:
        parts.append("# SOURCE (whole corpus)\n")
        for p in sorted(sources):
            parts.append(render_numbered(p, sorted(sources[p].items())))
    parts.append("\n# TASK\n" + (
        "Decide every candidate above (confirm/merge or reject), THEN add the issues you find in the source "
        "and spec notes that no candidate covers." if discovery else
        "Decide every candidate above (confirm/merge or reject). Do not add new issues in this batch."))
    return "\n".join(parts)


def run_llm(cands, sources, output_dir: Path, llm, max_calls: int, max_cost: float,
            source_char_cap: int, max_tokens: int, log=print, batch_size: int = 100) -> tuple[list[dict], list[dict], dict]:
    system = PROMPT_PATH.read_text(encoding="utf-8")
    stills = stills_digest(output_dir)
    facts = facts_digest(output_dir)
    total_src = sum(len(t) + 8 for lines in sources.values() for t in lines.values())
    full_source = total_src <= source_char_cap
    # batch candidates so each call stays bounded (one call when they fit); the first batch also does
    # open discovery, later batches see what was already reported so they do not repeat it
    n_batches = min(max_calls, max(1, -(-len(cands) // max(1, batch_size)))) if cands else 1
    per_batch = max(1, -(-len(cands) // n_batches)) if cands else 0
    batches = [cands[i:i + per_batch] for i in range(0, len(cands), per_batch)] if cands else [[]]
    batches = batches[:max_calls]
    issues, rejected = [], []
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
             "full_source_in_prompt": full_source}
    for bi, batch in enumerate(batches):
        user = build_user_message(batch, sources, stills if bi == 0 else "", facts if bi == 0 else "",
                                  full_source, discovery=(bi == 0))
        if issues:
            user += ("\n\n# ALREADY REPORTED (earlier batch) — do not repeat these; reject a candidate they cover "
                     "with reason 'covered by: <title>'\n" + "\n".join(
                         f"- {it.get('title', '')}" for it in issues))
        est = (len(system) + len(user)) / 3.5 * PRICE_IN + max_tokens * PRICE_OUT
        if usage["cost_usd"] + est > max_cost:
            log(f"  [issues] budget guard: skipping batch {bi + 1} (est ${est:.2f}, spent ${usage['cost_usd']:.2f})")
            continue
        log(f"  [issues] LLM batch {bi + 1}/{len(batches)}: {len(batch)} candidates, ~{len(user) // 4} input tokens")
        text, stop, tin, tout = llm(system, user, max_tokens)
        usage["calls"] += 1
        usage["input_tokens"] += tin
        usage["output_tokens"] += tout
        usage["cost_usd"] = round(usage["cost_usd"] + tin * PRICE_IN + tout * PRICE_OUT, 4)
        data = parse_llm_json(text)
        if data is None:
            raise RuntimeError(f"issues LLM batch {bi + 1} returned unparseable JSON (stop={stop})")
        if stop == "max_tokens":
            log(f"  [issues] WARNING batch {bi + 1} hit max_tokens; using the issues that parsed")
        for it in data.get("issues", []) or []:
            if isinstance(it, dict):
                it["_batch"] = bi + 1
                issues.append(it)
        rejected.extend(r for r in data.get("rejected", []) or [] if isinstance(r, dict))
    return issues, rejected, usage


def parse_llm_json(text: str):
    t = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", (text or "").strip())
    try:
        return json.loads(t)
    except ValueError:
        pass
    sys.path.insert(0, str(HERE))
    try:
        from json_extract import strip_json
        return json.loads(strip_json(t, "object"))
    except Exception:  # noqa: BLE001
        pass
    # truncated output: keep every complete issue object
    m = re.search(r'"issues"\s*:\s*\[', t)
    if not m:
        return None
    dec = json.JSONDecoder()
    i, items = m.end(), []
    while True:
        while i < len(t) and t[i] in " \n\r\t,":
            i += 1
        try:
            obj, j = dec.raw_decode(t, i)
        except ValueError:
            break
        items.append(obj)
        i = j
    return {"issues": items, "rejected": []}


def _pick(value: str, allowed: list[str], default: str) -> str:
    v = (value or "").strip().lower().replace("behavior", "behaviour")
    for a in allowed:
        if v == a.lower():
            return a
    for a in allowed:
        if a.lower().split()[0].split("/")[0] in v:
            return a
    return default


def finalize_issues(raw: list[dict], idx: SourceIndex, cands: list[Candidate]) -> tuple[list[dict], dict]:
    """Normalize fields, verify locations, drop unverifiable, merge duplicates, rank, assign ids."""
    cand_by_id = {c.id: c for c in cands}
    out, dropped, fixed = [], [], 0
    for it in raw:
        if NON_ISSUE.search(str(it.get("title", ""))):
            dropped.append({"title": it.get("title", ""), "reason": "model described a non-issue"})
            continue
        locs = it.get("locations") or []
        if isinstance(locs, (str, dict)):
            locs = [locs]
        verified = []
        for loc in locs:
            v = verify_location(idx, loc)
            if v:
                verified.append(v)
        if not verified:  # fall back to the cited candidates' own (deterministic, known-good) ranges
            for cid in it.get("candidate_ids") or []:
                c = cand_by_id.get(cid)
                for p, a, b in (c.locations if c else []):
                    v = verify_location(idx, {"file": p, "start": a, "end": b})
                    if v:
                        verified.append(v)
                        fixed += 1
        if not verified:
            dropped.append({"title": it.get("title", ""), "reason": "no verifiable location"})
            continue
        seen, uniq = set(), []
        for v in verified:
            if v["ref"] not in seen:
                seen.add(v["ref"])
                uniq.append(v)
        ev = _pick(it.get("evidence_class", ""), EVIDENCE, "Unknown")
        if ev == "Verified" and any(v["quote_verified"] is False for v in uniq):
            ev = "Inferred"  # a quoted fragment could not be found in the source
        out.append({
            "category": _pick(it.get("category", ""), CATEGORIES, "Undocumented behaviour"),
            "title": str(it.get("title", "")).strip()[:160] or "Untitled issue",
            "description": str(it.get("description", "")).strip(),
            "business_impact": str(it.get("business_impact", "")).strip(),
            "locations": uniq,
            "evidence_class": ev,
            "severity": _pick(it.get("severity", ""), SEVERITY, "Medium"),
            "sme_question": str(it.get("sme_question", "")).strip(),
            "candidate_ids": [c for c in (it.get("candidate_ids") or []) if isinstance(c, str)],
            "source": "candidate" if it.get("candidate_ids") else "discovered",
        })
    out = _merge_same(out)
    out.sort(key=rank_key)
    for i, it in enumerate(out, 1):
        it["id"] = f"ISS-{i:03d}"
    return out, {"dropped": dropped, "locations_backfilled_from_candidates": fixed}


def _title_words(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) > 3}


def _overlap(a: dict, b: dict) -> bool:
    return a["file"] == b["file"] and a["start"] <= b["end"] and b["start"] <= a["end"]


def _same_issue(m: dict, it: dict) -> bool:
    """Same title, or most locations overlap AND the titles share vocabulary (cross-batch repeats)."""
    tm, ti = _title_words(m["title"]), _title_words(it["title"])
    if tm == ti:
        return True
    small, big = sorted((m["locations"], it["locations"]), key=len)
    hit = sum(1 for a in small if any(_overlap(a, b) for b in big))
    jac = len(tm & ti) / max(1, len(tm | ti))
    return hit / max(1, len(small)) >= 0.5 and jac >= 0.2


def _merge_same(issues: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for it in sorted(issues, key=rank_key):
        dup = next((m for m in merged if _same_issue(m, it)), None)
        if dup:
            have = {v["ref"] for v in dup["locations"]}
            dup["locations"] += [v for v in it["locations"] if v["ref"] not in have]
            dup["candidate_ids"] = sorted(set(dup["candidate_ids"]) | set(it["candidate_ids"]))
        else:
            merged.append(it)
    return merged


def rank_key(it: dict):
    return (SEVERITY.index(it["severity"]), EVIDENCE.index(it["evidence_class"]),
            CATEGORIES.index(it["category"]), -len(it["locations"]), it["title"])


def deterministic_issues(cands: list[Candidate]) -> list[dict]:
    """--no-llm: one issue per candidate, factual text only, evidence 'Confirmation Required'."""
    out = []
    for c in cands:
        out.append({"candidate_ids": [c.id], "category": c.hint, "title": c.summary.split(":")[0][:100],
                    "description": c.summary,
                    "business_impact": "Not assessed — deterministic scan only (no model review).",
                    "locations": [{"file": p, "start": a, "end": b} for p, a, b in c.locations],
                    "evidence_class": "Confirmation Required",
                    "severity": "Medium" if c.priority <= 2 else "Low",
                    "sme_question": ""})
    return out


# ── Report writers ───────────────────────────────────────────────────────────

def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def write_markdown(path: Path, target: str, issues: list[dict], meta: dict) -> None:
    lines = [f"# {target} — Issues & Insights", "",
             "Prototype output for SME review: each item is a question to settle before modernization, "
             "not a confirmed defect. Locations were checked against the source pages.", "",
             f"**Issues:** {len(issues)}  ", f"**Candidates scanned:** {meta.get('candidates', 0)}  ",
             f"**Model:** {meta.get('model') or 'none (deterministic only)'}", "",
             "## Summary", "",
             "| # | Category | Title | Severity | Evidence | Where |", "|---|---|---|---|---|---|"]
    for it in issues:
        first = it["locations"][0]["ref"] + (f" (+{len(it['locations']) - 1})" if len(it["locations"]) > 1 else "")
        lines.append(f"| {it['id']} | {it['category']} | {_md_escape(it['title'])} | {it['severity']} | "
                     f"{it['evidence_class']} | {_md_escape(first)} |")
    counts = defaultdict(int)
    for it in issues:
        counts[it["category"]] += 1
    lines += ["", "**By category:** " + ", ".join(f"{c}: {counts[c]}" for c in CATEGORIES if counts[c]), ""]
    for cat in CATEGORIES:
        group = [it for it in issues if it["category"] == cat]
        if not group:
            continue
        lines += [f"## {cat}", ""]
        for it in group:
            lines += [f"### {it['id']} — {it['title']}", "",
                      f"- **Severity:** {it['severity']} · **Evidence:** {it['evidence_class']}",
                      f"- **What we found:** {it['description']}",
                      f"- **Business impact:** {it['business_impact']}",
                      "- **Locations:** " + ", ".join(f"`{v['ref']}`" for v in it["locations"])]
            if it.get("sme_question"):
                lines.append(f"- **Question for an SME:** {it['sme_question']}")
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def candidate_dict(c: Candidate) -> dict:
    return {"id": c.id, "detector": c.detector, "hint": c.hint, "summary": c.summary,
            "locations": [f"{p}:L{a}-L{b}" for p, a, b in c.locations]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Issues & Insights report (deterministic scan + bounded LLM review)")
    ap.add_argument("output_dir")
    ap.add_argument("--target", default="")
    ap.add_argument("--out-dir", default="", help="default: <output_dir>/issues")
    ap.add_argument("--model", default=os.environ.get("DISTILL_ISSUES_MODEL")
                    or os.environ.get("BEDROCK_MODEL_ID") or "us.anthropic.claude-sonnet-4-6")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""))
    ap.add_argument("--max-calls", type=int, default=int(os.environ.get("DISTILL_ISSUES_MAX_CALLS", "3")))
    ap.add_argument("--max-cost", type=float, default=float(os.environ.get("DISTILL_ISSUES_MAX_COST", "3.0")))
    ap.add_argument("--max-candidates", type=int, default=120)
    ap.add_argument("--max-tokens", type=int, default=32000)
    ap.add_argument("--batch-size", type=int, default=100, help="candidates per LLM call")
    ap.add_argument("--source-char-cap", type=int, default=int(os.environ.get("DISTILL_ISSUES_SOURCE_CHARS", "200000")),
                    help="send the whole numbered corpus when it is smaller than this; else excerpts only")
    ap.add_argument("--no-llm", action="store_true", help="deterministic candidates only (no AWS)")
    args = ap.parse_args(argv)
    return run(Path(args.output_dir), args)


def run(output_dir: Path, args, llm=None, log=print) -> int:
    out_dir = Path(args.out_dir) if getattr(args, "out_dir", "") else output_dir / "issues"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = getattr(args, "target", "") or output_dir.name
    sources = load_sources(output_dir)
    if not sources:
        log(f"  [issues] ERROR no numbered source pages under {output_dir}/.pages")
        return 2
    cands = collect_candidates(output_dir, sources, getattr(args, "max_candidates", 120))
    by_det = defaultdict(int)
    for c in cands:
        by_det[c.detector] += 1
    log(f"  [issues] {len(sources)} source files, {len(cands)} candidates {dict(by_det)}")
    idx = SourceIndex(sources)
    usage, rejected = {}, []
    if getattr(args, "no_llm", False):
        raw = deterministic_issues(cands)
        model = ""
    else:
        model = args.model
        llm = llm or default_llm(model, args.region, args.profile)
        try:
            raw, rejected, usage = run_llm(cands, sources, output_dir, llm, max(1, args.max_calls), args.max_cost,
                                           args.source_char_cap, args.max_tokens, log=log,
                                           batch_size=getattr(args, "batch_size", 100))
        except Exception as e:  # noqa: BLE001 — any model failure fails the stage
            log(f"  [issues] ERROR LLM review failed: {type(e).__name__}: {e}")
            return 2
    issues, vmeta = finalize_issues(raw, idx, cands)
    meta = {"target": target, "model": model, "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_files": len(sources), "candidates": len(cands), "candidates_by_detector": dict(by_det),
            "facts_json_present": (output_dir / "facts" / "facts.json").is_file(),
            "usage": usage, **vmeta,
            "inputs_sha256": hashlib.sha256(json.dumps(sorted((p, len(v)) for p, v in sources.items())).encode()).hexdigest()}
    status = "ok"
    if cands and not issues:
        status = "failed: candidates existed but no issue survived review/verification"
    doc = {"status": status, "meta": meta, "issues": issues, "rejected_candidates": rejected,
           "candidates": [candidate_dict(c) for c in cands]}
    (out_dir / "issues.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    md = out_dir / "issues.md"
    if status != "ok":
        if md.exists():
            md.unlink()
        log(f"  [issues] ERROR {status}")
        return 3
    write_markdown(md, target, issues, meta)
    log(f"  [issues] {len(issues)} issues → {md} (cost ${usage.get('cost_usd', 0):.2f}, "
        f"dropped {len(vmeta['dropped'])} unverifiable)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

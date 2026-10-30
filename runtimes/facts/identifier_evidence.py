#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""identifier_evidence.py: Phase 1.3 (identifier evidence). Deterministic, no model, no dotnet.

Legacy code is full of abbreviated column and variable names. A map call that sees one tends to
expand it from its letters alone, and a wrong expansion then propagates into every statement that
uses it (a one-letter prefix can stand for export, expense, expiry or executive). This step
collects, for every abbreviated identifier in the corpus, the nearby evidence of what it means:

  stated   a comment that mentions it (same line, the line before its definition, or a glossary
           comment anywhere), or a UI label string on a line that names one of its controls
  def      its column / parameter / variable definition (type, default, CHECK)
  set      where it is assigned, with the value and the enclosing routine (e.g. a routine named
           for what it does)
  tested   where it is compared, with the value and the enclosing routine

and renders one bounded "Identifier evidence" block per page (the abbreviated names that occur on
that page, with evidence from anywhere in the corpus, every item cited file:line). The page-centric
map appends it to the page with an instruction to expand an abbreviation only when the evidence
supports it and otherwise to write "meaning: Unknown".

Everything is generic: abbreviation detection uses segment shape (short or vowel-less CamelCase
segments that are not common words), not a domain vocabulary.

Outputs (under --out):
  pages/page_NNN.md        the per-page block (absent when a page has no abbreviated names)
  identifiers.json         the corpus-wide glossary (all candidates and their evidence)
  identifiers_status.json  {"status": "ok"|"skipped", "reason", "counts", "blocks_hash", ...}

Usage:
  identifier_evidence.py build --source-root S --manifest M --pages P --out O
                               [--max-chars 4000] [--max-entries 24]
  identifier_evidence.py hash --out O     # prints blocks_hash, or "none"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

HEADER = (
    "IDENTIFIER EVIDENCE FOR ABBREVIATED NAMES ON THIS PAGE (deterministic scan of the whole "
    "corpus; no model).\n"
    "Rule: expand an abbreviation ONLY when the evidence below or the source on this page "
    "supports it (a comment or UI label that states it, its column definition, or how the value "
    "is set and tested, e.g. the routine that sets it), and cite that evidence. If nothing "
    "supports an expansion, keep the name verbatim and write \"meaning: Unknown\". Never expand "
    "from the letters alone ('Ref' could be reference, refund or referral).\n"
    "[stated] = a comment or label states the meaning; [usage] = only how it is set or tested; "
    "[none] = nothing beyond its declaration."
)

DEFAULT_MAX_CHARS = 4000
DEFAULT_MAX_ENTRIES = 24
SNIPPET = 70

LANG_BY_EXT = {".vb": "vb", ".bas": "vb", ".cls": "vb", ".frm": "vb", ".cs": "cs",
               ".sql": "sql", ".prc": "sql", ".trg": "sql", ".tab": "sql"}

# Generic words and programming abbreviations that do NOT make a name cryptic.
COMMON = set("""
a an and are as at be by do for from get go has if in is it of on or set the to up
add all any app ask bad big bit box can day due end few fix id key log map max min net new no
not now off old one out own pay per put raw ref run sum tag tax top two use via yes
area auto back base bill body book call card case cash check city code copy cost data date
days deal desk done door down draw drop each edit else empty end entry error event exit fail
fast file fill find first flag flat form from full fund give good grid group hand head help
high hold home hour icon item join just keep kind last late left less level line link list
load lock long look loop main make mark mode month more move much must name next node none
note null number open order page paid part pass path plan post print rate read real role row
rule save send ship show sign site size sort start state step stop sub table task team term
test text then time title total type unit user valid value view void wait week what when
where work year zone count index month status amount
sql xml html http url uri api db ui io os vb net dll exe sys
str int dec dbl bool obj msg btn txt lbl chk cbo cmb lst grd dgv pnl frm mnu tmr err ex
dt ds dr dc da cmd cn conn param params parm parms arg args val vals var tmp temp num idx cnt
ctx cfg init len pos src dst min max avg qty pct no
""".split())

KEYWORDS = set(k.lower() for k in """
AddHandler AddressOf Alias And AndAlso As Boolean ByRef Byte ByVal Call Case Catch CBool CByte
CChar CDate CDbl CDec Char CInt Class CLng CObj Const Continue CSByte CShort CSng CStr CType
CUInt CULng CUShort Date Decimal Declare Default Delegate Dim DirectCast Do Double Each Else
ElseIf End EndIf Enum Erase Error Event Exit False Finally For Friend Function Get GetType
Global GoTo Handles If Implements Imports In Inherits Integer Interface Is IsNot Let Lib Like
Long Loop Me Mod Module MustInherit MustOverride MyBase MyClass Namespace Narrowing New Next Not
Nothing NotInheritable NotOverridable Object Of On Operator Option Optional Or OrElse Overloads
Overridable Overrides ParamArray Partial Private Property Protected Public RaiseEvent ReadOnly
ReDim REM RemoveHandler Resume Return SByte Select Set Shadows Shared Short Single Static Step
Stop String Structure Sub SyncLock Then Throw To True Try TryCast TypeOf UInteger ULong UShort
Using Variant When While Widening With WithEvents WriteOnly Xor IIf Trim Format Len Mid Left
Right UCase LCase Val IsDBNull DBNull IsNumeric MsgBox Now Today
abstract base bool break catch char checked const continue decimal delegate double enum
explicit extern false finally fixed float foreach goto implicit int internal lock namespace
null operator out override params readonly ref sbyte sealed sizeof stackalloc static struct
switch this throw true typeof uint ulong unchecked unsafe ushort using virtual void volatile var
ADD ALL ALTER ANY ASC AUTHORIZATION BACKUP BEGIN BETWEEN BIGINT BINARY BIT BREAK BROWSE BULK BY
CASCADE CASE CAST CHAR CHARINDEX CHECK CHECKPOINT CLOSE CLUSTERED COALESCE COLLATE COLUMN COMMIT
COMPUTE CONSTRAINT CONTAINS CONTINUE CONVERT COUNT CREATE CROSS CURRENT CURSOR DATABASE DATEADD
DATEDIFF DATEPART DATETIME DATETIME2 DEALLOCATE DECLARE DEFAULT DELETE DENY DESC DISTINCT DROP
ELSE END ERRLVL ESCAPE EXCEPT EXEC EXECUTE EXISTS EXIT FETCH FILLFACTOR FLOAT FOR FOREIGN FROM
FULL FUNCTION GETDATE GO GOTO GRANT GROUP HAVING IDENTITY IF IN INDEX INNER INSERT INT INTERSECT
INTO IS ISNULL JOIN KEY KILL LEFT LIKE MAX MERGE MIN MONEY NCHAR NOCHECK NOCOUNT NONCLUSTERED NOT
NULL NULLIF NUMERIC NVARCHAR OF OFF ON OPEN OPTION OR ORDER OUTER OUTPUT OVER PERCENT PRIMARY
PRINT PROC PROCEDURE RAISERROR READ REAL REFERENCES RETURN REVERT ROLLBACK ROUND ROWCOUNT RULE
SAVE SCHEMA SELECT SET SMALLDATETIME SMALLINT SMALLMONEY SOME SUM TABLE TEXT THEN THROW TINYINT
TOP TRAN TRANSACTION TRIGGER TRUNCATE TRY UNION UNIQUE UNIQUEIDENTIFIER UPDATE USE VALUES VARCHAR
VIEW WHEN WHERE WHILE WITH XACT_ABORT ANSI_NULLS QUOTED_IDENTIFIER ANSI_PADDING XML dbo inserted deleted sp_executesql YEAR MONTH DAY
System Data Collections Generic Windows Forms Drawing SqlClient SqlCommand SqlConnection
SqlParameter SqlDbType DataTable DataRow DataSet DataColumn DataView Console Math Convert
Exception EventArgs Sender
""".split())

SQL_WORDS = re.compile(r"\b(SELECT|UPDATE|INSERT|DELETE|FROM|WHERE|EXEC|JOIN|VALUES|SET)\b", re.I)
IDENT = re.compile(r"@{0,2}[A-Za-z_][A-Za-z0-9_]*")
SEG = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
VOWELS = set("aeiouyAEIOUY")

ROUTINE_RES = {
    "vb": re.compile(r"^\s*(?:(?:Public|Private|Friend|Protected|Shared|Overrides|Overridable|"
                     r"Static|Async|Partial)\s+)*(?:Function|Sub|Property)\s+([A-Za-z_]\w*)", re.I),
    "cs": re.compile(r"^\s*(?:(?:public|private|internal|protected|static|virtual|override|async|"
                     r"sealed|partial)\s+)+[\w<>\[\],.? ]+?\s+([A-Za-z_]\w*)\s*\("),
    "sql": re.compile(r"^\s*(?:CREATE|ALTER)\s+(?:OR\s+ALTER\s+)?(?:PROC|PROCEDURE|TRIGGER|FUNCTION|"
                      r"VIEW)\s+((?:\[?\w+\]?\.)?\[?\w+\]?)", re.I),
}
TYPE_DEF_RES = {
    "vb": re.compile(r"^\s*(?:(?:Public|Private|Friend|Protected|Partial|Shared)\s+)*"
                     r"(?:Class|Module|Structure|Interface|Enum)\s+([A-Za-z_]\w*)", re.I),
    "cs": re.compile(r"\b(?:class|struct|interface|enum|record)\s+([A-Za-z_]\w*)"),
    "sql": re.compile(r"^\s*CREATE\s+TABLE\s+((?:\[?\w+\]?\.)?\[?\w+\]?)", re.I),
}
SQL_TYPE = (r"(?:var)?n?char|varchar|nvarchar|int|bigint|smallint|tinyint|bit|numeric|decimal|money|"
            r"smallmoney|float|real|date|datetime2?|smalldatetime|time|uniqueidentifier|text|ntext|"
            r"varbinary|binary|xml")


# ── abbreviation shape ──────────────────────────────────────────────────────────────────────
def canonical(tok: str) -> str:
    """The name a token refers to: '@X' and 'm_X' are X; a control or Hungarian-prefixed variable
    ('chkX', 'txtX', 'decX') is X when X is itself an abbreviated name."""
    t = tok.lstrip("@")
    t = re.sub(r"^(?:m_|g_|s_|_)", "", t)
    m = re.match(r"^[a-z]{1,4}(?=[A-Z])", t)
    if m and len(t) - m.end() >= 3 and abbreviation_score(t[m.end():]) > 0:
        return t[m.end():]
    return t


def segments(name: str) -> list[str]:
    return SEG.findall(name)


def abbreviation_score(name: str) -> int:
    """>0 when the name looks abbreviated: CamelCase / acronym segments that are short or have no
    vowel and are not common words. 0 for plain words, keywords and all-lowercase locals."""
    if not (3 <= len(name) <= 24) or name.lower() in KEYWORDS or not re.search(r"[A-Z]", name):
        return 0
    if name.isupper() and len(name) <= 3:
        return 0
    score = 0
    segs = [s for s in segments(name) if not s.isdigit()]
    if not segs:
        return 0
    for s in segs:
        low = s.lower()
        if low in COMMON or len(s) == 1:
            continue
        if not (set(s) & VOWELS):
            score += 2            # vowel-less: Nbr, Pmt, Dsc
        elif len(s) <= 4:
            score += 1            # short, not a common word: Cust, Addr
    return min(score, 4)


def is_alias(tok: str, name: str) -> bool:
    """tok names the same thing as name: exact, or a control/variable prefix (chkX, txtX, m_X)."""
    c = canonical(tok)
    if c == name:
        return True
    return c.endswith(name) and re.fullmatch(r"[a-z]{1,4}", c[: len(c) - len(name)] or "") is not None


# ── line splitting ──────────────────────────────────────────────────────────────────────────
def split_line(line: str, lang: str, in_block: bool) -> tuple[str, str, list[str], bool]:
    """(code without strings/comments, comment text, string literals, still-in-block-comment)."""
    code, comment, strings = [], [], []
    i, n = 0, len(line)
    q = "'" if lang == "sql" else '"'
    while i < n:
        if in_block:
            j = line.find("*/", i)
            if j < 0:
                comment.append(line[i:]); i = n; break
            comment.append(line[i:j]); i = j + 2; in_block = False; continue
        ch = line[i]
        two = line[i:i + 2]
        if lang in ("sql", "cs") and two == "/*":
            in_block = True; i += 2; continue
        if (lang == "sql" and two == "--") or (lang == "cs" and two == "//") or \
                (lang == "vb" and (ch == "'" or re.match(r"REM\b", line[i:], re.I) and not "".join(code).strip())):
            comment.append(line[i + (1 if lang == "vb" and ch == "'" else 2):]); break
        if lang not in ("sql", "cs", "vb") and ch == "#":
            comment.append(line[i + 1:]); break
        if ch == q:
            j = i + 1
            buf = []
            while j < n:
                if line[j] == q and j + 1 < n and line[j + 1] == q:
                    buf.append(q); j += 2; continue
                if line[j] == q:
                    break
                buf.append(line[j]); j += 1
            lit = "".join(buf)
            strings.append(lit)
            # vb/cs: inline SQL and column-name strings stay scannable ("SET X='Y'", r("X"));
            # natural-language strings (labels, messages) are blanked out of the code part
            code.append(f" {lit} " if lang in ("vb", "cs") and not _natural(lit) else " ")
            i = j + 1
            continue
        code.append(ch)
        i += 1
    return "".join(code), " ".join(c.strip() for c in comment).strip(), strings, in_block


def _snip(text: str, around: str | None = None, width: int = SNIPPET) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) <= width:
        return t
    if around and around in t:
        k = t.index(around)
        a = max(0, k - width // 3)
        piece = t[a:a + width]
        return ("…" if a else "") + piece + ("…" if a + width < len(t) else "")
    return t[: width - 1] + "…"


def _natural(s: str) -> bool:
    words = re.findall(r"[A-Za-z]{2,}", s)
    return len(words) >= 2 and len(s) <= 120 and not SQL_WORDS.search(s) and "{" not in s


# ── corpus scan ─────────────────────────────────────────────────────────────────────────────
class _Entry:
    def __init__(self, name: str):
        self.name = name
        self.score = abbreviation_score(name)
        self.occ: dict[str, set[int]] = defaultdict(set)   # file -> lines
        self.ev: dict[str, list[dict]] = defaultdict(list)  # kind -> items
        self.is_column = False

    def add(self, kind: str, file: str, line: int, text: str, routine: str = "", cap: int = 3,
            first: bool = False):
        """Keep at most `cap` items per kind; `first` items (e.g. glossary-style "X = meaning"
        comments) go to the front and may displace a weaker one."""
        items = self.ev[kind]
        if any(e["file"] == file and e["line"] == line for e in items):
            return
        item = {"file": file, "line": line, "text": text, **({"in": routine} if routine else {})}
        if first:
            items.insert(0, item)
            del items[cap:]
        elif len(items) < cap:
            items.append(item)

    @property
    def status(self) -> str:
        if self.ev.get("comment") or self.ev.get("label"):
            return "stated"
        if self.ev.get("set") or self.ev.get("tested"):
            return "usage"
        return "none"

    def to_json(self) -> dict:
        return {"name": self.name, "status": self.status, "abbreviation_score": self.score,
                "column_or_parameter": self.is_column,
                "occurrences": sum(len(v) for v in self.occ.values()),
                "evidence": {k: v for k, v in self.ev.items() if v}}


def _read_text(p: Path) -> str | None:
    try:
        b = p.read_bytes()
    except OSError:
        return None
    if b"\0" in b[:4096]:
        return None
    return b.decode("utf-8", "replace")


def scan(files: dict[str, str]) -> dict[str, _Entry]:
    """files: {rel_path: text}. Returns {name: _Entry} for abbreviated identifiers."""
    entries: dict[str, _Entry] = {}
    excluded: set[str] = set()       # routine / type / table names
    parsed: dict[str, list[tuple[str, str, list[str], str]]] = {}

    for rel, text in files.items():
        lang = LANG_BY_EXT.get(Path(rel).suffix.lower(), "other")
        rows, in_block, routine = [], False, ""
        rre, tre = ROUTINE_RES.get(lang), TYPE_DEF_RES.get(lang)
        for raw in text.splitlines():
            code, comment, strings, in_block = split_line(raw, lang, in_block)
            if rre:
                m = rre.match(code)
                if m:
                    routine = m.group(1).split(".")[-1].strip("[]")
                    excluded.update((routine, canonical(routine)))
            if tre:
                for m in tre.finditer(code):
                    t = m.group(1).split(".")[-1].strip("[]")
                    excluded.update((t, canonical(t)))
            rows.append((code, comment, strings, routine, raw))
        parsed[rel] = rows

    # pass 1: candidates + occurrences. `bare` counts uses that are not `.Member` access in
    # VB/C# code, so framework members (x.AddWithValue, .ForeColor) can be dropped below.
    bare: dict[str, int] = defaultdict(int)
    for rel, rows in parsed.items():
        is_sql = LANG_BY_EXT.get(Path(rel).suffix.lower()) == "sql"
        for ln, (code, _c, _s, _r, _raw) in enumerate(rows, 1):
            for m in IDENT.finditer(code):
                name = canonical(m.group(0))
                if name in excluded or abbreviation_score(name) == 0:
                    continue
                # constraint / handler names built on a table, type or routine name (dfTbl_Col, frmX_Load)
                if "_" in name and name.split("_")[0] in excluded:
                    continue
                e = entries.get(name) or entries.setdefault(name, _Entry(name))
                e.occ[rel].add(ln)
                if is_sql or m.start() == 0 or code[m.start() - 1] != ".":
                    bare[name] += 1

    names = sorted(entries, key=len, reverse=True)
    # pass 2: evidence
    for rel, rows in parsed.items():
        lang = LANG_BY_EXT.get(Path(rel).suffix.lower(), "other")
        in_table = False
        for ln, (code, comment, strings, routine, raw) in enumerate(rows, 1):
            if lang == "sql":
                if re.match(r"^\s*CREATE\s+TABLE\b", code, re.I):
                    in_table = True
                elif in_table and re.match(r"^\s*\)\s*;?\s*$", code) or re.match(r"^\s*GO\s*$", code, re.I):
                    in_table = False
            toks = [canonical(t) for t in IDENT.findall(code)]
            raw_toks = IDENT.findall(code)
            present = {t for t in toks if t in entries}
            # aliases (chkX -> X) present on the line
            for t in raw_toks:
                c = canonical(t)
                if c in entries:
                    continue
                for nm in names:
                    if len(nm) < len(c) and is_alias(t, nm):
                        present.add(nm)
                        break
            if comment and len(re.findall(r"[A-Za-z]{2,}", comment)) < 2:
                comment = ""   # separators, bare numbers: not evidence
            # glossary-style comments anywhere ("X = meaning", "X - meaning")
            if comment:
                for nm in entries:
                    if re.search(rf"(?<![\w@]){re.escape(nm)}(?!\w)", comment):
                        gloss = re.search(rf"(?<![\w@]){re.escape(nm)}\s*(?:=|:|-)\s*[A-Za-z]", comment)
                        entries[nm].add("comment", rel, ln, _snip(comment, nm), cap=2, first=bool(gloss))
            for nm in present:
                e = entries[nm]
                if comment:
                    e.add("comment", rel, ln, _snip(comment, nm), cap=2)
                for s in strings:
                    if _natural(s) and any(canonical(t) == nm and t.lstrip("@") != nm for t in raw_toks):
                        e.add("label", rel, ln, _snip(s), cap=2)
                pat = rf"(?<![\w@])@?{re.escape(nm)}\b"
                stripped = raw.strip()
                # definitions
                is_def = table_col = False
                if lang == "sql":
                    table_col = in_table and bool(re.match(rf"^\s*\[?{re.escape(nm)}\]?\s+(?:{SQL_TYPE})\b", code, re.I))
                    if table_col or re.search(rf"@{re.escape(nm)}\s+(?:{SQL_TYPE})\b", code, re.I):
                        is_def = True
                        e.is_column = True
                elif lang == "vb":
                    if re.search(rf"\b(?:Dim|ByVal|ByRef|Private|Public|Friend|Protected|Const|Property)\s+"
                                 rf"(?:Shared\s+|ReadOnly\s+|WithEvents\s+)*[a-z_]{{0,4}}{re.escape(nm)}\b", code) \
                            or re.search(rf"Columns\.Add\(\s*{re.escape(nm)}\b", code):
                        is_def = True
                        e.is_column = e.is_column or "ByVal" in code or "Columns" in code
                elif lang == "cs":
                    if re.search(rf"\b(?:const\s+)?[A-Za-z_<>\[\]?]+\s+{re.escape(nm)}\s*(?:=|;|,|\))", code):
                        is_def = True
                if is_def:
                    # a table column definition (type, default) beats a parameter declaration
                    e.add("def", rel, ln, _snip(stripped, nm), routine, cap=2, first=table_col)
                    prev = rows[ln - 2] if ln >= 2 else None
                    if prev and len(re.findall(r"[A-Za-z]{2,}", prev[1])) >= 2 and not prev[0].strip():
                        # full-line comment just above the definition
                        e.add("comment", rel, ln - 1, _snip(prev[1]), cap=2)
                    continue
                if re.search(rf"\bCHECK\b.*{pat}", code, re.I):
                    e.add("def", rel, ln, _snip(stripped, nm), routine, cap=2)
                    continue
                # set vs tested
                m_set = re.search(rf"(?:\bSET\b[^=]*?|,\s*|^\s*(?:Me\.)?(?:\w+\.)*\w*\(?\"?){pat}\"?\)?\s*=\s*(?!=)", code, re.I)
                is_set = bool(m_set) and not re.search(rf"\b(?:WHERE|AND|OR|IF|WHEN|ON|ElseIf|While|AndAlso|OrElse|Case)\b[^=]*{pat}", code, re.I)
                if lang == "sql" and re.search(rf"\bSET\b.*{pat}\s*=", code, re.I):
                    is_set = True
                kind = "set" if is_set else ("tested" if re.search(
                    rf"{pat}\s*(?:<>|>=|<=|=|<|>|\bIN\b|\bIS\b)|(?:<>|>=|<=|=|<|>)\s*@?(?:\w+\.)?{re.escape(nm)}\b",
                    code, re.I) else "")
                if kind:
                    e.add(kind, rel, ln, _snip(stripped, nm), routine, cap=2)
    for nm in list(entries):
        e = entries[nm]
        e.is_column = e.is_column or bool(e.ev.get("def"))
        if not bare.get(nm) and not (e.ev.get("def") or e.ev.get("comment") or e.ev.get("label")):
            del entries[nm]   # only ever `.Member` access with no definition: a framework member
    return entries


def collect(source_root: str | Path, rel_paths: list[str]) -> dict[str, _Entry]:
    files = {}
    for rel in rel_paths:
        t = _read_text(Path(source_root) / rel)
        if t is not None:
            files[rel] = t
    return scan(files)


# ── supported vs unsupported expansions (also used by tests) ─────────────────────────────────
_STOP = {"the", "and", "for", "with", "from", "that", "this", "has", "was", "are", "not", "into",
         "flag", "value", "field", "column", "indicator"}


def evidence_text(entry: dict) -> str:
    parts = []
    for kind, items in entry.get("evidence", {}).items():
        for it in items:
            parts.append(it["text"])
            if it.get("in"):
                parts.append(" ".join(segments(it["in"])))
    return " ".join(parts).lower()


def expansion_supported(entry: dict, expansion: str) -> bool:
    """True when at least half of the expansion's content words occur (4-letter prefix match) in
    the identifier's evidence text (comments, labels, definitions, set/test lines, routine names).
    A deterministic check of whether a stated meaning has any support in the code."""
    words = [w for w in re.findall(r"[a-z]{3,}", expansion.lower()) if w not in _STOP]
    if not words:
        return False
    ev = set(re.findall(r"[a-z]{3,}", evidence_text(entry)))
    prefixes = {w[:4] for w in ev}
    hit = sum(1 for w in words if w in ev or w[:4] in prefixes)
    return hit * 2 >= len(words)


# ── rendering ───────────────────────────────────────────────────────────────────────────────
def short_names(rel_paths) -> dict[str, str]:
    """rel path -> basename when the basename is unique in the corpus (keeps citations short)."""
    count: dict[str, int] = defaultdict(int)
    for r in rel_paths:
        count[Path(r).name] += 1
    return {r: (Path(r).name if count[Path(r).name] == 1 else r) for r in rel_paths}


def _cite(it: dict, short: dict | None = None) -> str:
    f = (short or {}).get(it["file"], it["file"])
    return f"{f}:L{it['line']}"


def render_entry(e: dict, short: dict | None = None) -> str:
    """One compact line. [stated]: the stating comment/label + definition. [usage]: definition +
    one set + one test (with the enclosing routine). [none]: the definition."""
    ev = e["evidence"]
    if e["status"] == "stated":
        kinds = ("label", "comment", "def")
    elif e["status"] == "usage":
        kinds = ("def", "set", "tested")
    else:
        kinds = ("def",)
    bits = []
    for kind in kinds:
        for it in ev.get(kind, [])[:1]:
            where = f" in {it['in']}" if it.get("in") and kind in ("set", "tested") else ""
            q = '"' if kind in ("comment", "label") else "`"
            bits.append(f"{kind}{where} {q}{it['text']}{q} ({_cite(it, short)})")
    return f"- {e['name']} [{e['status']}] " + ("; ".join(bits) if bits else "no evidence found")


def page_units(page_dir: Path) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    p = page_dir / "units.jsonl"
    if not p.is_file():
        return {}
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            r = json.loads(ln)
            if r.get("status") == "emitted":
                out[r["path"]].append((int(r["line_start"]), int(r["line_end"])))
    return dict(out)


def render_page_block(glossary: dict[str, dict], occ: dict[str, dict[str, list[int]]],
                      units: dict[str, list[tuple[int, int]]], max_chars: int = DEFAULT_MAX_CHARS,
                      max_entries: int = DEFAULT_MAX_ENTRIES, short: dict | None = None) -> str:
    """Bounded block for one page: abbreviated names occurring on the page, ranked by (column or
    parameter, abbreviation strength, occurrences on the page; ALL_CAPS constants last, the facts
    block already gives their values). '' when none apply."""
    if not units or max_chars <= len(HEADER) + 60:
        return ""
    cand = []
    for name, e in glossary.items():
        n = 0
        for f, lines in occ.get(name, {}).items():
            for a, b in units.get(f, []):
                n += sum(1 for x in lines if a <= x <= b)
        if n:
            rank = (2 if e["column_or_parameter"] else 0) + e["abbreviation_score"] + min(n, 6) / 2
            if name.upper() == name:
                rank -= 3
            cand.append((-rank, name))
    if not cand:
        return ""
    cand.sort()
    out, used, shown, dropped = [HEADER], len(HEADER), 0, 0
    for _r, name in cand:
        line = "\n" + render_entry(glossary[name], short)
        if shown >= max_entries or used + len(line) + 80 > max_chars:
            dropped += 1
            continue
        out.append(line)
        used += len(line)
        shown += 1
    if not shown:
        return ""
    if dropped:
        out.append(f"\n- ({dropped} more abbreviated names on this page omitted for length)")
    return "".join(out) + "\n"


# ── stage driver ────────────────────────────────────────────────────────────────────────────
def blocks_hash(out: Path) -> str | None:
    st = out / "identifiers_status.json"
    if not st.is_file() or json.loads(st.read_text()).get("status") != "ok":
        return None
    h = hashlib.sha256()
    for p in sorted((out / "pages").glob("page_*.md")):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return "sha256:" + h.hexdigest()


def build(source_root: Path, manifest: Path, pages_dir: Path, out: Path,
          max_chars: int = DEFAULT_MAX_CHARS, max_entries: int = DEFAULT_MAX_ENTRIES) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    for stale in ("pages", "identifiers.json"):
        p = out / stale
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    rels = [r.strip() for r in manifest.read_text(encoding="utf-8").splitlines() if r.strip()]
    entries = collect(source_root, rels)
    glossary = {n: e.to_json() for n, e in sorted(entries.items())}
    occ = {n: {f: sorted(v) for f, v in e.occ.items()} for n, e in entries.items()}
    short = short_names(rels)
    (out / "identifiers.json").write_text(json.dumps(glossary, indent=1), encoding="utf-8")
    (out / "pages").mkdir()
    pages = {}
    for pd in sorted(p for p in pages_dir.iterdir() if p.is_dir() and p.name.startswith("page_")):
        block = render_page_block(glossary, occ, page_units(pd), max_chars, max_entries, short)
        pages[pd.name] = {"chars": len(block)}
        if block:
            (out / "pages" / f"{pd.name}.md").write_text(block, encoding="utf-8")
    by_status = defaultdict(int)
    for e in glossary.values():
        by_status[e["status"]] += 1
    status = {"status": "ok", "max_chars": max_chars, "max_entries": max_entries,
              "counts": {"identifiers": len(glossary), **by_status}, "pages": pages}
    (out / "identifiers_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    status["blocks_hash"] = blocks_hash(out)
    (out / "identifiers_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 1.3: identifier evidence (abbreviation glossary)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--source-root", required=True, type=Path)
    b.add_argument("--manifest", required=True, type=Path)
    b.add_argument("--pages", required=True, type=Path)
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    b.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    h = sub.add_parser("hash")
    h.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    if a.cmd == "hash":
        print(blocks_hash(a.out) or "none")
        return 0
    if a.max_chars <= 0 or a.max_entries <= 0:
        print("identifier_evidence: --max-chars and --max-entries must be > 0", file=sys.stderr)
        return 2
    st = build(a.source_root, a.manifest, a.pages, a.out, a.max_chars, a.max_entries)
    sizes = [p["chars"] for p in st["pages"].values()]
    c = st["counts"]
    print(f"  [identifiers] {c['identifiers']} abbreviated names ({c.get('stated', 0)} stated, "
          f"{c.get('usage', 0)} usage-only, {c.get('none', 0)} no evidence); page blocks "
          f"{sum(1 for s in sizes if s)}/{len(sizes)} (max {max(sizes, default=0)} chars, bound "
          f"{st['max_chars']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

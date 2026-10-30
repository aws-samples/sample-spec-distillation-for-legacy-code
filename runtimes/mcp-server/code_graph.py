#!/usr/bin/env python3
"""
code_graph.py — LLM-based code dependency graph pass.

Builds a FACTUAL code-dependency layer that runs parallel to (and independent of)
the still map/reduce pipeline. It reconstructs source files from the combined
pages, uses a small, fast Bedrock model (Claude Haiku) to extract dependency structure + light business
bridge metadata, VALIDATES every LLM anchor deterministically against the source
(LLM proposes, regex disposes), and persists nodes/edges to the shared SQLite DB.

Design (validated in experiments/dep-graph-poc):
  - Structural (high certainty): imports/usings, references, defines.
  - Qualifiers: read/write direction, resolution (static/dynamic_sql), confidence.
  - Node metadata: architectural role, target-system module (allowlisted), business_intent.
  - Business bridge: cryptic-name -> business-entity decoding (join key to stills).
  - Validation: every emitted code_name/reference/using must appear in the source
    (case-insensitive); dynamic-SQL refs are EXEMPT (assembled at runtime) and kept
    at lower confidence. Module tokens are allowlist-classified; non-allowlist
    tokens are preserved as `unverified_tag` (never discarded).
  - Provenance: model id + timestamp + page stamped on every file.
  - Deterministic parser preferred: where <run>/facts/facts.json exists (Roslyn VB.NET + C#,
    ScriptDom T-SQL — runtimes/facts), a model-proposed relation the parser also has is confirmed
    outright and the parser's operation/direction win; a relation the parser found and the model
    missed is BACKFILLED, tagged `facts:*` in code_edges.symbol so the model-proposed and
    parser-backed populations stay separable. See FactsIndex.

Relations extracted per language (all four rigs now emit the SAME dependency relations, so a
whole-language blind spot cannot hide behind an "it is a different shape" story):
    sql     table reads/writes, proc/function EXEC, trigger attachment, defines
    vbnet   method->method calls, proc EXEC (SqlCommand/CommandText/proc literal), table
            reads/writes (inline SQL and via a DAL), trigger/report references, Imports
    csharp  the same as vbnet, in C# spelling
    cobol   CALL/COPY/file access

Cost (Haiku 4.5): output-dominated (output priced ~5x input),
so cost scales with enrichment volume, not source size — a small fraction of one Sonnet
map run on the same corpus.

Tables written into the shared vector DB:
  source_files, code_objects, code_edges, code_entities

CLI:
    python code_graph.py <pages_dir> <shared_vectors_db> [--limit N] [--concurrency N]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import bedrock_client

REGION_DEFAULT = "us-east-1"
DEFAULT_MODEL = os.environ.get("DISTILL_CODE_GRAPH_MODEL", "anthropic.claude-haiku-4-5")
PRICE_IN, PRICE_OUT = 1.0, 5.0  # $/M tokens (Haiku 4.5 US)
# Fail the pass (exit 1) when more than this share of files errored (LLM call or JSON parse).
MAX_ERROR_RATE = float(os.environ.get("DISTILL_CODE_GRAPH_MAX_ERROR_RATE", "0.2"))

CODE_EXTS = {"sql": "sql", "cs": "csharp", "vb": "vbnet",
             "cob": "cobol", "cbl": "cobol", "cpy": "cobol"}  # reasonable SEED, not a finish line;
# untapped languages are surfaced by preflight + a path-to-cover reasoned by the rectifier
# (raw-extractable → new _LANG_PROMPTS template; needs intermediary → ingest-helper spec).
# See spec L-coverage-doctrine + code-graph-arbitration X-cga-B2/B3.
FILE_MARKER = re.compile(r"^\[FILE:\s*(.+?)\]\s*$")
END_MARKER = re.compile(r"^\[END FILE:")
LINE_PREFIX = re.compile(r"^\s*\d+\s\|\s?")

# The [FILE:] marker carries the PATH followed by optional space-delimited attributes:
#   [FILE: <path>]                                            (raw, pre-materialization)
#   [FILE: <path> KIND: raw|inferred|referential SIZE: N VIA: <method>]   (materialized)
#   [FILE: <path> [PART k/n] ORIGINAL_LINE_START: N]          (split oversized file)
#   [FILE: <path> [PART k/n] KIND: raw ORIGINAL_LINE_START: N LINE_SEGMENT: s/S]  (hard-split long line)
#   [FILE: <path> KIND: unreadable|binary REASON: <text>]     (marker-only: nothing paged, see skipped.tsv)
# The path MAY contain spaces (e.g. "My Report.rpt"), so we cannot split on space. The path is
# everything up to the FIRST attribute keyword. parse_file_marker() is the single canonical parser
# every consumer uses — the marker format is defined ONCE here.
_MARKER_ATTR = re.compile(r"\s+(?:KIND:|SIZE:|VIA:|\[PART\b|ORIGINAL_LINE_START:|LINE_SEGMENT:|REASON:)")


def parse_file_marker(inner: str) -> tuple[str, dict]:
    """Given the text INSIDE a [FILE: … ] marker (the FILE_MARKER group), return (path, attrs).
    attrs may include kind/size/via/part/original_line_start. Path is everything before the first
    attribute keyword (paths may contain spaces)."""
    m = _MARKER_ATTR.search(inner)
    path = (inner[:m.start()] if m else inner).strip()
    attrs = {}
    if m:
        rest = inner[m.start():]
        for key, rx in (("kind", r"KIND:\s*(\S+)"), ("size", r"SIZE:\s*(\S+)"),
                        ("via", r"VIA:\s*(\S+)"), ("original_line_start", r"ORIGINAL_LINE_START:\s*(\d+)")):
            mm = re.search(rx, rest)
            if mm:
                attrs[key] = mm.group(1)
        pm = re.search(r"\[PART\s+(\d+)/(\d+)\]", rest)
        if pm:
            attrs["part"] = (int(pm.group(1)), int(pm.group(2)))
        sm = re.search(r"LINE_SEGMENT:\s*(\d+)/(\d+)", rest)
        if sm:
            attrs["line_segment"] = (int(sm.group(1)), int(sm.group(2)))
        rm = re.search(r"REASON:\s*(.*)$", rest)
        if rm:
            attrs["reason"] = rm.group(1).strip()
    return path, attrs


def marker_path(inner: str) -> str:
    """Just the path from a marker's inner text (attributes stripped)."""
    return parse_file_marker(inner)[0]

# Target-system module codes — OPERATOR-CONFIGURED per target, not committed.
# Set env DISTILL_MODULE_ALLOWLIST to a comma-separated list of the source system's
# module codes (e.g. its short 2-4 char business-area prefixes). Default empty: with
# no allowlist, every inferred module lands in `unverified_tag` (kept, never dropped),
# and frequency across the corpus is the signal for which tags are real modules.
# Do NOT hard-code a specific customer's module set here.
MODULE_ALLOWLIST = {
    m.strip().upper() for m in os.environ.get("DISTILL_MODULE_ALLOWLIST", "").split(",")
    if m.strip()
}

SQL_STOP = {"dbo", "sys", "information_schema", "sp_executesql", "guest", "objects",
            "columns", "procedures", "parameters", "sql_modules", "tables", "views",
            "tempdb", "master", "msdb", "model"}
CS_STOP = {"system", "microsoft"}

SQL_SYSTEM = ("You are a precise T-SQL analyst that also decodes abbreviated schema identifiers. "
              "Output ONLY one JSON object, no prose, no fences.")
SQL_USER = """Analyze this T-SQL file: {path}

Emit JSON:
"file_role": "stored_procedure"|"view"|"function"|"trigger"|"table"|"migration"|"script"
"module": the source system's short business-area/module code (typically 2-4
    uppercase letters) if clearly inferable from names/prefixes/comments, else null
"business_intent": ONE short sentence: the business action this performs
"defines": [{{"name":"schema.obj","kind":"...","columns":[{{"name":"...","type":"...","key":"pk"|"fk"|null}}]}}]
"references": [{{"name":"schema.obj","kind":"table"|"view"|"procedure"|"function"|"unknown",
    "operation":"select"|"insert"|"update"|"delete"|"exec"|"join"|"unknown",
    "direction":"read"|"write"|"call","resolution":"static"|"dynamic_sql",
    "confidence":<0.0-1.0>}}]
"business_entities": [{{"code_name":"XXXX","business_name":"<the decoded business name>","confidence":<0.0-1.0>}}]

Rules: one entry per DISTINCT referenced object; EXCLUDE CTEs/#temp/@vars/aliases
and the object this file defines. Refs built in EXEC(@sql)/sp_executesql -> resolution="dynamic_sql".

Source:
```sql
{source}
```
Output ONLY the JSON object."""

CS_SYSTEM = ("You are a precise C# analyst. You trace, for every file, who it CALLS, which stored "
             "procedures it EXECUTES and which database tables it READS and WRITES — including SQL "
             "built in string literals and data access delegated to a repository. You also decode "
             "abbreviated schema identifiers. Output ONLY one JSON object, no prose, no fences.")
CS_USER = """Analyze this C# file: {path}

Emit JSON:
"file_role": "controller"|"service"|"repository"|"entity"|"dto"|"test"|"config"|"utility"|"other"
"module": the source system's short business-area/module code if clearly inferable, else null
"business_intent": ONE short sentence
"usings": [namespace strings]
"declarations": [{{"name":"Type","kind":"class"|"interface"|"struct"|"enum"|"record",
    "base":"<base or null>","implements":[interfaces]}}]
"calls": [{{"caller":"<enclosing method name or null>",
    "callee":"<method name, qualified Type.Method when the receiver's type is known>",
    "receiver":"<the receiver expression as written, or null>","confidence":<0.0-1.0>}}]
"data_refs": [{{"name":"<stored procedure / table / view / trigger / report, VERBATIM from the source>",
    "kind":"stored_procedure"|"function"|"table"|"view"|"trigger"|"report",
    "operation":"exec"|"select"|"insert"|"update"|"delete"|"merge"|"join"|"unknown",
    "direction":"read"|"write"|"call",
    "via":"inline_sql"|"sqlcommand"|"dal"|"adapter"|"proc_literal"|"orm"|"report_call"|"other",
    "resolution":"static"|"dynamic_sql","confidence":<0.0-1.0>}}]
"references": [{{"symbol":"Type/member","kind":"type"|"call"|"property_get"|"property_set"|"instantiate"|"inherit"|"implement",
    "resolution":"local"|"remote"|"builtin","gateway":"<using-namespace or null>","confidence":<0.0-1.0>}}]
"business_entities": [{{"code_name":"...","business_name":"...","confidence":<0.0-1.0>}}]

How to find each relation:
- calls: every invocation of a method/property defined in THIS system — `Foo(...)`, `this.Foo(...)`,
  `obj.Foo(...)`, `Helper.Foo(...)`, a method group passed as a delegate. Qualify with the declaring
  type when the receiver's declared type is visible. SKIP framework members (string/List/Convert/...).
- stored procedures: `CommandType.StoredProcedure`, `CommandText = "usp_X"`, a proc name passed to a
  helper, `EXEC usp_X` inside a SQL string, Dapper/EF `ExecuteProcedure("usp_X")`.
  kind "stored_procedure", operation "exec", direction "call".
- tables: every table or view named in SQL text in this file (literal, concatenated, interpolated,
  verbatim or raw string), plus an ORM entity set whose mapped table name is stated in this file.
  operation = the SQL verb; direction read for select/join, write for insert/update/delete/merge.
- tables via a repository: when the called data-access method's own name names the table, emit it
  with via "dal" and confidence <= 0.6. Never invent a table that is nowhere named in this file.
- triggers / reports named in this file -> kind "trigger" / "report".
- SQL assembled at runtime -> resolution "dynamic_sql"; never guess the assembled part.

TWO THINGS NOT TO DO (both are checked, and both cost precision):
- A name that is a method of THIS system is a CALL, not a stored procedure: `_repo.AddHold`,
  `GetOpenHolds` belong in "calls". Use kind "stored_procedure" only where the source EXECUTES one
  (a `usp_`/`sp_`-shaped name in CommandText / CommandType.StoredProcedure / EXEC).
- A table or procedure named only in a COMMENT is not a dependency of this file. Emit a data_ref
  only when the name appears in CODE. (A checker strips comments and re-tests every name you give.)

Rules: one entry per DISTINCT relation; every `name` in data_refs and every `callee` in calls MUST
appear verbatim in the source below (a deterministic checker verifies this); gateway MUST be one of
the usings or null.

Source:
```csharp
{source}
```
Output ONLY the JSON object."""

VB_SYSTEM = ("You are a precise VB.NET analyst. You trace, for every file, who it CALLS, which "
             "stored procedures it EXECUTES and which database tables it READS and WRITES — "
             "including SQL built in string literals and data access delegated to a DAL class. "
             "You also decode abbreviated schema identifiers. "
             "Output ONLY one JSON object, no prose, no fences.")
VB_USER = """Analyze this VB.NET file: {path}

Emit JSON:
"file_role": "controller"|"service"|"repository"|"entity"|"dto"|"test"|"config"|"utility"|"form"|"other"
"module": the source system's short business-area/module code if clearly inferable, else null
"business_intent": ONE short sentence
"usings": [Imports namespace strings]   (VB `Imports X` — the namespace, not the alias)
"declarations": [{{"name":"Type","kind":"class"|"module"|"interface"|"structure"|"enum",
    "base":"<Inherits base or null>","implements":[Implements interfaces]}}]
"calls": [{{"caller":"<enclosing Sub/Function/Property name or null>",
    "callee":"<method name, qualified Class.Method when the receiver's type is known>",
    "receiver":"<the receiver expression as written, or null for an unqualified call>",
    "confidence":<0.0-1.0>}}]
"data_refs": [{{"name":"<stored procedure / table / view / trigger / report, VERBATIM from the source>",
    "kind":"stored_procedure"|"function"|"table"|"view"|"trigger"|"report",
    "operation":"exec"|"select"|"insert"|"update"|"delete"|"merge"|"join"|"unknown",
    "direction":"read"|"write"|"call",
    "via":"inline_sql"|"sqlcommand"|"dal"|"adapter"|"proc_literal"|"report_call"|"other",
    "resolution":"static"|"dynamic_sql","confidence":<0.0-1.0>}}]
"references": [{{"symbol":"Type/member","kind":"type"|"call"|"property_get"|"property_set"|"instantiate"|"inherit"|"implement",
    "resolution":"local"|"remote"|"builtin","gateway":"<Imports-namespace or null>","confidence":<0.0-1.0>}}]
"business_entities": [{{"code_name":"XXXX","business_name":"<the decoded business name>","confidence":<0.0-1.0>}}]

How to find each relation (this is the part that matters):
- calls: every invocation of a Sub/Function/Property defined in THIS system — `Foo(...)`,
  `Me.Foo(...)`, `obj.Foo(...)`, `modXxx.Foo(...)`, `Call Foo`, `AddressOf Foo`. Qualify the callee
  with the declaring class when the receiver's declared type is visible (`Dim o As clsX` /
  `Private m_x As clsX` / a `New clsX`), else give the bare method name. SKIP framework/runtime
  members (String/DataTable/Convert/MessageBox/Trim/CDec...). One entry per DISTINCT caller->callee.
- stored procedures: a `SqlCommand`/`OleDbCommand` whose `CommandType` is `StoredProcedure`, a
  `.CommandText = "usp_Something"`, a proc name passed to a helper
  (`ExecProc("usp_X", ...)`, `RunProc("dbo.usp_X")`), or an `EXEC usp_X` inside a SQL string.
  kind "stored_procedure", operation "exec", direction "call".
- tables: every table or view named in SQL text in this file — in a literal, in a concatenated or
  interpolated SQL string, in a `SELECT ... FROM`, `INSERT INTO`, `UPDATE`, `DELETE FROM`, `MERGE`,
  or a JOIN. Give the table name only (drop the `dbo.` schema if you like, but keep the spelling).
  operation = the SQL verb; direction read for select/join, write for insert/update/delete/merge.
- tables via a DAL: when this file calls a data-access method whose own name or nearby comment
  names the table (`m_DAL.GetAPInvHdr`, `DAL.SaveInvoiceHeader`), emit the table with via "dal" and
  a confidence <= 0.6. Do NOT invent a table that is nowhere named in this file.
- triggers / reports: a trigger or report (`.rpt`, Crystal `ReportDocument`, a report number) named
  in this file, kind "trigger" / "report".
- SQL assembled at runtime (`sql &= " WHERE " & col`) -> resolution "dynamic_sql" for the refs you
  can still see; never guess the part that is assembled from variables.

TWO THINGS NOT TO DO (both are checked, and both cost precision):
- A name that is a Sub/Function of THIS system is a CALL, not a stored procedure. `m_DAL.AddHold`,
  `GetOpenHolds`, `ApproveInvoice` belong in "calls". Only put a name in data_refs with kind
  "stored_procedure" when the source shows it being EXECUTED as one — a proc-shaped name
  (`usp_`/`sp_` prefix) in a CommandText / CommandType.StoredProcedure / EXEC position.
- A table or procedure named only in a COMMENT is not a dependency of this file. Comments like
  `' --- invoice status codes (APInvHdr.Status) ---` or `' calls usp_APInvPost` describe something;
  they do not access it. Emit a data_ref only when the name appears in CODE — in SQL text, in a
  command, or in a call. (A checker strips comments and re-tests every name you give.)

VB notes: `Class`/`Module`/`Interface`/`Structure`/`Enum` are declarations; `Inherits` = base,
`Implements` = interfaces; `New X()` = instantiate; a `Property` getter/setter = property_get/set;
a WinForms code-behind (Inherits System.Windows.Forms.Form / has Handles clauses) -> file_role "form".
Rules: one entry per DISTINCT relation; every `name` in data_refs and every `callee` in calls MUST
appear verbatim in the source below (a deterministic checker verifies this and lowers the confidence
of anything it cannot find); gateway MUST be one of the Imports or null.

Source:
```vbnet
{source}
```
Output ONLY the JSON object."""

COBOL_SYSTEM = ("You are a precise COBOL analyst that also decodes abbreviated data names. "
                "Output ONLY one JSON object, no prose, no fences.")
COBOL_USER = """Analyze this COBOL source file: {path}

Emit JSON:
"file_role": "program"|"subprogram"|"copybook"|"batch"|"online"|"other"
"module": the source system's short business-area/module code if clearly inferable, else null
"business_intent": ONE short sentence: the business action this performs
"defines": [{{"name":"PROGRAM-ID or 01-level record/paragraph name","kind":"program"|"record"|"file"|"paragraph"|"section"}}]
"references": [{{"name":"called program / COPY member / file / record","kind":"program"|"copybook"|"file"|"record"|"unknown",
    "operation":"call"|"copy"|"read"|"write"|"rewrite"|"open"|"perform"|"unknown",
    "direction":"read"|"write"|"call","confidence":<0.0-1.0>}}]
"business_entities": [{{"code_name":"WS-OR-FD-NAME","business_name":"<the decoded business name>","confidence":<0.0-1.0>}}]

Rules: one entry per DISTINCT external program (CALL 'X'), COPY member, or file; EXCLUDE the
program this file defines. Names must appear verbatim in the source.

Source:
```cobol
{source}
```
Output ONLY the JSON object."""

# lang → (system, user) prompt. Default (unknown lang) uses the C# template (closest general shape).
_LANG_PROMPTS = {"sql": (SQL_SYSTEM, SQL_USER), "csharp": (CS_SYSTEM, CS_USER),
                 "vbnet": (VB_SYSTEM, VB_USER), "cobol": (COBOL_SYSTEM, COBOL_USER)}


# ── SQLite schema ────────────────────────────────────────────────────────────

def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_files (
            path TEXT PRIMARY KEY, lang TEXT, role TEXT, role_confirmed INTEGER,
            module TEXT, unverified_tag TEXT, module_validated INTEGER,
            business_intent TEXT, page TEXT, model TEXT, extracted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS code_objects (
            path TEXT, object TEXT, kind TEXT, PRIMARY KEY (path, object)
        );
        CREATE TABLE IF NOT EXISTS code_edges (
            src TEXT, dst TEXT, type TEXT, lang TEXT, kind TEXT, operation TEXT,
            direction TEXT, resolution TEXT, symbol TEXT,
            validated INTEGER, dynamic INTEGER, confidence REAL,
            verdict TEXT, direction_verdict TEXT, derived_confidence REAL
        );
        CREATE TABLE IF NOT EXISTS code_entities (
            path TEXT, code_name TEXT, business_name TEXT, confidence REAL,
            anchor_verdict TEXT, stills_corroborated INTEGER,
            PRIMARY KEY (path, code_name)
        );
        CREATE INDEX IF NOT EXISTS idx_edges_src ON code_edges(src);
        CREATE INDEX IF NOT EXISTS idx_edges_dst ON code_edges(dst);
        CREATE INDEX IF NOT EXISTS idx_files_module ON source_files(module);
        CREATE INDEX IF NOT EXISTS idx_entities_code ON code_entities(code_name);
        -- reserves-and-recovery ledger (L4): the survey (census) vs. what a rig tapped, per
        -- extension + its extraction modality. alarm=1 marks a signal-bearing reserve with a
        -- rig-gap (0 recovery) — booked, not written off. Queryable ground truth for the guard.
        CREATE TABLE IF NOT EXISTS source_coverage (
            ext TEXT PRIMARY KEY, modality TEXT, census INTEGER, processed INTEGER,
            coverage_pct REAL, in_allowlist INTEGER, alarm INTEGER, computed_at TEXT
        );
        """
    )
    conn.commit()


# ── Page reconstruction (fully-contained code files) ─────────────────────────

def reconstruct_code_files(pages_dir: Path):
    """Yield (path, clean_source, page_name) for fully-contained code files.

    A segment is complete when a [FILE:] marker is followed by another marker in
    the same concat.txt, or (for the page's last segment) when it is closed by an
    [END FILE:] marker; an unterminated tail is skipped as possibly truncated.
    Line-number prefixes are stripped.
    """
    for page in sorted(pages_dir.glob("page_*")):
        concat = page / "concat.txt"
        if not concat.exists():
            continue
        lines = concat.read_text(encoding="utf-8", errors="replace").splitlines()
        markers = [(i, parse_file_marker(m.group(1))) for i, ln in enumerate(lines)
                   if (m := FILE_MARKER.match(ln))]
        if not markers:
            continue
        # The page's last segment is complete when it is closed by an [END FILE:] marker (what
        # concat-batch.sh emits); only an unterminated tail is treated as possibly truncated.
        last_closed = any(END_MARKER.match(ln) for ln in lines[markers[-1][0] + 1:])
        bounds = [m[0] for m in markers[1:]] + ([len(lines)] if last_closed else [])
        for k, end_idx in enumerate(bounds):
            start_idx, (path, attrs) = markers[k]
            # Only RAW source is reconstructable to source lines; inferred/referential are not code.
            if attrs.get("kind") in ("inferred", "referential", "unreadable", "binary"):
                continue
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext not in CODE_EXTS:
                continue
            body = lines[start_idx + 1: end_idx]
            clean = "\n".join(LINE_PREFIX.sub("", ln) for ln in body
                              if not END_MARKER.match(ln))
            yield path, clean, page.name


# ── Source census (the denominator — the coverage-conservation guard) ─────────
# Ground-truth integrity: the dependency pass must ACCOUNT FOR EVERY file it saw as source,
# not silently narrow the corpus to the two languages it happens to have extractors for. The
# census enumerates EVERY [FILE:] marker (no allowlist filter) so a downstream guard can prove
# processed ∪ excluded == census, per-language, and surface any code-bearing language with 0%
# coverage as a loud finding rather than a silent hole. Extensions are LEARNED from the tree
# (source-independent), never hardcoded. (SYSTEM_SPEC coverage-conservation law.)

# MODALITY classification (SYSTEM_SPEC L4 — no permanent inert set). Every reservoir carries
# signal; the only question is which extraction MODALITY (rig) can tap it. We classify a census
# extension into a modality so recovery can be reported per-modality and any reservoir with a
# rig-gap (0% recovery, no extractor for that modality) is BOOKED and surfaced — never written
# off as "benign". There is deliberately NO inert set: an unknown extension defaults to a
# signal-bearing modality (text/binary by a cheap content sniff), so a new type is flagged, never
# silently ignored. Modalities are named capabilities, not a hardcoded worth-judgment.
#   text-imperative   — call/dependency source (rig: LLM code extractor; have sql/cs)
#   declarative-topology — build/project graphs (.sln/.csproj/… — rig: NOT built)
#   schema            — data models (.xsd/.wsdl/.edmx/… — rig: NOT built)
#   config-contract   — wiring/DTO/rules (.xml/.json/.yaml/.config — rig: NOT built)
#   vocabulary        — business terms/labels (.resx/.resw — rig: NOT built)
#   report            — report defs/layout (.rpt/.rdl/.dtsx — rig: NOT built)
#   image             — diagrams/screens (.png/.svg/… — rig: vision, NOT built)
#   binary            — opaque blobs (.dll/.zip/… — reserve pending a rig; not written off)
_MODALITY_BY_EXT = {
    # text-imperative (the only modality with a rig today)
    "sql": "text-imperative", "cs": "text-imperative", "vb": "text-imperative",
    "js": "text-imperative", "ts": "text-imperative", "py": "text-imperative",
    "java": "text-imperative", "cpp": "text-imperative", "c": "text-imperative",
    "h": "text-imperative", "vbs": "text-imperative", "ps1": "text-imperative",
    "rb": "text-imperative", "go": "text-imperative", "php": "text-imperative",
    "cob": "text-imperative", "cbl": "text-imperative", "cpy": "text-imperative",
    # declarative-topology
    "sln": "declarative-topology", "csproj": "declarative-topology",
    "vbproj": "declarative-topology", "vcxproj": "declarative-topology",
    "proj": "declarative-topology", "props": "declarative-topology",
    # schema
    "xsd": "schema", "wsdl": "schema", "edmx": "schema", "dbml": "schema", "avsc": "schema",
    # config-contract
    "xml": "config-contract", "json": "config-contract", "yaml": "config-contract",
    "yml": "config-contract", "config": "config-contract", "toml": "config-contract",
    # vocabulary
    "resx": "vocabulary", "resw": "vocabulary",
    # report
    "rpt": "report", "rdl": "report", "rdlc": "report", "dtsx": "report",
    # markup/ui (text, but not imperative — its own rig)
    "html": "markup", "cshtml": "markup", "aspx": "markup", "ascx": "markup",
    "xaml": "markup", "css": "markup",
    # image
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "bmp": "image",
    "svg": "image", "ico": "image", "tif": "image", "tiff": "image",
    # binary (opaque — a reserve pending a rig, NOT written off)
    "dll": "binary", "exe": "binary", "pdb": "binary", "zip": "binary", "gz": "binary",
    "tar": "binary", "7z": "binary", "rar": "binary", "woff": "binary", "woff2": "binary",
    "ttf": "binary", "eot": "binary",
}

# Which modalities have a working extractor (rig) in THIS build. The guard alarms on any modality
# that has reserves (census > 0) but no rig — the untapped-reserve = declared coverage gap.
_MODALITIES_WITH_RIG = {"text-imperative"}  # and only for the exts in CODE_EXTS (see below)


def _modality_of(ext: str) -> str:
    """Classify a census extension into an extraction modality. Unknown extensions default to a
    signal-bearing modality (never 'inert'): a short/uncommon token → 'text-imperative' candidate,
    else 'binary' — either way BOOKED, never silently dropped."""
    if not ext:
        return "unknown"
    return _MODALITY_BY_EXT.get(ext, "unknown")


def census_source_files(pages_dir: Path):
    """Yield (path, ext, page_name) for EVERY reconstructed [FILE:] segment — the full source
    denominator, with NO language filter. This is what every coverage claim measures against.
    Every marker counts (including a page's LAST one, which the old `markers[:-1]` loop dropped —
    the "3/2 files" bug) and a file split into [PART k/n] segments counts once."""
    seen: set[str] = set()
    for page in sorted(pages_dir.glob("page_*")):
        concat = page / "concat.txt"
        if not concat.exists():
            continue
        lines = concat.read_text(encoding="utf-8", errors="replace").splitlines()
        for ln in lines:
            m = FILE_MARKER.match(ln)
            if not m:
                continue
            path = marker_path(m.group(1))
            if path in seen:
                continue
            seen.add(path)
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            yield path, ext, page.name


def ext_label(ext: str) -> str:
    """Canonical display label for a single extension: '' -> '<no_extension>' (a real type,
    never a blank token that renders as a floating comma). R-map-reduce-extraction-7."""
    return "<no_extension>" if not ext else ext


def format_ext_counts(ext_counts: dict) -> str:
    """Canonical extension-inventory label: 'py(133), cs(20), <no_extension>(5)', sorted by
    count desc. ONE source of this format so preflight stdout / action text / dashboard agree."""
    return ", ".join(f"{ext_label(e)}({n})"
                     for e, n in sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0])))


def compute_source_coverage(pages_dir: Path) -> dict:
    """Reserves-and-recovery ledger (SYSTEM_SPEC L4). Surveys the FULL census, buckets every file
    NOTE: see format_ext_counts() for the canonical extension-inventory display label.
    by extraction MODALITY, and reports recovery per modality (tapped vs. booked-but-untapped).
    ALARMS on any modality that has reserves (census > 0) but 0 recovery — the untapped-reserve /
    silent-exclusion failure vector — regardless of whether it is 'code'. There is NO inert set:
    every reservoir is booked; a rig-gap is declared, never written off. Deterministic, no LLM."""
    census = Counter()                 # ext -> file count (the survey / denominator)
    for _, ext, _ in census_source_files(pages_dir):
        census[ext] += 1
    processed = Counter()              # ext -> files the current rig (CODE_EXTS) will tap
    for path, _, _ in reconstruct_code_files(pages_dir):
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        processed[ext] += 1

    per_ext = {}
    modality_reserves = Counter()      # modality -> census files
    modality_recovered = Counter()     # modality -> processed files
    for ext, n in sorted(census.items(), key=lambda x: -x[1]):
        proc = processed.get(ext, 0)
        modality = _modality_of(ext)
        per_ext[ext] = {"census": n, "processed": proc,
                        "coverage_pct": round(100.0 * proc / n, 1) if n else 0.0,
                        "modality": modality, "in_allowlist": ext in CODE_EXTS}
        modality_reserves[modality] += n
        modality_recovered[modality] += proc

    # per-modality recovery factor + reserves-with-no-rig alarms (the booked-but-untapped gaps)
    per_modality = {}
    alarms = []
    for modality, reserves in sorted(modality_reserves.items(), key=lambda x: -x[1]):
        rec = modality_recovered[modality]
        exts = sorted(e for e, d in per_ext.items() if d["modality"] == modality)
        # extension_counts keeps RAW ext keys ("" for no-extension) so matching stays exact;
        # displays render the label (<no_extension>) + count from this. extensions stays raw
        # (used by preflight _example_paths / _action_for_coverage_gap to match on real ext).
        ext_counts = {e: per_ext[e]["census"] for e in exts}
        per_modality[modality] = {"reserves": reserves, "structurally_parsed": rec,
                                  "structural_parse_pct": round(100.0 * rec / reserves, 1) if reserves else 0.0,
                                  "has_structural_rig": rec > 0, "extensions": exts,
                                  "extension_counts": ext_counts,
                                  "extensions_label": format_ext_counts(ext_counts)}
        if reserves > 0 and rec == 0:
            alarms.append({"modality": modality, "reserves": reserves, "extensions": exts,
                           "reason": "signal-bearing reserve with NO extractor (rig) — untapped; "
                                     "book + declare the gap, build a rig to recover (never write off)"})

    total_census = sum(census.values())
    total_proc = sum(processed.values())
    # NAMING (critical, go/no-go metric): "structurally_parsed" = files a CODE-GRAPH structural
    # extractor (rig) parsed into code entities — NOT files a distillation still extracted from, and
    # NOT "carries signal". The two axes are unrelated; the old name ("recovered"/"tapped") read as
    # the latter and caused a 0.0% (code-graph off by default) to be misread as "corpus untapped /
    # plate broken". This metric measures ONLY code-graph structural parse coverage.
    # conservation: every census file is either structurally parsed or a booked untapped reserve.
    booked_untapped = total_census - total_proc
    return {
        "total_source_files": total_census,
        "total_structurally_parsed": total_proc,
        "structural_parse_pct": round(100.0 * total_proc / total_census, 1) if total_census else 0.0,
        "booked_untapped": booked_untapped,
        "conserved": (total_proc + booked_untapped) == total_census,  # survey fully accounted for
        "per_modality": per_modality,
        "per_extension": per_ext,
        "coverage_alarms": alarms,
    }


# ── Extraction + validation ──────────────────────────────────────────────────

def _strip_json(text: str) -> str:
    # Single source of truth: hardened balanced-scan (ignores fences + trailing
    # prose). The extraction prompts return one JSON OBJECT. Replaces the old naive
    # first-'{'/last-'}' slice that could swallow trailing prose / stray braces.
    from json_extract import strip_json
    return strip_json(text, kind="object")


def _norm(s):
    return re.sub(r"[\[\]]", "", (s or "")).strip().lower()


def _present(token: str, source_lower: str) -> bool:
    bare = (token or "").split(".")[-1].strip().lower()
    return bool(bare) and bare in source_lower


# ── Anti-overfit guard helpers ───────────────────────────────────────────────
# Guards may CONFIRM (positive match) or REFUTE (positive contrary evidence),
# but NEVER reject on mere absence of a match. Absence -> keep + flag, not drop.

def _confirms_write(bare: str, source_lower: str) -> bool:
    """Positive-only: is there write DML (INSERT/UPDATE/DELETE/MERGE/SELECT INTO)
    targeting this object? Absence proves nothing (could be dynamic or a form we
    don't match), so we only ever use this to CONFIRM a write, never to force read."""
    if not bare:
        return False
    b = re.escape(bare)
    pat = (r"(?:insert\s+into|update|delete\s+from|merge(?:\s+into)?|into)\s+"
           r"(?:\[?\w+\]?\.)?\[?" + b + r"\b")
    return re.search(pat, source_lower) is not None


# ── Deterministic parser facts (runtimes/facts) — preferred over the model ────
# build_facts.py writes <run>/facts/facts.json with Roslyn (VB.NET, C#) + ScriptDom (T-SQL) output.
# Where it exists it is GROUND TRUTH for the relations it models, so this pass uses it two ways:
#   CORROBORATE  a model-proposed edge the parser also has is `confirmed` outright, and a direction
#                or relation the parser contradicts is RELABELLED (model proposes, parser disposes).
#   BACKFILL     a relation the parser found and the model missed is added, tagged in `symbol` with
#                its provenance (`facts:*`) so the two populations stay separable afterwards.
# Nothing here needs the LLM, so a facts-backed run degrades gracefully to model-only when absent.

FACTS_PROVENANCE = "facts:"          # prefix written into code_edges.symbol for backfilled edges
_OP_DIRECTION = {"select": "read", "join": "read", "insert": "write", "update": "write",
                 "delete": "write", "merge": "write", "select into": "write", "exec": "call"}


class FactsIndex:
    """Per-file view of the deterministic parser output, for corroboration and backfill."""

    def __init__(self, facts: dict | None):
        self.present = bool(facts)
        # file -> set of (dst_bare, kind, operation, direction) the parser actually found
        self.edges: dict[str, list[dict]] = defaultdict(list)
        # file -> {bare table name: set of write ops}, for direction arbitration
        self.writes: dict[str, set[str]] = defaultdict(set)
        # file -> the table a trigger defined in that file is attached to
        self.trigger_table: dict[str, str] = {}
        # corpus-wide name sets: what is REALLY a stored procedure vs a method of this system.
        # A model asked for "stored procedures" readily labels a DAL method (`m_DAL.AddHold`) as
        # one; the parser knows which is which, so it can settle it.
        self.proc_names: set[str] = set()
        self.method_names: set[str] = set()
        self.table_names: set[str] = set()
        if not facts:
            return
        sql = facts.get("sql") or {}
        for p in sql.get("procs") or []:
            if p.get("name"):
                self.proc_names.add(_normalize_dst(p["name"]))
        for t in sql.get("tables") or []:
            if t.get("name"):
                self.table_names.add(_normalize_dst(t["name"]))
        for sect in ("vb", "cs"):
            for m in (facts.get(sect) or {}).get("methods") or []:
                for key in ("name", "id"):
                    if m.get(key):
                        self.method_names.add(_normalize_dst(m[key]))
        for t in sql.get("triggers") or []:
            if t.get("file") and t.get("table"):
                self.trigger_table[t["file"]] = t["table"]
                self._add(t["file"], t["table"], "trigger", "trigger_on_table", "write",
                          "sql_trigger")
        # CLR sections: method calls, stored-procedure references, inline-SQL table access
        for sect in ("vb", "cs"):
            s = facts.get(sect) or {}
            for c in s.get("calls") or []:
                if c.get("file") and c.get("to"):
                    self._add(c["file"], c["to"], "method", "call", "call", f"{sect}_call")
            for r in s.get("proc_refs") or []:
                if r.get("file") and r.get("proc"):
                    self._add(r["file"], r["proc"], "stored_procedure", "exec", "call",
                              f"{sect}_proc_ref")
            for q in s.get("inline_sql") or []:
                self._add_access(q.get("file"), q, f"{sect}_inline_sql")
        # SQL bodies: the procs/triggers/scripts own reads, writes and EXECs
        for group, tag in (("procs", "sql_proc"), ("triggers", "sql_trigger"),
                           ("scripts", "sql_script")):
            for u in sql.get(group) or []:
                self._add_access(u.get("file"), u, tag)
                for x in u.get("exec_calls") or []:
                    if u.get("file") and x.get("proc"):
                        self._add(u["file"], x["proc"], "stored_procedure", "exec", "call", tag)

    def _add(self, file, dst, kind, operation, direction, tag):
        bare = _norm(dst).split(".")[-1]
        if not file or not bare:
            return
        self.edges[file].append({"dst": dst, "bare": bare, "kind": kind, "operation": operation,
                                 "direction": direction, "tag": tag})

    def _add_access(self, file, unit, tag):
        if not file:
            return
        for x in unit.get("reads") or []:
            if x.get("table"):
                self._add(file, x["table"], "table", "select", "read", tag)
        for x in unit.get("writes") or []:
            if not x.get("table"):
                continue
            op = (x.get("op") or "update").lower()
            self._add(file, x["table"], "table", op, _OP_DIRECTION.get(op, "write"), tag)
            self.writes[file].add(_norm(x["table"]).split(".")[-1])

    # -- queries ------------------------------------------------------------
    def for_file(self, path: str) -> list[dict]:
        return self.edges.get(path, [])

    def find(self, path: str, bare: str, kind_class: str | None = None,
             direction: str | None = None) -> dict | None:
        """The parser edge for (file, bare name), optionally restricted to a relation class
        ('table' / 'proc' / 'method' / 'trigger') and a direction.

        A table is commonly BOTH read and written in one file, so the direction matters: matching on
        the name alone would confirm a read against a write and lose one of the two relations.
        With no same-direction edge the first candidate is returned, which is what lets the parser
        overturn a direction the model got wrong."""
        cands = [e for e in self.edges.get(path, ())
                 if e["bare"] == bare and (not kind_class or _kind_class(e["kind"]) == kind_class)]
        if not cands:
            return None
        if direction:
            for e in cands:
                if e["direction"] == direction:
                    return e
        return cands[0]

    def knows_file(self, path: str) -> bool:
        return path in self.edges or path in self.trigger_table

    def is_method_not_proc(self, bare: str) -> bool:
        """Positive contrary evidence that a 'stored procedure' is really a method of this system."""
        return bool(bare) and bare in self.method_names and bare not in self.proc_names


def _kind_class(kind) -> str:
    k = (kind or "").strip().lower()
    if k in ("stored_procedure", "procedure", "proc", "function", "sproc"):
        return "proc"
    if k in ("table", "view", "synonym", "", "unknown"):
        return "table"
    if k == "method":
        return "method"
    if k == "trigger":
        return "trigger"
    return k


def load_facts(pages_dir: Path, facts_path: str | None = None) -> tuple[dict | None, str]:
    """(facts, how). Looks at --facts, then <run>/facts/facts.json next to the pages dir."""
    if facts_path == "":                       # explicitly disabled
        return None, "disabled"
    cands = [Path(facts_path)] if facts_path else [Path(pages_dir).parent / "facts" / "facts.json"]
    for p in cands:
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8", errors="replace")), str(p)
            except ValueError as exc:
                return None, f"{p} is not valid JSON ({exc})"
    return None, f"absent ({cands[0]})"


# Mid-line comment openers per language. A WHOLE-LINE opener (VB `REM`) is handled separately,
# because "rem " also occurs inside code (`Dim rem As Integer`, `Trem (x)`) and matching it anywhere
# would strip real code — and stripping real code is the one way this helper can do harm.
_LINE_COMMENT = {"vbnet": ("'",), "csharp": ("//",), "sql": ("--",), "cobol": ("*>",)}
_WHOLE_LINE_COMMENT = {"vbnet": ("rem",), "cobol": ("*",)}


def strip_comments(body: str, lang: str) -> str:
    """The source with its comment text removed, so "is this name in CODE?" can be asked.

    A single-pass scanner that respects double-quoted strings (so `"don't"` does not start a VB
    comment) and `/* */` blocks. Deliberately simple and deliberately CONSERVATIVE: it is used only
    to decide whether a name the model proposed appears anywhere outside a comment, and the failure
    that matters is stripping code (which would refute a real dependency), so it errs towards
    keeping text. An unknown language gets only the two unambiguous openers.
    """
    starts = _LINE_COMMENT.get(lang, ("//", "--"))
    whole = _WHOLE_LINE_COMMENT.get(lang, ())
    out: list[str] = []
    for raw in (body or "").splitlines():
        head = raw.lstrip().lower()
        if any(head == w or head.startswith(w + " ") or head.startswith(w + "\t") for w in whole):
            out.append("")
            continue
        line, i, in_str, n = [], 0, False, len(raw)
        while i < n:
            ch = raw[i]
            if in_str:
                if ch == '"':
                    if raw[i + 1:i + 2] == '"':           # VB/SQL doubled-quote escape
                        line.append('""')
                        i += 2
                        continue
                    in_str = False
                line.append(ch)
                i += 1
                continue
            if ch == '"':
                in_str = True
                line.append(ch)
                i += 1
                continue
            low = raw[i:].lower()
            if any(low.startswith(s) for s in starts):
                break                                     # rest of the line is a comment
            if low.startswith("/*"):
                end = raw.find("*/", i + 2)
                if end < 0:
                    break
                i = end + 2
                continue
            line.append(ch)
            i += 1
        out.append("".join(line))
    return "\n".join(out)


def _derive_confidence(llm_conf, verdict: str) -> float:
    base = llm_conf if isinstance(llm_conf, (int, float)) else 0.5
    if verdict == "confirmed":
        return round(max(base, 0.9), 2)
    if verdict == "conflict":
        return 0.3
    if verdict == "dynamic":
        return round(min(base, 0.6), 2)
    if verdict == "unconfirmed":
        return round(min(base, 0.5), 2)
    return round(base, 2)


def _derive_sql_role(defines, llm_role):
    """CREATE kind is ground truth for role; fall back to the LLM for scripts."""
    kinds = {_norm(d.get("kind")) for d in defines if isinstance(d, dict)}
    for k, role in [("procedure", "stored_procedure"), ("view", "view"),
                    ("function", "function"), ("trigger", "trigger"), ("table", "table")]:
        if k in kinds:
            return role, True
    return llm_role, False


def _confirm_cs_role(path, declarations):
    """Positive-only role confirmation from filename/decl-kind conventions; else None.
    Overrides the LLM's guess only when a reliable structural signal is present."""
    base = re.split(r"[\\/]", path)[-1]
    bl = base.lower()
    p = path.lower()
    kinds = {(_norm(d.get("kind")) or "") for d in declarations if isinstance(d, dict)}
    bases = " ".join(((d.get("base") or "") + " " + " ".join(d.get("implements") or []))
                     for d in declarations if isinstance(d, dict)).lower()

    # generated code (Designer/.g/.generated) — highly reliable filename signal (C# + VB)
    if (bl.endswith(".designer.cs") or bl.endswith(".g.cs") or bl.endswith(".generated.cs")
            or bl.endswith(".designer.vb")):
        return "generated"
    # VB.NET WinForms code-behind: `Inherits System.Windows.Forms.Form` (structural) or the
    # pervasive `frm`-prefix convention — a reliable role signal for the large VB client layer.
    if bl.endswith(".vb") and ("windows.forms.form" in bases or "system.windows.forms" in bases
                               or bl.startswith("frm")):
        return "form"
    # executable entry point
    if bl in ("program.cs", "startup.cs"):
        return "entrypoint"
    # interface / contract: C# 'I<Upper>' convention (guarded against I-prefixed
    # classes like IOManager) OR declarations that are exclusively interfaces
    if (re.match(r"I[A-Z]", base) and "class" not in kinds) or (kinds and kinds <= {"interface"}):
        return "interface"
    # service proxy / client
    if "proxy" in bl:
        return "service"
    if "controller" in bl or "controller" in bases or "apicontroller" in bases:
        return "controller"
    if "/repositories/" in p or bl.endswith("repository.cs") or "repository" in bases:
        return "repository"
    if "/dto" in p or bl.endswith("dto.cs"):
        return "dto"
    if bl.startswith("test") or bl.endswith("tests.cs") or "/tests/" in p:
        return "test"
    return None


def _classify_module(mod, source_lower, path):
    if not mod:
        return None, None, False
    m = mod.strip()
    validated = _present(m, source_lower) or m.lower() in path.lower()
    if m.upper() in MODULE_ALLOWLIST:
        return m.upper(), None, validated
    return None, m, validated


def _mk_edge(src, dst, etype, lang, verdict, **kw):
    """Build an edge with a three-state verdict and derived confidence.
    Nothing is dropped on absence — unconfirmed edges are KEPT and flagged."""
    e = {"src": src, "dst": dst, "type": etype, "lang": lang, "kind": kw.get("kind"),
         "operation": kw.get("operation"), "direction": kw.get("direction"),
         "resolution": kw.get("resolution"), "symbol": kw.get("symbol"),
         "validated": int(verdict == "confirmed"), "dynamic": int(bool(kw.get("dynamic"))),
         "confidence": kw.get("confidence"), "verdict": verdict,
         "direction_verdict": kw.get("direction_verdict"),
         "derived_confidence": _derive_confidence(kw.get("confidence"), verdict)}
    return e


CLR_CALL_STOP = {
    # framework/runtime members a `calls` entry must never be: they are not system methods.
    "tostring", "trim", "ltrim", "rtrim", "ucase", "lcase", "len", "length", "substring", "split",
    "replace", "format", "parse", "tryparse", "add", "addwithvalue", "clear", "close", "open",
    "dispose", "equals", "gethashcode", "gettype", "show", "showdialog", "writeline", "readline",
    "cint", "cdec", "cstr", "cdate", "clng", "cdbl", "cbyte", "isnothing", "isdbnull", "isnull",
    "nothing", "new", "select", "where", "count", "contains", "tolist", "toarray", "concat",
}


def _normalize_dst(name) -> str:
    """Join-friendly destination token: brackets/quotes stripped, schema prefix dropped."""
    return _norm((name or "").strip().strip("'\"[]")).split(".")[-1]


def _assemble(path, lang, llm, body, page, model_id, facts: "FactsIndex | None" = None):
    source_lower = body.lower()
    facts = facts if facts is not None else FactsIndex(None)
    # the same source with comment TEXT removed: a dependency has to be stated in code, and a name
    # that appears only in a comment is a mention (real, useful, different relation), not an edge.
    code_lower = strip_comments(body, lang).lower()
    mod, tag, mod_ok = _classify_module(llm.get("module"), source_lower, path)
    defines = [d for d in (llm.get("defines") or llm.get("declarations") or [])
               if isinstance(d, dict) and d.get("name")]

    # Entities: code_name anchor validated by presence; keep-and-flag on absence
    # (the business_name decoding stays inferred — cross-checked vs stills elsewhere).
    ents = []
    for e in (llm.get("business_entities") or []):
        if not isinstance(e, dict) or not e.get("code_name"):
            continue
        anchored = _present(e["code_name"], source_lower)
        ents.append({"code_name": e["code_name"], "business_name": e.get("business_name"),
                     "confidence": e.get("confidence"),
                     "anchor_verdict": "confirmed" if anchored else "unconfirmed"})

    # Role: SQL role is ground-truthed by CREATE kind; C# confirmed by path/base.
    if lang == "sql":
        role, role_conf = _derive_sql_role(defines, llm.get("file_role"))
    elif lang == "cobol":
        role, role_conf = llm.get("file_role"), False
    else:
        cs_role = _confirm_cs_role(path, defines)
        role, role_conf = (cs_role or llm.get("file_role")), bool(cs_role)

    node = {
        "path": path, "lang": lang, "role": role, "role_confirmed": role_conf,
        "module": mod, "unverified_tag": tag, "module_validated": mod_ok,
        "business_intent": llm.get("business_intent"),
        "defines": defines, "business_entities": ents,
        "prov": {"model": model_id, "extracted_at": _dt.datetime.now(_dt.UTC).isoformat(),
                 "page": page},
        "edges": [],
    }

    if lang == "sql":
        self_bare = {_norm(d.get("name")).split(".")[-1] for d in defines}
        for r in llm.get("references", []):
            if not isinstance(r, dict):
                continue
            name = _norm(r.get("name"))
            bare = name.split(".")[-1]
            # structural excludes only (safe deterministic, not LLM-judgment)
            if not name or bare in SQL_STOP or name in SQL_STOP or bare in self_bare:
                continue
            dynamic = r.get("resolution") == "dynamic_sql"
            if dynamic:
                verdict = "dynamic"            # unconfirmable by design — keep, lower conf
            elif _present(name, source_lower):
                verdict = "confirmed"
            else:
                verdict = "unconfirmed"        # KEEP (regex silence != refutation)
            # direction: confirm-only. write DML present -> confirm/conflict; else defer.
            llm_dir = r.get("direction")
            dverdict = "unconfirmed"
            if _confirms_write(bare, source_lower):
                dverdict = "confirmed" if llm_dir == "write" else "conflict"
            kind, operation, etype = r.get("kind"), r.get("operation"), "REFERENCES"
            # Parser disposal: a reference from a TRIGGER's own file to the table the trigger is
            # attached to, which the parser records no write for, is the trigger-attachment
            # relation — not a table write. Relabel it instead of carrying a mislabelled edge.
            # The edge type changes too: arbitrate.resolve_kind_by_create() overturns the `kind` of
            # a REFERENCES edge to the target's CREATE kind ("table"), which would silently undo
            # this. `TRIGGERS` is outside that resolver's filter, so the relation survives.
            if (facts.trigger_table.get(path) and bare == _normalize_dst(facts.trigger_table[path])
                    and bare not in facts.writes.get(path, set())
                    and _kind_class(kind) == "table"):
                kind, operation, dverdict = "trigger", "trigger_on_table", "arbitrated"
                etype = "TRIGGERS"
            node["edges"].append(_mk_edge(
                path, name, etype, "sql", verdict,
                kind=kind, operation=operation, direction=llm_dir,
                resolution=r.get("resolution"), dynamic=dynamic,
                confidence=r.get("confidence"), direction_verdict=dverdict))
    elif lang == "cobol":
        self_names = {_norm(d.get("name")) for d in defines}
        for r in llm.get("references", []):
            if not isinstance(r, dict):
                continue
            name = _norm(r.get("name")).strip("'\"")
            if not name or name in self_names:
                continue
            verdict = "confirmed" if _present(name, source_lower) else "unconfirmed"
            etype = {"call": "CALLS", "copy": "IMPORTS"}.get(r.get("operation"), "REFERENCES")
            node["edges"].append(_mk_edge(
                path, name, etype, "cobol", verdict, kind=r.get("kind"),
                operation=r.get("operation"), direction=r.get("direction"),
                resolution="static", confidence=r.get("confidence")))
    else:
        # C# and VB.NET share the CLR shape: imports/gateway namespaces PLUS the relations that
        # actually carry the dependency structure — method->method calls, stored-procedure
        # executions and table reads/writes (inline SQL or via a DAL). Model proposes; the
        # occurrence regex and the deterministic parser facts dispose.
        for u in llm.get("usings", []):
            if not _norm(u):
                continue
            verdict = "confirmed" if _present(u, source_lower) else "unconfirmed"
            node["edges"].append(_mk_edge(
                path, _norm(u), "IMPORTS", lang, verdict, resolution="static"))
        for r in llm.get("references", []):
            if not isinstance(r, dict) or (r.get("resolution") or "").lower() != "remote":
                continue
            gw = _norm(r.get("gateway"))
            if not gw or gw in CS_STOP:
                continue
            verdict = "confirmed" if _present(gw, source_lower) else "unconfirmed"
            node["edges"].append(_mk_edge(
                path, gw, "REFERENCES", lang, verdict, kind=r.get("kind"),
                symbol=r.get("symbol"), resolution="static", confidence=r.get("confidence")))

        # ---- method -> method calls ------------------------------------------------------
        for c in (llm.get("calls") or []):
            if not isinstance(c, dict):
                continue
            callee = (c.get("callee") or "").strip()
            bare = _normalize_dst(callee)
            if not bare or bare in CLR_CALL_STOP:
                continue
            hit = facts.find(path, bare, "method")
            if hit:
                verdict, dv = "confirmed", "parser_confirmed"
            elif _present(callee, source_lower):
                verdict, dv = "confirmed", None
            else:
                verdict, dv = "unconfirmed", None       # KEEP + flag (silence != refutation)
            node["edges"].append(_mk_edge(
                path, _norm(callee) or bare, "CALLS", lang, verdict, kind="method",
                operation="call", direction="call", resolution="static",
                symbol=c.get("receiver") or c.get("caller"), confidence=c.get("confidence"),
                direction_verdict=dv))

        # ---- stored procedures, tables, triggers, reports --------------------------------
        for r in (llm.get("data_refs") or []):
            if not isinstance(r, dict):
                continue
            raw = (r.get("name") or "").strip()
            bare = _normalize_dst(raw)
            if not bare or bare in SQL_STOP or bare in CS_STOP:
                continue
            kind = (r.get("kind") or "table").lower()
            operation = (r.get("operation") or "unknown").lower()
            direction = (r.get("direction") or _OP_DIRECTION.get(operation) or "read").lower()
            dynamic = (r.get("resolution") or "").lower() == "dynamic_sql"

            # Parser disposal A: "stored procedure" that the parser knows as a METHOD of this
            # system (`m_DAL.AddHold`) is a call, not an EXEC. Re-emit it as the relation it is.
            if _kind_class(kind) == "proc" and facts.is_method_not_proc(bare):
                node["edges"].append(_mk_edge(
                    path, _norm(raw) or bare, "CALLS", lang, "confirmed", kind="method",
                    operation="call", direction="call", resolution="static",
                    symbol="arbitrated:method_not_proc", confidence=r.get("confidence"),
                    direction_verdict="arbitrated"))
                continue
            # Parser disposal B: the name occurs ONLY inside a comment -> a comment mention, not a
            # dependency. Positive contrary evidence, so the relation is relabelled (kept, visible,
            # and excluded from dependency scoring) rather than asserted or thrown away.
            if not dynamic and _present(bare, source_lower) and not _present(bare, code_lower):
                node["edges"].append(_mk_edge(
                    path, _norm(raw) or bare, "MENTIONS", lang, "refuted", kind="comment_mention",
                    operation="mention", direction=None, resolution="comment_only",
                    symbol=f"arbitrated:comment_only:{kind}", confidence=r.get("confidence"),
                    direction_verdict="refuted"))
                continue

            hit = facts.find(path, bare, _kind_class(kind), direction)
            if hit:
                # the parser found this relation too: confirmed, and ITS operation/direction win
                verdict, dv = "confirmed", "parser_confirmed"
                operation, direction = hit["operation"], hit["direction"]
            elif dynamic:
                verdict, dv = "dynamic", None           # unconfirmable by design
            elif _present(bare, source_lower):
                verdict, dv = "confirmed", None
                if direction != "write" and _confirms_write(bare, source_lower):
                    dv = "conflict"                     # write DML positively found; arbitrate later
                elif direction == "write" and _confirms_write(bare, source_lower):
                    dv = "confirmed"
            else:
                verdict, dv = "unconfirmed", None
            node["edges"].append(_mk_edge(
                path, _norm(raw) or bare, "REFERENCES", lang, verdict, kind=kind,
                operation=operation, direction=direction,
                resolution="dynamic_sql" if dynamic else "static", dynamic=dynamic,
                symbol=r.get("via"), confidence=r.get("confidence"), direction_verdict=dv))

    # ---- BACKFILL from the deterministic parser --------------------------------------------
    # Anything the parser found and the model did not propose is added here, tagged with its
    # provenance in `symbol`, so a parser-backed relation is never lost to a prompt's blind spot
    # and the model-only population stays separable for measurement.
    # The dedup key carries the DIRECTION: one file routinely both reads and writes the same table,
    # and collapsing those into one key would drop whichever relation the model did not propose.
    have = {(e["type"], _normalize_dst(e["dst"]), _kind_class(e.get("kind")), e.get("direction"))
            for e in node["edges"]}
    for fe in facts.for_file(path):
        etype = {"method": "CALLS", "trigger": "TRIGGERS"}.get(fe["kind"], "REFERENCES")
        key = (etype, fe["bare"], _kind_class(fe["kind"]), fe["direction"])
        if key in have:
            continue
        have.add(key)
        node["edges"].append(_mk_edge(
            path, fe["dst"], etype, lang, "confirmed", kind=fe["kind"],
            operation=fe["operation"], direction=fe["direction"], resolution="static",
            symbol=FACTS_PROVENANCE + fe["tag"], confidence=1.0,
            direction_verdict="parser_confirmed"))
    return node


def _extract_one(llm, model_id, path, lang, body, page, facts: "FactsIndex | None" = None):
    system, tmpl = _LANG_PROMPTS.get(lang, (CS_SYSTEM, CS_USER))  # unknown lang → C#-shape default
    user = tmpl.format(path=path, source=body)
    res, last = None, None
    for attempt in range(4):
        try:
            res = bedrock_client.call_llm(llm, model_id, system, user,
                                             max_tokens=8192, temperature=0.0)
            break
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    if res is None:
        return {"path": path, "lang": lang, "error": str(last)}, {"in": 0, "out": 0}
    meta = {"in": res.input_tokens, "out": res.output_tokens}
    try:
        llm = json.loads(_strip_json(res.output_text))
    except Exception as exc:
        return {"path": path, "lang": lang, "error": f"parse: {exc}"}, meta
    return _assemble(path, lang, llm, body, page, model_id, facts), meta


# ── Persistence ──────────────────────────────────────────────────────────────

def _persist(conn: sqlite3.Connection, node: dict) -> None:
    p = node["path"]
    conn.execute(
        "INSERT OR REPLACE INTO source_files "
        "(path,lang,role,role_confirmed,module,unverified_tag,module_validated,business_intent,page,model,extracted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (p, node["lang"], node["role"], int(bool(node.get("role_confirmed"))),
         node["module"], node["unverified_tag"], int(node["module_validated"]),
         node["business_intent"], node["prov"]["page"], node["prov"]["model"],
         node["prov"]["extracted_at"]))
    for d in node["defines"]:
        conn.execute("INSERT OR REPLACE INTO code_objects (path,object,kind) VALUES (?,?,?)",
                     (p, _norm(d.get("name")), d.get("kind")))
    for e in node["business_entities"]:
        conn.execute("INSERT OR REPLACE INTO code_entities "
                     "(path,code_name,business_name,confidence,anchor_verdict,stills_corroborated) "
                     "VALUES (?,?,?,?,?,?)",
                     (p, e["code_name"], e.get("business_name"), e.get("confidence"),
                      e.get("anchor_verdict"), None))
    for ed in node["edges"]:
        conn.execute("INSERT INTO code_edges "
                     "(src,dst,type,lang,kind,operation,direction,resolution,symbol,validated,dynamic,confidence,verdict,direction_verdict,derived_confidence) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ed["src"], ed["dst"], ed["type"], ed["lang"], ed.get("kind"),
                      ed.get("operation"), ed.get("direction"), ed.get("resolution"),
                      ed.get("symbol"), int(bool(ed.get("validated"))),
                      int(bool(ed.get("dynamic"))), ed.get("confidence"),
                      ed.get("verdict"), ed.get("direction_verdict"),
                      ed.get("derived_confidence")))


# ── Orchestration ────────────────────────────────────────────────────────────

def build_code_graph(pages_dir: str, db_path: str, model_id: str = DEFAULT_MODEL,
                     profile: str = "", region: str = REGION_DEFAULT,
                     concurrency: int = 12, min_chars: int = 400, max_chars: int = 30000,
                     limit: int = 0, facts_path: str | None = None) -> dict:
    """Run the code-graph pass over the pages and persist to the shared DB."""
    pages = Path(pages_dir)
    facts_doc, facts_how = load_facts(pages, facts_path)
    facts = FactsIndex(facts_doc)
    print(f"[code_graph] deterministic parser facts: {facts_how}"
          + (f" ({len(facts.edges)} files, "
             f"{sum(len(v) for v in facts.edges.values())} parser relations)" if facts.present
             else " — corroboration/backfill OFF, model proposals only"))
    files = [(p, CODE_EXTS[p.rsplit('.', 1)[-1].lower()], s, pg)
             for p, s, pg in reconstruct_code_files(pages)
             if min_chars <= len(s) <= max_chars]
    if limit:
        files = files[:limit]
    print(f"[code_graph] {len(files)} code files to process (concurrency={concurrency})")

    conn = sqlite3.connect(db_path)
    ensure_tables(conn)

    # ── COVERAGE-CONSERVATION GUARD (ground-truth integrity) ──────────────────────────────
    # Prove the pass accounts for every source file it saw, per language. Persist + emit the
    # report; surface any code-bearing language with 0% coverage as a LOUD alarm (never silent).
    cov = compute_source_coverage(pages)
    now = _dt.datetime.now(_dt.UTC).isoformat()
    for ext, d in cov["per_extension"].items():
        conn.execute(
            "INSERT OR REPLACE INTO source_coverage "
            "(ext,modality,census,processed,coverage_pct,in_allowlist,alarm,computed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ext, d["modality"], d["census"], d["processed"], d["coverage_pct"],
             int(d["in_allowlist"]), int(d["processed"] == 0), now))
    conn.commit()
    print(f"[code_graph] source census: {cov['total_source_files']} files surveyed, "
          f"{cov['structural_parse_pct']}% structurally parsed by code-graph rigs (CODE_EXTS={sorted(CODE_EXTS)}); "
          f"{cov['booked_untapped']} files booked as untapped reserve; conserved={cov['conserved']}")
    for modality, m in sorted(cov["per_modality"].items(), key=lambda x: -x[1]["reserves"]):
        print(f"[code_graph]   modality {modality:22s} reserves={m['reserves']:6d} "
              f"structurally_parsed={m['structurally_parsed']:6d} ({m['structural_parse_pct']}%) "
              f"rig={'yes' if m['has_structural_rig'] else 'NO'}")
    if cov["coverage_alarms"]:
        print("[code_graph] *** COVERAGE ALARM — signal-bearing reserves with NO extractor rig "
              "(booked/untapped, NOT written off — build a rig or declare the caveat downstream): ***")
        for a in sorted(cov["coverage_alarms"], key=lambda x: -x["reserves"]):
            print(f"[code_graph]     modality {a['modality']}: {a['reserves']} files "
                  f"(exts {a['extensions'][:8]}), 0 recovered")

    llm = bedrock_client.create_llm_client(profile, region)

    tot_in = tot_out = ok = errs = 0
    edge_verdicts = Counter()
    dir_verdicts = Counter()
    ent_verdicts = Counter()
    edge_provenance = Counter()   # model-proposed vs parser-backfilled, per relation class
    edge_relations = Counter()
    role_confirmed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [pool.submit(_extract_one, llm, model_id, p, lang, body, pg, facts)
                for (p, lang, body, pg) in files]
        for i, fut in enumerate(as_completed(futs), 1):
            node, meta = fut.result()
            tot_in += meta["in"]; tot_out += meta["out"]
            if "error" in node:
                errs += 1
                if errs <= 10:
                    print(f"[code_graph]   ERROR {node['path']}: {str(node['error'])[:300]}")
            else:
                _persist(conn, node)
                ok += 1
                role_confirmed += int(bool(node.get("role_confirmed")))
                for e in node["edges"]:
                    edge_verdicts[e.get("verdict")] += 1
                    if e.get("direction_verdict"):
                        dir_verdicts[e["direction_verdict"]] += 1
                    backfilled = str(e.get("symbol") or "").startswith(FACTS_PROVENANCE)
                    edge_provenance[("parser_backfill" if backfilled else "model_proposed")] += 1
                    edge_relations[(node["lang"], _kind_class(e.get("kind")) or e.get("type"),
                                    "parser_backfill" if backfilled else "model_proposed")] += 1
                for e in node["business_entities"]:
                    ent_verdicts[e.get("anchor_verdict")] += 1
            if i % 200 == 0:
                conn.commit()
                print(f"[code_graph]   {i}/{len(files)} (ok={ok} err={errs})")
    conn.commit()
    conn.close()

    cost = tot_in / 1e6 * PRICE_IN + tot_out / 1e6 * PRICE_OUT
    total_edges = sum(edge_verdicts.values())
    stats = {
        "coverage": cov,   # the census-vs-processed conservation report (ground-truth guard)
        "files": len(files), "ok": ok, "errors": errs,
        "input_tokens": tot_in, "output_tokens": tot_out,
        "cost_usd": round(cost, 2), "wall_seconds": round(time.time() - t0, 1),
        # fit diagnostics: high 'unconfirmed' => regex too narrow (overfit);
        # 'conflict' => LLM/regex disagreement to human-audit.
        "edge_verdicts": dict(edge_verdicts),
        "edge_unconfirmed_rate": round(edge_verdicts.get("unconfirmed", 0) / total_edges, 3) if total_edges else None,
        "direction_verdicts": dict(dir_verdicts),
        "entity_anchor_verdicts": dict(ent_verdicts),
        # provenance split: how much of the graph the MODEL proposed vs how much the deterministic
        # parser backfilled. Reported, never blended — an accuracy claim has to say which it is.
        "parser_facts": {"source": facts_how, "present": facts.present,
                         "files_with_parser_relations": len(facts.edges),
                         "parser_relations": sum(len(v) for v in facts.edges.values())},
        "edge_provenance": dict(edge_provenance),
        "edge_relations": {f"{lg}/{rel}/{prov}": n
                           for (lg, rel, prov), n in sorted(edge_relations.items(),
                                                            key=lambda kv: str(kv[0]))},
        "role_confirmed_files": role_confirmed,
    }
    print(f"[code_graph] done: {stats}")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM-based code dependency graph pass")
    ap.add_argument("pages_dir")
    ap.add_argument("db_path", help="shared_vectors.db to write tables into")
    ap.add_argument("--model-id", default=DEFAULT_MODEL)
    ap.add_argument("--profile", default="")
    ap.add_argument("--region", default=REGION_DEFAULT)
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0, help="0 = all files")
    ap.add_argument("--stats-out", default="", help="write the run stats JSON here")
    ap.add_argument("--facts", default=None,
                    help="deterministic parser facts.json to corroborate/backfill with "
                         "(default: <pages_dir>/../facts/facts.json). Pass an empty string to "
                         "run on model proposals only.")
    args = ap.parse_args()
    stats = build_code_graph(args.pages_dir, args.db_path, model_id=args.model_id,
                             profile=args.profile, region=args.region,
                             concurrency=args.concurrency, limit=args.limit,
                             facts_path=args.facts)
    if args.stats_out:
        Path(args.stats_out).write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")
    # Surface LLM failures instead of exiting 0 on a graph that silently lost files.
    files, errs = stats["files"], stats["errors"]
    if files == 0:
        print("[code_graph] WARNING: 0 code files matched CODE_EXTS — code graph is empty "
              "(see the coverage census above).")
        return 0
    if errs and errs / files > MAX_ERROR_RATE:
        print(f"[code_graph] ERROR: {errs}/{files} files failed extraction "
              f"(> {MAX_ERROR_RATE:.0%} allowed; DISTILL_CODE_GRAPH_MAX_ERROR_RATE) — failing the pass.")
        return 1
    if errs:
        print(f"[code_graph] WARNING: {errs}/{files} files failed extraction (within tolerance).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

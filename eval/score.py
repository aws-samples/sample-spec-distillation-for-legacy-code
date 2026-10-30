#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Score a spec-distillation output against an answer key.

The report is split into two layers that must not be read the same way.

**A. DETERMINISTIC (exact).** No model is involved; the same inputs always give
the same bytes. Anything here is a fact about the spec and the source tree:

  1. Citation verification -- parse every supported citation format, resolve the
     file under ``--source-root``, check the line range against an exactly
     counted file, separate genuine out-of-range errors from the pager's
     phantom-trailing-line off-by-one, and record which key locations each
     citation overlaps.
  2. Source coverage -- which key locations the spec cites anywhere, which source
     files and lines are never referenced, per-file citation density.
  3. Structural checks -- duplicate requirement ids, unresolved cross-references,
     requirements with no citation, orphan citations, malformed sections.
  4. Segmentation -- named, recorded, and compared. ``--segment requirements``
     normalises a spec with requirement ids to one finding per requirement, and
     ``--compare-to`` refuses to let two specs be compared quietly when their
     finding counts differ by more than 2x.

**B. JUDGED (estimated, with measured uncertainty).** An LLM judge decides
whether a finding expresses a key item and whether it is true of the source. It
is run N times (``--judge-runs``, default 3) with a majority vote per item; it
may abstain (``uncertain``); it returns schema-validated JSON through Converse
tool use; verdicts are cached by content hash; and the report carries per-item
agreement, across-pass spread, bootstrap 95% CIs and Fleiss/Cohen kappa.
See ``eval/judge/`` and the "what is exact vs estimated" section of eval/README.md.

Usage
  python3 eval/score.py --spec SPEC.md|SPEC.json --key KEY.yaml --source-root DIR \
      --out-dir OUT [--segment requirements] [--judge-runs 3] [--no-llm]

Note for callers importing this module (eval/benefits/*): ``score()`` keeps its
old positional signature AND its old defaults (one judge pass, full source dump),
so existing programmatic callers are unaffected. The *CLI* defaults to the new,
more rigorous settings.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

SRC_EXT = r"(?:cob|cbl|cpy|cobol|js|ts|py|java|rs|c|h|cs|sql|jcl|vb|txt|rpt)"
FILE_RE = rf"(?P<file>[A-Za-z0-9_./\\-]+\.{SRC_EXT})"
LINE_RE = r"L?(?P<a>\d+)(?:\s*(?:-|–|—|to|\.\.)\s*L?(?P<b>\d+))?"

#: Forward citations: a file name, then a line or range.
#: file.vb:L10-L20 | file.vb:10-20 | file.vb#L10 | file.vb (lines 10-20)
#: file.vb L10-20 | file.vb, 10-20 | file.vb:ll. 10-20 | en/em dash | "to" | ".."
CIT_PATTERNS = [
    re.compile(FILE_RE + r"`?\s*(?::|#|,)?\s*\(?\s*(?:lines?\s+|ll?\.\s*)?" + LINE_RE + r"\b", re.I),
]
#: Reverse citations: "lines 10-20 of file.vb" / "L10-L20 in path/file.sql".
REVERSE_CIT_RE = re.compile(
    r"(?:lines?\s+|L)(?P<a>\d+)(?:\s*(?:-|–|—|to|\.\.)\s*L?(?P<b>\d+))?\s+(?:of|in|from)\s+`?" + FILE_RE,
    re.I)
#: A line or range with no file: resolved against the nearest preceding cited
#: file, else a file-named heading, else the only file in the source root.
BARE_LINE_RE = re.compile(
    r"(?<![\w.:/#-])(?:lines?\s+|L)(?P<a>\d+)(?:\s*(?:-|–|—|to)\s*L?(?P<b>\d+))?\b", re.I)
FILE_ONLY_RE = re.compile(FILE_RE, re.I)

#: Requirement identifiers, e.g. DT-0005, RL-0001, BR-018, REQ-12.
REQ_ID_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,7}-\d{2,6})\b")

STOP = set("""a an the of to in on for and or is are be by with as at from that this it its into
than then when if else any all each per via which who whom whose was were has have had not no
only also can may will shall should must does do done using use used value values program
system file line lines""".split())

#: Per-finding text retained in memory and in the report. Citations are parsed
#: from the *full* text before truncation; this only bounds memory on huge specs.
DEFAULT_MAX_FINDING_CHARS = 20000
#: Per-finding detail written to score.json. Metrics always cover every finding.
DEFAULT_MAX_REPORT_FINDINGS = 5000
#: Warn when two specs' finding counts differ by more than this factor.
COMPARABILITY_RATIO = 2.0

CITATION_FORMATS = [
    "file.ext:L10-L20", "file.ext:10-20", "file.ext#L10-L20", "file.ext (lines 10-20)",
    "file.ext L10", "file.ext:L10", "file.ext, 10-20", "file.ext:ll. 10-20",
    "en/em dash, 'to' and '..' range separators",
    "file.ext:L10-L20, L30-L40 (later ranges inherit the file)",
    "bare L10-L20 / lines 10-20 (nearest cited file or file-named heading)",
    "lines 10-20 of file.ext (reverse order)",
    "literal names with spaces/parens, e.g. 'AP Open Payables by Job (412).txt:L30'",
]


# ============================================================ exact line handling
#: Only CR, LF and CRLF start a new line. str.splitlines() also breaks on \x0b,
#: \x0c (form feed -- common in legacy COBOL and VB listings), \x1c-\x1e, \x85,
#: \u2028 and \u2029, which no editor, pager or compiler counts as a line break.
#: Using splitlines() would over-count lines and silently shift every citation.
_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")


def split_lines_exact(text: str) -> list[str]:
    """Split on CR/LF/CRLF only. A single trailing newline does not add a line."""
    parts = _LINE_SPLIT_RE.split(text)
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def read_source_text(p: Path) -> str:
    """Read a source file, dropping a UTF-8 BOM (it is not part of line 1)."""
    data = p.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")


def line_count(p: Path) -> int:
    """Exact 1-based line count of a source file."""
    return len(split_lines_exact(read_source_text(p)))


# ----------------------------------------------------------------------- loading
def load_key(path: Path) -> dict:
    text = Path(path).read_text()
    if Path(path).suffix in (".yaml", ".yml"):
        if yaml is None:
            raise SystemExit("PyYAML not available; provide a JSON key")
        key = yaml.safe_load(text)
    else:
        key = json.loads(text)
    if not isinstance(key, dict):
        raise SystemExit(f"{path}: answer key must be a mapping with an 'items' list")
    return normalize_key(key)


def normalize_key(key: dict) -> dict:
    """Accept both key formats.

    COBOL format: items[{id, type, core, statement, citations: ["file:a-b"], evidence}]
    Synthetic-corpus format: items[{id, type, statement,
        locations: [{file, start_line, end_line}], evidence_class, flags: []}]
    Synthetic items are mapped onto the COBOL fields (citations, evidence); flags are kept.
    """
    key.setdefault("items", [])
    seen = set()
    for it in key["items"]:
        if "citations" not in it and it.get("locations"):
            it["citations"] = [f"{loc['file']}:{loc['start_line']}-{loc.get('end_line', loc['start_line'])}"
                               for loc in it["locations"]]
        if "evidence" not in it and "evidence_class" in it:
            it["evidence"] = it["evidence_class"]
        it.setdefault("flags", [])
        it.setdefault("citations", [])
        kid = str(it.get("id", ""))
        if not kid:
            raise SystemExit("answer key has an item with no id")
        if kid in seen:
            raise SystemExit(f"answer key has duplicate item id {kid!r}")
        seen.add(kid)
    return key


def source_files(root: Path, exclude: list[str] | None = None) -> dict[str, Path]:
    from fnmatch import fnmatch
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(root))
            if exclude and any(fnmatch(rel, g) or fnmatch(p.name, g) for g in exclude):
                continue
            out[rel] = p
    return out


def literal_file_patterns(files: dict[str, Path]) -> list[re.Pattern]:
    """Citation patterns for source files whose names FILE_RE cannot express
    (spaces, parentheses, unusual extensions), e.g. 'AP Open Payables by Job (412).txt:L30'."""
    pats = []
    for rel in sorted(files):
        base = rel.split("/")[-1]
        if re.fullmatch(r"[A-Za-z0-9_.\-]+\." + SRC_EXT, base, re.I):
            continue
        alts = sorted({re.escape(rel), re.escape(base)}, key=len, reverse=True)
        pats.append(re.compile(r"(?P<file>" + "|".join(alts) + r")`?\s*(?::|#|,)?\s*\(?\s*(?:lines?\s+|ll?\.\s*)?"
                               + LINE_RE + r"\b", re.I))
    return pats


def resolve_file(name: str, files: dict[str, Path]) -> str | None:
    """Resolve a cited name to a key of ``files``: exact relative path first, then
    a unique basename match, then the first basename match in sorted order."""
    if not name:
        return None
    name = name.replace("\\", "/").lstrip("./")
    if name in files:
        return name
    base = name.split("/")[-1].lower()
    cands = sorted(k for k in files if k.split("/")[-1].lower() == base)
    if cands:
        return cands[0]
    # cited with extra leading path components, e.g. "src/app/file.vb" for "app/file.vb"
    tail = [k for k in sorted(files) if name.endswith("/" + k) or k.endswith("/" + name)]
    return tail[0] if len(tail) == 1 else None


# ================================================================== segmentation
EVIDENCE_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?[*_]{0,2}\s*(?:evidence|citations?)\s*[*_]{0,2}\s*:", re.I)

SEGMENT_MODES = ("auto", "lines", "requirements")
SEGMENT_DOC = {
    "auto": "heading sections containing an 'Evidence:'/'Citations:' line become one finding; "
            "everything else splits per bullet / paragraph / table row / code block",
    "lines": "every bullet, paragraph, table row and code block is its own finding "
             "(the original behaviour)",
    "requirements": "one finding per requirement id; index-table rows fold into their "
                    "requirement; remaining prose is reported separately as narrative",
}


def iter_spec_lines(path: Path):
    """Stream a spec file line by line, BOM-stripped and newline-stripped.

    Streaming matters: a 50 MB spec must not be materialised twice as one string.
    """
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        for raw in fh:
            yield raw.rstrip("\n").rstrip("\r")


def heading_sections(text: str) -> list[dict]:
    """Split markdown (outside code fences) into heading sections: {line, level, has_evidence}.
    Section 0 is the preamble before the first heading."""
    return _heading_sections_from(lambda: iter(text.splitlines()))


def _heading_sections_from(line_source) -> list[dict]:
    secs = [{"line": "", "level": 0, "has_evidence": False}]
    in_code = False
    for raw in line_source():
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code:
            m = re.match(r"^(#{1,6})\s+(.*)", raw.rstrip())
            if m:
                secs.append({"line": raw.rstrip(), "level": len(m.group(1)), "has_evidence": False})
                continue
            if EVIDENCE_LINE_RE.match(raw):
                secs[-1]["has_evidence"] = True
    return secs


def segment_markdown(text: str, mode: str = "auto",
                     max_finding_chars: int = DEFAULT_MAX_FINDING_CHARS) -> list[dict]:
    """Segment markdown held in memory. See ``segment_spec`` for the streaming form."""
    return _segment(lambda: iter(text.splitlines()), mode, max_finding_chars)


def segment_markdown_lines(text: str, keep_secs: set | None = None) -> list[dict]:
    """Backwards-compatible entry point for the 'lines' strategy."""
    return _segment_lines(lambda: iter(text.splitlines()), keep_secs, DEFAULT_MAX_FINDING_CHARS)


def segment_spec(path: Path, mode: str = "auto",
                 max_finding_chars: int = DEFAULT_MAX_FINDING_CHARS) -> list[dict]:
    return _segment(lambda: iter_spec_lines(path), mode, max_finding_chars)


def _segment(line_source, mode: str, max_finding_chars: int) -> list[dict]:
    if mode not in SEGMENT_MODES:
        raise SystemExit(f"unknown --segment {mode!r}; choose from {', '.join(SEGMENT_MODES)}")
    if mode == "lines":
        return _segment_lines(line_source, None, max_finding_chars)
    if mode == "requirements":
        return _segment_requirements(line_source, max_finding_chars)
    secs = _heading_sections_from(line_source)
    keep = {i for i, x in enumerate(secs) if x["has_evidence"] and i}
    findings = _segment_lines(line_source, keep, max_finding_chars)
    merged, by_sec = [], {}
    for f in findings:
        sec = f.pop("sec")
        if not secs[sec]["has_evidence"] or sec == 0:
            merged.append(f)
            continue
        if sec in by_sec:
            g = by_sec[sec]
            g["text"] = _cap(g["text"] + "\n" + f["text"], max_finding_chars, g)
            g["spec_lines"] = [g["spec_lines"][0], f["spec_lines"][1]]
        else:
            g = dict(f, merged=True, kind="entry")
            g["spec_lines"] = list(f["spec_lines"])
            g["text"] = _cap(secs[sec]["line"] + "\n" + f["text"], max_finding_chars, g)
            by_sec[sec] = g
            merged.append(g)
    return _finalize(merged)


def _cap(text: str, limit: int, holder: dict) -> str:
    """Bound retained text without losing the ends, where citations live.

    A spec entry puts its ``Evidence:`` line last; a plain truncation would drop
    it and the finding would look uncited. So keep a head and a tail.
    """
    if limit and len(text) > limit:
        holder["text_truncated_chars"] = len(text) - limit
        tail = min(2000, max(0, limit // 4))
        head = limit - tail
        if tail == 0:
            return text[:limit]
        return text[:head] + f"\n[... {len(text) - limit} chars omitted ...]\n" + text[-tail:]
    return text


def _finalize(findings: list[dict]) -> list[dict]:
    for i, f in enumerate(findings, 1):
        f["id"] = f"F{i}"
        f.setdefault("kind", "fragment")
        f.setdefault("req_id", None)
    return findings


def _segment_lines(line_source, keep_secs: set | None, max_finding_chars: int) -> list[dict]:
    findings: list[dict] = []
    heading, ctx_file = "", None
    cur: dict | None = None
    in_code = False
    sec = 0
    lineno = 0

    def new(text: str) -> dict:
        return {"parts": [text], "context_file": ctx_file, "heading": heading, "sec": sec,
                "spec_lines": [lineno, lineno]}

    def flush():
        nonlocal cur
        if cur is not None:
            text = "\n".join(cur.pop("parts"))
            if len(re.findall(r"[A-Za-z]{3,}", text)) >= 4 or (keep_secs and cur.get("sec") in keep_secs):
                cur["text"] = _cap(text, max_finding_chars, cur)
                findings.append(cur)
        cur = None

    for raw in line_source():
        lineno += 1
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            if cur is not None:
                cur["parts"].append(line)
                cur["spec_lines"][1] = lineno
            continue
        if in_code:
            if cur is None:
                cur = new(f"[{heading}]")
            cur["parts"].append(line)
            cur["spec_lines"][1] = lineno
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            sec += 1
            heading = m.group(2).strip()
            fm = FILE_ONLY_RE.search(heading)
            if fm:
                ctx_file = fm.group("file")
            continue
        if not line.strip():
            flush()
            continue
        if re.match(r"^\s*\|?\s*:?-{3,}", line):  # table separator
            continue
        is_item = re.match(r"^\s*([-*+]|\d+[.)])\s+", line)
        is_row = line.lstrip().startswith("|")
        indent = len(line) - len(line.lstrip())
        if is_row:
            flush()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(len(c) < 25 for c in cells) and not any(re.search(r"\d", c) for c in cells):
                continue  # header row
            cur = new(" | ".join(cells))
            cur["is_table_row"] = True
            cur["cells"] = cells
            flush()
            continue
        if is_item and (indent < 2 or cur is None):
            flush()
            cur = new(line.strip())
        elif is_item and cur is not None:
            cur["parts"].append(line.strip())
            cur["spec_lines"][1] = lineno
        else:
            if cur is None:
                cur = new(line.strip())
            else:
                cur["parts"].append(line.strip())
                cur["spec_lines"][1] = lineno
    flush()
    if keep_secs is None:
        for f in findings:
            f.pop("sec", None)
        return _finalize(findings)
    return findings


def _segment_requirements(line_source, max_finding_chars: int) -> list[dict]:
    """One finding per requirement id, with index-table rows folded in.

    Pass 1 collects the ids that are *defined* by a heading. Pass 2 builds one
    unit per definition (heading plus everything until the next heading at the
    same or shallower level) and attaches any table row or aside elsewhere in the
    document whose leading cell is that id -- so a 317-row requirements index
    does not turn into 317 extra findings and halve apparent precision.
    Prose that belongs to no requirement is kept, marked ``kind="narrative"``.
    """
    defined: dict[str, int] = {}
    order: list[str] = []
    in_code = False
    for raw in line_source():
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", raw.rstrip())
        if m:
            rid = _heading_req_id(m.group(2))
            if rid:
                defined[rid] = len(m.group(1))
                if rid not in order:
                    order.append(rid)
    if not defined:
        # No requirement ids: nothing to normalise to. Caller records the fallback.
        return _segment(line_source, "auto", max_finding_chars)

    units: list[dict] = []
    extras: dict[str, list[str]] = {}
    cur: dict | None = None
    cur_level = 0
    narrative: dict | None = None
    heading, ctx_file = "", None
    in_code = False
    lineno = 0

    def flush_narrative():
        nonlocal narrative
        if narrative is not None:
            text = "\n".join(narrative.pop("parts"))
            if len(re.findall(r"[A-Za-z]{3,}", text)) >= 4:
                narrative["text"] = _cap(text, max_finding_chars, narrative)
                units.append(narrative)
        narrative = None

    def close_unit():
        nonlocal cur
        if cur is not None:
            cur["_parts_done"] = True
            units.append(cur)
        cur = None

    for raw in line_source():
        lineno += 1
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            holder = cur if cur is not None else narrative
            if holder is not None:
                holder["parts"].append(line)
                holder["spec_lines"][1] = lineno
            continue
        if in_code:
            holder = cur if cur is not None else narrative
            if holder is not None:
                holder["parts"].append(line)
                holder["spec_lines"][1] = lineno
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            rid = _heading_req_id(title)
            flush_narrative()
            if rid:
                close_unit()
                cur_level = level
                cur = {"parts": [line], "context_file": ctx_file, "heading": title,
                       "kind": "requirement", "req_id": rid, "spec_lines": [lineno, lineno]}
            else:
                if cur is not None and level <= cur_level:
                    close_unit()
                heading = title
                fm = FILE_ONLY_RE.search(title)
                if fm:
                    ctx_file = fm.group("file")
            continue
        if not line.strip():
            flush_narrative()
            continue
        if re.match(r"^\s*\|?\s*:?-{3,}", line):
            continue
        # a table row keyed by a defined requirement id belongs to that requirement
        if line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rid = _cell_req_id(cells, defined)
            if rid and (cur is None or cur.get("req_id") != rid):
                extras.setdefault(rid, []).append(" | ".join(cells))
                continue
            if all(len(c) < 25 for c in cells) and not any(re.search(r"\d", c) for c in cells):
                continue
        if cur is not None:
            cur["parts"].append(line.strip())
            cur["spec_lines"][1] = lineno
            continue
        if narrative is None:
            narrative = {"parts": [line.strip()], "context_file": ctx_file, "heading": heading,
                         "kind": "narrative", "req_id": None, "spec_lines": [lineno, lineno]}
        else:
            narrative["parts"].append(line.strip())
            narrative["spec_lines"][1] = lineno
    flush_narrative()
    close_unit()

    units.sort(key=lambda u: (u["spec_lines"][0], u.get("req_id") or ""))
    for u in units:
        if "parts" in u:
            text = "\n".join(u.pop("parts"))
            extra = extras.get(u.get("req_id") or "", [])
            if extra:
                text += "\n" + "\n".join(extra)
                u["index_rows"] = len(extra)
            u["text"] = _cap(text, max_finding_chars, u)
        u.pop("_parts_done", None)
    # index rows for ids that were never defined by a heading: keep them visible
    for rid in sorted(set(extras) - {u.get("req_id") for u in units}):
        units.append({"text": "\n".join(extras[rid]), "context_file": None, "heading": "",
                      "kind": "index_only", "req_id": rid, "spec_lines": [0, 0]})
    return _finalize(units)


def _heading_req_id(title: str) -> str | None:
    """Requirement id defined by a heading: '#### [DT-0005] Title' or '#### DT-0005: Title'."""
    m = re.match(r"^\s*[\[(]?\s*(" + REQ_ID_RE.pattern.strip("\\b()") + r")\s*[\])]?\s*[:.\-—]?\s+\S",
                 title)
    if m:
        return m.group(1)
    m2 = re.match(r"^\s*[\[(]\s*(" + REQ_ID_RE.pattern.strip("\\b()") + r")\s*[\])]\s*$", title)
    return m2.group(1) if m2 else None


def _cell_req_id(cells, defined) -> str | None:
    """Requirement id in a table row's leading cell.

    Any id-shaped token counts, not just a defined one: an index row for an id
    that was never written up is a structural defect worth surfacing, and folding
    it in here is what makes ``index_entries_without_definition`` observable.
    """
    if not cells:
        return None
    first = cells[0].strip().strip("`*[]()")
    return first if REQ_ID_RE.fullmatch(first) else None


def segment_json(obj) -> list[dict]:
    findings = []

    def walk(o, ctx=None):
        if isinstance(o, dict):
            stmt = None
            for k in ("statement", "description", "rule", "text", "summary", "name"):
                if isinstance(o.get(k), str) and len(o[k]) > 15:
                    stmt = o[k]
                    break
            if stmt:
                extra = []
                for k in ("citations", "citation", "evidence", "source", "location", "file", "lines", "line_range"):
                    if k in o:
                        extra.append(json.dumps(o[k]) if not isinstance(o[k], str) else o[k])
                title = o.get("title") or o.get("id") or ""
                rid = str(o["id"]) if isinstance(o.get("id"), str) and REQ_ID_RE.fullmatch(str(o["id"])) else None
                findings.append({"text": f"{title}: {stmt} " + " ".join(extra),
                                 "context_file": o.get("file") if isinstance(o.get("file"), str) else ctx,
                                 "heading": str(title), "kind": "requirement" if rid else "fragment",
                                 "req_id": rid, "spec_lines": [0, 0]})
                return
            for v in o.values():
                walk(v, o.get("file") if isinstance(o.get("file"), str) else ctx)
        elif isinstance(o, list):
            for v in o:
                walk(v, ctx)

    walk(obj)
    return _finalize(findings)


def load_findings(spec: Path, mode: str = "auto",
                  max_finding_chars: int = DEFAULT_MAX_FINDING_CHARS) -> list[dict]:
    spec = Path(spec)
    if spec.suffix == ".json":
        return segment_json(json.loads(spec.read_text()))
    return segment_spec(spec, mode, max_finding_chars)


# =================================================================== citations
CIT_OK = "ok"
CIT_OFF_BY_ONE = "off_by_one_end"
CIT_PHANTOM = "phantom_line_only"
CIT_OUT_OF_RANGE = "out_of_range"
CIT_NOT_FOUND = "file_not_found"
CIT_BAD_RANGE = "bad_range"
CIT_NO_FILE = "no_file_context"

CIT_CATEGORIES = (CIT_OK, CIT_OFF_BY_ONE, CIT_PHANTOM, CIT_OUT_OF_RANGE,
                  CIT_NOT_FOUND, CIT_BAD_RANGE, CIT_NO_FILE)


def classify_citation(resolved: str | None, start: int, end: int, n_lines: int | None,
                      *, had_file_name: bool = True) -> tuple[str, str]:
    """Exact verdict for one citation. Pure function; the property tests pin it.

    ``off_by_one_end`` is the pager artifact: a file of N lines that ends with a
    newline looks like N+1 lines to anything that splits on "\\n" without
    dropping the empty tail, so tools cite N+1. It is still wrong, but it is a
    different defect from citing line 900 of a 100-line file, and lumping the two
    together is what made citation validity look worse than it was.
    """
    if not had_file_name:
        return CIT_NO_FILE, "no file context"
    if resolved is None or n_lines is None:
        return CIT_NOT_FOUND, "file not found"
    if start < 1 or end < start:
        return CIT_BAD_RANGE, "bad range"
    if end <= n_lines:
        return CIT_OK, "ok"
    if n_lines >= 1 and end == n_lines + 1:
        if start == end:
            return CIT_PHANTOM, f"phantom trailing line only (file has {n_lines} lines)"
        return CIT_OFF_BY_ONE, f"off-by-one at end of file (file has {n_lines} lines)"
    return CIT_OUT_OF_RANGE, f"out of range (file has {n_lines} lines)"


def extract_citations(f: dict, files: dict[str, Path], counts: dict[str, int],
                      default_file: str | None, extra_patterns: list | None = None) -> list[dict]:
    """Parse and verify every citation in one finding, in text order."""
    text = f.get("full_text", f["text"])
    cits, spans = [], []
    for pat in list(extra_patterns or []) + CIT_PATTERNS + [REVERSE_CIT_RE]:
        for m in pat.finditer(text):
            if any(s < m.end() and m.start() < e for s, e in spans):
                continue
            a = int(m.group("a"))
            b = int(m.group("b")) if m.group("b") else a
            cits.append({"raw": m.group(0).strip(), "file": m.group("file"), "start": a, "end": b})
            spans.append(m.span())
    if cits:  # keep text order (extra/literal patterns run first)
        order = sorted(range(len(cits)), key=lambda i: spans[i][0])
        cits, spans = [cits[i] for i in order], [spans[i] for i in order]
    # extra ranges after a file citation: "main.cob:L10-L12, L20-L22"
    ctx = f.get("context_file") or default_file
    last_file = cits[-1]["file"] if cits else ctx
    for m in BARE_LINE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        prev = [c for c, s in zip(cits, spans) if s[1] <= m.start()]
        fname = prev[-1]["file"] if prev else (ctx or last_file)
        a = int(m.group("a"))
        b = int(m.group("b")) if m.group("b") else a
        cits.append({"raw": m.group(0).strip(), "file": fname, "start": a, "end": b,
                     "inherited_file": True})
    for c in cits:
        r = resolve_file(c["file"], files) if c["file"] else None
        c["resolved"] = r
        n = counts.get(r) if r is not None else None
        cat, reason = classify_citation(r, c["start"], c["end"], n,
                                       had_file_name=bool(c["file"]))
        c["category"] = cat
        c["reason"] = reason
        c["valid"] = cat == CIT_OK
        c["off_by_one"] = cat in (CIT_OFF_BY_ONE, CIT_PHANTOM)
        c["file_lines"] = n
    return cits


def parse_key_cit(s: str):
    m = re.match(r"(.+?):(\d+)(?:-(\d+))?$", s)
    if not m:
        raise SystemExit(f"answer key citation {s!r} is not 'file:start[-end]'")
    a = int(m.group(2))
    return m.group(1), a, int(m.group(3) or a)


def same_file(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    a, b = a.replace("\\", "/"), b.replace("\\", "/")
    return a == b or a.split("/")[-1].lower() == b.split("/")[-1].lower()


def overlap(c: dict, key_cits: list) -> bool:
    """True when a *valid* citation's line range intersects any of ``key_cits``."""
    if not c.get("valid"):
        return False
    for kf, a, b in key_cits:
        if same_file(c.get("resolved"), kf) and c["start"] <= b and a <= c["end"]:
            return True
    return False


def overlapping_key_ids(c: dict, kc: dict) -> list[str]:
    return sorted(kid for kid, cits in kc.items() if overlap(c, cits))


# ============================================================== source coverage
def source_coverage(findings, files, counts, kc, items) -> dict:
    """Which key locations the spec cites, and which source lines it never touches."""
    cited_by_file: dict[str, list[tuple[int, int]]] = {}
    cits_per_file: dict[str, int] = {}
    for f in findings:
        for c in f["citations"]:
            if c["valid"]:
                cited_by_file.setdefault(c["resolved"], []).append((c["start"], c["end"]))
                cits_per_file[c["resolved"]] = cits_per_file.get(c["resolved"], 0) + 1

    from judge.evidence import covered_lines, merge_intervals

    per_file = []
    for rel in sorted(files):
        n = counts[rel]
        ivs = merge_intervals(cited_by_file.get(rel, []))
        covered = covered_lines(ivs)
        uncovered = _complement(ivs, n)
        per_file.append({
            "file": rel, "lines": n, "citations": cits_per_file.get(rel, 0),
            "lines_cited": covered,
            "coverage_pct": round(100.0 * covered / n, 1) if n else None,
            "citations_per_100_lines": round(100.0 * cits_per_file.get(rel, 0) / n, 2) if n else None,
            "uncited_ranges": [list(x) for x in uncovered[:40]],
            "uncited_ranges_truncated": max(0, len(uncovered) - 40),
        })
    never = [p["file"] for p in per_file if p["citations"] == 0]

    loc_rows, covered_items = [], 0
    for k in items:
        locs = kc.get(str(k["id"]), [])
        row = {"key_id": str(k["id"]), "locations": [], "covered": 0, "total": len(locs)}
        for kf, a, b in locs:
            hit = []
            for f in findings:
                if any(c["valid"] and same_file(c["resolved"], kf) and c["start"] <= b and a <= c["end"]
                       for c in f["citations"]):
                    hit.append(f["id"])
            row["locations"].append({"file": kf, "start": a, "end": b,
                                     "cited_by": hit[:10], "cited_by_count": len(hit)})
            if hit:
                row["covered"] += 1
        row["fully_covered"] = row["total"] > 0 and row["covered"] == row["total"]
        if row["covered"]:
            covered_items += 1
        loc_rows.append(row)

    n_locs = sum(r["total"] for r in loc_rows)
    n_cov = sum(r["covered"] for r in loc_rows)
    total_lines = sum(counts[r] for r in files) or 0
    total_cited = sum(p["lines_cited"] for p in per_file)
    return {
        "source_lines": total_lines,
        "source_lines_cited": total_cited,
        "source_line_coverage_pct": round(100.0 * total_cited / total_lines, 1) if total_lines else None,
        "files_never_cited": never,
        "files_never_cited_n": len(never),
        "per_file": per_file,
        "key_locations": {"total": n_locs, "cited": n_cov,
                          "pct": round(100.0 * n_cov / n_locs, 1) if n_locs else None,
                          "items_with_any_location_cited": covered_items,
                          "items_fully_cited": sum(1 for r in loc_rows if r["fully_covered"])},
        "key_location_detail": loc_rows,
    }


def _complement(merged, n_lines: int) -> list[tuple[int, int]]:
    out, prev = [], 0
    for a, b in merged:
        if a > prev + 1:
            out.append((prev + 1, a - 1))
        prev = max(prev, b)
    if prev < n_lines:
        out.append((prev + 1, n_lines))
    return out


# ============================================================ structural checks
def structural_checks(spec: Path, findings, key_ids: set[str]) -> dict:
    """Deterministic defects in the spec document itself."""
    defined: dict[str, list[int]] = {}
    mentions: dict[str, int] = {}
    level_jumps, empty_sections, dup_headings = [], [], []
    seen_headings: dict[str, int] = {}
    in_code, fence_opens = False, 0
    prev_level, prev_heading_line = 0, None
    lineno = 0
    is_json = Path(spec).suffix == ".json"

    lines = (iter_spec_lines(spec) if not is_json
             else iter(json.dumps(json.loads(Path(spec).read_text()), indent=1).splitlines()))
    for raw in lines:
        lineno += 1
        if raw.strip().startswith("```"):
            in_code = not in_code
            fence_opens += 1
            continue
        if in_code:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", raw.rstrip())
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            if prev_heading_line is not None and prev_heading_line == lineno - 1:
                empty_sections.append(prev_heading_line)
            if prev_level and level > prev_level + 1:
                level_jumps.append({"line": lineno, "from": prev_level, "to": level})
            if title in seen_headings:
                dup_headings.append({"title": title[:120], "lines": [seen_headings[title], lineno]})
            else:
                seen_headings[title] = lineno
            prev_level, prev_heading_line = level, lineno
            rid = _heading_req_id(title)
            if rid:
                defined.setdefault(rid, []).append(lineno)
            for rid2 in REQ_ID_RE.findall(title):
                mentions[rid2] = mentions.get(rid2, 0) + 1
            continue
        prev_heading_line = None
        for rid in REQ_ID_RE.findall(raw):
            mentions[rid] = mentions.get(rid, 0) + 1

    for f in findings:  # JSON specs define ids on objects, not headings
        if is_json and f.get("req_id"):
            defined.setdefault(f["req_id"], []).append(0)

    duplicate_ids = sorted((rid, ls) for rid, ls in defined.items() if len(ls) > 1)
    unresolved = sorted((rid, n) for rid, n in mentions.items()
                        if rid not in defined and rid not in key_ids)
    key_mentions = sorted((rid, n) for rid, n in mentions.items() if rid in key_ids)

    req_units = [f for f in findings if f.get("kind") == "requirement"]
    req_no_cit = [f["req_id"] or f["id"] for f in req_units if not f["citations"]]
    # "Orphan" only means something when the segmentation actually produced requirement
    # units (--segment requirements, or a JSON spec with ids). Under --segment auto no
    # finding is a requirement, so counting every citation as an orphan would be a
    # measurement artifact, not a defect in the spec.
    requirement_level = bool(req_units)
    orphan = [f["id"] for f in findings
              if requirement_level and f.get("kind") != "requirement" and f["citations"]]
    orphan_n = (sum(len(f["citations"]) for f in findings if f.get("kind") != "requirement")
                if requirement_level else None)
    thin = [f["req_id"] for f in req_units
            if len(re.findall(r"[A-Za-z]{3,}", f["text"])) < 10]
    index_only = [f["req_id"] for f in findings if f.get("kind") == "index_only"]

    return {
        "requirement_ids_defined": len(defined),
        "duplicate_requirement_ids": [{"id": rid, "defined_at_lines": ls} for rid, ls in duplicate_ids],
        "duplicate_requirement_ids_n": len(duplicate_ids),
        "unresolved_cross_references": [{"id": rid, "mentions": n} for rid, n in unresolved[:100]],
        "unresolved_cross_references_n": len(unresolved),
        "answer_key_ids_mentioned": [{"id": rid, "mentions": n} for rid, n in key_mentions[:50]],
        "requirements_without_citation": req_no_cit[:200],
        "requirements_without_citation_n": len(req_no_cit),
        "requirement_units": len(req_units),
        "orphan_citations": orphan_n,
        "orphan_citations_applicable": requirement_level,
        "orphan_citation_findings": orphan[:100],
        "index_entries_without_definition": index_only[:100],
        "index_entries_without_definition_n": len(index_only),
        "thin_requirements": thin[:100],
        "thin_requirements_n": len(thin),
        "malformed": {
            "heading_level_jumps": level_jumps[:50],
            "heading_level_jumps_n": len(level_jumps),
            "empty_sections": empty_sections[:50],
            "empty_sections_n": len(empty_sections),
            "duplicate_headings_n": len(dup_headings),
            "duplicate_headings": dup_headings[:20],
            "unclosed_code_fence": fence_opens % 2 == 1,
        },
    }


# ============================================================ deterministic match
def tokens(s: str) -> set[str]:
    s = s.lower().replace("9(6)v99", " pic96v99 ")
    toks = re.findall(r"[a-z][a-z0-9-]+|\d+(?:\.\d+)?", s)
    out = set()
    for t in toks:
        if t in STOP or len(t) < 3 and not t.isdigit():
            continue
        t = re.sub(r"(ing|ed|es|s)$", "", t) if len(t) > 5 else t
        out.add(t)
    return out


#: Lexical containment needed on its own, with no citation evidence.
LEXICAL_THRESH = 0.45
#: Lower bar when a citation also lands inside the key item's own lines.
LEXICAL_THRESH_WITH_CITATION = 0.25


def match_deterministic(findings, key_items, thresh: float = LEXICAL_THRESH,
                        thresh_cited: float = LEXICAL_THRESH_WITH_CITATION):
    """Lexical containment plus citation overlap. A SMOKE TEST, not a measurement.

    Shared vocabulary alone used to be enough to "match" (threshold 0.22 with a
    flat citation bonus), which inflated recall on any spec that reused the key's
    nouns. The bar is now 0.45 on its own, or 0.25 with a citation that actually
    lands inside the key item's lines. It is still lexical, so it cannot tell a
    rule from its negation; never publish a recall number from this path.
    """
    ktoks = {str(k["id"]): tokens(k["statement"]) for k in key_items}
    kc = {str(k["id"]): [parse_key_cit(c) for c in k.get("citations", [])] for k in key_items}
    result = {}
    for f in findings:
        ft = tokens(f["text"])
        scored = []
        for k in key_items:
            kid = str(k["id"])
            kt = ktoks[kid]
            if not kt:
                continue
            contain = len(kt & ft) / len(kt)
            cited = any(overlap(c, kc[kid]) for c in f["citations"])
            bar = thresh_cited if cited else thresh
            if contain >= bar:
                scored.append((contain + (0.15 if cited else 0.0), contain, kid))
        scored.sort(reverse=True)
        matched = [kid for _, _, kid in scored[:3]]
        result[f["id"]] = {
            "matches": [{"key_id": m, "degree": "full" if i == 0 else "partial"}
                        for i, m in enumerate(matched)],
            "verdict": "matched" if matched else "unmatched",
            "note": f"top containment {scored[0][1]:.2f}" if scored else "no key item above threshold",
        }
    return result


# ====================================================================== metrics
def _pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def key_coverage_from(findings, judged, items) -> dict[str, str]:
    """full > partial > missed, per key item, from one set of judgements."""
    best: dict[str, str] = {}
    for f in findings:
        j = judged.get(f["id"]) or {}
        for m in j.get("matches", []) or []:
            kid = m.get("key_id")
            if kid is None:
                continue
            if m.get("degree") == "full" or best.get(kid) is None:
                best[kid] = "full" if (m.get("degree") == "full" or best.get(kid) == "full") else "partial"
    return {str(k["id"]): best.get(str(k["id"]), "missed") for k in items}


def compute_metrics(findings, judged, items, kc) -> dict:
    """All ratio metrics for one set of judgements. Raises if a percentage
    exceeds 100 -- the symptom of the old double-counting bug that published
    'precision 104.8%'."""
    cov = key_coverage_from(findings, judged, items)
    n_items = len(items)
    full = sum(1 for k in items if cov[str(k["id"])] == "full")
    partial = sum(1 for k in items if cov[str(k["id"])] == "partial")
    core = [k for k in items if k.get("core")]
    core_full = sum(1 for k in core if cov[str(k["id"])] == "full")
    core_any = sum(1 for k in core if cov[str(k["id"])] != "missed")

    nf = len(findings)
    verdicts, matched_flags = [], []
    for f in findings:
        j = judged.get(f["id"]) or {}
        verdicts.append(j.get("verdict", "unjudged"))
        matched_flags.append(bool(j.get("matches")))
    n_matched = sum(matched_flags)
    n_extra = sum(1 for v, mf in zip(verdicts, matched_flags) if v == "correct_extra" and not mf)
    n_incorrect = verdicts.count("incorrect")
    n_unsupported = verdicts.count("unsupported")
    n_uncertain = verdicts.count("uncertain")
    n_unjudged = verdicts.count("unjudged")
    decided = nf - n_uncertain - n_unjudged

    acc_num = acc_den = 0
    for f in findings:
        for m in (judged.get(f["id"]) or {}).get("matches", []) or []:
            acc_den += 1
            if any(overlap(c, kc.get(m["key_id"], [])) for c in f["citations"]):
                acc_num += 1

    out = {
        "key_coverage": cov,
        "recall": {
            "strict_full_pct": _pct(full, n_items),
            "partial_credit_pct": _pct(full + 0.5 * partial, n_items),
            "any_match_pct": _pct(full + partial, n_items),
            "core_full_pct": _pct(core_full, len(core)),
            "core_any_pct": _pct(core_any, len(core)),
            "full": full, "partial": partial, "missed": n_items - full - partial,
        },
        "precision": {
            "keyed_pct": _pct(n_matched, nf),
            "correct_pct": _pct(n_matched + n_extra, nf),
            "keyed_pct_of_decided": _pct(n_matched, decided),
            "findings_matched": n_matched, "correct_extra": n_extra,
            "unsupported": n_unsupported, "incorrect": n_incorrect,
            "uncertain": n_uncertain, "unjudged": n_unjudged, "decided": decided,
        },
        "citation_accuracy_pct": _pct(acc_num, acc_den),
        "citation_accuracy_num": acc_num, "citation_accuracy_den": acc_den,
    }
    assert_percentages_sane(out)
    return out


def assert_percentages_sane(metrics: dict) -> None:
    """No ratio metric may exceed 100%.

    The published 'precision, true of source = 104.8%' came from counting a
    finding once as matched and again as a correct extra. The guard stays even
    though that path is fixed: a scorer that can print an impossible number is a
    scorer whose other numbers cannot be trusted either.
    """
    for group in ("recall", "precision"):
        for name, v in (metrics.get(group) or {}).items():
            if name.endswith("_pct") and v is not None and v > 100.0:
                raise RuntimeError(
                    f"{group}.{name} = {v}% > 100%: double counting bug, refusing to publish")


def recall_breakdown(items, cov, keyfn) -> dict:
    groups: dict[str, list] = {}
    for k in items:
        for g in keyfn(k):
            groups.setdefault(str(g), []).append(k)
    out = {}
    for g, ks in sorted(groups.items()):
        f_ = sum(1 for k in ks if cov[str(k["id"])] == "full")
        p_ = sum(1 for k in ks if cov[str(k["id"])] == "partial")
        out[g] = {"n": len(ks), "full": f_, "partial": p_, "missed": len(ks) - f_ - p_,
                  "full_pct": _pct(f_, len(ks)), "any_pct": _pct(f_ + p_, len(ks))}
    return out


# ============================================================ golden calibration
def load_golden(path: Path) -> dict:
    text = Path(path).read_text()
    data = yaml.safe_load(text) if Path(path).suffix in (".yaml", ".yml") else json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("key_items"), list):
        raise SystemExit(f"{path}: golden file needs a 'key_items' list")
    allowed = {"full", "partial", "missed"}
    for row in data["key_items"]:
        if row.get("expected_coverage") not in allowed:
            raise SystemExit(f"{path}: item {row.get('id')!r} expected_coverage must be one of "
                             f"{sorted(allowed)}")
    # PyYAML turns a bare 2026-10-06 into datetime.date, which json.dumps refuses.
    for k in ("labelled_on", "spec_sha256", "labelled_by", "spec", "key", "corpus", "method"):
        if k in data and data[k] is not None:
            data[k] = str(data[k])
    return data


def golden_agreement(golden: dict, cov: dict[str, str], items) -> dict:
    """Judge-vs-human agreement on the hand-labelled subset.

    This is the only number in the report that measures *judge error* rather than
    assuming it. Cohen's kappa treats the human labels as one rater and the
    judge's majority vote as the other.
    """
    from judge.stats import cohen_kappa

    known = {str(k["id"]) for k in items}
    rows, human, machine = [], [], []
    for row in golden["key_items"]:
        kid = str(row["id"])
        if kid not in known:
            rows.append({"key_id": kid, "expected": row["expected_coverage"],
                         "observed": None, "agree": None, "note": "id not in answer key"})
            continue
        obs = cov.get(kid, "missed")
        rows.append({"key_id": kid, "expected": row["expected_coverage"], "observed": obs,
                     "agree": obs == row["expected_coverage"],
                     "basis": row.get("basis", "")[:300]})
        human.append(row["expected_coverage"])
        machine.append(obs)
    n = len(human)
    agree = sum(1 for h, m in zip(human, machine) if h == m)
    confusion: dict[str, int] = {}
    for h, m in zip(human, machine):
        confusion[f"{h}->{m}"] = confusion.get(f"{h}->{m}", 0) + 1
    strict = sum(1 for h, m in zip(human, machine)
                 if (h == "missed") == (m == "missed"))
    return {
        "source": golden.get("_path"),
        "labelled_by": golden.get("labelled_by"),
        "labelled_on": golden.get("labelled_on"),
        "spec": golden.get("spec"),
        "n_labelled": n,
        "agreement_pct": _pct(agree, n),
        "found_vs_missed_agreement_pct": _pct(strict, n),
        "cohen_kappa": cohen_kappa(human, machine, ["full", "partial", "missed"]),
        "confusion": dict(sorted(confusion.items())),
        "items": rows,
    }


# ======================================================================== scoring
def number_source(files: dict[str, Path], max_chars=80000) -> str:
    """Legacy whole-tree numbered dump. Still used by --judge-evidence full and by
    eval/benefits/*. Numbering uses the exact splitter, so the numbers the judge
    sees are the numbers the citation checker verified."""
    from judge.evidence import full_dump
    text, _ = full_dump(files, max_chars, split_lines_exact)
    return text


def score(spec: Path, key_path: Path, src_root: Path, use_llm: bool, gen_log: Path | None,
          gen_tag_prefix: str | None, exclude: list[str] | None = None, segment: str = "auto",
          max_source_chars: int = 80000, judge_tag: str | None = None,
          reuse_judgement: Path | None = None, *,
          judge_runs: int = 1, judge_batch: int = 40, judge_evidence: str = "full",
          max_evidence_chars: int = 40000, evidence_context: int = 8,
          evidence_key_items: int = 12, cache_enabled: bool = True,
          cache_dir: Path | None = None, max_judge_cost_usd: float | None = None,
          dry_run_judge: bool = False, prompt_dir: Path | None = None,
          golden: Path | None = None, compare_to: Path | None = None,
          bootstrap_iters: int = 2000, seed: int = 20261006,
          max_finding_chars: int = DEFAULT_MAX_FINDING_CHARS,
          max_report_findings: int = DEFAULT_MAX_REPORT_FINDINGS,
          progress=None):
    """Score ``spec`` against ``key_path``.

    The first eleven parameters keep their historical positional order and
    defaults so that ``eval/benefits/insights.py`` and anything else importing
    this module behaves exactly as before. Everything after ``*`` is new and
    keyword-only; the CLI opts into the stronger defaults.
    """
    say = progress if progress is not None else (lambda *a, **k: print(*a, **k))
    spec, key_path, src_root = Path(spec), Path(key_path), Path(src_root)
    key = load_key(key_path)
    items = key["items"]
    key_ids = {str(k["id"]) for k in items}
    files = source_files(src_root, exclude)
    counts = {k: line_count(p) for k, p in files.items()}
    default_file = next(iter(files)) if len(files) == 1 else None
    extra_pats = literal_file_patterns(files)
    kc = {str(k["id"]): [parse_key_cit(c) for c in k.get("citations", [])] for k in items}

    # ------------------------------------------------- A1-A4: deterministic layer
    requested_segment = segment
    findings = load_findings(spec, segment, max_finding_chars)
    effective_segment = segment
    if segment == "requirements" and not any(f.get("kind") == "requirement" for f in findings):
        effective_segment = "auto (requested 'requirements'; the spec defines no requirement ids)"
    for f in findings:
        f["citations"] = extract_citations(f, files, counts, default_file, extra_pats)
        for c in f["citations"]:
            c["overlaps_key_items"] = overlapping_key_ids(c, kc)

    all_cits = [c for f in findings for c in f["citations"]]
    by_cat = {cat: sum(1 for c in all_cits if c["category"] == cat) for cat in CIT_CATEGORIES}
    valid = by_cat[CIT_OK]
    off_by_one = by_cat[CIT_OFF_BY_ONE] + by_cat[CIT_PHANTOM]
    uncited = sum(1 for f in findings if not f["citations"])
    coverage = source_coverage(findings, files, counts, kc, items)
    structural = structural_checks(spec, findings, key_ids)

    kinds: dict[str, int] = {}
    for f in findings:
        kinds[f.get("kind", "fragment")] = kinds.get(f.get("kind", "fragment"), 0) + 1
    unit_chars = sorted(len(f["text"]) for f in findings)
    from judge.stats import median as _median
    segmentation = {
        "strategy_requested": requested_segment,
        "strategy": effective_segment,
        "strategy_doc": SEGMENT_DOC.get(requested_segment, ""),
        "findings": len(findings),
        "by_kind": dict(sorted(kinds.items())),
        "merged_entry_findings": sum(1 for f in findings if f.get("merged")),
        "requirement_ids_in_spec": structural["requirement_ids_defined"],
        "finding_chars": {"min": unit_chars[0] if unit_chars else 0,
                          "median": _median(unit_chars),
                          "max": unit_chars[-1] if unit_chars else 0,
                          "total": sum(unit_chars)},
        "spec_bytes": spec.stat().st_size,
        "findings_truncated": sum(1 for f in findings if f.get("text_truncated_chars")),
    }
    comparability = None
    if compare_to:
        comparability = _comparability(len(findings), Path(compare_to), effective_segment)
        segmentation["comparability"] = comparability

    # --------------------------------------------------------- B: judged layer
    judge_block = None
    judge_usage = None
    per_run_judged: list[dict] = []
    if reuse_judgement:
        prior = json.loads(Path(reuse_judgement).read_text())
        pf = {f["id"]: f for f in prior["findings"]}
        for f in findings:
            if f["id"] not in pf or pf[f["id"]]["text"] != f["text"][:600]:
                raise SystemExit(f"--reuse-judgement: finding {f['id']} differs from {reuse_judgement}")
        judged = {fid: v["judgement"] for fid, v in pf.items()}
        judge_usage = prior.get("judge_usage")
        method = prior["method"]
        judge_block = prior.get("judge")
    elif use_llm:
        from judge.cache import JudgeCache
        from judge.runner import judge_findings
        cache = JudgeCache(cache_dir or (Path(__file__).resolve().parent / "judge" / "cache"),
                           enabled=cache_enabled)
        res = judge_findings(
            findings, items, files=files, counts=counts, split=split_lines_exact,
            tokenize=tokens, key_cits=kc, runs=judge_runs, batch=judge_batch,
            evidence_mode=judge_evidence, max_evidence_chars=max_evidence_chars,
            evidence_context=evidence_context, max_source_chars=max_source_chars,
            evidence_key_items=evidence_key_items, cache=cache,
            tag=judge_tag or f"judge:{spec.parent.name}", model=None,
            max_cost_usd=max_judge_cost_usd, dry_run=dry_run_judge,
            prompt_dir=prompt_dir, progress=say)
        if res.get("dry_run"):
            return {"dry_run": True, "judge_plan": res["plan"], "segmentation": segmentation,
                    "spec": str(spec), "key": str(key_path)}, items
        judged = res["consensus"]
        per_run_judged = res["runs"]
        judge_usage = res["usage"]
        method = "llm"
        judge_block = _judge_report(res, findings, per_run_judged, judged, items, kc,
                                    bootstrap_iters, seed)
    else:
        judged = match_deterministic(findings, items)
        method = "deterministic"

    m = compute_metrics(findings, judged, items, kc)
    cov = m["key_coverage"]
    for f in findings:
        f["judgement"] = judged.get(f["id"], {"matches": [], "verdict": "unjudged"})

    if golden:
        g = load_golden(golden)
        g["_path"] = str(golden)
        if judge_block is None:
            judge_block = {}
        judge_block["golden"] = golden_agreement(g, cov, items)

    possible_inventions = []
    for f in findings:
        bad = [c for c in f["citations"] if not c["valid"]]
        v = f["judgement"]["verdict"]
        if v == "incorrect" \
                or (not f["judgement"]["matches"] and v not in ("correct_extra", "uncertain") and bad) \
                or (method == "deterministic" and v == "unmatched"):
            possible_inventions.append({
                "finding_id": f["id"], "verdict": v, "note": f["judgement"].get("note", ""),
                "invalid_citations": [c["raw"] for c in bad], "text": f["text"][:300]})

    gen_usage = None
    if gen_log and Path(gen_log).exists():
        from bedrock_util import summarize_log
        gen_usage = summarize_log(Path(gen_log), gen_tag_prefix)

    report = {
        "scorer_version": SCORER_VERSION,
        "spec": str(spec), "key": str(key_path), "source_root": str(src_root), "method": method,
        "counts": {"key_items": len(items), "core_items": sum(1 for k in items if k.get("core")),
                   "findings": len(findings), "citations": len(all_cits)},
        "recall": {k: v for k, v in m["recall"].items()},
        "precision": {k: v for k, v in m["precision"].items()},
        "citations": {
            "validity_pct": _pct(valid, len(all_cits)),
            "valid": valid, "invalid": len(all_cits) - valid,
            "off_by_one": off_by_one,
            "validity_pct_allowing_off_by_one": _pct(valid + off_by_one, len(all_cits)),
            "by_category": by_cat,
            "accuracy_pct": m["citation_accuracy_pct"],
            "accuracy_num": m["citation_accuracy_num"], "accuracy_den": m["citation_accuracy_den"],
            "findings_without_citation": uncited,
            "supported_formats": CITATION_FORMATS,
        },
        "source_coverage": coverage,
        "structural": structural,
        "possible_inventions": possible_inventions,
        "key_coverage": cov,
        "recall_by": {
            "type": recall_breakdown(items, cov, lambda k: [k.get("type", "?")]),
            "evidence": recall_breakdown(items, cov, lambda k: [k.get("evidence", "?")]),
            "flag": recall_breakdown(items, cov, lambda k: k.get("flags") or ["(none)"]),
        },
        "segmentation": segmentation,
        "missed_items": [{"id": str(k["id"]), "statement": " ".join(k["statement"].split())}
                         for k in items if cov[str(k["id"])] == "missed"],
        "judge": judge_block,
        "judge_usage": judge_usage,
        "generation_usage": gen_usage,
        "findings": [{"id": f["id"], "text": f["text"][:600], "kind": f.get("kind"),
                      "req_id": f.get("req_id"), "spec_lines": f.get("spec_lines"),
                      "citations": [{k: c.get(k) for k in
                                     ("raw", "resolved", "start", "end", "valid", "category",
                                      "reason", "overlaps_key_items")}
                                    for c in f["citations"]],
                      "judgement": f["judgement"]} for f in findings[:max_report_findings]],
        "findings_reported": min(len(findings), max_report_findings),
        "findings_total": len(findings),
    }
    return report, items


SCORER_VERSION = "2026-10-06.hardened-1"


def _comparability(n_findings: int, other_path: Path, strategy: str) -> dict:
    try:
        other = json.loads(other_path.read_text())
    except Exception as exc:
        raise SystemExit(f"--compare-to {other_path}: cannot read ({exc})")
    other_n = (other.get("counts") or {}).get("findings") or (other.get("segmentation") or {}).get("findings")
    other_strategy = (other.get("segmentation") or {}).get("strategy")
    if not other_n:
        raise SystemExit(f"--compare-to {other_path}: no finding count in that report")
    hi, lo = max(n_findings, other_n), min(n_findings, other_n)
    ratio = round(hi / lo, 2) if lo else None
    warning = None
    if ratio and ratio > COMPARABILITY_RATIO:
        warning = (f"finding counts differ by {ratio}x ({n_findings} vs {other_n}): recall and "
                   f"precision are NOT comparable between these specs. More findings give the "
                   f"judge more chances to match. Re-score both with --segment requirements, or "
                   f"compare only counts of incorrect findings and citation metrics.")
    elif other_strategy and other_strategy != strategy:
        warning = (f"segmentation differs ('{strategy}' vs '{other_strategy}'); metrics are not "
                   f"like for like even though the counts are close.")
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    return {"other": str(other_path), "other_findings": other_n, "this_findings": n_findings,
            "ratio": ratio, "other_strategy": other_strategy, "this_strategy": strategy,
            "comparable": not bool(warning), "warning": warning}


def _judge_report(res, findings, per_run, consensus_judged, items, kc, iters, seed) -> dict:
    """Agreement, spread, CIs and kappa for the judged layer."""
    from judge import schema as sch
    from judge.stats import (bootstrap_proportion_ci, bootstrap_weighted_ci, fleiss_kappa,
                             kappa_label, mean_pairwise_cohen, spread)

    n_runs = len(per_run)
    per_run_metrics = [compute_metrics(findings, r, items, kc) for r in per_run]

    fids = [f["id"] for f in findings]
    verdict_ratings = [[(r.get(fid) or {}).get("verdict", sch.UNJUDGED) for r in per_run]
                       for fid in fids]
    cov_per_run = [mm["key_coverage"] for mm in per_run_metrics]
    cov_ratings = [[c[str(k["id"])] for c in cov_per_run] for k in items]

    cons_cov = key_coverage_from(findings, consensus_judged, items)
    full_flags = [cons_cov[str(k["id"])] == "full" for k in items]
    any_flags = [cons_cov[str(k["id"])] != "missed" for k in items]
    pc_weights = [1.0 if cons_cov[str(k["id"])] == "full"
                  else 0.5 if cons_cov[str(k["id"])] == "partial" else 0.0 for k in items]
    matched_flags = [bool((consensus_judged.get(f["id"]) or {}).get("matches")) for f in findings]

    agree = [(consensus_judged.get(fid) or {}).get("verdict_agreement") for fid in fids]
    agree = [a for a in agree if a is not None]
    unanimous = sum(1 for fid in fids if (consensus_judged.get(fid) or {}).get("unanimous_verdict"))
    no_majority = sum(1 for fid in fids if (consensus_judged.get(fid) or {}).get("no_majority"))
    uncertain = sum(1 for fid in fids
                    if (consensus_judged.get(fid) or {}).get("verdict") == "uncertain")

    k_verdict = fleiss_kappa(verdict_ratings, sch.ALL_VERDICT_LABELS)
    k_cov = fleiss_kappa(cov_ratings, ("full", "partial", "missed"))
    return {
        "plan": res["plan"],
        "runs": n_runs,
        "model": res["plan"]["model"],
        "schema_version": res["plan"]["schema_version"],
        "schema_hash": res["plan"]["schema_hash"],
        "temperature": res["plan"]["temperature"],
        "evidence_mode": res["plan"]["evidence_mode"],
        "evidence_truncated_batches": res["plan"]["evidence_truncated_batches"],
        "cache": res["cache"],
        "issues": res["issues"][:200],
        "issues_n": len(res["issues"]),
        "consensus_rule": "a verdict or key match needs strictly more than half the passes; "
                          "otherwise the finding is reported as 'uncertain' / no-majority",
        "determinism": {
            "temperature": res["plan"]["temperature"],
            "key_items_sorted": True, "findings_sorted": True,
            "pass_salt": "the prompt differs between passes only by 'Evaluation pass k of N', so "
                         "measured across-pass spread is a LOWER BOUND on judge noise",
        },
        "abstention": {"uncertain_findings": uncertain,
                       "uncertain_pct": _pct(uncertain, len(findings)),
                       "no_majority_findings": no_majority},
        "agreement": {
            "mean_per_finding": round(sum(agree) / len(agree), 3) if agree else None,
            "unanimous_findings": unanimous,
            "unanimous_pct": _pct(unanimous, len(findings)),
            "fleiss_kappa_verdicts": k_verdict,
            "fleiss_kappa_verdicts_label": kappa_label(k_verdict),
            "fleiss_kappa_key_coverage": k_cov,
            "fleiss_kappa_key_coverage_label": kappa_label(k_cov),
            "mean_pairwise_cohen_verdicts": mean_pairwise_cohen(verdict_ratings,
                                                               sch.ALL_VERDICT_LABELS),
            "mean_pairwise_cohen_key_coverage": mean_pairwise_cohen(cov_ratings,
                                                                   ("full", "partial", "missed")),
        },
        "across_runs": {
            "recall_strict_full_pct": spread([mm["recall"]["strict_full_pct"] for mm in per_run_metrics]),
            "recall_any_match_pct": spread([mm["recall"]["any_match_pct"] for mm in per_run_metrics]),
            "precision_keyed_pct": spread([mm["precision"]["keyed_pct"] for mm in per_run_metrics]),
            "incorrect": spread([float(mm["precision"]["incorrect"]) for mm in per_run_metrics]),
            "uncertain": spread([float(mm["precision"]["uncertain"]) for mm in per_run_metrics]),
        },
        "bootstrap_ci95": {
            "basis": "percentile bootstrap resampling KEY ITEMS (recall) or FINDINGS (precision) "
                     "from the majority-vote labels; it measures small-sample uncertainty, not "
                     "judge noise, and does not shrink with more judge passes",
            "iters": iters, "seed": seed,
            "recall_strict_full_pct": bootstrap_proportion_ci(full_flags, iters=iters, seed=seed),
            "recall_any_match_pct": bootstrap_proportion_ci(any_flags, iters=iters, seed=seed),
            "recall_partial_credit_pct": bootstrap_weighted_ci(pc_weights, iters=iters, seed=seed),
            "precision_keyed_pct": bootstrap_proportion_ci(matched_flags, iters=iters, seed=seed),
        },
        "per_run_metrics": [{"recall": mm["recall"], "precision": mm["precision"]}
                            for mm in per_run_metrics],
        "per_run_key_coverage": cov_per_run,
        "evidence": res["evidence"],
    }


# ======================================================================= markdown
def to_markdown(r: dict, items) -> str:
    if r.get("dry_run"):
        return ("# Judge plan (dry run)\n\n```json\n"
                + json.dumps(r["judge_plan"], indent=2) + "\n```\n")
    seg = r.get("segmentation") or {}
    jj = r.get("judge") or {}
    L = [f"# Score: `{r['spec']}`", "",
         f"- Key: `{r['key']}` ({r['counts']['key_items']} items, {r['counts']['core_items']} core)",
         f"- Matcher: **{r['method']}**"
         + (f" ({jj['runs']} judge passes, majority vote)" if jj.get("runs") else ""),
         f"- Segmentation: **{seg.get('strategy', '?')}** -> {r['counts']['findings']} findings"
         + (f" ({', '.join(f'{k} {v}' for k, v in (seg.get('by_kind') or {}).items())})" if seg.get("by_kind") else ""),
         f"- Citations parsed: {r['counts']['citations']}",
         f"- Scorer: `{r.get('scorer_version', '?')}`", ""]
    if (seg.get("comparability") or {}).get("warning"):
        L += [f"> **Not comparable.** {seg['comparability']['warning']}", ""]

    L += ["## Deterministic (exact)", "", "| Check | Value |", "|---|---|"]
    ci, sc, st = r["citations"], r["source_coverage"], r["structural"]
    L += [f"| Citations valid | {ci['valid']}/{ci['valid'] + ci['invalid']} ({ci['validity_pct']}%) |",
          f"| ... allowing the off-by-one phantom line | {ci['validity_pct_allowing_off_by_one']}% |",
          f"| Citation categories | {', '.join(f'{k}={v}' for k, v in ci['by_category'].items() if v)} |",
          f"| Findings without a citation | {ci['findings_without_citation']} |",
          f"| Key locations cited | {sc['key_locations']['cited']}/{sc['key_locations']['total']} "
          f"({sc['key_locations']['pct']}%) |",
          f"| Key items with every location cited | {sc['key_locations']['items_fully_cited']}"
          f"/{r['counts']['key_items']} |",
          f"| Source lines referenced | {sc['source_lines_cited']}/{sc['source_lines']} "
          f"({sc['source_line_coverage_pct']}%) |",
          f"| Source files never cited | {sc['files_never_cited_n']} |",
          f"| Requirement ids defined | {st['requirement_ids_defined']} |",
          f"| Duplicate requirement ids | {st['duplicate_requirement_ids_n']} |",
          f"| Unresolved cross-references | {st['unresolved_cross_references_n']} |",
          f"| Requirements with no citation | {st['requirements_without_citation_n']} |",
          f"| Orphan citations (outside any requirement) | "
          f"{st['orphan_citations'] if st['orphan_citations_applicable'] else 'n/a at this segmentation'} |",
          f"| Malformed: heading level jumps / empty sections / unclosed fence | "
          f"{st['malformed']['heading_level_jumps_n']} / {st['malformed']['empty_sections_n']} / "
          f"{st['malformed']['unclosed_code_fence']} |", ""]

    rc, pr = r["recall"], r["precision"]
    j = r.get("judge") or {}
    L += [f"## Judged ({'estimated, ' + str(j.get('runs')) + ' passes' if j.get('runs') else 'estimated'})",
          "", "| Metric | Value | 95% CI (key-size) | Across passes (min/med/max) |", "|---|---|---|---|"]

    def ciof(name):
        b = (j.get("bootstrap_ci95") or {}).get(name)
        return f"[{b['lo']}, {b['hi']}]" if b and b.get("lo") is not None else "n/a"

    def spof(name):
        s = (j.get("across_runs") or {}).get(name)
        return f"{s['min']} / {s['median']} / {s['max']}" if s and s.get("median") is not None else "n/a"

    rows = [("Recall (full matches)", rc["strict_full_pct"], "recall_strict_full_pct", "recall_strict_full_pct"),
            ("Recall (partial = 0.5)", rc["partial_credit_pct"], "recall_partial_credit_pct", None),
            ("Recall (any match)", rc["any_match_pct"], "recall_any_match_pct", "recall_any_match_pct"),
            ("Core recall (full)", rc["core_full_pct"], None, None),
            ("Core recall (any)", rc["core_any_pct"], None, None),
            ("Precision (matched to key)", pr["keyed_pct"], "precision_keyed_pct", "precision_keyed_pct"),
            ("Precision (correct incl. true extras)", pr["correct_pct"], None, None),
            ("Incorrect findings", pr["incorrect"], None, "incorrect"),
            ("Unsupported findings", pr["unsupported"], None, None),
            ("Uncertain findings (abstained)", pr["uncertain"], None, "uncertain"),
            ("Citation accuracy (overlaps key lines)", ci["accuracy_pct"], None, None)]
    for label, v, cik, spk in rows:
        val = "n/a" if v is None else f"{v}{'%' if isinstance(v, float) else ''}"
        L.append(f"| {label} | {val} | {ciof(cik) if cik else ''} | {spof(spk) if spk else ''} |")
    for name, u in (("Judge", r["judge_usage"]), ("Generation", r["generation_usage"])):
        if u:
            L.append(f"| {name} tokens in/out (cost) | {u['input_tokens']}/{u['output_tokens']} "
                     f"(${u['cost_usd']:.4f}) |  |  |")

    if j.get("agreement"):
        a = j["agreement"]
        L += ["", "### Judge agreement across passes", "", "| Statistic | Value |", "|---|---|",
              f"| Mean per-finding agreement | {a['mean_per_finding']} |",
              f"| Unanimous findings | {a['unanimous_findings']} ({a['unanimous_pct']}%) |",
              f"| Fleiss kappa, verdicts | {a['fleiss_kappa_verdicts']} ({a['fleiss_kappa_verdicts_label']}) |",
              f"| Fleiss kappa, key coverage | {a['fleiss_kappa_key_coverage']} "
              f"({a['fleiss_kappa_key_coverage_label']}) |",
              f"| Mean pairwise Cohen kappa, verdicts | {a['mean_pairwise_cohen_verdicts']} |",
              f"| Mean pairwise Cohen kappa, key coverage | {a['mean_pairwise_cohen_key_coverage']} |",
              f"| Findings with no majority | {j['abstention']['no_majority_findings']} |",
              f"| Cache hits / misses | {j['cache']['hits']} / {j['cache']['misses']} |"]
    if j.get("golden"):
        g = j["golden"]
        L += ["", "### Calibration against human labels", "",
              f"- Golden set: `{g['source']}` ({g['n_labelled']} key items, labelled by "
              f"{g['labelled_by']} on {g['labelled_on']})",
              f"- Exact agreement: **{g['agreement_pct']}%**; found-vs-missed agreement: "
              f"**{g['found_vs_missed_agreement_pct']}%**; Cohen kappa: **{g['cohen_kappa']}**",
              f"- Confusion (human -> judge): {g['confusion']}", "",
              "| Key item | Human | Judge | Agree |", "|---|---|---|---|"]
        for row in g["items"]:
            L.append(f"| {row['key_id']} | {row['expected']} | {row['observed']} | "
                     f"{'yes' if row['agree'] else 'NO'} |")

    for dim, title in (("type", "type"), ("evidence", "evidence class"), ("flag", "trap flag")):
        rb = r.get("recall_by", {}).get(dim) or {}
        if len(rb) > 1 or dim == "flag" and any(g != "(none)" for g in rb):
            L += ["", f"## Recall by {title}", "", "| Group | Items | Full | Partial | Missed | Full % | Any % |",
                  "|---|---|---|---|---|---|---|"]
            for g, v in rb.items():
                L.append(f"| {g} | {v['n']} | {v['full']} | {v['partial']} | {v['missed']} | "
                         f"{v['full_pct']}% | {v['any_pct']}% |")

    L += ["", "## Key coverage", "",
          "| Key item | Type | Core | Evidence | Flags | Coverage | Locations cited |",
          "|---|---|---|---|---|---|---|"]
    loc = {d["key_id"]: d for d in r["source_coverage"]["key_location_detail"]}
    for k in items:
        kid = str(k["id"])
        d = loc.get(kid, {"covered": 0, "total": 0})
        L.append(f"| {kid} | {k['type']} | {'yes' if k.get('core') else ''} | {k.get('evidence', '')} | "
                 f"{', '.join(k.get('flags') or [])} | {r['key_coverage'][kid]} | "
                 f"{d['covered']}/{d['total']} |")
    if r["missed_items"]:
        L += ["", "## Missed key items", ""] + [f"- **{m['id']}**: {m['statement']}" for m in r["missed_items"]]
    if r["possible_inventions"]:
        L += ["", "## Possible inventions / incorrect findings", ""]
        for p in r["possible_inventions"][:200]:
            L.append(f"- **{p['finding_id']}** ({p['verdict']}) {p['note']} "
                     f"{'invalid cits: ' + ', '.join(p['invalid_citations']) if p['invalid_citations'] else ''}"
                     f"\n  > {p['text'][:200].replace(chr(10), ' ')}")
    bad = [(f["id"], c) for f in r["findings"] for c in f["citations"] if not c["valid"]]
    if bad:
        L += ["", "## Invalid citations", ""] + [f"- {fid}: `{c['raw']}` -> {c['reason']}"
                                                for fid, c in bad[:300]]
    never = r["source_coverage"]["files_never_cited"]
    if never:
        L += ["", "## Source files never cited", ""] + [f"- `{x}`" for x in never]
    return "\n".join(L) + "\n"


# ============================================================================ CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True, type=Path)
    ap.add_argument("--key", required=True, type=Path)
    ap.add_argument("--source-root", required=True, type=Path)
    ap.add_argument("--out-dir", type=Path, help="default: directory of --spec")
    ap.add_argument("--no-llm", action="store_true",
                    help="deterministic lexical matcher, no Bedrock calls. SMOKE TEST ONLY: "
                         "lexical matching cannot tell a rule from its negation")
    ap.add_argument("--gen-log", type=Path, help="JSONL token log of the run that produced the spec")
    ap.add_argument("--gen-tag-prefix", help="only count generation-log entries with this tag prefix")
    ap.add_argument("--suffix", default="", help="output file suffix, e.g. '.nollm'")
    ap.add_argument("--exclude", action="append", default=[],
                    help="glob (relative path or basename) of files under --source-root to ignore, "
                         "e.g. --exclude ANSWER_KEY.yaml; repeatable")

    g = ap.add_argument_group("A. deterministic layer")
    g.add_argument("--segment", choices=list(SEGMENT_MODES), default="auto",
                   help="; ".join(f"{k}: {v}" for k, v in SEGMENT_DOC.items()))
    g.add_argument("--compare-to", type=Path, metavar="SCORE_JSON",
                   help="an earlier score.json; warn loudly when the two finding counts differ by "
                        f"more than {COMPARABILITY_RATIO}x, because the metrics are then not comparable")
    g.add_argument("--max-finding-chars", type=int, default=DEFAULT_MAX_FINDING_CHARS,
                   help="cap on text retained per finding (citations are parsed from the full text "
                        "first); bounds memory on very large specs")
    g.add_argument("--max-report-findings", type=int, default=DEFAULT_MAX_REPORT_FINDINGS,
                   help="cap on per-finding detail written to score.json; metrics always cover all")

    g = ap.add_argument_group("B. judged layer")
    g.add_argument("--judge-runs", type=int, default=3, metavar="N",
                   help="judge every finding N times and take a majority vote (default 3). "
                        "Cost scales linearly; use 1 for a cheap check")
    g.add_argument("--judge-batch", type=int, default=25, help="findings per judge call (default 25)")
    g.add_argument("--judge-evidence", choices=["chunked", "full"], default="chunked",
                   help="chunked (default): only the lines the batch cites plus the closest key "
                        "locations; full: the legacy whole-tree dump capped by --max-source-chars")
    g.add_argument("--max-evidence-chars", type=int, default=40000,
                   help="per-batch cap on chunked source evidence (default 40000)")
    g.add_argument("--evidence-context", type=int, default=8,
                   help="context lines around each cited range (default 8)")
    g.add_argument("--evidence-key-items", type=int, default=12,
                   help="how many lexically closest key items contribute their source lines "
                        "to a batch (default 12)")
    g.add_argument("--max-source-chars", type=int, default=80000,
                   help="cap on the whole-tree dump used by --judge-evidence full (default 80000)")
    g.add_argument("--no-cache", action="store_true",
                   help="bypass the content-addressed judge cache (neither read nor write)")
    g.add_argument("--cache-dir", type=Path, help="default: eval/judge/cache")
    g.add_argument("--max-judge-cost-usd", type=float,
                   help="abort before calling Bedrock if the estimate (cache hits count as $0) "
                        "exceeds this, and abort mid-run if actual spend passes it; exits 3")
    g.add_argument("--dry-run-judge", action="store_true",
                   help="print the call/token/cost plan and exit without calling Bedrock")
    g.add_argument("--log-judge-prompts", type=Path, metavar="DIR",
                   help="write every judge prompt verbatim, for auditing what the judge saw")
    g.add_argument("--golden", type=Path,
                   help="human-labelled calibration file (eval/judge/golden/*.yaml); reports "
                        "judge-vs-human agreement and Cohen kappa")
    g.add_argument("--bootstrap-iters", type=int, default=2000)
    g.add_argument("--seed", type=int, default=20261006)
    g.add_argument("--judge-tag", help="tag for judge calls in the Bedrock log")
    g.add_argument("--reuse-judgement", type=Path,
                   help="take judge verdicts from an earlier score.json (same spec + segmentation); "
                        "no LLM calls")
    return ap


#: exit code when --max-judge-cost-usd stops the run (before or during judging). The suite
#: runner (eval/ci/run_suite.py) maps it to the stage status ``budget_exceeded``.
EXIT_BUDGET = 3


def main(argv=None):
    a = build_parser().parse_args(argv)
    from judge.runner import JudgeBudgetExceeded
    try:
        return _main(a)
    except JudgeBudgetExceeded as exc:
        print(f"score.py: STOPPED by the judge budget guard: {exc}", file=sys.stderr)
        return EXIT_BUDGET


def _main(a):
    report, items = score(
        a.spec, a.key, a.source_root, not a.no_llm, a.gen_log, a.gen_tag_prefix,
        a.exclude, a.segment, a.max_source_chars, a.judge_tag, a.reuse_judgement,
        judge_runs=max(1, a.judge_runs), judge_batch=a.judge_batch,
        judge_evidence=a.judge_evidence, max_evidence_chars=a.max_evidence_chars,
        evidence_context=a.evidence_context, evidence_key_items=a.evidence_key_items,
        cache_enabled=not a.no_cache, cache_dir=a.cache_dir,
        max_judge_cost_usd=a.max_judge_cost_usd, dry_run_judge=a.dry_run_judge,
        prompt_dir=a.log_judge_prompts, golden=a.golden, compare_to=a.compare_to,
        bootstrap_iters=a.bootstrap_iters, seed=a.seed,
        max_finding_chars=a.max_finding_chars, max_report_findings=a.max_report_findings)
    out = a.out_dir or a.spec.parent
    out.mkdir(parents=True, exist_ok=True)
    # default=str: a judged run can cost several dollars, so an unexpected value type
    # (e.g. a bare YAML date in a golden file) must not throw the whole run away.
    (out / f"score{a.suffix}.json").write_text(
        json.dumps(report, indent=2, sort_keys=False, default=str))
    (out / f"score{a.suffix}.md").write_text(to_markdown(report, items))
    if report.get("dry_run"):
        print(json.dumps(report["judge_plan"], indent=2))
    else:
        summary = {k: report[k] for k in ("method", "counts", "recall", "precision", "citations",
                                          "segmentation") if k in report}
        summary["citations"] = {k: v for k, v in summary["citations"].items()
                                if k != "supported_formats"}
        summary["deterministic"] = {
            "key_locations_cited_pct": report["source_coverage"]["key_locations"]["pct"],
            "source_line_coverage_pct": report["source_coverage"]["source_line_coverage_pct"],
            "files_never_cited": report["source_coverage"]["files_never_cited_n"],
            "duplicate_requirement_ids": report["structural"]["duplicate_requirement_ids_n"],
            "unresolved_cross_references": report["structural"]["unresolved_cross_references_n"],
            "requirements_without_citation": report["structural"]["requirements_without_citation_n"],
            "orphan_citations": report["structural"]["orphan_citations"],
        }
        if report.get("judge"):
            jr = report["judge"]
            summary["judge"] = {"runs": jr.get("runs"), "agreement": jr.get("agreement"),
                                "abstention": jr.get("abstention"),
                                "bootstrap_ci95": {k: v for k, v in (jr.get("bootstrap_ci95") or {}).items()
                                                   if k.startswith(("recall", "precision"))},
                                "across_runs": jr.get("across_runs"),
                                "cache": jr.get("cache"), "golden": (jr.get("golden") or {}).get("agreement_pct")}
        print(json.dumps(summary, indent=2))
        print(f"possible inventions: {len(report['possible_inventions'])}; "
              f"wrote {out}/score{a.suffix}.{{json,md}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

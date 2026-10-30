# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
"""Lean pipeline v1: spec/finding helpers shared by lean.py, ask.py and impact.py.

- CitationIndex: resolves `file:Lx-Ly` citations written with a stem, basename or relative path (and bare
  `Lx` ranges that follow a file) to canonical `basename.ext:Lx-Ly`, clamped to the file length.
- parse_findings / render_findings: markdown tables `| ID | Statement | Citations | Evidence | Flags |`
  <-> dicts. The rendered layout is what eval/score.py segments (one table row = one finding).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

EVIDENCE = ["Verified", "Inferred", "Unknown", "Confirmation Required"]
FLAGS = ["inconsistent-values", "db-only", "ui-only", "duplicated-across-layers", "duplicated-logic", "dead-code",
         "comment-only", "removed", "external", "undocumented", "logic-flaw", "contract"]


class CitationIndex:
    def __init__(self, src: Path, files: list[str]):
        self.src = src
        self.files = files
        self.nlines = {f: len((src / f).read_text(errors="replace").splitlines()) for f in files}
        base_count = {}
        for f in files:
            b = f.split("/")[-1].lower()
            base_count[b] = base_count.get(b, 0) + 1
        self.alias = {}   # lowercase alias -> relpath
        stems = {}
        for f in files:
            b = f.split("/")[-1]
            self.alias[f.lower()] = f
            if base_count[b.lower()] == 1:
                self.alias[b.lower()] = f
            st = b.rsplit(".", 1)[0].lower()
            stems.setdefault(st, []).append(f)
        for st, fs in stems.items():
            if len(fs) == 1 and st not in self.alias and len(st) >= 3:
                self.alias[st] = fs[0]
        names = sorted(self.alias, key=len, reverse=True)
        alt = "|".join(re.escape(n) for n in names)
        # file token (optionally back-ticked / with path prefix), then a line range
        self.cite_re = re.compile(
            r"(?P<pre>`?)(?P<file>(?:[\w./\\-]*/)?(?:" + alt + r"))`?"
            r"(?:\s*[:#]\s*\(?\s*(?:lines?\s+)?L?|\s*,?\s*\(?\s*(?:L|lines?\s+|ll?\.\s*))"
            r"(?P<a>\d+)(?:\s*(?:-|–|—|to|\.\.)\s*L?(?P<b>\d+))?\b`?", re.I)
        self.bare_re = re.compile(r"(?<![\w.:/#-])`?(?:lines?\s+|L)(?P<a>\d+)(?:\s*(?:-|–|—|to)\s*L?(?P<b>\d+))?\b`?", re.I)

    def resolve(self, name: str) -> str | None:
        n = name.replace("\\", "/").strip("`").lstrip("./").lower()
        if n in self.alias:
            return self.alias[n]
        tail = n.split("/")[-1]
        return self.alias.get(tail)

    def label(self, f: str) -> str:
        b = f.split("/")[-1]
        return b if self.alias.get(b.lower()) == f else f

    def fmt(self, f: str, a: int, b: int | None = None) -> str | None:
        n = self.nlines.get(f, 0)
        b = b or a
        if a > b:
            a, b = b, a
        if a < 1 or a > n:
            return None
        b = min(b, n)
        return f"{self.label(f)}:L{a}" if a == b else f"{self.label(f)}:L{a}-L{b}"

    def extract(self, text: str, default_file: str | None = None) -> list[tuple[str, int, int]]:
        """All citations in text as (relpath, a, b), in order; bare ranges inherit the previous file."""
        out, spans = [], []
        for m in self.cite_re.finditer(text):
            f = self.resolve(m.group("file"))
            if not f:
                continue
            a = int(m.group("a"))
            b = int(m.group("b") or a)
            out.append((m.start(), f, a, b))
            spans.append((m.start(), m.end()))
        for m in self.bare_re.finditer(text):
            if any(s <= m.start() < e for s, e in spans):
                continue
            prev = [x for x in out if x[0] < m.start()]
            f = prev[-1][1] if prev else default_file
            if f:
                out.append((m.start(), f, int(m.group("a")), int(m.group("b") or m.group("a"))))
        out.sort()
        res = []
        for _, f, a, b in out:
            if a > b:
                a, b = b, a
            n = self.nlines.get(f, 0)
            if 1 <= a <= n:
                res.append((f, a, min(b, n)))
        return list(dict.fromkeys(res))

    def normalize_text(self, text: str, default_file: str | None = None) -> str:
        """Rewrite citations in free text to canonical `name.ext:Lx-Ly` (bare `Lx` ranges get the preceding
        file, else default_file); out-of-range citations are dropped."""
        reps = []  # (start, end, replacement)
        last_file = None
        cites = [(m.start(), m.end(), m) for m in self.cite_re.finditer(text)]
        bares = [(m.start(), m.end(), m) for m in self.bare_re.finditer(text)
                 if not any(s <= m.start() < e for s, e, _ in cites)]
        for s0, e0, m in sorted(cites + bares, key=lambda x: x[0]):
            if "file" in m.groupdict() and m.groupdict().get("file") is not None:
                f = self.resolve(m.group("file"))
                if not f:
                    continue
                last_file = f
            else:
                f = last_file or default_file
                if not f:
                    continue
            c = self.fmt(f, int(m.group("a")), int(m.group("b") or m.group("a")))
            reps.append((s0, e0, f"`{c}`" if c else "(invalid citation removed)"))
        for s0, e0, r in reversed(reps):
            text = text[:s0] + r + text[e0:]
        return text

    def cell(self, cits: list[tuple[str, int, int]]) -> str:
        return "; ".join(f"`{self.fmt(f, a, b)}`" for f, a, b in cits if self.fmt(f, a, b))


# ---------------------------------------------------------------------------- findings
ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")


def split_row(line: str) -> list[str]:
    inner = line.strip()[1:-1]
    # split on | not inside backticks
    cells, cur, tick = [], "", False
    for ch in inner:
        if ch == "`":
            tick = not tick
        if ch == "|" and not tick:
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
    cells.append(cur.strip())
    return cells


def parse_findings(md: str, file: str | None, ci: CitationIndex, origin: str = "layer2") -> list[dict]:
    """Parse `## Section` + table rows (and Open Questions bullets) from one model answer."""
    out, section, header = [], None, None
    for raw in md.splitlines():
        line = raw.rstrip()
        m = re.match(r"^#{1,4}\s+(.*)", line)
        if m:
            section, header = m.group(1).strip(), None
            continue
        if ROW_RE.match(line):
            cells = split_row(line)
            if re.match(r"^:?-{2,}", cells[0] or "-"):
                continue
            if header is None and cells and cells[0].lower() in ("id", "#") or (len(cells) > 1 and cells[1].lower() == "statement"):
                header = [c.lower() for c in cells]
                continue
            hdr = header or ["id", "statement", "citations", "evidence", "flags"]
            row = dict(zip(hdr, cells + [""] * (len(hdr) - len(cells))))
            stmt = row.get("statement", "")
            cits = ci.extract(row.get("citations", ""), file)
            cits = list(dict.fromkeys(cits + ci.extract(stmt, None) + ci.extract(row.get("flags", ""), None)))
            ev = next((e for e in EVIDENCE if e.lower() in row.get("evidence", "").lower()), "Inferred")
            flags = [f for f in FLAGS if re.search(r"(?<![\w-])" + re.escape(f) + r"(?![\w-])", row.get("flags", ""))]
            out.append({"local_id": row.get("id", ""), "section": section or "", "file": file, "statement": stmt,
                        "citations": cits, "evidence": ev, "flags": flags, "flag_note": row.get("flags", ""),
                        "origin": origin})
            continue
        b = re.match(r"^\s*[-*]\s+(.*)", line)
        if b and section and "question" in section.lower():
            txt = b.group(1).strip()
            out.append({"local_id": "", "section": section, "file": file, "statement": txt,
                        "citations": ci.extract(txt, file), "evidence": "Confirmation Required", "flags": [],
                        "flag_note": "", "origin": origin, "bullet": True})
    return out


def esc(s: str) -> str:
    return (s or "").replace("\n", " ").replace("|", "\\|").strip()


def render_findings(fs: list[dict], ci: CitationIndex, id_key: str = "local_id") -> str:
    rows = ["| ID | Statement | Citations | Evidence | Flags |", "|----|-----------|-----------|----------|-------|"]
    for f in fs:
        stmt = ci.normalize_text(f["statement"], f.get("file"))
        flags = ci.normalize_text(f.get("flag_note") or "", f.get("file")).strip() or \
            (", ".join(f"`{x}`" for x in f["flags"]) or "-")
        if f.get("note"):
            flags += f" ({f['note']})"
        rows.append(f"| {esc(f.get(id_key) or f.get('id', ''))} | {esc(stmt)} | {ci.cell(f['citations']) or '-'} | {f['evidence']} | {esc(flags)} |")
    return "\n".join(rows)


def load_findings_json(run: Path) -> list[dict]:
    return json.loads((run / "findings.json").read_text())["findings"]

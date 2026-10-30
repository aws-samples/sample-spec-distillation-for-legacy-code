#!/usr/bin/env python3
"""
citations.py — parse, resolve and VERIFY a source citation against the paged source.

WHY THIS IS SHARED
------------------
A citation ("this rule lives at usp_APInvPost.sql#L66-L72") is only worth anything if
it can be checked. Several consumers need the same check: the Issues report verifies
every location a model proposes, the MCP trace tools answer "where did this come from"
and "what does this file mean", and traceability export maps requirements to source.

Each one needs identical behaviour, because the whole value of a citation is that a
reader can follow it. Three private copies of this logic would drift, and drift here
produces confidently wrong line numbers — the kind of error nobody notices until they
open the file and the rule is not there.

So this module owns it once. It was extracted verbatim from issues.py (which now
imports it) to preserve behaviour exactly; issues.py's output must not change.

THE SOURCE OF TRUTH IS THE PAGES, NOT THE REPO
----------------------------------------------
Line numbers are resolved against `<output>/.pages/*/concat.txt`, not the original
source tree. That is deliberate: the pages are what the model actually read, including
the lifted text of non-text formats and the line numbering it was shown. Verifying
against the repo instead would mismatch for `inferred` units and would silently drift
if the working tree moved on after the run.

NOT IN SCOPE: eval/score.py has its own citation regex accepting several looser shapes
(`f.cob:10-20`, `f.cob (lines 10-20)`). It is deliberately left alone — it scores
evaluation runs, and changing how citations parse there would change recorded scores.
Align it only as a conscious, separately-reviewed decision.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

# ── Page markers (as written by runtimes/pipeline/concat-batch.sh) ───────────

FILE_MARKER = re.compile(r"^\[FILE:\s*(.+?)\]\s*$")
END_MARKER = re.compile(r"^\[END FILE:")
NUMBERED = re.compile(r"^\s*(\d+) \| ?(.*)$")
_MARKER_ATTR = re.compile(
    r"\s+(?:KIND:|SIZE:|VIA:|\[PART\b|ORIGINAL_LINE_START:|LINE_SEGMENT:|REASON:)")

# A citation as it appears in a concept body or a model response. Accepts
# "path#L10-L20", "path:L10", "path:10-20" and "path lines 10-20".
LOC_STR = re.compile(
    r"^\s*(.+?\.\w+)\s*(?:[:#]\s*L?|\s+lines?\s+)(\d+)(?:\s*[-–]\s*L?(\d+))?\s*$", re.I)

# Citations embedded in prose, e.g. an "- Evidence: a.vb#L1-L2, b.sql#L9 (note)" line.
# Scanning rather than anchoring lets several refs share one line.
LOC_INLINE = re.compile(r"([\w./\\-]+\.\w+)\s*[:#]\s*L?(\d+)(?:\s*[-–]\s*L?(\d+))?", re.I)


def marker_path(inner: str) -> str:
    """Strip KIND:/SIZE:/VIA:/[PART …] attributes from a [FILE: …] marker body."""
    m = _MARKER_ATTR.search(inner)
    return (inner[: m.start()] if m else inner).strip()


def load_sources(output_dir) -> dict[str, dict[int, str]]:
    """{relative path: {line number: text}} from every page concat (line numbers as paged)."""
    sources: dict[str, dict[int, str]] = {}
    pages = Path(output_dir) / ".pages"
    for concat in sorted(pages.glob("*/concat.txt")) if pages.is_dir() else []:
        cur = None
        for raw in concat.read_text(encoding="utf-8", errors="replace").splitlines():
            m = FILE_MARKER.match(raw)
            if m:
                cur = sources.setdefault(marker_path(m.group(1)), {})
                continue
            if END_MARKER.match(raw):
                cur = None
                continue
            if cur is None:
                continue
            n = NUMBERED.match(raw)
            if n:
                cur.setdefault(int(n.group(1)), n.group(2))
    return {p: lines for p, lines in sources.items() if lines}


def excerpt(sources: dict[str, dict[int, str]], path: str, start: int, end: int,
            pad: int = 2, max_lines: int = 30) -> list[tuple[int, str]]:
    """The cited lines plus `pad` of context, capped at `max_lines`."""
    lines = sources.get(path, {})
    a, b = max(1, start - pad), end + pad
    if b - a + 1 > max_lines:
        b = a + max_lines - 1
    return [(n, lines[n]) for n in range(a, b + 1) if n in lines]


def render_numbered(path: str, rows: list[tuple[int, str]]) -> str:
    return f"[FILE: {path}]\n" + "\n".join(f"{n:5d} | {t}" for n, t in rows)


class SourceIndex:
    """Paths-to-lines, with tolerant path resolution.

    Citations name files inconsistently (bare basename, partial path, different case,
    backslashes), so an exact-match-only lookup would reject valid references. Matching
    widens stepwise and only accepts a basename or suffix match when it is UNAMBIGUOUS —
    returning None rather than guessing between two candidates.
    """

    def __init__(self, sources: dict[str, dict[int, str]]):
        self.sources = sources
        self.by_lower = {p.lower().replace("\\", "/"): p for p in sources}
        base = defaultdict(list)
        for p in sources:
            base[Path(p).name.lower()].append(p)
        self.by_base = base

    def resolve(self, file: str) -> str | None:
        f = (file or "").strip().strip("`'\"").replace("\\", "/").lstrip("./")
        if f in self.sources:
            return f
        if f.lower() in self.by_lower:
            return self.by_lower[f.lower()]
        suffix = [p for lo, p in self.by_lower.items() if lo.endswith("/" + f.lower())]
        if len(suffix) == 1:
            return suffix[0]
        b = self.by_base.get(Path(f).name.lower(), [])
        return b[0] if len(b) == 1 else None


def from_output_dir(output_dir) -> SourceIndex:
    """Convenience: build a SourceIndex straight from a run's output directory."""
    return SourceIndex(load_sources(output_dir))


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_location(idx: SourceIndex, loc) -> dict | None:
    """Return a verified {file,start,end,ref,quote_verified,excerpt} or None if unverifiable.

    `loc` is either a citation string or a dict with file/start/end (and optional quote).
    None means "could not verify" — the caller must drop the citation rather than pass it
    on unchecked.

    When a `quote` is supplied and is not found in the cited range, the citation is
    RELOCATED to the line where that text actually appears. A model's line arithmetic is
    less reliable than the text it quoted, so the quote is treated as the stronger
    signal; `quote_verified` records which way it went.
    """
    quote = ""
    if isinstance(loc, str):
        m = LOC_STR.match(loc)
        if not m:
            return None
        file, start, end = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
    elif isinstance(loc, dict):
        file = loc.get("file") or loc.get("path") or ""
        try:
            start = int(str(loc.get("start", loc.get("line", ""))).lstrip("Ll"))
            end = int(str(loc.get("end", start)).lstrip("Ll"))
        except (TypeError, ValueError):
            if isinstance(loc.get("ref"), str):
                return verify_location(idx, loc["ref"])
            return None
        quote = str(loc.get("quote") or "")
    else:
        return None
    path = idx.resolve(file)
    if not path:
        return None
    lines = idx.sources[path]
    if end < start:
        start, end = end, start
    if start < 1 or start not in lines or end not in lines:
        return None
    quote_verified = None
    q = _norm_ws(quote)
    if q:
        def hit(a, b):
            return q in _norm_ws(" ".join(lines.get(n, "") for n in range(a, b + 1)))
        if hit(max(1, start - 3), end + 3):
            quote_verified = True
        else:
            found = [n for n in sorted(lines) if q in _norm_ws(lines[n])]
            if found:  # model's range was off: relocate to where the quoted text actually is
                width = min(end - start, 40)
                start = min(found, key=lambda n: abs(n - start))
                end = min(start + width, max(lines))
                while end not in lines:
                    end -= 1
                quote_verified = True
            else:
                quote_verified = False
    if end - start > 120:
        end = start + 120
        while end not in lines:
            end -= 1
    return {"file": path, "start": start, "end": end, "ref": f"{path}:L{start}-L{end}",
            "quote_verified": quote_verified,
            "excerpt": [[n, t] for n, t in
                        excerpt(idx.sources, path, start, end, pad=1, max_lines=40)]}


def parse_inline(text: str) -> list[dict]:
    """Extract every citation from a prose line (e.g. an "Evidence:" field).

    Returns unverified {file, start, end, ref} dicts in order of appearance; pass each
    through verify_location to confirm it. Unverified citations must never be surfaced
    as evidence — an unchecked line number is what makes a plausible claim unfalsifiable.
    """
    out, seen = [], set()
    for m in LOC_INLINE.finditer(text or ""):
        f, a, b = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
        key = (f, a, b)
        if key in seen:
            continue
        seen.add(key)
        out.append({"file": f, "start": a, "end": b, "ref": m.group(0).strip()})
    return out

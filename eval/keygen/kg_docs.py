# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""DOCS (optional): README / markdown headings and feature bullets -> capability candidates.

Documentation states intent, not behaviour, so every docs candidate is ``evidence_class:
Inferred`` and type ``workflow``. A docs item that the code harvest or the tests corroborate is
worth more than either alone; one nobody corroborates is a question for the reviewer
("is this still true?"), not a rule.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from kg_source import Candidate, clip, loc

SKIP_HEADINGS = re.compile(r"(licen[cs]e|contribut|install|setup|getting started|build|deploy|docker|"
                           r"azure|prerequisite|table of contents|reference|related|credits|sponsor|"
                           r"troubleshoot|running|code of conduct|security|changelog|faq|configur|"
                           r"topics|ebook|toc\b|linux|macos|windows|local|environment|migration)", re.I)


def doc_files(root: Path, globs: list[str]) -> list[str]:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or ".git" in p.parts or "node_modules" in p.parts:
            continue
        rel = p.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, g) for g in globs):
            out.append(rel)
    return out


def mine_docs(root: Path, globs: list[str], max_per_file: int = 40) -> list[Candidate]:
    out = []
    for rel in doc_files(root, globs):
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").split("\n")
        heading, skip, in_code, n = "", False, False, 0
        for i, raw in enumerate(lines, 1):
            s = raw.strip()
            if s.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            hm = re.match(r"^(#{1,4})\s+(.+?)\s*#*$", s)
            if hm:
                heading = re.sub(r"[*_`]", "", hm.group(2)).strip()
                skip = bool(SKIP_HEADINGS.search(heading))
                continue
            bm = re.match(r"^(?:[-*+]|\d+\.)\s+(.+)$", s)
            if not bm or skip or n >= max_per_file:
                continue
            text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", bm.group(1))
            text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
            text = re.sub(r"[*_`]", "", text).strip()
            words = re.findall(r"[A-Za-z]{2,}", text)
            if len(words) < 4 or text.lower().startswith(("http", "see ", "note")):
                continue
            stmt = f"Documented capability ({heading or rel}): {clip(text, 200)}"
            out.append(Candidate("workflow", stmt, [loc(rel, i)], "docs", "markdown",
                                 evidence_class="Inferred",
                                 keys=[f"member:{w.lower()}" for w in words if len(w) > 4][:6],
                                 extra={"snippet": clip(s), "heading": heading}))
            n += 1
    return out

#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""content_guard.py — fail-closed checks on what the model SAW and what it SAID.

Two failure modes used to pass as success (pipeline-assessment §5.3):
  1. Pages that carry only referential markers ([FILE: x KIND: referential] with no body) — the
     model receives no source at all.
  2. A still whose reduce output is a model REFUSAL ("I don't have the actual file contents…")
     or empty — the completeness barrier only tested that {still}_raw_concat.md existed.

CLI:
  content_guard.py pages <pages_dir>                       exit 3 if NO page carries content
  content_guard.py barrier <stills_base> --stills a,b,c    one line per still:
        <still>\t<status>\t<detail>   status in ok|no_findings|refusal|empty|missing
        exit 4 if any still is refusal/empty/missing
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

FILE_MARKER = re.compile(r"^\[FILE:\s*(.+?)\]\s*$")
END_MARKER = re.compile(r"^\[END FILE:")
_PART = re.compile(r"\s*\[PART\s+\d+/\d+\]")

# Phrases a model uses when it was given no usable source. Matched case-insensitively.
REFUSAL_PATTERNS = [
    r"\bI (?:don't|do not|didn't|did not) (?:have|see|receive)\b.{0,40}\b(?:actual|real)?\s*(?:file|source)?\s*(?:contents?|code|source)",
    r"\bI need to see the actual (?:content|contents|source|code|files?)",
    r"\bno source (?:material|code|content|files?)\b.{0,20}\b(?:available|provided|present|shown|included)",
    r"\bsource content (?:is |was )?absent\b",
    r"\bonly (?:placeholder|referential) (?:markers|stubs)\b",
    r"\b(?:provided|given) (?:only )?as (?:empty shells|referential stubs|placeholders?)\b",
    r"\bplease provide the (?:actual )?(?:file contents|source|source code|files?)\b",
    r"\bno (?:line-numbered )?(?:source )?(?:code|content) (?:was|is) (?:included|provided)\b",
    r"\bcannot (?:extract|analy[sz]e|identify) .{0,60}\bwithout (?:seeing|the actual|access)",
    r"\bno findings (?:can be|could be) (?:extracted|produced)\b",
]
_REFUSAL_RX = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in REFUSAL_PATTERNS]

# A concept/finding block header in a still output ("### Rule: ...", "### Entity: ...").
_BLOCK_RX = re.compile(r"^#{2,4}\s+\S", re.MULTILINE)
_HEADER_LINES = re.compile(r"^(# .*Raw Extraction Concat|Pages:.*|Generated:.*|---|<!--.*-->)\s*$")


def is_refusal(text: str) -> bool:
    return any(rx.search(text) for rx in _REFUSAL_RX)


def _split_pages(text: str) -> list[tuple[str, str]]:
    """Split a raw_concat into (page_label, body) sections on <!-- PAGE: x --> comments."""
    parts = re.split(r"<!--\s*PAGE:\s*([^>]+?)\s*-->", text)
    if len(parts) == 1:
        return [("all", text)]
    out = []
    for i in range(1, len(parts), 2):
        out.append((parts[i], parts[i + 1] if i + 1 < len(parts) else ""))
    return out


def classify_text(text: str) -> tuple[str, str]:
    """Classify a still output -> (status, detail).
    ok           at least one page section has finding blocks
    no_findings  non-empty, no blocks, no refusal language (an honest "nothing here") — allowed
    refusal      every substantive page section is a refusal (model saw no usable source)
    empty        nothing but headers/whitespace
    """
    body_lines = [ln for ln in text.splitlines() if ln.strip() and not _HEADER_LINES.match(ln.strip())]
    if not body_lines:
        return "empty", "no content beyond headers"
    sections = _split_pages(text)
    blocks = refusals = 0
    refused_pages = []
    for label, body in sections:
        b = body.strip()
        if not b:
            continue
        has_blocks = bool(_BLOCK_RX.search(b))
        if has_blocks and not (is_refusal(b[:1500]) and len(_BLOCK_RX.findall(b)) <= 1):
            blocks += 1
        elif is_refusal(b):
            refusals += 1
            refused_pages.append(label)
    if blocks:
        detail = f"{blocks} section(s) with findings"
        if refused_pages:
            detail += f"; refusal on page(s) {', '.join(refused_pages[:10])}"
        return "ok", detail
    if refusals:
        return "refusal", f"model refusal / no-source response on page(s) {', '.join(refused_pages[:10])}"
    return "no_findings", "non-empty output without finding blocks"


def check_still(stills_base: Path, still: str) -> tuple[str, str]:
    rc = stills_base / still / f"{still}_raw_concat.md"
    if not rc.is_file():
        return "missing", f"{rc.name} absent"
    try:
        text = rc.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return "missing", f"unreadable: {e}"
    return classify_text(text)


def page_content(pages_dir: Path) -> dict:
    """Per page: how many [FILE:] blocks carry a non-empty body vs are marker-only/referential."""
    report = {"pages": {}, "content_pages": 0, "marker_only_pages": 0,
              "files_with_body": 0, "files_marker_only": 0}
    for page in sorted(Path(pages_dir).glob("page_*")):
        concat = page / "concat.txt"
        if not concat.is_file():
            continue
        lines = concat.read_text(encoding="utf-8", errors="replace").splitlines()
        with_body = marker_only = 0
        cur, kind, body = None, "", 0

        def _close():
            nonlocal with_body, marker_only
            if cur is None:
                return
            if kind == "referential" or body == 0:
                marker_only += 1
            else:
                with_body += 1

        for ln in lines:
            m = FILE_MARKER.match(ln)
            if m:
                _close()
                inner = m.group(1)
                km = re.search(r"KIND:\s*(\S+)", inner)
                cur, kind, body = _PART.sub("", inner), (km.group(1) if km else "raw"), 0
                continue
            if END_MARKER.match(ln):
                _close()
                cur = None
                continue
            if cur is not None and re.sub(r"^\s*\d+\s\|\s?", "", ln).strip():
                body += 1
        _close()
        report["pages"][page.name] = {"files_with_body": with_body, "files_marker_only": marker_only}
        report["files_with_body"] += with_body
        report["files_marker_only"] += marker_only
        if with_body:
            report["content_pages"] += 1
        else:
            report["marker_only_pages"] += 1
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Fail-closed content checks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pages")
    p.add_argument("pages_dir")
    b = sub.add_parser("barrier")
    b.add_argument("stills_base")
    b.add_argument("--stills", required=True)
    t = sub.add_parser("classify")
    t.add_argument("file")
    args = ap.parse_args()

    if args.cmd == "pages":
        rep = page_content(Path(args.pages_dir))
        print(json.dumps({k: v for k, v in rep.items() if k != "pages"}))
        for name, d in rep["pages"].items():
            if not d["files_with_body"]:
                print(f"marker-only page: {name} ({d['files_marker_only']} file marker(s), no body)",
                      file=sys.stderr)
        return 3 if rep["content_pages"] == 0 else 0
    if args.cmd == "barrier":
        bad = 0
        for still in [s for s in args.stills.split(",") if s]:
            status, detail = check_still(Path(args.stills_base), still)
            print(f"{still}\t{status}\t{detail}")
            if status in ("refusal", "empty", "missing"):
                bad += 1
        return 4 if bad else 0
    if args.cmd == "classify":
        print("\t".join(classify_text(Path(args.file).read_text(encoding="utf-8", errors="replace"))))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Source-conservation gate for concat pages (run after paging, before any model call).

Proves that every manifest entry is accounted for EXACTLY ONCE across all pages:
  * either as emitted text — parts 1..n each present once, line ranges contiguous from 1 to the
    file's line count (over-cap lines: segments 1..S in order), and the numbered body in
    concat.txt carries exactly those line numbers — and, for raw source / lifted text, the
    emitted text re-joins to the file content read independently here;
  * or as an explicit marker-only unit (skipped.tsv) with a reason (unreadable, binary,
    referential), where the reason is cross-checked against the file itself.

Anything else — a missing entry, a dropped part, a gap/overlap in lines, a duplicate unit, a
page without a ledger (built by the pre-fix concat-batch.sh), a byte-cap breach — fails the
run with a non-zero exit code.

Usage:
  check-page-conservation.py --pages DIR --manifest FILE --source-root DIR
                             [--materialized TSV] [--page-byte-cap N] [--report FILE]

Writes <pages>/conservation_report.json and <pages>/skipped_manifest.tsv (run-level list of
every skipped unit with its reason). Exit 0 = conserved, 1 = violation, 2 = usage error.

The decode convention matches concat-batch.sh: UTF-8 with errors="replace", universal newlines,
lines = text.split("\\n").
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

LINE_RE = re.compile(r"^ *(\d+) \| (.*)\Z", re.S)


def _is_non_text(c: str) -> bool:
    return c == "\ufffd" or (ord(c) < 32 and c not in "\n\r\t")


def _is_binary_line(line: str) -> bool:
    # same per-line rule concat-batch.sh uses when cleaning mixed files
    return len(line) > 0 and sum(_is_non_text(c) for c in line) / len(line) > 0.30


def _read_text(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(), None
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"


def _load_materialized(path):
    mat = {}
    if path and os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh, delimiter="\t"):
                if len(row) >= 6:
                    mat[row[0]] = {"kind": row[1], "unit": row[2]}
    return mat


def _parse_page(page: Path, errors: list) -> list:
    """Return [(record, header_line, body_lines)] for a page, verifying ledger vs concat.txt."""
    ledger = page / "units.jsonl"
    concat = page / "concat.txt"
    if not ledger.exists():
        errors.append(f"{page.name}: no units.jsonl ledger (page built by pre-fix concat-batch.sh?) "
                      f"— delete the pages directory and rebuild")
        return []
    records = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    with open(concat, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    lines = text.split("\n")
    out, pos = [], 0
    for r in records:
        # blocks are joined by "\n" and each ends with "\n" → one empty separator line between
        if out:
            if pos >= len(lines) or lines[pos] != "":
                errors.append(f"{page.name}: missing block separator before unit {r['path']!r} part {r['part']}")
                return out
            pos += 1
        if pos >= len(lines):
            errors.append(f"{page.name}: concat.txt ends before unit {r['path']!r} part {r['part']}")
            return out
        hdr = lines[pos]
        pos += 1
        exp_prefix = f"[FILE: {r['path']}"
        if not hdr.startswith(exp_prefix) or not hdr.endswith("]"):
            errors.append(f"{page.name}: header {hdr[:200]!r} does not match ledger unit {r['path']!r}")
            return out
        if r["parts"] > 1:
            need = [f"[PART {r['part']}/{r['parts']}]", f"ORIGINAL_LINE_START: {r['line_start']}"]
            if r["seg"]:
                need.append(f"LINE_SEGMENT: {r['seg']}/{r['segs']}")
            for n in need:
                if n not in hdr:
                    errors.append(f"{page.name}: header for {r['path']!r} part {r['part']} lacks {n!r}")
        if r["status"] == "emitted":
            nbody = r["line_end"] - r["line_start"] + 1
        else:
            nbody = 0
        body = lines[pos:pos + nbody]
        pos += nbody
        end_expected = f"[END FILE: {r['path']} [PART {r['part']}/{r['parts']}]]" if r["parts"] > 1 \
            else f"[END FILE: {r['path']}]"
        if pos >= len(lines) or lines[pos] != end_expected:
            got = lines[pos][:200] if pos < len(lines) else "<EOF>"
            errors.append(f"{page.name}: expected {end_expected!r} after {nbody} body line(s) of "
                          f"{r['path']!r}, got {got!r}")
            return out
        pos += 1
        texts = []
        for j, bl in enumerate(body):
            mm = LINE_RE.match(bl)
            want = r["line_start"] + j
            if not mm or int(mm.group(1)) != want:
                errors.append(f"{page.name}: {r['path']!r} part {r['part']}: body line {j} is not "
                              f"numbered {want}: {bl[:80]!r}")
                break
            texts.append(mm.group(2))
        out.append((r, texts))
    tail = lines[pos:]
    if tail not in ([], [""]):
        errors.append(f"{page.name}: {len(tail)} unaccounted trailing line(s) in concat.txt")
    return out


def check(pages_dir: Path, manifest: Path, source_root: str, materialized: str | None,
          page_byte_cap: int) -> dict:
    errors: list[str] = []
    with open(manifest, encoding="utf-8") as fh:
        entries = [ln.strip() for ln in fh if ln.strip()]
    mat = _load_materialized(materialized)

    by_idx: dict[int, list] = {}
    pages = sorted(p for p in pages_dir.glob("page_*") if p.is_dir() and (p / "concat.txt").exists())
    for page in pages:
        size = (page / "concat.txt").stat().st_size
        if size > page_byte_cap:
            errors.append(f"{page.name}: concat.txt is {size} bytes > page cap {page_byte_cap}")
        for r, texts in _parse_page(page, errors):
            by_idx.setdefault(r["idx"], []).append((page.name, r, texts))

    skipped_rows = []
    stats = {"entries": len(entries), "emitted_files": 0, "skipped_files": 0, "parts": 0,
             "lines": 0, "pages": len(pages)}
    for idx in sorted(set(by_idx) - set(range(len(entries)))):
        errors.append(f"ledger has unit idx {idx} outside the manifest (size {len(entries)})")

    for idx, rel in enumerate(entries):
        units = by_idx.get(idx, [])
        if not units:
            errors.append(f"[{idx}] {rel!r}: not present on any page and not listed as skipped")
            continue
        bad_path = [u for u in units if u[1]["path"] != rel]
        if bad_path:
            errors.append(f"[{idx}] {rel!r}: ledger path mismatch {bad_path[0][1]['path']!r}")
            continue
        kind_m = mat.get(rel, {}).get("kind", "raw")
        content_path = mat.get(rel, {}).get("unit") if kind_m == "inferred" else None
        if content_path in ("", "-"):
            content_path = None
        read_from = content_path or os.path.join(source_root, rel)
        statuses = {u[1]["status"] for u in units}

        if "skipped" in statuses:
            if len(units) != 1:
                errors.append(f"[{idx}] {rel!r}: skipped unit appears {len(units)} times / mixed with parts")
                continue
            page, r, _ = units[0]
            if not r.get("reason"):
                errors.append(f"[{idx}] {rel!r}: skipped without a reason")
            # Cross-check the claimed reason against the file itself.
            text, err = (None, None) if kind_m == "referential" else _read_text(read_from)
            claim = r["content"]
            if kind_m == "referential":
                ok = claim == "referential"
            elif claim == "unreadable":
                ok = err is not None
            elif claim == "binary":
                ok = (text is not None and any(_is_non_text(c) for c in text[:8000])
                      and not any(ln.strip() and not _is_binary_line(ln) for ln in text.split("\n")))
            else:
                ok = False
            if not ok:
                errors.append(f"[{idx}] {rel!r}: claimed skip reason {claim!r} not confirmed by the file")
            skipped_rows.append((idx, rel, claim, r.get("reason", ""), page))
            stats["skipped_files"] += 1
            continue

        recs = sorted(units, key=lambda u: u[1]["part"])
        n = recs[0][1]["parts"]
        parts = [u[1]["part"] for u in recs]
        if any(u[1]["parts"] != n for u in recs) or parts != list(range(1, n + 1)):
            errors.append(f"[{idx}] {rel!r}: parts {parts} of {n} — each part must appear exactly once")
            continue
        total = recs[0][1]["total_lines"]
        content_kind = recs[0][1]["content"]
        # Contiguity: lines 1..total, segments of an over-cap line in order 1..S.
        expect_line, open_seg, problems = 1, None, []
        rebuilt: list[str] = []
        for page, r, texts in recs:
            if r["seg"]:
                if open_seg is None:
                    if r["line_start"] != expect_line or r["seg"] != 1:
                        problems.append(f"part {r['part']} starts line {r['line_start']} seg {r['seg']}, expected line {expect_line} seg 1")
                    open_seg = (r["line_start"], 1, r["segs"])
                    rebuilt.append(texts[0] if texts else "")
                else:
                    line, s, segs = open_seg
                    if r["line_start"] != line or r["seg"] != s + 1 or r["segs"] != segs:
                        problems.append(f"part {r['part']} is seg {r['seg']} of line {r['line_start']}, expected seg {s + 1} of line {line}")
                    open_seg = (line, s + 1, segs)
                    rebuilt[-1] += texts[0] if texts else ""
                if open_seg[1] == open_seg[2]:
                    expect_line, open_seg = open_seg[0] + 1, None
            else:
                if open_seg is not None:
                    problems.append(f"line {open_seg[0]} segments incomplete ({open_seg[1]}/{open_seg[2]})")
                    open_seg = None
                if r["line_start"] != expect_line:
                    problems.append(f"part {r['part']} starts at line {r['line_start']}, expected {expect_line} (gap/overlap)")
                rebuilt.extend(texts)
                expect_line = r["line_end"] + 1
        if open_seg is not None:
            problems.append(f"line {open_seg[0]} segments incomplete")
        if expect_line - 1 != total:
            problems.append(f"emitted lines end at {expect_line - 1}, file has {total}")
        if len(rebuilt) != total:
            problems.append(f"rebuilt {len(rebuilt)} lines from page bodies, expected {total}")
        # Content: re-read independently and compare line-by-line.
        if content_kind in ("source", "lifted"):
            text, err = _read_text(read_from)
            if text is None:
                problems.append(f"cannot re-read for verification: {err}")
            else:
                if content_kind == "source" and any(_is_non_text(c) for c in text[:8000]):
                    problems.append("emitted as plain source but file contains binary content")
                src_lines = text.split("\n")
                if len(src_lines) != total:
                    problems.append(f"file now has {len(src_lines)} lines, ledger says {total}")
                elif src_lines != rebuilt:
                    first = next(j for j, (a, b) in enumerate(zip(src_lines, rebuilt)) if a != b)
                    problems.append(f"emitted text differs from source at line {first + 1}")
        elif content_kind == "cleaned":
            text, err = _read_text(read_from)
            if text is None or not any(_is_non_text(c) for c in text[:8000]):
                problems.append("marked binary-cleaned but the file has no binary content")
        else:
            problems.append(f"unknown content kind {content_kind!r}")
        if problems:
            errors.append(f"[{idx}] {rel!r}: " + "; ".join(problems))
            continue
        stats["emitted_files"] += 1
        stats["parts"] += n
        stats["lines"] += total

    report = {"ok": not errors, "stats": stats, "errors": errors,
              "skipped": [{"idx": i, "path": p, "kind": k, "reason": r, "page": pg}
                          for i, p, k, r, pg in skipped_rows]}
    with open(pages_dir / "skipped_manifest.tsv", "w", encoding="utf-8") as fh:
        fh.write("idx\tpath\tkind\treason\tpage\n")
        for row in skipped_rows:
            fh.write("\t".join(str(x) for x in row) + "\n")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pages", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--materialized", default=os.environ.get("MATERIALIZED_MANIFEST") or None)
    ap.add_argument("--page-byte-cap", type=int,
                    default=int(os.environ.get("CONCAT_PAGE_BYTE_CAP", "380000")))
    ap.add_argument("--report", default=None)
    a = ap.parse_args(argv)
    pages_dir = Path(a.pages)
    if not pages_dir.is_dir() or not Path(a.manifest).is_file():
        print("check-page-conservation: --pages must be a directory and --manifest a file", file=sys.stderr)
        return 2
    report = check(pages_dir, Path(a.manifest), a.source_root, a.materialized, a.page_byte_cap)
    out = Path(a.report) if a.report else pages_dir / "conservation_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    s = report["stats"]
    if report["ok"]:
        print(f"Conservation OK: {s['entries']} entries = {s['emitted_files']} emitted "
              f"({s['parts']} units, {s['lines']} lines) + {s['skipped_files']} skipped "
              f"across {s['pages']} pages → {out}")
        return 0
    print(f"Conservation FAILED: {len(report['errors'])} violation(s) → {out}", file=sys.stderr)
    for e in report["errors"][:20]:
        print(f"  - {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

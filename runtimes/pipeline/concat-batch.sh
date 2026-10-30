#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Concat Batch Tool — Specification Distillation
# ─────────────────────────────────────────────────────────────────────────────
# Concatenates source files from a manifest into a single document with
# delimiters, filling up to a token budget and a hard byte cap. Writes the
# page plus a per-unit ledger that check-page-conservation.py verifies.
#
# Usage:
#   ./concat-batch.sh <source_root> <manifest_file> <output_dir> [offset] [token_budget]
#
# Arguments:
#   source_root    — root path to source files
#   manifest_file  — text file with one relative path per line (the queue)
#   output_dir     — where to write concat output + included list
#   offset         — cursor to start from (default: 0). Either "N" (start of the
#                    N-th manifest entry, 0-based, blank lines ignored) or "N:K"
#                    (part K, 0-based, of entry N). Callers treat it as opaque and
#                    pass back the value of next_offset.txt.
#   token_budget   — max estimated tokens to include (default: 600000)
#
# Work units: every manifest entry is one or more UNITS. A file whose numbered
# block fits the page byte cap AND the token budget is one unit; otherwise it is
# split into parts at line boundaries ([PART k/n]), and a single line that alone
# exceeds the limits is hard-split into LINE_SEGMENT parts. Every unit fits an
# empty page by construction. A page emits whole units only; a unit that does
# not fit is deferred whole and next_offset points at it ("N:K"), so the next
# page resumes exactly where this one stopped. Sizes are measured on the
# NUMBERED text that is actually written.
#
# Unreadable files and pure-binary files are NOT dropped: they are emitted as a
# marker-only block (KIND: unreadable|binary REASON: ...) and listed in
# skipped.tsv with the reason. Referential files (materialized manifest) are
# marker-only by design and are also recorded in skipped.tsv.
#
# Outputs:
#   <output_dir>/concat.txt        — concatenated source with [FILE: path] headers
#   <output_dir>/included.txt      — distinct paths with a block on this page
#   <output_dir>/next_offset.txt   — cursor for the next batch ("N" or "N:K")
#   <output_dir>/stats.txt         — "units_emitted total_bytes est_tokens remaining_files"
#   <output_dir>/units.jsonl       — one JSON record per emitted unit (ledger)
#   <output_dir>/skipped.tsv       — idx<TAB>path<TAB>reason for marker-only units
#   <output_dir>/complexity.txt    — page complexity score
#
# Environment (optional):
#   MATERIALIZED_MANIFEST   — ingest_helpers TSV (raw|inferred|referential)
#   CONCAT_PAGE_BYTE_CAP    — hard cap on concat.txt bytes (default 380000)
#   CONCAT_MAX_PART_BYTES   — cap on one unit's numbered body (default 370000)
#
# Token estimation: chars/2 (conservative for code)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SOURCE_ROOT="${1:?Usage: concat-batch.sh <source_root> <manifest_file> <output_dir> [offset] [token_budget]}"
MANIFEST_FILE="${2:?}"
OUTPUT_DIR="${3:?}"
OFFSET="${4:-0}"
TOKEN_BUDGET="${5:-600000}"

mkdir -p "${OUTPUT_DIR}"

# Arguments are passed through argv (quoted heredoc) — never interpolated into Python source.
exec python3 - "${SOURCE_ROOT}" "${MANIFEST_FILE}" "${OUTPUT_DIR}" "${OFFSET}" "${TOKEN_BUDGET}" <<'PYEOF'
import csv as _csv
import json
import os
import re
import sys

source_root, manifest_file, output_dir, offset_arg, budget_arg = sys.argv[1:6]

m = re.fullmatch(r"\s*(\d+)(?::(\d+))?\s*", offset_arg)
if not m:
    sys.exit(f"concat-batch: invalid offset {offset_arg!r} (expected N or N:K)")
file_offset, part_offset = int(m.group(1)), int(m.group(2) or 0)
if not re.fullmatch(r"\s*\d+\s*", budget_arg) or int(budget_arg) <= 0:
    sys.exit(f"concat-batch: invalid token_budget {budget_arg!r}")
token_budget = int(budget_arg)

PAGE_BYTE_CAP = int(os.environ.get("CONCAT_PAGE_BYTE_CAP", "380000"))   # kiro-cli read limit is 400KB
MAX_PART_BYTES = int(os.environ.get("CONCAT_MAX_PART_BYTES", "370000"))
# Worst-case header/end-marker overhead is reserved per unit from the byte cap (computed per file).
# Tokens are estimated as len(numbered_body)//2, so a unit body may hold at most 2*budget chars.
# A floor keeps progress possible for absurdly small budgets (each unit still fits an empty page).
CHAR_CAP = max(2 * token_budget, 256)

with open(manifest_file, encoding="utf-8") as f:
    all_paths = [line.strip() for line in f if line.strip()]

# Optional MATERIALIZED manifest (from ingest_helpers.py): rel_path -> content-fidelity metadata.
# TSV rows: path, kind(raw|inferred|referential), unit_path, unit_size, source_size, method, conf, notes.
#   raw          -> read + number the source file; marker KIND: raw
#   inferred     -> read + number the CACHED LIFTED TEXT (derived artifact); KIND: inferred VIA: <method>.
#                   Line numbers index the DERIVED artifact, not the (binary) source.
#   referential  -> marker-only (no body): file exists, no extractor lifted it.
# Absent (env unset) -> every file is treated raw (backward-compatible).
mat = {}
_mat_path = os.environ.get("MATERIALIZED_MANIFEST", "")
if _mat_path and os.path.exists(_mat_path):
    with open(_mat_path, newline="", encoding="utf-8") as _mf:
        for row in _csv.reader(_mf, delimiter="\t"):
            if len(row) >= 6:
                mat[row[0]] = {"kind": row[1], "unit": row[2], "source_size": row[4], "method": row[5]}


def _marker_attrs(rel):
    """Return (marker suffix, kind, content_path) for a rel_path."""
    mm = mat.get(rel)
    if not mm:
        return " KIND: raw", "raw", None
    kind = mm["kind"]
    suffix = f" KIND: {kind} SIZE: {mm['source_size']}"
    if kind == "inferred" and mm.get("method"):
        suffix += f" VIA: {mm['method']}"
    content_path = mm["unit"] if kind == "inferred" and mm.get("unit") not in ("", "-") else None
    return suffix, kind, content_path


def _clean_reason(s):
    # Reasons live inside a [FILE: ...] marker: no brackets/newlines/tabs allowed.
    return re.sub(r"[\[\]\r\n\t]+", " ", str(s)).strip() or "unspecified"


def _is_non_text(c, allow_nl=True):
    return c == "\ufffd" or (ord(c) < 32 and c not in ("\n\r\t" if allow_nl else "\r\t"))


def _load(rel):
    """Return ("text", content, content_kind, suffix) or ("skip", kind, reason, suffix)."""
    suffix, kind, content_path = _marker_attrs(rel)
    if kind == "referential":
        return ("skip", "referential", "referential: booked by materialized manifest, no lifted text", suffix)
    read_from = content_path if (kind == "inferred" and content_path) else os.path.join(source_root, rel)
    try:
        with open(read_from, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except (OSError, IOError) as e:
        return ("skip", "unreadable", f"unreadable: {type(e).__name__}: {e.strerror or e}", "")
    if kind == "inferred":
        return ("text", content, "lifted", suffix)
    content_kind = "source"
    if content:
        sample = content[:8000]
        if any(_is_non_text(c) for c in sample):
            # Mixed file: keep readable text lines, collapse binary runs into a placeholder line.
            cleaned, binary_run, has_text = [], 0, False
            for line in content.split("\n"):
                bad = sum(1 for c in line if _is_non_text(c, allow_nl=False))
                if len(line) > 0 and bad / len(line) > 0.30:
                    binary_run += len(line.encode("utf-8", errors="replace"))
                else:
                    if binary_run > 0:
                        cleaned.append(f"[{binary_run} bytes of binary data]")
                        binary_run = 0
                    if line.strip():
                        cleaned.append(line)
                        has_text = True
            if binary_run > 0:
                cleaned.append(f"[{binary_run} bytes of binary data]")
            if not has_text:
                return ("skip", "binary", "binary: no readable text", "")
            content = "\n".join(cleaned)
            content_kind = "cleaned"
    return ("text", content, content_kind, suffix)


def _numbered(i, text):
    return f"{i:>5} | {text}"


def _split_segments(prefix, text, byte_cap, char_cap):
    """Hard-split one over-cap line into chunks whose numbered form fits both caps."""
    avail_b = byte_cap - len(prefix.encode("utf-8"))
    avail_c = char_cap - len(prefix)
    if avail_b < 4 or avail_c < 1:
        raise SystemExit("concat-batch: page cap too small for a line prefix")
    out, pos = [], 0
    while pos < len(text):
        chunk = text[pos:pos + avail_c]
        b = chunk.encode("utf-8")
        if len(b) > avail_b:
            chunk = b[:avail_b].decode("utf-8", errors="ignore")  # never split a multibyte char
        out.append(chunk)
        pos += len(chunk)
    return out


def _plan(rel, content, suffix):
    """Split content into units. Each unit: dict(ls, le, seg, segs, body)."""
    lines = content.split("\n")
    # Reserve the worst-case marker overhead so every unit's block fits an empty page.
    worst_hdr = f"[FILE: {rel} [PART 9999999/9999999]{suffix} ORIGINAL_LINE_START: 999999999 LINE_SEGMENT: 9999999/9999999]\n"
    worst_end = f"\n[END FILE: {rel} [PART 9999999/9999999]]\n"
    overhead = len((worst_hdr + worst_end).encode("utf-8"))
    byte_cap = min(MAX_PART_BYTES, PAGE_BYTE_CAP - overhead)
    if byte_cap < 64:
        raise SystemExit(f"concat-batch: CONCAT_PAGE_BYTE_CAP too small for marker of {rel!r}")
    numbered = [_numbered(i, ln) for i, ln in enumerate(lines, start=1)]
    whole = "\n".join(numbered)
    if len(whole.encode("utf-8")) <= byte_cap and len(whole) <= CHAR_CAP:
        return [dict(ls=1, le=len(lines), seg=0, segs=0, body=whole)]
    units, cur, cur_b, cur_c, cur_start = [], [], 0, 0, 1

    def flush(end_line):
        nonlocal cur, cur_b, cur_c
        if cur:
            units.append(dict(ls=cur_start, le=end_line, seg=0, segs=0, body="\n".join(cur)))
        cur, cur_b, cur_c = [], 0, 0

    for i, nl in enumerate(numbered, start=1):
        nb, nc = len(nl.encode("utf-8")), len(nl)
        if nb > byte_cap or nc > CHAR_CAP:
            flush(i - 1)
            prefix = f"{i:>5} | "
            chunks = _split_segments(prefix, lines[i - 1], byte_cap, CHAR_CAP)
            for s, ch in enumerate(chunks, start=1):
                units.append(dict(ls=i, le=i, seg=s, segs=len(chunks), body=prefix + ch))
            cur_start = i + 1
            continue
        sep = 1 if cur else 0
        if cur and (cur_b + sep + nb > byte_cap or cur_c + sep + nc > CHAR_CAP):
            flush(i - 1)
            cur_start = i
            sep = 0
        if not cur:
            cur_start = i
        cur.append(nl)
        cur_b += sep + nb
        cur_c += sep + nc
    flush(len(lines))
    return units


def _units_for(idx):
    rel = all_paths[idx]
    loaded = _load(rel)
    if loaded[0] == "skip":
        _, kind, reason, suffix = loaded
        reason = _clean_reason(reason)
        if kind == "referential":
            hdr = f"[FILE: {rel}{suffix}]"
        else:
            hdr = f"[FILE: {rel} KIND: {kind} REASON: {reason}]"
        blk = f"{hdr}\n[END FILE: {rel}]\n"
        rec = dict(idx=idx, path=rel, part=1, parts=1, line_start=0, line_end=0, seg=0, segs=0,
                   status="skipped", content=kind, total_lines=0, reason=reason)
        return [(blk, len(blk) // 2, rec)]
    _, content, content_kind, suffix = loaded
    plan = _plan(rel, content, suffix)
    total_lines = content.count("\n") + 1
    n = len(plan)
    out = []
    for k, u in enumerate(plan, start=1):
        if n == 1:
            blk = f"[FILE: {rel}{suffix}]\n{u['body']}\n[END FILE: {rel}]\n"
        else:
            label = f"{rel} [PART {k}/{n}]"
            seg_attr = f" LINE_SEGMENT: {u['seg']}/{u['segs']}" if u["seg"] else ""
            blk = (f"[FILE: {label}{suffix} ORIGINAL_LINE_START: {u['ls']}{seg_attr}]\n"
                   f"{u['body']}\n[END FILE: {label}]\n")
        rec = dict(idx=idx, path=rel, part=k, parts=n, line_start=u["ls"], line_end=u["le"],
                   seg=u["seg"], segs=u["segs"], status="emitted", content=content_kind,
                   total_lines=total_lines, reason="")
        out.append((blk, len(u["body"]) // 2, rec))
    return out


total_files = len(all_paths)
blocks, records = [], []
page_bytes = 0
current_tokens = 0
cursor = (file_offset, part_offset)
i, k = file_offset, part_offset

while i < total_files:
    units = _units_for(i)
    if k >= len(units):
        sys.exit(f"concat-batch: offset {i}:{k} out of range — {all_paths[i]!r} has {len(units)} part(s); "
                 "the source changed since the previous page was built. Rebuild all pages.")
    stop = False
    while k < len(units):
        blk, toks, rec = units[k]
        add = len(blk.encode("utf-8")) + (1 if blocks else 0)  # pages are "\n".join(blocks)
        if blocks and (page_bytes + add > PAGE_BYTE_CAP or current_tokens + toks > token_budget):
            stop = True
            break
        blocks.append(blk)
        records.append(rec)
        page_bytes += add
        current_tokens += toks
        k += 1
    if stop:
        break
    i, k = i + 1, 0

next_offset = f"{i}:{k}" if k else str(i)
remaining = total_files - i
total_bytes = page_bytes

included = []
for r in records:
    if r["path"] not in included:
        included.append(r["path"])

with open(os.path.join(output_dir, "concat.txt"), "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(blocks))
with open(os.path.join(output_dir, "included.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(included) + "\n" if included else "")
with open(os.path.join(output_dir, "next_offset.txt"), "w") as f:
    f.write(next_offset + "\n")
with open(os.path.join(output_dir, "stats.txt"), "w") as f:
    f.write(f"{len(records)} {total_bytes} {current_tokens} {remaining}\n")
with open(os.path.join(output_dir, "units.jsonl"), "w", encoding="utf-8") as f:
    for r in records:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
with open(os.path.join(output_dir, "skipped.tsv"), "w", encoding="utf-8") as f:
    for r in records:
        if r["status"] == "skipped":
            f.write(f"{r['idx']}\t{r['path']}\t{r['reason']}\n")

# Page complexity score (0.0 = simple, 1.0 = dense logic)
logic_indicators = 0
total_lines = 0
for part in blocks:
    for line in part.split("\n"):
        total_lines += 1
        stripped = line.strip().lower()
        if any(kw in stripped for kw in ['def ', 'class ', 'function ', 'if ', 'elif ', 'else:', 'switch', 'case ', 'require', 'permit', 'assert', 'raise ', 'throw ']):
            logic_indicators += 1
complexity = min(1.0, logic_indicators / max(1, total_lines) * 10)
with open(os.path.join(output_dir, "complexity.txt"), "w") as f:
    f.write(f"{complexity:.3f}\n")

print(f"Concat: {len(records)} units, {len(included)} files, {total_bytes} bytes, ~{current_tokens} tokens, {remaining} remaining")
PYEOF

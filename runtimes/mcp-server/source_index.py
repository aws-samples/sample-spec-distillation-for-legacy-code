#!/usr/bin/env python3
"""
source_index.py — Post-distillation source file indexer.

Builds a mapping from concept blocks back to their source files,
enabling evidence links in the viewer (e.g., "v3107_..." → GitHub URL).

Runs after distillation is complete. No LLM calls — purely deterministic
string matching against page manifests.

Pipeline Phase 6.5 (after viewer generation).

Usage:
    python source_index.py <pages_dir> <vector_db_path> <source_repo_url>

Example:
    python source_index.py \
      output/.pages \
      output/stills/shared_vectors.db \
      https://github.com/<org>/<repo>/blob/main
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

from code_graph import marker_path as _mp  # canonical [FILE:] path parse (strips KIND/SIZE/VIA)


# ── Build page → files mapping ───────────────────────────────────────────────


def build_page_file_index(pages_dir: str) -> dict[str, list[str]]:
    """Scan all page concat files and extract file paths per page.

    Looks for [FILE: path/to/file] markers in each page's concat.txt.

    Returns:
        Dict of page_name → [file_path, file_path, ...]
    """
    pages_path = Path(pages_dir)
    index = {}

    for page_dir in sorted(pages_path.glob("page_*")):
        concat_file = page_dir / "concat.txt"
        if not concat_file.exists():
            continue

        page_name = page_dir.name  # e.g., "page_042"
        content = concat_file.read_text(encoding="utf-8")

        # Extract [FILE: ...] markers (strip KIND/SIZE/VIA/PART attributes → bare paths)
        files = [_mp(inner) for inner in
                 re.findall(r'^\[FILE:\s*(.+?)\]\s*$', content, re.MULTILINE)]
        if files:
            index[page_name] = files

    return index


# ── Match evidence strings to source files ───────────────────────────────────


def match_evidence_to_files(
    evidence_text: str,
    page_files: list[str],
) -> list[dict]:
    """Match an evidence string against file paths from a page.

    Uses substring matching: if any part of the evidence string
    matches a filename (without path), it's a hit.

    Returns:
        List of {file_path, match_score} dicts.
    """
    matches = []
    # Extract potential file/migration identifiers from evidence
    # Common patterns: v3107_name, filename.py, module.name
    tokens = re.findall(r'[a-zA-Z0-9_\-\.]{6,}', evidence_text)

    for file_path in page_files:
        filename = Path(file_path).stem  # e.g., "v3107_costmodel_budget_line_idempotency"
        for token in tokens:
            if token.lower() in filename.lower() or filename.lower() in token.lower():
                matches.append({
                    "file_path": file_path,
                    "matched_token": token,
                    "score": 1.0 if token.lower() == filename.lower() else 0.7,
                })
                break

    return matches


# ── Build full source index ──────────────────────────────────────────────────


def build_source_index(
    pages_dir: str,
    vector_db_path: str,
    source_repo_url: str,
) -> dict:
    """Build source reference index and store in vector DB.

    1. Scans pages for file markers → page_file_index
    2. For each block in vector DB, looks up its source_page → files
    3. Matches evidence text in block body against file names
    4. Stores results in a source_refs table

    Args:
        pages_dir: Path to .pages/ directory with page concat files.
        vector_db_path: Path to shared_vectors.db.
        source_repo_url: Base URL for source links (e.g., https://github.com/org/repo/blob/main)

    Returns:
        Stats dict.
    """
    # Step 1: Build page → files index
    page_index = build_page_file_index(pages_dir)
    total_files = sum(len(f) for f in page_index.values())
    print(f"  Page index: {len(page_index)} pages, {total_files} file references")

    if not page_index:
        return {"error": "No page files found", "pages_scanned": 0}

    # Step 2: Open vector DB and create source_refs table
    conn = sqlite3.connect(vector_db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_refs (
            block_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            matched_token TEXT,
            score REAL,
            url TEXT,
            PRIMARY KEY (block_id, file_path)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source_refs_block ON source_refs(block_id)")

    # Also store the page_file_index for direct lookup
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_files (
            page_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_index INTEGER,
            PRIMARY KEY (page_name, file_path)
        )
    """)
    # Clear existing data (idempotent rebuild)
    conn.execute("DELETE FROM source_refs")
    conn.execute("DELETE FROM page_files")

    # Insert page_file_index
    for page_name, files in page_index.items():
        for idx, fp in enumerate(files):
            conn.execute(
                "INSERT OR IGNORE INTO page_files (page_name, file_path, file_index) VALUES (?, ?, ?)",
                (page_name, fp, idx),
            )

    # Step 3: For each block, find source file matches
    blocks = conn.execute(
        "SELECT block_id, source_page, body FROM vectors WHERE body != ''"
    ).fetchall()

    refs_found = 0
    blocks_with_refs = 0

    for block_id, source_page, body in blocks:
        page_files = page_index.get(source_page, [])
        if not page_files:
            continue

        # Always link the block to all files on its source page
        # (the block was extracted from content on that page)
        # But also try to match specific evidence references
        matches = match_evidence_to_files(body, page_files)

        if not matches:
            # No specific evidence match — link to all page files as context
            # (too noisy for large pages, skip this)
            continue

        blocks_with_refs += 1
        for m in matches:
            url = f"{source_repo_url.rstrip('/')}/{m['file_path']}"
            conn.execute(
                "INSERT OR IGNORE INTO source_refs (block_id, file_path, matched_token, score, url) VALUES (?, ?, ?, ?, ?)",
                (block_id, m["file_path"], m["matched_token"], m["score"], url),
            )
            refs_found += 1

    conn.commit()
    conn.close()

    print(f"  Blocks analyzed: {len(blocks)}")
    print(f"  Blocks with source refs: {blocks_with_refs}")
    print(f"  Total source references: {refs_found}")

    return {
        "pages_scanned": len(page_index),
        "total_source_files": total_files,
        "blocks_analyzed": len(blocks),
        "blocks_with_refs": blocks_with_refs,
        "refs_found": refs_found,
        "source_repo_url": source_repo_url,
    }


# ── Query helpers (for use by query_spec and viewer) ─────────────────────────


def get_source_refs(vector_db_path: str, block_id: str) -> list[dict]:
    """Get source file references for a block."""
    conn = sqlite3.connect(vector_db_path)
    rows = conn.execute(
        "SELECT file_path, matched_token, score, url FROM source_refs WHERE block_id = ?",
        (block_id,),
    ).fetchall()
    conn.close()
    return [{"file_path": r[0], "token": r[1], "score": r[2], "url": r[3]} for r in rows]


def get_page_files(vector_db_path: str, page_name: str) -> list[str]:
    """Get all source files for a given page."""
    conn = sqlite3.connect(vector_db_path)
    rows = conn.execute(
        "SELECT file_path FROM page_files WHERE page_name = ? ORDER BY file_index",
        (page_name,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python source_index.py <pages_dir> <vector_db_path> <source_repo_url>")
        sys.exit(1)

    result = build_source_index(sys.argv[1], sys.argv[2], sys.argv[3])
    print(f"\nResult: {json.dumps(result, indent=2)}")

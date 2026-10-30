#!/usr/bin/env python3
"""
normalize_output.py — Post-Phase-2 output normalization (immutable).

The distill-worker agent (LLM) writes artifacts to unpredictable locations.
This script enforces the expected layout before downstream phases run.

Expected structure per still after Phase 2:
  stills/{still}/
    {still}_raw_concat.md          ← required (the zero-loss concat)
    partials/page_NNN/{still}_extract.md  ← map partials
    {still}_dedup_index.json       ← dedup overlay (optional)
    README.md                      ← agent summary (optional)

Anything the agent created that conflicts with Phase 2.5+ is MOVED to
.backup/{timestamp}/ for auditing — never deleted.

Usage:
    python normalize_output.py <stills_dir>
"""

import shutil
import sys
import time
from pathlib import Path


# Patterns to relocate from the still root (agent-created, conflicts with Phase 2.5)
RELOCATE_PATTERNS = [
    "knowledge_graph.json",
    "*_vectors.db",
    "shared_vectors.db",
    "vectors.db",
    "kg/",
    "*_kg_build/",
]


def _backup_path(stills_path: Path) -> Path:
    """Create a timestamped backup directory under .backup/."""
    backup_dir = stills_path / ".backup" / time.strftime("%Y%m%d_%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def normalize_stills(stills_dir: str) -> dict:
    """Normalize all still directories after Phase 2.

    Moves agent-created artifacts to .backup/ so Phase 2.5+ starts clean.
    Validates that required artifacts exist.

    Returns:
        Stats dict with relocated counts and validation results.
    """
    stills_path = Path(stills_dir)
    stats = {"stills_found": 0, "relocated": 0, "valid": 0, "invalid": []}
    backup_dir = None  # Lazily created only if we need it

    def ensure_backup():
        nonlocal backup_dir
        if backup_dir is None:
            backup_dir = _backup_path(stills_path)
        return backup_dir

    # Relocate top-level shared_vectors.db only if knowledge phase hasn't run yet
    # (if .complete markers exist in final/ dirs, this DB is a real artifact, not agent junk)
    top_db = stills_path / "shared_vectors.db"
    if top_db.exists():
        # Check if any still has completed knowledge phase — if so, this DB is legit
        has_knowledge = any(
            (d / "final" / ".complete").exists()
            for d in stills_path.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        if not has_knowledge:
            dest = ensure_backup() / "shared_vectors.db"
            shutil.move(str(top_db), str(dest))
            stats["relocated"] += 1
            print(f"  Backed up stale: {top_db.name} → .backup/")
        else:
            print(f"  Keeping: {top_db.name} (knowledge phase completed)")

    for still_dir in sorted(stills_path.iterdir()):
        if not still_dir.is_dir():
            continue
        # Skip non-still directories
        if still_dir.name.startswith(".") or still_dir.name.endswith("_worker.log"):
            continue

        still_name = still_dir.name
        stats["stills_found"] += 1

        # Check for required artifact
        raw_concat = still_dir / f"{still_name}_raw_concat.md"
        if not raw_concat.exists():
            stats["invalid"].append(still_name)
            continue

        # Relocate agent-created artifacts that conflict with Phase 2.5
        still_backup = None
        for pattern in RELOCATE_PATTERNS:
            if pattern.endswith("/"):
                for d in still_dir.glob(pattern.rstrip("/")):
                    if d.is_dir():
                        if still_backup is None:
                            still_backup = ensure_backup() / still_name
                            still_backup.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(d), str(still_backup / d.name))
                        stats["relocated"] += 1
                        print(f"  [{still_name}] Backed up dir: {d.name}/ → .backup/")
            else:
                for f in still_dir.glob(pattern):
                    if f.is_file():
                        if still_backup is None:
                            still_backup = ensure_backup() / still_name
                            still_backup.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(f), str(still_backup / f.name))
                        stats["relocated"] += 1
                        print(f"  [{still_name}] Backed up: {f.name} → .backup/")

        stats["valid"] += 1

    if backup_dir:
        print(f"\n  Backup location: {backup_dir}")

    return stats


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python normalize_output.py <stills_dir>")
        sys.exit(1)

    print(f"Normalizing: {sys.argv[1]}")
    result = normalize_stills(sys.argv[1])
    print(f"\nResult: {result['stills_found']} stills, {result['valid']} valid, "
          f"{result['relocated']} artifacts relocated to .backup/")
    if result["invalid"]:
        print(f"  Missing raw_concat: {result['invalid']}")

#!/usr/bin/env python3
"""
classify_still_output.py — Signal/Forensics bifurcation module.

Importable classification logic for splitting formatted still output into:
  - signal/   — Specification content (BizRel >= 2, or BizRel = 1 but not anti-finding)
  - forensics/ — Audit trail (anti-findings, naming_conflicts.md, excluded_terms.md)

This module is imported by format_still.py for post-format classification,
and also available as a standalone CLI via the kiro-cli version.

Immutable data model: never modifies or deletes original formatted/ files.
"""

import re
from pathlib import Path


# ── Anti-finding patterns (case-insensitive match in concept text) ────────────

ANTI_FINDING_PATTERNS = [
    "no business workflows present",
    "no content to format",
    "no content extractable",
    "binary report files",
    "no extractable business",
    "no business logic",
    "no actors, triggers, or actions",
    "no business rules",
    "purely presentation-layer",
    "third-party library",
    "no business processes recoverable",
    "unreadable binary",
    "unparseable binary",
    "naming-convention inference only",
    "not a verifiable rule",
    "not a verified rule",
]

# Files that are always forensics regardless of content
FORENSICS_FILES = {"naming_conflicts.md", "excluded_terms.md"}


# ── Classification logic ─────────────────────────────────────────────────────


def is_anti_finding(concept_text: str) -> bool:
    """Check if a concept matches any anti-finding pattern."""
    text_lower = concept_text.lower()
    for pattern in ANTI_FINDING_PATTERNS:
        if pattern in text_lower:
            return True
    return False


def get_business_relevance(concept_text: str) -> int:
    """Extract Business Relevance score from concept text. Returns 0 if not found."""
    match = re.search(r"Business Relevance:\s*(\d+)", concept_text)
    if match:
        return int(match.group(1))
    return 0


def classify_concept(concept_text: str, source_filename: str) -> str:
    """Classify a single concept as 'signal' or 'forensics'.

    Returns:
        'forensics' if:
          - source file is naming_conflicts.md or excluded_terms.md
          - Business Relevance = 1 AND matches anti-finding pattern
        'signal' otherwise (default)
    """
    if source_filename in FORENSICS_FILES:
        return "forensics"

    biz_rel = get_business_relevance(concept_text)

    if biz_rel == 1 and is_anti_finding(concept_text):
        return "forensics"

    return "signal"


def split_file_into_parts(content: str) -> tuple:
    """Split a markdown file into preamble and concepts.

    Returns:
        (preamble, concepts) where:
          - preamble = everything before the first ### header
          - concepts = list of concept strings (each starting with ### )
    """
    lines = content.split("\n")
    preamble_lines = []
    concepts = []
    current_concept_lines = []
    in_concept = False

    for line in lines:
        if line.startswith("### "):
            if in_concept and current_concept_lines:
                concepts.append("\n".join(current_concept_lines))
            current_concept_lines = [line]
            in_concept = True
        elif in_concept:
            current_concept_lines.append(line)
        else:
            preamble_lines.append(line)

    # Last concept
    if in_concept and current_concept_lines:
        concepts.append("\n".join(current_concept_lines))

    preamble = "\n".join(preamble_lines)
    return preamble, concepts


def write_classified_file(
    output_path: Path, preamble: str, concepts: list, category: str
):
    """Write a classified output file with updated BLOCK_MANIFEST.

    Only writes if concepts is non-empty.
    """
    if not concepts:
        return

    count = len(concepts)
    updated_preamble = re.sub(
        r"<!--\s*BLOCK_MANIFEST:\s*count=\d+",
        f"<!-- BLOCK_MANIFEST: count={count}",
        preamble,
    )

    parts = [updated_preamble.rstrip()]
    parts.append("")
    for concept in concepts:
        parts.append(concept)
        parts.append("")

    parts.append(f"<!-- END_MANIFEST: expected={count} -->")
    parts.append("")
    parts.append(f"*{count} entries documented.*")
    parts.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts), encoding="utf-8")


def classify_still(still_dir: Path) -> dict:
    """Classify all formatted files in a single still.

    Args:
        still_dir: Path to a still directory (e.g., stills/workflow/)

    Returns:
        Stats dict with status, total_concepts, signal, forensics, signal_pct.
    """
    if isinstance(still_dir, str):
        still_dir = Path(still_dir)

    formatted_dir = still_dir / "final" / "formatted"
    if not formatted_dir.exists():
        return {"status": "skipped", "reason": "no formatted dir"}

    md_files = sorted(f for f in formatted_dir.glob("*.md") if not f.name.startswith("."))
    if not md_files:
        return {"status": "skipped", "reason": "no .md files"}

    signal_dir = formatted_dir / "signal"
    forensics_dir = formatted_dir / "forensics"

    total_concepts = 0
    signal_count = 0
    forensics_count = 0

    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8")
        preamble, concepts = split_file_into_parts(content)

        if not concepts:
            continue

        signal_concepts = []
        forensics_concepts = []

        for concept in concepts:
            category = classify_concept(concept, md_file.name)
            if category == "forensics":
                forensics_concepts.append(concept)
            else:
                signal_concepts.append(concept)

        total_concepts += len(concepts)
        signal_count += len(signal_concepts)
        forensics_count += len(forensics_concepts)

        if signal_concepts:
            write_classified_file(
                signal_dir / md_file.name, preamble, signal_concepts, "signal"
            )

        if forensics_concepts:
            write_classified_file(
                forensics_dir / md_file.name, preamble, forensics_concepts, "forensics"
            )

    signal_pct = (signal_count / total_concepts * 100) if total_concepts > 0 else 0

    return {
        "status": "ok",
        "total_concepts": total_concepts,
        "signal": signal_count,
        "forensics": forensics_count,
        "signal_pct": round(signal_pct, 1),
    }

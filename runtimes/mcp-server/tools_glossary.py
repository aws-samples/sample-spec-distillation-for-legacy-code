#!/usr/bin/env python3
"""
tools_glossary.py — decode the domain vocabulary of a legacy system.

WHY THIS IS THE HIGHEST-VALUE COMPREHENSION TOOL
------------------------------------------------
The main barrier to reading unfamiliar enterprise code is not logic, it is naming.
`APInvHdr`, `RetgAmt`, `LienWvrRcvd`, `VendGrp` mean nothing until someone explains
them, and that explanation usually lives only in a colleague's head. The
domain_language still already recovers exactly that mapping — business term, plain
definition, and the code identifiers it appears as — so exposing it turns "I need to
ask someone" into a lookup.

The decisive field is `Aliases`: it lists the code-side spellings of a business
concept, so this tool is indexed in BOTH directions. Ask for "AP Invoice" and get the
definition; ask for "APInvHdr" and get the same entry, which is the question an agent
reading source actually has.

Reads the domain_language still's materialized markdown. No model calls, no credentials.
"""
from __future__ import annotations

import re
from pathlib import Path

import graph_query
from spec_tooling import err, ok, quiet

# All three files share one shape: "### Title" then "- Field: value" lines.
_SOURCES = {
    "concept_registry.md": "registry",
    "domain_map.md": "domain_map",
    "naming_conflicts.md": "naming_conflict",
}
_FIELDS_KEPT = ("definition", "aliases", "evidence", "attributes", "relationships",
                "statement", "confidence", "business relevance")


def _parse_blocks(path: Path, kind: str) -> list[dict]:
    """Split a materialized still file into its ### blocks with their - Field: values."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    out = []
    for chunk in re.split(r"(?m)^### ", text)[1:]:
        lines = chunk.splitlines()
        title = lines[0].strip()
        fields: dict[str, str] = {}
        for ln in lines[1:]:
            m = re.match(r"\s*-\s*([A-Za-z][A-Za-z ]*?):\s*(.*)$", ln)
            if m:
                key = m.group(1).strip().lower()
                if key in _FIELDS_KEPT:
                    fields[key] = m.group(2).strip()
        aliases = [a.strip() for a in re.split(r",(?![^(]*\))", fields.get("aliases", ""))
                   if a.strip()]
        out.append({
            "term": title,
            "source": kind,
            "definition": fields.get("definition", ""),
            "aliases": aliases,
            "evidence": [e.strip() for e in fields.get("evidence", "").split(",") if e.strip()],
            "attributes": fields.get("attributes", ""),
            "relationships": fields.get("relationships", ""),
            "statement": fields.get("statement", ""),
            "confidence": fields.get("confidence", ""),
        })
    return out


def _load(run_dir: str) -> tuple[list[dict], str]:
    """Return (entries, error). Error is non-empty when the glossary is unavailable."""
    paths = graph_query.resolve_paths(run_dir)
    final = Path(paths["stills_dir"]) / "domain_language" / "final"
    if not final.is_dir():
        return [], (
            "this run has no domain_language still output, so there is no glossary. "
            "Re-run with 'domain_language' in DISTILL_STILLS."
        )
    entries: list[dict] = []
    for fname, kind in _SOURCES.items():
        entries.extend(_parse_blocks(final / fname, kind))
    if not entries:
        return [], f"domain_language output found at {final} but no concept blocks parsed"
    return entries, ""


def _matches(entry: dict, needle: str) -> bool:
    """Match on the business term OR any code-side alias, substring, case-insensitive."""
    n = needle.lower()
    if n in entry["term"].lower():
        return True
    return any(n in a.lower() for a in entry["aliases"])


def register(server):
    @server.tool(
        description=(
            "Decode the domain vocabulary of the analysed system: business terms, plain "
            "definitions, and the cryptic code identifiers each one appears as. Free; no "
            "AWS credentials needed.\n\n"
            "Use this when you hit an identifier you do not understand. It is indexed "
            "BOTH ways: `term='AP Invoice'` returns the definition, and "
            "`term='APInvHdr'` returns the same entry via its alias list — so you can "
            "look up a name you found in source.\n\n"
            "Omit `term` to list the whole vocabulary (use `limit`). Entries from "
            "`naming_conflicts` are terms the extraction found used inconsistently — the "
            "same name for different things, or several names for one thing — which is "
            "institutional history worth reading, not noise.\n\n"
            "Requires `run_dir` of a run that included the `domain_language` still."
        )
    )
    @quiet
    async def get_glossary(run_dir: str, term: str = "", limit: int = 25) -> str:
        """Look up domain terms and their code aliases."""
        try:
            entries, problem = _load(run_dir)
        except graph_query.RunNotUsable as e:
            return err(str(e), run_dir=run_dir)
        except Exception as e:
            return err(f"{type(e).__name__}: {e}", run_dir=run_dir)
        if problem:
            return err(problem, run_dir=run_dir, terms=None)

        hits = [e for e in entries if _matches(e, term)] if term else entries
        # Exact term match first, then alias-only matches, so the obvious answer leads.
        if term:
            hits.sort(key=lambda e: (term.lower() not in e["term"].lower(), e["term"]))

        return ok({
            "run_dir": run_dir,
            "term": term,
            "total_terms": len(entries),
            "matched": len(hits),
            "returned": min(limit, len(hits)),
            "conflicts_present": any(e["source"] == "naming_conflict" for e in hits),
            "terms": hits[:limit],
        })

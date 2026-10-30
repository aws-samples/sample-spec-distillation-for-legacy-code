#!/usr/bin/env python3
"""
concept_symbols.py — Extract code-symbol references from business concept blocks.

Bridges the business concept layer to the code-dependency graph. Each concept's
formal Statement + Aliases reference code identifiers (proc names like
MyProcName, dotted fields like entity.some_field, bracketed [Some Table],
snake_case terms). We extract those tokens so a concept can be anchored to real
code_objects, and conversely a code object can be mapped back to the concept
blocks that reference it.

The extraction is heuristic (token shapes), but precision comes from JOINING the
extracted tokens against actual code_objects.object names in the code graph — a
token only counts as a code anchor if it matches a real code object.

Produces two indexes:
  by_concept: {concept_key: {"title","still","stem","canonical_key","symbols":[...]}}
  by_symbol:  {symbol_lower: [concept_key, ...]}

concept_key = "{still}::{title}" (title includes the [XX-NNNN] prefix) — the FORMATTED-still
spelling, kept as the index key for backward compatibility (migrate_concept_ids.py and
dependency_query.py bind to it).

Every entry ALSO carries `canonical_key` — the one shared key defined in concept_key.py, with the
assigned id and the `"<Type>: "` prefix stripped exactly once. The concept graph derives the same
canonical key from its RAW `### ` header, so the two artifacts agree on a key verbatim instead of
being matched through a ladder of normalisations. export_graph.py puts the CANONICAL key on every
bridge link; see concept_key.py for why.
"""

import re

import concept_key as ckey

# Formal-notation keywords to exclude from symbol extraction
_STOPWORDS = {
    "require", "permit", "prohibit", "deny", "forbid", "warn", "default",
    "enable", "reject", "flag", "derive", "and", "or", "not", "where", "if",
    "then", "else", "when", "for", "across", "within", "every", "in", "is",
    "implies", "including", "case", "end", "null", "true", "false", "unique",
    "exists", "all", "any", "must", "the", "each", "with", "from", "into",
    "before", "after", "via", "per", "on", "of", "to", "by", "as",
}

# Identifier token patterns (ordered; each contributes candidate symbols)
_PAT_DOTTED = re.compile(r'\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b')
_PAT_BRACKET = re.compile(r'\[([A-Za-z0-9_][A-Za-z0-9_ ]*?)\]')
_PAT_CAMEL = re.compile(r'\b[a-zA-Z]+[A-Z][A-Za-z0-9]{2,}\b')
_PAT_SNAKE = re.compile(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b')


def extract_symbols(statement_text, aliases_text=""):
    """Extract candidate code symbols from a concept's Statement + Aliases.

    Returns a de-duplicated list of symbol strings (original case preserved).
    """
    symbols = []
    seen = set()

    def _add(tok):
        t = tok.strip().strip('.').strip()
        if not t or len(t) < 3:
            return
        if t.lower() in _STOPWORDS:
            return
        key = t.lower()
        if key not in seen:
            seen.add(key)
            symbols.append(t)

    blob = statement_text + "\n" + aliases_text
    for m in _PAT_DOTTED.finditer(blob):
        _add(m.group(0))
    for m in _PAT_BRACKET.finditer(blob):
        _add(m.group(1))
    for m in _PAT_CAMEL.finditer(blob):
        _add(m.group(0))
    for m in _PAT_SNAKE.finditer(blob):
        _add(m.group(0))
    return symbols


def _split_concepts(content):
    lines = content.split("\n")
    concepts = []
    current = None
    for line in lines:
        if line.startswith("### "):
            if current is not None:
                concepts.append("\n".join(current))
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        concepts.append("\n".join(current))
    return concepts


def _concept_title(concept_text):
    first = concept_text.split("\n", 1)[0]
    # "### [RL-0001] Rule: ..." -> "[RL-0001] Rule: ..."
    return first[4:].strip() if first.startswith("### ") else first.strip()


def _field_value(concept_text, label):
    """Return the value(s) of a '- Label:' field, including a following fenced
    block for Statement. Best-effort, single field."""
    lines = concept_text.split("\n")
    out = []
    capturing = False
    in_fence = False
    for line in lines:
        if not capturing:
            if line.startswith(f"- {label}:"):
                out.append(line.split(":", 1)[1].strip())
                capturing = True
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(stripped)
            continue
        # stop at next top-level field
        if re.match(r'^- [A-Z][^:]*:', line):
            break
        if line.strip() == "" and not in_fence:
            # allow blank within, but a blank then next field handled above
            continue
        out.append(stripped)
    return "\n".join(out)


def build_index(stills_dir, subdir="signal"):
    """Scan all formatted concept files and build the symbol indexes.

    Returns {"by_concept": {...}, "by_symbol": {...}}.
    """
    from pathlib import Path
    stills = Path(stills_dir)
    by_concept = {}
    by_symbol = {}

    for still_dir in sorted(stills.iterdir()):
        if not still_dir.is_dir():
            continue
        still = still_dir.name
        fmt = still_dir / "final" / "formatted" / subdir
        if not fmt.exists():
            continue
        for md in sorted(fmt.glob("*.md")):
            stem = md.stem
            content = md.read_text(encoding="utf-8")
            for c in _split_concepts(content):
                title = _concept_title(c)
                statement = _field_value(c, "Statement")
                aliases = _field_value(c, "Aliases")
                syms = extract_symbols(statement, aliases)
                if not syms:
                    continue
                key = f"{still}::{title}"
                by_concept[key] = {
                    "title": title, "still": still, "stem": stem, "symbols": syms,
                    # the ONE shared key (concept_key.py); the concept graph derives the same
                    # string from its raw header, so a bridge link resolves by equality.
                    "canonical_key": ckey.concept_key(still, title),
                }
                for s in syms:
                    by_symbol.setdefault(s.lower(), []).append(key)

    return {"by_concept": by_concept, "by_symbol": by_symbol,
            "key_schema": {"index_key": "{still}::{formatted title incl. [XX-NNNN] id}",
                           "canonical_key": "{still}::{canonical_title}",
                           "canonical_key_source": "runtimes/mcp-server/concept_key.py"}}


if __name__ == "__main__":
    import argparse, json, sys
    from pathlib import Path
    ap = argparse.ArgumentParser(description="Build concept->symbol index from formatted stills.")
    ap.add_argument("stills_dir")
    ap.add_argument("--out", default="", help="Output JSON path (default: <stills_dir>/concept_symbols.json)")
    ap.add_argument("--subdir", default="signal")
    args = ap.parse_args()
    idx = build_index(args.stills_dir, subdir=args.subdir)
    out = args.out or str(Path(args.stills_dir) / "concept_symbols.json")
    Path(out).write_text(json.dumps(idx), encoding="utf-8")
    print(f"concepts with symbols: {len(idx['by_concept'])}", flush=True)
    print(f"distinct symbols: {len(idx['by_symbol'])}", flush=True)
    print(f"written: {out}", flush=True)

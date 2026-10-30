#!/usr/bin/env python3
"""
symbol_backmap.py — Back-mapping ledger: synthesized concept identifiers -> the
source-faithful tokens they were derived from.

WHY THIS EXISTS
---------------
The map/synth/refine phases author formal Statements using canonical/idealized
identifiers (e.g. `entity.expiry_date`, `some_exclusion_rule`) that are
SEMANTICALLY faithful to the source but LEXICALLY paraphrased — the real source
token may be `expiresOn`. That paraphrase is invisible and breaks source-faithful
anchoring (the code-graph bridge, traceability, audit). This module builds a
ledger that maps every synthesized symbol back to the observed source token, with
provenance, so:
  1. the bridge can anchor on the source-faithful `observed_value`, and
  2. any synthesized identifier with NO source token behind it is flagged
     `coined_unverified` — a hallucination detector, per the no-made-up-signal tenet.

ANTI-HALLUCINATION CONTRACT
---------------------------
An `observed_value` is recorded ONLY when the token is actually present in one of
the block's cited source files. It is NEVER invented. When no source token is
found deterministically, the row is written with observed_value=NULL and
transform_type='coined_unverified' (verdict deferred to an optional LLM residue
pass — this module is deterministic and makes no LLM call).

SCHEMA  (written into shared_vectors.db)
----------------------------------------
symbol_backmap(
  id PK, block_key, still, new_value,
  observed_value, observed_source, observed_line,
  transform_type,        -- verbatim | normalized | tail_match | coined_unverified
  rationale, confidence,  -- our derived confidence (0..1)
  resolver,               -- 'deterministic' (this module) | 'llm' (residue pass)
  ts
)

This module is deterministic (no LLM, no network). CLI at bottom.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import concept_symbols as cs  # reuse block parsing + symbol extraction
from code_graph import marker_path as _mp  # canonical [FILE:] path parse (strips KIND/SIZE/VIA)

# ── source tokenizer (generous: catch camel/snake/dotted/kebab/bracket/plain) ──
_TOK = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
                  r"|[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+")


def _norm(s: str) -> str:
    """Separator/case-insensitive normal form (does NOT stem — a semantic
    rename like expiresOn<->expiry_date will NOT match, by design)."""
    return re.sub(r"[ _.\-]", "", s.lower())


def _cited_files(block_text: str) -> list[str]:
    """File paths / filenames the block cites (Evidence + Aliases + Enforcement).
    Returns candidate path strings (may be full repo paths, or bare filenames)."""
    ev = cs._field_value(block_text, "Evidence")
    al = cs._field_value(block_text, "Aliases")
    en = cs._field_value(block_text, "Enforcement")
    blob = "\n".join([ev, al, en])
    cands = set()
    # file#Lnnn or bare paths / filenames with a known code/doc extension
    for m in re.finditer(r"[\w./\-]+\.(?:cs|sql|ps1|psm1|md|json|py|js|ts|xml|rpt|dot|yml|yaml|jsonl)", blob):
        cands.add(m.group(0))
    return sorted(cands)


def index_pages(pages_dir: str) -> dict:
    """One pass over pages → locator: path_lower -> (page_file, start, end) for
    fully-contained [FILE:]..[END FILE]/next-[FILE:] segments. Also basename map.
    Cheap: stores only offsets, not text."""
    locator = {}
    by_base = {}
    file_re = re.compile(r"\[FILE:\s*(.+?)\]")
    for cf in sorted(Path(pages_dir).glob("page_*/concat.txt")):
        lines = cf.read_text(errors="replace").split("\n")
        cur_path = None
        cur_start = None
        for i, ln in enumerate(lines):
            m = file_re.match(ln)
            if m:
                # close previous open segment (fully-contained: bounded by next marker)
                if cur_path is not None:
                    _register(locator, by_base, cur_path, str(cf), cur_start, i)
                cur_path = _mp(m.group(1))  # strip KIND/SIZE/VIA/PART attributes → bare path
                cur_start = i + 1
            elif ln.startswith("[END FILE"):
                if cur_path is not None:
                    _register(locator, by_base, cur_path, str(cf), cur_start, i)
                    cur_path = None
                    cur_start = None
        # trailing open segment is skipped (may be truncated at page boundary)
    return {"locator": locator, "by_base": by_base}


def _register(locator, by_base, path, page_file, start, end):
    key = path.lower()
    if key not in locator:  # first fully-contained wins
        locator[key] = (page_file, start, end)
        base = path.rsplit("/", 1)[-1].lower()
        by_base.setdefault(base, []).append(key)


def _load_segment(page_file: str, start: int, end: int) -> list[tuple[int, str]]:
    """Return [(source_line_no, text), ...] for a page segment, stripping the
    'NNNN | ' line-number prefix the pages embed."""
    lines = Path(page_file).read_text(errors="replace").split("\n")
    out = []
    for raw in lines[start:end]:
        m = re.match(r"\s*(\d+)\s*\|\s?(.*)$", raw)
        if m:
            out.append((int(m.group(1)), m.group(2)))
        else:
            out.append((0, raw))
    return out


def _resolve(pages_index, cited: str):
    """Resolve a cited path/filename to a page segment locator key."""
    loc = pages_index["locator"]
    key = cited.lower()
    if key in loc:
        return key
    base = cited.rsplit("/", 1)[-1].lower()
    hits = pages_index["by_base"].get(base, [])
    return hits[0] if hits else None


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbol_backmap(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_key TEXT, still TEXT, new_value TEXT,
            observed_value TEXT, observed_source TEXT, observed_line INTEGER,
            transform_type TEXT, rationale TEXT, confidence REAL,
            resolver TEXT, ts REAL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_backmap_block ON symbol_backmap(block_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_backmap_new ON symbol_backmap(new_value)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_backmap_obs ON symbol_backmap(observed_value)")
    conn.commit()


def _match_symbol(sym: str, obs_tokens: dict):
    """obs_tokens: normalized_token -> (orig_token, line). Try full then dotted tail.
    Returns (observed_value, observed_line, transform_type) or None."""
    ns = _norm(sym)
    if ns in obs_tokens:
        orig, line = obs_tokens[ns]
        tt = "verbatim" if orig.lower() == sym.lower() else "normalized"
        return orig, line, tt
    if "." in sym:
        tail = sym.rsplit(".", 1)[-1]
        nt = _norm(tail)
        if len(nt) >= 3 and nt in obs_tokens:
            orig, line = obs_tokens[nt]
            return orig, line, "tail_match"
    return None


def build(stills_dir: str, pages_dir: str, db_path: str, subdir: str = "signal") -> dict:
    print("[backmap] indexing pages...", flush=True)
    pages_index = index_pages(pages_dir)
    print(f"[backmap] indexed {len(pages_index['locator']):,} fully-contained source files", flush=True)

    conn = sqlite3.connect(db_path)
    ensure_table(conn)

    # segment text cache (path_key -> obs_tokens dict) to avoid re-reading
    seg_cache: dict[str, dict] = {}

    def _obs_for(cited_files: list[str]) -> tuple[dict, list[str]]:
        """Union of observed tokens across a block's cited files (with lines)."""
        obs: dict[str, tuple[str, int]] = {}
        resolved_paths = []
        for cf in cited_files:
            k = _resolve(pages_index, cf)
            if not k:
                continue
            resolved_paths.append(k)
            if k not in seg_cache:
                page_file, s, e = pages_index["locator"][k]
                toks = {}
                for lineno, text in _load_segment(page_file, s, e):
                    for m in _TOK.finditer(text):
                        t = m.group(0)
                        nt = _norm(t)
                        if len(nt) >= 3 and nt not in toks:
                            toks[nt] = (t, lineno)
                seg_cache[k] = toks
            # merge (first occurrence wins)
            for nt, v in seg_cache[k].items():
                obs.setdefault(nt, v)
        return obs, resolved_paths

    stills = Path(stills_dir)
    counts = {"blocks": 0, "symbols": 0, "verbatim": 0, "normalized": 0,
              "tail_match": 0, "coined_unverified": 0, "no_cited_source": 0,
              "rows": 0}
    now = time.time()
    batch = []

    still_dirs = [d for d in sorted(stills.iterdir()) if d.is_dir()]
    for si, still_dir in enumerate(still_dirs):
        still = still_dir.name
        fmt = still_dir / "final" / "formatted" / subdir
        if not fmt.exists():
            continue
        for md in sorted(fmt.glob("*.md")):
            content = md.read_text(encoding="utf-8")
            for block in cs._split_concepts(content):
                title = cs._concept_title(block)
                statement = cs._field_value(block, "Statement")
                aliases = cs._field_value(block, "Aliases")
                syms = cs.extract_symbols(statement, aliases)
                if not syms:
                    continue
                counts["blocks"] += 1
                block_key = f"{still}::{title}"
                cited = _cited_files(block)
                obs, resolved = _obs_for(cited) if cited else ({}, [])
                src_label = resolved[0] if resolved else None
                for sym in syms:
                    counts["symbols"] += 1
                    hit = _match_symbol(sym, obs) if obs else None
                    if hit:
                        observed, line, tt = hit
                        counts[tt] += 1
                        # find which resolved file the token came from (best-effort: src_label)
                        batch.append((block_key, still, sym, observed, src_label, line,
                                      tt, f"{tt} match in cited source", 0.9,
                                      "deterministic", now))
                    else:
                        tt = "coined_unverified"
                        if not cited:
                            counts["no_cited_source"] += 1
                        counts[tt] += 1
                        batch.append((block_key, still, sym, None, src_label, None,
                                      tt, ("no cited source file" if not cited
                                           else "not found in cited source (semantic rename or coined)"),
                                      0.0, "deterministic", now))
                if len(batch) >= 5000:
                    _flush(conn, batch); counts["rows"] += len(batch); batch = []
        print(f"[backmap] still {si+1}/{len(still_dirs)} '{still}': "
              f"blocks={counts['blocks']} symbols={counts['symbols']} "
              f"attested={counts['verbatim']+counts['normalized']+counts['tail_match']} "
              f"coined={counts['coined_unverified']}", flush=True)
    if batch:
        _flush(conn, batch); counts["rows"] += len(batch)
    conn.commit()
    conn.close()

    attested = counts["verbatim"] + counts["normalized"] + counts["tail_match"]
    counts["attested"] = attested
    counts["attested_rate"] = round(attested / counts["symbols"], 4) if counts["symbols"] else None
    print(f"[backmap] DONE: {counts}", flush=True)
    return counts


def _flush(conn, batch):
    conn.executemany(
        "INSERT INTO symbol_backmap(block_key,still,new_value,observed_value,"
        "observed_source,observed_line,transform_type,rationale,confidence,resolver,ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
    conn.commit()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Build the symbol back-mapping ledger (deterministic).")
    ap.add_argument("stills_dir")
    ap.add_argument("pages_dir")
    ap.add_argument("db_path")
    ap.add_argument("--subdir", default="signal")
    ap.add_argument("--rebuild", action="store_true", help="DROP existing symbol_backmap first")
    args = ap.parse_args()
    if args.rebuild:
        c = sqlite3.connect(args.db_path)
        c.execute("DROP TABLE IF EXISTS symbol_backmap")
        c.commit(); c.close()
        print("[backmap] dropped existing symbol_backmap", flush=True)
    build(args.stills_dir, args.pages_dir, args.db_path, subdir=args.subdir)

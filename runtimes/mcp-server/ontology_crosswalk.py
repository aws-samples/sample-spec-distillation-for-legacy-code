"""Concept identity crosswalk — the HOT PATCH (interim, transitional).

Two-island defect (System SYSTEM_SPEC.md §8; repave = R-id-vending-6): the same logical
concept has two handles —
  Island A (embedding/graph): vectors.block_id / uc_  (raw titles)
  Island B (id/format/bridge): ids.db issued_id ([DT-0001]) / symbol_backmap / concept_symbols
                               (formatted titles, keyed "still::[ID] title")
— and no persisted crosswalk joins them. This module reconstructs a BEST-EFFORT, REVERSIBLE
crosswalk so ontology-classification can reach the code-symbol co-location axis on an existing
(pre-repave) artifact.

Deliberately NOT gold-plated. It is a pothole patch: it must keep traffic moving and it must
be reversible (non-destructive on a store COPY; original substrate retained), but it is not
held to completeness. Residue is kept-and-flagged (L3c), never a gate. The real fix is
R-id-vending-6 (one canonical identity at generation time), which makes this module moot.

Join key that survives both islands: (still, normalized title-core). Island B titles carry a
"[XX-NNNN]" issued-id prefix + formatting; Island A titles are raw. We normalize both to a
title-core and match within a still. Deterministic exact-core match is the tier we trust;
everything else is residue left flagged (a reasoning/LLM residue tier can be added later IF
this patch ever needs to be better — but per the proportionality decision, it should not be
over-built while the repave is the real answer).

Writes a `concept_crosswalk` table into the WORKING COPY only:
    uc_id | issued_id | block_key | still | match_tier | confidence | evidence

CLI:
    python ontology_crosswalk.py <wip_db> <stills_dir> --dry-run
    python ontology_crosswalk.py <wip_db> <stills_dir> --commit
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import datetime as _dt
from collections import defaultdict
from pathlib import Path

ISSUED_ID_RE = re.compile(r"\[([A-Z]{2,3}-\d{4})\]")


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def normalize_core(title: str) -> str:
    """Strip a leading [XX-NNNN] issued-id, collapse whitespace, casefold. The shared join
    key across islands. Intentionally simple (patch-grade)."""
    t = ISSUED_ID_RE.sub("", title or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t.casefold()


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS concept_crosswalk (
            uc_id       TEXT,           -- island A: unified concept (or block_id if ungrouped)
            issued_id   TEXT,           -- island B: [DT-0001] style
            block_key   TEXT,           -- island B: "still::[ID] title" (symbol index key)
            still       TEXT,
            match_tier  TEXT NOT NULL,  -- 'deterministic_core' | 'residue_flagged'
            confidence  REAL,
            evidence    TEXT,           -- JSON
            created_at  TEXT NOT NULL,
            PRIMARY KEY (uc_id, block_key)
        );
        CREATE INDEX IF NOT EXISTS idx_crosswalk_uc ON concept_crosswalk(uc_id);
        CREATE INDEX IF NOT EXISTS idx_crosswalk_issued ON concept_crosswalk(issued_id);
        """
    )
    conn.commit()


def load_island_b(stills_dir: Path):
    """block_key -> (still, issued_id, title_core, symbols) from concept_symbols.json."""
    cs = json.load(open(stills_dir / "concept_symbols.json"))
    out = {}
    for bkey, meta in cs.get("by_concept", {}).items():
        title = meta.get("title", "")
        still = meta.get("still", bkey.split("::", 1)[0] if "::" in bkey else "")
        m = ISSUED_ID_RE.search(title) or ISSUED_ID_RE.search(bkey)
        issued = m.group(1) if m else None
        out[bkey] = {
            "still": still,
            "issued_id": issued,
            "core": normalize_core(title),
            "symbols": meta.get("symbols") or [],
        }
    return out


def load_island_a(conn: sqlite3.Connection):
    """Island A concept-level rows: prefer uc_ (post-unification), keyed by (still, title-core).

    We map each unified_concept to a (still, core). unified_concepts.title is raw; source_stills
    is a JSON list — we use the first still as the primary (patch-grade; most concepts are
    single-still in this corpus)."""
    rows = []
    for uc, title, stills_json in conn.execute(
        "SELECT concept_id, title, source_stills FROM unified_concepts"
    ):
        still = None
        try:
            sl = json.loads(stills_json) if stills_json else []
            still = sl[0] if sl else None
        except (json.JSONDecodeError, TypeError):
            still = None
        rows.append((uc, still, normalize_core(title)))
    return rows


def reconstruct(conn: sqlite3.Connection, stills_dir: Path):
    """Return (matches, residue_a, residue_b, stats). Deterministic (still, core) exact match."""
    island_b = load_island_b(stills_dir)
    # index island B by (still, core) -> list of block_keys
    b_by_key = defaultdict(list)
    for bkey, m in island_b.items():
        b_by_key[(m["still"], m["core"])].append(bkey)

    island_a = load_island_a(conn)
    matches = []
    matched_b = set()
    residue_a = 0
    for uc, still, core in island_a:
        cands = b_by_key.get((still, core), [])
        if len(cands) == 1:
            bkey = cands[0]
            m = island_b[bkey]
            matches.append({
                "uc_id": uc, "issued_id": m["issued_id"], "block_key": bkey, "still": still,
                "match_tier": "deterministic_core", "confidence": 1.0,
                "evidence": {"join": "still+title_core_exact_unique"},
            })
            matched_b.add(bkey)
        elif len(cands) > 1:
            # ambiguous: multiple island-B concepts share the core in this still -> residue (L3c)
            residue_a += 1
        else:
            residue_a += 1
    residue_b = [bk for bk in island_b if bk not in matched_b]
    stats = {
        "island_a_count": len(island_a),
        "island_b_count": len(island_b),
        "deterministic_matches": len(matches),
        "residue_a_unmatched_or_ambiguous": residue_a,
        "residue_b_unmatched": len(residue_b),
        "match_rate_a": round(len(matches) / len(island_a), 4) if island_a else None,
        "match_rate_b": round(len(matches) / len(island_b), 4) if island_b else None,
    }
    return matches, residue_a, residue_b, stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Concept identity crosswalk hot-patch (interim, reversible)")
    ap.add_argument("db_path", help="shared_vectors WORKING COPY (never the baseline in place)")
    ap.add_argument("stills_dir")
    ap.add_argument("--dry-run", action="store_true", help="report rates; write nothing")
    ap.add_argument("--commit", action="store_true", help="write concept_crosswalk into the working copy")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db_path)
    try:
        matches, residue_a, residue_b, stats = reconstruct(conn, Path(args.stills_dir))
        report = {"stats": stats, "examples": matches[:5]}
        if not args.commit:
            report["mode"] = "dry-run (nothing written)"
            print(json.dumps(report, indent=2, sort_keys=True))
            return
        ensure_table(conn)
        now = _now()
        for m in matches:
            conn.execute(
                "INSERT OR REPLACE INTO concept_crosswalk "
                "(uc_id,issued_id,block_key,still,match_tier,confidence,evidence,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (m["uc_id"], m["issued_id"], m["block_key"], m["still"], m["match_tier"],
                 m["confidence"], json.dumps(m["evidence"], sort_keys=True), now),
            )
        conn.commit()
        report["mode"] = "committed"
        report["rows_written"] = len(matches)
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

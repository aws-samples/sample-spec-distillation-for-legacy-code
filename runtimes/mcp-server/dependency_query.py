#!/usr/bin/env python3
"""
dependency_query.py — Answer "what does this business concept depend on?"

Bridges the business concept layer to the factual code-dependency graph:

  business concept  --(its symbols)-->  code objects (anchors)
       code objects  --(code_edges)-->  dependency closure (calls/reads/writes)
       closure objects  --(by_symbol)-->  other business concept blocks

Given a concept (identified by its symbols), returns:
  - requires  (upstream): code objects/concepts this concept's code depends on
                          (procs it calls, tables it reads/writes)
  - required_by (downstream): code objects/concepts that depend on it

Each edge carries direction (read/write/call), type, and a confidence derived
by the code-graph guards. Related code objects are mapped back to the business
concept blocks that reference them (via the concept-symbol index) and to their
business decoding (via code_entities), so the answer is expressed in business
terms, not just raw object names.
"""

import sqlite3
from collections import defaultdict


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def code_graph_ready(db_path):
    """True if the code-dependency tables are populated."""
    conn = sqlite3.connect(db_path)
    try:
        if not _table_exists(conn, "code_edges") or not _table_exists(conn, "code_objects"):
            return False
        return conn.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0] > 0
    finally:
        conn.close()


def query_dependencies(db_path, sym_index, concept_key, per_object_cap=8, limit=60):
    """Compute upstream (requires) and downstream (required_by) dependencies
    for a concept identified by concept_key ("{still}::{title}").

    sym_index: the concept-symbol index dict (by_concept / by_symbol).
    Returns a structured dict.
    """
    cinfo = sym_index["by_concept"].get(concept_key)
    if not cinfo:
        return {"error": "concept not found in symbol index", "concept": concept_key,
                "resolved": False, "requires": None, "required_by": None}
    symbols = cinfo["symbols"]

    conn = sqlite3.connect(db_path)
    if not _table_exists(conn, "code_edges"):
        conn.close()
        return {"error": "code dependency graph not built yet", "concept": concept_key,
                "symbols": symbols,
                "resolved": False, "requires": None, "required_by": None}

    # ── Anchor concept symbols to real code objects ──
    obj_rows = conn.execute("SELECT DISTINCT object FROM code_objects").fetchall()
    objs_lower = {r[0].lower(): r[0] for r in obj_rows}
    # also allow anchoring on edge endpoints (objects referenced but not defined locally)
    for r in conn.execute("SELECT DISTINCT src FROM code_edges"):
        objs_lower.setdefault(r[0].lower(), r[0])
    for r in conn.execute("SELECT DISTINCT dst FROM code_edges"):
        objs_lower.setdefault(r[0].lower(), r[0])

    # ── Source-faithful fallback map (symbol_backmap.observed_value) ──
    # The map/synth/refine phases author paraphrased identifiers (e.g. `expiry_date`)
    # that will not match a code object, while the source-faithful token they denote
    # (`expiresOn`) will. The symbol back-map ledger records that alignment. We consult
    # it ONLY as a fallback when a symbol does not anchor directly — additive, never
    # removes an anchor. Scoped to this concept's still to avoid cross-still bleed.
    backmap = {}  # synthesized_symbol_lower -> source-faithful observed_value
    if _table_exists(conn, "symbol_backmap"):
        still_name = cinfo["still"]
        for nv, ov in conn.execute(
            "SELECT new_value, observed_value FROM symbol_backmap "
            "WHERE still=? AND observed_value IS NOT NULL", (still_name,)
        ):
            if nv and ov:
                backmap.setdefault(nv.lower(), ov)

    def _anchor_token(tok_lower):
        """Resolve a single lowercased token to a code object (direct or dotted tail)."""
        if tok_lower in objs_lower:
            return objs_lower[tok_lower]
        if "." in tok_lower:
            tail = tok_lower.rsplit(".", 1)[-1]
            if tail in objs_lower:
                return objs_lower[tail]
        return None

    anchors = []
    backmap_anchors = []  # observability: which anchors came via the back-map fallback
    for s in symbols:
        key = s.lower()
        hit = _anchor_token(key)
        if hit is None and key in backmap:
            # fallback: try the source-faithful token this symbol was derived from
            ov = backmap[key]
            hit = _anchor_token(ov.lower())
            if hit is not None:
                backmap_anchors.append({"symbol": s, "observed_value": ov, "object": hit})
        if hit is not None:
            anchors.append(hit)
    anchors = list(dict.fromkeys(anchors))  # dedup, keep order

    if not anchors:
        conn.close()
        # UNRESOLVED is not the same answer as "nothing depends on this", and a
        # programmatic caller (an agent deciding whether a change is safe) cannot tell
        # the two apart from empty lists. So requires/required_by are None, never [],
        # and `resolved` carries the distinction explicitly.
        return {"concept": concept_key, "symbols": symbols, "anchors": [],
                "resolved": False,
                "requires": None, "required_by": None,
                "reason": "no concept symbol anchored to a code object",
                "graph_scope": "cross-boundary references only; file-local symbols "
                               "(methods, members) are not modelled as graph nodes",
                "note": "No concept symbols matched a code object. Either the "
                        "code graph lacks these objects or the concept has no "
                        "code-level footprint."}

    # ── Entity decodings (code_name -> business_name) for labeling ──
    decode = {}
    if _table_exists(conn, "code_entities"):
        for cn, bn in conn.execute("SELECT code_name, business_name FROM code_entities"):
            if cn and bn:
                decode[cn.lower()] = bn

    def _rel_concepts(obj):
        """Business concept blocks that reference this code object."""
        keys = sym_index["by_symbol"].get(obj.lower(), [])
        # exclude self
        return [k for k in keys if k != concept_key][:per_object_cap]

    # ── Traverse edges from anchors ──
    requires = []       # anchor --calls/reads/writes--> dst
    required_by = []    # src --> anchor
    for a in anchors:
        for dst, etype, direction, conf in conn.execute(
            "SELECT dst, type, direction, derived_confidence FROM code_edges "
            "WHERE src = ? COLLATE NOCASE", (a,)
        ):
            requires.append({
                "via_object": a, "object": dst,
                "business_name": decode.get((dst or "").lower(), ""),
                "type": etype, "direction": direction or "",
                "confidence": round(conf or 0.0, 2),
                "concepts": _rel_concepts(dst),
            })
        for src, etype, direction, conf in conn.execute(
            "SELECT src, type, direction, derived_confidence FROM code_edges "
            "WHERE dst = ? COLLATE NOCASE", (a,)
        ):
            required_by.append({
                "via_object": a, "object": src,
                "business_name": decode.get((src or "").lower(), ""),
                "type": etype, "direction": direction or "",
                "confidence": round(conf or 0.0, 2),
                "concepts": _rel_concepts(src),
            })
    conn.close()

    # Rank by confidence desc, dedup by (object, direction)
    def _dedup_rank(rows):
        seen = {}
        for r in rows:
            k = (r["object"].lower(), r["direction"])
            if k not in seen or r["confidence"] > seen[k]["confidence"]:
                seen[k] = r
        return sorted(seen.values(), key=lambda r: r["confidence"], reverse=True)[:limit]

    result = {
        "concept": concept_key,
        "title": cinfo["title"],
        "still": cinfo["still"],
        "symbols": symbols,
        "anchors": anchors,
        "resolved": True,
        "requires": _dedup_rank(requires),
        "required_by": _dedup_rank(required_by),
    }
    if backmap_anchors:
        # Provenance: anchors recovered via the source-faithful back-map fallback.
        result["backmap_anchors"] = backmap_anchors
    return result


def query_by_title(db_path, sym_index, title, still=None, **kw):
    """Convenience: resolve a concept by (fuzzy) title, optionally scoped to a still."""
    # exact key first
    if still:
        key = f"{still}::{title}"
        if key in sym_index["by_concept"]:
            return query_dependencies(db_path, sym_index, key, **kw)
    # search by title match (strip [XX-NNNN] prefix for comparison)
    import re
    def norm(t):
        return re.sub(r'^\[[A-Z]+-\d+\]\s*', '', t).strip().lower()
    tnorm = norm(title)
    for key, info in sym_index["by_concept"].items():
        if still and info["still"] != still:
            continue
        if norm(info["title"]) == tnorm:
            return query_dependencies(db_path, sym_index, key, **kw)
    for key, info in sym_index["by_concept"].items():
        if norm(info["title"]).startswith(tnorm) or tnorm.startswith(norm(info["title"])):
            return query_dependencies(db_path, sym_index, key, **kw)
    return {"error": "concept not found", "title": title,
            "resolved": False, "requires": None, "required_by": None}


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description="Query dependencies for a business concept.")
    ap.add_argument("db_path")
    ap.add_argument("symbols_json", help="concept_symbols.json")
    ap.add_argument("--title", required=True)
    ap.add_argument("--still", default=None)
    args = ap.parse_args()
    sym_index = json.load(open(args.symbols_json))
    result = query_by_title(args.db_path, sym_index, args.title, still=args.still)
    print(json.dumps(result, indent=2))

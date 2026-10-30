#!/usr/bin/env python3
"""
graph_query.py — read-only lookup over a completed run's concept knowledge graph.

Answers "what do we know about X, and how well evidenced is it?" for a caller that
has a finished distillation output directory. Pure reads: SQLite + one JSON load, no
model calls, no AWS credentials, no mutation of the run.

TWO STORES, TWO PURPOSES
------------------------
A unified concept's own attributes live in the `unified_concepts` table of
shared_vectors.db. Its *relationships* do not — there is no concept-edge table, so
cross-still corroboration is only recoverable from knowledge_graph.json. We therefore
read both, and cache the JSON parse per path (it is ~0.5 MB and immutable for a
completed run).

CONVERGENCE IS THE POINT
------------------------
A concept found by one still is a reading; the same concept independently recovered by
several stills is evidence (strategy memo §13: "convergence tells you where to trust").
That signal is carried by `references` edges, which link the *observation blocks* that
contributed to unified concepts — not the unified concepts themselves. So computing it
means hopping: unified concept -> its contributor blocks -> their `references` edges ->
the stills on the far side.

`same_as` edges are the other kind: a within-still merge. We surface the resulting
`contributor_count` rather than hiding it, because a concept that is an aggregate of N
observations should be legible as such to the caller.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# knowledge_graph.json parse cache: path -> (nodes_by_id, edges)
_GRAPH_CACHE: dict[str, tuple[dict, list]] = {}


class RunNotUsable(Exception):
    """The run directory is missing an artifact this module needs."""


def resolve_paths(run_dir: str) -> dict:
    """Resolve a run directory to the artifacts we read.

    Prefers run_manager (which rebases `output:`-tagged paths onto wherever the run
    actually sits now, so a run copied off its build host still resolves). Falls back
    to layout convention when run.json is absent or unreadable — a run that was
    produced before the manifest existed is still queryable.
    """
    root = Path(run_dir).expanduser().resolve()
    if not root.is_dir():
        raise RunNotUsable(f"not a directory: {root}")

    vectors = stills = None
    try:
        import run_manager

        run_manager.load(str(root))
        vectors = Path(run_manager.resolve("output:stills/shared_vectors.db"))
        stills = Path(run_manager.resolve("output:stills"))
    except Exception:
        pass  # no run.json, or an older schema — fall through to convention

    if vectors is None or not vectors.is_file():
        stills = root / "stills"
        vectors = stills / "shared_vectors.db"

    if not vectors.is_file():
        raise RunNotUsable(
            f"no vector store at {vectors} — is {root} a completed distillation run?"
        )

    # Not in run.json's path registry; derived from the stills dir / run root.
    graph = stills / "knowledge_graph.json"
    if not graph.is_file():
        graph = root / "knowledge_graph" / "knowledge_graph.json"

    return {
        "run_dir": str(root),
        "vectors_db": str(vectors),
        "stills_dir": str(stills),
        "graph_json": str(graph) if graph.is_file() else "",
        "symbols_json": str(stills / "concept_symbols.json"),
    }


def _load_graph(graph_json: str) -> tuple[dict, list]:
    """Parse knowledge_graph.json into (nodes_by_id, edges), cached per path."""
    if not graph_json:
        return {}, []
    if graph_json in _GRAPH_CACHE:
        return _GRAPH_CACHE[graph_json]
    with open(graph_json, encoding="utf-8") as f:
        g = json.load(f)
    nodes = {n["id"]: n for n in g.get("nodes", [])}
    edges = g.get("edges", g.get("links", []))
    _GRAPH_CACHE[graph_json] = (nodes, edges)
    return nodes, edges


def _adjacency(nodes: dict, edges: list) -> dict:
    """block_id -> list of (edge_type, other_block_id, score), both directions."""
    adj: dict[str, list] = {}
    for e in edges:
        et = e.get("edge_type") or e.get("type") or ""
        if et not in ("references", "same_as"):
            continue  # CONTRIBUTES_TO is the block->concept hop, not corroboration
        s, d, sc = e.get("source"), e.get("target"), e.get("score")
        adj.setdefault(s, []).append((et, d, sc))
        adj.setdefault(d, []).append((et, s, sc))
    return adj


def _json_list(raw) -> list:
    """source_stills / contributor_ids are JSON-encoded lists in TEXT columns."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else [v]
    except (json.JSONDecodeError, TypeError):
        return [s.strip() for s in str(raw).split(",") if s.strip()]


def _convergence(contributor_ids: list, own_stills: list, nodes: dict, adj: dict) -> dict:
    """Cross-still corroboration for one unified concept.

    Walks each contributor block's `references` edges and collects the stills on the
    far side, excluding the concept's own still(s) — agreement only counts when it
    comes from an independently-run lens.
    """
    own = {s.lower() for s in own_stills}
    by_still: dict[str, float] = {}
    for bid in contributor_ids:
        for etype, other, score in adj.get(bid, []):
            if etype != "references":
                continue
            still = (nodes.get(other, {}).get("source_still") or "").lower()
            if not still or still in own:
                continue
            if score is not None and score > by_still.get(still, -1.0):
                by_still[still] = score
    ordered = sorted(by_still.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "count": len(ordered),
        "stills": [s for s, _ in ordered],
        "scores": [round(sc, 3) for _, sc in ordered],
    }


def _row_to_concept(row, nodes, adj, include_body) -> dict:
    (cid, title, btype, conf, n_contrib, stills_raw, pages_raw, contrib_raw,
     body_raw, body_synth) = row
    stills = _json_list(stills_raw)
    out = {
        "concept_id": cid,
        "title": title,
        "block_type": btype,
        "confidence": conf,
        "source_stills": stills,
        "source_pages": _json_list(pages_raw),
        "contributor_count": n_contrib,
        "convergence": _convergence(_json_list(contrib_raw), stills, nodes, adj),
    }
    if include_body:
        out["body"] = body_synth or body_raw or ""
    return out


_SELECT = """SELECT concept_id, title, block_type, confidence, contributor_count,
                    source_stills, source_pages, contributor_ids,
                    body_raw, body_synthesized
             FROM unified_concepts"""


def query(run_dir: str, query: str = "", block_type: str = "", still: str = "",
          limit: int = 10, include_body: bool = False) -> dict:
    """Find concepts by title substring, optionally filtered by type and still.

    Results are ordered by convergence (most independently-corroborated first), then
    confidence — so the most trustworthy finding leads rather than an arbitrary row.
    """
    paths = resolve_paths(run_dir)
    nodes, edges = _load_graph(paths["graph_json"])
    adj = _adjacency(nodes, edges)

    sql, args = _SELECT, []
    where = []
    if query:
        where.append("LOWER(title) LIKE ?")
        args.append(f"%{query.lower()}%")
    if block_type:
        where.append("LOWER(block_type) = ?")
        args.append(block_type.lower())
    if still:
        where.append("LOWER(source_stills) LIKE ?")
        args.append(f"%{still.lower()}%")
    if where:
        sql += " WHERE " + " AND ".join(where)

    conn = sqlite3.connect(paths["vectors_db"])
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    concepts = [_row_to_concept(r, nodes, adj, include_body) for r in rows]
    concepts.sort(key=lambda c: (c["convergence"]["count"], c["confidence"] or 0),
                  reverse=True)
    return {
        "run_dir": paths["run_dir"],
        "total_matched": len(concepts),
        "returned": min(limit, len(concepts)),
        "convergence_available": bool(paths["graph_json"]),
        "concepts": concepts[:limit],
    }


def get_concept(run_dir: str, concept_id: str) -> dict:
    """Full detail for one concept by id, including its body text."""
    paths = resolve_paths(run_dir)
    nodes, edges = _load_graph(paths["graph_json"])
    adj = _adjacency(nodes, edges)

    conn = sqlite3.connect(paths["vectors_db"])
    try:
        row = conn.execute(_SELECT + " WHERE concept_id = ?", (concept_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"error": "concept not found", "concept_id": concept_id}

    out = _row_to_concept(row, nodes, adj, include_body=True)
    out["run_dir"] = paths["run_dir"]
    return out


def stats(run_dir: str) -> dict:
    """Shape of the run: concept counts by type and by still, graph size."""
    paths = resolve_paths(run_dir)
    nodes, edges = _load_graph(paths["graph_json"])

    conn = sqlite3.connect(paths["vectors_db"])
    try:
        total = conn.execute("SELECT COUNT(*) FROM unified_concepts").fetchone()[0]
        by_type = dict(conn.execute(
            "SELECT block_type, COUNT(*) FROM unified_concepts GROUP BY block_type "
            "ORDER BY COUNT(*) DESC"))
        merged = conn.execute(
            "SELECT COUNT(*) FROM unified_concepts WHERE contributor_count > 1"
        ).fetchone()[0]
        stills = dict(conn.execute(
            "SELECT source_stills, COUNT(*) FROM unified_concepts GROUP BY source_stills"))
    finally:
        conn.close()

    by_still: dict[str, int] = {}
    for raw, n in stills.items():
        for s in _json_list(raw):
            by_still[s] = by_still.get(s, 0) + n

    edge_types: dict[str, int] = {}
    for e in edges:
        et = e.get("edge_type") or e.get("type") or "?"
        edge_types[et] = edge_types.get(et, 0) + 1

    return {
        "run_dir": paths["run_dir"],
        "concepts": total,
        "concepts_merged_from_multiple_observations": merged,
        "by_block_type": by_type,
        "by_still": dict(sorted(by_still.items(), key=lambda kv: -kv[1])),
        "graph_nodes": len(nodes),
        "graph_edges": sum(edge_types.values()),
        "edge_types": edge_types,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Query a distilled run's concept graph.")
    ap.add_argument("run_dir")
    ap.add_argument("--query", default="", help="title substring")
    ap.add_argument("--block-type", default="")
    ap.add_argument("--still", default="")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--body", action="store_true", help="include concept body text")
    ap.add_argument("--concept-id", default="", help="fetch one concept by id")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    if a.stats:
        out = stats(a.run_dir)
    elif a.concept_id:
        out = get_concept(a.run_dir, a.concept_id)
    else:
        out = query(a.run_dir, a.query, a.block_type, a.still, a.limit, a.body)
    print(json.dumps(out, indent=2))

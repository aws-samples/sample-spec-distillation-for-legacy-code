#!/usr/bin/env python3
"""
reconcile.py — Graph-driven reconciliation across stills.

Replaces the LLM-based reconciliation agent with deterministic graph traversal:
1. Load the shared vector store + per-still knowledge graphs
2. Identify CONVERGENCE: concepts appearing across multiple stills (cross-still edges)
3. Identify DIVERGENCE: duplicate groups with conflicting descriptions
4. Assess vocabulary consistency: same title appearing in different stills with different types
5. Produce reconciliation.md

No LLM calls. Pure graph traversal + string comparison.

Usage:
    python3 reconcile.py <stills_dir> <output_dir>
"""

import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np


# ── Data Structures ──────────────────────────────────────────────────────────


class ReconciliationReport:
    """Holds all reconciliation findings."""

    def __init__(self):
        self.convergence: list[dict] = []  # concepts confirmed by multiple stills
        self.divergence: list[dict] = []   # conflicts between stills
        self.vocab_issues: list[dict] = []  # same term, different meaning
        self.coverage_gaps: list[dict] = []  # stills that should have covered X but didn't
        self.confidence_summary: dict[str, dict] = {}  # per-still stats
        self.stats: dict = {}


# ── Core Logic ───────────────────────────────────────────────────────────────


def reconcile(stills_dir: str, output_dir: str) -> dict:
    """Run reconciliation across all stills using their knowledge graphs.

    Args:
        stills_dir: Path to the stills/ directory containing per-still outputs.
        output_dir: Path to write reconciliation.md.

    Returns:
        Stats dict with counts of convergence, divergence, vocab issues.
    """
    stills_path = Path(stills_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    t_total = time.time()
    print(f"[reconcile] ═══════════════════════════════════════════════════════")
    print(f"[reconcile] Stills dir: {stills_path}")
    print(f"[reconcile] Output dir: {output_path}")
    print(f"[reconcile] ═══════════════════════════════════════════════════════")

    # Step 1: Load all knowledge graphs
    print(f"[reconcile] Step 1/6: Loading knowledge graphs...")
    t0 = time.time()
    graphs = _load_all_graphs(stills_path)
    if not graphs:
        return {"error": "No knowledge graphs found in stills directory"}

    total_nodes = sum(len(g.get("nodes", [])) for g in graphs.values())
    total_edges = sum(len(g.get("edges", g.get("links", []))) for g in graphs.values())
    print(f"[reconcile]   Loaded {len(graphs)} stills: {', '.join(sorted(graphs.keys()))}")
    print(f"[reconcile]   Total nodes: {total_nodes:,} | Total edges: {total_edges:,}")
    print(f"[reconcile]   ({time.time() - t0:.1f}s)")

    # Step 2: Cross-still convergence via vector similarity
    shared_db = stills_path / "shared_vectors.db"
    cross_still_pairs = []
    if shared_db.exists():
        print(f"[reconcile] Step 2/6: Finding cross-still convergence (ANN search)...")
        t0 = time.time()
        cross_still_pairs = _find_cross_still_convergence(shared_db)
        print(f"[reconcile]   Found {len(cross_still_pairs)} convergent pairs ({time.time() - t0:.1f}s)")
    else:
        print(f"[reconcile] Step 2/6: Skipped (no shared_vectors.db)")

    # Build the report
    report = ReconciliationReport()

    # Step 3: CONVERGENCE analysis
    print(f"[reconcile] Step 3/6: Analyzing convergence...")
    t0 = time.time()
    _find_convergence(graphs, cross_still_pairs, report)
    print(f"[reconcile]   {len(report.convergence)} convergence points ({time.time() - t0:.1f}s)")

    # Step 4: DIVERGENCE analysis
    print(f"[reconcile] Step 4/6: Analyzing divergence...")
    t0 = time.time()
    _find_divergence(graphs, report)
    print(f"[reconcile]   {len(report.divergence)} divergence conflicts ({time.time() - t0:.1f}s)")

    # Step 5: VOCABULARY consistency
    print(f"[reconcile] Step 5/6: Checking vocabulary consistency...")
    t0 = time.time()
    _find_vocab_issues(graphs, report)
    print(f"[reconcile]   {len(report.vocab_issues)} vocabulary issues ({time.time() - t0:.1f}s)")

    # Step 6: Confidence summary + write output
    print(f"[reconcile] Step 6/6: Building confidence summary and writing output...")
    t0 = time.time()
    _build_confidence_summary(graphs, report)

    report.stats = {
        "stills_analyzed": len(graphs),
        "convergence_points": len(report.convergence),
        "divergence_points": len(report.divergence),
        "vocabulary_issues": len(report.vocab_issues),
        "coverage_gaps": len(report.coverage_gaps),
    }

    _write_reconciliation_md(report, output_path, graphs)
    print(f"[reconcile]   Written: {output_path / 'reconciliation.md'} ({time.time() - t0:.1f}s)")

    elapsed_total = time.time() - t_total
    print(f"[reconcile] ═══════════════════════════════════════════════════════")
    print(f"[reconcile] Complete in {elapsed_total:.1f}s")
    print(f"[reconcile]   Convergence: {len(report.convergence)}")
    print(f"[reconcile]   Divergence:  {len(report.divergence)}")
    print(f"[reconcile]   Vocab issues: {len(report.vocab_issues)}")
    print(f"[reconcile] ═══════════════════════════════════════════════════════")

    return report.stats


# ── Graph Loading ────────────────────────────────────────────────────────────


def _load_all_graphs(stills_path: Path) -> dict[str, dict]:
    """Load the knowledge graph(s).

    Checks for unified graph at stills root first, then falls back to per-still.
    Returns dict of still_name → {nodes: [...], edges: [...]}.
    """
    graphs = {}
    
    # Unified graph at stills root
    unified = stills_path / "knowledge_graph.json"
    if unified.exists():
        try:
            print(f"[reconcile]   Loading unified graph: {unified}")
            t0 = time.time()
            data = json.loads(unified.read_text())
            print(f"[reconcile]   JSON parsed ({time.time() - t0:.1f}s)")

            # Build node-to-still index in a single pass
            t0 = time.time()
            nodes_by_still: dict[str, list] = defaultdict(list)
            node_to_still: dict[str, str] = {}
            for node in data.get("nodes", []):
                still = node.get("source_still", node.get("still", "unknown"))
                nodes_by_still[still].append(node)
                nid = node.get("id", "")
                if nid:
                    node_to_still[nid] = still

            print(f"[reconcile]   Indexed {len(node_to_still):,} nodes across {len(nodes_by_still)} stills ({time.time() - t0:.1f}s)")

            # Bucket edges by still in a single pass (edge belongs to source's still)
            t0 = time.time()
            edges_by_still: dict[str, list] = defaultdict(list)
            all_edges = data.get("edges", data.get("links", []))
            for edge in all_edges:
                src = edge.get("source", "")
                tgt = edge.get("target", "")
                src_still = node_to_still.get(src, "")
                tgt_still = node_to_still.get(tgt, "")
                # Assign edge to both stills it touches
                if src_still:
                    edges_by_still[src_still].append(edge)
                if tgt_still and tgt_still != src_still:
                    edges_by_still[tgt_still].append(edge)

            print(f"[reconcile]   Indexed {len(all_edges):,} edges ({time.time() - t0:.1f}s)")

            for still_name, nodes in nodes_by_still.items():
                graphs[still_name] = {
                    "nodes": nodes,
                    "edges": edges_by_still.get(still_name, []),
                }
            return graphs
        except (json.JSONDecodeError, IOError) as e:
            print(f"[reconcile]   WARNING: Failed to load unified graph: {e}")
    
    # Fallback: per-still graphs
    for graph_file in stills_path.glob("*/final*/knowledge_graph.json"):
        still_name = graph_file.parent.parent.name
        # Pick latest by mtime if multiple
        if still_name in graphs:
            existing_mtime = graphs[still_name]["_mtime"]
            if graph_file.stat().st_mtime <= existing_mtime:
                continue

        try:
            data = json.loads(graph_file.read_text())
            data["_mtime"] = graph_file.stat().st_mtime
            data["_path"] = str(graph_file)
            graphs[still_name] = data
        except (json.JSONDecodeError, IOError) as e:
            # A silently-skipped graph shrinks the reconciled aggregate with no trace. Skipping a
            # single bad file shouldn't kill the batch, but the skip MUST be visible.
            print(f"[reconcile] WARN skipping unreadable graph for still '{still_name}' "
                  f"({graph_file}): {type(e).__name__}: {e}", flush=True)
            continue

    return graphs


def _find_cross_still_convergence(db_path: Path) -> list[tuple[str, str, str, str, float]]:
    """Query the shared vector store for cross-still high-similarity pairs.

    Uses hnswlib ANN index per-still to find convergent concepts across stills
    without building O(N²) dense matrices.

    Returns list of (block_id_a, still_a, block_id_b, still_b, score).
    """
    import sqlite3
    import hnswlib

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT block_id, still_name, title, embedding FROM vectors"
    ).fetchall()
    conn.close()

    if len(rows) < 2:
        return []

    # Group by still
    by_still: dict[str, list] = defaultdict(list)
    dim = 0
    for block_id, still, title, emb_blob in rows:
        vec = np.frombuffer(emb_blob, dtype=np.float32).copy()
        if dim == 0:
            dim = len(vec)
        if len(vec) == dim:
            by_still[still].append((block_id, title, vec))

    still_names = sorted(by_still.keys())
    if len(still_names) < 2:
        return []

    print(f"[reconcile]   Stills in vector store: {len(still_names)}")
    for s in still_names:
        print(f"[reconcile]     {s}: {len(by_still[s]):,} vectors")

    # For each still, build an hnswlib index. Then query every OTHER still against it.
    # This avoids N_a × N_b dense matrices.
    threshold = 0.80
    k_neighbors = 3  # top-3 matches per query vector
    pairs = []

    # Build indexes for each still
    indexes: dict[str, tuple] = {}  # still → (index, ids_list)
    for still in still_names:
        blocks = by_still[still]
        n = len(blocks)
        if n == 0:
            continue

        mat = np.array([b[2] for b in blocks], dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1e-10
        normed = mat / norms

        index = hnswlib.Index(space="ip", dim=dim)
        index.init_index(max_elements=n, ef_construction=200, M=32)
        index.add_items(normed, list(range(n)))
        index.set_ef(max(50, k_neighbors * 2))

        indexes[still] = (index, blocks)

    print(f"[reconcile]   Built {len(indexes)} ANN indexes")

    # Query each still's vectors against every OTHER still's index
    comparisons_done = 0
    total_comparisons = len(still_names) * (len(still_names) - 1) // 2

    for i in range(len(still_names)):
        still_a = still_names[i]
        blocks_a = by_still[still_a]
        if not blocks_a:
            continue

        # Build normalized query matrix for still_a
        mat_a = np.array([b[2] for b in blocks_a], dtype=np.float32)
        norms_a = np.linalg.norm(mat_a, axis=1, keepdims=True)
        norms_a[norms_a == 0] = 1e-10
        normed_a = mat_a / norms_a

        for j in range(i + 1, len(still_names)):
            still_b = still_names[j]
            if still_b not in indexes:
                continue

            index_b, blocks_b = indexes[still_b]
            n_b = len(blocks_b)
            if n_b == 0:
                continue

            effective_k = min(k_neighbors, n_b)
            labels, distances = index_b.knn_query(normed_a, k=effective_k)

            # Collect pairs above threshold
            pair_count = 0
            for row_idx in range(len(blocks_a)):
                for col_idx in range(effective_k):
                    neighbor_idx = int(labels[row_idx][col_idx])
                    similarity = 1.0 - float(distances[row_idx][col_idx])
                    if similarity >= threshold:
                        pairs.append((
                            blocks_a[row_idx][0],  # block_id_a
                            still_a,
                            blocks_b[neighbor_idx][0],  # block_id_b
                            still_b,
                            similarity,
                        ))
                        pair_count += 1

            comparisons_done += 1
            print(f"[reconcile]     {still_a} × {still_b}: {pair_count} pairs "
                  f"({comparisons_done}/{total_comparisons})")

    # Sort by score descending, deduplicate
    pairs.sort(key=lambda x: -x[4])
    seen = set()
    deduped = []
    for p in pairs:
        key = tuple(sorted([p[0], p[2]]))
        if key not in seen:
            seen.add(key)
            deduped.append(p)

    print(f"[reconcile]   Total convergent pairs: {len(deduped)} (after dedup from {len(pairs)} raw)")
    return deduped[:500]  # Cap at 500 most significant convergences


# ── Analysis Functions ───────────────────────────────────────────────────────


def _find_convergence(graphs: dict, cross_pairs: list, report: ReconciliationReport):
    """Identify concepts confirmed by multiple stills (high confidence)."""
    # Build node lookup across all graphs
    all_nodes: dict[str, dict] = {}  # block_id → node attrs + still
    for still_name, graph_data in graphs.items():
        for node in graph_data.get("nodes", []):
            nid = node.get("id", "")
            if nid:
                all_nodes[nid] = {**node, "_still": still_name}

    # Group cross-still pairs by title similarity
    convergence_groups: dict[str, list] = defaultdict(list)
    for bid_a, still_a, bid_b, still_b, score in cross_pairs:
        node_a = all_nodes.get(bid_a, {})
        node_b = all_nodes.get(bid_b, {})
        title_a = node_a.get("title", "")
        title_b = node_b.get("title", "")

        # Use the shorter title as the group key
        key = title_a if len(title_a) <= len(title_b) else title_b
        key = key[:80]  # Truncate for grouping

        convergence_groups[key].append({
            "stills": sorted(set([still_a, still_b])),
            "score": score,
            "title_a": title_a,
            "title_b": title_b,
            "type_a": node_a.get("block_type", ""),
            "type_b": node_b.get("block_type", ""),
        })

    # Convert to report entries — only keep groups with genuinely different stills
    for key, entries in sorted(convergence_groups.items(), key=lambda x: -max(e["score"] for e in x[1])):
        stills_involved = set()
        for e in entries:
            stills_involved.update(e["stills"])
        if len(stills_involved) >= 2:
            best = max(entries, key=lambda e: e["score"])
            report.convergence.append({
                "concept": key,
                "stills": sorted(stills_involved),
                "confidence": round(best["score"] * 100),
                "still_count": len(stills_involved),
                "example": f"{best['title_a']} ({best['type_a']}) ↔ {best['title_b']} ({best['type_b']})",
            })


def _find_divergence(graphs: dict, report: ReconciliationReport):
    """Identify conflicts: same concept, contradictory descriptions across stills."""
    # Build title index across stills
    title_index: dict[str, list] = defaultdict(list)
    for still_name, graph_data in graphs.items():
        for node in graph_data.get("nodes", []):
            title = node.get("title", "").strip().lower()
            if len(title) > 10:  # Skip very short titles
                title_index[title].append({
                    "still": still_name,
                    "type": node.get("block_type", ""),
                    "confidence": node.get("confidence", 3),
                    "preview": node.get("body_preview", "")[:150],
                })

    # Find titles that appear in multiple stills with DIFFERENT types
    for title, entries in title_index.items():
        stills = set(e["still"] for e in entries)
        types = set(e["type"] for e in entries)
        if len(stills) >= 2 and len(types) >= 2:
            report.divergence.append({
                "concept": title[:80],
                "stills": sorted(stills),
                "types_seen": sorted(types),
                "entries": entries[:4],  # Cap for readability
                "issue": f"Classified as {'/'.join(sorted(types))} across {len(stills)} stills",
            })


def _find_vocab_issues(graphs: dict, report: ReconciliationReport):
    """Find vocabulary inconsistencies: same term used differently."""
    # Extract titles normalized
    term_usage: dict[str, list] = defaultdict(list)
    for still_name, graph_data in graphs.items():
        for node in graph_data.get("nodes", []):
            title = node.get("title", "")
            # Extract the core noun (after "Entity:", "Rule:", etc.)
            core = re.sub(r'^(Entity|Rule|Business Object|Invariant|Obligation|Boundary|Finding):\s*', '', title, flags=re.IGNORECASE)
            core = re.sub(r'\s*\(.*?\)\s*', '', core).strip().lower()
            if len(core) > 3:
                term_usage[core].append({
                    "still": still_name,
                    "full_title": title,
                    "type": node.get("block_type", ""),
                })

    # Find terms with usage in 3+ stills (these are core vocabulary)
    for term, usages in term_usage.items():
        stills = set(u["still"] for u in usages)
        types = set(u["type"] for u in usages)
        if len(stills) >= 3 and len(types) >= 2:
            report.vocab_issues.append({
                "term": term,
                "stills": sorted(stills),
                "types": sorted(types),
                "usage_count": len(usages),
                "issue": f"Used as {'/'.join(sorted(types))} across {len(stills)} stills",
            })

    # Sort by number of stills (most widespread inconsistencies first)
    report.vocab_issues.sort(key=lambda x: -x["usage_count"])


def _build_confidence_summary(graphs: dict, report: ReconciliationReport):
    """Build per-still statistics for the confidence summary."""
    for still_name, graph_data in graphs.items():
        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", graph_data.get("links", []))

        # Count by type
        type_counts: dict[str, int] = defaultdict(int)
        confidences = []
        for node in nodes:
            type_counts[node.get("block_type", "unknown")] += 1
            confidences.append(node.get("confidence", 3))

        avg_conf = sum(confidences) / len(confidences) if confidences else 0
        same_as_edges = sum(1 for e in edges if e.get("edge_type") == "same_as")

        report.confidence_summary[still_name] = {
            "total_concepts": len(nodes),
            "total_edges": len(edges),
            "duplicate_pairs": same_as_edges,
            "avg_confidence": round(avg_conf, 1),
            "types": dict(type_counts),
        }


# ── Output ───────────────────────────────────────────────────────────────────


def _write_reconciliation_md(report: ReconciliationReport, output_path: Path, graphs: dict):
    """Write the reconciliation.md report."""
    out = output_path / "reconciliation.md"

    with open(out, "w", encoding="utf-8") as f:
        f.write("# Specification Distillation — Reconciliation Report\n\n")
        f.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n")
        f.write(f"Stills analyzed: {len(graphs)}\n\n")
        f.write("---\n\n")

        # Summary stats
        f.write("## Summary\n\n")
        f.write("| Metric | Value |\n|--------|-------|\n")
        for k, v in report.stats.items():
            f.write(f"| {k.replace('_', ' ').title()} | {v} |\n")
        f.write("\n")

        # Confidence summary
        f.write("## Per-Still Confidence\n\n")
        f.write("| Still | Concepts | Duplicates | Avg Confidence | Top Types |\n")
        f.write("|-------|----------|------------|----------------|----------|\n")
        for still, stats in sorted(report.confidence_summary.items()):
            top_types = ", ".join(
                f"{t}({c})" for t, c in sorted(stats["types"].items(), key=lambda x: -x[1])[:3]
            )
            f.write(f"| {still} | {stats['total_concepts']} | {stats['duplicate_pairs']} | {stats['avg_confidence']} | {top_types} |\n")
        f.write("\n")

        # Convergence
        f.write(f"## Convergence ({len(report.convergence)} cross-still confirmations)\n\n")
        f.write("Concepts independently discovered by multiple stills — high confidence findings.\n\n")
        if report.convergence:
            f.write("| Concept | Stills | Confidence | Example |\n")
            f.write("|---------|--------|------------|--------|\n")
            for c in report.convergence[:100]:
                stills_str = ", ".join(c["stills"])
                f.write(f"| {c['concept'][:50]} | {stills_str} | {c['confidence']}% | {c['example'][:60]} |\n")
            if len(report.convergence) > 100:
                f.write(f"\n*...and {len(report.convergence) - 100} more convergence points.*\n")
        else:
            f.write("*No cross-still convergence found (only one still analyzed or no shared vector store).*\n")
        f.write("\n")

        # Divergence
        f.write(f"## Divergence ({len(report.divergence)} conflicts)\n\n")
        f.write("Same concept classified differently across stills — needs resolution.\n\n")
        if report.divergence:
            f.write("| Concept | Stills | Issue |\n")
            f.write("|---------|--------|-------|\n")
            for d in report.divergence[:50]:
                stills_str = ", ".join(d["stills"])
                f.write(f"| {d['concept'][:50]} | {stills_str} | {d['issue']} |\n")
            if len(report.divergence) > 50:
                f.write(f"\n*...and {len(report.divergence) - 50} more divergence points.*\n")
        else:
            f.write("*No classification conflicts detected across stills.*\n")
        f.write("\n")

        # Vocabulary issues
        f.write(f"## Vocabulary Consistency ({len(report.vocab_issues)} issues)\n\n")
        f.write("Terms used inconsistently across stills.\n\n")
        if report.vocab_issues:
            f.write("| Term | Stills | Types | Occurrences |\n")
            f.write("|------|--------|-------|-------------|\n")
            for v in report.vocab_issues[:50]:
                stills_str = ", ".join(v["stills"])
                types_str = ", ".join(v["types"])
                f.write(f"| {v['term'][:40]} | {stills_str} | {types_str} | {v['usage_count']} |\n")
            if len(report.vocab_issues) > 50:
                f.write(f"\n*...and {len(report.vocab_issues) - 50} more vocabulary issues.*\n")
        else:
            f.write("*No vocabulary inconsistencies detected.*\n")
        f.write("\n")

        # Recommendations for master blend
        f.write("## Recommendations for Master Blend\n\n")
        if report.convergence:
            f.write(f"1. **{len(report.convergence)} high-confidence concepts** confirmed across multiple stills — these form the reliable core of the specification.\n")
        if report.divergence:
            f.write(f"2. **{len(report.divergence)} classification conflicts** need resolution — the master blend should pick the most appropriate type based on the still's primary domain.\n")
        if report.vocab_issues:
            f.write(f"3. **{len(report.vocab_issues)} vocabulary inconsistencies** should be normalized — pick one canonical type per term and use it consistently.\n")

        total_concepts = sum(s["total_concepts"] for s in report.confidence_summary.values())
        total_dups = sum(s["duplicate_pairs"] for s in report.confidence_summary.values())
        f.write(f"4. **Total concept universe**: {total_concepts} concepts across {len(graphs)} stills, with {total_dups} within-still duplicates already resolved.\n")
        f.write(f"5. **Cross-still deduplication** will further reduce the concept count when merging into the master blend.\n")


# ── CLI Entry Point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python3 reconcile.py <stills_dir> <output_dir>")
        print()
        print("Example:")
        print("  python3 reconcile.py output/stills output/reconciliation")
        sys.exit(1)

    result = reconcile(sys.argv[1], sys.argv[2])
    print(f"\nResult: {json.dumps(result, indent=2)}")

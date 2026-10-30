"""Ontology assembly — connected-component story tier (Phase-1 step T-1, first half).

Story: knowledge-layer/ontology-classification, task T-1 ("write concept->story and
story->epic edges into the shared graph"). The comparator + residue tiers produced pairwise
concept<->concept co-membership edges (reasoning family). This step ASSEMBLES those pairwise
signals into actual STORY groupings: each connected component of co-membership edges is a
candidate story; we mint a stable story id and write concept->story membership edges.

Two-layer design (grounded on measured structure, 2026-08-20):
  - BACKBONE (confident edges only): tight clusters — max 44 nodes, giant 0.1%. The
    trustworthy story tier.
  - OVERLAY (confident + unsure_kept, incl Half B shared_dependency_target): merges some
    look-different/live-together concepts into shared-resource stories — max 212, giant 0.6%,
    NO hairball. Recorded as a SEPARATE, reviewable layer so the clean backbone stays
    inspectable and the resource merges are visible-but-separable (§7.2 family separability).

Story identity is L6-stable: a content hash of the sorted member-concept set, so re-running
over the same clustering yields the same story id (deterministic identity), and a story that
gains/loses a member gets a new id (its membership IS its identity at this tier — no
positional ordinal).

Membership edges are written via the step-1 substrate (write_edge) with relation
`concept_in_story`, dst = the minted story id. Immutable-assets: WORKING COPY only.

CLI:
    python ontology_assemble.py <wip_db> --layer backbone --dry-run
    python ontology_assemble.py <wip_db> --layer backbone --commit
    python ontology_assemble.py <wip_db> --layer overlay  --commit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import datetime as _dt
from collections import defaultdict

import ontology_classify as oc

REL_CONCEPT_IN_STORY = "concept_in_story"  # concept -> story membership (T-1)
LAYER_BACKBONE = "backbone"    # confident co-membership only
LAYER_OVERLAY = "overlay"      # confident + unsure_kept (incl Half B)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def story_id(members: set) -> str:
    """L6-stable story id = hash of the sorted member set. Membership is identity at this tier."""
    h = hashlib.sha256("\x1f".join(sorted(members)).encode()).hexdigest()
    return "story_" + h[:16]


def connected_components(conn, layer: str):
    """Union-find over reasoning co-membership edges for the chosen layer.
    Returns {root: set(member concept ids)} for components of size >= 2 (a singleton is not a
    grouping — it stays an ungrouped concept, surfaced elsewhere, never dropped: L3c)."""
    if layer == LAYER_BACKBONE:
        where = "family='reasoning' AND verdict='confident'"
    elif layer == LAYER_OVERLAY:
        where = "family='reasoning'"  # confident + unsure_kept
    else:
        raise ValueError(f"unknown layer {layer!r}")

    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path-compress
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    n_edges = 0
    for src, dst in conn.execute(f"SELECT src, dst FROM ontology_edges WHERE {where}"):
        union(src, dst)
        n_edges += 1
    comps = defaultdict(set)
    for node in list(parent.keys()):
        comps[find(node)].add(node)
    # only real groupings (>=2); singletons are ungrouped concepts, not stories
    comps = {r: m for r, m in comps.items() if len(m) >= 2}
    return comps, n_edges


def ensure_story_meta(conn):
    """A small table recording each minted story + which layer/clustering produced it
    (provenance for the assembly step; the membership edges live in ontology_edges)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ontology_story (
            story_id TEXT PRIMARY KEY,
            layer TEXT NOT NULL,
            member_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def assemble(conn, layer: str, commit: bool):
    comps, n_edges = connected_components(conn, layer)
    sizes = sorted((len(m) for m in comps.values()), reverse=True)
    covered = sum(sizes)
    report = {
        "layer": layer,
        "co_membership_edges": n_edges,
        "stories_formed": len(comps),
        "concepts_grouped": covered,
        "largest_story": sizes[0] if sizes else 0,
        "top10_sizes": sizes[:10],
    }
    if not commit:
        # show a couple example stories with member titles
        uc_title = {r[0]: r[1] for r in conn.execute("SELECT concept_id, title FROM unified_concepts")}
        examples = []
        for root, members in list(comps.items())[:3]:
            sid = story_id(members)
            examples.append({"story_id": sid, "size": len(members),
                             "members": [uc_title.get(m, m)[:60] for m in list(members)[:6]]})
        report["mode"] = "dry-run (nothing written)"
        report["examples"] = examples
        return report

    ensure_story_meta(conn)
    now = _now()
    edges_written = 0
    for root, members in comps.items():
        sid = story_id(members)
        conn.execute(
            "INSERT OR REPLACE INTO ontology_story (story_id, layer, member_count, created_at) "
            "VALUES (?,?,?,?)", (sid, layer, len(members), now))
        for concept in members:
            oc.write_edge(
                conn, src=concept, dst=sid, family=oc.FAMILY_REASONING,
                relation=REL_CONCEPT_IN_STORY, criterion=f"assembled_{layer}",
                classifier="assembly", verdict=oc.VERDICT_CONFIDENT if layer == LAYER_BACKBONE else oc.VERDICT_UNSURE_KEPT,
                confidence=1.0 if layer == LAYER_BACKBONE else 0.6,
                evidence={"layer": layer, "story_size": len(members)})
            edges_written += 1
        conn.commit()  # incremental per story
    report["mode"] = "committed"
    report["membership_edges_written"] = edges_written
    return report


def main():
    ap = argparse.ArgumentParser(description="Ontology assembly — component->story tier (T-1)")
    ap.add_argument("db_path", help="shared_vectors WORKING COPY")
    ap.add_argument("--layer", choices=[LAYER_BACKBONE, LAYER_OVERLAY], default=LAYER_BACKBONE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db_path)
    try:
        oc.ensure_tables(conn)
        report = assemble(conn, args.layer, commit=args.commit and not args.dry_run)
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

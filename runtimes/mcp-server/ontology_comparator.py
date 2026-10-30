"""Ontology classification — join comparator + deterministic tier (Phase-1 steps 2-3).

Story: knowledge-layer/ontology-classification. Steps 2 (T-2 join comparator) and 3
(T-3 deterministic tier). Reads the baseline store's already-materialized ground truth and
proposes grouping edges that are MECHANICALLY settleable — writing them through the step-1
substrate (`ontology_classify.write_edge`). It does NOT call an LLM: the genuinely ambiguous
residue is left for T-4 (the residue tier), which lands separately.

The three axes of the join (R-3 / T-2 — never similarity alone):
  1. semantic similarity / convergence  — knowledge_graph.json `references` edges (scored)
                                            between raw block_ids, plus `vectors` embeddings.
  2. code-dependency co-location         — concepts sharing code symbols (concept_symbols.json
                                            `by_symbol` inverse index) and, deeper, shared
                                            code_edges dst targets. The orthogonal axis.
  3. the concept set                     — unified_concepts (uc_ nodes); raw block_ids lift to
                                            uc_ via contributor_ids.

Deterministic tier (T-3, R-2): only propose a grouping when it is settleable WITHOUT an LLM —
  (a) STRONG-CONVERGENCE: a high-scored `references` link AND shared code co-location, or
  (b) SHARED-DEPENDENCY-TARGET: concepts whose symbols hit the same code_edges dst (CRUD on
      one resource = one story, System §7.2 boundary policy), even at low textual similarity.
Everything else is residue -> left for T-4, NEVER force-grouped (L3c keep-and-flag).

Boundary-policy criteria recorded on each edge (T-5, R-4): the `criterion` column names WHY a
grouping was made, so a wrong grouping is diagnosable and the policy is tunable.

Immutable-assets: reads baseline artifacts read-only; writes ONLY into the working-copy DB
passed on the CLI (never the baseline in place). No upstream rerun.

CLI (sample-first, per the no-expensive-rerun discipline):
    python ontology_comparator.py <wip_db> <stills_dir> --sample 500 --dry-run
    python ontology_comparator.py <wip_db> <stills_dir> --sample 500 --commit
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import ontology_classify as oc

# ── Tunables (chosen with statistical grounding 2026-08-20; see IMPLEMENTATION_PLAN log) ──
# The deterministic tier should only claim what is clearly settleable; ambiguity -> T-4 residue.
# SIM_STRONG=0.82: a smooth point on the score curve; higher (0.90) starves recall, lower (0.80)
#   loosens "strong". MIN_SHARED_SYMBOLS=2: in the shared-symbol-count distribution among strong
#   pairs, the single-shared-symbol bucket is the largest slice and is where a lone incidental token
#   (e.g. "id") produces false co-location; requiring >=2 keeps the genuine multi-symbol pairs as
#   `confident` and pushes the weakest single-symbol pairs to T-4.
SIM_STRONG = 0.82        # a `references` score at/above this is a strong semantic signal
MIN_SHARED_SYMBOLS = 2   # >=2 shared code symbols = real co-location (ms=1 is the false-positive bucket)
# criterion tags (recorded on the edge; the boundary-policy WHY)
CRIT_STRONG_CONVERGENCE = "strong_convergence_and_colocation"
CRIT_SHARED_DEP_TARGET = "shared_dependency_target"

# Half-B guardrails. A shared dependency target makes a pair ELIGIBLE for co-grouping (R-3),
# it is not an automatic single story (T-5 boundary policy refines that). We cap the fan-out
# of a single hub target so a 600-concept resource does not explode into ~180k pairwise edges;
# above the cap the target is a "hub" and pairwise co-grouping on it alone is too weak to be
# deterministic (left for T-4 / higher-tier grouping). Conservative on purpose (hot-patch-era).
# MAX_DEP_TARGET_FANOUT=15: the fanout distribution is heavy-tailed. Large-fanout targets are HUBS
# (e.g. a company-wide code shared by hundreds of concepts) — a shared hub is weak grouping evidence
# and is where the pairwise explosion lives (raising the cap multiplies edges superlinearly). A cap
# just below the p75 fanout keeps the specific-resource targets (tractable pair volume) and cleanly
# excludes the hub tail. These edges are `unsure_kept` (eligibility), so the cap governs what is
# specific enough to be worth proposing.
MAX_DEP_TARGET_FANOUT = 15   # targets shared by more than this many concepts are hubs -> skip pairwise
MIN_DEP_TARGETS_SHARED = 1   # >=1 resolved shared dependency target


# ── Loaders (baseline artifacts, read-only) ───────────────────────────────────

def load_concept_symbols(stills_dir: Path) -> dict:
    """Return concept_symbols.json: {'by_concept': {block_key: {...symbols[]}}, 'by_symbol': {symbol: [block_keys]}}."""
    with open(stills_dir / "concept_symbols.json") as f:
        return json.load(f)


def block_key_for(still: str, title: str) -> str:
    return f"{still}::{title}"


def load_block_to_uc(conn: sqlite3.Connection) -> dict:
    """Map raw block_id -> uc_ concept_id via unified_concepts.contributor_ids.

    contributor_ids is a JSON array of the raw block_ids unified into that uc_.
    """
    b2u: dict[str, str] = {}
    for uc, cids in conn.execute(
        "SELECT concept_id, contributor_ids FROM unified_concepts WHERE contributor_ids IS NOT NULL"
    ):
        try:
            for bid in json.loads(cids):
                b2u[bid] = uc
        except (json.JSONDecodeError, TypeError):
            continue
    return b2u


def load_similarity_pairs(stills_dir: Path, min_score: float):
    """Stream `references` edges (scored similarity) from knowledge_graph.json.

    The graph is large (~340MB) and links live at the tail, so we parse the links array
    incrementally without a full DOM load. Yields (src_block, dst_block, score) with
    score >= min_score only. Falls back to a full json.load if streaming is unavailable.
    """
    kg = stills_dir / "knowledge_graph.json"
    # This networkx dump puts edges under "edges" (not the default "links"). Accept both so
    # the loader is robust to either node-link convention; verified against ground truth the
    # baseline uses "edges" with edge_type in {references, same_as, CONTRIBUTES_TO}.
    with open(kg) as f:
        data = json.load(f)  # baseline is local; acceptable for the sample pass. (full pass may stream.)
    edge_list = data.get("edges")
    if not edge_list:
        edge_list = data.get("links", [])
    for e in edge_list:
        if e.get("edge_type") == "references":
            s = e.get("score")
            if s is not None and s >= min_score:
                yield e.get("source"), e.get("target"), float(s)


# ── The join comparator (T-2) ────────────────────────────────────────────────

def build_colocation_index(cs: dict) -> dict:
    """block_key -> set(symbols). The co-location primitive: two concepts sharing a symbol
    are code-co-located (System §7.2 orthogonal axis)."""
    idx: dict[str, set] = {}
    for bkey, meta in cs.get("by_concept", {}).items():
        syms = meta.get("symbols") or []
        if syms:
            idx[bkey] = set(syms)
    return idx


def uc_key_for_block(conn: sqlite3.Connection, block_id: str, b2u: dict) -> str | None:
    return b2u.get(block_id)


def load_uc_symbols_via_crosswalk(conn, stills_dir: Path) -> dict:
    """uc_id -> set(symbols), reached THROUGH the hot-patch crosswalk (ontology_crosswalk.py).

    The co-location axis is unreachable by naive still::raw-title (the two-island defect,
    SYSTEM_SPEC §8). We route: uc_id -> concept_crosswalk.block_key -> concept_symbols.symbols.
    Concepts with no crosswalk row (the L3c-flagged residue) simply have no co-location signal
    here — they are not force-grouped, exactly as the deterministic tier requires.
    """
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='concept_crosswalk'"
    ).fetchone():
        raise SystemExit(
            "concept_crosswalk not found — run ontology_crosswalk.py --commit on this working "
            "copy first (the hot-patch bridge; see SYSTEM_SPEC §8)."
        )
    cs = load_concept_symbols(stills_dir)
    by_concept = cs.get("by_concept", {})
    uc_syms: dict[str, set] = {}
    for uc, bkey in conn.execute("SELECT uc_id, block_key FROM concept_crosswalk"):
        syms = by_concept.get(bkey, {}).get("symbols") or []
        if syms:
            uc_syms.setdefault(uc, set()).update(syms)
    return uc_syms


def load_dependency_targets(conn) -> set:
    """The set of code_edges dst objects, lowercased — the real 'resources' a concept can
    depend on. A concept symbol counts as a dependency target only if it resolves to one of
    these (exact, lowercased) — stronger + far less false than bare title-tail matching."""
    return {r[0].lower() for r in conn.execute("SELECT DISTINCT dst FROM code_edges") if r[0]}


def load_uc_dep_targets(conn, stills_dir: Path, dep_targets: set) -> dict:
    """uc_id -> set(resolved dependency targets), via the crosswalk. Only symbols that resolve
    to a code_edges dst are kept."""
    cs = load_concept_symbols(stills_dir)
    by_concept = cs.get("by_concept", {})
    uc_t: dict[str, set] = {}
    for uc, bkey in conn.execute("SELECT uc_id, block_key FROM concept_crosswalk"):
        for s in (by_concept.get(bkey, {}).get("symbols") or []):
            sl = s.lower()
            if sl in dep_targets:
                uc_t.setdefault(uc, set()).add(sl)
    return uc_t


def _half_a_convergence(conn, stills_dir, uc_syms, b2u, sample, proposed):
    """Half A (R-3): look-alike + live-together. Strong similarity AND shared code symbol."""
    seen = 0
    for src_b, dst_b, score in load_similarity_pairs(stills_dir, SIM_STRONG):
        seen += 1
        if sample and len(proposed) >= sample:
            break
        src_uc = b2u.get(src_b)
        dst_uc = b2u.get(dst_b)
        if not src_uc or not dst_uc or src_uc == dst_uc:
            continue
        shared = uc_syms.get(src_uc, set()) & uc_syms.get(dst_uc, set())
        if len(shared) >= MIN_SHARED_SYMBOLS:
            proposed.append({
                "src": src_uc, "dst": dst_uc, "relation": oc.REL_CONCEPT_STORY,
                "criterion": CRIT_STRONG_CONVERGENCE, "verdict": oc.VERDICT_CONFIDENT,
                "confidence": round(min(1.0, score), 4),
                "evidence": {"similarity_score": round(score, 4), "shared_symbols": sorted(shared)[:10],
                             "shared_symbol_count": len(shared), "src_block": src_b, "dst_block": dst_b,
                             "via": "concept_crosswalk"},
            })
    return seen


def _half_b_shared_dep_target(conn, uc_targets, sample, proposed):
    """Half B (R-3): look-different + live-together. Concepts sharing a code dependency target
    are ELIGIBLE for co-grouping even at low textual similarity (the case similarity alone
    wrongly splits). Hub targets (fan-out > cap) are too weak to co-group pairwise on alone
    and are skipped here (left for a higher grouping tier / T-4). Verdict is unsure_kept, NOT
    confident: a shared resource is eligibility evidence, not a settled single story (T-5)."""
    from collections import defaultdict
    tgt_to_ucs = defaultdict(set)
    for uc, ts in uc_targets.items():
        for t in ts:
            tgt_to_ucs[t].add(uc)
    considered_pairs = 0
    for tgt, ucs in tgt_to_ucs.items():
        if len(ucs) < 2 or len(ucs) > MAX_DEP_TARGET_FANOUT:
            continue  # singleton = nothing to group; hub = too weak to co-group pairwise alone
        ucs = sorted(ucs)
        for i in range(len(ucs)):
            for j in range(i + 1, len(ucs)):
                if sample and len(proposed) >= sample:
                    return considered_pairs
                considered_pairs += 1
                proposed.append({
                    "src": ucs[i], "dst": ucs[j], "relation": oc.REL_CONCEPT_STORY,
                    "criterion": CRIT_SHARED_DEP_TARGET, "verdict": oc.VERDICT_UNSURE_KEPT,
                    "confidence": 0.6,  # eligibility, not settled (R-3 "eligible"); T-5/T-4 refine
                    "evidence": {"shared_dependency_target": tgt, "target_fanout": len(ucs),
                                 "via": "concept_crosswalk+code_edges"},
                })
    return considered_pairs


def deterministic_groupings(conn, stills_dir: Path, sample: int | None):
    """Propose deterministic grouping edges (both R-3 halves). Returns proposed-edge dicts
    (NOT yet written) so --dry-run shows them before --commit persists via write_edge.

    R-3 is a JOIN, never similarity alone:
      Half A (strong_convergence_and_colocation): look-alike + live-together -> confident.
      Half B (shared_dependency_target):          look-different + live-together -> eligible
                                                   (unsure_kept; a shared resource is not an
                                                   automatic single story — T-5 refines).
    Both reach co-location THROUGH the crosswalk (two-island bridge, SYSTEM_SPEC §8), never by
    raw-title string match. Everything neither half settles is residue for T-4 (kept-and-flag).
    """
    b2u = load_block_to_uc(conn)
    uc_syms = load_uc_symbols_via_crosswalk(conn, stills_dir)
    dep_targets = load_dependency_targets(conn)
    uc_targets = load_uc_dep_targets(conn, stills_dir, dep_targets)

    proposed: list = []
    # split the sample budget across the two halves so a dry-run shows both
    half = (sample // 2) if sample else None
    seen_a = _half_a_convergence(conn, stills_dir, uc_syms, b2u, half, proposed)
    pairs_b = _half_b_shared_dep_target(conn, uc_targets, sample, proposed)
    meta = {"similarity_pairs_scanned": seen_a, "half_b_pairs_considered": pairs_b}
    return proposed, meta


def main() -> None:
    ap = argparse.ArgumentParser(description="Ontology join comparator + deterministic tier (Phase-1 steps 2-3)")
    ap.add_argument("db_path", help="shared_vectors WORKING COPY (never the baseline in place)")
    ap.add_argument("stills_dir", help="stills/ dir holding knowledge_graph.json + concept_symbols.json")
    ap.add_argument("--sample", type=int, default=500, help="cap proposed edges (local-sample discipline)")
    ap.add_argument("--dry-run", action="store_true", help="show proposed groupings; write nothing")
    ap.add_argument("--commit", action="store_true", help="persist proposed groupings via the step-1 write path")
    args = ap.parse_args()

    stills = Path(args.stills_dir)
    conn = sqlite3.connect(args.db_path)
    try:
        oc.ensure_tables(conn)  # substrate must exist
        proposed, meta = deterministic_groupings(conn, stills, args.sample)
        by_criterion = {}
        for p in proposed:
            by_criterion[p["criterion"]] = by_criterion.get(p["criterion"], 0) + 1
        report = {
            **meta,
            "deterministic_groupings_proposed": len(proposed),
            "sample_cap": args.sample,
            "criteria": by_criterion,
        }
        if args.dry_run or not args.commit:
            report["mode"] = "dry-run (nothing written)"
            report["examples"] = proposed[:5]
            print(json.dumps(report, indent=2, sort_keys=True))
            return
        # commit
        written = 0
        for p in proposed:
            oc.write_edge(
                conn, src=p["src"], dst=p["dst"], family=oc.FAMILY_REASONING,
                relation=p["relation"], criterion=p["criterion"], classifier="deterministic",
                verdict=p["verdict"], confidence=p["confidence"], evidence=p["evidence"],
            )
            written += 1
        conn.commit()
        report["mode"] = "committed"
        report["edges_written"] = written
        report["substrate"] = oc.verify(conn)
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

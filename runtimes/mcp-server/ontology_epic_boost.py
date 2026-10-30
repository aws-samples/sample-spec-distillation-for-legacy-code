"""Epic-tier boosted separation — resolve the contested knot with additional weak signals.

Story: knowledge-layer/ontology-classification, story→epic. The structural dual-pass
(`ontology_epic_seed.py`) produced confident seeds where divisive+agglomerative agreed, and a
large CONTESTED KNOT where the target-sharing signal TIES across the population (the "everyone
shares a birthday" degeneracy — a single discriminator with no variance on that residual has no
separating power). This module boosts: it brings ADDITIONAL weak signals that DO have variance
on the residual, splits on whichever separates, and recurses — the deterministic-first,
residue-to-judgment discipline applied *within* the epic pass (SYSTEM_SPEC.md §7.2 tier-formation
+ §9.2 asymptotic convergence).

Verified before building (code-wins-on-fact): on the knot, block_type is mostly-degenerate
(~70% entity), but the **embedding-centroid semantic axis separates** (pairwise cosine spans
0.04–0.87, p10=0.28/p90=0.53 — wide). So the primary boosting learner is semantic proximity,
orthogonal to the exhausted target signal.

Weak learners, strongest-first (each attacks the residual the prior could not split):
  1. embedding-centroid semantic proximity (strong on this residual)
  2. block_type profile / operation(CRUD) role (secondary, for semantically-tied sub-residuals)

Boosting loop: on a residual, cluster by the strongest separating learner into cohesive
sub-groups; a sub-group that is cohesive AND within the cognitive-load span → confident seed; a
sub-group still too large/incoherent → recurse with the next learner; a residual NO learner can
separate → a genuine irreducible knot for the reasoning tier (kept-and-flagged, L3c, never
force-split — manufacturing a boundary is the divisive blind spot we guard against).

Prove-before-codify: this run REPORTS what the boost separates; it does not yet enshrine the
principle. Immutable-assets: reads WIP copy; writes nothing unless --commit.

CLI:
    python ontology_epic_boost.py <wip_db> <stills_dir> --dry-run
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict, Counter
from pathlib import Path

import numpy as np

import ontology_epic_seed as S

# Cohesion / span parameters — DIAGNOSTIC, not gates optimized-to. A sub-group is "coherent
# enough to seed" if its within-group mean cosine clears the residual's own p75 (relative, not a
# magic absolute), and "holdable" if within the cognitive span (recurse if larger).
COHESION_REL_PCTILE = 75     # a seed sub-group must be tighter than the residual's own p75 sim
COG_SPAN_SOFT = 30           # soft span ceiling (diagnostic; ~Dunbar-order, recurse above it)


def load_story_concepts(conn):
    s2c = defaultdict(list)
    for src, dst in conn.execute("SELECT src, dst FROM ontology_edges WHERE criterion='assembled_backbone'"):
        s2c[dst].append(src)
    return s2c


def story_centroids(conn, stories, s2c):
    """story_id -> unit-normalized mean embedding of its member concepts' representative blocks."""
    uc_contrib = {}
    for uc, cids in conn.execute("SELECT concept_id, contributor_ids FROM unified_concepts WHERE contributor_ids IS NOT NULL"):
        try:
            uc_contrib[uc] = json.loads(cids)
        except (json.JSONDecodeError, TypeError):
            pass
    need = set()
    for st in stories:
        for c in s2c[st]:
            for b in uc_contrib.get(c, [])[:1]:
                need.add(b)
    emb = {}
    need = list(need)
    for i in range(0, len(need), 900):
        chunk = need[i:i+900]
        for bid, blob in conn.execute(
            "SELECT block_id, embedding FROM vectors WHERE block_id IN (%s)" % ",".join("?"*len(chunk)), chunk):
            if blob:
                emb[bid] = np.frombuffer(blob, dtype=np.float32)
    cent = {}
    for st in stories:
        vs = [emb[b] for c in s2c[st] for b in uc_contrib.get(c, [])[:1] if b in emb]
        if vs:
            v = np.mean(vs, axis=0)
            n = np.linalg.norm(v)
            if n > 0:
                cent[st] = v / n
    return cent


TRIM_MARGIN = 0.05   # a boundary story moves A->B only if sim(B)-sim(A) exceeds this margin
                     # (a real margin, not >, so ties don't churn)


def build_lattice(cent, span=COG_SPAN_SOFT):
    """PASS 1 — take the model to the ~span (30) lattice, then LOCK it. Greedy: each unassigned
    story seeds a cell; pull in nearest unassigned until the cell hits `span`; move on. Cells are
    span-sized by construction (the intended initial lattice, NOT an artifact). Returns
    assignment {story: cell_idx} and the list of member-lists. NOTE: cells are LOCKED after this
    — no Lloyd iteration, no centroid drift (that is the entropy that collapses to a yarn ball)."""
    keys = list(cent.keys())
    M = np.array([cent[k] for k in keys])
    n = len(keys)
    assigned = [False] * n
    cells = []
    for i in range(n):
        if assigned[i]:
            continue
        members = [i]
        assigned[i] = True
        # nearest unassigned to THIS seed (fixed seed vector — no drift), up to span
        sims_i = M @ M[i]
        order = np.argsort(-sims_i)
        for j in order:
            if len(members) >= span:
                break
            if not assigned[j]:
                members.append(int(j))
                assigned[j] = True
        cells.append(members)
    assign = {}
    for ci, mem in enumerate(cells):
        for m in mem:
            assign[keys[m]] = ci
    return keys, M, assign, cells


def cell_contexts(M, cells):
    """Aggregate context per cell = unit-normalized mean of its members (computed ONCE, from the
    locked lattice; never re-derived during trim — the lock)."""
    ctx = np.zeros((len(cells), M.shape[1]), dtype=np.float32)
    for ci, mem in enumerate(cells):
        v = M[mem].mean(axis=0)
        ctx[ci] = v / (np.linalg.norm(v) + 1e-9)
    return ctx


def trim_pass(keys, M, assign, cells, ctx, margin=TRIM_MARGIN):
    """PASS 2 — SINGLE bounded trim (NOT iterate-to-fixpoint). For each story, compare its fit to
    its own locked cell vs the BEST OTHER cell context. Move A->B iff sim(B)-sim(A) > margin.
    Cells/contexts do NOT update mid-pass (locked) — this is edge-alignment, not re-clustering,
    so there is no attractor dynamic and no yarn-ball collapse. Returns new assignment + move
    stats (the entropy tell: a large move fraction means the lock isn't holding)."""
    key_idx = {k: i for i, k in enumerate(keys)}
    own_sim = np.array([float(M[key_idx[k]] @ ctx[assign[k]]) for k in keys])
    all_sims = M @ ctx.T                      # (n_stories, n_cells) fit to every cell context
    new_assign = dict(assign)
    moves = 0
    for k in keys:
        i = key_idx[k]
        a = assign[k]
        row = all_sims[i].copy()
        row[a] = -1.0                          # exclude own cell
        b = int(np.argmax(row))
        if row[b] - own_sim[i] > margin:
            new_assign[k] = b
            moves += 1
    return new_assign, moves


def main():
    ap = argparse.ArgumentParser(description="Epic-tier boosted separation of the contested knot")
    ap.add_argument("db_path")
    ap.add_argument("stills_dir")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db_path)
    try:
        stills = Path(args.stills_dir)
        story_tgt, total = S.load_story_targets(conn, stills)
        idf, df, hubs, cut = S.target_idf(story_tgt)
        cores = S.agglomerative_cores(story_tgt, idf)
        cleaves = S.divisive_cleaves(story_tgt, idf)
        conf, knots = S.converge(cores, cleaves)
        if not knots:
            print(json.dumps({"note": "no knots to boost"}, indent=2)); return
        giant = max(knots, key=lambda k: k["size"])
        knot = set(giant["stories"])
        s2c = load_story_concepts(conn)
        cent = story_centroids(conn, knot, s2c)

        # PASS 1: lattice to the ~30 span, LOCKED
        keys, M, assign, cells = build_lattice(cent, span=COG_SPAN_SOFT)
        ctx = cell_contexts(M, cells)
        key_idx = {k: i for i, k in enumerate(keys)}
        # cohesion BEFORE trim = mean(story · own-cell context)
        coh_before = float(np.mean([M[key_idx[k]] @ ctx[assign[k]] for k in keys]))

        # PASS 2: single bounded trim (NOT to fixpoint)
        new_assign, moves = trim_pass(keys, M, assign, cells, ctx)
        # recompute cell membership + contexts AFTER the single trim (for reporting cohesion only)
        cells_after = defaultdict(list)
        for k, ci in new_assign.items():
            cells_after[ci].append(key_idx[k])
        ctx_after = {ci: (M[mem].mean(axis=0) / (np.linalg.norm(M[mem].mean(axis=0)) + 1e-9))
                     for ci, mem in cells_after.items()}
        coh_after = float(np.mean([M[key_idx[k]] @ ctx_after[new_assign[k]] for k in keys]))

        sizes_before = sorted((len(m) for m in cells), reverse=True)
        sizes_after = sorted((len(m) for m in cells_after.values()), reverse=True)
        move_frac = moves / len(keys)
        report = {
            "knot_size": len(knot),
            "stories_with_centroid": len(cent),
            "lattice_cells": len(cells),
            "lattice_span": COG_SPAN_SOFT,
            "trim_moves": moves,
            "trim_move_fraction": round(move_frac, 3),
            "cohesion_before_trim": round(coh_before, 4),
            "cohesion_after_trim": round(coh_after, 4),
            "cohesion_delta": round(coh_after - coh_before, 4),
            "cell_size_dist_before_top10": sizes_before[:10],
            "cell_size_dist_after_top10": sizes_after[:10],
            "cells_after": len(cells_after),
        }
        # ── GATE (prove-or-abort, stated up front) ──
        # Fruitful iff: (a) the trim IMPROVES aggregate cohesion (delta > 0 — the trim is doing
        # useful edge-alignment), and (b) the move fraction is a SENSIBLE MINORITY (the lock is
        # holding — a large fraction moving means the lattice is sliding toward the yarn-ball
        # collapse, i.e. the lock failed). Both must hold.
        abort = []
        if report["cohesion_delta"] <= 0:
            abort.append(f"trim did not improve cohesion (delta={report['cohesion_delta']}) — no useful alignment")
        if move_frac > 0.35:
            abort.append(f"trim moved {move_frac:.0%} — lock not holding, sliding toward collapse")
        report["abort_reasons"] = abort
        report["verdict"] = ("ABORT — " + "; ".join(abort)) if abort else \
            "FRUITFUL — locked lattice + bounded trim improved cohesion with a minority of edge moves"
        # eyeball a few resulting cells for coherence
        uc_title = {r[0]: r[1] for r in conn.execute("SELECT concept_id, title FROM unified_concepts")}
        ex = []
        for ci, mem in sorted(cells_after.items(), key=lambda kv: -len(kv[1]))[:6]:
            titles = []
            for mi in mem[:3]:
                st = keys[mi]
                if s2c[st]:
                    titles.append(uc_title.get(s2c[st][0], "")[:50])
            ex.append({"size": len(mem), "sample_titles": titles})
        report["cell_examples"] = ex
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

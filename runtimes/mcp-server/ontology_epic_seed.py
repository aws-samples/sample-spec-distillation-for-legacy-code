"""Epic-tier seed generation — adversarial dual-pass → informed seeds (Phase-1, epic tier).

Story: knowledge-layer/ontology-classification, story→epic (T-1 second half). Implements the
FIRST half of the codified tier-formation method (SYSTEM_SPEC.md §7.2 "tier-formation is
seeded-steering"): run a divisive (top-down) and an agglomerative (bottom-up) pass over the
story space adversarially, then converge them into SEEDS —
  - agreement  → a CONFIDENT seed (independent-method agreement = convergence-is-confidence, L12)
  - disagreement → a CONTESTED KNOT (the §9.2 freeform-reasoning gap; adjudication deferred to
                   a later reasoned/LLM step, then injected as a deliberately-placed seed)
This module does NOT produce the final epics. It produces seeds + a knot queue. The population
resolves against seeds in a later step (seed steers, artifact governs, starved seeds drop).

Grounded on measured structure (see run notes): only ~21% of stories carry a code
dependency-target, and the raw co-occurrence graph is hub-driven (a few "company-master"-style
hub targets fuse ~86% into one component) while ~64% of targets are discriminative (touch <=3
stories). So the structural passes DOWN-WEIGHT hubs (TF-IDF-style) to expose real fault lines,
and seed only where the structural signal is real — seeds need not cover the whole population
(that is the point of seeding vs. partitioning; the embedding channel extends coverage in the
later resolve step).

Mechanism-free re: any specific clustering algorithm — this is the structural seed step; the
principle fixes the architecture, not the library.

Immutable-assets: reads the WIP copy; writes seed/knot tables into the WIP copy only.

CLI:
    python ontology_epic_seed.py <wip_db> <stills_dir> --dry-run
    python ontology_epic_seed.py <wip_db> <stills_dir> --commit
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import datetime as _dt
from collections import defaultdict, Counter
from pathlib import Path

# A target seen in more than this fraction of target-bearing stories is a HUB — near-useless as
# a discriminator (it fuses everything), down-weighted toward zero. NOT a hard gate on grouping;
# a weight in the affinity, and reported. (Diagnostic value, not a magic-number cutoff.)
HUB_STORY_FRACTION = 0.03   # ~ appears in >3% of target-bearing stories => hub-like
MIN_SHARED_DISCRIMINATIVE = 1  # two stories co-seed if they share >=1 discriminative target


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def load_story_targets(conn, stills_dir: Path):
    """story_id -> Counter(dep_target -> #member-concepts touching it). Only targets that
    resolve to a real code_edges dst (via the crosswalk) count."""
    s2c = defaultdict(list)
    for src, dst in conn.execute("SELECT src, dst FROM ontology_edges WHERE criterion='assembled_backbone'"):
        s2c[dst].append(src)
    cs = json.load(open(stills_dir / "concept_symbols.json"))
    by_concept = cs.get("by_concept", {})
    uc_bkey = {r[0]: r[1] for r in conn.execute("SELECT uc_id, block_key FROM concept_crosswalk")}
    dst_all = {r[0].lower() for r in conn.execute("SELECT DISTINCT dst FROM code_edges") if r[0]}
    story_tgt = defaultdict(Counter)
    for st, concepts in s2c.items():
        for cid in concepts:
            bk = uc_bkey.get(cid)
            if not bk:
                continue
            for s in (by_concept.get(bk, {}).get("symbols") or []):
                sl = s.lower()
                if sl in dst_all:
                    story_tgt[st][sl] += 1
    return dict(story_tgt), len(s2c)


def target_idf(story_tgt: dict):
    """Inverse-story-frequency weight per target: rare (discriminative) targets weigh high,
    hub targets weigh ~0. This is the hub down-weighting that exposes fault lines."""
    n_stories = len(story_tgt)
    tgt_df = Counter()
    for tc in story_tgt.values():
        for t in tc:
            tgt_df[t] += 1
    hub_cut = max(3, int(HUB_STORY_FRACTION * n_stories))
    idf = {}
    hubs = set()
    for t, df in tgt_df.items():
        if df > hub_cut:
            idf[t] = 0.0
            hubs.add(t)
        else:
            idf[t] = math.log((n_stories + 1) / (df + 1)) + 1.0
    return idf, tgt_df, hubs, hub_cut


# ── Agglomerative pass (bottom-up): cohesive cores by DISCRIMINATIVE shared targets ──

def agglomerative_cores(story_tgt: dict, idf: dict):
    """Union stories that share a discriminative (non-hub) target. Down-weighting hubs is what
    keeps this from re-fusing into the giant component. Returns list of frozensets (cores)."""
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        r = x
        while parent[r] != r:
            r = parent[r]
        while parent[x] != r:
            parent[x], x = r, parent[x]
        return r
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    # index stories by each discriminative target
    tgt_stories = defaultdict(list)
    for st, tc in story_tgt.items():
        for t in tc:
            if idf.get(t, 0.0) > 0.0:  # discriminative only
                tgt_stories[t].append(st)
    for t, sts in tgt_stories.items():
        for i in range(1, len(sts)):
            union(sts[0], sts[i])
    comps = defaultdict(set)
    for st in story_tgt:
        if any(idf.get(t, 0.0) > 0.0 for t in story_tgt[st]):
            comps[find(st)].add(st)
    return [frozenset(v) for v in comps.values() if len(v) >= 2]


# ── Divisive pass (top-down): the giant component's dominant-target cleavages ──

def divisive_cleaves(story_tgt: dict, idf: dict):
    """Assign each target-bearing story to the cleave keyed by its single highest-IDF-weighted
    target (its most discriminative resource). This is the top-down 'natural fault line' view:
    two stories land together iff their most-discriminative resource is the same. Returns
    {cleave_key: set(stories)}."""
    cleaves = defaultdict(set)
    for st, tc in story_tgt.items():
        best_t, best_w = None, -1.0
        for t, cnt in tc.items():
            w = idf.get(t, 0.0) * cnt
            if w > best_w:
                best_w, best_t = w, t
        if best_t is not None and idf.get(best_t, 0.0) > 0.0:
            cleaves[best_t].add(st)
    return {k: v for k, v in cleaves.items() if len(v) >= 2}


# ── Converge: agreement -> confident seed; disagreement -> contested knot ──

def converge(cores, cleaves):
    """A CONFIDENT seed = a set of stories that BOTH passes group together (a core that sits
    inside one cleave, i.e. the two independent methods agree). A CONTESTED KNOT = a core that
    straddles multiple cleaves (methods disagree on where its stories belong)."""
    # map story -> cleave key
    st_cleave = {}
    for ck, sts in cleaves.items():
        for st in sts:
            st_cleave[st] = ck
    confident = []
    knots = []
    for core in cores:
        cleave_keys = Counter(st_cleave.get(st) for st in core if st in st_cleave)
        cleave_keys.pop(None, None)
        if len(cleave_keys) == 1:
            # both methods agree: this core lives in one cleave -> confident seed
            confident.append({"stories": sorted(core), "cleave": next(iter(cleave_keys)),
                              "size": len(core)})
        elif len(cleave_keys) >= 2:
            # methods disagree on where the core's stories belong -> contested knot
            knots.append({"stories": sorted(core), "straddles": dict(cleave_keys),
                          "size": len(core)})
        # cleave_keys empty => core has no divisive assignment (all-hub); left for later channel
    return confident, knots


def ensure_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ontology_epic_seed (
            seed_id TEXT PRIMARY KEY, kind TEXT, cleave_key TEXT,
            story_count INTEGER, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ontology_epic_seed_member (
            seed_id TEXT, story_id TEXT, PRIMARY KEY (seed_id, story_id)
        );
        CREATE TABLE IF NOT EXISTS ontology_epic_knot (
            knot_id TEXT PRIMARY KEY, straddles TEXT, story_count INTEGER, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ontology_epic_knot_member (
            knot_id TEXT, story_id TEXT, PRIMARY KEY (knot_id, story_id)
        );
        """
    )
    conn.commit()


def _sid(prefix, stories):
    import hashlib
    h = hashlib.sha256("\x1f".join(sorted(stories)).encode()).hexdigest()
    return f"{prefix}_{h[:16]}"


def main():
    ap = argparse.ArgumentParser(description="Epic-tier seed generation (adversarial dual-pass → seeds + knots)")
    ap.add_argument("db_path")
    ap.add_argument("stills_dir")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db_path)
    try:
        story_tgt, total_stories = load_story_targets(conn, Path(args.stills_dir))
        idf, tgt_df, hubs, hub_cut = target_idf(story_tgt)
        cores = agglomerative_cores(story_tgt, idf)
        cleaves = divisive_cleaves(story_tgt, idf)
        confident, knots = converge(cores, cleaves)
        report = {
            "total_stories": total_stories,
            "target_bearing_stories": len(story_tgt),
            "distinct_targets": len(tgt_df),
            "hub_targets_downweighted": len(hubs), "hub_cut_story_count": hub_cut,
            "agglomerative_cores": len(cores),
            "divisive_cleaves": len(cleaves),
            "confident_seeds": len(confident),
            "contested_knots": len(knots),
            "stories_in_confident_seeds": sum(s["size"] for s in confident),
            "stories_in_knots": sum(k["size"] for k in knots),
        }
        if not args.commit:
            report["mode"] = "dry-run (nothing written)"
            report["confident_seed_examples"] = confident[:5]
            report["knot_examples"] = knots[:3]
            print(json.dumps(report, indent=2, sort_keys=True))
            return
        ensure_tables(conn)
        now = _now()
        for s in confident:
            sid = _sid("eseed", s["stories"])
            conn.execute("INSERT OR REPLACE INTO ontology_epic_seed VALUES (?,?,?,?,?)",
                         (sid, "confident", s["cleave"], s["size"], now))
            for st in s["stories"]:
                conn.execute("INSERT OR REPLACE INTO ontology_epic_seed_member VALUES (?,?)", (sid, st))
        for k in knots:
            kid = _sid("eknot", k["stories"])
            conn.execute("INSERT OR REPLACE INTO ontology_epic_knot VALUES (?,?,?,?)",
                         (kid, json.dumps(k["straddles"], sort_keys=True), k["size"], now))
            for st in k["stories"]:
                conn.execute("INSERT OR REPLACE INTO ontology_epic_knot_member VALUES (?,?)", (kid, st))
        conn.commit()
        report["mode"] = "committed"
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

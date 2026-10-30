"""Epic-tier annealing — name → merge-by-name → discard-and-recut → fixed point.

Story: knowledge-layer/ontology-classification, story→epic. Operates on the locked lattice+trim
cells from `ontology_epic_boost.py`. The annealing coordinate is the cluster NAME (a semantic
compression of its members). Cycle:
  1. NAME each cell (LLM reasons a theme from member titles).
  2. MERGE candidates by name-similarity (reasoning over the compressed handles, cheap).
  3. RE-SPLIT each merged cell: DISCARD the name, seek a DECISIVE cut line (a boosting attribute
     that cleanly bifurcates the members with a margin); split + re-name iff a clean cut exists,
     else the merge stands. This falsifies name-collision merges against the ground-truth stories.
  4. Compare to prior round. Changed → anneal again (cooler bars). Unchanged, or a merge↔resplit
     2-cycle on a pair → FIXED POINT; freeze; record 2-cycle pairs as STABLE-DISTINCT findings.
  5. Bounded max cycles (ELE discipline — never unbounded).

Merge and split are ADVERSARIAL (merge→fewer/bigger, split→more/nuanced); the balance point is
the natural epic granularity — it falls out, not targeted. Asymptotic (§9.2): each cycle closer;
stop at the fixed point, never claim "correct."

All LLM calls go through the bounded-emit protocol (AUTHORING_CONTRACT.md): validate → recast-
with-error → deterministic salvage-last, never fabricate. Observability: per-cycle + per-call
heartbeat (L8). Cost tracked. Immutable-assets: WIP copy only.

Prove-or-abort: fruitful iff it reaches a fixed point in a few cycles AND the final grouping is
different/more-nuanced than the raw lattice. Abort if it oscillates unboundedly (never cools) or
just reproduces the lattice (naming added nothing).

CLI:
    python ontology_epic_anneal.py <wip_db> <stills_dir> --profile P --smoke      # name a few, check
    python ontology_epic_anneal.py <wip_db> <stills_dir> --profile P --max-cycles 4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
import datetime as _dt
from collections import defaultdict
from pathlib import Path

import numpy as np

import bedrock_client
import json_extract
import ontology_epic_seed as S
import ontology_epic_boost as B

DEFAULT_MODEL = "anthropic.claude-haiku-4-5"
PRICE_IN, PRICE_OUT = 1.0, 5.0
MAX_JSON_RECASTS = 2
# Cooling schedule for the ABSOLUTE population (centroid-cos) merge bar. Ambient pairwise
# centroid-cos is ~0.74; genuine near-duplicate cells are ~0.85–0.93. Start just below the
# near-dup band so real twins merge, and RAISE the bar each cycle so merge pressure → 0 and the
# anneal freezes (the temperature the relative bar lacked). These are diagnostic starting points,
# not gates optimized-to; the fixed point (not a target count) is what we read.
MERGE_BAR_START = 0.85
MERGE_BAR_COOL = 0.03         # bar rises 0.03/cycle → cooling
MAX_CYCLES_DEFAULT = 6        # ELE bound


def _cell_id(stories) -> str:
    """Stable, deterministic identity of a cell = hash of its sorted member-story set.

    This is the SAME function the commit block uses to mint epic_id. Findings key on THIS
    (a stable membership-derived id), never on the LLM-generated name — the name is a per-cycle,
    non-deterministic HANDLE and using it as a cross-event join key is the two-island / name-merge
    defect class (identity carried by a generated label blurs at the macro cross without a
    deterministic bridge). Membership IS identity at the epic tier; the bridge is this hash.
    """
    return "epic_" + hashlib.sha256("\x1f".join(sorted(stories)).encode()).hexdigest()[:16]


def _now():
    return _dt.datetime.now(_dt.UTC).isoformat()


# ── rebuild the locked lattice+trim cells (deterministic; same as boost) ──

def rebuild_cells(conn, stills, scope="all"):
    """Lattice+trim over the chosen story SCOPE. scope='all' = the whole story space (Option A —
    one coherent system-wide epic map, knot and non-knot merge-tested together); scope='knot' =
    only the structural contested knot (legacy). The lattice+trim+anneal method is corpus-
    agnostic, so 'all' is the correct uniform pass."""
    s2c = B.load_story_concepts(conn)
    if scope == "knot":
        story_tgt, _ = S.load_story_targets(conn, stills)
        idf, df, hubs, cut = S.target_idf(story_tgt)
        cores = S.agglomerative_cores(story_tgt, idf)
        cleaves = S.divisive_cleaves(story_tgt, idf)
        conf, knots = S.converge(cores, cleaves)
        scope_stories = set(max(knots, key=lambda k: k["size"])["stories"])
    else:  # all backbone-assembled stories
        scope_stories = set(r[0] for r in conn.execute(
            "SELECT DISTINCT dst FROM ontology_edges WHERE criterion='assembled_backbone'"))
    cent = B.story_centroids(conn, scope_stories, s2c)
    keys, M, assign, cells = B.build_lattice(cent, span=B.COG_SPAN_SOFT)
    ctx = B.cell_contexts(M, cells)
    new_assign, moves = B.trim_pass(keys, M, assign, cells, ctx)
    final = defaultdict(list)
    for k, ci in new_assign.items():
        final[ci].append(k)
    return dict(final), keys, M, {k: i for i, k in enumerate(keys)}, cent, s2c


# ── bounded-emit LLM call (validate → recast → salvage-last) ──

def _llm_json(llm, model, system, user, want="object", max_tokens=400):
    tok_in = tok_out = 0
    cur = user
    for attempt in range(MAX_JSON_RECASTS + 1):
        try:
            res = bedrock_client.call_llm(llm, model, system, cur, max_tokens=max_tokens, temperature=0.0)
        except Exception as e:
            return None, tok_in, tok_out, f"call_error: {type(e).__name__}: {e}"
        tok_in += res.input_tokens; tok_out += res.output_tokens
        try:
            return json.loads(json_extract.strip_json(res.output_text, want)), tok_in, tok_out, None
        except Exception as e:
            cur = (f"{user}\n\nYour previous reply was NOT valid JSON ({type(e).__name__}). "
                   "Return ONLY one valid JSON object, no fence, escape inner quotes as \\\".")
    return None, tok_in, tok_out, "unparseable_after_recasts"


NAME_SYS = ("You name the single business theme shared by a set of software-spec concepts. "
            "Emit STRICT JSON: {\"name\":\"<=6 words\",\"topic\":\"<one domain word>\"}. No prose.")
CUT_SYS = ("You are given concepts in one cluster. Decide if they cleanly split into TWO coherent "
           "sub-themes on a single discriminating attribute, or are one unified theme. Emit STRICT "
           "JSON: {\"decisive\":true|false,\"attribute\":\"<what splits them or empty>\","
           "\"name_a\":\"\",\"name_b\":\"\"}. decisive=true ONLY for a clean bifurcation.")


def name_cell(llm, model, titles):
    sample = "\n".join(f"- {t[:70]}" for t in titles[:15])
    r, ti, to, err = _llm_json(llm, model, NAME_SYS, f"Concepts:\n{sample}\n\nName the theme.")
    if r is None:
        return {"name": "UNNAMED", "topic": "unknown"}, ti, to, err
    return {"name": r.get("name", "UNNAMED")[:60], "topic": (r.get("topic") or "unknown")[:30]}, ti, to, None


def recut_cell(llm, model, titles):
    sample = "\n".join(f"- {t[:70]}" for t in titles[:20])
    r, ti, to, err = _llm_json(llm, model, CUT_SYS,
                               f"Concepts (name discarded):\n{sample}\n\nClean two-way split, or unified?")
    if r is None:
        return {"decisive": False}, ti, to, err
    return r, ti, to, None


_name_model = None

def _get_name_model():
    """Local sentence embedding model (same as the store's knowledge.py path), offline."""
    global _name_model
    if _name_model is None:
        import os
        os.environ["HF_HUB_OFFLINE"] = "1"; os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from sentence_transformers import SentenceTransformer
        _name_model = SentenceTransformer("all-mpnet-base-v2")
    return _name_model


def name_embeddings(names, conn):
    """SEMANTIC name similarity — embed the cell names with the local model and compare by
    cosine. This catches topical collapse that token-overlap misses (e.g. 'Payment Processing
    Controls' ≈ 'Vendor Payment Controls', or the several '...Access Control...' facets). The
    merge is a SEMANTIC-reasoning prefilter as specified, not string overlap; the re-split still
    falsifies any merge that shouldn't hold. Returns fn(i,j)->cosine."""
    model = _get_name_model()
    E = model.encode(names, batch_size=64, show_progress_bar=False, normalize_embeddings=True)
    E = np.asarray(E, dtype=np.float32)
    def sim(i, j):
        return float(E[i] @ E[j])
    return sim


def main():
    ap = argparse.ArgumentParser(description="Epic-tier annealing (name → merge → recut → fixed point)")
    ap.add_argument("db_path"); ap.add_argument("stills_dir")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--profile", default=""); ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--max-cycles", type=int, default=MAX_CYCLES_DEFAULT)
    ap.add_argument("--smoke", action="store_true", help="name only the first 5 cells and stop (cheap check)")
    ap.add_argument("--commit", action="store_true", help="persist the final epic map (candidate — human-validate)")
    ap.add_argument("--scope", choices=["all", "knot"], default="all", help="story scope (Option A = all)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db_path)
    stills = Path(args.stills_dir)
    cells, keys, M, key_idx, cent, s2c = rebuild_cells(conn, stills, scope=args.scope)
    uc_title = {r[0]: r[1] for r in conn.execute("SELECT concept_id, title FROM unified_concepts")}
    def cell_titles(story_ids):
        ts = []
        for st in story_ids:
            for c in s2c[st][:2]:
                ts.append(uc_title.get(c, ""))
        return ts

    llm = bedrock_client.create_llm_client(args.profile, args.region)
    tok_in = tok_out = 0
    t0 = time.time()

    cell_list = list(cells.items())
    print(f"[anneal] rebuilt {len(cell_list)} lattice+trim cells", flush=True)

    if args.smoke:
        print("[anneal] SMOKE: naming first 5 cells", flush=True)
        for ci, sids in cell_list[:5]:
            nm, ti, to, err = name_cell(llm, args.model, cell_titles(sids))
            tok_in += ti; tok_out += to
            print(f"  cell {ci} (n={len(sids)}): {nm['name']}  [{nm['topic']}]"
                  + (f"  ERR={err}" if err else ""), flush=True)
        print(json.dumps({"smoke_cost_usd": round(tok_in/1e6*PRICE_IN + tok_out/1e6*PRICE_OUT, 4),
                          "in_tok": tok_in, "out_tok": tok_out}, indent=2), flush=True)
        conn.close(); return

    # ── full anneal (bounded cycles) ──
    def name_all(cell_items):
        nonlocal tok_in, tok_out
        names = []
        for idx, (ci, sids) in enumerate(cell_items):
            nm, ti, to, err = name_cell(llm, args.model, cell_titles(sids))
            tok_in += ti; tok_out += to
            names.append(nm["name"])
            if (idx + 1) % 10 == 0:
                print(f"[anneal] named {idx+1}/{len(cell_items)} "
                      f"(${(tok_in/1e6*PRICE_IN+tok_out/1e6*PRICE_OUT):.2f})", flush=True)
        return names

    def cell_centroids(cell_story_lists):
        """Unit centroid per cell in the real concept-embedding space (the POPULATION, not the
        name). This is the merge discriminator — diagnosis proved name-cosine tested the lossy
        label (2/1326 triggers) while population-centroid carries the real signal."""
        cs = []
        for sids in cell_story_lists:
            idxs = [key_idx[st] for st in sids if st in key_idx]
            v = M[idxs].mean(axis=0)
            cs.append(v / (np.linalg.norm(v) + 1e-9))
        return np.array(cs, dtype=np.float32)

    cur_cells = [sids for _, sids in cell_list]
    history = []
    stable_distinct = []
    for cycle in range(args.max_cycles):
        names = name_all(list(enumerate(cur_cells)))   # names are the human HANDLE only now
        # MERGE on POPULATION proximity (centroid cosine), gated by an ABSOLUTE bar with a
        # COOLING schedule — the temperature the relative-bar version lacked. A relative (p95)
        # bar re-creates merge pressure every cycle (there is always a top-5%) → limit cycle,
        # never freezes. An absolute bar that RISES each cycle makes merge pressure decrease to
        # zero, so the anneal cools and settles. Only genuinely-near-identical populations merge
        # (ambient centroid-cos ~0.74; near-dups are 0.85–0.93), and the bar climbs past them.
        CC = cell_centroids(cur_cells)
        sims = CC @ CC.T
        n = len(cur_cells)
        merge_bar = MERGE_BAR_START + MERGE_BAR_COOL * cycle
        merged = []
        used = [False] * n
        merges_this_cycle = 0
        for i in range(n):
            if used[i]:
                continue
            group = [i]
            used[i] = True
            for j in range(i + 1, n):
                if not used[j] and sims[i, j] >= merge_bar:   # POPULATION collapse → merge candidate
                    group.append(j); used[j] = True; merges_this_cycle += 1
            merged_stories = [st for gi in group for st in cur_cells[gi]]
            merged.append((group, merged_stories))
        # RE-SPLIT each merged cell: discard name, seek decisive cut
        next_cells = []
        splits_this_cycle = 0
        for group, stories in merged:
            if len(group) > 1:  # was a merge — test it
                cut, ti, to, err = recut_cell(llm, args.model, cell_titles(stories))
                tok_in += ti; tok_out += to
                if cut.get("decisive"):
                    # merge was name-collision; split back by re-running lattice on THIS subset
                    sub_cent = {st: cent[st] for st in stories if st in cent}
                    sk, sM, sassign, scells = B.build_lattice(sub_cent, span=B.COG_SPAN_SOFT)
                    parts = defaultdict(list)
                    for st, ci in sassign.items():
                        parts[ci].append(st)
                    for p in parts.values():
                        next_cells.append(p)
                    splits_this_cycle += 1
                    if len(group) == 2:  # 2-cycle candidate: merged then immediately re-split
                        # Record by STABLE MEMBERSHIP identity, not by the per-cycle name.
                        # Keep each cell's member set so commit-time can resolve to the final
                        # epic by containment even if the cell is later absorbed/re-split.
                        ci0, ci1 = cur_cells[group[0]], cur_cells[group[1]]
                        stable_distinct.append({
                            "cell_ids": [_cell_id(ci0), _cell_id(ci1)],
                            "members": [sorted(ci0), sorted(ci1)],
                            "names_at_record": [names[group[0]], names[group[1]]],
                            "cycle": cycle,
                        })
                else:
                    next_cells.append(stories)  # merge stands
            else:
                next_cells.append(stories)
        changed = len(next_cells) != len(cur_cells)
        hb = {"cycle": cycle, "cells_in": len(cur_cells), "merges": merges_this_cycle,
              "splits": splits_this_cycle, "cells_out": len(next_cells),
              "cost_usd": round(tok_in/1e6*PRICE_IN + tok_out/1e6*PRICE_OUT, 2)}
        print(f"[anneal] {json.dumps(hb)}", flush=True)
        history.append(hb)
        cur_cells = next_cells
        if not changed and merges_this_cycle == 0 and splits_this_cycle == 0:
            break  # fixed point

    # final naming for the result
    final_names = name_all(list(enumerate(cur_cells)))
    elapsed = time.time() - t0
    # ── prove-or-abort ──
    abort = []
    if len(history) >= args.max_cycles and history[-1]["merges"] + history[-1]["splits"] > 0:
        abort.append("did not reach a fixed point within max cycles — may be oscillating (never cooled)")
    if len(cur_cells) == len(cell_list) and not stable_distinct:
        abort.append("final grouping == raw lattice — annealing added nothing")
    # duplicate-name pathology: if the SAME final name appears >1×, the merge under-fired
    from collections import Counter as _Cn
    dupes = {n: c for n, c in _Cn(final_names).items() if c > 1}
    if dupes:
        abort.append(f"duplicate final epic names {dupes} — merge under-fired (over-fragmented)")
    # ── Resolve each finding's two cells to FINAL epics by MEMBERSHIP CONTAINMENT ──
    # The deterministic bridge: a finding was recorded by the two cells' member sets, not their
    # (per-cycle, non-deterministic) names. Final epics partition the story space, so each story
    # maps to exactly one final epic; a recorded cell resolves to the final epic holding the
    # PLURALITY of its members. This survives later merges/re-splits of that cell — the exact
    # staleness that keying on the name suffered. (L3d: a cell that resolves to nothing, or whose
    # pair collapses to a single final epic, is dropped-and-flagged, never fabricated.)
    story2final = {}
    final_id_by_index = []
    for idx, stories in enumerate(cur_cells):
        eid = _cell_id(stories)
        final_id_by_index.append(eid)
        for st in stories:
            story2final[st] = eid
    final_name_by_id = {final_id_by_index[i]: final_names[i] for i in range(len(cur_cells))}

    def _resolve_cell_to_final(members):
        counts = _Cn(story2final[st] for st in members if st in story2final)
        if not counts:
            return None
        return counts.most_common(1)[0][0]

    resolved_findings = []       # [{epic_ids:[a,b], epic_names:[..], names_at_record:[..], cycle}]
    dropped_findings = []        # {names_at_record, why}
    for f in stable_distinct:
        a = _resolve_cell_to_final(f["members"][0])
        b = _resolve_cell_to_final(f["members"][1])
        if a is None or b is None:
            dropped_findings.append({"names_at_record": f["names_at_record"], "why": "cell resolved to no final epic"})
            continue
        if a == b:
            # the two adjacent-but-distinct cells ended up inside ONE final epic — the tension
            # was absorbed by a later merge; it is no longer a cross-epic finding.
            dropped_findings.append({"names_at_record": f["names_at_record"], "why": "both cells → same final epic (merged away)"})
            continue
        resolved_findings.append({
            "epic_ids": [a, b],
            "epic_names": [final_name_by_id[a], final_name_by_id[b]],
            "names_at_record": f["names_at_record"],
            "cycle": f["cycle"],
        })

    report = {
        "lattice_cells_in": len(cell_list),
        "final_epics": len(cur_cells),
        "cycles_run": len(history),
        "history": history,
        "stable_distinct_recorded": len(stable_distinct),
        "stable_distinct_resolved": len(resolved_findings),
        "stable_distinct_dropped": len(dropped_findings),
        "stable_distinct_pairs": [rf["epic_names"] for rf in resolved_findings[:20]],
        "final_epic_names": final_names,
        "total_cost_usd": round(tok_in/1e6*PRICE_IN + tok_out/1e6*PRICE_OUT, 2),
        "elapsed_s": round(elapsed, 1),
        "abort_reasons": abort,
        "verdict": ("ABORT — " + "; ".join(abort)) if abort else
                   "FRUITFUL — annealed to a fixed point, grouping refined vs raw lattice",
    }
    print(json.dumps(report, indent=2), flush=True)

    if args.commit and not abort:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ontology_epic (
                epic_id TEXT PRIMARY KEY, name TEXT, story_count INTEGER, source TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ontology_epic_member (
                epic_id TEXT, story_id TEXT, PRIMARY KEY (epic_id, story_id)
            );
            CREATE TABLE IF NOT EXISTS ontology_epic_finding (
                kind TEXT, detail TEXT, created_at TEXT
            );
            """
        )
        now = _now()
        # whole-space anneal supersedes any prior (e.g. knot-only) epic map — clear then rewrite
        conn.execute("DELETE FROM ontology_epic")
        conn.execute("DELETE FROM ontology_epic_member")
        conn.execute("DELETE FROM ontology_epic_finding")
        for stories, nm in zip(cur_cells, final_names):
            eid = _cell_id(stories)   # SAME id used for finding resolution — one bridge, no divergence
            conn.execute("INSERT OR REPLACE INTO ontology_epic VALUES (?,?,?,?,?)",
                         (eid, nm, len(stories), f"{args.scope}_anneal", now))
            for st in stories:
                conn.execute("INSERT OR REPLACE INTO ontology_epic_member VALUES (?,?)", (eid, st))
        # Findings persisted by RESOLVED epic_id (the deterministic bridge), names as annotation.
        for rf in resolved_findings:
            conn.execute("INSERT INTO ontology_epic_finding VALUES (?,?,?)",
                         ("stable_distinct", json.dumps(rf), now))
        conn.commit()
        print(f"[anneal] committed {len(cur_cells)} epics + {len(resolved_findings)} stable-distinct findings "
              f"(by epic_id; {len(dropped_findings)} dropped-and-flagged) "
              f"(non-deterministic candidate map — human-validate; see §9.2)", flush=True)
    conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""story_dedup.py — story-tier near-duplicate anneal (generalized Finding-3 resolution).

THE DEFECT (measured, not assumed). The story tier is connected components of concept↔concept
co_membership edges (`ontology_assemble.py`). The comparator/residue tiers judge co-membership
CONCEPT-to-CONCEPT; they never ask the STORY-level question "do these two whole stories assert
the SAME capability, worded differently?". So one capability stated three ways
(formula / prose / sign-rule) lands as three separate stories — over-fragmentation the reader
sees as near-dupes in the epic's story index.

THE DISCRIMINATOR (proven, prove-before-codify, 2026-08-23). Three facets were tested on a
known near-dupe triplet vs its high-cosine-but-distinct siblings, same embedder as the store
(all-mpnet-base-v2, local):
  - blended title+body centroid (the existing vector): does NOT separate (gap −0.075 — a
    distinct sibling "auto release" vs "manual release" outscored every true dupe).
  - predicate/spine embedding: does NOT separate (gap −0.161 — conflates same-OPERATION with
    same-CAPABILITY; manual vs automatic release share a mechanism).
  - TITLE-only embedding: SEPARATES (gap +0.110; true dupes ≥0.658, distinct siblings ≤0.548).
A true near-dupe is "the same capability NAMED two ways" — and the name is exactly where
"same capability" lives, stripped of topic-prose and operation-mechanism. So the recall+
precision gate is intra-epic TITLE-facet cosine ≥ a floor (~0.60, safely inside the proven
gap), and — because an embedding is never a merge AUTHORITY — an LLM judge confirms each
surviving candidate before any edge is written.

THE MERGE (non-destructive, reversible, deterministic re-derivation). A confirmed merge is
enacted by ADDING confident `co_membership` reasoning edges (criterion `neardupe_title_llm`)
that bridge the stories' concepts — the SAME edge family/shape the comparator writes — so a
re-run of `ontology_assemble.py` fuses the components into one story by the existing union-find.
No story is edited or deleted here; the story tier is re-DERIVED from the augmented edge set.
Reversal: `DELETE FROM ontology_edges WHERE relation='co_membership' AND criterion='neardupe_title_llm'`
then re-assemble (or restore the pre-run snapshot). All edge writes are into the WIP copy.

Generalizes to ANY source tree: the only signals are titles, embeddings, and an LLM
same-capability judgment — no domain vocabulary, no hardcoded module list.

CLI (run from runtimes/mcp-server):
    # dry-run: embed, gate, JUDGE (spends LLM), REPORT proposed merges — writes NO edges:
    python story_dedup.py <wip_db> --profile P --dry-run
    # commit: same, then write the bridging co_membership edges (still re-assemble separately):
    python story_dedup.py <wip_db> --profile P --commit
"""
from __future__ import annotations

import os
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import argparse
import json
import sqlite3
import time
from collections import defaultdict

import numpy as np

import bedrock_client

PRICE_IN, PRICE_OUT = 1.0, 5.0            # Haiku 4.5 $/1M tokens (same meter as the rest)
TITLE_FLOOR = 0.60                        # proven recall+precision gate (probe 2026-08-23)
CO_CRITERION = "neardupe_title_llm"       # reversibility tag for the edges this writes
HEARTBEAT_EVERY = 200


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.UTC).isoformat()


# ── load the story tier + title-facet text (proven discriminator input) ──────
def load_stories(conn):
    story_epic = {s: e for s, e in conn.execute(
        "SELECT src, dst FROM ontology_edges WHERE relation='story_in_epic'")}
    s2c = defaultdict(list)
    for src, dst in conn.execute("SELECT src, dst FROM ontology_edges WHERE relation='concept_in_story'"):
        s2c[dst].append(src)
    title = {r[0]: r[1] for r in conn.execute("SELECT concept_id, title FROM unified_concepts")}
    btype = {r[0]: r[1] for r in conn.execute("SELECT concept_id, block_type FROM unified_concepts")}
    return story_epic, s2c, title, btype


def title_text(sid, s2c, title):
    """The proven facet input: the story's member-concept titles, joined. Titles carry the
    'same capability' signal; joining all members (not just the lead) is robust to which
    concept happens to sort first."""
    return " ; ".join(sorted(title.get(c, "") for c in s2c[sid]))


def embed_titles(texts):
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-mpnet-base-v2")
    V = model.encode(texts, batch_size=64, show_progress_bar=False)
    V = np.array(V, dtype=np.float32)
    V = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-10)
    return V


# ── candidate gate: intra-epic title-cosine >= floor ─────────────────────────
def candidate_pairs(story_epic, s2c, title, floor):
    epic_stories = defaultdict(list)
    for s, e in story_epic.items():
        if s in s2c:
            epic_stories[e].append(s)
    stories = [s for s in story_epic if s in s2c]
    V = embed_titles([title_text(s, s2c, title) for s in stories])
    idx = {s: i for i, s in enumerate(stories)}
    pairs = []
    for e, sts in epic_stories.items():
        if len(sts) < 2:
            continue
        M = V[[idx[s] for s in sts]]
        S = M @ M.T
        for i in range(len(sts)):
            for j in range(i + 1, len(sts)):
                c = float(S[i, j])
                if c >= floor:
                    pairs.append((c, sts[i], sts[j], e))
    pairs.sort(reverse=True)
    return pairs


# ── LLM judge: same capability, or distinct? (precision authority) ───────────
JUDGE_SYS = (
    "You decide whether two software-specification STORIES describe the SAME capability stated "
    "differently, or GENUINELY DISTINCT capabilities. Same-capability means a reader would be "
    "confused to see both as separate entries — they assert the same rule/behavior/obligation, "
    "just worded or scoped differently (e.g. 'release cannot exceed hold' vs 'release amount "
    "must not exceed amount on hold'). DISTINCT means a real difference in trigger, operation, "
    "scope, or subject (e.g. AUTOMATIC release vs MANUAL release; a period LOCK vs a purge "
    "BOUNDARY; a rule vs the stored-proc it depends on). When unsure, answer DISTINCT (never "
    "merge away a real difference). Reply with ONLY a compact JSON object: "
    '{"same": true|false, "why": "<=12 words"}.'
)


def judge_pair(llm, model, a_titles, b_titles):
    user = ("STORY A concepts:\n- " + "\n- ".join(a_titles[:8]) +
            "\n\nSTORY B concepts:\n- " + "\n- ".join(b_titles[:8]) +
            "\n\nSame capability or distinct?")
    res = bedrock_client.call_llm(llm, model, JUDGE_SYS, user, max_tokens=120, temperature=0.0)
    txt = (res.output_text or "").strip()
    same = False
    why = ""
    try:
        j0, j1 = txt.find("{"), txt.rfind("}")
        obj = json.loads(txt[j0:j1 + 1]) if j0 >= 0 and j1 > j0 else {}
        same = bool(obj.get("same", False))
        why = str(obj.get("why", ""))[:80]
    except Exception:
        same = False
        why = "parse-failed→distinct(fail-safe)"
    return same, why, res.input_tokens, res.output_tokens


def union_find_merges(confirmed):
    """confirmed = list of (a,b). Return groups (list of sets) of stories to merge."""
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
    for a, b in confirmed:
        union(a, b)
    groups = defaultdict(set)
    for n in list(parent.keys()):
        groups[find(n)].add(n)
    return [g for g in groups.values() if len(g) >= 2]


def write_bridge_edges(conn, groups, s2c, btype):
    """Enact merges by adding confident co_membership edges that bridge each group's stories.
    Strategy: pick a canonical member concept per story (prefer a non-meta, spec-bearing block
    type; else the id-min), then link every other story's canonical concept to the group's
    single anchor. Union-find in ontology_assemble will then fuse the whole group into one
    story. Edges carry criterion=neardupe_title_llm for reversibility. Returns edges written."""
    import hashlib
    META = {"finding", "observation"}
    def canonical_concept(sid):
        cs = s2c[sid]
        spec = [c for c in cs if btype.get(c) not in META]
        pool = spec or cs
        return sorted(pool)[0]
    def edge_id(src, dst, family, criterion):
        h = hashlib.sha256(f"{src}\x1f{dst}\x1f{family}\x1f{criterion}".encode()).hexdigest()
        return "oe_" + h[:16]
    now = _now()
    written = 0
    for g in groups:
        stories = sorted(g)                      # deterministic anchor
        anchor = canonical_concept(stories[0])
        for other in stories[1:]:
            oc = canonical_concept(other)
            src, dst = sorted([anchor, oc])      # stable orientation
            eid = edge_id(src, dst, "reasoning", CO_CRITERION)
            ev = json.dumps({"reason": "story-tier near-dupe merge (title-facet + LLM judge)",
                             "group_size": len(stories)}, sort_keys=True)
            conn.execute(
                "INSERT OR REPLACE INTO ontology_edges "
                "(edge_id,src,dst,family,relation,criterion,classifier,verdict,confidence,evidence,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, src, dst, "reasoning", "co_membership", CO_CRITERION,
                 "llm_residue", "confident", 1.0, ev, now, now))
            conn.execute(
                "INSERT INTO ontology_log (edge_id,action,field,original_value,new_value,classifier,evidence,ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (eid, "insert", "*", None, "confident", "llm_residue", ev, now))
            written += 1
    conn.commit()
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="Story-tier near-duplicate anneal (Finding-3)")
    ap.add_argument("db", help="WIP store copy (mutated only with --commit)")
    ap.add_argument("--floor", type=float, default=TITLE_FLOOR, help="title-cosine gate (proven ~0.60)")
    ap.add_argument("--model", default="anthropic.claude-haiku-4-5")
    ap.add_argument("--profile", default=""); ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--concurrency", type=int, default=16, help="parallel judge workers")
    ap.add_argument("--max-group-size", type=int, default=3,
                    help="commit merges only for groups up to this size (larger groups are "
                         "horizontal-pattern over-fusions, NOT near-dupes — flagged, not merged)")
    ap.add_argument("--dry-run", action="store_true", help="judge + report; write NO edges")
    ap.add_argument("--commit", action="store_true", help="write bridging co_membership edges")
    ap.add_argument("--report", default="", help="optional path to write the full proposal JSON")
    ap.add_argument("--resume-report", default="",
                    help="reuse confirmed pairs from a prior --report JSON (skip re-judging/re-spend); "
                         "recompute groups + (optionally) commit under the current --max-group-size")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    story_epic, s2c, title, btype = load_stories(conn)

    # ── resume path: reuse a prior run's confirmed pairs (no embed, no LLM re-spend) ──
    if args.resume_report:
        prior = json.load(open(args.resume_report))
        confirmed = [(p["a"], p["b"]) for p in prior["proposals"] if p.get("same")]
        proposals = prior["proposals"]
        tin = prior["summary"].get("tok_in", 0); tout = prior["summary"].get("tok_out", 0)
        pairs = [(p["cosine"], p["a"], p["b"], p["epic"]) for p in proposals]
        t0 = time.time()
        print(json.dumps({"stage": "resume", "from": args.resume_report,
                          "confirmed_same_pairs": len(confirmed)}), flush=True)
        return _finish(conn, args, confirmed, proposals, pairs, s2c, btype, title, tin, tout, t0)

    t0 = time.time()
    pairs = candidate_pairs(story_epic, s2c, title, args.floor)
    print(json.dumps({"stage": "gate", "floor": args.floor, "candidate_pairs": len(pairs),
                      "embed_gate_s": round(time.time() - t0, 1)}), flush=True)
    if not pairs:
        print(json.dumps({"status": "no-candidates"}), flush=True)
        conn.close(); return 0

    # LLM judge (parallel, observable, no cost cap). Fail-safe = DISTINCT (never over-merge).
    from concurrent.futures import ThreadPoolExecutor, as_completed
    llm = bedrock_client.create_llm_client(args.profile, args.region)
    def work(p):
        c, a, b, e = p
        at = sorted(title.get(x, "") for x in s2c[a])
        bt = sorted(title.get(x, "") for x in s2c[b])
        same, why, ti, to = judge_pair(llm, args.model, at, bt)
        return (p, same, why, ti, to)

    confirmed = []; proposals = []; tin = tout = 0; done = 0
    tj = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(work, p) for p in pairs]
        for fut in as_completed(futs):
            (c, a, b, e), same, why, ti, to = fut.result()
            tin += ti; tout += to; done += 1
            rec = {"cosine": round(c, 3), "same": same, "why": why,
                   "a": a, "b": b, "epic": e,
                   "a_lead": (sorted(title.get(x, "") for x in s2c[a]) or [""])[0][:60],
                   "b_lead": (sorted(title.get(x, "") for x in s2c[b]) or [""])[0][:60]}
            proposals.append(rec)
            if same:
                confirmed.append((a, b))
            if done % HEARTBEAT_EVERY == 0 or done == len(pairs):
                cost = tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
                el = time.time() - tj
                rate = done / el if el > 0 else 0
                print(json.dumps({"stage": "judge", "done": f"{done}/{len(pairs)}",
                                  "confirmed_same": len(confirmed),
                                  "rate_per_s": round(rate, 1), "cost_usd": round(cost, 2)}),
                      flush=True)

    return _finish(conn, args, confirmed, proposals, pairs, s2c, btype, title, tin, tout, t0)


def _finish(conn, args, confirmed, proposals, pairs, s2c, btype, title, tin, tout, t0):
    """Apply the group-size cap, split committed (near-dupe) from flagged (horizontal over-
    fusion), optionally write edges, report. Group size is the transitivity guard: pairwise
    'same' unioned transitively can collapse a whole HORIZONTAL (same pattern, distinct module
    instances) into one story — those are a DIFFERENT finding (horizontal consolidation), so we
    commit only small groups (true near-dupes) and FLAG large ones for separate review."""
    all_groups = union_find_merges(confirmed)
    cap = args.max_group_size
    commit_groups = [g for g in all_groups if len(g) <= cap]
    flagged_groups = [g for g in all_groups if len(g) > cap]
    cost = tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
    summary = {"stage": "result", "candidate_pairs": len(pairs),
               "confirmed_same_pairs": len(confirmed),
               "merge_groups_total": len(all_groups),
               "max_group_size": cap,
               "commit_groups": len(commit_groups),
               "stories_merged_away": sum(len(g) - 1 for g in commit_groups),
               "flagged_large_groups": len(flagged_groups),
               "flagged_stories_held": sum(len(g) for g in flagged_groups),
               "tok_in": tin, "tok_out": tout, "cost_usd": round(cost, 2),
               "elapsed_s": round(time.time() - t0, 1)}

    if args.report:
        def lead(s):
            ts = sorted(title.get(c, "") for c in s2c[s]); return ts[0][:70] if ts else s
        with open(args.report, "w") as f:
            json.dump({"summary": summary,
                       "groups": [sorted(g) for g in all_groups],
                       "commit_groups": [sorted(g) for g in commit_groups],
                       "flagged_large_groups": [
                           {"size": len(g), "stories": [lead(s) for s in sorted(g)]}
                           for g in sorted(flagged_groups, key=len, reverse=True)],
                       "proposals": sorted(proposals, key=lambda r: -r["cosine"])}, f, indent=2)
        summary["report"] = args.report

    if args.commit and not args.dry_run:
        written = write_bridge_edges(conn, commit_groups, s2c, btype)
        summary["bridge_edges_written"] = written
        summary["mode"] = "committed (small groups only; large groups flagged, not merged)"
        summary["reversal"] = (f"DELETE FROM ontology_edges WHERE relation='co_membership' "
                               f"AND criterion='{CO_CRITERION}'; then re-run ontology_assemble.py")
        summary["next"] = ("re-run: ontology_assemble.py <db> --layer backbone --commit (+ overlay), "
                           "then re-persist story_in_epic + re-project")
    else:
        summary["mode"] = "dry-run (no edges written)"

    print(json.dumps(summary, indent=2), flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

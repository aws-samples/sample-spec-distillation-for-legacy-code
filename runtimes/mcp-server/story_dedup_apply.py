#!/usr/bin/env python3
"""story_dedup_apply.py — surgically apply confirmed near-dupe merges to the story tier.

WHY SURGICAL (measured, not assumed). The stored story tier is ground truth: every stored
story id == hash(its stored member concept set) (verified for the full stored set). Re-running
the full union-find over the live `verdict='confident'` edge set does NOT reproduce the stored
partition (the live confident-edge set differs from what the original assembly saw — count
matches, ids do not), so a blind re-assemble would REPARTITION EVERY story, not surgically merge
the small set of confirmed near-dupes. That is an unacceptable blast radius for a targeted fix.

So this module takes the STORED membership as the base and mutates ONLY the merge groups: for
each group (all its stories share one epic — verified invariant), union the member sets into one
new story (new id = hash of the union, the same identity function), rewrite that group's
`concept_in_story` + `story_in_epic` + `ontology_story` + `ontology_epic_member` +
`ontology_story_object` from the old ids to the one new id, and adjust `ontology_epic.story_count`.
Every story NOT in a merge group is left byte-identical. Deterministic, minimal, reversible
(the pre-run snapshot + the criterion-tagged co_membership edges both allow undo).

Reads the merge plan from a story_dedup.py --report (its `commit_groups`). Idempotent-safe: if a
group is already merged (its new id present, old ids absent) it is skipped.

CLI (run from runtimes/mcp-server):
    python story_dedup_apply.py <wip_db> --report <committed_report.json> --dry-run
    python story_dedup_apply.py <wip_db> --report <committed_report.json> --commit
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sqlite3
from collections import defaultdict


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def story_id(members) -> str:
    return "story_" + hashlib.sha256("\x1f".join(sorted(members)).encode()).hexdigest()[:16]


def edge_id(src, dst, family, criterion) -> str:
    h = hashlib.sha256(f"{src}\x1f{dst}\x1f{family}\x1f{criterion}".encode()).hexdigest()
    return "oe_" + h[:16]


def load_membership(conn):
    """stored story -> set(concepts), for the backbone tier (the projected tier)."""
    s2c = defaultdict(set)
    for src, dst in conn.execute(
        "SELECT src, dst FROM ontology_edges WHERE relation='concept_in_story'"):
        s2c[dst].add(src)
    return s2c


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply confirmed near-dupe merges surgically")
    ap.add_argument("db")
    ap.add_argument("--report", required=True, help="story_dedup.py committed report (commit_groups)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    plan = json.load(open(args.report))
    groups = [sorted(g) for g in plan["commit_groups"]]

    conn = sqlite3.connect(args.db)
    s2c = load_membership(conn)
    story_epic = {s: e for s, e in conn.execute(
        "SELECT src, dst FROM ontology_edges WHERE relation='story_in_epic'")}

    # criterion tag on the existing membership/epic edges (to mint replacements consistently)
    cis_crit = conn.execute(
        "SELECT criterion FROM ontology_edges WHERE relation='concept_in_story' LIMIT 1").fetchone()
    cis_crit = cis_crit[0] if cis_crit else "assembled_backbone"
    sie_crit = conn.execute(
        "SELECT criterion FROM ontology_edges WHERE relation='story_in_epic' LIMIT 1").fetchone()
    sie_crit = sie_crit[0] if sie_crit else "part3-persist-epics"

    planned = []      # (old_ids, new_id, epic, union_members)
    skipped = 0
    for g in groups:
        present = [s for s in g if s in s2c]
        if len(present) < 2:
            skipped += 1
            continue
        epics = {story_epic.get(s) for s in present}
        if len(epics) != 1 or None in epics:
            skipped += 1                     # invariant guard: only intra-epic merges
            continue
        epic = epics.pop()
        union = set()
        for s in present:
            union |= s2c[s]
        new_id = story_id(union)
        planned.append((present, new_id, epic, sorted(union)))

    summary = {
        "groups_in_plan": len(groups),
        "groups_applicable": len(planned),
        "groups_skipped": skipped,
        "stories_before": len(s2c),
        "stories_removed": sum(len(old) for old, _, _, _ in planned),
        "stories_added": len(planned),
        "stories_after_expected": len(s2c) - sum(len(old) for old, _, _, _ in planned) + len(planned),
    }

    if not (args.commit and not args.dry_run):
        summary["mode"] = "dry-run (no writes)"
        # show a couple examples
        summary["examples"] = [
            {"old": old, "new": nid, "epic": ep, "members": len(mem)}
            for old, nid, ep, mem in planned[:5]]
        print(json.dumps(summary, indent=2), flush=True)
        conn.close()
        return 0

    now = _now()
    epic_delta = defaultdict(int)   # epic -> change in story_count
    for old_ids, new_id, epic, members in planned:
        # 1) concept_in_story: remove old (concept->old_story) edges, add (concept->new_story)
        for old in old_ids:
            for concept in s2c[old]:
                conn.execute("DELETE FROM ontology_edges WHERE edge_id=?",
                             (edge_id(concept, old, "reasoning", cis_crit),))
        for concept in members:
            eid = edge_id(concept, new_id, "reasoning", cis_crit)
            conn.execute(
                "INSERT OR REPLACE INTO ontology_edges "
                "(edge_id,src,dst,family,relation,criterion,classifier,verdict,confidence,evidence,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, concept, new_id, "reasoning", "concept_in_story", cis_crit,
                 "assembly", "confident", 1.0,
                 json.dumps({"merged_from": old_ids, "via": "neardupe_title_llm"}), now, now))
        # 2) story_in_epic: remove old story->epic, add new story->epic (same epic)
        for old in old_ids:
            conn.execute("DELETE FROM ontology_edges WHERE edge_id=?",
                         (edge_id(old, epic, "reasoning", sie_crit),))
        conn.execute(
            "INSERT OR REPLACE INTO ontology_edges "
            "(edge_id,src,dst,family,relation,criterion,classifier,verdict,confidence,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (edge_id(new_id, epic, "reasoning", sie_crit), new_id, epic, "reasoning",
             "story_in_epic", sie_crit, "assembly", "unsure_kept", 0.6,
             json.dumps({"merged_from": old_ids}), now, now))
        # 3) ontology_story table
        for old in old_ids:
            conn.execute("DELETE FROM ontology_story WHERE story_id=?", (old,))
        conn.execute(
            "INSERT OR REPLACE INTO ontology_story (story_id, layer, member_count, created_at) "
            "VALUES (?,?,?,?)", (new_id, "backbone", len(members), now))
        # 4) ontology_epic_member (if present)
        for old in old_ids:
            conn.execute("DELETE FROM ontology_epic_member WHERE story_id=?", (old,))
        conn.execute("INSERT OR REPLACE INTO ontology_epic_member (epic_id, story_id) VALUES (?,?)",
                     (epic, new_id))
        # 5) ontology_story_object: remap old story ids -> new (merge ops)
        for old in old_ids:
            for co, ops in conn.execute(
                "SELECT code_object, ops FROM ontology_story_object WHERE story_id=?", (old,)).fetchall():
                conn.execute("INSERT OR REPLACE INTO ontology_story_object (story_id,code_object,ops) VALUES (?,?,?)",
                             (new_id, co, ops))
            conn.execute("DELETE FROM ontology_story_object WHERE story_id=?", (old,))
        # net story_count change for this epic: -len(old_ids) + 1
        epic_delta[epic] += (1 - len(old_ids))
    # 6) ontology_epic.story_count
    for epic, delta in epic_delta.items():
        conn.execute("UPDATE ontology_epic SET story_count = story_count + ? WHERE epic_id=?",
                     (delta, epic))
    conn.commit()

    # verify against ground truth
    after = conn.execute("SELECT COUNT(DISTINCT dst) FROM ontology_edges WHERE relation='story_in_epic'").fetchone()[0]
    cis_stories = conn.execute("SELECT COUNT(DISTINCT dst) FROM ontology_edges WHERE relation='concept_in_story'").fetchone()[0]
    summary["mode"] = "committed"
    summary["story_in_epic_distinct_stories_after"] = after
    summary["concept_in_story_distinct_stories_after"] = cis_stories
    summary["epics_touched"] = len(epic_delta)
    summary["reversal"] = ("restore the pre-run snapshot, OR: DELETE the neardupe co_membership "
                           "edges and rebuild the tier from the snapshot")
    print(json.dumps(summary, indent=2), flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

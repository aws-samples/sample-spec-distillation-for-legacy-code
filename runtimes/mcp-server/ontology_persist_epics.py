#!/usr/bin/env python3
"""
ontology_persist_epics.py — Phase-1 "Part 3": lift the committed epic map out of the
side tables (`ontology_epic*`) into `ontology_edges` PROPER so the graph carries the full
System→Epic→Story lattice as first-class, queryable edges that master-blend / projection
consume. Until this runs, epics live only in side tables and are invisible to the graph
walk that projection performs.

WHAT IT WRITES (additive, audited, idempotent — via ontology_classify.write_edge):
  - story_in_epic  (reasoning family): src=story_id (child) → dst=epic_id (parent).
    verdict = unsure_kept. The epic map is an LLM-in-the-loop anneal product: a
    NON-DETERMINISTIC, human-validatable CANDIDATE (SYSTEM_SPEC §9.2), not a deterministic
    artifact. Marking it `confident` would overclaim — `unsure_kept` is the honest three-state
    (L3c: kept + flagged, never dropped). classifier = llm_residue.
  - inconsistency  (findings family): the stable-distinct adjacent-but-distinct epic pairs
    the anneal's re-split falsifier surfaced (merged-then-immediately-resplit 2-cycles).
    src/dst = the two epic_ids; verdict = unsure_kept; criterion = stable_distinct_adjacent.
    A findings edge so a projection can SURFACE the tension, never silently merge/separate.

DISCIPLINE:
  - Immutable assets: writes ONLY to the working-copy DB passed in; baseline never touched.
    A pre-mutation snapshot of the exact at-risk tables is the caller's gate (see run notes).
  - Reversible: every edge carries criterion carrying the run source tag; the exact reversal
    is `DELETE FROM ontology_edges WHERE relation IN ('story_in_epic') AND criterion LIKE
    '<source>%'` (+ the findings rows), recorded in the run notes.
  - Observable (L8): heartbeat every N edges; a final JSON summary reconciled against ground
    truth (row counts before/after).
  - Idempotent (L6): edge_id is a stable hash; a second run UPDATES in place, never dupes.
  - No fabrication (L3d): a finding whose epic name does not resolve to exactly one epic_id
    is SKIPPED and reported, never guessed.

CLI:
  python ontology_persist_epics.py <wip_db> [--source all_anneal] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

import ontology_classify as OC


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", help="working-copy store (NEVER the baseline)")
    ap.add_argument("--source", default="all_anneal",
                    help="run source tag; becomes part of the edge criterion for reversibility")
    ap.add_argument("--hb", type=int, default=2000, help="heartbeat every N edges")
    ap.add_argument("--dry-run", action="store_true",
                    help="count what WOULD be written; write nothing")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    OC.ensure_tables(conn)  # idempotent; guarantees ontology_edges/ontology_log exist

    # ── ground truth BEFORE ──
    def _counts() -> dict[str, int]:
        return {
            r: conn.execute(
                "SELECT COUNT(*) FROM ontology_edges WHERE relation=?", (r,)
            ).fetchone()[0]
            for r in (OC.REL_CONCEPT_STORY, OC.REL_STORY_EPIC, OC.REL_INCONSISTENCY)
        }

    before = _counts()
    members = conn.execute(
        "SELECT m.story_id, m.epic_id, e.name FROM ontology_epic_member m "
        "JOIN ontology_epic e ON e.epic_id=m.epic_id"
    ).fetchall()
    findings = conn.execute("SELECT kind, detail FROM ontology_epic_finding").fetchall()

    print(json.dumps({
        "phase": "part3-persist-epics", "source": args.source, "dry_run": args.dry_run,
        "before_edges": before,
        "to_write": {"story_in_epic": len(members), "findings_pairs": len(findings)},
    }), flush=True)

    if args.dry_run:
        print("[persist] dry-run — nothing written", flush=True)
        return 0

    crit = f"{args.source}"          # story_in_epic criterion (reversibility tag)
    crit_find = "stable_distinct_adjacent"
    epic_ids = {r[0] for r in conn.execute("SELECT epic_id FROM ontology_epic")}

    # ── story_in_epic (reasoning) ──
    written = 0
    for story_id, epic_id, epic_name in members:
        OC.write_edge(
            conn,
            src=story_id, dst=epic_id,
            family=OC.FAMILY_REASONING, relation=OC.REL_STORY_EPIC,
            classifier="llm_residue", verdict=OC.VERDICT_UNSURE_KEPT,
            criterion=crit, confidence=None,
            evidence={"epic_name": epic_name, "source": args.source,
                      "note": "non-deterministic anneal candidate (SYSTEM_SPEC §9.2)"},
        )
        written += 1
        if written % args.hb == 0:
            print(f"[persist] story_in_epic {written}/{len(members)}", flush=True)
    conn.commit()

    # ── inconsistency (findings): stable-distinct adjacent epic pairs ──
    # Findings are now recorded EPIC_ID-KEYED by the anneal (the label-identity fix): each
    # detail is {"epic_ids":[a,b], "epic_names":[..], ...}. We consume the stable ids directly —
    # no name→id resolution, no guessing. (L4/L3d: an id that is not a current epic is skipped
    # and flagged, never fabricated; legacy name-pair rows, if any, are likewise flagged.)
    find_written = 0
    find_skipped = []
    for kind, detail in findings:
        try:
            rec = json.loads(detail)
        except Exception:
            find_skipped.append({"detail": detail, "why": "unparseable detail"}); continue
        if not (isinstance(rec, dict) and isinstance(rec.get("epic_ids"), list) and len(rec["epic_ids"]) == 2):
            find_skipped.append({"detail": rec, "why": "not an epic_id-keyed finding (legacy/name-pair?) — rerun anneal to regenerate"})
            continue
        a, b = rec["epic_ids"]
        if a not in epic_ids or b not in epic_ids:
            find_skipped.append({"epic_ids": [a, b], "why": "epic_id not in current epic map"})
            continue
        OC.write_edge(
            conn,
            src=a, dst=b,
            family=OC.FAMILY_FINDINGS, relation=OC.REL_INCONSISTENCY,
            classifier="llm_residue", verdict=OC.VERDICT_UNSURE_KEPT,
            criterion=crit_find, confidence=None,
            evidence={"kind": kind, "epic_names": rec.get("epic_names"), "source": args.source,
                      "note": "merged-then-resplit 2-cycle: adjacent but distinct (anneal falsifier)"},
        )
        find_written += 1
    conn.commit()

    after = _counts()
    summary = {
        "phase": "part3-persist-epics", "source": args.source,
        "before_edges": before, "after_edges": after,
        "story_in_epic_written": written,
        "findings_written": find_written,
        "findings_skipped": find_skipped,
        "delta_story_in_epic": after[OC.REL_STORY_EPIC] - before[OC.REL_STORY_EPIC],
        "reversal": (
            f"DELETE FROM ontology_edges WHERE (relation='story_in_epic' AND criterion='{crit}') "
            f"OR (relation='inconsistency' AND criterion='{crit_find}');"
        ),
    }
    print(json.dumps(summary, indent=2), flush=True)

    # reconcile against ground truth — story_in_epic must now equal membership count
    ok = after[OC.REL_STORY_EPIC] == len(members)
    print(f"[persist] reconcile story_in_epic == memberships: {ok}", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

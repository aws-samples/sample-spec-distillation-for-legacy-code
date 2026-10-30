"""Ontology classification — persistence substrate (Phase-1 step 1).

Story: knowledge-layer/ontology-classification. This module owns the *persisted graph
structure* that the ontology classification pass writes: grouping edges (concept->story,
story->epic) and inconsistency (grey-failure) edges, each typed by family and carrying
provenance. It is the substrate steps 2+ (join comparator, deterministic tier, LLM residue
tier) write onto.

Design anchors (see spec/epics/knowledge-layer/stories/ontology-classification/):
  - R-1 / T-1  ontology membership is graph edges (concept->story, story->epic), each with
               classifier + confidence + evidence (provenance). A concept MAY have >1 edge.
  - R-5 / T-7  inconsistency (grey-failure) is a DISTINCT edge family, never a merge/drop.
  - R-6 / T-8  every edge typed by family (reasoning | findings) + provenance
               (classifier / evidence / confidence / verdict).
  - R-2 / T-4  verdict is three-state (confident | unsure_kept | rejected) — L3.
  - R-7 / T-9  edges persist into the shared store (record of truth), never a projected doc.
  - System L1  non-destructive: this writes into a WORKING COPY of the store; the baseline
               shared_vectors.db is never mutated in place. Every write is additive +
               audited (ontology_log), mirroring arbitrate.py's lossless discipline.
  - System L6  deterministic identity: an edge's identity is a stable hash of
               (src, dst, family, criterion), so re-running is idempotent (INSERT OR REPLACE
               on the same logical edge), modulo recorded LLM-residue jitter.

This step (Phase-1 step 1) is the SUBSTRATE ONLY: schema + audited write path + read-back
verification. The join comparator (step 2), deterministic tier (step 3/T-3), boundary policy
(T-5) and LLM residue tier (T-4) are NOT here — they land on top of this.

CLI:
    python ontology_classify.py <shared_vectors_db> --init      # create tables
    python ontology_classify.py <shared_vectors_db> --verify    # read back + report
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sqlite3

# ── Edge families + verdicts (the vocabulary; see R-6/T-8, R-2/T-4) ───────────

FAMILY_REASONING = "reasoning"   # grouping edges (concept->story, story->epic)
FAMILY_FINDINGS = "findings"     # inconsistency / grey-failure edges
FAMILIES = (FAMILY_REASONING, FAMILY_FINDINGS)

# Grouping relation kinds (reasoning family)
REL_CONCEPT_STORY = "concept_in_story"
REL_STORY_EPIC = "story_in_epic"
# Findings relation kind (findings family)
REL_INCONSISTENCY = "inconsistency"

# Three-state verdict (System L3 — anti-overfit; kept-and-flagged is first-class)
VERDICT_CONFIDENT = "confident"       # positive grouping/finding evidence
VERDICT_UNSURE_KEPT = "unsure_kept"   # ambiguous -> kept + flagged, NEVER dropped (L3c)
VERDICT_REJECTED = "rejected"         # rejected only on positive contrary evidence (L3d)
VERDICTS = (VERDICT_CONFIDENT, VERDICT_UNSURE_KEPT, VERDICT_REJECTED)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _edge_id(src: str, dst: str, family: str, criterion: str) -> str:
    """Stable, deterministic edge identity (System L6).

    Identity is a hash of the logical edge (endpoints + family + boundary criterion), NOT a
    positional ordinal and NOT the classifier/verdict — so re-classifying the same logical
    relationship with a firmer verdict UPDATES the same edge rather than duplicating it.
    16 hex chars matches the store's existing concept-id width (sha256[:16], tuning row 10).
    """
    h = hashlib.sha256(f"{src}\x1f{dst}\x1f{family}\x1f{criterion}".encode()).hexdigest()
    return "oe_" + h[:16]


# ── Schema (mirrors code_graph.py / arbitrate.py idioms) ──────────────────────

def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the ontology edge table + its audit log. Idempotent (IF NOT EXISTS).

    ontology_edges is a SIBLING of code_edges — same store, distinct table, distinct purpose.
    It is not merged into code_edges because code_edges is code-dependency fact; these are
    reasoning/findings classification over concepts.
    """
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS ontology_edges (
            edge_id     TEXT PRIMARY KEY,   -- stable hash id (L6); INSERT OR REPLACE idempotent
            src         TEXT NOT NULL,      -- concept_id (or story key) — the child
            dst         TEXT NOT NULL,      -- story key (or epic key)   — the parent
            family      TEXT NOT NULL,      -- '{FAMILY_REASONING}' | '{FAMILY_FINDINGS}' (T-8)
            relation    TEXT NOT NULL,      -- '{REL_CONCEPT_STORY}'|'{REL_STORY_EPIC}'|'{REL_INCONSISTENCY}'
            criterion   TEXT,               -- the boundary-policy criterion this rests on (T-5, R-4)
            classifier  TEXT NOT NULL,      -- who drew it: 'deterministic' | 'llm_residue' (T-2/T-3/T-4)
            verdict     TEXT NOT NULL,      -- three-state (L3): {'|'.join(VERDICTS)}
            confidence  REAL,               -- [0,1]; provenance (T-8)
            evidence    TEXT,               -- JSON: the signal (similarity/convergence/co-location refs) (T-8)
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ontology_edges_src ON ontology_edges(src);
        CREATE INDEX IF NOT EXISTS idx_ontology_edges_dst ON ontology_edges(dst);
        CREATE INDEX IF NOT EXISTS idx_ontology_edges_family ON ontology_edges(family);
        CREATE INDEX IF NOT EXISTS idx_ontology_edges_verdict ON ontology_edges(verdict);

        CREATE TABLE IF NOT EXISTS ontology_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            edge_id TEXT, action TEXT,          -- 'insert' | 'update'
            field TEXT, original_value TEXT, new_value TEXT,
            classifier TEXT, evidence TEXT, ts TEXT
        );
        """
    )
    conn.commit()


# ── Audited write path (additive + lossless; L1/L8) ───────────────────────────

def write_edge(
    conn: sqlite3.Connection,
    *,
    src: str,
    dst: str,
    family: str,
    relation: str,
    classifier: str,
    verdict: str,
    criterion: str | None = None,
    confidence: float | None = None,
    evidence: dict | None = None,
) -> str:
    """Insert or idempotently update one ontology edge, writing an audit row either way.

    Returns the edge_id. Validates the family/verdict vocabulary so a caller cannot write an
    untyped or two-state edge (guards R-6/T-8 and the L3 three-state contract at the seam).
    """
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {FAMILIES}, got {family!r}")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    if family == FAMILY_REASONING and relation not in (REL_CONCEPT_STORY, REL_STORY_EPIC):
        raise ValueError(f"reasoning family relation must be a grouping relation, got {relation!r}")
    if family == FAMILY_FINDINGS and relation != REL_INCONSISTENCY:
        raise ValueError(f"findings family relation must be {REL_INCONSISTENCY!r}, got {relation!r}")

    eid = _edge_id(src, dst, family, criterion or "")
    ev_json = json.dumps(evidence, sort_keys=True) if evidence is not None else None
    now = _now()

    existing = conn.execute(
        "SELECT verdict, confidence, evidence FROM ontology_edges WHERE edge_id=?", (eid,)
    ).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO ontology_edges "
            "(edge_id,src,dst,family,relation,criterion,classifier,verdict,confidence,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, src, dst, family, relation, criterion, classifier, verdict, confidence, ev_json, now, now),
        )
        conn.execute(
            "INSERT INTO ontology_log (edge_id,action,field,original_value,new_value,classifier,evidence,ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, "insert", "*", None, verdict, classifier, ev_json, now),
        )
    else:
        old_verdict = existing[0]
        conn.execute(
            "UPDATE ontology_edges SET classifier=?, verdict=?, confidence=?, evidence=?, updated_at=? "
            "WHERE edge_id=?",
            (classifier, verdict, confidence, ev_json, now, eid),
        )
        conn.execute(
            "INSERT INTO ontology_log (edge_id,action,field,original_value,new_value,classifier,evidence,ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, "update", "verdict", old_verdict, verdict, classifier, ev_json, now),
        )
    return eid


# ── Verification (read-back self-check) ───────────────────────────────────────

def verify(conn: sqlite3.Connection) -> dict:
    """Read the substrate back and report its shape. Also proves the tables exist and the
    family/verdict/relation vocabulary round-trips."""
    tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    report = {
        "ontology_edges_present": "ontology_edges" in tabs,
        "ontology_log_present": "ontology_log" in tabs,
    }
    if report["ontology_edges_present"]:
        report["edge_count"] = conn.execute("SELECT COUNT(*) FROM ontology_edges").fetchone()[0]
        report["by_family"] = dict(
            conn.execute("SELECT family, COUNT(*) FROM ontology_edges GROUP BY family").fetchall()
        )
        report["by_verdict"] = dict(
            conn.execute("SELECT verdict, COUNT(*) FROM ontology_edges GROUP BY verdict").fetchall()
        )
    if report["ontology_log_present"]:
        report["log_count"] = conn.execute("SELECT COUNT(*) FROM ontology_log").fetchone()[0]
    return report


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Ontology classification persistence substrate (Phase-1 step 1)")
    ap.add_argument("db_path", help="shared_vectors WORKING COPY to write into (never the baseline in place)")
    ap.add_argument("--init", action="store_true", help="create ontology_edges + ontology_log tables")
    ap.add_argument("--verify", action="store_true", help="read back + report substrate shape")
    ap.add_argument("--self-test", action="store_true",
                    help="write one edge of each family into the WORKING COPY, then verify + report (idempotency check)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db_path)
    try:
        if args.init or args.self_test:
            ensure_tables(conn)
        if args.self_test:
            # one grouping edge + one inconsistency edge; write twice to prove idempotency
            for _ in range(2):
                write_edge(conn, src="concept_demo_a", dst="story_demo", family=FAMILY_REASONING,
                           relation=REL_CONCEPT_STORY, criterion="shared_forcing_function",
                           classifier="deterministic", verdict=VERDICT_CONFIDENT, confidence=0.99,
                           evidence={"note": "self-test grouping edge"})
                write_edge(conn, src="concept_demo_a", dst="concept_demo_b", family=FAMILY_FINDINGS,
                           relation=REL_INCONSISTENCY, criterion="near_identical_never_colocated",
                           classifier="deterministic", verdict=VERDICT_UNSURE_KEPT, confidence=0.5,
                           evidence={"note": "self-test grey-failure edge"})
            conn.commit()
        if args.verify or args.self_test:
            print(json.dumps(verify(conn), indent=2, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

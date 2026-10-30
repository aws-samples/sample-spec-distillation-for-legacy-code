#!/usr/bin/env python3
"""
crosscheck.py — corroborate code-graph entity decodings against the business stills.

The code graph's `business_entities` decode cryptic names to domain nouns
(APVM -> "AP Vendor Master"). Those decodings are LLM inference. This pass
corroborates them against the actual business concepts the still pipeline
extracted (vectors.title + unified_concepts.title): if a decoding's noun phrase
matches a concept title, it is real signal.

ANTI-OVERFIT: this is a CONFIDENCE BOOSTER, never a dropper. A match sets
code_entities.stills_corroborated=1; a non-match sets 0 and is NEUTRAL — the
concept may be phrased differently or simply absent from the stills, which is
not evidence the decode is wrong. Nothing is deleted.

Deterministic (no LLM, no embeddings): builds a bigram set from concept titles
and checks whether any bigram of the decoded business_name appears in it.

CLI:
    python crosscheck.py <graph_db> [--concepts-db <db>]
    (concepts-db defaults to graph_db; in production both live in shared_vectors.db)
"""
from __future__ import annotations

import argparse
import re
import sqlite3

_STOP = {"the", "of", "a", "an", "and", "for", "to", "in", "on", "by", "with",
         "or", "entity", "table", "view"}


def _tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [t for t in toks if t not in _STOP and len(t) > 1]


def _bigrams(toks: list[str]) -> set[str]:
    return {f"{toks[i]} {toks[i+1]}" for i in range(len(toks) - 1)}


def build_concept_index(concepts_conn) -> tuple[set[str], set[str]]:
    """Return (bigram_set, distinctive_unigram_set) from concept titles."""
    from collections import Counter
    bigrams: set[str] = set()
    unigram_df: Counter = Counter()
    n_titles = 0
    tables_seen = 0
    for table, col in [("vectors", "title"), ("unified_concepts", "title")]:
        try:
            rows = concepts_conn.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL")
        except sqlite3.OperationalError:
            # Expected when one of the two candidate tables isn't present in this DB. Benign, but
            # note it so "table absent" is distinguishable from "table present but empty".
            print(f"[crosscheck] note: table '{table}' absent, skipping", flush=True)
            continue
        tables_seen += 1
        for (title,) in rows:
            toks = _tokens(title)
            if not toks:
                continue
            n_titles += 1
            bigrams |= _bigrams(toks)
            for t in set(toks):
                unigram_df[t] += 1
    if tables_seen == 0:
        print("[crosscheck] WARN neither 'vectors' nor 'unified_concepts' table present — "
              "concept index is empty (crosscheck will find no concept matches)", flush=True)
    # distinctive unigrams: appear, but not ubiquitously (< 2% of titles)
    cap = max(5, int(0.02 * n_titles))
    distinctive = {u for u, df in unigram_df.items() if df <= cap}
    return bigrams, distinctive


def crosscheck(graph_db: str, concepts_db: str = "") -> dict:
    conn = sqlite3.connect(graph_db)
    cconn = sqlite3.connect(concepts_db) if concepts_db and concepts_db != graph_db else conn

    bigrams, distinctive = build_concept_index(cconn)
    print(f"[crosscheck] concept index: {len(bigrams):,} bigrams, "
          f"{len(distinctive):,} distinctive unigrams")

    rows = conn.execute("SELECT rowid, business_name FROM code_entities").fetchall()
    corroborated = 0
    for rowid, bn in rows:
        toks = _tokens(bn)
        bg = _bigrams(toks)
        hit = bool(bg & bigrams)
        if not hit and len(toks) == 1:
            hit = toks[0] in distinctive  # single-word names: distinctive-unigram match
        conn.execute("UPDATE code_entities SET stills_corroborated=? WHERE rowid=?",
                     (1 if hit else 0, rowid))
        corroborated += int(hit)
    conn.commit()

    total = len(rows)
    stats = {
        "entities": total,
        "corroborated": corroborated,
        "corroboration_rate": round(corroborated / total, 3) if total else None,
        "note": "booster only — uncorroborated is neutral, not penalized/dropped",
    }
    if cconn is not conn:
        cconn.close()
    conn.close()
    print(f"[crosscheck] {stats}")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Corroborate entity decodings vs business stills")
    ap.add_argument("graph_db")
    ap.add_argument("--concepts-db", default="")
    args = ap.parse_args()
    crosscheck(args.graph_db, args.concepts_db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

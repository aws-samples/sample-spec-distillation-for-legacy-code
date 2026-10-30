#!/usr/bin/env python3
"""
arbitrate.py — Tier-1 (deterministic) global arbitration over the code graph.

After code_graph.py populates the graph, per-file guards leave a residue of
CONFLICTS and inconsistencies that can only be resolved with corpus-wide context.
This pass resolves them using accumulated GROUND TRUTH already in the graph —
no LLM. Whatever it cannot resolve deterministically is reported as the residue
for the (future) Tier-2 Sonnet arbiter.

Resolvers (all binding, all lossless):
  1. kind_normalize     — canonicalize kind labels (procedure -> stored_procedure).
  2. direction_by_dml   — direction_verdict='conflict' means write DML was positively
                          found while the LLM said non-write -> set direction='write'.
  3. kind_by_create     — a REFERENCES edge's kind that disagrees with the object's
                          true kind (from CREATE statements in code_objects) is
                          overturned to the CREATE ground-truth kind.
  4. entity_alias_merge — an unconfirmed-anchor entity (LLM-coined abbreviation) whose
                          business_name matches a CONFIRMED entity in the same file is
                          marked an alias of that canonical entity.

Binding but lossless: every change updates the graph field AND writes the original
value + evidence to `arbitration_log`. Inputs are never destroyed; changes are
fully reversible and auditable.

CLI:
    python arbitrate.py <shared_vectors_db>
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sqlite3
from collections import Counter, defaultdict

SONNET_MODEL = "us.anthropic.claude-sonnet-4-6"

KIND_CANON = {
    "proc": "stored_procedure", "procedure": "stored_procedure",
    "sproc": "stored_procedure", "sp": "stored_procedure",
    "tbl": "table", "vw": "view", "fn": "function", "udf": "function",
}


def _canon_kind(k):
    k = (k or "").strip().lower()
    return KIND_CANON.get(k, k)


def _now():
    return _dt.datetime.now(_dt.UTC).isoformat()


def ensure_log(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS arbitration_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_table TEXT, target_key TEXT, field TEXT,
            original_value TEXT, new_value TEXT,
            resolver TEXT, evidence TEXT, source TEXT, ts TEXT
        );
        """
    )
    conn.commit()


def _log(conn, table, key, field, old, new, resolver, evidence):
    conn.execute(
        "INSERT INTO arbitration_log "
        "(target_table,target_key,field,original_value,new_value,resolver,evidence,source,ts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (table, str(key), field, str(old), str(new), resolver, evidence, "deterministic", _now()))


# ── Resolver 1: kind label normalization ─────────────────────────────────────

def resolve_kind_normalize(conn) -> int:
    n = 0
    for rowid, kind in conn.execute("SELECT rowid, kind FROM code_objects WHERE kind IS NOT NULL").fetchall():
        c = _canon_kind(kind)
        if c != (kind or "").strip().lower():
            conn.execute("UPDATE code_objects SET kind=? WHERE rowid=?", (c, rowid))
            _log(conn, "code_objects", rowid, "kind", kind, c, "kind_normalize", "label canonicalization")
            n += 1
    for rowid, kind in conn.execute("SELECT rowid, kind FROM code_edges WHERE kind IS NOT NULL").fetchall():
        c = _canon_kind(kind)
        if c != (kind or "").strip().lower():
            conn.execute("UPDATE code_edges SET kind=? WHERE rowid=?", (c, rowid))
            _log(conn, "code_edges", rowid, "kind", kind, c, "kind_normalize", "label canonicalization")
            n += 1
    conn.commit()
    return n


# ── Resolver 2: direction conflicts -> write (regex found positive write DML) ─

def resolve_direction_by_dml(conn) -> int:
    rows = conn.execute(
        "SELECT rowid, src, dst, direction FROM code_edges WHERE direction_verdict='conflict'"
    ).fetchall()
    n = 0
    for rowid, src, dst, direction in rows:
        conn.execute(
            "UPDATE code_edges SET direction='write', direction_verdict='arbitrated', "
            "derived_confidence=0.95 WHERE rowid=?", (rowid,))
        _log(conn, "code_edges", rowid, "direction", direction, "write", "direction_by_dml",
             f"write DML positively matched for {dst} in {src}")
        n += 1
    conn.commit()
    return n


# ── Resolver 3: reference kind vs CREATE ground truth ─────────────────────────

def _create_kind_map(conn):
    by_bare = defaultdict(Counter)
    for obj, kind in conn.execute("SELECT object, kind FROM code_objects WHERE kind IS NOT NULL").fetchall():
        bare = (obj or "").split(".")[-1]
        if bare:
            by_bare[bare][_canon_kind(kind)] += 1
    # majority kind per bare name (ground truth from CREATE statements)
    return {b: c.most_common(1)[0][0] for b, c in by_bare.items() if c}


def resolve_kind_by_create(conn) -> int:
    # SQL ONLY: for SQL refs, kind (table/view/procedure/function) is the SAME
    # ontological axis as a CREATE's definitional kind, so a mismatch is a real
    # error. C# ref kinds (call/type/property_get/instantiate/...) are a DIFFERENT
    # axis (usage, not definition) and must NOT be "corrected" against CREATE kinds.
    # A trigger-attachment edge is exempt: `kind='trigger'` there names the RELATION (this file's
    # trigger is attached to that table), not the target's definitional kind, so "correcting" it to
    # the table's CREATE kind would silently undo the relation. code_graph.py also emits it as
    # type='TRIGGERS' for the same reason; this is the belt to that braces.
    kmap = _create_kind_map(conn)
    n = 0
    rows = conn.execute(
        "SELECT rowid, dst, kind FROM code_edges "
        "WHERE type='REFERENCES' AND kind IS NOT NULL AND lang='sql' "
        "AND COALESCE(operation,'') <> 'trigger_on_table'"
    ).fetchall()
    for rowid, dst, kind in rows:
        bare = (dst or "").split(".")[-1]
        truth = kmap.get(bare)
        if truth and _canon_kind(kind) != truth:
            conn.execute("UPDATE code_edges SET kind=? WHERE rowid=?", (truth, rowid))
            _log(conn, "code_edges", rowid, "kind", kind, truth, "kind_by_create",
                 f"CREATE ground-truth kind for {bare} is {truth}")
            n += 1
    conn.commit()
    return n


# ── Resolver 4: merge LLM-coined entity abbreviations to canonical ────────────

def _norm_name(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def resolve_entity_alias_merge(conn) -> int:
    # group entities by file
    by_file = defaultdict(list)
    for rowid, path, code_name, business_name, verdict in conn.execute(
        "SELECT rowid, path, code_name, business_name, anchor_verdict FROM code_entities"
    ).fetchall():
        by_file[path].append((rowid, code_name, business_name, verdict))
    n = 0
    for path, ents in by_file.items():
        confirmed = {_norm_name(bn): cn for (_id, cn, bn, v) in ents if v == "confirmed" and bn}
        for rowid, code_name, business_name, verdict in ents:
            if verdict == "confirmed" or not business_name:
                continue
            canon = confirmed.get(_norm_name(business_name))
            if canon and canon != code_name:
                conn.execute("UPDATE code_entities SET anchor_verdict='alias' WHERE rowid=?", (rowid,))
                _log(conn, "code_entities", rowid, "anchor_verdict", verdict, "alias",
                     "entity_alias_merge", f"{code_name} aliases canonical {canon} ('{business_name}')")
                n += 1
    conn.commit()
    return n


# ── Tier-2: Sonnet arbiter for the entity-abbreviation residue ───────────────

_T2_SYSTEM = (
    "You are a strict JUDGE deciding whether an extracted cryptic-name -> business "
    "decoding is trustworthy signal. For each item choose exactly one verdict:\n"
    "- alias_of:<canonical_code_name> - denotes the SAME entity as one of the listed "
    "confirmed canonicals in this file.\n"
    "- distinct - a real, separate domain entity whose decoding is well-supported: "
    "attested in the file/filename/context, or an unambiguous standard code (e.g. a "
    "state/tax/module abbreviation).\n"
    "- weak - the decoding is plausible but you have NO positive evidence either way "
    "(no local canonical, not attested here). This is KEPT and flagged, not discarded.\n"
    "- reject - ONLY when you have POSITIVE CONTRARY evidence the decoding is wrong or "
    "the token is not an entity: the token is a language keyword / loop variable / temp "
    "/ SQL fragment, is a substring fragment of a larger identifier, or the decoding "
    "directly contradicts the token's clear meaning in this file.\n"
    "CRITICAL: absence of a matching canonical, or absence of filename attestation, is "
    "NOT grounds to reject - absence is ambiguous (the decoding may be correct but its "
    "evidence simply is not in this file). When in doubt choose weak, never reject. "
    "Cite a one-line rationale that names your evidence. Output ONLY a JSON array."
)


def _strip_json(text: str) -> str:
    # Single source of truth: hardened balanced-scan (ignores fences + trailing
    # prose). Tier-2 returns a JSON array. Replaces the old naive first-'['/last-']'
    # slice that was susceptible to the trailing-prose failure residue already fixed.
    from json_extract import strip_json
    return strip_json(text, kind="array")


def resolve_entities_tier2(conn, model_id: str, profile: str, region: str,
                           concurrency: int = 8) -> dict:
    """LLM arbitration over the unconfirmed-anchor entity residue (batched per file).

    Observability + safety (per design tenets):
      - Concurrent Sonnet calls (network-bound) via a thread pool; DB writes are
        serialized in the main thread and COMMITTED PER FILE so the write lock is
        never held across network calls and progress persists.
      - Resumable/idempotent: only processes anchor_verdict='unconfirmed', so a
        killed run resumes cleanly on re-invocation.
      - Prints incremental progress (N/M files, running counts) with flush=True.
    """
    import bedrock_client
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time

    rows = conn.execute(
        "SELECT rowid, path, code_name, business_name FROM code_entities "
        "WHERE anchor_verdict='unconfirmed'"
    ).fetchall()
    if not rows:
        print("[arbitrate:tier2] no unconfirmed entity residue — nothing to do", flush=True)
        return {"arbitrated": 0, "alias": 0, "distinct": 0, "reject": 0, "files": 0, "errors": 0}

    by_file = defaultdict(list)
    for rowid, path, cn, bn in rows:
        by_file[path].append((rowid, cn, bn))

    canon = defaultdict(list)
    for path, cn, bn in conn.execute(
        "SELECT path, code_name, business_name FROM code_entities WHERE anchor_verdict='confirmed'"
    ).fetchall():
        canon[path].append((cn, bn))

    llm = bedrock_client.create_llm_client(profile, region)
    counts = {"arbitrated": 0, "alias": 0, "distinct": 0, "weak": 0, "reject": 0,
              "files": 0, "errors": 0}

    total_files = len(by_file)
    print(f"[arbitrate:tier2] {len(rows)} unconfirmed decodings across {total_files} "
          f"files; concurrency={concurrency}", flush=True)

    def _call(path, ents):
        cands = canon.get(path, [])
        cand_txt = "\n".join(f"  - {cn}: {bn}" for cn, bn in cands) or "  (none)"
        items = "\n".join(f"  {i}. {cn}: {bn}" for i, (rid, cn, bn) in enumerate(ents))
        user = (
            f"File: {path}\n\n"
            f"Confirmed canonical entities in this file:\n{cand_txt}\n\n"
            f"Unconfirmed decodings to arbitrate:\n{items}\n\n"
            "For each index emit {\"index\":N,\"judgment\":\"alias_of:<canonical_code_name>\""
            "|\"distinct\"|\"weak\"|\"reject\",\"rationale\":\"<cite specific evidence>\"}. "
            "Use alias_of ONLY when it clearly denotes the SAME entity as a listed canonical. "
            "Use reject ONLY with positive contrary evidence (keyword / fragment / "
            "contradiction). If there is no local canonical and no contrary evidence, use "
            "weak. Output ONLY a JSON array."
        )
        res = bedrock_client.call_llm(llm, model_id, _T2_SYSTEM, user,
                                         max_tokens=2048, temperature=0.0)
        return json.loads(_strip_json(res.output_text))

    done = 0
    t0 = _time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_call, path, ents): (path, ents) for path, ents in by_file.items()}
        for fut in as_completed(futures):
            path, ents = futures[fut]
            done += 1
            try:
                judgments = fut.result()
            except Exception:
                counts["errors"] += 1
                judgments = None
            if judgments is not None:
                counts["files"] += 1
                for j in judgments:
                    if not isinstance(j, dict) or "index" not in j:
                        continue
                    idx = j["index"]
                    if not (0 <= idx < len(ents)):
                        continue
                    rowid, cn, bn = ents[idx]
                    verdict = (j.get("judgment") or "").strip()
                    if verdict.startswith("alias_of:"):
                        new = "alias"; counts["alias"] += 1
                    elif verdict == "distinct":
                        new = "arbitrated_distinct"; counts["distinct"] += 1
                    elif verdict == "weak":
                        new = "arbitrated_uncorroborated"; counts["weak"] += 1
                    elif verdict == "reject":
                        new = "rejected"; counts["reject"] += 1
                    else:
                        continue
                    conn.execute("UPDATE code_entities SET anchor_verdict=? WHERE rowid=?", (new, rowid))
                    _log_t2(conn, "code_entities", rowid, "anchor_verdict", "unconfirmed", new,
                            f"{verdict} :: {j.get('rationale', '')[:120]}", model_id)
                    counts["arbitrated"] += 1
            conn.commit()  # per-file: release lock + persist progress (resumable)
            if done % 25 == 0 or done == total_files:
                rate = done / max(1e-6, _time.time() - t0)
                eta = (total_files - done) / rate if rate else 0
                print(f"[arbitrate:tier2] {done}/{total_files} files "
                      f"(alias={counts['alias']} distinct={counts['distinct']} "
                      f"weak={counts['weak']} reject={counts['reject']} "
                      f"err={counts['errors']}) ~{eta:.0f}s left", flush=True)
    return counts


def _log_t2(conn, table, key, field, old, new, evidence, model):
    conn.execute(
        "INSERT INTO arbitration_log "
        "(target_table,target_key,field,original_value,new_value,resolver,evidence,source,ts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (table, str(key), field, str(old), str(new), "entity_tier2", evidence, model, _now()))


# ── Orchestration ─────────────────────────────────────────────────────────────

def arbitrate(db_path: str, tier2: bool = False, model_id: str = SONNET_MODEL,
              profile: str = "", region: str = "us-east-1") -> dict:
    conn = sqlite3.connect(db_path)
    ensure_log(conn)

    stats = {
        "kind_normalized": resolve_kind_normalize(conn),
        "direction_conflicts_resolved": resolve_direction_by_dml(conn),
        "reference_kinds_corrected": resolve_kind_by_create(conn),
        "entity_aliases_merged": resolve_entity_alias_merge(conn),
    }
    if tier2:
        stats["tier2_entities"] = resolve_entities_tier2(conn, model_id, profile, region)

    # Residue: what Tier-1 could NOT resolve (feeds the future Sonnet arbiter).
    residue = {
        "unresolved_direction_conflicts": conn.execute(
            "SELECT COUNT(*) FROM code_edges WHERE direction_verdict='conflict'").fetchone()[0],
        "entities_still_unconfirmed": conn.execute(
            "SELECT COUNT(*) FROM code_entities WHERE anchor_verdict='unconfirmed'").fetchone()[0],
    }
    total_edges = conn.execute("SELECT COUNT(*) FROM code_edges").fetchone()[0]
    total_changes = sum(v for v in stats.values() if isinstance(v, int))
    stats["overturn_rate_vs_edges"] = round(total_changes / total_edges, 4) if total_edges else None
    stats["residue"] = residue
    conn.close()

    print("[arbitrate] Tier-1 (deterministic) results:")
    for k, v in stats.items():
        print(f"[arbitrate]   {k}: {v}")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Graph arbitration (Tier-1 deterministic + optional Tier-2 Sonnet)")
    ap.add_argument("db_path")
    ap.add_argument("--tier2", action="store_true", help="Run the Sonnet arbiter over the residue (needs creds)")
    ap.add_argument("--model-id", default=SONNET_MODEL)
    ap.add_argument("--profile", default="")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()
    arbitrate(args.db_path, tier2=args.tier2, model_id=args.model_id,
              profile=args.profile, region=args.region)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

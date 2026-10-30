"""Ontology classification — LLM residue tier (Phase-1 step T-4).

Story: knowledge-layer/ontology-classification, task T-4. The deterministic tier
(ontology_comparator.py) settled the clearly-groupable pairs and DELIBERATELY left a residue:
pairs that are semantically similar (references score >= SIM_STRONG) but share exactly ONE
code symbol — the ms=1 bucket (~17k pairs), the false-positive-prone class where a lone
incidental token is too weak to auto-assert `confident`. This pass asks an LLM to adjudicate
each residue pair: are these two concepts one story, or genuinely separate?

Mirrors symbol_backmap_residue.py's safety contract exactly:
  - Model: Claude Haiku 4.5 (the residue-tier precedent). Bounded same/not-same judgment, not
    synthesis — Haiku is the right tool (materially cheaper than Sonnet, ~3x, for this
    bounded judgment).
  - temp 0.0; thread-pooled network calls; DB writes serialized in the main thread and
    committed incrementally (resumable/idempotent).
  - ANTI-OVERFIT (System L3): three-state verdict only — SAME (confident group) / UNSURE
    (kept+flagged) / SEPARATE (rejected). The model may ONLY emit one of these; it never
    invents a grouping rationale beyond the rubric. UNSURE and SEPARATE are both first-class:
    a rejected pair is NOT dropped, it simply gets no grouping edge (the concepts persist).
  - Resumable: skips pairs that already have an llm_residue edge in ontology_edges.
  - Cost recorded (in/out tokens) so spend is ground-truth, never guessed.

Writes reasoning-family grouping edges (verdict SAME->confident, UNSURE->unsure_kept) via the
step-1 substrate write_edge; SEPARATE writes NO edge (kept, not grouped). All into the WORKING
COPY only.

CLI:
    python ontology_residue.py <wip_db> <stills_dir> --limit 50 --profile P    # smoke test (tiny)
    python ontology_residue.py <wip_db> <stills_dir> --profile P                # full residue
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import bedrock_client
import json_extract
import ontology_classify as oc
import ontology_comparator as C

DEFAULT_MODEL = "anthropic.claude-haiku-4-5"
PRICE_IN, PRICE_OUT = 1.0, 5.0  # $/M tokens (Haiku 4.5 US) — same source as symbol_backmap_residue
CRIT_LLM_RESIDUE = "llm_residue_pair_judgment"
MAX_JSON_RECASTS = 2  # on invalid JSON, re-ask with the parse error fed back (self-correct at source)

_SYSTEM = (
    "You decide whether two software-specification concepts belong to the SAME story "
    "(one cohesive capability) or are SEPARATE. A story = one capability: cohesive purpose, "
    "independent value, shared invariants. CRUD on one resource is one story. Split only on "
    "divergent WHY + independent value, never on surface wording. You are given two concepts "
    "that ARE textually similar and DO share one code symbol — judge whether that reflects one "
    "capability or an incidental overlap. Emit STRICT JSON: "
    '{"verdict":"SAME"|"UNSURE"|"SEPARATE","confidence":0.0-1.0,"reason":"<=200 chars"}. '
    "Use SAME only with clear shared forcing-function; SEPARATE when the shared symbol is "
    "incidental and the WHY diverges; UNSURE when genuinely ambiguous. Never invent facts."
)

VERDICT_MAP = {
    "SAME": oc.VERDICT_CONFIDENT,
    "UNSURE": oc.VERDICT_UNSURE_KEPT,
    "SEPARATE": None,  # no edge written; concepts kept, not grouped (L3c — kept, not dropped)
}


import re as _re

def _tolerant_parse(text: str):
    """Recover {verdict, confidence, reason} when the model emits JSON whose `reason` value
    contains unescaped inner quotes (breaks strict json.loads). verdict and confidence are
    clean structured tokens — pull them by regex; reason is best-effort. This is the
    deterministic "instructor grabs the wheel" intermediary of the bounded-emit protocol
    (AUTHORING_CONTRACT.md): it coerces ONLY value actually present, and returns None when the
    verdict cannot be found — an honest flagged failure (L3c/L4), NEVER a fabricated verdict."""
    vm = _re.search(r'"verdict"\s*:\s*"(SAME|UNSURE|SEPARATE)"', text, _re.IGNORECASE)
    if not vm:
        return None
    cm = _re.search(r'"confidence"\s*:\s*([0-9.]+)', text)
    rm = _re.search(r'"reason"\s*:\s*"(.*)', text, _re.DOTALL)
    reason = ""
    if rm:
        # take up to the last closing brace of the object; strip trailing fence/prose
        reason = rm.group(1).split('"\n}')[0].split('"}')[0][:300]
    return {"verdict": vm.group(1).upper(),
            "confidence": float(cm.group(1)) if cm else None,
            "reason": reason,
            "_tolerant": True}


def load_residue_pairs(conn, stills_dir: Path, limit: int = 0):
    """Re-derive the deterministic tier's residue: sim>=SIM_STRONG, exactly 1 shared symbol.

    This is the exact complement of Half A's confident set (which required >=2). Returns list
    of (src_uc, dst_uc, score, shared_symbol)."""
    b2u = C.load_block_to_uc(conn)
    uc_syms = C.load_uc_symbols_via_crosswalk(conn, stills_dir)
    out = []
    seen = set()
    for src_b, dst_b, score in C.load_similarity_pairs(stills_dir, C.SIM_STRONG):
        su, du = b2u.get(src_b), b2u.get(dst_b)
        if not su or not du or su == du:
            continue
        shared = uc_syms.get(su, set()) & uc_syms.get(du, set())
        if len(shared) == 1:  # THE residue bucket: exactly one shared symbol
            key = tuple(sorted((su, du)))
            if key in seen:
                continue
            seen.add(key)
            out.append((su, du, score, next(iter(shared))))
            if limit and len(out) >= limit:
                break
    return out


def _existing_residue_edges(conn) -> set:
    """Pairs already adjudicated (resumability): edge_id is hash(src,dst,family,criterion)."""
    done = set()
    for src, dst in conn.execute(
        "SELECT src, dst FROM ontology_edges WHERE criterion=?", (CRIT_LLM_RESIDUE,)
    ):
        done.add(tuple(sorted((src, dst))))
    # Also treat SEPARATE-logged pairs as done (they wrote no edge but were judged)
    for eid_pair in conn.execute(
        "SELECT DISTINCT edge_id FROM ontology_log WHERE evidence LIKE '%\"criterion\": \"llm_residue%'"
    ):
        pass  # edge_id space differs; rely on the edge presence above + separate_log table below
    return done


def ensure_separate_log(conn):
    """SEPARATE verdicts write no grouping edge, but we MUST record that the pair was judged
    (so a resume does not re-spend on it, and so the decision is auditable)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ontology_residue_separate (
            src TEXT, dst TEXT, confidence REAL, reason TEXT, shared_symbol TEXT, ts TEXT,
            PRIMARY KEY (src, dst)
        );
        """
    )
    conn.commit()


def _judged_separate(conn) -> set:
    return {tuple(sorted((s, d))) for s, d in
            conn.execute("SELECT src, dst FROM ontology_residue_separate")}


def build(db_path: str, stills_dir: str, model_id: str, profile: str, region: str,
          concurrency: int = 8, limit: int = 0) -> dict:
    conn = sqlite3.connect(db_path)
    oc.ensure_tables(conn)
    ensure_separate_log(conn)
    stills = Path(stills_dir)

    print("[residue] deriving residue pairs (sim>=%.2f, exactly 1 shared symbol)..." % C.SIM_STRONG, flush=True)
    pairs = load_residue_pairs(conn, stills, limit=0)  # derive all; filter done below
    done = _existing_residue_edges(conn) | _judged_separate(conn)
    pairs = [p for p in pairs if tuple(sorted((p[0], p[1]))) not in done]
    if limit:
        pairs = pairs[:limit]
    if not pairs:
        print("[residue] nothing to do (all residue pairs already judged)", flush=True)
        return {"judged": 0}
    print(f"[residue] {len(pairs)} pairs to judge (concurrency={concurrency}, model={model_id})", flush=True)

    # concept bodies for the prompt
    body = {}
    title = {}
    for uc, t, b in conn.execute("SELECT concept_id, title, body_raw FROM unified_concepts"):
        body[uc] = b or ""
        title[uc] = t or ""

    llm = bedrock_client.create_llm_client(profile, region)
    counts = {"judged": 0, "same": 0, "unsure": 0, "separate": 0, "errors": 0,
              "in_tok": 0, "out_tok": 0}
    err_samples = []

    def _judge(pair):
        su, du, score, sym = pair
        user = (
            f"Concept A: {title.get(su,'')}\n{body.get(su,'')[:1800]}\n\n"
            f"Concept B: {title.get(du,'')}\n{body.get(du,'')[:1800]}\n\n"
            f"They share code symbol: {sym}. Textual similarity: {score:.3f}.\n"
            "Same story, unsure, or separate?"
        )
        # generate -> extract -> validate -> RECAST-on-failure (feed the parse error back so
        # the model self-corrects) -> tolerant salvage only as last-resort defense-in-depth.
        # I asked for JSON, so validating it is MY responsibility; a malformed emit is fixed
        # AT GENERATION (the recast), not merely salvaged after.
        in_tok = out_tok = 0
        cur_user = user
        last_stripped = last_text = ""
        for attempt in range(MAX_JSON_RECASTS + 1):
            try:
                res = bedrock_client.call_llm(llm, model_id, _SYSTEM, cur_user,
                                                 max_tokens=500, temperature=0.0)
            except Exception as e:
                return {"pair": pair, "error": f"{type(e).__name__}: {e}",
                        "in": in_tok, "out": out_tok}
            in_tok += res.input_tokens; out_tok += res.output_tokens
            last_text = res.output_text
            last_stripped = json_extract.strip_json(res.output_text, "object")
            try:
                parsed = json.loads(last_stripped)
                return {"pair": pair, "parsed": parsed, "in": in_tok, "out": out_tok,
                        "recasts": attempt}
            except Exception as e:
                # feed the exact failure back and ask for a clean re-emit (bounded)
                cur_user = (
                    f"{user}\n\nYour previous reply was NOT valid JSON ({type(e).__name__}: {e}). "
                    "Return ONLY a single valid JSON object, no code fence, no prose after it. "
                    "CRITICAL: escape every double-quote INSIDE any string value as \\\" "
                    "(e.g. write StateNumericCodes(\\\"OR\\\") )."
                )
        # recasts exhausted -> last-resort salvage (verdict/confidence are clean tokens)
        parsed = _tolerant_parse(last_text)
        if parsed is None:
            return {"pair": pair, "error": "unparseable: JSONDecodeError (after recasts)",
                    "snippet": (last_text or "")[:600], "stripped": last_stripped[:400],
                    "in": in_tok, "out": out_tok}
        return {"pair": pair, "parsed": parsed, "in": in_tok, "out": out_tok, "salvaged": True}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(_judge, p): p for p in pairs}
        total = len(pairs)
        processed = 0
        HEARTBEAT_EVERY = 100  # emit liveness every N completed pairs — observability is not optional
        for fut in as_completed(futs):
            r = fut.result()
            su, du, score, sym = r["pair"]
            processed += 1
            if processed % HEARTBEAT_EVERY == 0 or processed == total:
                el = time.time() - t0
                rate = processed / el if el > 0 else 0.0
                eta_min = (total - processed) / rate / 60 if rate > 0 else 0.0
                spent = counts["in_tok"] / 1e6 * PRICE_IN + counts["out_tok"] / 1e6 * PRICE_OUT
                print(f"[residue] {processed}/{total} judged "
                      f"(same={counts['same']} unsure={counts['unsure']} separate={counts['separate']} "
                      f"err={counts['errors']}) rate={rate:.1f}/s eta={eta_min:.1f}min ${spent:.2f}",
                      flush=True)
            if r.get("error"):
                counts["errors"] += 1
                counts["in_tok"] += r.get("in", 0); counts["out_tok"] += r.get("out", 0)
                if len(err_samples) < 20:
                    err_samples.append({"pair": [su, du], "detail": r["error"],
                                        "snippet": r.get("snippet", ""), "stripped": r.get("stripped", ""),
                                        "stop": r.get("stop", ""), "out_tok": r.get("out", 0)})
                continue  # NON-DESTRUCTIVE: leave unjudged so a rerun retries
            counts["in_tok"] += r["in"]; counts["out_tok"] += r["out"]
            p = r["parsed"]
            verdict_raw = (p.get("verdict") or "").strip().upper()
            conf = p.get("confidence")
            reason = (p.get("reason") or "")[:200]
            mapped = VERDICT_MAP.get(verdict_raw)
            counts["judged"] += 1
            if verdict_raw == "SAME":
                counts["same"] += 1
            elif verdict_raw == "UNSURE":
                counts["unsure"] += 1
            elif verdict_raw == "SEPARATE":
                counts["separate"] += 1
            else:
                # unrecognized verdict -> treat as UNSURE (kept+flagged), never dropped
                mapped = oc.VERDICT_UNSURE_KEPT
                counts["unsure"] += 1
            if mapped is None:
                # SEPARATE: record the judgment, write NO grouping edge (kept, not grouped)
                conn.execute(
                    "INSERT OR REPLACE INTO ontology_residue_separate (src,dst,confidence,reason,shared_symbol,ts) "
                    "VALUES (?,?,?,?,?,?)",
                    (su, du, conf, reason, sym, oc._now()))
            else:
                oc.write_edge(
                    conn, src=su, dst=du, family=oc.FAMILY_REASONING, relation=oc.REL_CONCEPT_STORY,
                    criterion=CRIT_LLM_RESIDUE, classifier="llm_residue", verdict=mapped,
                    confidence=conf,
                    evidence={"llm_verdict": verdict_raw, "reason": reason, "shared_symbol": sym,
                              "similarity_score": round(score, 4), "model": model_id})
            conn.commit()  # incremental: resumable after any interruption
    dt = time.time() - t0
    usd = counts["in_tok"] / 1e6 * PRICE_IN + counts["out_tok"] / 1e6 * PRICE_OUT
    counts["cost_usd_est"] = round(usd, 2)
    counts["elapsed_s"] = round(dt, 1)
    if err_samples:
        counts["error_samples"] = err_samples
    print(json.dumps(counts, indent=2), flush=True)
    conn.close()
    return counts


def main():
    ap = argparse.ArgumentParser(description="Ontology LLM residue tier (T-4) — pair same/separate judgment")
    ap.add_argument("db_path", help="shared_vectors WORKING COPY")
    ap.add_argument("stills_dir")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--profile", default="")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="cap pairs (smoke test); 0 = full residue")
    args = ap.parse_args()
    build(args.db_path, args.stills_dir, args.model, args.profile, args.region,
          concurrency=args.concurrency, limit=args.limit)


if __name__ == "__main__":
    main()

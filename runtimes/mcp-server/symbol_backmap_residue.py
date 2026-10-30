#!/usr/bin/env python3
"""
symbol_backmap_residue.py — LLM residue pass to complete the symbol back-mapping
ledger to (verifiable) 100% fidelity.

The deterministic backmap (symbol_backmap.py) attests a symbol only when it matches
a source token verbatim/normalized. The remainder that HAS a cited source but no
deterministic match (transform_type='coined_unverified', observed_source set) is a
SEMANTIC-alignment problem: the synthesized/paraphrased identifier (e.g.
`expiry_date`) may denote a real source identifier (`expiresOn`) that a string match
cannot see. This pass asks Haiku to align each coined symbol to a VERBATIM observed
identifier from its cited file — or NULL.

ANTI-OVERFIT CONTRACT (same as the rest of the subsystem):
  - The model may ONLY map to an identifier that appears in the provided observed
    list. Any mapping not in the list is rejected (treated as null). It NEVER invents.
  - No positive match -> transform_type='coined_confirmed' (kept, flagged; genuinely
    not attested to source), never dropped.
  - A positive match -> observed_value set, transform_type='llm_aligned'.

OBSERVABILITY / SAFETY (terminal-hygiene + design tenets):
  - Concurrent Haiku calls (network-bound) via a thread pool; DB writes serialized in
    the main thread, COMMITTED PER FILE (lock released, progress persisted, resumable).
  - Resumable/idempotent: only processes rows still coined_unverified with a source.
  - Incremental progress (N/M files, running counts, ETA, running $) with flush.

CLI:
  python symbol_backmap_residue.py <stills_dir> <pages_dir> <db_path> \
      [--limit N] [--concurrency 8] [--profile P] [--model anthropic.claude-haiku-4-5]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import bedrock_client
import symbol_backmap as sbm  # reuse page indexing + tokenizer

DEFAULT_MODEL = "anthropic.claude-haiku-4-5"
PRICE_IN, PRICE_OUT = 1.0, 5.0  # $/M tokens (Haiku 4.5 US)
MAX_OBSERVED = 180              # cap candidate identifiers per file (prompt size)
MAX_SYMS = 60                   # cap coined symbols per LLM call (chunk large files)

_SYSTEM = (
    "You align paraphrased/synthesized identifiers to the VERBATIM source identifiers "
    "they denote. You may ONLY map a synthesized name to an identifier that appears in "
    "the provided OBSERVED list. If a synthesized name does not clearly denote one of "
    "the observed identifiers, output null — NEVER invent or guess. This is anti-overfit: "
    "when unsure, null. Output ONLY a JSON array."
)


def _strip_json(text: str) -> str:
    """Extract the first complete, balanced top-level JSON ARRAY from a response.

    Delegates to the shared json_extract.strip_json (single source of truth) — the
    hardened balanced-scan that ignores fences and trailing prose (e.g. the model
    emitting a valid array then continuing with 'Wait, let me reconsider...').
    """
    from json_extract import strip_json
    return strip_json(text, kind="array")


# Salvage matcher: a single well-formed alignment object. Used ONLY when whole-array
# json.loads fails, to recover the valid entries instead of discarding the file.
# Bedrock at temperature=0 is NOT bitwise-deterministic (server-side batching / MoE
# routing), so a given prompt can occasionally emit one malformed object mid-array.
# Salvaging is still anti-overfit: recovered mappings are re-validated against the
# observed set by the caller exactly like fully-parsed ones.
_OBJ = re.compile(
    r'\{\s*"index"\s*:\s*(\d+)\s*,\s*"observed"\s*:\s*(null|"(?:[^"\\]|\\.)*")\s*\}'
)


def _salvage_objects(text: str):
    """Recover every well-formed {"index":N,"observed":...} object from a partially
    malformed array. Returns a list of dicts (same shape as json.loads would give),
    or None if nothing salvageable."""
    out = []
    for m in _OBJ.finditer(text):
        idx = int(m.group(1))
        raw = m.group(2)
        try:
            observed = json.loads(raw)  # handles null and quoted/escaped strings
        except Exception:
            continue
        out.append({"index": idx, "observed": observed})
    return out or None


def _observed_identifiers(pages_index, cited_source: str, cap: int = MAX_OBSERVED):
    """Distinct original identifier tokens from a cited file (best-effort)."""
    key = sbm._resolve(pages_index, cited_source)
    if not key:
        return []
    page_file, s, e = pages_index["locator"][key]
    seen = {}
    for _lineno, text in sbm._load_segment(page_file, s, e):
        for m in sbm._TOK.finditer(text):
            tok = m.group(0)
            if len(tok) >= 3 and tok not in seen:
                seen[tok] = None
    toks = list(seen.keys())
    # Prefer longer / more distinctive tokens if we must cap.
    toks.sort(key=lambda t: (-len(t), t))
    return toks[:cap]


def build_residue(stills_dir: str, pages_dir: str, db_path: str, model_id: str,
                  profile: str, region: str, concurrency: int = 8, limit: int = 0) -> dict:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT observed_source, new_value FROM symbol_backmap "
        "WHERE observed_value IS NULL AND observed_source IS NOT NULL "
        "AND transform_type='coined_unverified'"
    ).fetchall()
    if not rows:
        print("[residue] nothing to do (no coined-with-source residue)", flush=True)
        return {"files": 0}

    by_file = defaultdict(set)
    for src, sym in rows:
        by_file[src].add(sym)
    files = sorted(by_file.keys())
    if limit:
        files = files[:limit]
    total_files = len(files)
    print(f"[residue] indexing pages...", flush=True)
    pages_index = sbm.index_pages(pages_dir)
    print(f"[residue] {len(rows)} coined-with-source rows across {len(by_file)} files; "
          f"processing {total_files} (concurrency={concurrency})", flush=True)

    llm = bedrock_client.create_llm_client(profile, region)
    counts = {"files": 0, "salvaged_files": 0, "aligned": 0, "confirmed_coined": 0,
              "no_candidates": 0, "errors": 0, "err_call": 0, "err_unparseable": 0,
              "in_tok": 0, "out_tok": 0}
    # Bounded per-reason error samples so a failure is diagnosable from ground truth
    # (recorded fact) rather than reconstructed from a guess later.
    err_samples = []

    def _call(cited_source, syms):
        observed = _observed_identifiers(pages_index, cited_source)
        if not observed:
            return {"_no_candidates": True}
        syms = sorted(syms)[:MAX_SYMS]
        obs_txt = ", ".join(observed)
        items = "\n".join(f"  {i}. {s}" for i, s in enumerate(syms))
        user = (
            f"File: {cited_source}\n\n"
            f"OBSERVED identifiers (map ONLY to one of these, verbatim):\n{obs_txt}\n\n"
            f"Synthesized names to align:\n{items}\n\n"
            "For each index emit {\"index\":N,\"observed\":\"<verbatim from OBSERVED>\"|null}. "
            "Output ONLY a JSON array."
        )
        # A failure here is recorded as one of exactly two kinds, so the report states
        # what actually failed (a throttle shows up as a call_error with its message):
        #   1. call_error  — the SDK/network raised (captured type+message)
        #   2. unparseable — a response came back but json.loads could not read it
        try:
            res = bedrock_client.call_llm(llm, model_id, _SYSTEM, user,
                                             max_tokens=2048, temperature=0.0)
        except Exception as e:  # call_error: real exception, surface it verbatim
            return {"error_kind": "call_error",
                    "error_detail": f"{type(e).__name__}: {e}"}
        extracted = _strip_json(res.output_text)
        salvaged = False
        try:
            parsed = json.loads(extracted)
        except Exception as e:
            # Whole-array parse failed (occasional malformed object under the model's
            # non-deterministic temp=0). Salvage the well-formed objects rather than
            # discard the whole file — anti-overfit re-validation still applies below.
            parsed = _salvage_objects(extracted)
            if parsed is None:
                snippet = (res.output_text or "")[:200].replace("\n", " ")
                return {"error_kind": "unparseable",
                        "error_detail": f"{type(e).__name__}: {e}",
                        "snippet": snippet, "in": res.input_tokens, "out": res.output_tokens}
            salvaged = True
        return {"syms": syms, "observed_set": set(observed), "parsed": parsed,
                "salvaged": salvaged, "in": res.input_tokens, "out": res.output_tokens}

    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(_call, src, by_file[src]): src for src in files}
        for fut in as_completed(futs):
            src = futs[fut]
            done += 1
            try:
                r = fut.result()
            except Exception as e:
                # future itself raised (should not happen: _call catches its own) —
                # record it as a call_error rather than an opaque blank.
                r = {"error_kind": "call_error",
                     "error_detail": f"future: {type(e).__name__}: {e}"}
            if r is None:
                counts["errors"] += 1
            elif r.get("error_kind"):
                counts["errors"] += 1
                kind = r["error_kind"]
                counts["err_call" if kind == "call_error" else "err_unparseable"] += 1
                # accumulate tokens spent even on unparseable responses (cost is real)
                counts["in_tok"] += r.get("in", 0); counts["out_tok"] += r.get("out", 0)
                if len(err_samples) < 20:
                    smp = {"file": src, "kind": kind, "detail": r.get("error_detail", "")}
                    if r.get("snippet"):
                        smp["snippet"] = r["snippet"]
                    err_samples.append(smp)
                # NON-DESTRUCTIVE: leave the file's rows as coined_unverified so the
                # next run retries them. Do not confirm-coin on an error.
            elif r.get("_no_candidates"):
                counts["no_candidates"] += 1
                # confirm-coined for all this file's rows (no source candidates to match)
                conn.execute(
                    "UPDATE symbol_backmap SET transform_type='coined_confirmed', resolver='llm' "
                    "WHERE observed_source=? AND observed_value IS NULL AND transform_type='coined_unverified'",
                    (src,))
            elif "parsed" in r:
                # parsed may legitimately be an empty list (model mapped nothing);
                # that is a valid, successful call — not an error.
                counts["files"] += 1
                if r.get("salvaged"):
                    counts["salvaged_files"] += 1
                counts["in_tok"] += r["in"]; counts["out_tok"] += r["out"]
                syms, obs_set, parsed = r["syms"], r["observed_set"], r["parsed"]
                mapped = {}
                for j in parsed:
                    if not isinstance(j, dict) or "index" not in j:
                        continue
                    idx = j["index"]
                    if not (0 <= idx < len(syms)):
                        continue
                    ov = j.get("observed")
                    # ANTI-OVERFIT: accept only if the mapping is a verbatim observed token
                    if isinstance(ov, str) and ov in obs_set:
                        mapped[syms[idx]] = ov
                for sym in syms:
                    if sym in mapped:
                        conn.execute(
                            "UPDATE symbol_backmap SET observed_value=?, transform_type='llm_aligned', "
                            "confidence=0.7, resolver='llm' WHERE observed_source=? AND new_value=? "
                            "AND observed_value IS NULL AND transform_type='coined_unverified'",
                            (mapped[sym], src, sym))
                        counts["aligned"] += 1
                    else:
                        conn.execute(
                            "UPDATE symbol_backmap SET transform_type='coined_confirmed', resolver='llm' "
                            "WHERE observed_source=? AND new_value=? AND observed_value IS NULL "
                            "AND transform_type='coined_unverified'",
                            (src, sym))
                        counts["confirmed_coined"] += 1
            else:
                # unreachable by construction (every branch above is exhaustive);
                # if it ever fires, record it distinctly so it can't masquerade.
                counts["errors"] += 1
                if len(err_samples) < 20:
                    err_samples.append({"file": src, "kind": "unclassified",
                                        "detail": f"keys={sorted(r.keys())}"})
            conn.commit()  # per-file: release lock + persist (resumable)
            if done % 50 == 0 or done == total_files:
                rate = done / max(1e-6, time.time() - t0)
                eta = (total_files - done) / rate if rate else 0
                cost = counts["in_tok"] / 1e6 * PRICE_IN + counts["out_tok"] / 1e6 * PRICE_OUT
                print(f"[residue] {done}/{total_files} files "
                      f"(aligned={counts['aligned']} coined={counts['confirmed_coined']} "
                      f"no_cand={counts['no_candidates']} "
                      f"err={counts['errors']} [call={counts['err_call']} "
                      f"unparse={counts['err_unparseable']}]) "
                      f"~{eta:.0f}s left ${cost:.2f}", flush=True)
    conn.commit()
    conn.close()
    counts["cost_usd"] = round(counts["in_tok"] / 1e6 * PRICE_IN + counts["out_tok"] / 1e6 * PRICE_OUT, 2)
    print(f"[residue] DONE: {counts}", flush=True)
    if err_samples:
        # Recorded fact, not a guess: exactly what failed and why. call_error =
        # SDK/network raised; unparseable = a response came back but was not JSON.
        # Concurrency is bounded by the caller, so these are not expected to be throttles.
        print(f"[residue] ERROR SAMPLES ({len(err_samples)} shown):", flush=True)
        for s in err_samples:
            line = f"  [{s['kind']}] {s['file']} :: {s['detail']}"
            if s.get("snippet"):
                line += f" :: snippet={s['snippet']!r}"
            print(line, flush=True)
    counts["err_samples"] = err_samples
    return counts


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LLM residue pass for the symbol back-map ledger.")
    ap.add_argument("stills_dir")  # unused directly; kept for CLI symmetry
    ap.add_argument("pages_dir")
    ap.add_argument("db_path")
    ap.add_argument("--limit", type=int, default=0, help="0 = all files")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--profile", default="")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()
    build_residue(args.stills_dir, args.pages_dir, args.db_path, args.model,
                  args.profile, args.region, concurrency=args.concurrency, limit=args.limit)

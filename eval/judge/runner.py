# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Repeated, cached, schema-validated judging with a majority vote.

What this fixes, relative to the single free-text judge call it replaces:

* one call per batch, N passes, **majority vote per finding** -- a verdict only
  counts when more than half the passes agree, so the +-3-5 item wobble in
  full-match recall becomes visible instead of being baked into the headline;
* **structured output** via Converse tool use, validated against a JSON Schema,
  retried once, then raised -- never silently mis-parsed;
* **abstention**: the judge may answer ``uncertain``; those findings are bucketed,
  not forced into a verdict;
* **bounded evidence**: the lines the batch cites plus the closest key locations,
  with a per-batch manifest of exactly what was shown;
* **content-addressed cache**: re-scoring is free and byte-identical.

Determinism knobs: temperature 0, key items sorted by id, findings in stable
order, fixed section order in the prompt. The only thing that differs between
passes is the "Evaluation pass k of N" line, which is what makes each pass an
independent sample while keeping it individually reproducible. Measured spread
across passes is therefore a *lower bound* on judge noise, not an upper one.

Cache keying: the cache key covers the prompt with the pass header reduced to
"Evaluation pass k." -- the pass *index* k is kept, the pass *count* N is not.
So pass 1 of a 1-pass run and pass 1 of a 3-pass run share one cache entry, and
raising ``--judge-runs`` from 1 to 3 only pays for passes 2 and 3. Entries written
before this rule (keyed on the full "pass k of N" prompt) are still found: on a
miss the runner also tries the legacy key for every N' >= k up to
``LEGACY_MAX_RUNS`` and migrates a hit to the new key. The cost plan checks the
cache the same way, so ``--max-judge-cost-usd`` and ``--dry-run-judge`` count
cache hits as $0.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from . import evidence as ev
from . import schema as sch
from .cache import JudgeCache
from .client import JudgeError, JudgeProtocolError, converse_tool

#: rough chars-per-token for Claude on English + code; used only for the cost plan
CHARS_PER_TOKEN = 3.6
#: observed output size of one judgment, in tokens; used only for the cost plan
OUTPUT_TOKENS_PER_FINDING = 95

JUDGE_SYSTEM = (
    "You are a strict evaluator of software-specification extraction. You compare findings "
    "extracted by a tool against an answer key and against numbered source code excerpts. "
    "You never guess: when the excerpt you were given does not settle a question, you say so. "
    "You answer only by calling the record_judgments tool."
)

_PROMPT = """Evaluation pass {run} of {runs}. Judge independently; do not try to be consistent \
with any other pass.

# 1. Answer key items (id: statement)
{key}

# 2. Source code excerpts (line-numbered, 1-based, inclusive)
These are only the lines relevant to the findings below plus the key items closest to them.
They are NOT the whole codebase. If a claim needs code that is not shown, that is what the
"uncertain" verdict is for.
{source}

# 3. Extracted findings to judge (id: text)
{findings}

# 4. What to decide, for EVERY finding id in section 3 and no others
- "matches": the key items the finding substantially expresses.
    degree "full"    - the finding states the key item's essential claim. Wording may differ;
                       exact message text or other incidental detail is not required.
    degree "partial" - it touches the item but misses, blurs or contradicts its essential claim.
  A finding may match several key items. Do not match on shared topic alone.
- "verdict": exactly one of
    "matched"       - at least one full or partial match above;
    "correct_extra" - no key match, but the claim is true of the source shown (extra detail);
    "unsupported"   - no key match, and the claim is the kind that source could never confirm
                      (speculation, recommendation, rationale, generic boilerplate);
    "incorrect"     - the claim contradicts source that IS shown to you. Use this for a matched
                      finding that also asserts something false; keep its matches.
    "uncertain"     - you cannot tell, because the deciding lines are not in section 2, or the
                      finding is too vague to evaluate. Prefer this over guessing.
- "confidence": "high", "medium" or "low".
- "note": at most 25 words, required for incorrect / unsupported / uncertain.
{corrective}"""

_CORRECTIVE_SCHEMA = """
# CORRECTION
Your previous tool call did not satisfy the schema: {error}
Call record_judgments again. Use only the allowed verdicts and degrees, include no extra
properties, and give exactly one judgment per finding id listed in section 3.
"""

_CORRECTIVE_MISSING = """
# CORRECTION
Your previous tool call omitted these finding ids: {missing}
Call record_judgments again with a judgment for every finding id in section 3, including those.
"""


#: header of every judge prompt; group 1 is the pass index k
_PASS_HEADER_RE = re.compile(r"\AEvaluation pass (\d+) of \d+\.")
#: how far the legacy-key fallback looks for entries written under "pass k of N"
LEGACY_MAX_RUNS = 9
#: recorded in every new cache key so the keying rule is explicit and versioned
PASS_KEYING = "pass-index-only/v1"


def cache_prompt(prompt: str) -> str:
    """The prompt as the cache sees it: "pass k of N." becomes "pass k."."""
    return _PASS_HEADER_RE.sub(r"Evaluation pass \1.", prompt, count=1)


def _key_payload(prompt: str, *, model, max_tokens, temperature, legacy: bool = False) -> dict:
    payload = {"prompt": prompt if legacy else cache_prompt(prompt), "system": JUDGE_SYSTEM,
               "model": model or _model_id(), "tool": sch.TOOL_NAME,
               "schema": sch.schema_hash(), "schema_version": sch.SCHEMA_VERSION,
               "max_tokens": max_tokens, "temperature": temperature}
    if not legacy:
        payload["pass_keying"] = PASS_KEYING
    return payload


def cache_keys(prompt: str, *, model=None, max_tokens: int = 12000,
               temperature: float = 0.0) -> list[str]:
    """Cache keys to try for one prompt, best first: the pass-index key, then the legacy
    full-prompt keys for every pass count N' >= k (so older caches stay usable)."""
    keys = [JudgeCache.key(_key_payload(prompt, model=model, max_tokens=max_tokens,
                                        temperature=temperature))]
    m = _PASS_HEADER_RE.match(prompt)
    if m:
        k = int(m.group(1))
        for n in range(k, max(k, LEGACY_MAX_RUNS) + 1):
            legacy_prompt = _PASS_HEADER_RE.sub(f"Evaluation pass {k} of {n}.", prompt, count=1)
            keys.append(JudgeCache.key(_key_payload(legacy_prompt, model=model,
                                                    max_tokens=max_tokens,
                                                    temperature=temperature, legacy=True)))
    return keys


class JudgeBudgetExceeded(JudgeError):
    """Actual judge spend passed --max-judge-cost-usd; the run stops rather than overspending."""


# --------------------------------------------------------------------- formatting
def _nat_key(fid: str):
    """Stable ordering for finding ids like F9 < F10 (not F10 < F9)."""
    head = fid.rstrip("0123456789")
    tail = fid[len(head):]
    return (head, int(tail) if tail else -1, fid)


def _one_line(s: str) -> str:
    return " ".join(str(s).split())


def format_key(key_items) -> str:
    return "\n".join(f"{k['id']}: {_one_line(k['statement'])}"
                     for k in sorted(key_items, key=lambda k: str(k["id"])))


def format_findings(batch, max_chars_per_finding: int) -> str:
    out = []
    for f in batch:
        text = f["text"]
        limit = max_chars_per_finding * (3 if f.get("merged") or f.get("kind") == "requirement" else 1)
        if len(text) > limit:
            text = text[:limit] + f"\n[... {len(f['text']) - limit} more chars not shown ...]"
        out.append(f"{f['id']}: {text}")
    return "\n\n".join(out)


# ----------------------------------------------------------------------- batching
def make_batches(findings, batch_size: int):
    ordered = sorted(findings, key=lambda f: _nat_key(str(f["id"])))
    return [ordered[i:i + batch_size] for i in range(0, len(ordered), batch_size)]


def build_batch_evidence(batch, key_items, *, files, counts, split, tokenize, mode: str,
                         max_evidence_chars: int, context: int, max_source_chars: int,
                         key_cits: dict, top_k: int, read_cache: dict):
    """Source excerpt + manifest for one batch."""
    if mode == "full":
        text, manifest = ev.full_dump(files, max_source_chars, split)
        manifest["mode"] = "full"
        manifest["key_items_shown"] = sorted(str(k["id"]) for k in key_items)
        return text, manifest

    requests = []
    for f in batch:
        for c in f.get("citations", []):
            rel = c.get("resolved")
            if rel:
                iv = ev.expand((c["start"], c["end"]), context, counts.get(rel, 0))
                if iv:
                    requests.append((0, rel, iv[0], iv[1]))
    chosen = ev.select_key_items(batch, key_items, tokenize, top_k=top_k)
    for kid in chosen:
        for kf, a, b in key_cits.get(kid, []):
            rel = kf if kf in files else _resolve_basename(kf, files)
            if rel:
                iv = ev.expand((a, b), context, counts.get(rel, 0))
                if iv:
                    requests.append((1, rel, iv[0], iv[1]))
    text, manifest = ev.render(files, counts, requests, max_chars=max_evidence_chars,
                               split=split, cache=read_cache)
    manifest["mode"] = "chunked"
    manifest["key_items_shown"] = chosen
    manifest["context_lines"] = context
    return text, manifest


def _resolve_basename(name: str, files) -> str | None:
    base = name.replace("\\", "/").split("/")[-1].lower()
    for rel in sorted(files):
        if rel.split("/")[-1].lower() == base:
            return rel
    return None


# ------------------------------------------------------------------------- voting
def consensus(per_run: list[dict], finding_ids, n_runs: int) -> dict:
    """Majority vote per finding, plus the agreement that produced it.

    A verdict needs strictly more than half the passes. Otherwise the finding is
    reported as ``uncertain`` with ``no_majority`` -- an honest "the judge did not
    settle this", rather than an arbitrary tie-break. A key match is kept only if
    a majority of passes made it; its degree is ``full`` only if a majority of
    the passes that matched it said ``full``.
    """
    need = n_runs / 2.0
    out = {}
    for fid in finding_ids:
        labels = [r.get(fid, {}).get("verdict", sch.UNJUDGED) for r in per_run]
        counts = Counter(labels)
        top_label, top_n = counts.most_common(1)[0]
        no_majority = top_n <= need
        verdict = sch.VERDICTS[-1] if no_majority else top_label  # "uncertain"

        key_votes: Counter = Counter()
        full_votes: Counter = Counter()
        for r in per_run:
            seen = set()
            for m in r.get(fid, {}).get("matches", []) or []:
                kid = m.get("key_id")
                if kid is None or kid in seen:
                    continue
                seen.add(kid)
                key_votes[kid] += 1
                if m.get("degree") == "full":
                    full_votes[kid] += 1
        matches = []
        for kid in sorted(key_votes):
            if key_votes[kid] > need:
                degree = "full" if full_votes[kid] * 2 > key_votes[kid] else "partial"
                matches.append({"key_id": kid, "degree": degree,
                                "votes": key_votes[kid], "full_votes": full_votes[kid]})

        # a finding whose majority matches survive is "matched" even if the
        # majority verdict label drifted, so recall and precision stay consistent
        if matches and verdict in (sch.UNJUDGED,):
            verdict = "matched"

        notes = [r.get(fid, {}).get("note", "") for r in per_run if r.get(fid, {}).get("note")]
        confs = [r.get(fid, {}).get("confidence") for r in per_run if r.get(fid, {}).get("confidence")]
        match_sets = [tuple(sorted({m.get("key_id") for m in (r.get(fid, {}).get("matches") or [])}))
                      for r in per_run]
        majority_set = tuple(sorted(m["key_id"] for m in matches))
        out[fid] = {
            "verdict": verdict,
            "matches": [{"key_id": m["key_id"], "degree": m["degree"]} for m in matches],
            "note": notes[0] if notes else "",
            "runs": n_runs,
            "verdict_by_run": labels,
            "verdict_agreement": round(top_n / n_runs, 3) if n_runs else None,
            "unanimous_verdict": top_n == n_runs,
            "no_majority": bool(no_majority),
            "match_agreement": round(sum(1 for s in match_sets if s == majority_set) / n_runs, 3)
            if n_runs else None,
            "match_votes": {m["key_id"]: m["votes"] for m in matches},
            "confidence": Counter(confs).most_common(1)[0][0] if confs else None,
        }
    return out


# -------------------------------------------------------------------------- driver
def judge_findings(findings, key_items, *, files, counts, split, tokenize, key_cits,
                   runs: int = 3, batch: int = 25, evidence_mode: str = "chunked",
                   max_evidence_chars: int = 40000, evidence_context: int = 8,
                   max_source_chars: int = 80000, evidence_key_items: int = 12,
                   max_finding_chars: int = 1500,
                   cache: JudgeCache | None = None, tag: str = "judge",
                   log_path: Path | None = None, model: str | None = None,
                   max_tokens: int = 12000, temperature: float = 0.0,
                   max_cost_usd: float | None = None, dry_run: bool = False,
                   prompt_dir: Path | None = None, progress=None) -> dict:
    """Judge every finding ``runs`` times and return runs + consensus + usage.

    Raises on an unusable judge response (bad schema twice, no tool call, empty
    response, truncated output, exhausted throttle retries) and on budget
    overrun. Nothing degrades quietly.
    """
    say = progress or (lambda *_a, **_k: None)
    cache = cache if cache is not None else JudgeCache(Path(__file__).parent / "cache")
    key_txt = format_key(key_items)
    valid_key_ids = {str(k["id"]) for k in key_items}

    batches = make_batches(findings, batch)
    read_cache: dict = {}
    bodies, manifests = [], []
    for bi, b in enumerate(batches):
        src_txt, manifest = build_batch_evidence(
            b, key_items, files=files, counts=counts, split=split, tokenize=tokenize,
            mode=evidence_mode, max_evidence_chars=max_evidence_chars,
            context=evidence_context, max_source_chars=max_source_chars,
            key_cits=key_cits, top_k=evidence_key_items, read_cache=read_cache)
        manifest["batch"] = bi
        manifest["finding_ids"] = [f["id"] for f in b]
        manifests.append(manifest)
        bodies.append({"key": key_txt, "source": src_txt,
                       "findings": format_findings(b, max_finding_chars)})

    # ---- cost plan, printed before anything is spent. Every (pass, batch) call is
    # looked up in the cache first; a hit costs $0 and is left out of est_cost_usd.
    import bedrock_util
    est_in_all = est_out_all = est_in = est_out = 0.0
    expected_hits = 0
    for run_idx in range(1, runs + 1):
        for b, body in zip(batches, bodies):
            prompt = _PROMPT.format(run=run_idx, runs=runs, corrective="", **body)
            t_in = len(prompt) / CHARS_PER_TOKEN
            t_out = OUTPUT_TOKENS_PER_FINDING * len(b)
            est_in_all += t_in
            est_out_all += t_out
            if cache.contains_any(cache_keys(prompt, model=model, max_tokens=max_tokens,
                                             temperature=temperature)):
                expected_hits += 1
            else:
                est_in += t_in
                est_out += t_out
    est_cost_all = bedrock_util.cost_usd(int(est_in_all), int(est_out_all))
    est_cost = bedrock_util.cost_usd(int(est_in), int(est_out))
    plan = {
        "findings": len(findings), "batches": len(batches), "runs": runs,
        "calls": len(batches) * runs,
        "expected_cache_hits": expected_hits,
        "expected_bedrock_calls": len(batches) * runs - expected_hits,
        "evidence_mode": evidence_mode,
        "evidence_chars": {"min": min((m["chars"] for m in manifests), default=0),
                           "max": max((m["chars"] for m in manifests), default=0),
                           "total_per_run": sum(m["chars"] for m in manifests)},
        "evidence_truncated_batches": sum(1 for m in manifests if m.get("truncated")),
        "est_input_tokens": int(est_in), "est_output_tokens": int(est_out),
        # what this run is expected to SPEND: cache hits excluded
        "est_cost_usd": round(est_cost, 4),
        # what it would cost with a cold cache
        "est_cost_usd_uncached": round(est_cost_all, 4),
        "est_input_tokens_uncached": int(est_in_all),
        "cache_keying": PASS_KEYING,
        "model": model or bedrock_util.MODEL_ID,
        "region": bedrock_util.region_in_use(),
        "schema_version": sch.SCHEMA_VERSION, "schema_hash": sch.schema_hash(),
        "temperature": temperature, "cache_enabled": cache.enabled,
    }
    say(f"judge plan: {len(findings)} findings -> {len(batches)} batches x {runs} pass(es) "
        f"= {plan['calls']} calls; evidence {evidence_mode} "
        f"({plan['evidence_chars']['max']} chars max/batch); "
        f"{expected_hits} expected from cache; "
        f"est. {int(est_in):,} in / {int(est_out):,} out tokens "
        f"~= ${est_cost:.2f} at list price (${est_cost_all:.2f} with a cold cache)")
    if max_cost_usd is not None and est_cost > max_cost_usd:
        raise JudgeBudgetExceeded(
            f"estimated judge cost ${est_cost:.2f} exceeds --max-judge-cost-usd ${max_cost_usd:.2f}; "
            f"lower --judge-runs, raise the cap, or run --dry-run-judge to inspect the plan")
    if dry_run:
        return {"plan": plan, "dry_run": True, "runs": [], "consensus": {},
                "usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                "evidence": manifests, "cache": cache.stats(), "issues": []}

    usage = {"calls": 0, "cached_calls": 0, "input_tokens": 0, "output_tokens": 0,
             "cost_usd": 0.0, "retries": 0, "schema_retries": 0, "missing_retries": 0}
    per_run: list[dict] = []
    issues: list[dict] = []

    for run_idx in range(1, runs + 1):
        judged: dict = {}
        for bi, (b, body) in enumerate(zip(batches, bodies)):
            prompt = _PROMPT.format(run=run_idx, runs=runs, corrective="", **body)
            data, extra = _call_with_retry(
                prompt, batch_ids=[f["id"] for f in b], tag=f"{tag}:r{run_idx}:b{bi}",
                cache=cache, usage=usage, log_path=log_path, model=model,
                max_tokens=max_tokens, temperature=temperature, issues=issues,
                run_idx=run_idx, runs=runs, body=body, say=say)
            if prompt_dir:
                p = Path(prompt_dir) / f"run{run_idx}" / f"batch{bi:03d}.txt"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(prompt)
            if max_cost_usd is not None and usage["cost_usd"] > max_cost_usd:
                raise JudgeBudgetExceeded(
                    f"judge spend ${usage['cost_usd']:.2f} passed --max-judge-cost-usd "
                    f"${max_cost_usd:.2f} after {usage['calls']} calls")
            _absorb(data, judged, b, valid_key_ids, issues, run_idx, bi, extra)
        per_run.append(judged)
        say(f"  pass {run_idx}/{runs}: {len(judged)} findings judged; "
            f"spend so far ${usage['cost_usd']:.4f} "
            f"({usage['calls']} calls, {usage['cached_calls']} from cache)")

    finding_ids = [f["id"] for f in sorted(findings, key=lambda f: _nat_key(str(f["id"])))]
    cons = consensus(per_run, finding_ids, runs)
    usage["cost_usd"] = round(usage["cost_usd"], 6)
    return {"plan": plan, "dry_run": False, "runs": per_run, "consensus": cons,
            "usage": usage, "evidence": manifests, "cache": cache.stats(), "issues": issues}


def _call_with_retry(prompt, *, batch_ids, tag, cache, usage, log_path, model,
                     max_tokens, temperature, issues, run_idx, runs, body, say):
    """One batch call: cache lookup, schema validation, one corrective retry."""
    def request(p):
        keys = cache_keys(p, model=model, max_tokens=max_tokens, temperature=temperature)
        key = keys[0]
        hit, hit_key = cache.get_any(keys)
        if hit is not None:
            usage["cached_calls"] += 1
            # A legacy "pass k of N" hit is migrated to the pass-index key, but only
            # after it validates (see ``accept``): copying a bad entry would just
            # spread it.
            return hit, {"cached": True, "cache_key": key,
                         "migrate_from": hit_key if hit_key != key else None}
        r = converse_tool(p, system=JUDGE_SYSTEM, max_tokens=max_tokens,
                          temperature=temperature, tag=tag, log_path=log_path, model=model)
        usage["calls"] += 1
        usage["retries"] += r.get("retries", 0)
        for k in ("input_tokens", "output_tokens", "cost_usd"):
            usage[k] += r[k]
        # Not written to the cache here: the answer has not been validated yet.
        return r["data"], {"cached": False, "cache_key": key,
                           "input_tokens": r["input_tokens"],
                           "output_tokens": r["output_tokens"]}

    def accept(data, meta):
        """Normalise, validate, and only then cache. Returns the normalised data.

        Caching used to happen inside ``request``, before validation, so an answer
        that violated the schema was stored under its prompt's key. A re-score then
        replayed the same bad answer (and the same deterministic corrective prompt)
        for free and failed identically, turning a transient model flake into a
        permanent failure. Now an invalid answer raises before anything is written.
        Cache keys and the on-disk entry format are untouched.
        """
        data = sch.drop_null_optionals(data)
        sch.validate(data)
        if meta.get("cached"):
            if meta.get("migrate_from"):
                cache.put(meta["cache_key"], data, {"tag": tag, "migrated_from": meta["migrate_from"]})
        else:
            cache.put(meta["cache_key"], data, {"tag": tag, "input_tokens": meta["input_tokens"],
                                                "output_tokens": meta["output_tokens"]})
        return data

    data, meta = request(prompt)
    try:
        data = accept(data, meta)
    except sch.SchemaError as err:
        usage["schema_retries"] += 1
        issues.append({"kind": "schema_violation", "tag": tag, "error": str(err)})
        say(f"  !! {tag}: judge output violated the schema ({err}); retrying once")
        data, meta = request(prompt + _CORRECTIVE_SCHEMA.format(error=str(err)))
        data = accept(data, meta)  # second failure is fatal, by design

    got = {j.get("finding_id") for j in data.get("judgments", [])}
    missing = [fid for fid in batch_ids if fid not in got]
    if missing and len(missing) > max(1, math.ceil(0.10 * len(batch_ids))):
        usage["missing_retries"] += 1
        issues.append({"kind": "missing_judgments", "tag": tag, "count": len(missing),
                       "of": len(batch_ids)})
        say(f"  !! {tag}: {len(missing)}/{len(batch_ids)} findings missing from judge output; "
            "retrying once")
        data2, meta2 = request(prompt + _CORRECTIVE_MISSING.format(missing=", ".join(missing[:60])))
        data2 = accept(data2, meta2)
        merged = {j["finding_id"]: j for j in data.get("judgments", [])}
        merged.update({j["finding_id"]: j for j in data2.get("judgments", [])})
        data, meta = {"judgments": list(merged.values())}, meta2
    return data, meta


def _model_id() -> str:
    import bedrock_util
    return bedrock_util.MODEL_ID


def _absorb(data, judged, batch, valid_key_ids, issues, run_idx, bi, extra):
    """Normalise one batch of validated judgments into ``judged``."""
    batch_ids = {f["id"] for f in batch}
    for j in data.get("judgments", []):
        fid = j.get("finding_id")
        if fid not in batch_ids:
            issues.append({"kind": "unknown_finding_id", "run": run_idx, "batch": bi, "id": fid})
            continue
        matches, dropped = [], []
        for m in j.get("matches", []) or []:
            if m.get("key_id") in valid_key_ids:
                matches.append({"key_id": m["key_id"], "degree": m.get("degree", "partial")})
            else:
                dropped.append(m.get("key_id"))
        if dropped:
            issues.append({"kind": "unknown_key_id", "run": run_idx, "finding_id": fid,
                           "ids": dropped})
        verdict = j.get("verdict", "uncertain")
        # "matched" with nothing to match is a contradiction: treat as abstention
        if verdict == "matched" and not matches:
            verdict = "uncertain"
            issues.append({"kind": "matched_without_match", "run": run_idx, "finding_id": fid})
        judged[fid] = {"matches": matches, "verdict": verdict, "note": j.get("note", ""),
                       "confidence": j.get("confidence"), "cached": extra.get("cached")}
    for f in batch:
        judged.setdefault(f["id"], {"matches": [], "verdict": sch.UNJUDGED,
                                    "note": "missing from judge output", "confidence": None,
                                    "cached": extra.get("cached")})

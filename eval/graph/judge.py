# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Section 4 — optional, bounded LLM check of concept <-> code bridge links.

Deliberately the only part of graph_eval that talks to a network, and it is off by default
(`--llm` turns it on). Everything else in graph_eval is deterministic, so a CI gate never needs
credentials, and the headline accuracy number in `parity` is never a model's opinion.

Design constraints, all enforced here:
  * bounded      a fixed `--sample N` (default 30) of links, drawn with a seeded RNG from a stable
                 sort, so the same run re-samples the same links; plus a hard `--max-cost` budget
                 checked before every call.
  * schema-valid output via Bedrock Converse TOOL USE with `toolChoice` pinned to the single tool,
                 so the model cannot answer in prose. The returned object is then validated against
                 the same schema locally (no jsonschema dependency) and a malformed reply is retried
                 once before being recorded as `unparsable` rather than silently dropped.
  * temperature 0 and a content-addressed cache keyed on sha256(model + system + prompt + schema),
                 so re-runs are free and reproducible.
  * reported with a bootstrap 95% CI, because 30 samples cannot support a bare percentage.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from collections import Counter
from pathlib import Path

from .load import ConceptResolver, GraphBundle, bare_name, norm_full, word_re

EVAL_DIR = Path(__file__).resolve().parent.parent
MODEL_ID = os.environ.get("EVAL_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
# Same rule as eval/bedrock_util.py: the eval harness calls us-east-1 unless
# EVAL_BEDROCK_REGION says otherwise. AWS_REGION is deliberately NOT read, because a shell
# that defaults to another region would silently move the calls (and the provenance).
REGION = os.environ.get("EVAL_BEDROCK_REGION") or "us-east-1"
PRICE_IN, PRICE_OUT = 3.00, 15.00          # $/1M tokens, Claude Sonnet 4.6
DEFAULT_CACHE = Path(__file__).resolve().parent / ".cache" / "bridge"

SYSTEM = (
    "You audit a legacy-modernisation knowledge graph. Each item gives one business CONCEPT "
    "extracted from a legacy codebase and one CODE SYMBOL the graph claims the concept is about. "
    "Decide whether that link is semantically right. Judge the link, not the quality of either "
    "side. Answer only by calling the tool."
)

TOOL_NAME = "record_verdict"
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["correct", "too_generic", "incorrect"],
            "description": ("correct = the concept really is about this code symbol; "
                            "too_generic = the symbol is mentioned by the concept but is not what "
                            "the concept is about (a shared column name, a module prefix, a utility); "
                            "incorrect = the concept is about something else entirely."),
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string", "description": "<= 25 words."},
    },
    "required": ["verdict", "confidence", "reason"],
    "additionalProperties": False,
}

PROMPT = """CONCEPT
  key:   {concept}
  title: {title}
  text:  {body}

CODE SYMBOL CLAIMED TO BE THE SAME THING
  symbol as written in the concept: {symbol}
  joined to:                        {code_name}   ({match}{kind})
  file:                             {code_path}   (role: {role}, language: {lang})
  what that file does:              {intent}
  business name recorded for it:    {business_name}

SOURCE CONTEXT (lines of {code_path} around the symbol)
{snippet}

Call {tool} once."""


# ----------------------------------------------------------------------------- validation
def validate(obj, schema: dict) -> list[str]:
    """Minimal structural validator for the tool schema above (type/enum/required/range/extra)."""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["not an object"]
    for k in schema.get("required", []):
        if k not in obj:
            errs.append(f"missing required field {k!r}")
    props = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        for k in obj:
            if k not in props:
                errs.append(f"unexpected field {k!r}")
    for k, spec in props.items():
        if k not in obj:
            continue
        v = obj[k]
        t = spec.get("type")
        if t == "string" and not isinstance(v, str):
            errs.append(f"{k}: expected string, got {type(v).__name__}")
        elif t == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errs.append(f"{k}: expected number, got {type(v).__name__}")
            else:
                if "minimum" in spec and v < spec["minimum"]:
                    errs.append(f"{k}: {v} below minimum {spec['minimum']}")
                if "maximum" in spec and v > spec["maximum"]:
                    errs.append(f"{k}: {v} above maximum {spec['maximum']}")
        if "enum" in spec and v not in spec["enum"]:
            errs.append(f"{k}: {v!r} not in {spec['enum']}")
    return errs


# ----------------------------------------------------------------------------- bedrock
class Bedrock:
    """Converse with forced tool use. Wraps boto3 so the module imports without AWS present."""

    def __init__(self, model_id: str = MODEL_ID, region: str = REGION, log_path: Path | None = None):
        self.model_id = model_id
        self.region = region
        # default: the gitignored per-machine log; eval/logs/bedrock_calls.jsonl is a
        # frozen historical record and is never appended to
        self.log_path = Path(log_path or os.environ.get("EVAL_BEDROCK_LOG")
                             or (EVAL_DIR / "logs" / "local" / "bedrock_calls.jsonl"))
        self._client = None

    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config
            self._client = boto3.client(
                "bedrock-runtime", region_name=self.region,
                config=Config(read_timeout=300, retries={"max_attempts": 4, "mode": "adaptive"}))
        return self._client

    def call(self, prompt: str, system: str, tag: str) -> dict:
        t0 = time.time()
        resp = self.client().converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 600, "temperature": 0.0},
            toolConfig={
                "tools": [{"toolSpec": {"name": TOOL_NAME,
                                        "description": "Record one verdict for one bridge link.",
                                        "inputSchema": {"json": TOOL_SCHEMA}}}],
                "toolChoice": {"tool": {"name": TOOL_NAME}},
            },
        )
        usage = resp.get("usage", {})
        it, ot = usage.get("inputTokens", 0), usage.get("outputTokens", 0)
        cost = it / 1e6 * PRICE_IN + ot / 1e6 * PRICE_OUT
        payload, text = None, []
        for blk in resp.get("output", {}).get("message", {}).get("content", []):
            if "toolUse" in blk and blk["toolUse"].get("name") == TOOL_NAME:
                payload = blk["toolUse"].get("input")
            if "text" in blk:
                text.append(blk["text"])
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tag": tag,
               "model": self.model_id, "input_tokens": it, "output_tokens": ot,
               "cost_usd": round(cost, 6), "latency_s": round(time.time() - t0, 2),
               "stop_reason": resp.get("stopReason")}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass
        return {"payload": payload, "text": "\n".join(text), "input_tokens": it,
                "output_tokens": ot, "cost_usd": cost, "stop_reason": resp.get("stopReason")}


# ----------------------------------------------------------------------------- prompt assembly
def _snippet(b: GraphBundle, rel: str, symbol: str, span: int = 6) -> str:
    try:
        lines = b.src.files[rel].read_text(errors="replace").splitlines()
    except (OSError, KeyError):
        return "(file unreadable)"
    needle = bare_name(symbol)
    hit = None
    pat = word_re(needle) if needle else None
    for i, ln in enumerate(lines):
        low = ln.casefold()
        if pat and pat.search(low):
            hit = i
            break
        if needle and needle in low and hit is None:
            hit = i
    if hit is None:
        return "(symbol not found in the file; first 12 lines)\n" + "\n".join(
            f"{i:4d}  {lk}" for i, lk in enumerate(lines[:12], 1))
    a, bb = max(0, hit - span), min(len(lines), hit + span + 1)
    return "\n".join(f"{i:4d}  {lk}" for i, lk in enumerate(lines[a:bb], a + 1))


class BodyIndex:
    """Concept key -> the best available body text, via load.ConceptResolver.

    Prefers a unified concept (whose `body_raw` is the lossless concat of every contributor) over a
    single observation block, and the longest body among ties. `lookup` also returns the match tier
    so a drifted title is distinguishable from a genuinely missing concept.
    """

    def __init__(self, b: GraphBundle):
        self.resolver = ConceptResolver(b.kg_nodes)
        self.bodies: dict[str, str] = {}
        self.rank: dict[str, tuple[int, int]] = {}
        for n in b.kg_nodes:
            nid = n.get("id")
            body = b.concept_bodies.get(nid) or n.get("body_preview") or ""
            self.bodies[nid] = body
            self.rank[nid] = (2 if n.get("node_type") == "unified_concept" else 1, len(body))

    def lookup(self, concept_key: str) -> tuple[str, str]:
        tier, ids = self.resolver.resolve(concept_key)
        if not ids:
            return "", tier
        best = max(ids, key=lambda i: self.rank.get(i, (0, 0)))
        return self.bodies.get(best, ""), tier


def _build(b: GraphBundle, link: dict, body_index: "BodyIndex") -> dict:
    rel, _ = b.src.resolve(link.get("code_path"))
    file_row = next((f for f in b.code_files if str(f.get("path")) == str(link.get("code_path"))), {})
    title = link.get("concept", "")
    body, tier = body_index.lookup(title)
    prompt = PROMPT.format(
        concept=title, title=title.split("::", 1)[-1],
        body=" ".join((body or "(body unavailable — the bridge's concept key does not resolve to "
                               "any node in the concept graph)").split())[:1600],
        symbol=link.get("symbol"), code_name=link.get("code_name"),
        match=link.get("match"),
        kind=f", {link['kind']}" if link.get("kind") else
             (f", business name {link['business_name']!r}" if link.get("business_name") else ""),
        code_path=link.get("code_path"), role=file_row.get("role") or "?",
        lang=file_row.get("lang") or "?",
        intent=file_row.get("business_intent") or "?",
        business_name=link.get("business_name") or "(none)",
        snippet=_snippet(b, rel, link.get("symbol") or "") if rel else "(file not found)",
        tool=TOOL_NAME)
    return {"prompt": prompt, "resolved": rel, "has_body": bool(body), "concept_tier": tier}


def cache_key(model: str, system: str, prompt: str) -> str:
    blob = json.dumps({"model": model, "system": system, "prompt": prompt,
                       "schema": TOOL_SCHEMA, "tool": TOOL_NAME},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- bootstrap
def bootstrap_ci(flags: list[int], iters: int = 4000, seed: int = 7,
                 alpha: float = 0.05) -> dict | None:
    n = len(flags)
    if n == 0:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        s = sum(flags[rng.randrange(n)] for _ in range(n))
        means.append(s / n)
    means.sort()
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return {"point_pct": round(100.0 * sum(flags) / n, 2),
            "ci95_low_pct": round(100.0 * lo, 2), "ci95_high_pct": round(100.0 * hi, 2),
            "n": n, "iterations": iters, "seed": seed}


# ----------------------------------------------------------------------------- entry point
def sample_and_judge(b: GraphBundle, sample: int = 30, seed: int = 0,
                     cache_dir: str | Path | None = None, max_cost: float = 2.0,
                     model_id: str = MODEL_ID, region: str = REGION,
                     client=None, stratify: bool = True) -> dict:
    """Judge a bounded sample of bridge links. `client` is injectable for tests (mock Bedrock)."""
    links = b.bridge_links
    if not links:
        return {"status": "skipped", "reason": "no bridge links"}
    cache = Path(cache_dir or DEFAULT_CACHE)
    cache.mkdir(parents=True, exist_ok=True)

    # stable order, then a seeded draw; stratified so symbol-occurrence failures are not missed
    ordered = sorted(enumerate(links),
                     key=lambda kv: (str(kv[1].get("concept")), str(kv[1].get("symbol")),
                                     str(kv[1].get("code_path")), kv[0]))
    rng = random.Random(seed)
    if stratify:
        src = b.src
        def tier(lk):
            rel, _ = src.resolve(lk.get("code_path"))
            if rel is None:
                return "unresolved"
            return "occurs" if src.occurs(rel, norm_full(lk.get("symbol") or "")) else "not_occurring"
        buckets: dict[str, list] = {}
        for i, lk in ordered:
            buckets.setdefault(tier(lk), []).append((i, lk))
        chosen: list = []
        # proportional allocation with at least one from every non-empty tier
        total = len(ordered)
        for name, rows in sorted(buckets.items()):
            k = max(1, round(sample * len(rows) / total)) if rows else 0
            k = min(k, len(rows))
            chosen += rng.sample(rows, k)
        if len(chosen) > sample:
            chosen = rng.sample(chosen, sample)
        elif len(chosen) < sample:
            taken = {i for i, _ in chosen}
            rest = [x for x in ordered if x[0] not in taken]
            chosen += rng.sample(rest, min(sample - len(chosen), len(rest)))
    else:
        chosen = rng.sample(ordered, min(sample, len(ordered)))
    chosen.sort(key=lambda kv: kv[0])

    bedrock = client if client is not None else Bedrock(model_id, region)
    body_index = BodyIndex(b)
    spent = 0.0
    cached = calls = no_body = 0
    concept_tiers: Counter = Counter()
    rows = []
    for idx, link in chosen:
        built = _build(b, link, body_index)
        no_body += int(not built["has_body"])
        concept_tiers[built["concept_tier"]] += 1
        key = cache_key(model_id, SYSTEM, built["prompt"])
        cpath = cache / f"{key}.json"
        payload = None
        if cpath.is_file():
            try:
                payload = json.loads(cpath.read_text())["payload"]
                cached += 1
            except (OSError, ValueError, KeyError):
                payload = None
        if payload is None:
            if spent >= max_cost:
                rows.append({"link_index": idx, "verdict": "budget_exhausted",
                             "symbol": link.get("symbol"), "code_name": link.get("code_name"),
                             "code_path": link.get("code_path"), "concept": link.get("concept")})
                continue
            errs, res = ["no reply"], None
            for _attempt in range(2):
                res = bedrock.call(built["prompt"], SYSTEM, tag=f"graph_eval:bridge:{key[:8]}")
                spent += res.get("cost_usd", 0.0)
                calls += 1
                errs = validate(res.get("payload"), TOOL_SCHEMA)
                if not errs:
                    break
            if errs:
                rows.append({"link_index": idx, "verdict": "unparsable", "schema_errors": errs,
                             "symbol": link.get("symbol"), "code_name": link.get("code_name"),
                             "code_path": link.get("code_path"), "concept": link.get("concept")})
                continue
            payload = res["payload"]
            try:
                cpath.write_text(json.dumps({"payload": payload, "model": model_id,
                                             "prompt_sha256": key}, indent=1))
            except OSError:
                pass
        rows.append({"link_index": idx, "concept": link.get("concept"),
                     "symbol": link.get("symbol"), "code_name": link.get("code_name"),
                     "code_path": link.get("code_path"), "match": link.get("match"),
                     **{k: payload.get(k) for k in ("verdict", "confidence", "reason")}})

    judged = [r for r in rows if r.get("verdict") in ("correct", "too_generic", "incorrect")]
    flags = [1 if r["verdict"] == "correct" else 0 for r in judged]
    return {
        "status": "ok" if judged else "no usable verdicts",
        "model": model_id, "region": region, "temperature": 0.0,
        "sample_requested": sample, "sample_drawn": len(chosen), "judged": len(judged),
        "sampled_without_a_concept_body": no_body,
        "sampled_concept_match_tiers": dict(concept_tiers),
        "calls": calls, "cache_hits": cached, "cache_dir": str(cache),
        "cost_usd": round(spent, 6), "max_cost_usd": max_cost,
        "verdicts": dict(Counter(r.get("verdict") for r in rows)),
        "agreement": bootstrap_ci(flags),
        "rows": rows,
        "note": "agreement = share of sampled links the judge calls `correct`; `too_generic` marks a "
                "link where the symbol is real but is not what the concept is about.",
    }

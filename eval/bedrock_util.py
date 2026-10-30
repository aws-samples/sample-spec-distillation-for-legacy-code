"""Minimal Bedrock Converse wrapper with per-call token/cost logging.

Every call appends one JSON line to ``eval/logs/local/bedrock_calls.jsonl`` (or the
path given by ``EVAL_BEDROCK_LOG``) so total spend for the experiment can be audited.
That default path is gitignored: each machine keeps its own running log.

``eval/logs/bedrock_calls.jsonl`` (one directory up, ``FROZEN_HISTORICAL_LOG``) is reserved
for a frozen, read-only record of calls behind published results; nothing writes to it.
It is not shipped with this sample.

``REGION`` is the region every call goes to. It is fixed here (``us-east-1``) and does
NOT follow ``AWS_REGION`` / ``AWS_DEFAULT_REGION``; set ``EVAL_BEDROCK_REGION`` to move
it. Provenance records read this constant (or ``region_in_use()``), not the
environment, so a host whose shell defaults to another region is recorded correctly.

boto3 is imported lazily, so importing this module for its constants, pricing or log
helpers needs no AWS SDK and no credentials.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

MODEL_ID = os.environ.get("EVAL_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
DEFAULT_REGION = "us-east-1"
REGION = os.environ.get("EVAL_BEDROCK_REGION") or DEFAULT_REGION
# USD per 1M tokens (Claude Sonnet 4.6 list price; Bedrock on-demand is the same tier).
PRICE_IN = 3.00
PRICE_OUT = 15.00

EVAL_DIR = Path(__file__).resolve().parent
#: committed, frozen record of historical calls; never appended to
FROZEN_HISTORICAL_LOG = EVAL_DIR / "logs" / "bedrock_calls.jsonl"
#: per-machine running log (gitignored)
LOCAL_LOG = EVAL_DIR / "logs" / "local" / "bedrock_calls.jsonl"
DEFAULT_LOG = Path(os.environ.get("EVAL_BEDROCK_LOG") or LOCAL_LOG)

_client = None


def region_in_use() -> str:
    """The region calls actually go to: the live client's, else the configured one."""
    if _client is not None:
        try:
            return str(_client.meta.region_name)
        except Exception:                                      # pragma: no cover
            pass
    return REGION


def client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client(
            "bedrock-runtime",
            region_name=REGION,
            config=Config(read_timeout=600, retries={"max_attempts": 4, "mode": "adaptive"}),
        )
    return _client


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return input_tokens / 1e6 * PRICE_IN + output_tokens / 1e6 * PRICE_OUT


def converse(prompt: str, *, system: str | None = None, max_tokens: int = 8000,
             temperature: float = 0.0, tag: str = "", log_path: Path | None = None) -> dict:
    """Single-turn Converse call. Returns {text, input_tokens, output_tokens, cost_usd, stop_reason}."""
    log_path = Path(log_path or DEFAULT_LOG)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    if system:
        kwargs["system"] = [{"text": system}]
    t0 = time.time()
    resp = client().converse(**kwargs)
    dt = time.time() - t0
    usage = resp.get("usage", {})
    it, ot = usage.get("inputTokens", 0), usage.get("outputTokens", 0)
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "model": MODEL_ID,
        "region": region_in_use(),
        "input_tokens": it,
        "output_tokens": ot,
        "cost_usd": round(cost_usd(it, ot), 6),
        "latency_s": round(dt, 2),
        "stop_reason": resp.get("stopReason"),
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return {"text": text, "input_tokens": it, "output_tokens": ot,
            "cost_usd": rec["cost_usd"], "stop_reason": rec["stop_reason"]}


def summarize_log(log_path: Path | None = None, tag_prefix: str | None = None) -> dict:
    log_path = Path(log_path or DEFAULT_LOG)
    tot = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    if not log_path.exists():
        return tot
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if tag_prefix and not r.get("tag", "").startswith(tag_prefix):
            continue
        tot["calls"] += 1
        tot["input_tokens"] += r["input_tokens"]
        tot["output_tokens"] += r["output_tokens"]
        tot["cost_usd"] += r["cost_usd"]
    tot["cost_usd"] = round(tot["cost_usd"], 4)
    return tot


def extract_json(text: str):
    """Pull the first JSON object/array out of a model response (tolerates ```json fences)."""
    s = text.strip()
    if "```" in s:
        parts = s.split("```")
        for p in parts[1::2]:
            p = p.split("\n", 1)[1] if p.lstrip().startswith(("json", "JSON")) else p
            try:
                return json.loads(p)
            except Exception:
                pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = s.find(opener), s.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    raise ValueError("no JSON found in model response")

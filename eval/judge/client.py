# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bedrock Converse call that returns schema-validated JSON via tool use.

Why not ``bedrock_util.converse``: that helper returns free text, which the old
judge then regex-scraped for a JSON object. A truncated or chatty response could
be mis-parsed into a plausible-looking but wrong verdict set. Here the model is
forced to call one tool whose ``inputSchema`` *is* the contract, and the result
is validated before it is used. Anything else raises.

``bedrock_util`` is shared with other workstreams and is not modified; this
module borrows its client, pricing and log format.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import bedrock_util

from . import schema as sch

#: Bedrock error codes worth retrying. Anything else surfaces immediately.
RETRYABLE = {
    "ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException",
    "InternalServerException", "ModelTimeoutException", "ModelNotReadyException",
    "ServiceQuotaExceededException",
}


class JudgeError(RuntimeError):
    """Base class for every judge failure. Always raised, never swallowed."""


class JudgeTransportError(JudgeError):
    """Bedrock refused the call after the retry budget was spent."""


class JudgeProtocolError(JudgeError):
    """The response carried no usable tool-use block (empty, text-only, truncated)."""


def _error_code(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str(resp.get("Error", {}).get("Code", "")) or exc.__class__.__name__
    return exc.__class__.__name__


def _log_call(log_path: Path, rec: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def converse_tool(prompt: str, *, system: str | None = None,
                  tool_schema: dict | None = None, tool_name: str = sch.TOOL_NAME,
                  tool_description: str = sch.TOOL_DESCRIPTION,
                  max_tokens: int = 12000, temperature: float = 0.0,
                  tag: str = "", log_path: Path | None = None,
                  model: str | None = None, max_retries: int = 5,
                  sleep=time.sleep) -> dict:
    """One Converse call with ``toolChoice`` pinned to ``tool_name``.

    Returns ``{data, input_tokens, output_tokens, cost_usd, stop_reason, latency_s}``
    where ``data`` is the raw tool input (a dict) -- NOT yet validated against the
    schema; the runner validates so it can retry with a corrective message.

    Raises :class:`JudgeTransportError` on exhausted retries and
    :class:`JudgeProtocolError` when no tool-use block came back.
    """
    tool_schema = sch.JUDGE_OUTPUT_SCHEMA if tool_schema is None else tool_schema
    model = model or bedrock_util.MODEL_ID
    log_path = Path(log_path or bedrock_util.DEFAULT_LOG)

    kwargs = dict(
        modelId=model,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        toolConfig={
            "tools": [{"toolSpec": {"name": tool_name, "description": tool_description,
                                    "inputSchema": {"json": tool_schema}}}],
            "toolChoice": {"tool": {"name": tool_name}},
        },
    )
    if system:
        kwargs["system"] = [{"text": system}]

    attempt = 0
    t0 = time.time()
    while True:
        try:
            resp = bedrock_util.client().converse(**kwargs)
            break
        except Exception as exc:  # noqa: BLE001 - classify, then re-raise or retry
            code = _error_code(exc)
            if code in RETRYABLE and attempt < max_retries:
                sleep(min(2 ** attempt, 30))
                attempt += 1
                continue
            if code in RETRYABLE:
                raise JudgeTransportError(
                    f"Bedrock {code} after {attempt + 1} attempts (tag={tag!r})") from exc
            raise
    latency = time.time() - t0

    usage = resp.get("usage") or {}
    it, ot = int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0))
    cost = round(bedrock_util.cost_usd(it, ot), 6)
    stop = resp.get("stopReason")

    content = ((resp.get("output") or {}).get("message") or {}).get("content") or []
    tool_inputs = [b["toolUse"]["input"] for b in content
                   if isinstance(b, dict) and isinstance(b.get("toolUse"), dict)
                   and b["toolUse"].get("name") == tool_name]
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict))

    _log_call(log_path, {
        "ts": datetime.now(timezone.utc).isoformat(), "tag": tag, "model": model,
        "region": bedrock_util.region_in_use(),
        "input_tokens": it, "output_tokens": ot, "cost_usd": cost,
        "latency_s": round(latency, 2), "stop_reason": stop,
        "mode": "tool_use", "tool": tool_name, "retries": attempt,
        "tool_blocks": len(tool_inputs),
    })

    out = {"input_tokens": it, "output_tokens": ot, "cost_usd": cost,
           "stop_reason": stop, "latency_s": round(latency, 2), "retries": attempt,
           "text": text}

    if not content:
        raise JudgeProtocolError(f"empty response from Bedrock (stop_reason={stop!r}, tag={tag!r})")
    if not tool_inputs:
        snippet = (text or "")[:300].replace("\n", " ")
        raise JudgeProtocolError(
            f"no {tool_name!r} tool-use block (stop_reason={stop!r}, tag={tag!r}); text was: {snippet!r}")
    if len(tool_inputs) > 1:
        raise JudgeProtocolError(
            f"{len(tool_inputs)} {tool_name!r} tool-use blocks; expected exactly one (tag={tag!r})")
    if stop == "max_tokens":
        raise JudgeProtocolError(
            f"response hit maxTokens ({max_tokens}); verdicts would be truncated (tag={tag!r})")

    data = tool_inputs[0]
    if isinstance(data, str):  # some runtimes hand back the JSON unparsed
        try:
            data = json.loads(data)
        except Exception as exc:
            raise JudgeProtocolError(f"tool input was a string but not JSON (tag={tag!r})") from exc
    out["data"] = data
    return out

"""
bedrock_client.py — Wraps boto3 Bedrock interactions with retry logic and error classification.

Provides functions for:
- Creating a configured boto3 bedrock-runtime client
- Calling the Amazon Bedrock Converse / ConverseStream APIs
- Calling Converse with structured retry/backoff
"""

import json
import os
import time
import threading
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

# ── Module Constants ─────────────────────────────────────────────────────────

DEFAULT_READ_TIMEOUT = 900
DEFAULT_CONNECT_TIMEOUT = 30
# Idle timeout for a streaming response: max seconds allowed BETWEEN stream events. A healthy
# long generation streams steadily (small gaps); a stalled stream goes silent. Bounding the GAP
# (not total duration) lets long-but-live generations run while killing a hang. A model returning
# zero tokens and then hanging is a failure mode seen in practice, and is exactly what hung a
# page-centric map run — this timeout converts that into a retryable error.
# In-code default is 300s (raised from 120s): large Opus reduce chunks have legitimately long
# gaps between events, and a 120s gap tripped the watchdog mid-generation on an unattended run.
# Still env-overridable via DISTILL_STREAM_IDLE_TIMEOUT_S for faster/slower stall detection.
DEFAULT_STREAM_IDLE_TIMEOUT = float(os.environ.get("DISTILL_STREAM_IDLE_TIMEOUT_S", "300"))
DEFAULT_RETRY_MAX_ATTEMPTS = 0  # We handle retries ourselves
DEFAULT_MAX_RETRIES = 6
DEFAULT_BACKOFF_BASE = 2  # seconds
DEFAULT_TEMPERATURE = float(os.environ.get("DISTILL_TEMPERATURE", "0.0"))

# Per-call output bound (budget guard): no single call may ask for more than this many output
# tokens, so one runaway generation cannot blow DISTILL_MAX_USD (64K Sonnet output tokens is about
# $0.96). 64000 is the Sonnet 4.x output ceiling; the largest output any stage produced on the
# reference runs was 43K. Requests above the bound are clamped (logged once per size).
DEFAULT_MAX_CALL_TOKENS = 64000
_clamp_warned: set = set()


def max_call_tokens() -> int:
    try:
        v = int(os.environ.get("DISTILL_MAX_CALL_TOKENS", "") or DEFAULT_MAX_CALL_TOKENS)
    except ValueError:
        v = DEFAULT_MAX_CALL_TOKENS
    return max(1, v)


def clamp_max_tokens(max_tokens: int) -> int:
    """max_tokens bounded by DISTILL_MAX_CALL_TOKENS (default 64000)."""
    bound = max_call_tokens()
    if max_tokens > bound:
        if max_tokens not in _clamp_warned:
            _clamp_warned.add(max_tokens)
            print(f"[bedrock_client] max_tokens {max_tokens} clamped to {bound} "
                  f"(DISTILL_MAX_CALL_TOKENS per-call bound)", flush=True)
        return bound
    return max_tokens


# Retryable HTTP status codes from Bedrock service errors
RETRYABLE_STATUS_CODES = {500, 502, 503}

# Error codes that should NOT be retried (throttling — let caller handle backpressure)
THROTTLE_ERROR_CODES = {"ThrottlingException", "TooManyRequestsException"}

# ── Shared retryable-exception taxonomy (map + reduce) ───────────────────────
# These two tuples are the SINGLE source of truth for "which raised exception is a transient,
# resubmit-worthy Bedrock condition" — classified by exception TYPE NAME and by SUBSTRING in the
# message, so they catch errors from any path (ClientError, StreamIdleTimeout, botocore
# ReadTimeoutError/ConnectTimeoutError) without importing every exception class. They mirror the
# resubmit set the page-centric map executor used to carry as a local closure (jobs.py); the map
# executor and the reduce retry now BOTH route through is_retryable_bedrock_error so the two
# paths can never drift. Keep these in sync if a new transient Bedrock failure mode shows up.
THROTTLE_EXC_NAMES = (
    "ThrottlingException", "TooManyRequestsException", "OverloadedError",
    "TooManyRequestsError", "429",
)
RETRYABLE_EXC_NAMES = THROTTLE_EXC_NAMES + (
    "StreamIdleTimeout", "ReadTimeoutError", "ConnectTimeoutError",
)


def _matches_tokens(exc: Exception, tokens: tuple) -> bool:
    """True if `exc`'s type name is one of `tokens`, or any token appears in str(exc), or the
    message carries a rate/limit throttle phrasing. Operates on ANY Exception (not just
    ClientError) so it works across the converse, streaming, and botocore timeout paths."""
    if type(exc).__name__ in tokens:
        return True
    s = str(exc)
    if any(t in s for t in tokens):
        return True
    low = s.lower()
    return "rate" in low and "limit" in low


def is_throttle_error(exc: Exception) -> bool:
    """True if `exc` is a throttling/overload condition (needs longer backoff). Name/string based,
    so it classifies exceptions from any path. Distinct from the ClientError-typed _is_throttle
    (used by call_converse_with_retry); this one is the shared, cross-path check for reduce."""
    return _matches_tokens(exc, THROTTLE_EXC_NAMES)


def is_retryable_bedrock_error(exc: Exception) -> bool:
    """True if `exc` is a transient, resubmit-worthy Bedrock condition (throttle/overload OR a
    stalled stream OR a read/connect timeout). Shared by the page-centric map executor and the
    reduce reconciliation retry so both agree on what is worth retrying."""
    return _matches_tokens(exc, RETRYABLE_EXC_NAMES)

# ── Telemetry ────────────────────────────────────────────────────────────────

_TELEMETRY_DIR = os.environ.get("DISTILL_TELEMETRY_DIR", "")
_telemetry_lock = threading.Lock()


def _emit_telemetry(record: dict) -> None:
    """Append a telemetry record to the appropriate JSONL file.

    File: {DISTILL_TELEMETRY_DIR}/{still}_{phase}.jsonl
    Falls back to {DISTILL_TELEMETRY_DIR}/calls.jsonl if still/phase not set.

    Reads DISTILL_TELEMETRY_DIR at CALL time (falling back to the import-time _TELEMETRY_DIR),
    so telemetry is robust to env set after this module is imported (import order was the
    cause of a validation run silently writing zero telemetry files).
    """
    tel_dir = os.environ.get("DISTILL_TELEMETRY_DIR", "") or _TELEMETRY_DIR
    if not tel_dir:
        return
    
    tel_path = Path(tel_dir)
    tel_path.mkdir(parents=True, exist_ok=True)
    
    still = record.get("still", "unknown")
    phase = record.get("phase", "unknown")
    filename = f"{still}_{phase}.jsonl"
    filepath = tel_path / filename
    
    line = json.dumps(record, default=str) + "\n"
    with _telemetry_lock:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line)


def log_llm_call(
    status: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    model_id: str = "",
    error_code: str = "",
    still: str = "",
    phase: str = "",
    page_id: str = "",
    job_id: str = "",
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> None:
    """Log an LLM call to telemetry. Called after every Bedrock interaction.

    cache_read_tokens / cache_write_tokens surface prompt-cache utilization (page-centric
    orchestration, X-9) so the cache win — and any regression — is observable in telemetry.
    """
    _emit_telemetry({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time()*1000)%1000:03d}Z",
        "job_id": job_id,
        "page_id": page_id,
        "still": still,
        "phase": phase,
        "status": status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "latency_ms": latency_ms,
        "model_id": model_id,
        "error_code": error_code or None,
    })


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class ConverseResult:
    """Result from a single Converse API call."""

    output_text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    # Prompt-cache accounting (orchestration-strategy X-9 / page-centric). Default 0 so the
    # still-centric path (no caching) and older callers are unaffected.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


# ── Client Creation ──────────────────────────────────────────────────────────


def create_client(profile: str, region: str):
    """Create a boto3 bedrock-runtime client with configured timeouts.

    Args:
        profile: AWS profile name (e.g. 'my-profile'). Empty string uses default credential chain.
        region: AWS region (e.g. 'us-east-1').

    Returns:
        A boto3 bedrock-runtime client configured with extended read timeout
        and retries disabled (handled externally by call_converse_with_retry).
    """
    session_kwargs = {"region_name": region}
    if profile:
        session_kwargs["profile_name"] = profile
    session = boto3.Session(**session_kwargs)
    config = BotoConfig(
        read_timeout=DEFAULT_READ_TIMEOUT,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT,
        retries={"max_attempts": DEFAULT_RETRY_MAX_ATTEMPTS},
    )
    return session.client("bedrock-runtime", config=config)


# ── Converse API ─────────────────────────────────────────────────────────────


def call_converse(
    client,
    model_id: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
    temperature: float = DEFAULT_TEMPERATURE,
) -> ConverseResult:
    """Make a single Converse API call to Bedrock.

    Args:
        client: boto3 bedrock-runtime client.
        model_id: Model identifier (e.g. 'us.anthropic.claude-sonnet-4-6').
        system_prompt: System-level instruction text.
        user_content: User message content.
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature (default 0.1).

    Returns:
        ConverseResult with output text, token counts, and stop reason.

    Raises:
        ClientError: On API errors (throttling, validation, server errors).
        ReadTimeoutError: If the read timeout is exceeded.
        ConnectTimeoutError: If the connection timeout is exceeded.
    """
    max_tokens = clamp_max_tokens(max_tokens)
    resolved_id = _converse_model_id(model_id)
    inference_config = {"maxTokens": max_tokens}
    if _converse_temperature_ok(resolved_id):
        inference_config["temperature"] = temperature
    response = client.converse(
        modelId=resolved_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_content}]}],
        inferenceConfig=inference_config,
    )

    output_text = _converse_output_text(response)
    usage = response.get("usage", {})
    stop_reason = response.get("stopReason", "unknown")

    return ConverseResult(
        output_text=output_text,
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        stop_reason=stop_reason,
        cache_read_tokens=usage.get("cacheReadInputTokens", 0) or 0,
        cache_write_tokens=usage.get("cacheWriteInputTokens", 0) or 0,
    )


def call_converse_blocks(
    client,
    model_id: str,
    system_blocks: list,
    user_text: str,
    max_tokens: int,
    temperature: float = DEFAULT_TEMPERATURE,
) -> ConverseResult:
    """Converse call accepting PRE-BUILT system content blocks (so a `cachePoint` block can be
    planted after the page). Used by the page-centric orchestration; still-centric keeps the
    string-based `call_converse`. Same usage/cache parsing as `call_converse`.

    Args:
        client: boto3 bedrock-runtime client.
        model_id: Model identifier.
        system_blocks: Converse `system` array (e.g. [{text: preamble}, {text: page}, {cachePoint}]).
        user_text: The user message text (the still instruction).
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature.

    Returns:
        ConverseResult with output text, token counts (incl. cache read/write), and stop reason.
    """
    max_tokens = clamp_max_tokens(max_tokens)
    resolved_id = _converse_model_id(model_id)
    inference_config = {"maxTokens": max_tokens}
    if _converse_temperature_ok(resolved_id):
        inference_config["temperature"] = temperature
    response = client.converse(
        modelId=resolved_id,
        system=system_blocks,
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        inferenceConfig=inference_config,
    )
    output_text = _converse_output_text(response)
    usage = response.get("usage", {})
    return ConverseResult(
        output_text=output_text,
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        stop_reason=response.get("stopReason", "unknown"),
        cache_read_tokens=usage.get("cacheReadInputTokens", 0) or 0,
        cache_write_tokens=usage.get("cacheWriteInputTokens", 0) or 0,
    )


def _is_retryable(error: ClientError) -> bool:
    """Determine if a ClientError is retryable.

    Retryable: 500, 502, 503 status codes AND ThrottlingException (with longer backoff).
    Not retryable: ValidationException, other 4xx responses.
    """
    error_code = error.response.get("Error", {}).get("Code", "")

    # Throttle IS retryable now — with longer backoff handled by caller
    if error_code in THROTTLE_ERROR_CODES:
        return True

    status_code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    if status_code in RETRYABLE_STATUS_CODES:
        return True

    # Any other 4xx is not retryable
    return False


def _is_throttle(error: ClientError) -> bool:
    """Check if error is a throttling error (needs longer backoff)."""
    error_code = error.response.get("Error", {}).get("Code", "")
    return error_code in THROTTLE_ERROR_CODES


def call_converse_with_retry(
    client,
    model_id: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
    temperature: float = DEFAULT_TEMPERATURE,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> ConverseResult:
    """Call Converse API with aggressive retry to guarantee completion.

    Retry policy:
    - Retries on: ReadTimeoutError, ConnectTimeoutError, 500/502/503, ThrottlingException.
    - No retry on: ValidationException, other non-transient 4xx.
    - Throttle backoff: 30s, 60s, 120s, 240s, 480s, 960s (with jitter). Never gives up on throttle.
    - Server error backoff: 2s, 4s, 8s, 16s, 32s, 64s.

    Args:
        client: boto3 bedrock-runtime client.
        model_id: Model identifier.
        system_prompt: System-level instruction text.
        user_content: User message content.
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature (default 0.1).
        max_retries: Maximum retry attempts (default 6).

    Returns:
        ConverseResult on success.

    Raises:
        ClientError: If non-retryable or retries exhausted on non-throttle errors.
        ReadTimeoutError: If retries exhausted on read timeouts.
        ConnectTimeoutError: If retries exhausted on connect timeouts.
    """
    import random
    last_exception: Exception | None = None

    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            result = call_converse(
                client, model_id, system_prompt, user_content, max_tokens, temperature
            )
            latency_ms = int((time.time() - t0) * 1000)
            log_llm_call(
                status="success",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=latency_ms,
                model_id=model_id,
            )
            return result
        except (ReadTimeoutError, ConnectTimeoutError) as e:
            latency_ms = int((time.time() - t0) * 1000)
            log_llm_call(status="error", latency_ms=latency_ms, model_id=model_id, error_code=type(e).__name__)
            last_exception = e
            if attempt < max_retries:
                backoff = DEFAULT_BACKOFF_BASE * (2**attempt)
                time.sleep(backoff)
                continue
            raise
        except ClientError as e:
            latency_ms = int((time.time() - t0) * 1000)
            status = "throttled" if _is_throttle(e) else "error"
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            log_llm_call(status=status, latency_ms=latency_ms, model_id=model_id, error_code=error_code)
            last_exception = e
            if not _is_retryable(e):
                raise
            if attempt < max_retries:
                if _is_throttle(e):
                    base_backoff = 30 * (2 ** min(attempt, 5))
                    jitter = random.uniform(0, base_backoff * 0.5)
                    backoff = base_backoff + jitter
                else:
                    backoff = DEFAULT_BACKOFF_BASE * (2**attempt)
                time.sleep(backoff)
                continue
            raise

    # Should not reach here, but satisfy type checker
    raise last_exception  # type: ignore[misc]


# ── Model calls used by the pipeline stages (Converse / ConverseStream) ──────
#
# Every pipeline stage calls Amazon Bedrock through the public Converse API. Small requests use
# `converse`; large ones (max_tokens >= STREAMING_THRESHOLD) use `converse_stream` so a long
# generation is never cut off by a client read timeout, and a stalled stream is bounded by an
# idle watchdog instead of blocking forever.
#
# Bedrock throttles on token quotas (HTTP 429 / ThrottlingException). These functions do not retry
# by themselves: the page-centric executor's congestion controller (jobs._pagecentric_executor)
# and the reduce retry loop classify the raised exception with is_retryable_bedrock_error() and
# back off. Use call_converse_with_retry when you want a self-retrying single call.

STREAMING_THRESHOLD = 16384


def create_llm_client(profile: str, region: str):
    """Create the bedrock-runtime client used by call_llm / call_llm_blocks.

    Uses the standard AWS credential chain (environment variables, shared config/credentials
    files, IAM Identity Center, or an instance/container role). `profile` is optional.
    """
    return create_client(profile, region)


def _converse_request(model_id: str, system_blocks: list, user_text: str, max_tokens: int,
                      temperature: float) -> dict:
    resolved_id = _converse_model_id(model_id)
    inference_config = {"maxTokens": clamp_max_tokens(max_tokens)}
    if _converse_temperature_ok(resolved_id):
        inference_config["temperature"] = temperature
    return {
        "modelId": resolved_id,
        "system": system_blocks,
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
        "inferenceConfig": inference_config,
    }


def _converse_any(client, model_id: str, system_blocks: list, user_text: str, max_tokens: int,
                  temperature: float) -> ConverseResult:
    request = _converse_request(model_id, system_blocks, user_text, max_tokens, temperature)
    if request["inferenceConfig"]["maxTokens"] >= STREAMING_THRESHOLD:
        return _call_llm_streaming(client, request)
    response = client.converse(**request)
    usage = response.get("usage", {})
    return ConverseResult(
        output_text=_converse_output_text(response),
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        stop_reason=response.get("stopReason", "unknown"),
        cache_read_tokens=usage.get("cacheReadInputTokens", 0) or 0,
        cache_write_tokens=usage.get("cacheWriteInputTokens", 0) or 0,
    )


def call_llm(
    client,
    model_id: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int,
    temperature: float = DEFAULT_TEMPERATURE,
) -> ConverseResult:
    """Single Bedrock Converse call with a plain-text system prompt. Logs one telemetry record.

    Args:
        client: bedrock-runtime client from create_llm_client / create_client.
        model_id: Bedrock model or inference-profile id (e.g. 'us.anthropic.claude-sonnet-4-6').
        system_prompt: System-level instruction text.
        user_content: User message content.
        max_tokens: Maximum tokens in the response (clamped by DISTILL_MAX_CALL_TOKENS).
        temperature: Sampling temperature (omitted for model families that reject it).
    """
    t0 = time.time()
    result = _converse_any(client, model_id, [{"text": system_prompt}], user_content,
                           max_tokens, temperature)
    log_llm_call(
        status="success",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=int((time.time() - t0) * 1000),
        model_id=model_id,
    )
    return result


def call_llm_blocks(
    client,
    model_id: str,
    system_blocks: list,
    user_text: str,
    max_tokens: int,
    temperature: float = DEFAULT_TEMPERATURE,
) -> ConverseResult:
    """Converse call with PRE-BUILT system blocks, so a `cachePoint` block can follow the page
    (prompt caching on the page prefix). Built by orchestration_strategy.compose_map_request.

    Intentionally does NOT log telemetry: the page-centric executor is the single logger for this
    path (it has the still/phase/page_id/job_id context), so logging here would double-count.
    """
    return _converse_any(client, model_id, system_blocks, user_text, max_tokens, temperature)


class StreamIdleTimeout(Exception):
    """Raised when a streaming response goes silent longer than the idle budget — a stalled
    stream, not a slow one. Treated as a retryable/backpressure-adjacent error by callers so a
    hung inference can never block the run indefinitely."""


def _call_llm_streaming(client, request: dict,
                        idle_timeout: float = DEFAULT_STREAM_IDLE_TIMEOUT) -> ConverseResult:
    """ConverseStream call, accumulating the full response.

    Bounded by an IDLE timeout: a watchdog thread closes the stream if no event arrives within
    `idle_timeout` seconds, and the call raises StreamIdleTimeout. This bounds the GAP between
    events (a live long generation streams steadily; a stall goes silent), so a legitimately long
    response runs to completion while a hung stream is killed instead of blocking forever.
    """
    response = client.converse_stream(**request)
    stream = response["stream"]
    last_event = [time.time()]
    done = threading.Event()
    timed_out = [False]

    def _watchdog():
        while not done.wait(timeout=min(5.0, idle_timeout / 2.0)):
            if time.time() - last_event[0] > idle_timeout:
                timed_out[0] = True
                try:
                    stream.close()
                except Exception:
                    pass
                return

    wd = threading.Thread(target=_watchdog, daemon=True)
    wd.start()
    texts: list[str] = []
    stop_reason = "unknown"
    usage: dict = {}
    try:
        for event in stream:
            last_event[0] = time.time()
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    texts.append(delta["text"])
            elif "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason", stop_reason)
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {}) or {}
        if timed_out[0]:
            raise StreamIdleTimeout(f"stream idle > {idle_timeout:.0f}s — stalled response")
    except StreamIdleTimeout:
        raise
    except Exception as e:
        if timed_out[0]:
            raise StreamIdleTimeout(
                f"stream idle > {idle_timeout:.0f}s — stalled response (closed: "
                f"{type(e).__name__})") from e
        raise
    finally:
        done.set()

    return ConverseResult(
        output_text="".join(texts),
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        stop_reason=stop_reason,
        cache_read_tokens=usage.get("cacheReadInputTokens", 0) or 0,
        cache_write_tokens=usage.get("cacheWriteInputTokens", 0) or 0,
    )


def _model_rejects_temperature(model: str) -> bool:
    """Return True for model families that reject the `temperature` parameter.

    Some newer Anthropic models deprecate `temperature` and return HTTP 400 if it is sent. The
    prefix match keeps future variants of those families covered without a code change.
    """
    return model.startswith((
        "anthropic.claude-sonnet-5",
        "anthropic.claude-opus-5",
    ))


def _converse_output_text(response: dict) -> str:
    """Extract the assistant text from a Converse response, robust to block ordering/types.

    The Converse `output.message.content` is a list of blocks. Older/simple responses put a
    single `{"text": ...}` first, but newer models (e.g. opus-5) can emit a `reasoningContent`
    (thinking) block — or other non-text blocks — BEFORE the text block. Blindly reading
    content[0]["text"] then raises `KeyError: 'text'`. Collect every `text` block in order and
    join them, ignoring reasoning/tool/other blocks.
    """
    blocks = (((response or {}).get("output") or {}).get("message") or {}).get("content") or []
    texts = [b["text"] for b in blocks if isinstance(b, dict) and "text" in b]
    return "".join(texts)


# Short model aliases used as CLI/env defaults in this repo -> the Bedrock runtime model id.
# Override any of them with a full model or inference-profile id (or an ARN) if your account
# uses a different one; check `aws bedrock list-inference-profiles` in your region.
MODEL_ALIASES = {
    "anthropic.claude-haiku-4-5": "anthropic.claude-haiku-4-5-20251001-v1:0",
}


def _converse_model_id(model_id: str) -> str:
    """Resolve a model id for the bedrock-runtime Converse API (on-demand path).

    Some models cannot be invoked on Converse with a bare model id and on-demand throughput —
    Bedrock returns `ValidationException: Invocation of model ID <id> with on-demand throughput
    isn't supported. Retry ... with ... an inference profile that contains this model.` These
    models must be addressed through a cross-region inference profile, whose id is the bare id
    prefixed with a geo (`us.`). Every call in this module goes through this mapping.

    A bare `anthropic.claude-...` id is mapped to its `us.` inference-profile id; ids that are
    already a profile (`us.`/`eu.`/`apac.`) or an ARN are returned unchanged.
    """
    model_id = MODEL_ALIASES.get(model_id, model_id)
    if model_id.startswith(("us.", "eu.", "apac.", "global.", "arn:")):
        return model_id
    if model_id.startswith("anthropic."):
        return "us." + model_id
    return model_id


def _converse_temperature_ok(model_id: str) -> bool:
    """Return True if the Converse call may send `temperature` for this model.

    Mirrors `_model_rejects_temperature` but keyed on the Converse-side model id (bare or
    profile form), e.g. `anthropic.claude-opus-5-5` or `us.anthropic.claude-opus-5-5`. The
    opus-5 / sonnet-5 families reject `temperature` with a 400, so it must be omitted.
    """
    bare = model_id.split(".", 1)[1] if model_id.startswith(("us.", "eu.", "apac.")) else model_id
    return not bare.startswith((
        "anthropic.claude-sonnet-5",
        "anthropic.claude-opus-5",
    ))

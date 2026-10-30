#!/usr/bin/env python3
"""
spec_tooling.py — shared plumbing for the spec-query MCP tool modules.

WHY THIS MODULE EXISTS
----------------------
Tool modules must not import `spec_server`: the server imports them, so that would be
circular. The pieces every tool needs — the stdout guard, the error envelope, the AWS
config — therefore live here, and both sides import this.

It also means a new tool module is a NEW FILE and nothing else: `spec_server` discovers
`tools_*.py` automatically, so several people can add tools in parallel without ever
editing the same file.

ADDING A TOOL MODULE
--------------------
Create `tools_<area>.py` next to this file with:

    from spec_tooling import err, quiet

    def register(server):
        @server.tool(description="...what it answers, and whether it costs money...")
        @quiet
        async def my_tool(run_dir: str, ...) -> str:
            ...
            return json.dumps(result)

Rules that matter:
  * `@server.tool` outermost, `@quiet` directly above `async def` — FastMCP builds the
    input schema by introspecting the function, and @quiet preserves the signature.
  * Return a JSON string. Never raise into the transport; use `err()`.
  * Say in the description whether the tool calls a model, so an agent can prefer the
    free structured tools when they suffice.
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import sys

# ── Environment ──────────────────────────────────────────────────────────────
# Only model-backed tools need these. Read at import of this module (cheap, no client
# is constructed); the Bedrock client itself is built lazily inside the tool that
# needs it, so the read-only tools work with no AWS credentials at all.

AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
AWS_DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")


def quiet(fn):
    """Keep tool bodies off stdout, which on stdio transport IS the JSON-RPC channel.

    The pipeline modules we reuse (run_manager, knowledge, dependency_query, …) print
    progress to stdout. A single stray line corrupts the protocol stream: the Python SDK
    logs "Failed to parse JSONRPC message from server" and limps on, but a stricter
    client drops the session outright.

    Redirecting to stderr keeps that output visible for debugging while leaving stdout
    exclusively to the transport. Applied as a decorator rather than per-call so a new
    tool cannot forget it.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        with contextlib.redirect_stdout(sys.stderr):
            return await fn(*args, **kwargs)
    return wrapper


def err(message: str, **extra) -> str:
    """Uniform error envelope. Tools never raise into the transport."""
    return json.dumps({"error": message, **extra})


def ok(payload) -> str:
    """Serialise a successful result."""
    return json.dumps(payload)

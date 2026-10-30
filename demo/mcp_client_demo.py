#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
mcp_client_demo.py - drive the spec-distillation MCP server (bedrock-distill)
as an MCP client over stdio, the same way Kiro or Claude Code would.

What it does
  1. Launches runtimes/mcp-server/server.py as a stdio subprocess and performs
     the MCP initialize handshake.
  2. Lists the server's tools (name, purpose, parameters).
  3. If --run-dir is given (a COMPLETED run output dir, i.e. DISTILL_OUTPUT):
       a. If <run-dir>/stills/shared_vectors.db is missing, calls
          build_knowledge_graph once per still that has a
          stills/<still>/<still>_raw_concat.md (mirrors Phase 2.5).
       b. Calls query_spec for each --question and prints the answer plus the
          cited concept blocks. With --evidence (default on) it also prints the
          "Evidence: file#Lx-Ly" source citations of each cited block, read
          client-side from the run's vector store (the server does not return
          them).

Bedrock cost: list-only mode makes no model calls (the server creates boto3
clients at start-up but does not invoke them). Each query_spec call
sends the WHOLE bottled spec plus top-k blocks to the model (~40K input tokens
on the synthetic AP run), so --max-questions caps the number of questions.

Requirements: the pipeline venv created by ./setup.sh (mcp 1.x, sentence-transformers,
numpy, boto3).

Examples
  # connection + tool listing only (no Bedrock calls)
  python demo/mcp_client_demo.py

  # ask questions against a completed run
  AWS_PROFILE=<sandbox> AWS_DEFAULT_REGION=us-east-1 \\
    python demo/mcp_client_demo.py --run-dir output/run-ap \\
      -q "When can an AP invoice be paid?" \\
      -q "How are duplicate vendor invoices detected?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import textwrap
from datetime import timedelta
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:  # pragma: no cover - environment guard
    sys.exit(
        "ERROR: the 'mcp' Python SDK is not importable. Run with the pipeline venv "
        "(runtimes/mcp-server/.venv/bin/python) created by ./setup.sh."
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVER = REPO_ROOT / "runtimes" / "mcp-server" / "server.py"
DEFAULT_VENV_PY = REPO_ROOT / "runtimes" / "mcp-server" / ".venv" / "bin" / "python"
MANIFEST_DIR = REPO_ROOT / "runtimes" / "pipeline" / "manifests"

# Indicative on-demand price for the default model, USD per 1M tokens. Used only
# to print a rough per-question estimate; check current Bedrock pricing.
DEFAULT_PRICE_IN = 3.0
DEFAULT_PRICE_OUT = 15.0

EVIDENCE_RE = re.compile(r"^\s*[-*]?\s*\**(Evidence|Source|Sources|Citation)s?\**\s*:\s*(.+)$", re.I | re.M)


def hr(title: str = "") -> None:
    print("\n" + ("─" * 4 + f" {title} " if title else "") + "─" * max(0, 76 - len(title)))


def tool_text(result) -> str:
    """Concatenate the text content items of a CallToolResult."""
    parts = []
    for item in result.content or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


def tool_json(result) -> dict:
    text = tool_text(result)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": True, "message": text or "<empty tool result>"}


def describe_params(schema: dict) -> str:
    props = (schema or {}).get("properties", {})
    required = set((schema or {}).get("required", []))
    out = []
    for name, spec in props.items():
        typ = spec.get("type") or "/".join(
            s.get("type", "?") for s in spec.get("anyOf", []) if isinstance(s, dict)
        ) or "any"
        out.append(f"{name}{'*' if name in required else ''}:{typ}")
    return ", ".join(out) or "(none)"


async def list_tools(session: ClientSession) -> list:
    tools = (await session.list_tools()).tools
    hr(f"{len(tools)} tools exposed by the server  (* = required)")
    for t in tools:
        desc = " ".join((t.description or "").split())
        print(f"\n  {t.name}({describe_params(t.inputSchema)})")
        print(textwrap.indent(textwrap.fill(desc, 96), "      "))
    return tools


async def ensure_knowledge_graph(session: ClientSession, run_dir: Path, mode: str) -> Path | None:
    stills_dir = run_dir / "stills"
    vector_db = stills_dir / "shared_vectors.db"
    if vector_db.exists() and mode != "always":
        print(f"\n  Vector store present: {vector_db} (skipping build_knowledge_graph)")
        return vector_db
    if mode == "never":
        print(f"\n  ERROR: {vector_db} not found and --build-kg=never")
        return None

    concats = sorted(stills_dir.glob("*/*_raw_concat.md"))
    if not concats:
        print(f"\n  ERROR: no stills/<still>/<still>_raw_concat.md under {run_dir}; "
              "is this a completed (Phase 2) run dir?")
        return None

    hr("build_knowledge_graph (Phase 2.5, local embeddings, no Bedrock)")
    for concat in concats:
        still = concat.parent.name
        manifest = MANIFEST_DIR / f"{still}.txt"
        args = {
            "concat_path": str(concat),
            "still_name": still,
            "output_dir": str(concat.parent / "final"),
            # Always pass a path, even if that still has no manifest (history, runtime):
            # the server's default manifest_path="" resolves to "." and fails with
            # "[Errno 21] Is a directory". run_distillation.sh passes the path the same way.
            "manifest_path": str(manifest),
            "graph_path": str(stills_dir / "knowledge_graph.json"),
        }
        print(f"  [{still}] building ...", flush=True)
        res = await session.call_tool("build_knowledge_graph", args,
                                      read_timeout_seconds=timedelta(minutes=30))
        data = tool_json(res)
        if res.isError or data.get("error"):
            print(f"  [{still}] FAILED: {data.get('message') or data.get('error')}")
            continue
        keep = {k: data[k] for k in ("blocks_parsed", "blocks_embedded", "duplicates_found",
                                     "vector_db_path") if k in data}
        print(f"  [{still}] ok {json.dumps(keep)}")
    return vector_db if vector_db.exists() else None


def load_evidence(vector_db: Path, block_ids: list[str]) -> dict[str, list[str]]:
    """Client-side citation enrichment: pull 'Evidence:' lines from block bodies."""
    out: dict[str, list[str]] = {}
    if not block_ids or not vector_db.exists():
        return out
    conn = sqlite3.connect(f"file:{vector_db}?mode=ro", uri=True)
    try:
        q = f"SELECT block_id, source_page, body FROM vectors WHERE block_id IN ({','.join('?' * len(block_ids))})"
        for block_id, page, body in conn.execute(q, block_ids):
            refs = [m.group(2).strip() for m in EVIDENCE_RE.finditer(body or "")]
            if not refs and page:
                refs = [f"(page {page})"]
            out[block_id] = refs
    finally:
        conn.close()
    return out


async def ask(session: ClientSession, vector_db: Path, question: str, top_k: int,
              show_sources: int, evidence: bool, price_in: float, price_out: float) -> float | None:
    hr(f"Q: {question}")
    res = await session.call_tool(
        "query_spec",
        {"question": question, "vector_db_path": str(vector_db), "top_k": top_k},
        read_timeout_seconds=timedelta(minutes=10),
    )
    data = tool_json(res)
    if res.isError or data.get("error"):
        print(f"  ERROR: {data.get('message') or data.get('error')}")
        return None

    print("\nA:\n" + textwrap.indent(data.get("answer", "").strip(), "  "))

    sources = data.get("sources", [])[:show_sources]
    ev = load_evidence(vector_db, [s["block_id"] for s in sources]) if evidence else {}
    print(f"\n  Cited/retrieved concept blocks (top {len(sources)} of {len(data.get('sources', []))}):")
    for i, s in enumerate(sources, 1):
        print(f"   [{i}] {s.get('title')}  (still={s.get('still')}, type={s.get('type')}, "
              f"relevance={s.get('relevance')}, block={s.get('block_id')})")
        for sf in s.get("source_files", []):          # server-side, only if source_refs exists
            print(f"         source: {sf.get('path')} {sf.get('url') or ''}".rstrip())
        for ref in ev.get(s.get("block_id"), []):     # client-side enrichment
            print(f"         evidence: {ref}")

    tin, tout = data.get("input_tokens") or 0, data.get("output_tokens") or 0
    cost = tin / 1e6 * price_in + tout / 1e6 * price_out
    print(f"\n  tokens: in={tin} out={tout}  est. cost ≈ ${cost:.3f}")
    return cost


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", type=Path, help="completed run output dir (DISTILL_OUTPUT)")
    ap.add_argument("-q", "--question", action="append", default=[], help="question (repeatable)")
    ap.add_argument("--top-k", type=int, default=15)
    ap.add_argument("--show-sources", type=int, default=8, help="max cited blocks to print per answer")
    ap.add_argument("--build-kg", choices=["auto", "always", "never"], default="auto",
                    help="auto: build only if shared_vectors.db is missing")
    ap.add_argument("--no-evidence", dest="evidence", action="store_false",
                    help="skip client-side Evidence: citation enrichment")
    ap.add_argument("--max-questions", type=int, default=3, help="cost guard")
    ap.add_argument("--server", type=Path, default=DEFAULT_SERVER, help="path to server.py")
    ap.add_argument("--python", default=str(DEFAULT_VENV_PY if DEFAULT_VENV_PY.exists() else sys.executable),
                    help="interpreter used to launch the server (default: pipeline venv)")
    ap.add_argument("--price-in", type=float, default=DEFAULT_PRICE_IN, help="USD per 1M input tokens")
    ap.add_argument("--price-out", type=float, default=DEFAULT_PRICE_OUT, help="USD per 1M output tokens")
    args = ap.parse_args()

    if len(args.question) > args.max_questions:
        print(f"ERROR: {len(args.question)} questions > --max-questions={args.max_questions} (cost guard)")
        return 2
    if args.question and not args.run_dir:
        print("ERROR: --question requires --run-dir")
        return 2

    env = dict(os.environ)
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    env.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    env.setdefault("PYTHONUNBUFFERED", "1")
    params = StdioServerParameters(
        command=args.python,
        args=[str(args.server.resolve())],
        env=env,
        cwd=str(args.server.resolve().parent),
    )

    hr("connect")
    print(f"  server : {args.python} {args.server}")
    print(f"  region : {env['AWS_DEFAULT_REGION']}  model: {env['BEDROCK_MODEL_ID']}  "
          f"profile: {env.get('AWS_PROFILE') or '(default chain)'}")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"  connected: {init.serverInfo.name} {init.serverInfo.version or ''} "
                  f"(protocol {init.protocolVersion})")
            await list_tools(session)

            if not args.run_dir:
                print("\n  No --run-dir given: connection + tool listing only (no Bedrock calls).")
                return 0

            run_dir = args.run_dir.resolve()
            vector_db = await ensure_knowledge_graph(session, run_dir, args.build_kg)
            if vector_db is None:
                return 1
            if not args.question:
                print("\n  Knowledge graph ready. Pass -q/--question to query it.")
                return 0

            total, failed = 0.0, 0
            for q in args.question:
                cost = await ask(session, vector_db, q, args.top_k, args.show_sources,
                                 args.evidence, args.price_in, args.price_out)
                if cost is None:
                    failed += 1
                else:
                    total += cost
            hr()
            print(f"  {len(args.question)} question(s), {failed} failed, est. total cost ≈ ${total:.3f}")
            return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

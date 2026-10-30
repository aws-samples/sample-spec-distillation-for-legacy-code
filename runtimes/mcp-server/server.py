#!/usr/bin/env python3
"""
bedrock-distill MCP Server — Entry point.

A local MCP server providing high-throughput Amazon Bedrock API access
for the map phase of Specification Distillation.

Started via MCP configuration (stdio transport). Exposes 4 tools:
- submit_distillation
- check_distillation
- get_distillation_results
- cancel_distillation
"""

import asyncio
import json
import os
import signal
import sys
import time

from mcp.server import FastMCP

import bedrock_client
import jobs
import tools

# ── Environment Configuration ────────────────────────────────────────────────

AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
AWS_DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

# ── Create Shared Clients ────────────────────────────────────────────────────

bedrock_runtime_client = bedrock_client.create_client(AWS_PROFILE, AWS_DEFAULT_REGION)
# Client for the pipeline stage calls (Converse / ConverseStream; see bedrock_client.call_llm)
llm_client = bedrock_client.create_llm_client(AWS_PROFILE, AWS_DEFAULT_REGION)

# ── MCP Server ───────────────────────────────────────────────────────────────

server = FastMCP("bedrock-distill")


# ── Tool Definitions ─────────────────────────────────────────────────────────


@server.tool(
    description=(
        "Submit a distillation job to process pages through a still extraction "
        "prompt via Amazon Bedrock. Returns immediately with a job_id for polling. "
        "Pass pages as either an inline list (pages param) or a file path containing "
        "one page path per line (pages_file param). Use pages_file for large page sets."
    )
)
async def submit_distillation(
    pages: list[str] = [],
    pages_file: str = "",
    still_prompt: str = "",
    still_name: str = "",
    output_dir: str = "",
    mode: str = "realtime",
    concurrency: int = 50,
    max_tokens: int = 8192,
    model_id: str = "us.anthropic.claude-sonnet-4-6",
) -> str:
    """Submit a distillation job to process pages through a still extraction prompt."""
    # If pages_file provided, read page paths from it
    actual_pages = pages
    if pages_file and not actual_pages:
        try:
            with open(pages_file, "r") as f:
                actual_pages = [line.strip() for line in f if line.strip()]
        except Exception as e:
            return json.dumps({"error": True, "message": f"Failed to read pages_file: {e}"})

    # If still_prompt looks like a file path, auto-read it
    actual_prompt = still_prompt
    if actual_prompt and actual_prompt.startswith("/") and actual_prompt.endswith(".md"):
        try:
            with open(actual_prompt, "r") as f:
                actual_prompt = f.read()
        except Exception:
            pass  # Keep as-is if read fails

    result = tools.handle_submit_distillation(
        {
            "pages": actual_pages,
            "still_prompt": actual_prompt,
            "still_name": still_name,
            "output_dir": output_dir,
            "mode": mode,
            "concurrency": concurrency,
            "max_tokens": max_tokens,
            "model_id": model_id,
        },
        bedrock_runtime_client,
        llm_client,
    )
    return json.dumps(result)


@server.tool(
    description=(
        "Submit a MAP job over pages × stills, dispatched by orchestration STRATEGY. Unified "
        "key-based surface: pass still NAMES (not prompt text) — the server resolves each name "
        "to its prompt from the packaged still registry. strategy='still-centric' sweeps each "
        "still over all pages (historical; n=1 is the degenerate case); strategy='page-centric' "
        "holds each page's prompt cache warm across all stills (cost amortization). Both write "
        "the identical {output_base}/{still}/page_NNN/{still}_extract.md layout, so reduce, the "
        "progress dashboard, and downstream are blind to which ran. Pass pages inline or via "
        "pages_file; stills=[] means all extraction stills. strategy omitted => registry "
        "effective-default. Returns job_id (page-centric) or job_ids per still (still-centric) "
        "for polling via check_distillation."
    )
)
async def submit_map(
    pages: list[str] = [],
    pages_file: str = "",
    stills: list[str] = [],
    output_base: str = "",
    strategy: str = "",
    max_tokens: int = 8192,
    model_id: str = "us.anthropic.claude-sonnet-4-6",
    concurrency: int = 50,
) -> str:
    """Submit a strategy-dispatched map job over pages × stills (stills by name)."""
    actual_pages = pages
    if pages_file and not actual_pages:
        try:
            with open(pages_file, "r") as f:
                actual_pages = [line.strip() for line in f if line.strip()]
        except Exception as e:
            return json.dumps({"error": True, "message": f"Failed to read pages_file: {e}"})

    result = tools.handle_submit_map(
        {
            "pages": actual_pages,
            "stills": stills,
            "output_base": output_base,
            "strategy": strategy or None,
            "max_tokens": max_tokens,
            "model_id": model_id,
            "concurrency": concurrency,
        },
        bedrock_runtime_client,
        llm_client,
    )
    return json.dumps(result)


@server.tool(
    description=(
        "Submit an async REDUCE job over a set of stills (key-based, strategy-agnostic). Each "
        "still merges its own partials_base/{still}/page_*/{still}_extract.md into output_base/"
        "{still}/{still}_raw_concat.md — the location still-centric uses, so downstream is blind "
        "to which map strategy ran. Reduce is a first-class async job: returns a job_id polled "
        "via check_distillation (uniform submit→poll contract with submit_map), reduce progress "
        "is observable, and each reconciliation call emits phase=reduce telemetry. stills=[] => "
        "all extraction stills. Idempotent: a still already reduced is skipped."
    )
)
async def submit_reduce(
    partials_base: str,
    stills: list[str] = [],
    output_base: str = "",
    max_tokens: int = 32768,
    model_id: str = "us.anthropic.claude-sonnet-4-6",
) -> str:
    """Submit an async per-still reduce job (stills by name) → poll via check_distillation."""
    result = tools.handle_submit_reduce(
        {
            "partials_base": partials_base,
            "stills": stills,
            "output_base": output_base,
            "max_tokens": max_tokens,
            "model_id": model_id,
        },
        bedrock_runtime_client,
        llm_client,
    )
    return json.dumps(result)


@server.tool(
    description=(
        "Poll the status of a running distillation job. "
        "Returns progress counts and status."
    )
)
async def check_distillation(job_id: str) -> str:
    """Check the status of a running distillation job."""
    result = tools.handle_check_distillation(
        {"job_id": job_id},
    )
    return json.dumps(result)


@server.tool(
    description=(
        "Retrieve summary metrics for a completed distillation job "
        "including token counts, timing, and cost estimate."
    )
)
async def get_distillation_results(job_id: str) -> str:
    """Get summary metrics for a completed distillation job."""
    result = tools.handle_get_distillation_results({"job_id": job_id})
    return json.dumps(result)


@server.tool(
    description=(
        "Cancel a running distillation job. In-flight requests complete; "
        "no new requests are dispatched."
    )
)
async def cancel_distillation(job_id: str) -> str:
    """Cancel a running distillation job."""
    result = tools.handle_cancel_distillation(
        {"job_id": job_id},
    )
    return json.dumps(result)


@server.tool(
    description=(
        "Reduce (merge) all page-level extraction partials into final still output. "
        "Reads all .md files from partials_dir, concatenates them with the reduce prompt, "
        "sends a single Bedrock call, and writes the merged result to output_dir. "
        "Use after a map-phase job completes to produce the final still deliverables."
    )
)
async def reduce_distillation(
    partials_dir: str,
    still_prompt: str,
    still_name: str,
    output_dir: str,
    expected_pages: int = 0,
    concurrency: int = 4,
    max_tokens: int = 32768,
    model_id: str = "us.anthropic.claude-sonnet-4-6",
    output_file: str = "",
    focus_prompt: str = "",
    still_prompt_file: str = "",
) -> str:
    """Merge all extraction partials into final still output via a single Bedrock call."""
    # If still_prompt_file provided, read the prompt from it
    actual_prompt = still_prompt
    if still_prompt_file and not actual_prompt:
        try:
            with open(still_prompt_file, "r") as f:
                actual_prompt = f.read()
        except Exception as e:
            return json.dumps({"error": True, "message": f"Failed to read still_prompt_file: {e}"})
    # If still_prompt looks like a file path, try to read it
    if actual_prompt and actual_prompt.startswith("/") and actual_prompt.endswith(".md"):
        try:
            with open(actual_prompt, "r") as f:
                actual_prompt = f.read()
        except Exception:
            pass  # Keep as-is if read fails

    result = tools.handle_reduce_distillation(
        {
            "partials_dir": partials_dir,
            "still_prompt": actual_prompt,
            "still_name": still_name,
            "output_dir": output_dir,
            "expected_pages": expected_pages if expected_pages > 0 else None,
            "concurrency": concurrency,
            "max_tokens": max_tokens,
            "model_id": model_id,
            "output_file": output_file,
            "focus_prompt": focus_prompt,
        },
        bedrock_runtime_client,
        llm_client,
    )
    return json.dumps(result)


# ── Signal Handling ──────────────────────────────────────────────────────────


@server.tool(
    description=(
        "Build knowledge graph from a raw concat file. "
        "IMPORTANT: This tool is invoked ONLY by the Phase 2.5 orchestrator (run_distillation.sh). "
        "It must NEVER be called by distill-worker agents during Phase 2 map/reduce. "
        "Parses concept blocks, embeds with all-mpnet-base-v2 (768-dim, local), "
        "stores in NetworkX graph + SQLite vector index, performs KNN dedup, "
        "and materializes per-manifest spec files. "
        "Against a COMPLETED run, pass run_dir=<DISTILL_OUTPUT> alone to (re)export the graph "
        "artifacts to <run_dir>/knowledge_graph/ (concept graph, code-dependency graph, "
        "concept<->code bridge; no LLM calls) and get their index; add still_name to rebuild that "
        "still's graph from <run_dir>/stills/<still>/<still>_raw_concat.md first."
    )
)
async def build_knowledge_graph(
    concat_path: str = "",
    still_name: str = "",
    output_dir: str = "",
    manifest_path: str = "",
    graph_path: str = "",
    vector_db_path: str = "",
    dedup_threshold: float = 0.85,
    run_dir: str = "",
) -> str:
    """Build knowledge graph and materialize spec files from raw concat."""
    import knowledge

    if run_dir:
        import export_graph
        stills = os.path.join(run_dir, "stills")
        if not os.path.isdir(stills):
            return json.dumps({"error": f"not a distillation output dir (no stills/): {run_dir}"})
        result = {}
        if still_name:
            concat_path = concat_path or os.path.join(stills, still_name, f"{still_name}_raw_concat.md")
            if not os.path.isfile(concat_path):
                return json.dumps({"error": f"raw concat not found: {concat_path}"})
            result["still"] = knowledge.process_still(
                concat_path=concat_path,
                still_name=still_name,
                manifest_path=manifest_path,
                output_dir=output_dir or os.path.join(stills, still_name, "final"),
                graph_path=graph_path or os.path.join(stills, "knowledge_graph.json"),
                vector_db_path=vector_db_path or os.path.join(stills, "shared_vectors.db"),
                dedup_threshold=dedup_threshold,
            )
        result["graph_artifacts"] = export_graph.export(run_dir)
        return json.dumps(result, default=str)

    if not (concat_path and still_name and output_dir):
        return json.dumps({"error": "pass run_dir, or concat_path + still_name + output_dir"})
    result = knowledge.process_still(
        concat_path=concat_path,
        still_name=still_name,
        manifest_path=manifest_path,
        output_dir=output_dir,
        graph_path=graph_path,
        vector_db_path=vector_db_path,
        dedup_threshold=dedup_threshold,
    )
    return json.dumps(result)


@server.tool(
    description=(
        "Query the distilled specification using natural language. "
        "IMPORTANT: This tool is for interactive use by the viewer server only, "
        "never during pipeline distillation phases. "
        "Embeds the question, retrieves relevant concept blocks via vector similarity, "
        "then generates a grounded answer with citations. "
        "Requires a vector store (shared_vectors.db) from a completed distillation run: pass "
        "vector_db_path, or run_dir=<DISTILL_OUTPUT> (uses <run_dir>/stills/shared_vectors.db)."
    )
)
async def query_spec(
    question: str,
    vector_db_path: str = "",
    top_k: int = 15,
    run_dir: str = "",
) -> str:
    """Ask a question about the distilled specification."""
    import query_spec as qs

    if not vector_db_path and run_dir:
        vector_db_path = os.path.join(run_dir, "stills", "shared_vectors.db")
    if not vector_db_path:
        return json.dumps({"error": "pass vector_db_path or run_dir"})

    result = qs.query_spec(
        question=question,
        vector_db_path=vector_db_path,
        top_k=top_k,
        profile=AWS_PROFILE,
        region=AWS_DEFAULT_REGION,
        model_id=BEDROCK_MODEL_ID,
    )
    return json.dumps(result)


@server.tool(
    description=(
        "Vend a UNIQUE, stable concept ID for a category (e.g. 'RL', 'DT'). "
        "This is the single authoritative source of concept IDs — never mint IDs "
        "any other way. Uniqueness and locking are owned by the vending machine. "
        "Vending is IDEMPOTENT on claim_key: pass a STABLE key for a concept "
        "(recommended: a hash of still+title+statement) and re-requests return the "
        "SAME id (reused=true), so IDs never churn across runs. Every call is "
        "audit-logged with caller/phase/evidence. Returns {id, category, seq, "
        "reused, claim_key}."
    )
)
async def vend_id(
    ids_db_path: str,
    category: str,
    claim_key: str,
    caller: str = "",
    phase: str = "",
    evidence: str = "",
    call_site: str = "",
) -> str:
    """Assign (or return the existing) unique ID for (category, claim_key)."""
    import id_vending_machine as ivm
    try:
        vm = ivm.get_vending_machine(ids_db_path)
        result = vm.claim(category, claim_key, caller=caller, phase=phase,
                          evidence=evidence, call_site=call_site)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": True, "message": str(e)})


# ── Signal Handling (original) ───────────────────────────────────────────────


def _shutdown_handler(signum, frame):
    """Gracefully cancel all running jobs and exit."""
    # Iterate all running jobs and cancel them
    for job_id, job in list(jobs._registry.items()):
        if job.status == "running":
            jobs.cancel_job(job)

    # Wait up to 30 seconds for in-flight work to complete
    deadline = time.time() + 30
    while time.time() < deadline:
        any_in_flight = any(
            j.pages_in_flight > 0
            for j in jobs._registry.values()
            if j.status in ("running", "cancelled")
        )
        if not any_in_flight:
            break
        time.sleep(0.5)

    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT, _shutdown_handler)


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    server.run(transport="stdio")

"""
tools.py — MCP tool handler functions for bedrock-distill.

Each handler validates input, delegates to jobs.py, and returns metadata-only responses.
NEVER includes extraction content in responses.
"""

import time
from pathlib import Path

import bedrock_client
import jobs
import orchestration_strategy
import still_registry


# ── Error Helpers ────────────────────────────────────────────────────────────


def _error(message: str) -> dict:
    """Return a standardized error response, truncating message to 500 chars."""
    return {"error": True, "message": str(message)[:500]}


# ── Tool Handlers ────────────────────────────────────────────────────────────


def handle_submit_distillation(
    params: dict,
    bedrock_runtime_client,
    llm_client=None,
) -> dict:
    """Submit a new distillation job.

    Validates input, creates a job via jobs.create_job, and starts realtime
    execution. (Bedrock batch inference was culled — SYSTEM_SPEC §12; realtime
    is the only execution economy.)

    Args:
        params: Dict with keys: pages, still_prompt, still_name, mode, concurrency,
                max_tokens, model_id, output_dir.
        bedrock_runtime_client: boto3 bedrock-runtime client for realtime calls.
        llm_client: Optional bedrock-runtime client for call_llm.

    Returns:
        Metadata dict with job_id, still_name, pages_submitted, mode, status.
    """
    # ── Validate pages ───────────────────────────────────────────────────
    pages = params.get("pages")
    if not pages or not isinstance(pages, list) or len(pages) == 0:
        return _error("'pages' must be a non-empty list of file paths")

    for p in pages:
        if not Path(p).exists():
            return _error(f"Page path does not exist: {p}")

    # ── Validate still_prompt ────────────────────────────────────────────
    still_prompt = params.get("still_prompt", "")
    if not still_prompt or not str(still_prompt).strip():
        return _error("'still_prompt' must be a non-empty string")
    still_prompt = str(still_prompt).strip()

    # ── Validate still_name ──────────────────────────────────────────────
    still_name = params.get("still_name", "")
    if not still_name or not str(still_name).strip():
        return _error("'still_name' must be a non-empty string")
    still_name = str(still_name).strip()

    # ── Validate mode ────────────────────────────────────────────────────
    # Realtime is the only supported economy. Bedrock batch inference was culled
    # (SYSTEM_SPEC §12); "batch" is rejected with a pointer, not silently accepted.
    mode = params.get("mode", "realtime")
    if mode == "batch":
        return _error(
            "'batch' mode was removed (Bedrock batch inference culled — see "
            "SYSTEM_SPEC §12). Use 'realtime'."
        )
    if mode != "realtime":
        return _error("'mode' must be 'realtime'")

    # ── Validate concurrency ─────────────────────────────────────────────
    concurrency = params.get("concurrency", 50)
    try:
        concurrency = int(concurrency)
    except (TypeError, ValueError):
        return _error("'concurrency' must be a positive integer")
    if concurrency < 0 or concurrency > 200:
        return _error("'concurrency' must be 0 (auto) or a positive integer up to 200")

    # ── Defaults ─────────────────────────────────────────────────────────
    max_tokens = params.get("max_tokens", 8192)
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        return _error("'max_tokens' must be a positive integer")

    model_id = params.get("model_id", "us.anthropic.claude-sonnet-4-6")
    output_dir = params.get("output_dir", "")
    if not output_dir:
        return _error("'output_dir' must be specified")

    # ── Create job ───────────────────────────────────────────────────────
    job = jobs.create_job(
        pages=pages,
        still_prompt=still_prompt,
        still_name=still_name,
        output_dir=output_dir,
        mode=mode,
        concurrency=concurrency,
        max_tokens=max_tokens,
        model_id=model_id,
    )

    # ── If all pages already complete (resume case), else start realtime ─
    if job.pages_complete == job.pages_total:
        job.status = "complete"
        job.end_time = time.time()
    else:
        jobs.start_realtime(job, bedrock_runtime_client, llm_client)

    return {
        "job_id": job.job_id,
        "still_name": job.still_name,
        "pages_submitted": len(job.pending_pages),
        "mode": job.mode,
        "status": job.status,
    }


def handle_submit_map(
    params: dict,
    bedrock_runtime_client,
    llm_client=None,
) -> dict:
    """Submit a MAP job over pages × stills, dispatched by orchestration STRATEGY.

    The unified, key-based map surface (door-3): the caller passes still NAMES (keys), never
    prompt text — the server resolves name→prompt via `still_registry` (the stills are engine
    source packaged with the server). `strategy` selects the loop shape via the
    `orchestration_strategy` registry:

      still-centric — each still swept over all pages independently (the historical behavior;
                      degenerate n=1 case of the same list — one Job per still here).
      page-centric  — one page held warm and swept by every still (page cached as prefix).

    Both strategies write the IDENTICAL per-still layout `{output_base}/{still}/{page}/{still}_
    extract.md`, so reduce, the progress dashboard, and downstream are blind to which ran.

    Args:
        params: {
            pages: [paths] (or resolved upstream), stills: [names] (empty => all extraction
            stills), output_base, strategy?, model_id?, max_tokens?
        }
        bedrock_runtime_client: boto3 bedrock-runtime client.
        llm_client: optional bedrock-runtime client for call_llm.

    Returns:
        Metadata dict: job_id(s), orchestration_strategy, pages/stills counts, status.
    """
    pages = params.get("pages")
    if not pages or not isinstance(pages, list) or len(pages) == 0:
        return _error("'pages' must be a non-empty list of file paths")
    for p in pages:
        if not Path(p).exists():
            return _error(f"Page path does not exist: {p}")

    output_base = params.get("output_base") or params.get("output_dir", "")
    if not output_base:
        return _error("'output_base' must be specified")

    model_id = params.get("model_id", "us.anthropic.claude-sonnet-4-6")
    try:
        max_tokens = int(params.get("max_tokens", 8192))
    except (TypeError, ValueError):
        return _error("'max_tokens' must be a positive integer")

    # ── Resolve strategy via the registry (never default to vapor) ───────
    try:
        strategy = orchestration_strategy.resolve(
            manifest_recorded=None, passin=params.get("strategy"))
    except ValueError as e:
        return _error(str(e))

    # ── Resolve stills by NAME (server owns name→prompt); [] => all ──────
    names = params.get("stills") or []
    if names and not isinstance(names, list):
        return _error("'stills' must be a list of still names")
    try:
        stills = still_registry.resolve_stills(names)
    except (FileNotFoundError, ValueError) as e:
        return _error(f"still resolution failed: {e}")
    if not stills:
        return _error("no stills resolved (empty registry and none specified)")

    if strategy == "page-centric":
        # One page-major Job sweeps all stills; per-still extracts land under each still's dir.
        job = jobs.create_job(
            pages=pages, still_prompt=stills[0][1], still_name=stills[0][0],
            output_dir=output_base, mode="realtime", concurrency=1,
            max_tokens=max_tokens, model_id=model_id,
        )
        job.orchestration_strategy = "page-centric"
        job.stills = stills
        job.facts_dir = str(params.get("facts_dir") or "")
        job.identifiers_dir = str(params.get("identifiers_dir") or "")
        job.pending_pages = list(enumerate(pages))
        job.pages_complete = 0
        jobs.start_pagecentric(job, bedrock_runtime_client, llm_client)
        return {
            "job_id": job.job_id,
            "orchestration_strategy": "page-centric",
            "pages_submitted": len(pages),
            "stills": [n for n, _ in stills],
            "status": job.status,
        }

    # still-centric: one Job per still (n=1 is just the single-element case). Each still's
    # output_dir is its own dir so the layout matches page-centric exactly.
    job_ids = {}
    for name, prompt in stills:
        job = jobs.create_job(
            pages=pages, still_prompt=prompt, still_name=name,
            output_dir=str(Path(output_base) / name), mode="realtime",
            concurrency=int(params.get("concurrency", 50)),
            max_tokens=max_tokens, model_id=model_id,
        )
        job.orchestration_strategy = "still-centric"
        if job.pages_complete == job.pages_total:
            job.status = "complete"
            job.end_time = time.time()
        else:
            jobs.start_realtime(job, bedrock_runtime_client, llm_client)
        job_ids[name] = job.job_id
    return {
        "job_ids": job_ids,
        "orchestration_strategy": "still-centric",
        "pages_submitted": len(pages),
        "stills": [n for n, _ in stills],
        "status": "running",
    }


def handle_submit_reduce(
    params: dict,
    bedrock_runtime_client,
    llm_client=None,
) -> dict:
    """Submit an async REDUCE job over a set of stills (key-based; strategy-agnostic).

    Reduce is still-major regardless of which map strategy produced the partials — each still
    merges its own `{partials_base}/{still}/page_*/{still}_extract.md` into `{output_base}/
    {still}/{still}_raw_concat.md`. Returns a job_id polled via check_distillation, symmetric
    with submit_map (uniform submit→poll contract; reduce progress + phase=reduce telemetry are
    observable). Idempotent: a still already reduced is skipped.

    Args:
        params: {stills: [names] (empty => all extraction stills), partials_base, output_base,
                 model_id?, max_tokens?}
    """
    partials_base = params.get("partials_base", "")
    if not partials_base or not Path(partials_base).is_dir():
        return _error(f"'partials_base' must be an existing directory: {partials_base}")
    output_base = params.get("output_base", "")
    if not output_base:
        return _error("'output_base' must be specified")

    model_id = params.get("model_id", "us.anthropic.claude-sonnet-4-6")
    try:
        max_tokens = int(params.get("max_tokens", 32768))
    except (TypeError, ValueError):
        return _error("'max_tokens' must be a positive integer")

    names = params.get("stills") or []
    if names and not isinstance(names, list):
        return _error("'stills' must be a list of still names")
    try:
        stills = still_registry.resolve_stills(names)
    except (FileNotFoundError, ValueError) as e:
        return _error(f"still resolution failed: {e}")
    if not stills:
        return _error("no stills resolved to reduce")

    job = jobs.create_reduce_job(
        stills=stills, partials_base=partials_base, output_base=output_base,
        model_id=model_id, max_tokens=max_tokens,
    )
    if job.pages_complete == job.pages_total:
        job.status = "complete"
        job.end_time = time.time()
    else:
        jobs.start_reduce(job, bedrock_runtime_client, llm_client)
    return {
        "job_id": job.job_id,
        "mode": "reduce",
        "stills": [n for n, _ in stills],
        "stills_total": job.pages_total,
        "stills_already_done": job.pages_complete,
        "status": job.status,
    }


def handle_check_distillation(
    params: dict,
) -> dict:
    """Check the status of an existing distillation job.

    Args:
        params: Dict with key: job_id.

    Returns:
        Metadata dict with job progress and status information.
    """
    # ── Validate job_id ──────────────────────────────────────────────────
    job_id = params.get("job_id", "")
    if not job_id:
        return _error("'job_id' is required")

    job = jobs.get_job(job_id)
    if job is None:
        return _error(f"Job not found: {job_id}")

    # ── Compute elapsed time ─────────────────────────────────────────────
    if job.end_time:
        elapsed_s = round(job.end_time - job.start_time, 2)
    else:
        elapsed_s = round(time.time() - job.start_time, 2)

    # ── Truncate errors: last 10, each message to 500 chars ──────────────
    truncated_errors = []
    for err in job.errors[-10:]:
        truncated_err = {
            "page_idx": err.get("page_idx"),
            "error_type": err.get("error_type", ""),
            "message": str(err.get("message", ""))[:500],
        }
        truncated_errors.append(truncated_err)

    return {
        "job_id": job.job_id,
        "still_name": job.still_name,
        "status": job.status,
        "mode": job.mode,
        "pages_total": job.pages_total,
        "pages_complete": job.pages_complete,
        "pages_failed": job.pages_failed,
        "pages_in_flight": job.pages_in_flight,
        "elapsed_s": elapsed_s,
        "output_dir": job.output_dir,
        "agent_instruction": "WAIT. Do not diagnose, script, or adjust concurrency. Sleep 30, poll again.",
    }


def handle_get_distillation_results(params: dict) -> dict:
    """Get final results and metrics for a completed distillation job.

    Only works on jobs with status == "complete".

    Args:
        params: Dict with key: job_id.

    Returns:
        Metadata dict with job metrics including token counts, timing, and cost.
    """
    # ── Validate job_id ──────────────────────────────────────────────────
    job_id = params.get("job_id", "")
    if not job_id:
        return _error("'job_id' is required")

    job = jobs.get_job(job_id)
    if job is None:
        return _error(f"Job not found: {job_id}")

    if job.status != "complete":
        return _error(
            f"Job is not complete (current status: '{job.status}'). "
            "Results are only available for completed jobs."
        )

    # ── Count actual .md files written ───────────────────────────────────
    files_written = 0
    output_path = Path(job.output_dir)
    if output_path.exists():
        for page_dir in output_path.iterdir():
            if page_dir.is_dir() and page_dir.name.startswith("page_"):
                for f in page_dir.iterdir():
                    if f.suffix == ".md":
                        files_written += 1

    # ── Compute timing metrics ───────────────────────────────────────────
    wall_time_s = 0.0
    if job.end_time and job.start_time:
        wall_time_s = round(job.end_time - job.start_time, 2)

    pages_per_minute = 0.0
    tokens_per_minute = 0.0
    if wall_time_s > 0:
        minutes = wall_time_s / 60.0
        pages_per_minute = round(job.pages_complete / minutes, 2)
        total_tokens = job.total_input_tokens + job.total_output_tokens
        tokens_per_minute = round(total_tokens / minutes, 2)

    # Cost is deliberately NOT emitted. Token usage is a fact this system owns and reports;
    # a dollar figure depends on an extrinsic, time-varying vendor rate this system has no
    # authority to assert. Consumers who hold a live rate compute cost from the token counts.

    return {
        "job_id": job.job_id,
        "still_name": job.still_name,
        "status": job.status,
        "output_dir": job.output_dir,
        "files_written": files_written,
        "total_input_tokens": job.total_input_tokens,
        "total_output_tokens": job.total_output_tokens,
        "wall_time_s": wall_time_s,
        "pages_per_minute": pages_per_minute,
        "tokens_per_minute": tokens_per_minute,
    }


def handle_cancel_distillation(
    params: dict,
) -> dict:
    """Cancel a running distillation job.

    Args:
        params: Dict with key: job_id.

    Returns:
        Metadata dict with cancellation details.
    """
    # ── Validate job_id ──────────────────────────────────────────────────
    job_id = params.get("job_id", "")
    if not job_id:
        return _error("'job_id' is required")

    job = jobs.get_job(job_id)
    if job is None:
        return _error(f"Job not found: {job_id}")

    # ── If not running, return current status without action ─────────────
    if job.status != "running":
        return {
            "job_id": job.job_id,
            "still_name": job.still_name,
            "status": job.status,
            "pages_complete": job.pages_complete,
            "pages_cancelled": 0,
            "output_dir": job.output_dir,
        }

    # ── Cancel the job ───────────────────────────────────────────────────
    jobs.cancel_job(job)

    pages_cancelled = job.pages_total - job.pages_complete - job.pages_failed

    return {
        "job_id": job.job_id,
        "still_name": job.still_name,
        "status": "cancelled",
        "pages_complete": job.pages_complete,
        "pages_cancelled": pages_cancelled,
        "output_dir": job.output_dir,
    }


# ── Reduce Handler ───────────────────────────────────────────────────────────


def handle_reduce_distillation(
    params: dict,
    bedrock_runtime_client,
    llm_client=None,
) -> dict:
    """Reduce via Concat + Reconciliation (zero signal loss).

    Phase 1: Deterministic concat of all page partials (no LLM, no loss)
    Phase 2: Reconciliation pass producing a dedup graph overlay

    The raw concat IS the complete output. The reconciliation adds structure.
    Nothing is ever removed or compressed.

    Args:
        params: Dict with keys: partials_dir, still_prompt, still_name, output_dir,
                max_tokens, model_id, concurrency, expected_pages.
        bedrock_runtime_client: boto3 bedrock-runtime client (used as fallback;
                               prefers llm_client from server module if available).

    Returns:
        Metadata dict with reduce results.
    """
    import re as _re

    # ── Validate ─────────────────────────────────────────────────────────
    partials_dir = params.get("partials_dir", "")
    if not partials_dir or not Path(partials_dir).is_dir():
        return _error(f"'partials_dir' must be an existing directory: {partials_dir}")

    still_name = params.get("still_name", "")
    if not still_name:
        return _error("'still_name' must be a non-empty string")

    output_dir = params.get("output_dir", "")
    if not output_dir:
        return _error("'output_dir' must be specified")

    max_tokens = int(params.get("max_tokens", 8192))
    model_id = params.get("model_id", "us.anthropic.claude-sonnet-4-6")
    concurrency = int(params.get("concurrency", 4))

    # ── Find all extract files ───────────────────────────────────────────
    partials_path = Path(partials_dir)
    extract_files = sorted(
        partials_path.glob(f"page_*/{still_name}_extract.md"),
        key=lambda p: p.parent.name
    )

    if not extract_files:
        return _error(f"No {still_name}_extract.md files found in {partials_dir}")

    # ── Completeness check ───────────────────────────────────────────────
    expected_pages = params.get("expected_pages")
    if expected_pages is not None:
        expected_pages = int(expected_pages)
        if len(extract_files) < expected_pages:
            return _error(
                f"INCOMPLETE: Only {len(extract_files)}/{expected_pages} pages have results. "
                f"Map phase must complete 100% before reduce. "
                f"Re-submit with same output_dir to process remaining {expected_pages - len(extract_files)} pages."
            )

    # ── Phase 1: Deterministic Concat (no LLM, zero loss) ────────────────
    start_time = time.time()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    concat_path = output_path / f"{still_name}_raw_concat.md"
    total_bytes = 0

    with open(concat_path, "w", encoding="utf-8") as out:
        out.write(f"# {still_name} — Raw Extraction Concat\n\n")
        out.write(f"Pages: {len(extract_files)}\n")
        out.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n\n")
        out.write("---\n\n")

        for ef in extract_files:
            page_name = ef.parent.name
            content = ef.read_text(encoding="utf-8")
            total_bytes += len(content.encode("utf-8"))
            out.write(f"<!-- PAGE: {page_name} -->\n")
            out.write(content)
            out.write(f"\n<!-- END PAGE: {page_name} -->\n\n")

    concat_elapsed = time.time() - start_time

    # ── Phase 2: Reconciliation (dedup graph overlay) ────────────────────
    reconcile_start = time.time()

    full_text = concat_path.read_text(encoding="utf-8")
    CHUNK_SIZE = 400000  # ~133K tokens per chunk
    chunks = [full_text[i:i + CHUNK_SIZE] for i in range(0, len(full_text), CHUNK_SIZE)]

    reconcile_prompt = f"""You are producing a DEDUP INDEX for specification distillation ({still_name} still).

Read the raw extraction results below. Your job:
1. Identify entities/concepts/rules that appear MULTIPLE TIMES across different pages
2. For each duplicate group, produce: canonical name, type, aliases, pages where seen, confidence
3. Count unique findings (appear only once)

OUTPUT FORMAT (JSON only, no markdown fences, no commentary):
{{
  "duplicates": [
    {{
      "canonical": "Example Entity",
      "type": "entity|rule|invariant|obligation|boundary|workflow|state",
      "aliases": ["alias1", "alias2"],
      "pages": ["page_003", "page_057"],
      "confidence": 5,
      "summary": "One-sentence description"
    }}
  ],
  "unique_count": 142,
  "total_findings": 213,
  "duplicate_groups": 47
}}

RULES:
- Output ONLY valid JSON. No markdown fences. No commentary before or after.
- Every finding must be accounted for (either in a duplicate group or counted as unique)
- confidence = min(5, number of pages where concept appears)
"""

    # Use call_llm if a client is passed in (preferred), else the self-retrying Converse call
    _llm = llm_client

    chunk_results = []
    total_input_tokens = 0
    total_output_tokens = 0

    for idx, chunk in enumerate(chunks):
        if _llm:
            result = bedrock_client.call_llm(
                _llm, model_id,
                reconcile_prompt,
                f"Chunk {idx+1}/{len(chunks)}:\n\n{chunk}",
                max_tokens,
            )
        else:
            result = bedrock_client.call_converse_with_retry(
                bedrock_runtime_client, model_id,
                reconcile_prompt,
                f"Chunk {idx+1}/{len(chunks)}:\n\n{chunk}",
                max_tokens,
            )
        total_input_tokens += result.input_tokens
        total_output_tokens += result.output_tokens
        chunk_results.append(result.output_text)

    # Parse and merge chunk results
    import json as _json
    merged_duplicates = []
    total_unique = 0
    total_findings = 0

    for cr in chunk_results:
        # Strip markdown fences if present
        cleaned = _re.sub(r'^```(?:json)?\s*', '', cr.strip())
        cleaned = _re.sub(r'```\s*$', '', cleaned)
        try:
            parsed = _json.loads(cleaned)
            merged_duplicates.extend(parsed.get("duplicates", []))
            total_unique += parsed.get("unique_count", 0)
            total_findings += parsed.get("total_findings", 0)
        except _json.JSONDecodeError:
            # Store raw if can't parse
            merged_duplicates.append({"raw_chunk": cr[:2000], "parse_error": True})

    # Write dedup index
    dedup_path = output_path / f"{still_name}_dedup_index.json"
    dedup_data = {
        "still_name": still_name,
        "pages_processed": len(extract_files),
        "reconciliation_chunks": len(chunks),
        "total_findings": total_findings,
        "unique_findings": total_unique,
        "duplicate_groups": len([d for d in merged_duplicates if not d.get("parse_error")]),
        "duplicates": merged_duplicates,
    }
    dedup_path.write_text(_json.dumps(dedup_data, indent=2), encoding="utf-8")

    # Write README
    readme_path = output_path / "README.md"
    readme_path.write_text(f"""# {still_name} Still — Reduced Output (Concat + Reconciliation)

## Files

| File | Purpose |
|------|---------|
| `{still_name}_raw_concat.md` | Complete extraction — every finding from every page. Zero loss. |
| `{still_name}_dedup_index.json` | Reconciliation overlay: duplicate groups, confidence, cross-references. |
| `README.md` | This file. |

## Stats

- Pages: {len(extract_files)}
- Raw concat: {total_bytes/1024:.0f} KB
- Duplicate groups: {len([d for d in merged_duplicates if not d.get("parse_error")])}
- Reconciliation chunks: {len(chunks)}
- Reconciliation time: {time.time() - reconcile_start:.0f}s
- Total time: {time.time() - start_time:.0f}s
""", encoding="utf-8")

    elapsed = time.time() - start_time

    return {
        "status": "complete",
        "still_name": still_name,
        "method": "concat_reconcile",
        "partials_read": len(extract_files),
        "partials_bytes": total_bytes,
        "concat_file": str(concat_path),
        "dedup_index_file": str(dedup_path),
        "reconciliation_chunks": len(chunks),
        "duplicate_groups": len([d for d in merged_duplicates if not d.get("parse_error")]),
        "output_dir": str(output_path),
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "elapsed_s": round(elapsed, 2),
        "model_id": model_id,
    }


## (end of tools.py)

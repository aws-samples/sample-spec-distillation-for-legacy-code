"""
jobs.py — Manages job lifecycle, orchestrates execution threads, and tracks progress.

Provides:
- Job dataclass with thread-safe counters
- Job registry for lookup by job_id
- Resume scanning to skip already-completed pages
- Realtime execution via ThreadPoolExecutor
"""

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import bedrock_client
import orchestration_strategy


# ── Data Model ───────────────────────────────────────────────────────────────


@dataclass
class Job:
    """Represents a distillation job with thread-safe progress tracking."""

    job_id: str
    still_name: str
    mode: str  # "realtime" (the only execution economy; Bedrock batch culled — SYSTEM_SPEC §12)
    status: str  # "running" | "complete" | "failed" | "cancelled"
    pages: list[str]  # input file paths (full list)
    pending_pages: list[tuple[int, str]]  # (page_idx, path) for pages still needing processing
    pages_total: int
    pages_complete: int
    pages_failed: int
    pages_in_flight: int
    output_dir: str
    still_prompt: str
    model_id: str
    max_tokens: int
    concurrency: int
    start_time: float
    end_time: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    errors: list[dict] = field(default_factory=list)
    # ── Orchestration strategy (X-map-reduce-extraction-9) ────────────────────
    # "still-centric" (default-preserving: one still × pages, the existing loop) or
    # "page-centric" (page-major: one page × many stills, page cached as prefix).
    orchestration_strategy: str = "still-centric"
    # page-centric only: the set of stills to sweep over each page. Each entry
    # (still_name, still_prompt). None/empty for still-centric (uses still_name/still_prompt).
    stills: list[tuple[str, str]] | None = None
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    # ── Reduce jobs (submit_reduce, X-map-reduce-extraction async reduce) ─────
    # A reduce Job reuses the pages_* progress vocabulary so check_distillation and the
    # progress dashboard need NO new shape: pages_total = number of stills to reduce,
    # pages_complete = stills reduced, pages_failed = stills that errored. `partials_base`
    # is the dir under which per-still `{still}/page_*/{still}_extract.md` partials live
    # (equals output_dir in the unified layout); output_dir is where `{still}/{still}_
    # raw_concat.md` is written. `stills` carries the (name, prompt) set being reduced.
    partials_base: str = ""
    # ── Structural facts (Phase 1.2, runtimes/facts) ──────────────────────────
    # Dir holding one pre-rendered, bounded facts block per page (`page_NNN.md`). When set, the
    # page-centric map appends the page's block to the page text (inside the cached prefix).
    # Empty = no facts (the pipeline continues without them).
    facts_dir: str = ""
    # Phase 1.3 identifier evidence (runtimes/facts/identifier_evidence.py): one bounded block per
    # page (`page_NNN.md`) of abbreviated names with the corpus evidence of their meaning,
    # appended after the facts block. Empty = none.
    identifiers_dir: str = ""


# ── Module-level Job Registry ────────────────────────────────────────────────

_registry: dict[str, Job] = {}


# ── Shared page-identity helper ──────────────────────────────────────────────

def _page_dir_name(page_path: str, page_idx: int) -> str:
    """Derive the page output directory name from the page path (System L6). A page whose
    file is `.../page_042/concat.txt` yields `page_042`; otherwise `page_{idx:03d}`. Shared by
    the still-centric and page-centric executors + resume_scan so the durable layout is
    identical regardless of orchestration strategy (X-map-reduce-extraction-3)."""
    p = Path(page_path)
    if p.name == "concat.txt":
        return p.parent.name
    return f"page_{page_idx:03d}"


FACTS_SEPARATOR = "\n\n[STRUCTURAL FACTS]\n"
IDENTIFIERS_SEPARATOR = "\n\n[IDENTIFIER EVIDENCE]\n"


def _page_block(block_dir: str, page_dir_name: str) -> str:
    if not block_dir:
        return ""
    try:
        return (Path(block_dir) / f"{page_dir_name}.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def page_with_facts(content: str, page_dir_name: str, facts_dir: str,
                    identifiers_dir: str = "") -> str:
    """The page text plus its Phase 1.2 structural-facts block and its Phase 1.3 identifier-
    evidence block, when they exist. Both are rendered and bounded upstream
    (runtimes/facts/build_facts.py, runtimes/facts/identifier_evidence.py) and their bytes are
    hashed into the run identity (facts_hash, identifiers_hash), so appending them here keeps
    resume decisions honest."""
    block = _page_block(facts_dir, page_dir_name)
    if block:
        content = content + FACTS_SEPARATOR + block + "\n"
    ident = _page_block(identifiers_dir, page_dir_name)
    if ident:
        content = content + IDENTIFIERS_SEPARATOR + ident + "\n"
    return content


# ── Public Functions ─────────────────────────────────────────────────────────


def create_job(
    pages: list[str],
    still_prompt: str,
    still_name: str,
    output_dir: str,
    mode: str,
    concurrency: int,
    max_tokens: int,
    model_id: str,
) -> Job:
    """Create a Job, scan output_dir for existing results (resume), and store in registry.

    Args:
        pages: List of input file paths to process.
        still_prompt: System prompt for the still extraction.
        still_name: Name of the still (used in output filenames).
        output_dir: Directory for writing results.
        mode: "realtime" (the only supported execution economy).
        concurrency: Number of concurrent workers.
        max_tokens: Max tokens for model responses.
        model_id: Bedrock model identifier.

    Returns:
        The newly created Job instance.
    """
    job_id = str(uuid.uuid4())

    # Scan for already-completed pages (resume support)
    completed_indices = resume_scan(output_dir, pages, still_name)

    # Build pending_pages: only pages not yet complete
    pending_pages = [
        (idx, path) for idx, path in enumerate(pages) if idx not in completed_indices
    ]

    job = Job(
        job_id=job_id,
        still_name=still_name,
        mode=mode,
        status="running",
        pages=pages,
        pending_pages=pending_pages,
        pages_total=len(pages),
        pages_complete=len(completed_indices),
        pages_failed=0,
        pages_in_flight=0,
        output_dir=output_dir,
        still_prompt=still_prompt,
        model_id=model_id,
        max_tokens=max_tokens,
        concurrency=concurrency,
        start_time=time.time(),
    )

    _registry[job_id] = job
    return job


def resume_scan(output_dir: str, pages: list[str], still_name: str) -> set[int]:
    """Scan output_dir for existing results to support job resumption.

    Derives expected output directory names from page paths:
    - If page path is .../page_042/concat.txt → checks output_dir/page_042/{still_name}_extract.md
    - Otherwise falls back to output_dir/page_{idx:03d}/{still_name}_extract.md

    Returns the set of page list indices that already have completed results.

    Args:
        output_dir: Base output directory.
        pages: Full list of input page paths.
        still_name: Still name used in output filenames.

    Returns:
        Set of list indices (0-based) that already have results on disk.
    """
    completed: set[int] = set()
    output_path = Path(output_dir)

    if not output_path.exists():
        return completed

    for idx, page_path in enumerate(pages):
        # Derive the expected output directory name from the page path
        p = Path(page_path)
        if p.name == "concat.txt":
            page_dir_name = p.parent.name  # e.g. "page_042"
        else:
            page_dir_name = f"page_{idx:03d}"

        result_file = output_path / page_dir_name / f"{still_name}_extract.md"
        if result_file.exists() and result_file.stat().st_size > 0:
            completed.add(idx)

    return completed


def start_realtime(job: Job, bedrock_runtime_client, llm_client=None) -> None:
    """Spawn a daemon thread that runs the realtime executor with ThreadPoolExecutor.

    The background thread fans out pending pages across workers, tracks progress,
    and sets final job status when all pages are processed.

    Args:
        job: The Job to execute.
        bedrock_runtime_client: boto3 bedrock-runtime client for Converse API calls.
        llm_client: Optional bedrock-runtime client for call_llm (preferred if set).
    """
    thread = threading.Thread(
        target=_realtime_executor,
        args=(job, bedrock_runtime_client, llm_client),
        daemon=True,
    )
    thread.start()


def start_pagecentric(job: Job, bedrock_runtime_client, llm_client=None) -> None:
    """Spawn the PAGE-MAJOR executor (orchestration-strategy `page-centric`,
    X-map-reduce-extraction-9): hold one page, sweep every still over it while the page's
    prompt cache is warm, then move to the next page. The page is presented once as a
    cacheable prefix; only the small per-still instruction varies, so the dominant page-input
    term is billed once at write rate and thereafter at read rate.

    Uses the Converse API with a `cachePoint` after the page (1 cache write, N cache reads per
    page), so the page input is billed once at the write rate and thereafter at the read rate.
    Emits the identical `page_NNN/{still}_extract.md` durable layout as still-centric, so
    `reduce` and downstream are blind to which strategy ran (X-map-reduce-extraction-3 /
    R-orchestration-strategy-2).

    Args:
        job: The Job (orchestration_strategy="page-centric", stills=[(name, prompt), ...]).
        bedrock_runtime_client: boto3 bedrock-runtime client (Converse fallback).
        llm_client: bedrock-runtime client for the stage calls (preferred if set).
    """
    thread = threading.Thread(
        target=_pagecentric_executor,
        args=(job, bedrock_runtime_client, llm_client),
        daemon=True,
    )
    thread.start()


def cancel_job(job: Job) -> None:
    """Signal cancellation on a job and update its status.

    Args:
        job: The Job to cancel.
    """
    job.cancel_event.set()
    job.status = "cancelled"
    job.end_time = time.time()


def get_job(job_id: str) -> Job | None:
    """Look up a job by ID from the registry.

    Args:
        job_id: UUID string of the job.

    Returns:
        The Job if found, None otherwise.
    """
    return _registry.get(job_id)


# ── Internal Functions ───────────────────────────────────────────────────────


def _realtime_executor(job: Job, bedrock_runtime_client, llm_client=None) -> None:
    """Background thread with AIMD concurrency windowing and page batching.

    Processes pages in batches (default 100) to avoid overwhelming the API
    and to provide visible progress between batches.

    Uses TCP-style Additive Increase / Multiplicative Decrease within each batch:
    - Start at window_size = 20 (conservative)
    - On success: window = min(window * 1.5, max_concurrency)
    - On throttle/error: window = max(window * 0.5, min_window)
    - After 30s of no errors: probe back up

    Concurrency semantics:
    - concurrency=0: AIMD self-tunes with no upper cap (defaults to 200)
    - concurrency>0: AIMD self-tunes with that value as the max cap

    Args:
        job: The Job being executed.
        bedrock_runtime_client: boto3 bedrock-runtime client.
        llm_client: Optional bedrock-runtime client for call_llm (preferred if set).
    """
    import random
    import os
    from concurrent.futures import Future

    # PAGE-batch size: how many pages are processed per progress-visible chunk on the REALTIME
    # (on-demand) path. This is the generalized distillation throughput abstraction — NOT Bedrock
    # batch inference (that modality was culled; see SYSTEM_SPEC §12). Explicitly named so the two
    # senses of "batch" can never be re-conflated.
    PAGE_BATCH_SIZE = int(os.environ.get("DISTILL_PAGE_BATCH_SIZE", "100"))
    max_window = job.concurrency if job.concurrency > 0 else 200
    min_window = 5
    window = min(20, max_window)  # Start conservative
    last_error_time = 0.0
    probe_interval = 30.0  # Seconds of success before growing again

    # Use a semaphore to control in-flight count dynamically
    semaphore = threading.Semaphore(window)
    window_lock = threading.Lock()

    def adjust_window(success: bool, is_throttle: bool = False):
        nonlocal window, last_error_time
        with window_lock:
            if success:
                # Additive increase: grow if stable for probe_interval
                if time.time() - last_error_time > probe_interval:
                    new_window = min(int(window * 1.5), max_window)
                    if new_window > window:
                        # Release extra semaphore slots
                        for _ in range(new_window - window):
                            semaphore.release()
                        window = new_window
            else:
                # Multiplicative decrease
                last_error_time = time.time()
                new_window = max(int(window * 0.5), min_window)
                if new_window < window:
                    window = new_window

    def process_with_semaphore(page_idx: int, page_path: str) -> dict:
        """Acquire semaphore slot, process page, release, adjust window."""
        semaphore.acquire()
        try:
            result = _process_page(job, page_idx, page_path, bedrock_runtime_client, llm_client)
            is_throttle = result.get("error_type", "") in ("ThrottlingException", "TooManyRequestsException", "OverloadedError")
            adjust_window(result["success"], is_throttle)
            return result
        finally:
            semaphore.release()

    # Process pages in PAGE-batches (progress-visible chunks on the realtime path)
    all_pending = list(job.pending_pages)
    total_page_batches = (len(all_pending) + PAGE_BATCH_SIZE - 1) // PAGE_BATCH_SIZE
    print(f"[{job.still_name}] Processing {len(all_pending)} pages in {total_page_batches} page-batches of {PAGE_BATCH_SIZE}", flush=True)

    for page_batch_num in range(total_page_batches):
        if job.cancel_event.is_set():
            break

        page_batch_start = page_batch_num * PAGE_BATCH_SIZE
        page_batch_end = min(page_batch_start + PAGE_BATCH_SIZE, len(all_pending))
        page_batch = all_pending[page_batch_start:page_batch_end]
        page_batch_t0 = time.time()

        print(f"[{job.still_name}] Page batch {page_batch_num + 1}/{total_page_batches} ({len(page_batch)} pages, window={window})", flush=True)

        # Reset AIMD window at page-batch start to avoid carrying over stale state
        with window_lock:
            window = min(20, max_window)

        with ThreadPoolExecutor(max_workers=max_window) as executor:
            futures: dict[Future, int] = {}
            for page_idx, page_path in page_batch:
                if job.cancel_event.is_set():
                    break
                future = executor.submit(process_with_semaphore, page_idx, page_path)
                futures[future] = page_idx

            for future in as_completed(futures):
                if job.cancel_event.is_set():
                    break
                try:
                    future.result()
                except Exception as e:
                    # _process_page records per-page failures internally; reaching here means an
                    # UNEXPECTED error escaped it. Do not swallow silently — surface it (print +
                    # count + record) so a failed page can never masquerade as a clean run.
                    page_idx = futures.get(future, -1)
                    print(f"[{job.still_name}] UNEXPECTED page-task error "
                          f"(page_idx={page_idx}): {type(e).__name__}: {e}", flush=True)
                    with job._lock:
                        job.pages_failed += 1
                        job.errors.append({"page_idx": page_idx,
                                           "error_type": type(e).__name__,
                                           "message": f"unexpected executor error: {e}"})

        page_batch_elapsed = time.time() - page_batch_t0
        print(f"[{job.still_name}] Page batch {page_batch_num + 1}/{total_page_batches} complete ({page_batch_elapsed:.0f}s, "
              f"total: {job.pages_complete}/{job.pages_total} done, {job.pages_failed} failed)", flush=True)

    # Set final status
    if job.cancel_event.is_set():
        job.status = "cancelled"
    elif job.pages_failed == 0:
        job.status = "complete"
    else:
        job.status = "failed"

    job.end_time = time.time()


def _process_page(
    job: Job, page_idx: int, page_path: str, client, llm_client=None
) -> dict:
    """Process a single page: read, call Bedrock, write result.

    Uses llm_client (call_llm, no internal retry) if provided.
    Falls back to bedrock-runtime client with retry logic.

    Thread-safe counter updates are performed under job._lock.

    Args:
        job: The parent Job.
        page_idx: Zero-based index of this page.
        page_path: Path to the input file on disk.
        client: boto3 bedrock-runtime client (fallback).
        llm_client: bedrock-runtime client for call_llm (preferred if set).

    Returns:
        Dict with success status, page_idx, and optional error info.
    """
    # Check for cancellation before starting
    if job.cancel_event.is_set():
        return {"success": False, "page_idx": page_idx, "error_type": "cancelled", "message": "Job cancelled"}

    # Increment in-flight counter
    with job._lock:
        job.pages_in_flight += 1

    try:
        # Read page content from disk
        content = Path(page_path).read_text(encoding="utf-8")

        # Derive page output directory from the page path (e.g. .../page_042/concat.txt → page_042)
        page_source = Path(page_path)
        if page_source.name == "concat.txt":
            page_dir_name = page_source.parent.name  # e.g. "page_042"
        else:
            page_dir_name = f"page_{page_idx:03d}"

        # Build the map-phase prompt: suppress boilerplate, extract raw findings only
        map_prompt = (
            "MAP PHASE INSTRUCTIONS (override document formatting requirements):\n"
            "- Do NOT include 'How to Read This Document' sections.\n"
            "- Do NOT include scoring guide tables.\n"
            "- Do NOT include term definitions or glossaries.\n"
            "- Do NOT include document headers or introductions.\n"
            "- Output ONLY raw findings: entities, rules, invariants, etc.\n"
            "- Use minimal structure (### headers for each finding, fields inline).\n"
            "- The reduce phase will add formatting, headers, and readability sections later.\n\n"
            + job.still_prompt
        )

        # Use call_llm if a client is given, else the self-retrying Converse call
        if llm_client:
            result = bedrock_client.call_llm(
                llm_client,
                job.model_id,
                map_prompt,
                content,
                job.max_tokens,
            )
        else:
            result = bedrock_client.call_converse_with_retry(
                client,
                job.model_id,
                map_prompt,
                content,
                job.max_tokens,
            )

        # Create output directory and write result
        page_dir = Path(job.output_dir) / page_dir_name
        page_dir.mkdir(parents=True, exist_ok=True)
        output_file = page_dir / f"{job.still_name}_extract.md"
        output_file.write_text(result.output_text, encoding="utf-8")

        # Update counters under lock
        with job._lock:
            job.pages_complete += 1
            job.pages_in_flight -= 1
            job.total_input_tokens += result.input_tokens
            job.total_output_tokens += result.output_tokens

        return {
            "success": True,
            "page_idx": page_idx,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }

    except Exception as e:
        error_type = type(e).__name__
        message = str(e)

        with job._lock:
            job.pages_failed += 1
            job.pages_in_flight -= 1
            job.errors.append({
                "page_idx": page_idx,
                "error_type": error_type,
                "message": message,
            })

        return {
            "success": False,
            "page_idx": page_idx,
            "error_type": error_type,
            "message": message,
        }


# Highest output budget a map retry may use (Sonnet 4.x max output is 64K tokens).
MAP_MAX_TOKENS_CEILING = 64000


def _is_truncated(result) -> bool:
    """True when the model stopped because it ran out of output budget (Anthropic
    "max_tokens", Converse "max_tokens"/"length")."""
    return str(getattr(result, "stop_reason", "") or "").lower() in ("max_tokens", "length")


def _pagecentric_executor(job: Job, client, llm_client=None) -> None:
    """Page-major executor with an IN-CODE congestion controller (R-orchestration-strategy-5,
    R-adaptive-throughput-2). Emits the identical `page_NNN/{still}_extract.md` layout.

    Cache economics (T-9 / R-orchestration-strategy-3): each page's still-sweep runs the stills
    SEQUENTIALLY so the cached page prefix warms on still #1 and is hit by #2..N (firing them in
    parallel would race the cache write). The unit of PARALLELISM is therefore the PAGE — a worker
    owns one page's full sequential still-sweep — never the still-within-a-page. This preserves the
    1-write/N-read cache win while letting the controller scale page-level concurrency.

    Congestion control (rebuilt in code — replaces the deleted agent-in-the-loop that drove a
    large (~10x) corpus by hand, and the sequential-no-backoff regression that hung a later run).
    Per R-adaptive-throughput-2 (Bedrock throttles on tokens-per-minute quotas):
      - process the pending pages in ROUNDS, each round at the current concurrency `window`,
      - detect over-capacity backpressure (Throttling/TooManyRequests/Overloaded/429) per page,
      - on throttle: multiplicative-DECREASE the window and WAIT for the TPM window to recover
        (minutes, not seconds) before the next round,
      - on a clean round: additive/gentle-INCREASE the window,
      - RESUBMIT the pages that failed on throttle in the next round (per-(page,still) resume skips
        already-written extracts, so a page that half-finished only redoes its missing stills),
      - converge when no pending pages remain; give up a page only on a NON-throttle hard error.
    """
    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import budget

    stills = job.stills or [(job.still_name, job.still_prompt)]
    n_stills = len(stills)
    cache_on = orchestration_strategy.cache_on(job.orchestration_strategy) and n_stills >= 2
    tel_dir = os.environ.get("DISTILL_TELEMETRY_DIR", "")
    # Budget guard (DISTILL_MAX_USD): before every map call, actual spend (telemetry) + what the
    # in-flight calls have reserved + one call's reserve must stay within the cap; otherwise the
    # page stops cleanly (its finished extracts are kept for resume) and the job ends
    # "budget_exhausted". None when no cap is set.
    guard = budget.guard_from_env(tel_dir, job.model_id, log=lambda m: print(m, flush=True))

    # Controller tunables (grounded in observed TPM recovery; env-overridable). The reference
    # driver walked concurrency 200→50→10→5→2 and waited ~90s→3-4min→5min for TPM recovery.
    # Defaults here mirror that shape.
    start_window = int(os.environ.get("DISTILL_PC_START_CONCURRENCY", "50"))
    max_window = job.concurrency if job.concurrency > 0 else int(os.environ.get("DISTILL_PC_MAX_CONCURRENCY", "200"))
    min_window = int(os.environ.get("DISTILL_PC_MIN_CONCURRENCY", "2"))
    recovery_wait = float(os.environ.get("DISTILL_PC_RECOVERY_WAIT_S", "120"))   # TPM-window recovery
    max_recovery_wait = float(os.environ.get("DISTILL_PC_MAX_RECOVERY_WAIT_S", "300"))
    window = max(min_window, min(start_window, max_window))
    # The resubmit-worthy set (throttle/overload + StreamIdleTimeout + read/connect timeout) now
    # lives in bedrock_client.is_retryable_bedrock_error and is SHARED with the reduce retry, so
    # map and reduce can't drift on what counts as transient. Behavior here is unchanged: a
    # retryable exception is signalled "throttled" (resubmit) to the AIMD controller below.
    def _is_throttle(exc: Exception) -> bool:
        return bedrock_client.is_retryable_bedrock_error(exc)

    def _sweep_page(page_idx: int, page_path: str) -> dict:
        """Run one page's SEQUENTIAL still-sweep (cache-preserving). Returns
        {page_idx, throttled: bool, hard_error: str|None} — throttled pages are resubmitted."""
        with job._lock:
            job.pages_in_flight += 1
        try:
            return _sweep_page_inner(page_idx, page_path)
        finally:
            with job._lock:
                job.pages_in_flight -= 1

    def _sweep_page_inner(page_idx: int, page_path: str) -> dict:
        try:
            content = Path(page_path).read_text(encoding="utf-8")
        except Exception as e:
            return {"page_idx": page_idx, "throttled": False, "hard_error": f"read failed: {e}"}
        page_dir_name = _page_dir_name(page_path, page_idx)
        content = page_with_facts(content, page_dir_name, job.facts_dir, job.identifiers_dir)
        for still_name, still_prompt in stills:
            if job.cancel_event.is_set():
                return {"page_idx": page_idx, "throttled": False, "hard_error": "cancelled"}
            page_dir = Path(job.output_dir) / still_name / page_dir_name
            page_dir.mkdir(parents=True, exist_ok=True)
            out_file = page_dir / f"{still_name}_extract.md"
            if out_file.exists() and out_file.stat().st_size > 0:
                continue  # per-(page,still) resume
            # A map extraction cut off at max_tokens silently loses everything after the cut, so
            # retry once at the model ceiling; if it is still truncated, fail the page (closed)
            # rather than write a partial extract that resume would then reuse as complete.
            budgets = [job.max_tokens]
            if job.max_tokens < MAP_MAX_TOKENS_CEILING:
                budgets.append(MAP_MAX_TOKENS_CEILING)
            for attempt, out_budget in enumerate(budgets):
                if guard is not None and not guard.try_reserve():
                    return {"page_idx": page_idx, "throttled": False, "hard_error": None,
                            "budget": True}
                t0 = time.time()
                try:
                    try:
                        system_blocks, _ = orchestration_strategy.compose_map_request(
                            content, still_prompt, cache_on=cache_on)
                        result = bedrock_client.call_llm_blocks(
                            llm_client or client, job.model_id, system_blocks, still_prompt,
                            out_budget)
                    except Exception as e:
                        if _is_throttle(e):
                            # Backpressure — NOT a page defect. Signal the controller to back off + resubmit.
                            return {"page_idx": page_idx, "throttled": True, "hard_error": None}
                        return {"page_idx": page_idx, "throttled": False,
                                "hard_error": f"[{still_name}] {type(e).__name__}: {e}"}
                    truncated = _is_truncated(result)
                    bedrock_client.log_llm_call(
                        status="truncated" if truncated else "success",
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        cache_read_tokens=result.cache_read_tokens,
                        cache_write_tokens=result.cache_write_tokens,
                        latency_ms=int((time.time() - t0) * 1000), model_id=job.model_id,
                        error_code="max_tokens" if truncated else "",
                        still=still_name, phase="map", page_id=page_dir_name, job_id=job.job_id)
                finally:
                    # the call is now in telemetry (actual spend), so drop its reservation
                    if guard is not None:
                        guard.release()
                with job._lock:
                    job.total_input_tokens += result.input_tokens
                    job.total_output_tokens += result.output_tokens
                    job.total_cache_read_tokens += result.cache_read_tokens
                    job.total_cache_write_tokens += result.cache_write_tokens
                if not truncated:
                    break
                print(f"[page-centric] {page_dir_name}/{still_name}: output hit max_tokens={out_budget}"
                      + (" — retrying at the ceiling" if attempt + 1 < len(budgets) else ""),
                      flush=True)
            if truncated:
                return {"page_idx": page_idx, "throttled": False,
                        "hard_error": f"[{still_name}] map output truncated at max_tokens="
                                      f"{budgets[-1]} (page too large for one extraction; "
                                      f"lower DISTILL_TOKEN_BUDGET)"}
            out_file.write_text(result.output_text, encoding="utf-8")
        return {"page_idx": page_idx, "throttled": False, "hard_error": None}

    print(f"[page-centric] {len(job.pending_pages)} pages x {n_stills} stills "
          f"(cache_on={cache_on}, window={window}, max={max_window})", flush=True)

    pending = list(job.pending_pages)
    round_num = 0
    budget_stopped = 0
    while pending and not job.cancel_event.is_set():
        if guard is not None and guard.exhausted:
            budget_stopped += len(pending)
            break
        round_num += 1
        throttled_pages: list = []
        completed_this_round = 0
        with job._lock:
            job.pages_in_flight = 0
        print(f"[page-centric] round {round_num}: {len(pending)} pages @ window={window}", flush=True)
        with ThreadPoolExecutor(max_workers=window) as ex:
            futures = {ex.submit(_sweep_page, pi, pp): (pi, pp) for pi, pp in pending}
            for fut in as_completed(futures):
                if job.cancel_event.is_set():
                    break
                pi, pp = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:  # unexpected escape — treat as hard error, surface it
                    r = {"page_idx": pi, "throttled": False,
                         "hard_error": f"unexpected: {type(e).__name__}: {e}"}
                if r.get("budget"):
                    budget_stopped += 1
                elif r["throttled"]:
                    throttled_pages.append((pi, pp))
                elif r["hard_error"] and r["hard_error"] != "cancelled":
                    with job._lock:
                        job.pages_failed += 1
                        job.errors.append({"page_idx": pi, "error_type": "hard_error",
                                           "message": r["hard_error"]})
                elif not r["hard_error"]:
                    completed_this_round += 1
                    with job._lock:
                        job.pages_complete += 1

        # Controller step: adapt the window based on what THIS round saw (AIMD model).
        if throttled_pages:
            new_window = max(min_window, window // 2)   # multiplicative decrease
            wait = min(max_recovery_wait, recovery_wait * (1.0 if window > new_window else 1.5))
            print(f"[page-centric] round {round_num}: {len(throttled_pages)} pages TPM-throttled "
                  f"(completed {completed_this_round}); window {window}->{new_window}, "
                  f"waiting {wait:.0f}s for TPM recovery before resubmit", flush=True)
            window = new_window
            # Anchored wait toward TPM-window recovery; poll cancel so a cancel doesn't hang the wait.
            waited = 0.0
            while waited < wait and not job.cancel_event.is_set():
                time.sleep(min(5.0, wait - waited))
                waited += 5.0
            pending = throttled_pages  # resubmit ONLY the throttled ones (resume skips done stills)
        else:
            # Clean round: gently increase toward the ceiling (additive-ish), nothing to resubmit.
            if completed_this_round > 0 and window < max_window:
                window = min(max_window, int(window * 1.5))
            pending = []

    # Final status
    if job.cancel_event.is_set():
        job.status = "cancelled"
    elif job.pages_failed:
        job.status = "failed"
    elif budget_stopped:
        job.status = "budget_exhausted"
        with job._lock:
            job.errors.append({"page_idx": None, "error_type": "budget_exhausted",
                               "message": f"{budget_stopped} page(s) stopped at DISTILL_MAX_USD "
                                          f"(spent ${guard.last_spent:.2f})"})
    else:
        job.status = "complete"
    job.end_time = time.time()
    print(f"[page-centric] done: {job.pages_complete}/{job.pages_total} pages, "
          f"{job.pages_failed} failed, cache_read={job.total_cache_read_tokens} "
          f"cache_write={job.total_cache_write_tokens}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Reduce jobs (async): per-still concat + reconciliation, run in a Job thread so
# reduce is observable (check_distillation + dashboard) and emits phase="reduce"
# telemetry — the same submit→poll contract as map (X-map-reduce-extraction).
# ─────────────────────────────────────────────────────────────────────────────

_REDUCE_CHUNK_SIZE = 400000  # ~133K tokens per reconciliation chunk (matches sync reduce)


def _reduce_reconcile_prompt(still_name: str) -> str:
    return f"""You are producing a DEDUP INDEX for specification distillation ({still_name} still).

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


def _reduce_chunk_max_attempts() -> int:
    """Max attempts (1 initial + retries) per reconciliation chunk. Default 4, overridable via
    DISTILL_REDUCE_CHUNK_ATTEMPTS. Parsed defensively (bad value → default) like max_call_tokens."""
    try:
        v = int(os.environ.get("DISTILL_REDUCE_CHUNK_ATTEMPTS", "") or REDUCE_CHUNK_MAX_ATTEMPTS)
    except ValueError:
        v = REDUCE_CHUNK_MAX_ATTEMPTS
    return max(1, v)


# Per-chunk reconciliation retry budget (1 initial attempt + up to 3 retries). Env-overridable via
# DISTILL_REDUCE_CHUNK_ATTEMPTS. See the WHY comment in reduce_one_still's Phase 2 loop.
REDUCE_CHUNK_MAX_ATTEMPTS = 4


def reduce_one_still(
    still_name: str,
    partials_base: str,
    output_base: str,
    model_id: str,
    max_tokens: int,
    client,
    llm_client=None,
    job_id: str = "",
    cancel_event: "threading.Event | None" = None,
) -> dict:
    """Reduce ONE still: deterministic concat (zero loss) + reconciliation dedup overlay.

    Reads `{partials_base}/{still}/page_*/{still}_extract.md`, writes `{output_base}/{still}/
    {still}_raw_concat.md` (+ dedup index + README) — the exact still-centric layout, so this is
    the single per-still reduce shared by the async reduce Job and the sync back-compat handler.

    Observability (the reason this exists as a named unit): every reconciliation Bedrock call is
    logged via log_llm_call(phase="reduce", still=..., job_id=...) so reduce token/cache usage
    lands in the same `{still}_reduce.jsonl` telemetry the analysis/dashboard read — closing the
    gap where the old sync reduce emitted no telemetry at all.
    """
    import json as _json
    import re as _re

    still_out = Path(output_base) / still_name
    partials_dir = Path(partials_base) / still_name
    extract_files = sorted(
        partials_dir.glob(f"page_*/{still_name}_extract.md"),
        key=lambda p: p.parent.name,
    )
    if not extract_files:
        return {"error": True, "still_name": still_name,
                "message": f"No {still_name}_extract.md under {partials_dir}"}

    still_out.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: deterministic concat (no LLM, zero loss) ─────────────────────
    concat_path = still_out / f"{still_name}_raw_concat.md"
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

    # ── Phase 2: reconciliation (dedup overlay) — telemetry-emitting ──────────
    full_text = concat_path.read_text(encoding="utf-8")
    chunks = [full_text[i:i + _REDUCE_CHUNK_SIZE]
              for i in range(0, len(full_text), _REDUCE_CHUNK_SIZE)]
    reconcile_prompt = _reduce_reconcile_prompt(still_name)
    total_in = 0
    total_out = 0
    chunk_results = []
    # WHY reduce now retries per chunk: the reconciliation path calls the model ONCE per
    # chunk with no internal retry, so a single transient Bedrock stall (StreamIdleTimeout) or a
    # read/connect timeout or a throttle on ONE chunk used to abandon the ENTIRE still — fatal for
    # an unattended Opus run that nobody can babysit. This mirrors the page-centric map executor's
    # congestion handling: a transient failure is backed off and resubmitted (same
    # bedrock_client.is_retryable_bedrock_error classifier the map path uses, so the two can't
    # drift), and only a genuinely non-retryable error or an exhausted retry budget fails the
    # still. The deterministic concat (Phase 1 above) is untouched; this only wraps the Phase 2
    # reconciliation LLM calls.
    max_attempts = _reduce_chunk_max_attempts()
    for idx, chunk in enumerate(chunks):
        for attempt in range(max_attempts):
            # Respect cancellation between attempts (and never sleep through a cancel below).
            if cancel_event is not None and cancel_event.is_set():
                return {"error": True, "still_name": still_name,
                        "message": f"reconciliation cancelled (still {still_name}, "
                                   f"chunk {idx+1}/{len(chunks)})"}
            t0 = time.time()
            try:
                if llm_client:
                    result = bedrock_client.call_llm(
                        llm_client, model_id, reconcile_prompt,
                        f"Chunk {idx+1}/{len(chunks)}:\n\n{chunk}", max_tokens)
                else:
                    result = bedrock_client.call_converse_with_retry(
                        client, model_id, reconcile_prompt,
                        f"Chunk {idx+1}/{len(chunks)}:\n\n{chunk}", max_tokens)
            except Exception as e:
                # Same telemetry fields/shape as before; only the status VALUE now distinguishes a
                # throttle (backpressure) from another transient/hard error, matching the converse
                # retry path's accounting.
                bedrock_client.log_llm_call(
                    status="throttled" if bedrock_client.is_throttle_error(e) else "error",
                    error_code=type(e).__name__, still=still_name,
                    phase="reduce", job_id=job_id, model_id=model_id,
                    latency_ms=int((time.time() - t0) * 1000))
                # Retryable AND budget remaining → exponential backoff (2/4/8s via the shared
                # DEFAULT_BACKOFF_BASE), then resubmit. Otherwise fall through to the error return.
                if bedrock_client.is_retryable_bedrock_error(e) and attempt + 1 < max_attempts:
                    if cancel_event is not None and cancel_event.is_set():
                        return {"error": True, "still_name": still_name,
                                "message": f"reconciliation cancelled (still {still_name}, "
                                           f"chunk {idx+1}/{len(chunks)})"}
                    time.sleep(bedrock_client.DEFAULT_BACKOFF_BASE * (2 ** attempt))
                    continue
                return {"error": True, "still_name": still_name,
                        "message": f"reconciliation chunk {idx+1}/{len(chunks)} failed after "
                                   f"{attempt+1} attempt(s): {type(e).__name__}: {e}"}
            else:
                total_in += result.input_tokens
                total_out += result.output_tokens
                bedrock_client.log_llm_call(
                    status="success", input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cache_read_tokens=getattr(result, "cache_read_tokens", 0),
                    cache_write_tokens=getattr(result, "cache_write_tokens", 0),
                    latency_ms=int((time.time() - t0) * 1000), model_id=model_id,
                    still=still_name, phase="reduce", job_id=job_id)
                chunk_results.append(result.output_text)
                break

    # Parse + merge chunk JSON
    merged_dupes = []
    total_unique = 0
    total_findings = 0
    for cr in chunk_results:
        cleaned = _re.sub(r'^```(?:json)?\s*', '', cr.strip())
        cleaned = _re.sub(r'```\s*$', '', cleaned)
        try:
            parsed = _json.loads(cleaned)
            merged_dupes.extend(parsed.get("duplicates", []))
            total_unique += parsed.get("unique_count", 0)
            total_findings += parsed.get("total_findings", 0)
        except _json.JSONDecodeError:
            merged_dupes.append({"raw_chunk": cr[:2000], "parse_error": True})

    dupe_groups = len([d for d in merged_dupes if not d.get("parse_error")])
    dedup_path = still_out / f"{still_name}_dedup_index.json"
    dedup_path.write_text(_json.dumps({
        "still_name": still_name,
        "pages_processed": len(extract_files),
        "reconciliation_chunks": len(chunks),
        "total_findings": total_findings,
        "unique_findings": total_unique,
        "duplicate_groups": dupe_groups,
        "duplicates": merged_dupes,
    }, indent=2), encoding="utf-8")

    (still_out / "README.md").write_text(
        f"""# {still_name} Still — Reduced Output (Concat + Reconciliation)

| File | Purpose |
|------|---------|
| `{still_name}_raw_concat.md` | Complete extraction — every finding from every page. Zero loss. |
| `{still_name}_dedup_index.json` | Reconciliation overlay: duplicate groups, confidence. |

- Pages: {len(extract_files)}
- Raw concat: {total_bytes/1024:.0f} KB
- Duplicate groups: {dupe_groups}
- Reconciliation chunks: {len(chunks)}
""", encoding="utf-8")

    return {
        "error": False,
        "still_name": still_name,
        "partials_read": len(extract_files),
        "partials_bytes": total_bytes,
        "concat_file": str(concat_path),
        "dedup_index_file": str(dedup_path),
        "duplicate_groups": dupe_groups,
        "input_tokens": total_in,
        "output_tokens": total_out,
    }


def create_reduce_job(
    stills: list[tuple[str, str]],
    partials_base: str,
    output_base: str,
    model_id: str,
    max_tokens: int,
) -> Job:
    """Create an async REDUCE Job over a set of stills.

    Reuses the pages_* vocabulary: pages_total = number of stills, pages_complete counts stills
    reduced. Resume: stills that already have `{output_base}/{still}/{still}_raw_concat.md` are
    pre-counted complete and skipped by the executor (idempotent re-invoke).
    """
    job_id = str(uuid.uuid4())
    # Resume pre-count: a still already reduced is complete.
    already = 0
    for name, _ in stills:
        rc = Path(output_base) / name / f"{name}_raw_concat.md"
        if rc.exists() and rc.stat().st_size > 0:
            already += 1
    job = Job(
        job_id=job_id,
        still_name="reduce",
        mode="reduce",
        status="running",
        pages=[],
        pending_pages=[],
        pages_total=len(stills),
        pages_complete=already,
        pages_failed=0,
        pages_in_flight=0,
        output_dir=output_base,
        still_prompt="",
        model_id=model_id,
        max_tokens=max_tokens,
        concurrency=1,
        start_time=time.time(),
        stills=stills,
        partials_base=partials_base,
    )
    _registry[job_id] = job
    return job


def _reduce_executor(job: Job, client, llm_client=None) -> None:
    """Reduce every still in job.stills, updating pages_* progress as each completes."""
    import os
    import budget

    print(f"[reduce] {job.pages_total} stills, {job.pages_complete} already done", flush=True)
    guard = budget.guard_from_env(os.environ.get("DISTILL_TELEMETRY_DIR", ""), job.model_id,
                                  log=lambda m: print(m, flush=True))
    reduce_unit = budget.unit_costs(job.model_id)[0]["reduce"][0]
    budget_stopped = False
    for still_name, _prompt in (job.stills or []):
        if job.cancel_event.is_set():
            break
        rc = Path(job.output_dir) / still_name / f"{still_name}_raw_concat.md"
        if rc.exists() and rc.stat().st_size > 0:
            continue  # already reduced (pre-counted in create_reduce_job)
        # Budget guard: one still's reduce is estimated at the per-(page, still) reduce unit cost
        # times its page count; stop before it when that would cross DISTILL_MAX_USD.
        n_parts = len(list((Path(job.partials_base) / still_name).glob(f"page_*/{still_name}_extract.md")))
        est = reduce_unit * max(1, n_parts)
        if guard is not None and not guard.try_reserve(est):
            budget_stopped = True
            break
        with job._lock:
            job.pages_in_flight += 1
        r = reduce_one_still(
            still_name=still_name,
            partials_base=job.partials_base,
            output_base=job.output_dir,
            model_id=job.model_id,
            max_tokens=job.max_tokens,
            client=client,
            llm_client=llm_client,
            job_id=job.job_id,
            cancel_event=job.cancel_event,
        )
        if guard is not None:
            guard.release(est)
        with job._lock:
            job.pages_in_flight -= 1
            if r.get("error"):
                job.pages_failed += 1
                job.errors.append({"page_idx": None, "error_type": "ReduceError",
                                   "message": f"[{still_name}] {r.get('message','')}"})
            else:
                job.pages_complete += 1
                job.total_input_tokens += r.get("input_tokens", 0)
                job.total_output_tokens += r.get("output_tokens", 0)

    # Opt-in tolerance for a SMALL number of failed stills. Default 0 = strict (unchanged: any
    # failure → "failed"). When DISTILL_REDUCE_MAX_FAILED_STILLS=N>0 and no more than N stills
    # failed AND at least one succeeded, the run finishes "complete" with the failed stills still
    # recorded in job.errors. Status MUST be exactly "complete": pagecentric_invoke.py treats any
    # other reduce status as a hard failure (rc 1), so a bespoke status would make the run fail
    # HARDER, not continue. This is safe because the Phase 2→2.5 completeness barrier re-validates
    # each still's `{still}_raw_concat.md` — written in Phase 1 (deterministic concat) BEFORE any
    # reconciliation LLM call — and the only artifact a failed reconciliation drops is the
    # `{still}_dedup_index.json` overlay, which no downstream phase consumes. The pages_complete>0
    # guard guarantees we never "succeed" a reduce where every still failed.
    try:
        max_failed = int(os.environ.get("DISTILL_REDUCE_MAX_FAILED_STILLS", "") or 0)
    except ValueError:
        max_failed = 0
    max_failed = max(0, max_failed)

    # Does the opt-in tolerance apply to this run's failures? (Only consulted on the pages_failed
    # branch below, so the original cancelled/failed/budget_exhausted precedence is preserved.)
    tolerated = (max_failed > 0 and 0 < job.pages_failed <= max_failed and job.pages_complete > 0)

    if job.cancel_event.is_set():
        job.status = "cancelled"
    elif job.pages_failed and not tolerated:
        job.status = "failed"
    elif budget_stopped:
        job.status = "budget_exhausted"
    else:
        job.status = "complete"
        if tolerated:
            # Keep the per-still error entries (recorded above) and add a summary note.
            job.errors.append({"page_idx": None, "error_type": "ReduceTolerated",
                               "message": f"tolerated {job.pages_failed} failed still(s) under "
                                          f"DISTILL_REDUCE_MAX_FAILED_STILLS={max_failed}"})
    job.end_time = time.time()
    print(f"[reduce] done: {job.pages_complete}/{job.pages_total} stills reduced, "
          f"{job.pages_failed} failed", flush=True)


def start_reduce(job: Job, client, llm_client=None) -> None:
    """Spawn the async reduce executor thread (submit→poll contract, symmetric with map)."""
    thread = threading.Thread(
        target=_reduce_executor,
        args=(job, client, llm_client),
        daemon=True,
    )
    thread.start()

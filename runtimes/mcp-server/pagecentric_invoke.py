#!/usr/bin/env python3
"""pagecentric_invoke.py — thin in-process invoker for the page-centric orchestration path.

The orchestrator (run_distillation.sh Phase 2, page-centric branch) shells out to this once. It
drives the unified async MCP surface directly (no agent, no kiro-cli tool-call plumbing) so the
distillation logic has NO runtime dependency on kiro-cli — swap the shell for anything and this
same invoker works:

    submit_map(strategy=page-centric, stills=[names], pages, output_base) -> poll -> complete
    submit_reduce(stills=[names], partials_base, output_base)             -> poll -> complete

Stills are passed by NAME only (resolved server-side by still_registry). The invoker builds the
bedrock client the same way server.py does. Progress is observable throughout: map emits
phase=map telemetry + pages_* progress; reduce emits phase=reduce telemetry + stills progress;
the filesystem layout ({base}/{still}/page_NNN/{still}_extract.md, {base}/{still}/{still}_raw_
concat.md) is exactly what the progress dashboard reads.

Usage:
    pagecentric_invoke.py --pages-file F --output-base D --stills a,b,c
                          [--model-id M] [--max-tokens N] [--reduce-max-tokens N]
                          [--poll-interval S] [--timeout S] [--facts-dir D]
                          [--identifiers-dir D]

--facts-dir: directory of per-page structural-facts blocks (page_NNN.md, written by
runtimes/facts/build_facts.py in Phase 1.2). Each page's block is appended to that page's text in
every map prompt. Omit it (or point at a missing dir) to map without facts.

--identifiers-dir: directory of per-page identifier-evidence blocks (page_NNN.md, written by
runtimes/facts/identifier_evidence.py in Phase 1.3), appended after the facts block.

Exit 0 on both phases complete; 6 (map) or 7 (reduce) when the DISTILL_MAX_USD budget guard
stopped the phase (finished extracts / reduced stills are kept; run_distillation.sh marks
run_status.json budget_exhausted); other non-zero on failure/timeout.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bedrock_client  # noqa: E402
import jobs as jobs_mod  # noqa: E402
import tools  # noqa: E402


EXIT_BUDGET = 6         # map stopped at the cap
EXIT_BUDGET_REDUCE = 7  # reduce stopped at the cap


def _log(msg: str) -> None:
    print(f"[pagecentric-invoke {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _poll(job_id: str, poll_interval: float, timeout: float, label: str) -> str:
    """Poll a job to a terminal state. Anchored-wait: sleep only the remaining slice toward the
    next poll tick relative to the loop's own clock, so it never overshoots. Returns final status.
    """
    deadline = time.time() + timeout
    while True:
        chk = tools.handle_check_distillation({"job_id": job_id})
        status = chk.get("status", "unknown")
        _log(f"{label}: {status} ({chk.get('pages_complete')}/{chk.get('pages_total')}, "
             f"in_flight={chk.get('pages_in_flight')})")
        if status in ("complete", "failed", "cancelled", "budget_exhausted"):
            return status
        if time.time() >= deadline:
            _log(f"{label}: TIMEOUT after {timeout}s")
            return "timeout"
        anchor = time.time()
        remaining = poll_interval - (time.time() - anchor)
        if remaining > 0:
            time.sleep(remaining)


def main() -> int:
    ap = argparse.ArgumentParser(description="Page-centric orchestration invoker")
    ap.add_argument("--pages-file", required=True, help="file of newline-separated page paths")
    ap.add_argument("--output-base", required=True, help="stills base dir ({base}/{still}/...)")
    ap.add_argument("--stills", required=True, help="comma-separated still NAMES")
    ap.add_argument("--model-id", default=os.environ.get("BEDROCK_MODEL_ID",
                                                         "us.anthropic.claude-sonnet-4-6"))
    ap.add_argument("--max-tokens", type=int, default=32768)
    ap.add_argument("--reduce-max-tokens", type=int, default=32768)
    ap.add_argument("--poll-interval", type=float, default=10.0)
    ap.add_argument("--timeout", type=float, default=7200.0)
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""))
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    ap.add_argument("--facts-dir", default="", help="per-page structural facts blocks (page_NNN.md)")
    ap.add_argument("--identifiers-dir", default="",
                    help="per-page identifier evidence blocks (page_NNN.md)")
    args = ap.parse_args()

    with open(args.pages_file) as f:
        pages = [ln.strip() for ln in f if ln.strip()]
    if not pages:
        _log("ERROR: no pages in pages-file")
        return 2
    names = [s.strip() for s in args.stills.split(",") if s.strip()]
    if not names:
        _log("ERROR: no stills")
        return 2

    _log(f"{len(pages)} pages x {len(names)} stills [{', '.join(names)}] -> {args.output_base}")
    facts_dir = args.facts_dir if args.facts_dir and Path(args.facts_dir).is_dir() else ""
    if args.facts_dir and not facts_dir:
        _log(f"WARNING facts dir {args.facts_dir} not found — mapping WITHOUT structural facts")
    elif facts_dir:
        n = len(list(Path(facts_dir).glob("page_*.md")))
        _log(f"structural facts: {n} page block(s) from {facts_dir}")
    ident_dir = args.identifiers_dir if args.identifiers_dir and Path(args.identifiers_dir).is_dir() else ""
    if ident_dir:
        n = len(list(Path(ident_dir).glob("page_*.md")))
        _log(f"identifier evidence: {n} page block(s) from {ident_dir}")
    # Both clients are bedrock-runtime clients (Converse API); mirrors server.py's client setup.
    # The page-centric executor plants a Converse `cachePoint` after each page (prompt caching).
    client = bedrock_client.create_client(args.profile, args.region)
    llm = bedrock_client.create_llm_client(args.profile, args.region)

    # ── MAP (page-centric) ───────────────────────────────────────────────
    r = tools.handle_submit_map(
        {"pages": pages, "stills": names, "output_base": args.output_base,
         "strategy": "page-centric", "model_id": args.model_id, "max_tokens": args.max_tokens,
         "facts_dir": facts_dir, "identifiers_dir": ident_dir},
        client, llm)
    if r.get("error"):
        _log(f"ERROR submit_map: {r.get('message')}")
        return 1
    map_job = r["job_id"]
    _log(f"map job {map_job} (strategy={r['orchestration_strategy']})")
    st = _poll(map_job, args.poll_interval, args.timeout, "map")
    if st == "budget_exhausted":
        _log("map stopped by the budget guard (DISTILL_MAX_USD); finished extracts kept for resume")
        return EXIT_BUDGET
    if st != "complete":
        _log(f"map did not complete (status={st})")
        return 1

    # ── REDUCE (async, per-still over the same per-still tree) ─────────────
    rr = tools.handle_submit_reduce(
        {"partials_base": args.output_base, "stills": names, "output_base": args.output_base,
         "model_id": args.model_id, "max_tokens": args.reduce_max_tokens},
        client, llm)
    if rr.get("error"):
        _log(f"ERROR submit_reduce: {rr.get('message')}")
        return 1
    reduce_job = rr["job_id"]
    _log(f"reduce job {reduce_job} ({rr['stills_total']} stills, "
         f"{rr['stills_already_done']} already done)")
    st = _poll(reduce_job, args.poll_interval, args.timeout, "reduce")
    if st == "budget_exhausted":
        _log("reduce stopped by the budget guard (DISTILL_MAX_USD); reduced stills kept for resume")
        return EXIT_BUDGET_REDUCE
    if st != "complete":
        _log(f"reduce did not complete (status={st})")
        return 1

    _log("page-centric map + reduce complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

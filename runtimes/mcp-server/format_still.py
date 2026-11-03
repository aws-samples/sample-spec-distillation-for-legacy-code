#!/usr/bin/env python3
"""
format_still.py — LLM formatting pass for materialized still files.

Takes raw concept blocks (already deduplicated by the knowledge layer)
and produces polished spec-quality documents via LLM. Each manifest file
gets its own LLM call(s) to format its blocks into structured prose.

Architecture Decision:
────────────────────
The LLM's job here is FORMATTING ONLY — organizing blocks into tables,
adding grouping headers, and standardizing structure. It is NOT permitted
to drop, merge, or summarize blocks. Every input block must appear as a
distinct entry in the output. This is enforced by:
1. Explicit "do not drop" instruction in the prompt
2. Block count verification (input count == output ### count)
3. Chunking large sets so the LLM never sees more than it can faithfully format

Usage:
    python3 format_still.py <raw_manifest_file> <output_file>

    Or import and call format_manifest_file() programmatically.
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import bedrock_client


# ── Constants ────────────────────────────────────────────────────────────────

# Max blocks per LLM call — keeps output faithful
# At ~200 tokens per formatted block, 500 blocks ≈ 100K output tokens
MAX_BLOCKS_PER_CHUNK = 200

FORMAT_SYSTEM_PROMPT = """You are formatting raw specification extractions into a polished specification document.

The input document declares its own block count in a BLOCK_MANIFEST comment at the top and an END_MANIFEST comment at the bottom. Your output MUST contain exactly that many ### entries. This is a hard constraint — treat it like a schema validation requirement.

RULES:
1. The BLOCK_MANIFEST declares number_of_blocks. Your output MUST have AT LEAST that many ### entries.
   If you split a mega-block (rule 12), the count will be higher — that is correct. Never FEWER.
2. You may NOT merge, summarize, or drop any block. Each numbered [NNN] = one output entry.
3. PRESERVE the [NNN] number prefix in each output header exactly as given.
4. The <!-- ref:xxxxxxxx --> comments are traceability tags — preserve them as-is.
5. You may NOT add new entries beyond what's numbered in the input.
6. Organize blocks into logical groupings with ## headers (these don't count toward the block count).
7. Within each block, standardize the structure:
   - For rules/invariants: Use REQUIRE/PERMIT/DENY formal predicates where possible
   - Add "Where:" clauses explaining variables
   - Add "Business rationale:" if derivable from the block content
   - Add "What breaks if violated:" if derivable
   - Keep Confidence scores
13. FIELD ORDER (hard constraint — business-first canonical order):
    Emit fields in this order within every ### entry:
      a. Orientation:      Definition
      b. Why it matters:   Business rationale, What breaks if violated, Scope, Exceptions
      c. How it works:     Preconditions, Steps, Effects, Trigger, Success/Failure outcome,
                           Actor, and any other behavioral fields
      d. Technical detail: Statement (formal predicate), Enforcement, Aliases, Evidence
      e. Confidence footer (ALWAYS last, contiguous, in this exact order):
             - Business Relevance: N
             - Evidence Diversity: N
             - Confidence: N
    Rationale: business readers want what -> why -> stakes -> how it works ->
    implementation detail -> confidence. Lead with meaning, not identifiers.
    NEVER place Aliases/Evidence/Statement above the business "why" fields.
    NEVER place any field after the Confidence footer — nothing follows Confidence.
8. Use tables where appropriate (entity registries, junction objects)
9. Strip implementation details embedded in prose (raw code snippets, import statements, framework class names used as explanations). PRESERVE the `- Evidence:` field and its file#L{start}-L{end} citations — these are provenance references, not implementation detail.
10. Strip LOCATION/ENFORCEMENT POINT lines only if their content is redundant with the Evidence field. If they add information not in Evidence, keep them.
11. If a block title is just "Rule" with no name, derive a descriptive name from its CONDITION/BEHAVIOR content
12. ONE CONCEPT PER ### ENTRY — NEVER. If an input block contains multiple distinct business objects
    (e.g., multiple entities listed under one heading like "Entities"), you MUST split them into separate
    ### entries, each with its own [NNN] number. Adjust the block count in the BLOCK_MANIFEST accordingly.
    A single ### entry titled "Entities" containing Order, Line Item, Customer as sub-items is WRONG —
    each of those is its own ### entry.

OUTPUT FORMAT:
- Emit the BLOCK_MANIFEST comment first (copy it from input)
- Brief description of what this document covers (2-3 sentences max)
- Group blocks by business domain / entity family under ## headers
- Each block: ### [NNN] Descriptive Name (with <!-- ref:... --> preserved)
- Emit the END_MANIFEST comment last
- Final line: *number_of_blocks entries documented.*
"""


# ── Core Logic ───────────────────────────────────────────────────────────────

# Requirement-id prefix per still (feedback_evidence.py keeps an equal table; a test checks it).
STILL_PREFIXES = {
    "domain_language": "DL", "data": "DT", "rule": "RL", "workflow": "WF",
    "state": "ST", "permission": "PM", "exception": "EX", "test": "TS",
    "interface": "IF", "history": "HI", "runtime": "RT",
}
# knowledge.py writes this line under each materialized file's title (`# Persistent Objects`).
STILL_META_RX = re.compile(r"^<!--\s*still:\s*([A-Za-z0-9_]+)\s*[|>-]", re.MULTILINE)


def still_prefix_for(input_path: str, doc_title: str = "", input_text: str = "",
                     still: str | None = None) -> tuple[str, str, str]:
    """(still, id prefix, how it was found) for a file the format pass reads. In order:
      1. `still`, when the caller knows it (format_all_stills passes the still directory name);
      2. the knowledge layer's metadata line `<!-- still: NAME | concepts: N | ... -->`;
      3. the path: .../stills/<still>/final[...]/file.md;
      4. legacy: a still name inside the file stem or the document title (titles used to read
         `# data — Persistent Objects`; since the knowledge layer writes `# Persistent Objects`
         this alone gave 181 of 328 AP requirements an `XX-` id).
    ("", "XX", "unknown") when none applies."""
    if still and still in STILL_PREFIXES:
        return still, STILL_PREFIXES[still], "argument"
    m = STILL_META_RX.search(input_text[:4096] if input_text else "")
    if m and m.group(1) in STILL_PREFIXES:
        return m.group(1), STILL_PREFIXES[m.group(1)], "metadata"
    parts = Path(input_path).parts
    for i, p in enumerate(parts):
        if p.startswith("final") and i > 0 and parts[i - 1] in STILL_PREFIXES:
            return parts[i - 1], STILL_PREFIXES[parts[i - 1]], "path"
    input_lower = Path(input_path).stem.lower() + " " + (doc_title or "").lower()
    for still_name, prefix in STILL_PREFIXES.items():
        if still_name in input_lower:
            return still_name, prefix, "name"
    return "", "XX", "unknown"



def format_manifest_file(
    input_path: str,
    output_path: str,
    profile: str = "",
    region: str = "us-east-1",
    model_id: str = "us.anthropic.claude-sonnet-4-6",
    max_blocks_per_chunk: int = MAX_BLOCKS_PER_CHUNK,
    concurrency: int = 10,
    max_tokens: int = 128000,
    still: str | None = None,
) -> dict:
    """Format a raw manifest file into a polished spec document.

    Reads the raw file (block dump from knowledge layer), chunks if needed,
    sends chunks to LLM in parallel for formatting, concatenates results.

    Args:
        input_path: Path to the raw manifest file (e.g., business_rule_catalog.md)
        output_path: Path to write the formatted output
        profile: AWS profile
        region: AWS region
        model_id: Bedrock model ID
        max_blocks_per_chunk: Max blocks per LLM call
        concurrency: Max parallel LLM calls
        max_tokens: Max output tokens per chunk call
        still: The still the file belongs to (sets the requirement id prefix). Optional: when
            absent it is read from the file's `<!-- still: NAME | ... -->` metadata line or its
            stills/<still>/final/ path (still_prefix_for).

    Returns:
        Stats dict with block counts and verification results.
    """
    input_text = Path(input_path).read_text(encoding="utf-8")

    # Derive still name from path for logging (e.g., .../stills/data/final/file.md → data)
    _file_label = Path(input_path).name
    _still_label = ""
    parts = Path(input_path).parts
    for i, p in enumerate(parts):
        if p == "final" and i > 0:
            _still_label = parts[i - 1]
            break
    if not _still_label:
        _still_label = Path(input_path).stem.split("_")[0]
    _log_prefix = f"[{_still_label}/{_file_label}]"

    # Parse into blocks (split on ### headers)
    blocks = _split_into_blocks(input_text)
    total_blocks = len(blocks)

    if total_blocks == 0:
        Path(output_path).write_text("*No content to format.*\n", encoding="utf-8")
        return {"input_blocks": 0, "output_blocks": 0, "status": "empty"}

    print(f"    {_log_prefix} {total_blocks} blocks", flush=True)

    # Progress trace file — written after each chunk for external monitoring
    progress_file = Path(output_path).parent / ".format_progress.jsonl"
    # Write start marker
    with open(progress_file, "a") as pf:
        pf.write(json.dumps({
            "ts": time.strftime("%H:%M:%S"),
            "file": Path(input_path).name,
            "event": "start",
            "total_blocks": total_blocks,
        }) + "\n")

    # Chunk if needed
    chunks = []
    for i in range(0, total_blocks, max_blocks_per_chunk):
        chunk_blocks = blocks[i:i + max_blocks_per_chunk]
        chunks.append(chunk_blocks)

    # Extract document title from the input file header
    doc_title = Path(input_path).stem.replace("_", " ").title()
    header_match = re.match(r"^#\s+(.+)$", input_text, re.MULTILINE)
    if header_match:
        doc_title = header_match.group(1)

    # Process chunks in parallel
    llm = bedrock_client.create_llm_client(profile, region)
    chunk_results: dict[int, str] = {}

    def process_chunk(chunk_idx, chunk_blocks: list[str], split_round: int = 0) -> tuple:
        import time as _time
        import hashlib as _hashlib
        # Number blocks with chunk prefix (C{chunk_idx}.{NNN}) to avoid collisions during assembly.
        # Global renumbering happens at assembly time.
        numbered_blocks = []
        for i, block in enumerate(chunk_blocks):
            block_num = i + 1
            guid = _hashlib.sha256(block[:200].encode()).hexdigest()[:8]
            chunk_local_id = f"C{chunk_idx:02d}.{block_num:03d}"
            if block.startswith("### "):
                header_end = block.index("\n") if "\n" in block else len(block)
                original_title = block[4:header_end]
                numbered_block = f"### [{chunk_local_id}] {original_title}\n<!-- ref:{guid} -->{block[header_end:]}"
            else:
                numbered_block = f"### [{chunk_local_id}]\n<!-- ref:{guid} -->\n{block}"
            numbered_blocks.append(numbered_block)

        block_count = len(chunk_blocks)
        chunk_text = f"<!-- BLOCK_MANIFEST: count={block_count} verify=true -->\n\n"
        chunk_text += "\n\n".join(numbered_blocks)
        chunk_text += f"\n\n<!-- END_MANIFEST: expected={block_count} -->"

        user_prompt = (
            f"DOCUMENT: {doc_title}\n\n"
            f"Format the following blocks. The BLOCK_MANIFEST at top declares the required count.\n\n"
            f"{chunk_text}"
        )

        # Estimate max_tokens needed for this chunk based on input size
        # ~4 chars per token, ~1.8x expansion for formatting
        chunk_input_chars = sum(len(b) for b in chunk_blocks)
        estimated_output_tokens = int((chunk_input_chars / 4) * 1.8)
        # Clamp between 20K (minimum useful) and 128K (model max)
        chunk_max_tokens = max(20000, min(128000, estimated_output_tokens))

        # Retry with exponential backoff + jitter on transient errors
        import random as _random
        t0 = _time.time()
        max_retries = 8
        for attempt in range(max_retries):
            try:
                result = bedrock_client.call_llm(
                    llm,
                    model_id,
                    FORMAT_SYSTEM_PROMPT,
                    user_prompt,
                    max_tokens=chunk_max_tokens,
                )
                break
            except Exception as e:
                err_str = str(type(e).__name__).lower() + " " + str(e).lower()
                # Only non-transient: validation errors, auth errors
                is_permanent = ("validationerror" in err_str or "credentialretrieval" in err_str
                                or "invalid_request" in err_str)
                if is_permanent:
                    raise
                if attempt < max_retries - 1:
                    # Rate limit: longer backoff with jitter to desynchronize workers
                    is_rate_limit = "rate_limit" in err_str or "429" in str(e) or "ratelimit" in err_str
                    if is_rate_limit:
                        backoff = (30 * (2 ** min(attempt, 4))) + _random.uniform(0, 20)
                    else:
                        backoff = 15 * (attempt + 1)
                    _time.sleep(backoff)
                    continue
                raise

        output_block_count = len(re.findall(r"^### \[C?\d+\.?\d*\]", result.output_text, re.MULTILINE))
        elapsed_ms = int((_time.time() - t0) * 1000)
        passed = output_block_count == block_count

        # Emit telemetry with chunk/split metadata
        bedrock_client.log_llm_call(
            status="success",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=elapsed_ms,
            model_id=model_id,
            still=Path(input_path).stem.split("_")[0] if "_" in Path(input_path).stem else "format",
            phase=f"format_r{split_round}",
            page_id=f"chunk_{chunk_idx}",
            job_id=f"{'pass' if passed else 'fail'}_{block_count}in_{output_block_count}out",
        )

        return chunk_idx, result.output_text, block_count, output_block_count

    def _run_chunks(chunk_list: list[tuple[int, list[str]]], split_round: int = 0) -> dict[int, str]:
        """Run a list of (chunk_idx, blocks) through process_chunk with concurrency."""
        import time as _time2
        results_map: dict[int, str] = {}
        failed_chunks: list[tuple[int, list[str]]] = []

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(process_chunk, idx, cblocks, split_round): (idx, cblocks)
                for idx, cblocks in chunk_list
            }
            for future in as_completed(futures):
                idx, cblocks = futures[future]
                chunk_idx, output_text, in_count, out_count = future.result()
                # Write progress trace
                trace = json.dumps({
                    "ts": _time2.strftime("%H:%M:%S"),
                    "file": Path(input_path).name,
                    "chunk": chunk_idx + 1,
                    "in": in_count,
                    "out": out_count,
                    "status": "ok" if out_count == in_count else ("loss" if out_count < in_count else "gain"),
                    "total_chunks": len(chunk_list),
                    "completed": len(results_map) + len(failed_chunks) + 1,
                })
                with open(progress_file, "a") as pf:
                    pf.write(trace + "\n")

                if out_count != in_count:
                    # Any mismatch (loss OR gain) means the model didn't faithfully preserve
                    failed_chunks.append((chunk_idx, cblocks))
                    direction = "LOSS" if out_count < in_count else "GAIN"
                    print(f"      {_log_prefix} chunk {chunk_idx + 1}: {in_count} in → {out_count} out ← {direction}, will split", flush=True)
                else:
                    results_map[chunk_idx] = output_text
                    print(f"      {_log_prefix} chunk {chunk_idx + 1}: {in_count} in → {out_count} out ✓", flush=True)

        return results_map, failed_chunks

    # First pass: run all chunks
    print(f"      {_log_prefix} Processing {len(chunks)} chunks (concurrency={concurrency})...", flush=True)
    initial_chunks = [(idx, cblocks) for idx, cblocks in enumerate(chunks)]
    chunk_results, failed = _run_chunks(initial_chunks, split_round=0)

    # Branch-and-cut: split any failed chunks in half and retry
    max_splits = 3  # prevent infinite recursion
    split_round = 0
    next_key = len(chunks)  # unique keys for split results
    while failed and split_round < max_splits:
        split_round += 1
        print(f"      {_log_prefix} Branch-and-cut round {split_round}: splitting {len(failed)} chunks...", flush=True)
        new_chunks = []
        for chunk_idx, cblocks in failed:
            mid = len(cblocks) // 2
            new_chunks.append((next_key, cblocks[:mid]))
            next_key += 1
            new_chunks.append((next_key, cblocks[mid:]))
            next_key += 1

        sub_results, failed = _run_chunks(new_chunks, split_round=split_round)
        chunk_results.update(sub_results)

    if failed:
        # Last resort: accept what we have from failed chunks
        print(f"      WARNING: {len(failed)} chunks still have loss after {max_splits} split rounds", flush=True)
        for chunk_idx, cblocks in failed:
            _, output_text, _, _ = process_chunk(chunk_idx, cblocks, split_round=max_splits + 1)
            chunk_results[next_key] = output_text
            next_key += 1

    # Assemble in order of insertion (initial chunks first, then splits in order)
    formatted_parts = [chunk_results[k] for k in sorted(chunk_results.keys())]

    # Assemble final output
    output_text = f"# {doc_title}\n\n"
    output_text += "\n\n".join(formatted_parts)

    # Unify duplicate H2 sections produced by independent chunk formatting.
    # Runs BEFORE renumbering so final IDs are monotonic in unified order.
    # Safe: only removes duplicate ## header lines, or returns input unchanged.
    try:
        from unify_sections import unify_text
        output_text, _dup = unify_text(output_text)
    except ImportError:
        pass

    # Global renumbering: replace chunk-local IDs (C{NN}.{NNN}) with still-prefixed sequential IDs.
    # The still is carried explicitly (still_prefix_for: the caller's `still`, the knowledge
    # layer's `<!-- still: NAME | ... -->` metadata line, or the stills/<still>/final/ path), not
    # guessed from the document title, which no longer names the still (docs/EDGE_CASES.md L31).
    _still_for_id, still_prefix, _prefix_source = still_prefix_for(
        input_path, doc_title, input_text, still=still)
    if still_prefix == "XX":
        print(f"    {_log_prefix} WARNING: could not tell which still this file belongs to; "
              f"requirement ids get the XX- prefix", flush=True)

    # Vend UNIQUE, stable IDs from the single authoritative vending machine
    # (replaces the old per-file counter that reset to 0001 and collided across
    # files). Vending is idempotent on a stable claim_key = hash(still+title+
    # statement), so re-formatting does NOT churn IDs. Category = still prefix, so
    # one monotonic sequence per prefix spans ALL files of the still.
    from id_vending_machine import get_default, make_claim_key
    import concept_symbols as _cs
    _vm = get_default()  # location owned by the vending machine; caller just asks

    _header_re = re.compile(r"^### \[C?\d+\.?\d*\](.*)$", re.MULTILINE)
    _matches = list(_header_re.finditer(output_text))
    _repls = []  # (start, end, new_header_line)
    for _idx, _m in enumerate(_matches):
        _trailing = _m.group(1)  # text after the id token on the header line
        _body_end = _matches[_idx + 1].start() if _idx + 1 < len(_matches) else len(output_text)
        _block_text = output_text[_m.start():_body_end]
        _title = _trailing.strip()
        _statement = _cs._field_value(_block_text, "Statement")
        _ck = make_claim_key(_still_label, _title, _statement)
        _res = _vm.claim(still_prefix, _ck, caller="format_still", phase="format",
                         evidence={"still": _still_label, "file": _file_label,
                                   "title": _title[:160]},
                         call_site=input_path)
        _repls.append((_m.start(), _m.end(), f"### [{_res['id']}]{_trailing}"))
    # Apply right-to-left so earlier offsets stay valid.
    for _start, _end, _new in reversed(_repls):
        output_text = output_text[:_start] + _new + output_text[_end:]

    # Annotate bare code fences as formal (for viewer rendering)
    lines = output_text.split("\n")
    annotated_lines = []
    in_fence = False
    for line in lines:
        if line.rstrip().lstrip() == "```" and not in_fence:
            annotated_lines.append(line.rstrip() + "formal")
            in_fence = True
        elif line.rstrip().lstrip() == "```" and in_fence:
            annotated_lines.append(line)
            in_fence = False
        else:
            annotated_lines.append(line)
    output_text = "\n".join(annotated_lines)

    # Deterministic field-order normalization (guarantee scoring trio is
    # contiguous with "What breaks if violated" preceding it, regardless of
    # LLM compliance). Safe: preserves line multiset or returns input unchanged.
    try:
        from normalize_field_order import normalize_text
        output_text, _reordered = normalize_text(output_text)
    except ImportError:
        pass

    # Write
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(output_text, encoding="utf-8")

    # Verification: count ### headers in output (any format — numbered or not)
    total_output_blocks = len(re.findall(r"^### \[", output_text, re.MULTILINE))
    loss = total_blocks - total_output_blocks
    loss_pct = (loss / total_blocks * 100) if total_blocks > 0 else 0
    loss_pct = (loss / total_blocks * 100) if total_blocks > 0 else 0

    return {
        "input_blocks": total_blocks,
        "output_blocks": total_output_blocks,
        "loss": loss,
        "loss_pct": round(loss_pct, 1),
        "chunks": len(chunks),
        "output_size_kb": round(Path(output_path).stat().st_size / 1024, 1),
        "status": "ok" if loss_pct < 5 else "CONTENT_LOSS",
    }


def _split_into_blocks(text: str) -> list[str]:
    """Split a markdown file into blocks at ### boundaries.

    Skips the file header (everything before the first ###).
    Returns list of block strings (each starting with ###).
    """
    lines = text.split("\n")
    blocks = []
    current_block_lines = []
    in_block = False

    for line in lines:
        if line.startswith("### "):
            if in_block and current_block_lines:
                blocks.append("\n".join(current_block_lines).strip())
            current_block_lines = [line]
            in_block = True
        elif in_block:
            current_block_lines.append(line)

    # Last block
    if in_block and current_block_lines:
        blocks.append("\n".join(current_block_lines).strip())

    return blocks


# ── Batch Processing ─────────────────────────────────────────────────────────


def format_all_stills(
    stills_dir: str,
    profile: str = "",
    region: str = "us-east-1",
    model_id: str = "us.anthropic.claude-sonnet-4-6",
) -> dict:
    """Format all raw manifest files across all stills.

    Reads from each still's final/ directory, writes formatted versions
    to final/formatted/ subdirectory.

    Args:
        stills_dir: Path to the stills/ directory.
        profile: AWS profile.
        region: AWS region.
        model_id: Bedrock model ID.

    Returns:
        Summary stats dict.
    """
    stills_path = Path(stills_dir)
    results = {}

    # Establish the run manifest ONCE for this run (run_root = the instance/output
    # dir that contains 'stills'). Downstream consumers resolve every run-scoped path
    # (ids.db, etc.) via run_manager and stay ignorant of where things live.
    import run_manager as _rm
    _rm.set_config(str(stills_path.parent),
                   params={"model_id": model_id, "region": region})

    # Collect all stills that need formatting
    stills_to_format = []
    stills_skipped = []
    for still_dir in sorted(stills_path.iterdir()):
        if not still_dir.is_dir():
            continue
        final_dirs = sorted(still_dir.glob("final*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not final_dirs:
            continue
        final_dir = final_dirs[0]
        formatted_dir = final_dir / "formatted"
        # Skip if already complete
        if (formatted_dir / ".complete").exists():
            stills_skipped.append(still_dir.name)
            continue
        raw_files = [f for f in final_dir.glob("*.md") if f.name != "README.md" and not f.name.startswith("._")]
        if not raw_files:
            continue
        stills_to_format.append((still_dir, final_dir, formatted_dir, raw_files))

    total_stills = len(stills_to_format) + len(stills_skipped)
    _log(f"Format phase: {total_stills} stills total, "
         f"{len(stills_skipped)} already complete, "
         f"{len(stills_to_format)} to process (parallelism=3)")
    if stills_skipped:
        _log(f"  Skipped: {', '.join(stills_skipped)}")

    def _format_one_still(args):
        """Format a single still — thread-safe."""
        still_idx, (still_dir, final_dir, formatted_dir, raw_files) = args
        formatted_dir.mkdir(exist_ok=True)
        still_name = still_dir.name
        still_results = {}

        # Verify each file's block count
        files_to_do = []
        files_done = []
        for raw_file in raw_files:
            output_file = formatted_dir / raw_file.name
            if output_file.exists() and output_file.stat().st_size > 0:
                source_blocks = len(re.findall(r"^### ", raw_file.read_text(encoding="utf-8"), re.MULTILINE))
                output_blocks = len(re.findall(r"^### \[", output_file.read_text(encoding="utf-8"), re.MULTILINE))
                if source_blocks > 0 and output_blocks >= source_blocks * 0.95:
                    files_done.append(raw_file.name)
                else:
                    _log(f"    [{still_name}] {raw_file.name}: incomplete ({output_blocks}/{source_blocks} blocks), will re-format")
                    files_to_do.append(raw_file)
            else:
                files_to_do.append(raw_file)

        _log(f"  [{still_name}] ({still_idx}/{len(stills_to_format)}) "
             f"{len(raw_files)} files: {len(files_done)} verified, {len(files_to_do)} remaining")

        still_start = time.time()

        # Write format progress marker for this still
        format_progress_marker = formatted_dir / ".format_progress.json"
        format_progress_marker.write_text(json.dumps({
            "still": still_name,
            "total_files": len(files_to_do),
            "processed": 0,
            "verified": len(files_done),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }))

        for file_idx, raw_file in enumerate(files_to_do, 1):
            output_file = formatted_dir / raw_file.name
            file_start = time.time()
            _log(f"    [{still_name}] [{file_idx}/{len(files_to_do)}] {raw_file.name}...")

            result = format_manifest_file(
                str(raw_file),
                str(output_file),
                profile=profile,
                region=region,
                model_id=model_id,
                still=still_name,
            )
            still_results[f"{still_name}/{raw_file.name}"] = result

            elapsed = time.time() - file_start
            status_icon = "✓" if result.get("status") == "ok" else "⚠"
            _log(f"    [{still_name}] [{file_idx}/{len(files_to_do)}] {raw_file.name} → "
                 f"{result.get('output_blocks', 0)}/{result.get('input_blocks', 0)} blocks, "
                 f"{result.get('output_size_kb', 0)}KB, {elapsed:.0f}s {status_icon}")

            if result.get("status") == "CONTENT_LOSS":
                _log(f"    [{still_name}] ⚠ WARNING: {raw_file.name} lost {result.get('loss', 0)} blocks ({result.get('loss_pct', 0)}%)")

            # Update format progress marker
            format_progress_marker.write_text(json.dumps({
                "still": still_name,
                "total_files": len(files_to_do),
                "processed": file_idx,
                "verified": len(files_done),
                "current_file": raw_file.name,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }))

        still_elapsed = time.time() - still_start

        # Post-format: run signal/forensics classification
        _log(f"  [{still_name}] Running signal/forensics classification...")
        try:
            from classify_still_output import classify_still as _classify_still
            classify_stats = _classify_still(still_dir)
            if classify_stats.get("status") == "ok":
                _log(
                    f"  [{still_name}] Classified: "
                    f"{classify_stats['signal']} signal, "
                    f"{classify_stats['forensics']} forensics "
                    f"({classify_stats['signal_pct']}% signal)"
                )
            else:
                _log(f"  [{still_name}] Classification skipped: {classify_stats.get('reason', 'unknown')}")
        except ImportError:
            _log(f"  [{still_name}] Classification module not available, skipping bifurcation")
        except Exception as e:
            _log(f"  [{still_name}] Classification error (non-fatal): {e}")

        # Write completion marker
        marker = formatted_dir / ".complete"
        marker.write_text(json.dumps({
            "still": still_name,
            "files_formatted": len(raw_files),
            "elapsed_seconds": round(still_elapsed),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }))
        _log(f"  [{still_name}] ✓ Complete ({len(raw_files)} files, {still_elapsed:.0f}s)")
        return still_results

    # Run stills in parallel (3 at a time)
    FORMAT_STILL_PARALLELISM = int(os.environ.get("DISTILL_FORMAT_PARALLELISM", "3"))
    indexed_stills = list(enumerate(stills_to_format, 1))

    with ThreadPoolExecutor(max_workers=FORMAT_STILL_PARALLELISM) as executor:
        futures = {executor.submit(_format_one_still, item): item for item in indexed_stills}
        for future in as_completed(futures):
            still_results = future.result()
            results.update(still_results)

    return results


def _log(msg: str):
    """Print with timestamp and flush immediately (no buffering in pipes)."""
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── CLI Entry Point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 format_still.py <stills_dir>              # format all stills")
        print("  python3 format_still.py <input.md> <output.md>    # format one file")
        sys.exit(1)

    if len(sys.argv) == 2:
        # Format all stills
        results = format_all_stills(sys.argv[1])
        print(f"\nResults: {json.dumps(results, indent=2)}")
    else:
        # Format one file
        result = format_manifest_file(sys.argv[1], sys.argv[2])
        print(f"\nResult: {json.dumps(result, indent=2)}")

#!/usr/bin/env python3
"""
synthesize_concepts.py — LLM synthesis pass for multi-contributor unified concepts.

Reads unified concepts from the shared vector store where contributor_count > 1
and body_synthesized is NULL. Sends body_raw to LLM for coherent merging.
Validates that all formal predicates and evidence citations are preserved.

Usage:
    python3 synthesize_concepts.py <vector_db_path> [--concurrency 5]

Can be run inline (Phase 2.6 in the orchestrator) or out-of-band.
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import bedrock_client
from knowledge import VectorStore


SYNTHESIS_PROMPT = """You are merging overlapping observations about the same concept into one coherent entry.

RULES:
1. Merge the NARRATIVE content (Definition, Aliases, Business rationale, What breaks if violated) into one coherent version.
2. Eliminate ONLY redundant phrasing where two contributors say the same thing differently.
3. The result must be a single coherent block in the same format as the inputs (### heading, bullet fields).
4. Do NOT add new information. Only merge what's there.
5. Do NOT summarize or compress. The output should be roughly the same length as the longest contributor.
6. Do NOT include Evidence citations or Formal Predicates — those will be injected separately after your merge.
7. If contributors disagree on Confidence, use the HIGHER value.
8. If contributors disagree on Business Relevance, use the HIGHER value.

Output ONLY the merged block (### heading through end). No preamble, no explanation."""


def _extract_predicates(text: str) -> set[str]:
    """Extract formal predicates from text (lines starting with REQUIRE/DENY/PERMIT/PROHIBIT)."""
    predicates = set()
    # Match full predicate lines (may span content within code fences)
    for match in re.finditer(r"(REQUIRE|DENY|PERMIT|PROHIBIT)\s+.+", text):
        pred = match.group(0).strip()
        # Clean trailing markdown artifacts
        pred = re.sub(r"\s*```\s*$", "", pred)
        if len(pred) > 10:  # Skip trivially short matches
            predicates.add(pred)
    return predicates


def _extract_citations(text: str) -> set[str]:
    """Extract evidence citations (file#L{N}-L{N} or file#L{N}) from text."""
    # Match patterns like: path/to/file.ext#L123-L456 or path/to/file.ext#L123
    return set(re.findall(r"[\w\-./]+(?:#L\d+(?:-L\d+)?)", text))


def _extract_code_fences(text: str) -> list[str]:
    """Extract complete code-fenced blocks from text."""
    fences = []
    for match in re.finditer(r"```[\s\S]*?```", text):
        fences.append(match.group(0))
    return fences


def _inject_preserved_content(
    body_synthesized: str,
    all_predicates: set[str],
    all_citations: set[str],
    all_code_fences: list[str],
) -> str:
    """Deterministically inject preserved predicates and citations into synthesized body.
    
    Appends an Evidence section (if citations exist) and a Formal Predicates section
    (if predicates exist) to the synthesized body, ensuring nothing is lost.
    """
    result = body_synthesized.rstrip()
    
    # Check what's already in the synthesis
    existing_citations = _extract_citations(result)
    existing_predicates = _extract_predicates(result)
    
    missing_citations = all_citations - existing_citations
    missing_predicates = all_predicates - existing_predicates
    
    # Inject missing citations into Evidence section
    if missing_citations:
        # Check if there's already an Evidence line we can append to
        if re.search(r"^(?:\*\*)?Evidence(?:\*\*)?:", result, re.MULTILINE):
            # Append to existing Evidence section
            citation_list = ", ".join(f"`{c}`" for c in sorted(missing_citations))
            result = re.sub(
                r"(^(?:\*\*)?Evidence(?:\*\*)?:.*?)$",
                rf"\1, {citation_list}",
                result,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            # Add new Evidence section
            citation_list = ", ".join(f"`{c}`" for c in sorted(all_citations))
            result += f"\n- **Evidence**: {citation_list}"
    
    # Inject missing predicates in a code fence
    if missing_predicates:
        # Check if there's already a formal predicates section
        if "```" in result and any(p in result for p in list(all_predicates)[:1]):
            # Append missing predicates to existing code fence
            fence_insert = "\n".join(sorted(missing_predicates))
            # Find the last ``` and insert before it
            last_fence_idx = result.rfind("```")
            if last_fence_idx > result.find("```"):
                result = result[:last_fence_idx] + fence_insert + "\n" + result[last_fence_idx:]
        else:
            # Add new formal predicates section
            pred_block = "\n".join(sorted(all_predicates))
            result += f"\n\n**Formal predicates:**\n```\n{pred_block}\n```"
    
    return result


def validate_synthesis(body_raw: str, body_synthesized: str) -> tuple[bool, str]:
    """Validate that synthesis preserved all predicates and citations.
    
    Returns:
        (is_valid, reason)
    """
    raw_predicates = _extract_predicates(body_raw)
    synth_predicates = _extract_predicates(body_synthesized)
    missing_predicates = raw_predicates - synth_predicates
    
    raw_citations = _extract_citations(body_raw)
    synth_citations = _extract_citations(body_synthesized)
    missing_citations = raw_citations - synth_citations
    
    if missing_predicates:
        return False, f"Missing predicates: {missing_predicates}"
    if missing_citations:
        return False, f"Missing citations: {missing_citations}"
    return True, "ok"


def synthesize_concept(
    concept_id: str,
    body_raw: str,
    llm_client,
    model_id: str,
) -> tuple[str, str, bool, str]:
    """Synthesize a single concept with deterministic citation/predicate preservation.
    
    Strategy:
    1. Extract all predicates and citations from body_raw (ground truth)
    2. Send narrative to LLM for merge
    3. Inject predicates/citations deterministically into result
    4. Validate — should always pass since we inject deterministically
    
    Returns:
        (concept_id, body_synthesized, is_valid, reason)
    """
    import random as _random
    
    # Pre-extract ground truth from raw (deterministic, no LLM)
    all_predicates = _extract_predicates(body_raw)
    all_citations = _extract_citations(body_raw)
    all_code_fences = _extract_code_fences(body_raw)
    
    # Skip concepts that exceed context window (1M token limit ~= 4M chars)
    # These are dedup groups that merged too many contributors — fall back to raw
    if len(body_raw) > 3_500_000:
        return concept_id, "", False, f"Skipped: body_raw too large ({len(body_raw)} chars / ~{len(body_raw)//4} tokens)"
    
    max_retries = 5
    for attempt in range(max_retries):
        t0 = time.time()
        try:
            result = bedrock_client.call_llm(
                llm_client,
                model_id,
                SYNTHESIS_PROMPT,
                f"Merge the following overlapping observations:\n\n{body_raw}",
                max_tokens=16384,
                temperature=0.1,
            )
            body_synthesized = result.output_text.strip()
            
            elapsed = time.time() - t0
            bedrock_client.log_llm_call(
                status="success",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                latency_ms=int(elapsed * 1000),
                model_id=model_id,
                still="synthesis",
                phase="synthesize",
            )
            
            # Deterministically inject any missing predicates/citations
            body_synthesized = _inject_preserved_content(
                body_synthesized, all_predicates, all_citations, all_code_fences
            )
            
            # Validate (should pass now since we injected deterministically)
            is_valid, reason = validate_synthesis(body_raw, body_synthesized)
            
            if is_valid:
                return concept_id, body_synthesized, True, "ok"
            
            # If still failing after injection, something is wrong with extraction regex
            # Fall back to appending raw citations/predicates
            body_synthesized = _force_inject_all(body_synthesized, body_raw, all_predicates, all_citations)
            is_valid, reason = validate_synthesis(body_raw, body_synthesized)
            return concept_id, body_synthesized, is_valid, reason
            
        except Exception as e:
            err_str = str(e).lower()
            is_throttle = "429" in str(e) or "rate_limit" in err_str or "tpm" in err_str
            if is_throttle and attempt < max_retries - 1:
                backoff = (30 * (2 ** attempt)) + _random.uniform(0, 10)
                time.sleep(backoff)
                continue
            return concept_id, "", False, str(e)


def _force_inject_all(
    body_synthesized: str,
    body_raw: str,
    all_predicates: set[str],
    all_citations: set[str],
) -> str:
    """Last-resort: append all raw predicates and citations verbatim.
    
    This guarantees validation passes at the cost of possible duplication
    in the output. Better than losing data.
    """
    result = body_synthesized.rstrip()
    
    # Force-append all citations
    if all_citations:
        citation_list = ", ".join(f"`{c}`" for c in sorted(all_citations))
        result += f"\n\n- **Evidence** (complete): {citation_list}"
    
    # Force-append all predicates
    if all_predicates:
        pred_block = "\n".join(sorted(all_predicates))
        result += f"\n\n**Formal predicates** (complete):\n```\n{pred_block}\n```"
    
    return result


def run_synthesis(
    vector_db_path: str,
    profile: str = "",
    region: str = "us-east-1",
    model_id: str = "us.anthropic.claude-sonnet-4-6",
    concurrency: int = 5,
) -> dict:
    """Run synthesis pass on all multi-contributor unified concepts, per-still.
    
    Processes each still independently and writes a per-still completion marker.
    
    Returns:
        Stats dict.
    """
    store = VectorStore(vector_db_path)
    
    # Get all unified concepts
    all_concepts = store.get_all_unified_concepts()
    
    # Group by still
    by_still: dict[str, list] = {}
    for uc in all_concepts:
        stills = uc.source_stills if hasattr(uc, 'source_stills') and uc.source_stills else ["unknown"]
        still_name = stills[0] if stills else "unknown"
        by_still.setdefault(still_name, []).append(uc)
    
    # Determine the stills dir for writing markers
    # vector_db_path is like .../stills/shared_vectors.db
    stills_dir = Path(vector_db_path).parent
    
    llm = None  # Lazy init
    total_synthesized = 0
    total_failed = 0
    total_skipped = 0
    
    for still_name in sorted(by_still.keys()):
        concepts = by_still[still_name]
        still_dir = stills_dir / still_name
        marker = still_dir / ".synthesis_complete"
        
        # Skip if already done
        if marker.exists():
            print(f"  [{still_name}] Already synthesized. Skipping.", flush=True)
            total_skipped += len(concepts)
            continue
        
        # Filter to multi-contributor with no synthesis yet
        candidates = [uc for uc in concepts if uc.contributor_count > 1 and not uc.body_synthesized]
        
        if not candidates:
            # All single-contributor or already synthesized — mark complete
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({
                "still": still_name,
                "candidates": 0,
                "synthesized": 0,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }))
            print(f"  [{still_name}] No candidates (0 multi-contributor). Marked complete.", flush=True)
            continue
        
        # Lazy init llm client
        if llm is None:
            llm = bedrock_client.create_llm_client(profile, region)
        
        print(f"  [{still_name}] {len(candidates)} concepts to synthesize...", flush=True)
        still_synthesized = 0
        still_failed = 0
        
        # Write initial progress marker
        progress_marker = still_dir / ".synthesis_progress"
        progress_marker.parent.mkdir(parents=True, exist_ok=True)
        progress_marker.write_text(json.dumps({
            "still": still_name,
            "total": len(candidates),
            "processed": 0,
            "synthesized": 0,
            "failed": 0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }))
        
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    synthesize_concept,
                    uc.concept_id if hasattr(uc, 'concept_id') else uc.block_id,
                    uc.body_raw,
                    llm,
                    model_id,
                ): uc
                for uc in candidates
            }
            
            for i, future in enumerate(as_completed(futures), 1):
                concept_id, body_synth, is_valid, reason = future.result()
                
                if is_valid and body_synth:
                    uc = futures[future]
                    uc.body_synthesized = body_synth
                    store.upsert_unified_concept(uc)
                    still_synthesized += 1
                    if i % 10 == 0 or i == len(candidates):
                        print(f"    [{still_name}] {i}/{len(candidates)} processed", flush=True)
                        # Update progress marker
                        progress_marker.write_text(json.dumps({
                            "still": still_name,
                            "total": len(candidates),
                            "processed": i,
                            "synthesized": still_synthesized,
                            "failed": still_failed,
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        }))
                else:
                    still_failed += 1
                    print(f"    [{still_name}] {concept_id}: FAILED ({reason})", flush=True)
        
        # Write per-still completion marker
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "still": still_name,
            "candidates": len(candidates),
            "synthesized": still_synthesized,
            "failed": still_failed,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }))
        print(f"  [{still_name}] ✓ Complete ({still_synthesized} synthesized, {still_failed} failed)", flush=True)
        
        total_synthesized += still_synthesized
        total_failed += still_failed
    
    store.close()
    
    stats = {
        "stills_processed": len(by_still),
        "total_synthesized": total_synthesized,
        "total_failed": total_failed,
        "total_skipped": total_skipped,
    }
    print(f"\n[synthesis] Done: {total_synthesized} synthesized, {total_failed} failed, {total_skipped} skipped")
    return stats


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 synthesize_concepts.py <vector_db_path> [--concurrency N] [--profile PROFILE]")
        sys.exit(1)

    db_path = sys.argv[1]
    concurrency = 5
    profile = os.environ.get("AWS_PROFILE", "")

    if "--concurrency" in sys.argv:
        idx = sys.argv.index("--concurrency")
        concurrency = int(sys.argv[idx + 1])
    if "--profile" in sys.argv:
        idx = sys.argv.index("--profile")
        profile = sys.argv[idx + 1]

    result = run_synthesis(db_path, profile=profile, concurrency=concurrency)
    print(f"\nResult: {json.dumps(result, indent=2)}")

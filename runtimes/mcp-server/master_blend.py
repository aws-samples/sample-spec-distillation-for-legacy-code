#!/usr/bin/env python3
"""
master_blend.py — Produce the Master Blend from knowledge graph outputs.

Architecture Decision: Why LLM Here But Not in Reconciliation
─────────────────────────────────────────────────────────────
The master blend is fundamentally a synthesis task — it needs to combine
concepts from ALL stills into a coherent narrative. Unlike reconciliation
(which is structural analysis: "which concepts overlap? which conflict?"),
the master blend requires writing quality: organizing heterogeneous findings
into a unified document, prioritizing by business importance, resolving
structural overlaps into flowing prose, and making editorial decisions about
grouping and emphasis. This is a legitimate LLM task that cannot be reduced
to graph traversal alone.

The approach is hybrid:
- Phase 1 (PREPARE): Deterministic. Assembles structured input from the
  knowledge graph, organized by blend section. Deduplicates across stills,
  prioritizes by convergence score, and truncates to fit within context
  budgets. No LLM involvement — pure data pipeline.
- Phase 2 (SYNTHESIZE): LLM-powered. Sends prepared sections to Bedrock
  for narrative synthesis. One call per section (11 sections × 1 call each),
  parallelizable. The LLM's job is writing, not analysis — all analytical
  work (dedup, convergence scoring, cross-referencing) is already done by
  the knowledge layer and reconciliation.

This keeps LLM usage minimal and targeted: the LLM never sees raw source
code, never does deduplication, never does cross-referencing. It only writes
prose from pre-organized, pre-deduplicated concept blocks.

Usage:
    python3 master_blend.py <stills_dir> <reconciliation_dir> <output_dir>
"""

import json
import re
import sys
import time
from pathlib import Path

import bedrock_client


# ── Blend Section Definitions ────────────────────────────────────────────────

# Each section maps to: title, which block_types to draw from, which stills to prioritize
BLEND_SECTIONS = [
    {
        "id": "01_architecture_principles",
        "title": "Architecture Principles",
        "description": "Foundational non-negotiable truths with highest convergence across stills.",
        "sources": {"types": ["rule", "invariant"], "stills": ["data", "rule", "interface"]},
        "priority": "convergence",  # prioritize concepts seen in multiple stills
    },
    {
        "id": "02_business_object_model",
        "title": "Business Object Model",
        "description": "Entities, relationships, cardinalities, and key invariants.",
        "sources": {"types": ["entity"], "stills": ["data", "domain_language"]},
        "priority": "confidence",
    },
    {
        "id": "03_state_machines",
        "title": "State Machine Specifications",
        "description": "Full transition tables with guards for all entity lifecycles.",
        "sources": {"types": ["entity", "state"], "stills": ["state", "data"]},
        "priority": "confidence",
    },
    {
        "id": "04_workflow_sequences",
        "title": "Workflow Sequences",
        "description": "Ordered steps with pre/postconditions for business processes.",
        "sources": {"types": ["workflow", "step", "event"], "stills": ["workflow", "state"]},
        "priority": "confidence",
    },
    {
        "id": "05_business_rules",
        "title": "Business Rules",
        "description": "Formal predicates: complete registry with Where sections.",
        "sources": {"types": ["rule", "invariant"], "stills": ["rule", "data"]},
        "priority": "confidence",
    },
    {
        "id": "06_edge_cases",
        "title": "Edge Cases and Operational Learning",
        "description": "Exceptions that encode non-obvious knowledge.",
        "sources": {"types": ["exception", "finding", "observation"], "stills": ["exception", "rule"]},
        "priority": "confidence",
    },
    {
        "id": "07_protected_behaviors",
        "title": "Protected Behaviors / Acceptance Criteria",
        "description": "What any reimplementation must recover — the invariants that define correctness.",
        "sources": {"types": ["invariant", "rule"], "stills": ["test", "rule", "data"]},
        "priority": "convergence",
    },
    {
        "id": "08_interface_contracts",
        "title": "Interface Contract Summary",
        "description": "External surfaces, access levels, obligations.",
        "sources": {"types": ["interface", "entity"], "stills": ["interface"]},
        "priority": "confidence",
    },
    {
        "id": "09_authorization_model",
        "title": "Authorization Model",
        "description": "Actors, roles, boundaries, permission structure.",
        "sources": {"types": ["permission", "boundary", "entity"], "stills": ["permission", "data"]},
        "priority": "confidence",
    },
    {
        "id": "10_gaps_and_open_questions",
        "title": "Known Gaps and Open Questions",
        "description": "Areas where specification is incomplete or uncertain.",
        "sources": {"types": ["finding", "observation"], "stills": []},  # all stills
        "priority": "low_confidence",  # prioritize low-confidence findings
    },
    {
        "id": "11_confidence_map",
        "title": "Confidence Map and Methodology",
        "description": "Per-domain confidence scores and the methodology used.",
        "sources": {"types": [], "stills": []},  # meta section, uses reconciliation
        "priority": "meta",
    },
]


# ── Phase 1: Prepare ─────────────────────────────────────────────────────────

MAX_SECTION_CHARS = 200000  # ~50K tokens per section input (fits in context)


def prepare_blend_inputs(stills_dir: str, reconciliation_dir: str) -> list[dict]:
    """Assemble structured input for each blend section from knowledge graphs.

    Reads all per-still materialized files and reconciliation data,
    organizes by section, deduplicates, and truncates to fit context.

    Returns list of {section_id, title, description, content} dicts.
    """
    stills_path = Path(stills_dir)
    recon_path = Path(reconciliation_dir)

    # Load reconciliation report
    recon_md = ""
    recon_file = recon_path / "reconciliation.md"
    if recon_file.exists():
        recon_md = recon_file.read_text(encoding="utf-8")

    # Discover all materialized .md files per still
    # Prefer formatted/ subdirectory (polished), fall back to raw manifest files
    still_files: dict[str, dict[str, str]] = {}  # still → {filename → content}
    for still_dir in sorted(stills_path.iterdir()):
        if not still_dir.is_dir():
            continue
        still_name = still_dir.name
        # Look for final* directories with materialized files
        final_dirs = sorted(still_dir.glob("final*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not final_dirs:
            continue
        final_dir = final_dirs[0]  # latest

        # Prefer formatted/ if it exists and has content
        formatted_dir = final_dir / "formatted"
        source_dir = formatted_dir if formatted_dir.exists() and any(formatted_dir.glob("*.md")) else final_dir

        files = {}
        for md_file in source_dir.glob("*.md"):
            if md_file.name == "README.md":
                continue
            files[md_file.name] = md_file.read_text(encoding="utf-8")
        if files:
            still_files[still_name] = files

    print(f"  Loaded materialized files from {len(still_files)} stills")

    # Prepare each section
    prepared = []
    for section in BLEND_SECTIONS:
        content = _prepare_section(section, still_files, recon_md)
        prepared.append({
            "section_id": section["id"],
            "title": section["title"],
            "description": section["description"],
            "content": content,
        })

    return prepared


def _prepare_section(section: dict, still_files: dict, recon_md: str) -> str:
    """Prepare input content for a single blend section."""
    section_id = section["id"]
    source_types = section["sources"]["types"]
    source_stills = section["sources"]["stills"]

    # Special case: confidence map uses reconciliation only
    if section["priority"] == "meta":
        return recon_md[:MAX_SECTION_CHARS]

    # Collect relevant content from still files
    parts = []

    # Map manifest filenames to their likely section relevance
    type_to_files = {
        "entity": ["persistent_objects.md", "concept_registry.md", "contract_registry.md",
                   "lifecycle_registry.md", "role_registry.md", "exception_catalog.md",
                   "workflow_registry.md", "protected_behaviors.md"],
        "rule": ["business_invariants.md", "business_rule_catalog.md", "rule_dependencies.md",
                 "transition_specs.md", "capability_matrix.md", "override_map.md"],
        "invariant": ["business_invariants.md", "business_rule_catalog.md", "transition_specs.md",
                      "capability_matrix.md", "disposition.md"],
        "obligation": ["data_obligations.md", "obligation_assessment.md", "disposition.md"],
        "boundary": ["data_boundaries.md", "boundary_spec.md"],
        "interface": ["contract_registry.md", "capability_map.md"],
        "permission": ["role_registry.md", "capability_matrix.md", "boundary_spec.md"],
        "exception": ["exception_catalog.md", "override_map.md"],
        "workflow": ["workflow_registry.md", "workflow_dependencies.md"],
        "state": ["lifecycle_registry.md", "transition_specs.md"],
        "finding": ["coverage_gaps.md", "gap_analysis.md", "excluded_terms.md"],
        "observation": ["coverage_gaps.md", "gap_analysis.md"],
        "step": ["workflow_registry.md"],
        "event": ["cross_entity_dependencies.md", "workflow_dependencies.md"],
    }

    # Determine which stills to read from
    target_stills = source_stills if source_stills else list(still_files.keys())

    # Determine which files to read
    target_files = set()
    for btype in source_types:
        target_files.update(type_to_files.get(btype, []))

    # If no specific types, read everything (for gaps/findings sections)
    if not source_types:
        target_files = None  # read all

    # Collect content
    total_chars = 0
    for still_name in target_stills:
        if still_name not in still_files:
            continue
        files = still_files[still_name]
        for fname, content in files.items():
            if target_files is not None and fname not in target_files:
                continue
            if total_chars + len(content) > MAX_SECTION_CHARS:
                # Truncate this file to fit
                remaining = MAX_SECTION_CHARS - total_chars
                if remaining > 1000:
                    parts.append(f"<!-- From {still_name}/{fname} (truncated) -->\n{content[:remaining]}\n")
                    total_chars = MAX_SECTION_CHARS
                break
            parts.append(f"<!-- From {still_name}/{fname} -->\n{content}\n")
            total_chars += len(content)
        if total_chars >= MAX_SECTION_CHARS:
            break

    # Also read from non-prioritized stills if we have budget
    if total_chars < MAX_SECTION_CHARS:
        for still_name, files in still_files.items():
            if still_name in target_stills:
                continue
            for fname, content in files.items():
                if target_files is not None and fname not in target_files:
                    continue
                if total_chars + len(content) > MAX_SECTION_CHARS:
                    break
                parts.append(f"<!-- From {still_name}/{fname} -->\n{content}\n")
                total_chars += len(content)
            if total_chars >= MAX_SECTION_CHARS:
                break

    return "\n".join(parts)


# ── Phase 2: Synthesize ──────────────────────────────────────────────────────

BLEND_SYSTEM_PROMPT = """You are writing the opening of a section in a business specification
recovered from an existing software system. Write for someone who owns or must rebuild that
business area — not for someone studying how this document was produced.

Write 2-3 paragraphs that tell the reader WHAT THIS PART OF THEIR BUSINESS DOES: the obligations
it enforces, the decisions it governs, where the money or compliance risk sits, and which
behaviours a rebuild cannot change without breaking something real. Name the specific rules,
amounts, roles and states that matter. Lead with the most consequential one.

NEVER write about the document or how it was made. All of the following are forbidden:
- the words "still", "stills", "convergence", "distilled", "analysis pass", "master blend",
  "specification", "section", "this document", "entries", "KB"
- how many items the section has, how large it is, or how confident the analysis was
- what the reader should expect, how entries are structured, or how to read what follows
- internal identifiers (DT-0004, RL-0012) unless you are naming the rule they stand for, in
  which case state the rule in business terms instead

Do not begin with a markdown heading; one is added for you. Do not restate the content that
follows — it is passed through verbatim beneath what you write. If the material does not
support a confident statement, say less rather than padding.

Good: "An invoice cannot be paid while an unreleased hold sits on it, with one deliberate
exception: retainage withholds only the retained amount and leaves the remainder payable.
Only Risk Management can clear an insurance hold — not the Controller, not the CFO."

Bad: "This section establishes the foundational truths that emerged with the highest degree of
convergence across all distilled stills. With roughly 64 entries spanning 73 KB, it functions
as the bedrock layer of the master specification."
"""


def synthesize_sections(
    prepared: list[dict],
    profile: str = "",
    region: str = "us-east-1",
    model_id: str = "us.anthropic.claude-sonnet-4-6",
    concurrency: int = 3,
) -> list[dict]:
    """Produce master blend sections via culling + enrichment stitching.

    Two-pass approach:
    - Pass 1 (deterministic): The formatted still content IS the section body.
      No LLM rewriting. Content passes through verbatim.
    - Pass 2 (LLM): Generate brief section introductions (enrichment) that
      provide context and cross-references. One small LLM call per section.

    The LLM never sees the full content for rewriting — only a summary
    of it (first 2000 chars) to write the introduction.

    Args:
        prepared: List of {section_id, title, description, content} dicts.
        profile: AWS profile.
        region: AWS region.
        model_id: Bedrock model ID.
        concurrency: Max parallel calls.

    Returns:
        List of {section_id, title, output} dicts with section content.
    """
    llm = bedrock_client.create_llm_client(profile, region)
    results = []

    def generate_intro(section: dict) -> dict:
        """Generate a brief introduction for a section (LLM enrichment)."""
        content = section["content"]
        if not content.strip():
            return {
                "section_id": section["section_id"],
                "title": section["title"],
                "output": f"*No source material available for this section.*\n",
            }

        # Only send a summary to the LLM for the intro — not the full content
        content_preview = content[:3000]
        content_size = len(content)
        block_count = content.count("\n### ")

        # Size and entry count are deliberately NOT sent. Supplying them invites exactly the
        # document-about-the-document prose the system prompt forbids ("64 entries spanning
        # 73 KB"), and the reader does not care how big the section is.
        user_prompt = (
            f"BUSINESS AREA: {section['title']}\n"
            f"SCOPE: {section['description']}\n\n"
            f"RECOVERED MATERIAL (excerpt, for grounding only — do not summarise it):\n\n"
            f"{content_preview}\n\n"
            f"Write the opening for this business area: what it does, what it enforces, and "
            f"what a rebuild must not break. Name specifics. No headings, no meta-commentary."
        )

        for attempt in range(3):
            try:
                result = bedrock_client.call_llm(
                    llm, model_id, BLEND_SYSTEM_PROMPT, user_prompt, max_tokens=2048,
                )
                break
            except Exception as e:
                if attempt < 2:
                    import time as _t
                    _t.sleep(10 * (attempt + 1))
                    continue
                # On final failure, use a generic intro
                # Fallback when the model is unreachable (expired credentials, throttling).
                # States the scope and nothing else: the old text ("contains 64 entries ...
                # highest convergence across stills") leaked entry counts and pipeline
                # vocabulary into the customer-facing document on every failed call.
                result = type("R", (), {
                    "output_text": section["description"].strip().rstrip(".") + ".",
                    "input_tokens": 0, "output_tokens": 0})()
                break

        # Assemble: intro + full content (verbatim passthrough)
        # Strip leading H1/H2 that duplicates the section title
        content_clean = content
        section_title_lower = section["title"].lower().strip()
        for prefix in ["# ", "## ", "### "]:
            lines = content_clean.lstrip("\n").split("\n", 1)
            if lines and lines[0].startswith(prefix):
                heading_text = lines[0][len(prefix):].strip().lower()
                if heading_text == section_title_lower or heading_text in section_title_lower:
                    content_clean = lines[1] if len(lines) > 1 else ""
                    break

        # Demote headings in content: ## → ###, ### → ####
        # The section title is already ## so content H2s should be H3 (children)
        import re as _re
        content_clean = _re.sub(r'^#### ', '##### ', content_clean, flags=_re.MULTILINE)
        content_clean = _re.sub(r'^### ', '#### ', content_clean, flags=_re.MULTILINE)
        content_clean = _re.sub(r'^## ', '### ', content_clean, flags=_re.MULTILINE)
        content_clean = _re.sub(r'^# ', '### ', content_clean, flags=_re.MULTILINE)

        # The caller writes "## {title}" before this text (see write below), and the model
        # opens its intro with the same heading however firmly the prompt says not to. Left
        # alone that produces the section title twice in a row — the first thing a reader
        # sees. Strip any leading heading from the intro, whatever its level or wording:
        # the heading is the caller's job, so the intro should never carry one.
        intro = result.output_text.lstrip("\n")
        while intro.startswith("#"):
            first, _, rest = intro.partition("\n")
            if not first.lstrip("#").strip():
                break
            intro = rest.lstrip("\n")

        assembled = intro + "\n\n" + content_clean

        return {
            "section_id": section["section_id"],
            "title": section["title"],
            "output": assembled,
            "input_tokens": getattr(result, "input_tokens", 0),
            "output_tokens": getattr(result, "output_tokens", 0),
        }

    # Generate intros in parallel (small calls, fast)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    # Write progress marker
    output_path = Path(prepared[0]["content"]).parent if prepared else Path(".")
    # Derive output dir from where we'll write (caller provides it)
    blend_progress = None
    
    processed_count = 0
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(generate_intro, s): s for s in prepared}
        for future in as_completed(futures):
            section = futures[future]
            processed_count += 1
            try:
                result = future.result()
                results.append(result)
                print(f"    [{result['section_id']}] intro generated ({result.get('output_tokens', 0)} tokens), content {len(result['output'])/1024:.0f} KB [{processed_count}/{len(prepared)}]")
            except Exception as e:
                print(f"    [{section['section_id']}] FAILED: {e}")
                results.append({
                    "section_id": section["section_id"],
                    "title": section["title"],
                    "output": section["content"],  # passthrough on failure
                })

    results.sort(key=lambda r: r["section_id"])
    return results


# ── Phase 3: Assemble Output ─────────────────────────────────────────────────


def assemble_blend(results: list[dict], output_dir: str, target_name: str = "") -> Path:
    """Assemble all synthesized sections into the final master_blend.md.

    Args:
        results: List of {section_id, title, output} from synthesize_sections.
        output_dir: Directory to write master_blend.md.
        target_name: Optional target system name for the header.

    Returns:
        Path to the written file.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    blend_file = output_path / "master_blend.md"

    with open(blend_file, "w", encoding="utf-8") as f:
        header = f"# {target_name} — " if target_name else "# "
        # "Master Blend" is internal vocabulary (the whisky metaphor); it tells a reader
        # nothing. This document is the specification WITH its evidence retained — the
        # bottled spec is the same content with the evidence stripped — so name it for
        # that difference. The bottled spec inherits this title, which is why it has been
        # appearing as "Master Blend Specification" too.
        f.write(f"{header}Business Specification (with evidence)\n\n")
        f.write(f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  \n")
        f.write(f"**Sections:** {len(results)}  \n")
        # Pipeline architecture ("Knowledge graph extraction → … → LLM synthesis") used to be
        # stated here. It tells a reader nothing about their own system and is the first line
        # they see. How the document was produced belongs in the run manifest and telemetry,
        # which already record it with far more precision than a fixed sentence.
        f.write("**Recovered from:** source analysis of the existing implementation  \n\n")
        f.write("---\n\n")

        # Sections (no inline TOC — sidebar handles navigation)
        for r in results:
            f.write(f"## {r['title']}\n\n")
            f.write(r["output"])
            f.write("\n\n---\n\n")

    return blend_file


# ── Orchestrator ─────────────────────────────────────────────────────────────


def run_master_blend(
    stills_dir: str,
    reconciliation_dir: str,
    output_dir: str,
    target_name: str = "",
    profile: str = "",
    region: str = "us-east-1",
    model_id: str = "us.anthropic.claude-sonnet-4-6",
    concurrency: int = 3,
) -> dict:
    """Full master blend pipeline: prepare → synthesize → assemble.

    Args:
        stills_dir: Path to stills/ directory with knowledge graph outputs.
        reconciliation_dir: Path to reconciliation/ directory.
        output_dir: Path to write master_blend.md.
        target_name: Display name for the target system.
        profile: AWS profile for Bedrock.
        region: AWS region.
        model_id: Bedrock model ID.
        concurrency: Max parallel LLM calls.

    Returns:
        Stats dict.
    """
    print(f"[master_blend] Stills: {stills_dir}")
    print(f"[master_blend] Reconciliation: {reconciliation_dir}")
    print(f"[master_blend] Output: {output_dir}")

    output_p = Path(output_dir)
    output_p.mkdir(parents=True, exist_ok=True)
    progress_marker = output_p / ".master_blend_progress"

    # Phase 1: Prepare
    t0 = time.time()
    prepared = prepare_blend_inputs(stills_dir, reconciliation_dir)
    prep_time = time.time() - t0
    total_input_chars = sum(len(p["content"]) for p in prepared)
    print(f"  Prepared {len(prepared)} sections ({total_input_chars/1024:.0f} KB input) in {prep_time:.1f}s")

    # Write initial progress
    progress_marker.write_text(json.dumps({
        "phase": "synthesize",
        "total_sections": len(prepared),
        "processed": 0,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }))

    # Phase 2: Synthesize
    t0 = time.time()
    print(f"  Synthesizing {len(prepared)} sections (concurrency={concurrency})...")
    results = synthesize_sections(prepared, profile, region, model_id, concurrency)
    synth_time = time.time() - t0
    total_output_chars = sum(len(r["output"]) for r in results)
    total_in_tokens = sum(r.get("input_tokens", 0) for r in results)
    total_out_tokens = sum(r.get("output_tokens", 0) for r in results)
    print(f"  Synthesized in {synth_time:.0f}s ({total_in_tokens} in → {total_out_tokens} out tokens)")

    # Update progress
    progress_marker.write_text(json.dumps({
        "phase": "assemble",
        "total_sections": len(prepared),
        "processed": len(results),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }))

    # Phase 3: Assemble
    blend_file = assemble_blend(results, output_dir, target_name)
    file_size = blend_file.stat().st_size
    print(f"  Written: {blend_file} ({file_size/1024:.1f} KB)")

    # Remove progress marker (replaced by the actual output file)
    progress_marker.unlink(missing_ok=True)

    return {
        "sections": len(results),
        "input_chars": total_input_chars,
        "output_chars": total_output_chars,
        "input_tokens": total_in_tokens,
        "output_tokens": total_out_tokens,
        "prep_time_s": round(prep_time, 1),
        "synth_time_s": round(synth_time, 1),
        "output_file": str(blend_file),
        "output_size_kb": round(file_size / 1024, 1),
    }


# ── CLI Entry Point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python3 master_blend.py <stills_dir> <reconciliation_dir> <output_dir> [target_name]")
        print()
        print("Example:")
        print("  python3 master_blend.py \\")
        print("    output/stills \\")
        print("    output/reconciliation \\")
        print("    output/master_blend \\")
        print("    MySystem")
        sys.exit(1)

    stills = sys.argv[1]
    recon = sys.argv[2]
    out = sys.argv[3]
    name = sys.argv[4] if len(sys.argv) > 4 else ""

    result = run_master_blend(stills, recon, out, name)
    print(f"\nResult: {json.dumps(result, indent=2)}")

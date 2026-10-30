#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""viewer_api.py — the viewer's chat / validate / dependencies / translate logic, factored
out of serve_viewer.py so BOTH the standalone server (serve_viewer.py) and the launcher
(launcher_server.py per-run routes) share one implementation.

Each function takes already-parsed inputs plus the per-run paths; there is no HTTP / `self`
state here. The heavy imports (query_spec, knowledge, bedrock_client — which pull
torch/sentence-transformers) are DEFERRED inside the functions, exactly as serve_viewer.py
does them, so a bare `import viewer_api` (and therefore `import launcher_server`) stays
importable wherever torch is absent. Only the chat/validate/dependencies code paths touch the
model stack.
"""

import json
import os
import re
import sqlite3
from pathlib import Path

# Concept-symbol index cache, keyed by the resolved concept_symbols.json path so runs served by
# the same process (the launcher serves many) do not collide. Replaces serve_viewer.py's single
# ViewerHandler.symbol_index class attribute.
_SYMBOL_INDEX_CACHE: dict[str, dict] = {}


def chat(vector_db_path: str, question: str, top_k: int = 15) -> dict:
    """RAG query against a run's vector store.

    Returns the query_spec result dict, mapping ImportError / expired-credential /
    AccessDenied into {"error": ...} exactly as the viewer handler did, so the browser's
    r.json() never throws.
    """
    try:
        import query_spec as qs

        return qs.query_spec(
            question=question,
            vector_db_path=vector_db_path,
            top_k=top_k,
        )
    except ImportError as e:
        return {"error": f"Chat needs boto3 and sentence-transformers in this "
                         f"interpreter: {e}"}
    except Exception as e:
        name = type(e).__name__
        msg = str(e)
        if "expired" in msg.lower() or "AuthenticationError" in name:
            msg = ("AWS credentials have expired. Refresh them (for example `aws sso login "
                   "--profile <profile>`, or export new AWS_* environment variables) and ask again.")
        elif "AccessDenied" in name or "AccessDenied" in msg:
            msg = f"This AWS account cannot invoke the model: {msg}"
        return {"error": f"{name}: {msg}"}


def _find_siblings(vector_db_path, title, query_text, k=6, min_score=0.4):
    """Vector-search the corpus for blocks related to a concept.

    Returns (cross_refs, sibling_bodies): cross_refs is a list of
    {title, still, relevance, block_id} for the viewer; sibling_bodies is a
    list of formatted block texts for LLM context. Best-effort — returns
    ([], []) on any failure or if no vector DB is present.
    """
    cross_refs, sibling_bodies = [], []
    try:
        from knowledge import VectorStore, _get_embedding_model
        import numpy as np

        if not Path(vector_db_path).exists():
            return cross_refs, sibling_bodies

        model = _get_embedding_model()
        query_emb = model.encode([query_text])[0].astype(np.float32)
        store = VectorStore(vector_db_path)
        results = store.knn(query_emb, k=k)

        conn = sqlite3.connect(vector_db_path)
        current = title.lower().strip()
        for block_id, score in results:
            if score < min_score:
                continue
            row = conn.execute(
                'SELECT title, still_name, body FROM vectors WHERE block_id = ?',
                (block_id,)
            ).fetchone()
            if row and row[0].lower().strip() != current:
                cross_refs.append({
                    "title": row[0], "still": row[1],
                    "relevance": round(score, 2), "block_id": block_id,
                })
                sibling_bodies.append(f"### [{row[1]}] {row[0]}\n{(row[2] or row[0])[:2000]}")
        conn.close()
        store.close()
    except Exception:
        pass
    return cross_refs, sibling_bodies


def validate(vector_db_path: str, source_root: str, repo_url: str,
             block: str, title: str, evidence: str) -> dict:
    """Validate a block against its source evidence, or (when source is
    unavailable) against sibling blocks from the distilled corpus.

    Returns {"answer", "source_content", "title", "cross_refs", "evidence_refs"} or
    {"error": ...} on a missing field.

    Source-root containment (NEW hardening, not present in the original handler): each
    evidence ref is resolved under source_root and read only when it stays inside
    source_root. A traversal ref like ``../../../../etc/passwd`` is skipped / recorded
    not-found rather than read.
    """
    block_text = block
    evidence_str = evidence

    if not block_text or not evidence_str:
        return {"error": "Missing 'block' or 'evidence' field"}

    # Parse evidence references: "path#L{start}-L{end}" or single-line "path#L{n}"
    ref_matches = re.findall(r'([\w./\-]+\.[\w]+)#L(\d+)(?:-L(\d+))?', evidence_str)
    refs = [(p, s, (e if e else s)) for (p, s, e) in ref_matches]
    if not refs:
        # Bare file paths without line anchors
        refs_no_lines = re.findall(r'([\w./\-]+\.[\w]+)', evidence_str)
        refs = [(r, "1", "50") for r in refs_no_lines[:3]]

    # Attempt to fetch source content (only if a source root is configured). Every ref is
    # resolved and required to stay under source_root before it is read.
    source_parts = []
    source_found = False
    if source_root:
        root_resolved = Path(source_root).resolve()
        for filepath, start_line, end_line in refs[:5]:
            full_path = (Path(source_root) / filepath).resolve()
            if not full_path.is_relative_to(root_resolved):
                # Containment breach (e.g. "../../etc/passwd") — never read outside the
                # analysed source root. Record it as not-found, do not disclose the path.
                source_parts.append(f"--- {filepath}: FILE NOT FOUND (outside source root) ---")
                continue
            if full_path.exists():
                try:
                    lines = full_path.read_text(encoding="utf-8", errors="replace").split("\n")
                    start = max(0, int(start_line) - 1)
                    end = min(len(lines), int(end_line))
                    excerpt = "\n".join(lines[start:end])
                    source_parts.append(f"--- {filepath}#L{start_line}-L{end_line} ---\n{excerpt}")
                    source_found = True
                except Exception as e:
                    source_parts.append(f"--- {filepath}: ERROR reading: {e} ---")
            else:
                source_parts.append(f"--- {filepath}: FILE NOT FOUND at {full_path} ---")
    source_content = "\n\n".join(source_parts)

    # ── No source available → validate against sibling blocks in the corpus ──
    if not source_found:
        cross_refs, sibling_bodies = _find_siblings(
            vector_db_path, title, f"{title}\n{block_text[:800]}", k=6
        )
        if not sibling_bodies:
            return {
                "answer": ("Source evidence is not available locally and no related blocks "
                           "were found in the corpus to cross-reference against. Unable to validate."),
                "source_content": "", "title": title, "cross_refs": [],
            }
        sibling_context = "\n\n".join(sibling_bodies[:4])
        noref_prompt = f"""You are validating a specification distillation entry. The original source code is NOT available, so validate for INTERNAL CONSISTENCY and CORROBORATION against other entries distilled from the same system.

**Entry being validated:**
{block_text}

**Related entries from the same specification corpus:**
{sibling_context}

**Task:**
1. Is the entry internally coherent (definition, statement, rationale align)?
2. Do the related entries corroborate, contradict, or extend this entry's claims?
3. Are there claims that no related entry supports (possible over-reach)?
4. Do related entries reveal behavior this entry omits?

**Output format:**
- Verdict: CORROBORATED | PLAUSIBLE (limited corroboration) | CONTRADICTED | UNVERIFIABLE (source needed)
- Cite specific related entries by title where they support or conflict.
- State explicitly that this is a corroboration-based assessment WITHOUT source code, not ground-truth verification.
"""
        try:
            import bedrock_client
            llm = bedrock_client.create_llm_client(os.environ.get("AWS_PROFILE", ""), "us-east-1")
            result = bedrock_client.call_llm(
                llm, "us.anthropic.claude-sonnet-4-6",
                "You validate specification entries by cross-referencing related distilled entries when source code is unavailable. Be precise; never claim ground-truth verification without source.",
                noref_prompt, max_tokens=4096,
            )
            answer = result.output_text
        except Exception as e:
            answer = f"Cross-reference validation LLM call failed: {e}"
        return {
            "answer": answer, "source_content": "",
            "title": title, "cross_refs": cross_refs[:3],
        }

    # Send to LLM for validation
    validation_prompt = f"""You are validating a specification distillation entry against its source code evidence.

**Entry being validated:**
{block_text}

**Source code at evidence location(s):**
{source_content}

**Task:**
1. Does the Definition accurately describe what the code does?
2. Are the listed Aliases actually present in the code?
3. Are the Attributes accurately described?
4. Is the Business Rationale grounded in what the code does?
5. Are there claims in the entry NOT supported by the source evidence?
6. Is there important behavior in the source NOT captured by the entry?

**Output format:**
- Verdict: CONFIRMED | PARTIALLY SUPPORTED | DIVERGED
- For each field, state whether it's accurate, inaccurate, or unverifiable from this evidence.
- If diverged, explain specifically what differs.
"""
    try:
        import bedrock_client
        llm = bedrock_client.create_llm_client(os.environ.get("AWS_PROFILE", ""), "us-east-1")
        result = bedrock_client.call_llm(
            llm,
            "us.anthropic.claude-sonnet-4-6",
            "You validate specification entries against source code. Be precise and concise.",
            validation_prompt,
            max_tokens=4096,
        )
        answer = result.output_text
    except Exception as e:
        answer = f"Validation LLM call failed: {e}\n\nSource content fetched successfully:\n{source_content[:2000]}"

    # Two-pass validation: if first pass flags issues, search for sibling blocks
    # and re-validate with that context included.
    cross_refs = []
    uncaptured_indicators = ["uncaptured", "not captured", "omits", "missing", "not reflected", "not mentioned",
                              "partially supported", "diverged"]
    needs_second_pass = any(ind in answer.lower() for ind in uncaptured_indicators)

    if needs_second_pass:
        try:
            from knowledge import VectorStore, _get_embedding_model
            import numpy as np

            if Path(vector_db_path).exists():
                model = _get_embedding_model()
                search_text = f"{title} uncaptured behavior: {answer[-500:]}"
                query_emb = model.encode([search_text])[0].astype(np.float32)

                store = VectorStore(vector_db_path)
                results = store.knn(query_emb, k=5)

                conn = sqlite3.connect(vector_db_path)
                current_title_lower = title.lower().strip()
                sibling_bodies = []
                for block_id, score in results:
                    if score < 0.4:
                        continue
                    row = conn.execute(
                        'SELECT title, still_name, body FROM vectors WHERE block_id = ?',
                        (block_id,)
                    ).fetchone()
                    if row and row[0].lower().strip() != current_title_lower:
                        cross_refs.append({
                            "title": row[0],
                            "still": row[1],
                            "relevance": round(score, 2),
                            "block_id": block_id,
                        })
                        body_text = row[2] or row[0]
                        sibling_bodies.append(f"### [{row[1]}] {row[0]}\n{body_text[:2000]}")
                conn.close()
                store.close()

                # Second pass: re-validate with sibling context
                if sibling_bodies:
                    sibling_context = "\n\n".join(sibling_bodies[:3])
                    second_pass_prompt = f"""You are re-validating a specification entry. On the first pass, some behavior was flagged as uncaptured or missing.

However, the specification is organized into multiple blocks. Below are SIBLING BLOCKS from the same specification that may cover the flagged behavior.

**Entry being validated:**
{block_text}

**Source code at evidence location(s):**
{source_content}

**Related sibling blocks from the specification:**
{sibling_context}

**First-pass findings:**
{answer}

**Task:**
Re-evaluate the first-pass findings considering the sibling blocks. For each "uncaptured" or "missing" item:
- If a sibling block covers it, state: "Covered by: [sibling title]" and change verdict accordingly.
- If no sibling covers it, keep the original finding.

Update the overall verdict (CONFIRMED | PARTIALLY SUPPORTED | DIVERGED) based on the complete picture.
"""
                    try:
                        import bedrock_client
                        llm = bedrock_client.create_llm_client(os.environ.get("AWS_PROFILE", ""), "us-east-1")
                        result2 = bedrock_client.call_llm(
                            llm,
                            "us.anthropic.claude-sonnet-4-6",
                            "You validate specification entries against source code with cross-reference context. Be precise.",
                            second_pass_prompt,
                            max_tokens=4096,
                        )
                        answer = result2.output_text
                    except Exception:
                        pass  # Keep first-pass answer if second pass fails

        except Exception:
            pass  # Vector search/second pass is best-effort

    return {
        "answer": answer,
        "source_content": source_content[:3000],
        "title": title,
        "cross_refs": cross_refs[:3],
        "evidence_refs": [
            {
                "path": r[0],
                "start": r[1],
                "end": r[2],
                "url": f"{repo_url}{r[0]}#L{r[1]}-L{r[2]}" if repo_url else "",
            }
            for r in refs
        ],
    }


def dependencies(vector_db_path: str, title: str, still: str | None = None) -> dict:
    """Return code-dependency-derived prerequisites for a business concept.

    The concept-symbol index is loaded from ``Path(vector_db_path).parent /
    "concept_symbols.json"`` — i.e. ``<run>/stills/concept_symbols.json`` — exactly where the
    original handler looked. Returns the query_by_title result or {"error": ...}.
    """
    if not title:
        return {"error": "Missing 'title' field"}

    still = still or None

    # Lazily load the concept-symbol index, cached per resolved path so runs do not collide.
    sym_path = Path(vector_db_path).parent / "concept_symbols.json"
    cache_key = str(sym_path.resolve())
    symbol_index = _SYMBOL_INDEX_CACHE.get(cache_key)
    if symbol_index is None:
        if not sym_path.exists():
            return {"error": "concept_symbols.json not found — run concept_symbols.py"}
        try:
            with open(sym_path, encoding="utf-8") as f:
                symbol_index = json.load(f)
            _SYMBOL_INDEX_CACHE[cache_key] = symbol_index
        except Exception as e:
            return {"error": f"Failed to load symbol index: {e}"}

    try:
        import dependency_query as dq
        if not dq.code_graph_ready(vector_db_path):
            return {
                "error": "Code dependency graph not built yet. Run code_graph.py "
                         "(Phase 2.8) to populate code_edges/code_objects.",
            }
        return dq.query_by_title(vector_db_path, symbol_index, title, still=still)
    except Exception as e:
        return {"error": f"Dependency query failed: {e}"}


def translate(code: str, language: str) -> dict:
    """Translate a formal predicate block into a target programming language.

    Returns {"code", "language"} or {"error": ...}.
    """
    code_block = (code or "").strip()
    language = (language or "").strip()

    if not code_block or not language:
        return {"error": "Missing 'code' or 'language' field"}

    translate_prompt = (
        f"Translate the following formal specification predicate into idiomatic {language} code.\n\n"
        "Rules:\n"
        "- Produce a working, runnable code snippet — not pseudocode\n"
        "- Include type annotations/signatures where the language supports them\n"
        "- Add a brief comment explaining the business rule\n"
        "- Do NOT add imports unless essential to understanding\n"
        "- Do NOT wrap in a class unless the rule naturally requires one\n"
        "- Keep it minimal — only the logic that implements the predicate\n\n"
        f"Formal predicate:\n```\n{code_block}\n```\n\n"
        f"Output ONLY the {language} code in a fenced code block. No explanation outside the block."
    )

    try:
        import bedrock_client
        profile = os.environ.get("AWS_PROFILE", "")
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        model_id = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        llm = bedrock_client.create_llm_client(profile, region)
        result = bedrock_client.call_llm(
            llm, model_id,
            "You are a code translator. Output only code.",
            translate_prompt,
            max_tokens=4096,
        )
        return {"code": result.output_text, "language": language}
    except Exception as e:
        return {"error": str(e)}


def eval_summary(run_dir: str, extra_dirs: list | None = None, run_id: str | None = None,
                 known_keys: list | None = None, repo=None) -> dict:
    """The viewer Evaluation page's data (GET /api/eval): the run's evaluation artifacts,
    aggregated read-only by eval_view.aggregate into separate ``exact`` and ``estimated``
    sections. No model call. The launcher serves the same document per run at
    /api/runs/<id>/eval (adding its own eval jobs' outputs); this entry point is for the
    standalone serve_viewer.py, where the run directory is the viewer directory's parent."""
    import eval_view  # local import keeps `import viewer_api` light, like the other routes
    try:
        import budget
        spent = budget.spent_usd(Path(run_dir))
    except Exception:
        spent = None
    known = known_keys
    if known is None:
        try:
            from launcher_server import load_eval_catalog  # the server-side key list
            known = list(load_eval_catalog(eval_view.REPO)[1].values())
        except Exception:
            known = []
    return eval_view.aggregate(Path(run_dir), extra_dirs=extra_dirs or [], known_keys=known,
                               repo=Path(repo) if repo else eval_view.REPO,
                               spent_usd=spent, run_id=run_id or Path(run_dir).name)


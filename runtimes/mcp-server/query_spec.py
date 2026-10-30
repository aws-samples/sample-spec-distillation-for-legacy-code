#!/usr/bin/env python3
"""
query_spec.py — RAG query against the distilled specification.

Embeds a user question, retrieves top-K relevant concept blocks from the
vector store, then sends question + context to the LLM for a grounded answer.

Used by:
- MCP tool (query_spec) for IDE/agent access
- HTTP endpoint (serve_viewer.py) for browser chat panel

Dependencies: sentence-transformers, numpy, sqlite3
"""

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

import bedrock_client
from knowledge import _get_embedding_model


# ── Core RAG Function ────────────────────────────────────────────────────────

RAG_SYSTEM_PROMPT = """You answer questions about a software system, using only the
specification excerpts you are given.

LENGTH FOLLOWS THE QUESTION. This is the rule that matters most.
- A factual question ("who can release an insurance hold?") gets the answer in 1-3
  sentences, plus the formal predicate or cited rule that supports it. Nothing else.
- A DEFINITIONAL question ("what is retainage?", "what's a hold?") wants the concept, not
  its rulebook. Give 2-3 plain sentences explaining what it is and why the business cares.
  Then add AT MOST the two or three most consequential rules — ordered by how surprising
  or costly they are, not by ID — and offer to go deeper. A reader who asked "what is X"
  and received nine rules has to do the summarising you were asked to do.
- A question that genuinely spans many rules gets more, but never a tour of the
  specification's structure.
- A greeting or a question about you ("hello", "what can you do?") gets ONE short
  sentence offering to answer questions about the system, and nothing more. Do NOT
  inventory the specification, list its sections, enumerate ID ranges, or describe your
  own method — none of that answers the question that was asked.

Answering:
- Lead with the answer. No preamble, no restating the question.
- Quote the formal predicate (REQUIRE / PERMIT / ALLOW ...) when one exists — it is more
  precise than any paraphrase.
- Cite the concept block ID and title for each claim you make ABOUT THE SYSTEM. Do not
  attach citations to small talk.
- If several blocks agree, say so briefly — independent agreement is itself information —
  but do not repeat the same point once per block.
- Never describe HOW the specification was produced. "Independently confirmed across the
  business rules, data invariants and state-machine sections" tells the reader about the
  document; "three separate code paths enforce this, which is unusual" tells them about
  their system. Prefer the second, and only when it matters.

Honesty:
- Answer ONLY from the provided context. Never invent behaviour, and never fill a gap
  with what such a system "usually" does.
- If the context does not answer the question, say that plainly in one sentence and stop.
  An explicit "the specification does not cover this" is a useful answer; a plausible
  guess is not.
- If the context is self-contradictory, say so and show both sides rather than choosing.
"""


def query_spec(
    question: str,
    vector_db_path: str,
    top_k: int = 15,
    profile: str = "",
    region: str = "us-east-1",
    model_id: str = "us.anthropic.claude-sonnet-4-6",
) -> dict:
    """RAG query: embed question → retrieve → generate answer.

    Includes the full bottled spec as background context alongside
    the vector-retrieved fragments for comprehensive grounding.

    Args:
        question: User's natural language question.
        vector_db_path: Path to the shared SQLite vector store.
        top_k: Number of context blocks to retrieve.
        profile: AWS profile for Bedrock.
        region: AWS region.
        model_id: Bedrock model ID.

    Returns:
        Dict with answer, sources (block titles + stills), and token counts.
    """
    if not Path(vector_db_path).exists():
        return {"error": f"Vector store not found: {vector_db_path}"}

    # Load bottled spec as background context
    bottled_spec = ""
    db_dir = Path(vector_db_path).parent.parent  # stills/ → output/
    spec_candidates = [
        db_dir / "bottled_spec_final" / "bottled_spec.md",
        db_dir / "bottled_spec" / "bottled_spec.md",
    ]
    for sc in spec_candidates:
        if sc.exists():
            bottled_spec = sc.read_text(encoding="utf-8")
            break

    # Step 1: Embed the question
    model = _get_embedding_model()
    query_embedding = model.encode([question])[0].astype(np.float32)

    # Step 2: KNN retrieval
    conn = sqlite3.connect(vector_db_path)
    rows = conn.execute(
        "SELECT block_id, still_name, block_type, title, embedding, body FROM vectors"
    ).fetchall()
    conn.close()

    if not rows:
        return {"error": "Vector store is empty"}

    # Build matrix and compute similarities
    ids, stills, types, titles, vecs, bodies = [], [], [], [], [], []
    dim = len(query_embedding)
    for block_id, still, btype, title, emb_blob, body in rows:
        vec = np.frombuffer(emb_blob, dtype=np.float32)
        if len(vec) == dim:
            ids.append(block_id)
            stills.append(still)
            types.append(btype)
            titles.append(title)
            vecs.append(vec)
            bodies.append(body or "")

    matrix = np.array(vecs, dtype=np.float32)
    # Normalize
    q_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
    m_norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    m_norms[m_norms == 0] = 1e-10
    normed = matrix / m_norms

    similarities = normed @ q_norm
    top_indices = np.argsort(similarities)[::-1][:top_k]

    # Step 3: Retrieve full block bodies for top-K
    context_blocks = []
    sources = []
    for idx in top_indices:
        score = float(similarities[idx])
        if score < 0.15:  # Skip truly irrelevant matches
            break
        block_id = ids[idx]
        title = titles[idx]
        still = stills[idx]
        btype = types[idx]
        body = bodies[idx]
        # Include body content (truncate very long blocks to fit context)
        block_text = body[:2000] if body else title
        context_blocks.append(f"### {title}\nStill: {still} | Type: {btype} | Relevance: {score:.2f}\n\n{block_text}")
        sources.append({
            "block_id": block_id,
            "title": title,
            "still": still,
            "type": btype,
            "relevance": round(score, 3),
        })

    if not context_blocks and not bottled_spec:
        return {"answer": "No relevant content found in the specification for this question.", "sources": []}

    # Step 3.5: Enrich sources with GitHub links from source_refs table
    try:
        ref_conn = sqlite3.connect(vector_db_path)
        for src in sources:
            refs = ref_conn.execute(
                "SELECT file_path, url FROM source_refs WHERE block_id = ? LIMIT 3",
                (src["block_id"],),
            ).fetchall()
            if refs:
                src["source_files"] = [{"path": r[0], "url": r[1]} for r in refs]
        ref_conn.close()
    except Exception:
        pass  # source_refs table might not exist yet

    # Step 4: Generate answer
    context_text = "\n\n".join(context_blocks)
    user_prompt = f"QUESTION: {question}\n\n"
    if bottled_spec:
        user_prompt += f"FULL SPECIFICATION (background context):\n\n{bottled_spec}\n\n---\n\n"
    user_prompt += f"ADDITIONAL RELEVANT BLOCKS (from vector retrieval):\n\n{context_text}"

    llm = bedrock_client.create_llm_client(profile, region)
    result = bedrock_client.call_llm(
        llm, model_id, RAG_SYSTEM_PROMPT, user_prompt, max_tokens=8192
    )

    return {
        "answer": result.output_text,
        "sources": sources,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }

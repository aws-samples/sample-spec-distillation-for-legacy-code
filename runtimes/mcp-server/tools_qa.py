#!/usr/bin/env python3
"""
tools_qa.py — natural-language Q&A over a distilled specification.

THE ONLY MODEL-BACKED TOOL in this server. Everything else is a local read, so this is
the only one that costs money or needs AWS credentials. boto3 is imported inside the
tool body, not at module import, so the rest of the server still loads without it.

KNOWN LIMITATION: query_spec puts the WHOLE bottled spec into
every prompt (~40K tokens even on the small synthetic AP run), with no prompt caching
and no per-call budget. Cost grows with corpus size and will hit context limits on a
real ERP. The fix belongs in query_spec.py — retrieval-only context, cached system
prompt, max_tokens/budget parameters — not here.
"""
from __future__ import annotations

import graph_query
from spec_tooling import (AWS_DEFAULT_REGION, AWS_PROFILE, BEDROCK_MODEL_ID, err, ok,
                          quiet)


def register(server):
    @server.tool(
        description=(
            "Ask a natural-language question about the distilled specification and get a "
            "prose answer grounded in retrieved concepts with citations.\n\n"
            "COST: unlike the other tools, this one calls a model on every invocation and "
            "requires AWS credentials with Bedrock access. Prefer query_graph when a "
            "structured lookup would answer the question — it is free and its evidence is "
            "easier to verify. Use ask_spec for open or synthesising questions that span "
            "many concepts.\n\n"
            "Requires `run_dir` of a completed run."
        )
    )
    @quiet
    async def ask_spec(run_dir: str, question: str, top_k: int = 15) -> str:
        """Natural-language Q&A over the distilled specification."""
        try:
            paths = graph_query.resolve_paths(run_dir)
        except graph_query.RunNotUsable as e:
            return err(str(e), run_dir=run_dir)

        try:
            # Deferred: pulls in boto3 + sentence-transformers, which the read-only
            # tools deliberately do not require.
            import query_spec as qs

            result = qs.query_spec(
                question=question,
                vector_db_path=paths["vectors_db"],
                top_k=top_k,
                profile=AWS_PROFILE,
                region=AWS_DEFAULT_REGION,
                model_id=BEDROCK_MODEL_ID,
            )
        except ImportError as e:
            return err(
                f"ask_spec needs boto3 and sentence-transformers, which are not installed "
                f"({e}). The other tools in this server do not. Install them, or use "
                f"query_graph instead.",
                run_dir=paths["run_dir"],
            )
        except Exception as e:
            return err(f"{type(e).__name__}: {e}", run_dir=paths["run_dir"])

        return ok(result)

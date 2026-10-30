#!/usr/bin/env python3
"""
tools_graph.py — concept-graph tools: what do we know, and how well evidenced is it?

Pure reads over a completed run's SQLite store and knowledge_graph.json. No model
calls, no AWS credentials.
"""
from __future__ import annotations

import graph_query
from spec_tooling import err, ok, quiet


def register(server):
    @server.tool(
        description=(
            "Search the distilled specification's concept graph for business concepts, "
            "rules, invariants, workflows, states, and permissions recovered from a "
            "codebase. Use this FIRST when you need to know what a system does or what "
            "rules govern an area — it is free, fast, and needs no AWS credentials.\n\n"
            "Returns each concept with a `convergence` block: how many OTHER independent "
            "extraction passes ('stills') recovered the same fact, and their similarity "
            "scores. High convergence means several independent methods agreed, so the "
            "finding is well evidenced; convergence 0 means only one pass saw it and it "
            "deserves more scepticism. Results are ordered most-corroborated first.\n\n"
            "`contributor_count` > 1 means the concept is an aggregate of several merged "
            "observations rather than a single finding.\n\n"
            "Requires `run_dir`: the directory of a COMPLETED distillation run (the one "
            "containing run.json). Pass a `query` substring to filter by title, or omit "
            "it with a block_type to browse a category."
        )
    )
    @quiet
    async def query_graph(
        run_dir: str,
        query: str = "",
        block_type: str = "",
        still: str = "",
        limit: int = 10,
        include_body: bool = False,
    ) -> str:
        """Search the concept graph of a completed distillation run."""
        try:
            return ok(graph_query.query(
                run_dir=run_dir, query=query, block_type=block_type,
                still=still, limit=limit, include_body=include_body,
            ))
        except graph_query.RunNotUsable as e:
            return err(str(e), run_dir=run_dir)
        except Exception as e:
            return err(f"{type(e).__name__}: {e}", run_dir=run_dir)

    @server.tool(
        description=(
            "Get the full text and evidence of ONE concept by its concept_id, as returned "
            "by query_graph. Free; no AWS credentials needed."
        )
    )
    @quiet
    async def get_concept(run_dir: str, concept_id: str) -> str:
        """Fetch one concept's full body and evidence."""
        try:
            return ok(graph_query.get_concept(run_dir=run_dir, concept_id=concept_id))
        except graph_query.RunNotUsable as e:
            return err(str(e), run_dir=run_dir)
        except Exception as e:
            return err(f"{type(e).__name__}: {e}", run_dir=run_dir)

    @server.tool(
        description=(
            "Summarise what a distillation run contains: concept counts by type and by "
            "extraction still, graph size, and how many concepts were merged from multiple "
            "observations. Use to orient yourself before querying, or to confirm a "
            "`run_dir` is a usable completed run. Free; no AWS credentials needed."
        )
    )
    @quiet
    async def run_stats(run_dir: str) -> str:
        """Shape and size of a completed distillation run."""
        try:
            return ok(graph_query.stats(run_dir))
        except graph_query.RunNotUsable as e:
            return err(str(e), run_dir=run_dir)
        except Exception as e:
            return err(f"{type(e).__name__}: {e}", run_dir=run_dir)

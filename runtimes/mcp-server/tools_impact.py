#!/usr/bin/env python3
"""
tools_impact.py — blast radius: what depends on a concept, and what it depends on.

Wraps dependency_query over the factual code-dependency graph. Pure reads; no model
calls, no AWS credentials.
"""
from __future__ import annotations

import json
import os

import graph_query
from spec_tooling import err, ok, quiet


def register(server):
    @server.tool(
        description=(
            "Blast-radius analysis: what code a business concept depends on (`requires`) "
            "and what depends on it (`required_by`), traced through the factual "
            "code-dependency graph. Use before changing an area, to find what might "
            "break. Free; no AWS credentials needed.\n\n"
            "CRITICAL — read `resolved` before using the result:\n"
            "  resolved=true   requires/required_by are real answers (possibly empty "
            "lists, which genuinely means nothing was found).\n"
            "  resolved=false  the tool COULD NOT DETERMINE the answer. requires and "
            "required_by are null. This NEVER means 'nothing depends on it' — treat it "
            "as unknown and verify another way (query_graph, or read the source). The "
            "`reason` and `graph_scope` fields explain why it could not resolve.\n\n"
            "The dependency graph models cross-boundary structure — files, tables, "
            "stored procedures, triggers, classes — not intra-file call graphs. A concept "
            "whose evidence is a method or member will often not resolve.\n\n"
            "Requires `run_dir` of a completed run whose code-graph phase (2.8) ran."
        )
    )
    @quiet
    async def query_impact(run_dir: str, title: str, still: str = "") -> str:
        """Dependency blast radius for a business concept."""
        try:
            paths = graph_query.resolve_paths(run_dir)
        except graph_query.RunNotUsable as e:
            return err(str(e), run_dir=run_dir)

        symbols_json = paths["symbols_json"]
        if not os.path.isfile(symbols_json):
            return err(
                "concept_symbols.json not found — the concept-symbol bridge (phase 2.95) "
                "has not run for this output, so impact cannot be traced.",
                run_dir=paths["run_dir"], resolved=False,
                requires=None, required_by=None,
            )

        try:
            import dependency_query as dq

            if not dq.code_graph_ready(paths["vectors_db"]):
                return err(
                    "code dependency graph not built for this run — run code_graph.py "
                    "(phase 2.8) to populate code_objects/code_edges.",
                    run_dir=paths["run_dir"], resolved=False,
                    requires=None, required_by=None,
                )
            with open(symbols_json, encoding="utf-8") as f:
                sym_index = json.load(f)
            result = dq.query_by_title(paths["vectors_db"], sym_index, title,
                                       still=still or None)
        except Exception as e:
            return err(f"{type(e).__name__}: {e}", run_dir=paths["run_dir"],
                       resolved=False, requires=None, required_by=None)

        result["run_dir"] = paths["run_dir"]
        return ok(result)

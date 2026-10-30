# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Section 5 — machine-readable thresholds for CI, plus the human-readable markdown.

`thresholds()` flattens the whole result into a flat {name: number|null} map. A CI job gates on
names from that map with `--fail-under NAME=VALUE` (minimum) / `--fail-over NAME=VALUE` (maximum);
a null value never passes silently — it is reported as `unavailable` and, when gated on, fails.
"""
from __future__ import annotations

# name -> (direction, one-line meaning). direction "min" = higher is better.
THRESHOLD_DOC: dict[str, tuple[str, str]] = {
    "parser_confirmed_edge_precision_pct":
        ("min", "LLM code-graph edges confirmed by the deterministic parser (relation-typed)"),
    "parser_confirmed_pair_precision_pct":
        ("min", "LLM edges whose (file, target) pair the parser also connects"),
    "parser_edge_recall_pct":
        ("min", "parser edges present in the LLM graph (all languages)"),
    "parser_edge_recall_sql_pct":
        ("min", "parser edges present in the LLM graph, SQL source files only"),
    "parser_edge_recall_vbnet_pct":
        ("min", "parser edges present in the LLM graph, VB.NET source files only — the whole-layer "
                "blind-spot detector"),
    "parser_edge_recall_model_only_pct":
        ("min", "parser edges present in the LLM graph counting ONLY edges the extraction prompt "
                "proposed (parser-backfilled edges excluded, since scoring those against the "
                "parser is circular) — the honest measure of the prompt"),
    "parser_edge_recall_vbnet_model_only_pct":
        ("min", "as parser_edge_recall_vbnet_pct, model-proposed edges only"),
    "parser_method_call_recall_pct":
        ("min", "parser method->method `calls` edges present in the LLM graph (extended relation "
                "set, scored separately from the headline)"),
    "parser_method_call_precision_pct":
        ("min", "LLM method_call edges confirmed by the parser (extended relation set)"),
    "parser_edge_fp_not_in_parser":
        ("max", "LLM edges connecting a pair the parser does not connect at all"),
    "bridge_symbol_occurrence_pct":
        ("min", "bridge links whose symbol occurs verbatim in the cited file"),
    "bridge_link_assertion_pct":
        ("min", "bridge links whose OWN assertion holds: the symbol verbatim, or a resolved dotted "
                "qualifier whose joined identifier occurs in the cited file at identifier boundaries"),
    "bridge_symbol_occurrence_bare_pct":
        ("min", "bridge links whose joined bare name occurs in the cited file"),
    "bridge_substring_only_links":
        ("max", "bridge links whose joined name occurs in the cited file ONLY inside a longer "
                "identifier (`cboVendor` for `Vendor`) — a plausible but wrong file"),
    "bridge_qualifier_dropped_links":
        ("max", "bridge links that dropped a dotted qualifier without resolving it"),
    "bridge_links_dropped_by_the_join":
        ("max", "links the join REFUSED to emit because their assertion could not be verified "
                "(failing closed; counted from the bridge's own `dropped[]` ledger)"),
    "bridge_name_absent_links":
        ("max", "bridge links whose joined name is nowhere in the cited file"),
    "bridge_concept_resolution_pct":
        ("min", "bridge links whose concept key resolves to a node in the concept graph "
                "(after the documented title normalisations)"),
    "bridge_concept_exact_key_pct":
        ("min", "bridge links whose concept key matches a node title VERBATIM — i.e. the bridge and "
                "the concept graph share a key"),
    "bridge_unresolved_concept_keys":
        ("max", "distinct bridge concept keys that match no node under any spelling"),
    "code_object_anchor_pct": ("min", "code_objects that resolve to a file and occur in it"),
    "code_entity_anchor_pct": ("min", "code_entities that resolve to a file and occur in it"),
    "code_edge_anchor_pct": ("min", "non-dynamic code_edges whose dst token occurs in the src file"),
    "anchor_claims_contradicted":
        ("max", "rows the graph declares `confirmed` that our independent occurrence check fails "
                "— the sharpest regression signal, since the pipeline's own validator and this one "
                "must agree"),
    "source_file_resolve_pct": ("min", "code-graph source_files that exist under the source root"),
    "concept_orphan_pct": ("max", "concept nodes with no edge at all"),
    "code_object_orphan_pct": ("max", "code_objects with no incident code edge"),
    "dangling_edges": ("max", "concept edges with a missing endpoint"),
    "duplicate_node_ids": ("max", "repeated concept node ids"),
    "self_loop_edges": ("max", "concept edges from a node to itself"),
    "duplicate_edges": ("max", "repeated (source, target, type) concept edges"),
    "same_as_contradictions": ("max", "same_as classes spanning two stills or two unified concepts"),
    "contributes_to_cycles": ("max", "cycles in the CONTRIBUTES_TO grouping (must be a DAG)"),
    "unified_id_mismatches": ("max", "unified concept ids not reproducible from their contributors"),
    "key_item_coverage_any_pct": ("min", "key items with a concept citing at least one location"),
    "key_item_coverage_full_pct": ("min", "key items with a concept citing every location"),
    "key_items_missing": ("max", "key items with no concept citing any of their locations"),
    "key_location_recall_pct": ("min", "individual key locations cited by some concept"),
    "bridge_llm_agreement_pct": ("min", "sampled bridge links the LLM judge calls correct (--llm)"),
    "bridge_llm_agreement_ci95_low_pct": ("min", "lower bound of the bootstrap 95% CI (--llm)"),
    "hard_structural_failures": ("max", "total count of section-1 defects"),
}


def _get(d, *path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def thresholds(result: dict) -> dict:
    s = result.get("structural") or {}
    p = result.get("parity") or {}
    c = result.get("concept_coverage") or {}
    j = result.get("bridge_llm") or {}
    cg, br, cp, orph = (s.get("code_graph") or {}, s.get("bridge") or {},
                        s.get("concept_graph") or {}, s.get("orphans") or {})
    parity_ok = p.get("status") == "ok"
    out = {
        "parser_confirmed_edge_precision_pct": _get(p, "typed", "precision_pct") if parity_ok else None,
        "parser_confirmed_pair_precision_pct": _get(p, "pair", "precision_pct") if parity_ok else None,
        "parser_edge_recall_pct": _get(p, "typed", "recall_pct") if parity_ok else None,
        "parser_edge_recall_sql_pct": _get(p, "by_source_language", "sql", "recall_pct") if parity_ok else None,
        "parser_edge_recall_vbnet_pct": _get(p, "by_source_language", "vbnet", "recall_pct") if parity_ok else None,
        "parser_edge_recall_model_only_pct":
            _get(p, "by_provenance", "model_proposed", "core", "recall_pct") if parity_ok else None,
        "parser_edge_recall_vbnet_model_only_pct":
            _get(p, "by_source_language_model_proposed", "vbnet", "recall_pct") if parity_ok else None,
        "parser_method_call_recall_pct": _get(p, "extended", "recall_pct") if parity_ok else None,
        "parser_method_call_precision_pct": _get(p, "extended", "precision_pct") if parity_ok else None,
        "parser_edge_fp_not_in_parser": _get(p, "false_positives", "pair_not_in_parser") if parity_ok else None,
        "bridge_symbol_occurrence_pct": br.get("headline_symbol_occurrence_pct"),
        "bridge_link_assertion_pct": br.get("link_assertion_pct"),
        "bridge_symbol_occurrence_bare_pct": _get(br, "tiers", "bare_sub", "pct"),
        "bridge_substring_only_links": _get(br, "failure_classes", "substring_only",
                                            default=0) if br else None,
        "bridge_qualifier_dropped_links": _get(br, "failure_classes", "qualifier_dropped",
                                               default=0) if br else None,
        "bridge_links_dropped_by_the_join": br.get("dropped_by_the_join") if br else None,
        "bridge_name_absent_links": (_get(br, "failure_classes", "name_absent", default=0)
                                     + br.get("file_unresolved", 0)) if br else None,
        "bridge_concept_resolution_pct": _get(br, "concept_resolution", "links_pct"),
        "bridge_concept_exact_key_pct": _get(br, "concept_resolution", "links_exact_pct"),
        "bridge_unresolved_concept_keys": _get(br, "concept_resolution", "unresolved"),
        "code_object_anchor_pct": _get(cg, "code_objects", "pct"),
        "code_entity_anchor_pct": _get(cg, "code_entities", "pct"),
        "code_edge_anchor_pct": _get(cg, "code_edges", "pct"),
        "anchor_claims_contradicted":
            (_get(cg, "code_entities", "declared_confirmed_but_failed", default=0)
             + _get(cg, "code_edges", "declared_confirmed_but_failed", default=0)) if cg else None,
        "source_file_resolve_pct": _get(cg, "source_files", "pct"),
        "concept_orphan_pct": _get(cp, "orphans", "pct"),
        "code_object_orphan_pct": orph.get("pct"),
        "dangling_edges": _get(cp, "integrity", "dangling_edges"),
        "duplicate_node_ids": _get(cp, "ids", "duplicate_ids"),
        "self_loop_edges": _get(cp, "integrity", "self_loops"),
        "duplicate_edges": _get(cp, "integrity", "duplicate_edges"),
        "same_as_contradictions": _get(cp, "same_as", "contradictions"),
        "contributes_to_cycles": _get(cp, "contributes_to", "cycles"),
        "unified_id_mismatches": _get(cp, "unified_ids", "mismatched"),
        "key_item_coverage_any_pct": _get(c, "overall", "any_pct") if c.get("status") == "ok" else None,
        "key_item_coverage_full_pct": _get(c, "overall", "full_pct") if c.get("status") == "ok" else None,
        "key_items_missing": _get(c, "overall", "missing") if c.get("status") == "ok" else None,
        "key_location_recall_pct": _get(c, "location_recall", "pct") if c.get("status") == "ok" else None,
        "bridge_llm_agreement_pct": _get(j, "agreement", "point_pct"),
        "bridge_llm_agreement_ci95_low_pct": _get(j, "agreement", "ci95_low_pct"),
        "hard_structural_failures": s.get("hard_failures"),
    }
    return out


def evaluate_gates(values: dict, fail_under: dict, fail_over: dict) -> list[dict]:
    violations = []
    for name, want in sorted(fail_under.items()):
        got = values.get(name)
        if name not in values:
            violations.append({"metric": name, "rule": "fail-under", "threshold": want,
                               "actual": None, "why": "unknown metric name"})
        elif got is None:
            violations.append({"metric": name, "rule": "fail-under", "threshold": want,
                               "actual": None, "why": "metric unavailable in this run"})
        elif got < want:
            violations.append({"metric": name, "rule": "fail-under", "threshold": want,
                               "actual": got, "why": f"{got} < {want}"})
    for name, want in sorted(fail_over.items()):
        got = values.get(name)
        if name not in values:
            violations.append({"metric": name, "rule": "fail-over", "threshold": want,
                               "actual": None, "why": "unknown metric name"})
        elif got is None:
            violations.append({"metric": name, "rule": "fail-over", "threshold": want,
                               "actual": None, "why": "metric unavailable in this run"})
        elif got > want:
            violations.append({"metric": name, "rule": "fail-over", "threshold": want,
                               "actual": got, "why": f"{got} > {want}"})
    return violations


# ----------------------------------------------------------------------------- markdown
def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _table(rows, header) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_fmt(x) for x in r) + " |")
    return out


def to_markdown(r: dict) -> str:
    L: list[str] = ["# Knowledge-graph validation", ""]
    m = r.get("meta", {})
    L += [f"- Run: `{m.get('output_dir')}`",
          f"- Source root: `{m.get('source_root')}`",
          f"- Answer key: `{m.get('key') or '(none)'}`",
          f"- Provenance: **{_get(r, 'provenance', 'status')}** — {_get(r, 'provenance', 'detail')}",
          f"- graph_eval version: {m.get('version')}", ""]

    counts = r.get("counts") or {}
    if counts:
        L += ["## Artifacts", ""] + _table(sorted(counts.items()), ["Artifact", "Count"]) + [""]

    # headline
    p = r.get("parity") or {}
    L += ["## Headline — LLM code graph vs the deterministic parser", ""]
    if p.get("status") != "ok":
        L += [f"**skipped**: {p.get('reason')}", "", p.get("note", ""), ""]
    else:
        t, pr = p["typed"], p["pair"]
        core = p.get("core_relations") or []
        if core:
            L += [f"Core relation set (frozen, so the number stays comparable across runs): "
                  f"`{'`, `'.join(core)}`.", ""]
        L += _table([
            ("Edge precision (relation-typed)", f"**{_fmt(t['precision_pct'])}%**",
             f"{t['confirmed']}/{t['llm_edges']} LLM edges confirmed by the parser"),
            ("Edge recall (relation-typed)", f"**{_fmt(t['recall_pct'])}%**",
             f"{t['confirmed']}/{t['parser_edges']} parser edges present in the LLM graph"),
            ("Pair precision (relation label ignored)", f"{_fmt(pr['precision_pct'])}%",
             f"{pr['confirmed']}/{pr['llm_edges']}"),
            ("F1", f"{_fmt(t['f1_pct'])}%", ""),
        ], ["Metric", "Value", "Basis"]) + [""]
        bp = p.get("by_provenance") or {}
        if bp:
            mc = (bp.get("model_proposed") or {}).get("core") or {}
            me = (bp.get("model_proposed") or {}).get("extended") or {}
            L += ["### By provenance — what the PROMPT produced vs what the parser backfilled", "",
                  "A code_edge whose `symbol` starts with `facts:` was copied from "
                  "`<run>/facts/facts.json` by `code_graph.py`, so scoring it against "
                  "`facts/graph.json` is circular. Quote the model-only row when the question is "
                  "\"how good is the extraction prompt\".", ""]
            L += _table([
                ("core, model-proposed only", _fmt(mc.get("precision_pct")),
                 _fmt(mc.get("recall_pct")),
                 f"{mc.get('confirmed')}/{mc.get('llm_edges')} precision, "
                 f"{mc.get('confirmed')}/{mc.get('parser_edges')} recall"),
                ("extended (method_call), model-proposed only", _fmt(me.get("precision_pct")),
                 _fmt(me.get("recall_pct")),
                 f"{me.get('confirmed')}/{me.get('llm_edges')} precision, "
                 f"{me.get('confirmed')}/{me.get('parser_edges')} recall"),
            ], ["Population", "Precision %", "Recall %", "Basis"]) + [""]
            L += [f"- edges present ONLY because the parser backfilled them: "
                  f"{bp.get('parser_backfilled_edges')}",
                  f"- edges the model and the parser both produced: "
                  f"{bp.get('model_and_parser_agree_edges')}", ""]
        ext = p.get("extended") or {}
        if ext:
            L += ["### Extended relation set (scored separately from the headline)", ""]
            L += _table([(", ".join(ext.get("relations") or []), ext.get("llm_edges"),
                          ext.get("parser_edges"), ext.get("confirmed"),
                          _fmt(ext.get("precision_pct")), _fmt(ext.get("recall_pct")))],
                        ["Relation", "LLM", "Parser", "Confirmed", "Precision %", "Recall %"]) + [""]
            if ext.get("note"):
                L += [ext["note"], ""]
        L += ["### By relation", ""] + _table(
            [(k, v["llm_edges"], v["parser_edges"], v["confirmed"],
              _fmt(v["precision_pct"]), _fmt(v["recall_pct"]))
             for k, v in sorted(p["by_relation"].items())],
            ["Relation", "LLM", "Parser", "Confirmed", "Precision %", "Recall %"]) + [""]
        plm = p.get("by_source_language_model_proposed") or {}
        L += ["### By source language", ""] + _table(
            [(k, v["llm_edges"], v["parser_edges"], v["confirmed"],
              _fmt(v["precision_pct"]), _fmt(v["recall_pct"]),
              _fmt((plm.get(k) or {}).get("recall_pct")))
             for k, v in sorted(p["by_source_language"].items())],
            ["Language", "LLM", "Parser", "Confirmed", "Precision %", "Recall %",
             "Recall % (model only)"]) + [""]
        fp = p["false_positives"]
        L += [f"False positives: {fp['total']} "
              f"({fp['relation_mislabelled']} same pair under a different relation, "
              f"{fp['pair_not_in_parser']} pairs the parser does not connect at all)", ""]
        if fp["examples"]:
            L += _table([(e["src"], e["dst"], e["llm_relation"],
                          ", ".join(e["parser_relations_for_same_pair"]) or "—", e["class"])
                         for e in fp["examples"]],
                        ["Source file", "Target", "LLM relation", "Parser relation(s)", "Class"]) + [""]
        miss = p["missing_from_llm_graph"]
        L += [f"Missing from the LLM graph: {miss['total']} parser edges — "
              f"by language {miss['by_source_language']}, by relation {miss['by_relation']}", ""]
        sc = p["scope"]
        L += [f"Scope difference (not scored): {sc['llm_edges_not_comparable']} LLM edges the parser "
              f"has no relation for, {sc['parser_edges_excluded']} parser edges excluded by the "
              f"extraction prompt, {sc['parser_edges_not_representable']} parser edges the LLM graph "
              f"cannot express.", ""]

    # structural
    s = r.get("structural") or {}
    if s:
        cg, br, cp, orph = s["code_graph"], s["bridge"], s["concept_graph"], s["orphans"]
        L += ["## Structural integrity (deterministic)", "",
              f"Verdict: **{s['verdict']}** ({s['hard_failures']} hard failures)", "",
              "### Code-graph anchors", ""]
        L += _table([
            ("source_files resolve to a real file", cg["source_files"]["total"],
             cg["source_files"]["resolved_and_anchored"], _fmt(cg["source_files"]["pct"])),
            ("code_objects occur in their file", cg["code_objects"]["total"],
             cg["code_objects"]["resolved_and_anchored"], _fmt(cg["code_objects"]["pct"])),
            ("code_entities occur in their file", cg["code_entities"]["total"],
             cg["code_entities"]["resolved_and_anchored"], _fmt(cg["code_entities"]["pct"])),
            (f"code_edges dst occurs in src file ({cg['code_edges']['dynamic_exempt']} dynamic exempt)",
             cg["code_edges"]["checkable"], cg["code_edges"]["anchored"], _fmt(cg["code_edges"]["pct"])),
        ], ["Check", "Checked", "Passed", "%"]) + [""]
        contradicted = (cg["code_entities"].get("declared_confirmed_but_failed", 0)
                        + cg["code_edges"].get("declared_confirmed_but_failed", 0))
        L += [f"Rows the graph itself declares `confirmed` but that fail this independent check: "
              f"**{contradicted}**. Declared verdicts — entities "
              f"{cg['code_entities'].get('declared_verdicts')}, edges "
              f"{cg['code_edges'].get('declared_verdicts')}.", ""]
        for label, key in (("code_entities", "code_entities"), ("code_edges", "code_edges")):
            ex = cg[key].get("examples") or []
            if ex:
                L += [f"Failing {label}:", ""] + _table(
                    [(x.get("path") or x.get("src"), x.get("name") or x.get("dst"),
                      x.get("declared"), x.get("reason")) for x in ex],
                    ["File", "Name", "Declared", "Reason"]) + [""]

        L += ["### Bridge symbol occurrence", "",
              "Five strictnesses on the same question, so a failure is identifiable. "
              "`join_word` is the link's OWN assertion: the identifier it says is in the cited "
              "file, at identifier boundaries.", ""]
        L += _table([(k, v["passed"], v["failed"], _fmt(v["pct"]))
                     for k, v in br["tiers"].items()],
                    ["Tier", "Passed", "Failed", "%"]) + [""]
        L += [f"- symbol verbatim (`full_sub`): **{_fmt(br.get('headline_symbol_occurrence_pct'))}%**",
              f"- the link's own assertion holds: **{_fmt(br.get('link_assertion_pct'))}%** "
              f"(verbatim, or a RESOLVED qualifier whose joined identifier is really there)",
              f"- links carrying a `join_name`: {br.get('links_with_a_join_name')}/{br['links']}",
              ""]
        if br.get("dropped_by_the_join") is not None:
            L += [f"Links the join refused to emit (failing closed rather than citing a file it "
                  f"could not verify): **{br['dropped_by_the_join']}**"
                  + (f" — {br['dropped_reasons']}" if br.get("dropped_reasons") else ""), ""]
        if br.get("resolutions"):
            L += ["How each emitted link was resolved: "
                  + ", ".join(f"`{k}` {v}" for k, v in sorted(br["resolutions"].items())), ""]
        if br.get("flags"):
            L += ["Links kept but FLAGGED: "
                  + ", ".join(f"`{k}` {v}" for k, v in sorted(br["flags"].items())), ""]
        L += ["Failure classes: " + ", ".join(f"`{k}` {v}" for k, v in sorted(br["failure_classes"].items())), ""]
        crn = br.get("concept_resolution") or {}
        if crn:
            L += ["### Bridge — the concept end of the link", "",
                  "Both ends of the link now use the one canonical key defined in "
                  "`runtimes/mcp-server/concept_key.py` (the assigned `[DT-NNNN] ` id and the "
                  "leading `\"<Type>: \"` label stripped exactly once), so `exact` — the two "
                  "artifacts agreeing by equality — is the expected tier. "
                  "`id_stripped`/`canonical` mean a key in this run is NOT canonical (a run made "
                  "before the shared key, or a regression in whatever wrote it); `unresolved` is a "
                  "genuinely dangling endpoint.", ""]
            L += _table([(t, crn.get("by_tier_keys", {}).get(t, 0),
                          crn.get("by_tier_links", {}).get(t, 0))
                         for t in ("exact", "id_stripped", "canonical", "unresolved")],
                        ["Tier", "Distinct concept keys", "Links"]) + [""]
            L += [f"- resolved at all: {crn['resolved_to_a_graph_node']}/"
                  f"{crn['distinct_concept_keys']} keys ({_fmt(crn['links_pct'])}% of links)",
                  f"- share a key verbatim: {_fmt(crn['exact_pct'])}% of keys "
                  f"({_fmt(crn['links_exact_pct'])}% of links)", ""]
            if crn.get("examples"):
                L += [f"Unresolved concept keys (first {len(crn['examples'])}):", ""] \
                     + [f"- `{x}`" for x in crn["examples"]] + [""]
            if crn.get("title_drift_examples"):
                L += [f"Title drift (first {len(crn['title_drift_examples'])}):", ""] + _table(
                    [(x["concept_key"], x["matched_as"]) for x in crn["title_drift_examples"]],
                    ["Concept key in the bridge", "Matched as"]) + [""]
        L += ["The same rate under the denominators a reader may assume:", ""]
        L += _table([(k, v["n"], v["failures"], _fmt(v["pct"]))
                     for k, v in br["denominators"].items()],
                    ["Denominator", "n", "Failures", "Pass %"]) + [""]
        for cls, rows in sorted(br["failure_examples"].items()):
            if cls == "ok" or not rows:
                continue
            L += [f"**{cls}** (first {len(rows)}):", ""]
            L += _table([(x.get("symbol"), x.get("code_name"), x.get("code_path"), x.get("match"))
                         for x in rows], ["Symbol", "Joined to", "Cited file", "Match"]) + [""]

        L += ["### Concept graph", ""]
        L += _table([
            ("nodes / edges", f"{cp['nodes']} / {cp['edges']}"),
            ("unified concepts / observation blocks",
             f"{cp['unified_concepts']} / {cp['observation_blocks']}"),
            ("edge types", ", ".join(f"{k}={v}" for k, v in sorted(cp["edge_types"].items()))),
            ("duplicate node ids", cp["ids"]["duplicate_ids"]),
            ("ids with an unexpected shape", cp["ids"]["unexpected_shape"]),
            ("dangling edges", cp["integrity"]["dangling_edges"]),
            ("self-loops", cp["integrity"]["self_loops"]),
            ("duplicate edges", cp["integrity"]["duplicate_edges"]),
            ("orphan concepts (no edge)",
             f"{cp['orphans']['count']} ({_fmt(cp['orphans']['pct'])}%)"),
            ("same_as merge classes",
             f"{cp['same_as']['merge_classes']} covering {cp['same_as']['members']} nodes, "
             f"largest {cp['same_as']['largest_class']}"),
            ("same_as asserted / implied pairs",
             f"{cp['same_as']['asserted_pairs']} / {cp['same_as']['implied_pairs']} "
             f"({cp['same_as']['implied_not_asserted']} implied by transitivity but not asserted)"),
            ("same_as reciprocal pairs / longer cycles",
             f"{cp['same_as']['reciprocal_pairs']} / {cp['same_as']['cycles_longer_than_2']}"),
            ("same_as contradictions",
             f"{cp['same_as']['contradictions']} "
             f"({cp['same_as']['cross_still_classes']} cross-still, "
             f"{cp['same_as']['split_across_unified_concepts']} split across unified concepts)"),
            ("same_as classes mixing block types", cp["same_as"]["block_type_conflict_classes"]),
            ("CONTRIBUTES_TO is a DAG", cp["contributes_to"]["is_dag"]),
            ("CONTRIBUTES_TO wrong direction / multi-parent blocks",
             f"{cp['contributes_to']['wrong_direction']} / "
             f"{cp['contributes_to']['blocks_with_multiple_parents']}"),
            ("blocks with no unified concept",
             cp["contributes_to"]["blocks_without_unified_concept"]),
            ("unified ids reproducible from contributors",
             f"{cp['unified_ids']['reproducible']}/{cp['unified_ids']['checked']} "
             f"({_fmt(cp['unified_ids']['pct'])}%)"),
        ], ["Property", "Value"]) + [""]

        L += ["### Orphans", "",
              f"- orphan code objects: {orph['orphan_code_objects']}/{orph['code_objects']} "
              f"({_fmt(orph['pct'])}%)",
              f"- source files with no code edge: {orph['source_files_without_edges']}", ""]
        sr = s["source_root"]
        if sr["case_insensitive_path_collisions"]:
            L += ["> **case-insensitive filename collisions under the source root** — resolution is "
                  "refused for these, so any check citing them fails loudly:", ""]
            for g in sr["case_insensitive_path_collisions"]:
                L.append(f"> - {', '.join(g)}")
            L.append("")

    # concept coverage
    c = r.get("concept_coverage") or {}
    if c.get("status") == "ok":
        o = c["overall"]
        L += ["## Concept coverage vs the answer key (location overlap, deterministic)", "",
              f"- concept bodies from: `{c['concept_bodies_source']}`; "
              f"citation parser: `{c['citation_parser']}`",
              f"- {c['concepts_with_any_citation']}/{c['concepts_with_bodies']} concepts carry at "
              f"least one valid citation ({c['concept_citations']['valid']}/"
              f"{c['concept_citations']['total']} citations valid)", ""]
        L += _table([("key items", o["items"]), ("full (every location cited)",
                     f"{o['full']} ({_fmt(o['full_pct'])}%)"),
                     ("partial", f"{o['partial']} ({_fmt(o['any_pct'])}% any)"),
                     ("**missing (no concept cites any location)**", f"**{o['missing']}**"),
                     ("partial-credit", f"{_fmt(o['partial_credit_pct'])}%"),
                     ("key locations cited",
                      f"{c['location_recall']['covered']}/{c['location_recall']['key_locations']} "
                      f"({_fmt(c['location_recall']['pct'])}%)")],
                    ["Metric", "Value"]) + [""]
        for dim, title in (("by_type", "item type"), ("by_evidence_class", "evidence class"),
                           ("by_flag", "trap flag")):
            d = c.get(dim) or {}
            if len(d) > 1 or dim == "by_flag":
                L += [f"### Coverage by {title}", ""] + _table(
                    [(g, v["items"], v["full"], v["partial"], v["missing"],
                      _fmt(v["full_pct"]), _fmt(v["any_pct"])) for g, v in d.items()],
                    ["Group", "Items", "Full", "Partial", "Missing", "Full %", "Any %"]) + [""]
        if c["missing_items"]:
            L += ["### Key items with NO concept at any of their locations", ""]
            for x in c["missing_items"]:
                L.append(f"- **{x['id']}** ({x['type']}, {x['evidence']}"
                         + (f", flags {', '.join(x['flags'])}" if x["flags"] else "")
                         + f", {x['locations']} locations): {x['statement']}")
            L.append("")
    elif c:
        L += ["## Concept coverage vs the answer key", "", f"**skipped**: {c.get('reason')}", ""]

    # llm bridge
    j = r.get("bridge_llm") or {}
    if j.get("status") == "ok":
        a = j["agreement"] or {}
        L += ["## Bridge quality — bounded LLM sample (optional)", "",
              f"- model `{j['model']}` @ temperature {j['temperature']}, forced tool use, "
              f"schema-validated",
              f"- sample {j['judged']}/{j['sample_drawn']} judged "
              f"({j['calls']} calls, {j['cache_hits']} cache hits, ${j['cost_usd']:.4f})",
              f"- sampled links with no resolvable concept body: "
              f"{j.get('sampled_without_a_concept_body', 0)}",
              f"- verdicts: {j['verdicts']}",
              f"- **agreement {_fmt(a.get('point_pct'))}%** "
              f"(bootstrap 95% CI {_fmt(a.get('ci95_low_pct'))}–{_fmt(a.get('ci95_high_pct'))}%)", ""]
        bad = [x for x in j["rows"] if x.get("verdict") in ("too_generic", "incorrect")]
        if bad:
            L += ["Links the judge did not accept:", ""] + _table(
                [(x["verdict"], x.get("symbol"), x.get("code_name"), x.get("code_path"),
                  (x.get("reason") or "")[:90]) for x in bad],
                ["Verdict", "Symbol", "Joined to", "File", "Reason"]) + [""]
    elif j:
        L += ["## Bridge quality — bounded LLM sample", "", f"**{j.get('status')}**: "
              f"{j.get('reason', '')}", ""]

    # thresholds
    th = r.get("thresholds") or {}
    if th:
        L += ["## Thresholds (machine-readable; gate with --fail-under / --fail-over)", ""]
        L += _table([(k, _fmt(v), THRESHOLD_DOC.get(k, ("", ""))[0],
                      THRESHOLD_DOC.get(k, ("", ""))[1]) for k, v in th.items()],
                    ["Metric", "Value", "Better", "Meaning"]) + [""]
    v = r.get("gate_violations") or []
    if v:
        L += ["### Gate violations", ""] + _table(
            [(x["metric"], x["rule"], x["threshold"], _fmt(x["actual"]), x["why"]) for x in v],
            ["Metric", "Rule", "Threshold", "Actual", "Why"]) + [""]

    if r.get("notes"):
        L += ["## Notes", ""] + [f"- {n}" for n in r["notes"]] + [""]
    return "\n".join(L) + "\n"

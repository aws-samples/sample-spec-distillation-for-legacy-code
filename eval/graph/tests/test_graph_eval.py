# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for eval/graph_eval.py. No AWS, no network: Bedrock is injected as a fake client.

Run from the repo root:
    python3 -m pytest eval/graph/tests -q
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest_graph import (uc_id, write_run,  # noqa: E402  (tests dir on sys.path)
                      write_source)
from graph import coverage, judge, load, parity, report, structural  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parents[2]
CLI = EVAL_DIR / "graph_eval.py"


def _bundle(d, **kw):
    return load.load_bundle(d["run"], d["root"], **kw)


# ============================================================ happy path
def test_good_run_is_structurally_clean(good_run):
    b = _bundle(good_run)
    s = structural.check(b)
    assert s["verdict"] == "clean", s
    assert s["hard_failures"] == 0
    cg = s["code_graph"]
    assert cg["source_files"]["pct"] == 100.0
    assert cg["code_objects"]["pct"] == 100.0
    assert cg["code_entities"]["pct"] == 100.0
    assert cg["code_edges"]["pct"] == 100.0
    assert cg["code_edges"]["declared_confirmed_but_failed"] == 0
    cp = s["concept_graph"]
    assert cp["integrity"] == {"dangling_edges": 0, "self_loops": 0, "duplicate_edges": 0,
                               "dangling_examples": [], "self_loop_examples": [],
                               "duplicate_edge_examples": []}
    assert cp["unified_ids"]["mismatched"] == 0
    assert cp["contributes_to"]["is_dag"] is True
    assert cp["same_as"]["contradictions"] == 0
    assert cp["orphans"]["count"] == 0


def test_bridge_tiers_classify_a_dropped_qualifier(good_run):
    """'h.Vendor' is not in the file verbatim; the bare name 'Vendor' is. That is a dropped
    qualifier, not a wrong file — the classification must say so."""
    b = _bundle(good_run)
    br = structural.check(b)["bridge"]
    assert br["links"] == 3
    assert br["tiers"]["full_sub"]["passed"] == 2
    assert br["tiers"]["bare_sub"]["passed"] == 3
    assert br["failure_classes"] == {"ok": 2, "qualifier_dropped": 1}
    assert br["failure_classes"].get("name_absent", 0) == 0
    assert br["headline_symbol_occurrence_pct"] == pytest.approx(66.67, abs=0.01)


def test_concept_resolver_tiers():
    nodes = [{"id": "a" * 16, "title": "Data Invariant: Invoice Number Required and Bounded",
              "source_still": "data"},
             {"id": "b" * 16, "title": "Entity: Invoice Header", "source_still": "data"},
             {"id": "c" * 16, "title": "Rule: Posted Invoices Are Immutable",
              "source_still": "domain_language"}]
    r = load.ConceptResolver(nodes)
    assert r.resolve("data::Entity: Invoice Header") == ("exact", ["b" * 16])
    assert r.resolve("data::[DT-0037] Entity: Invoice Header") == ("id_stripped", ["b" * 16])
    # the formatter drops the "<Type>: " prefix as well as adding the id
    tier, ids = r.resolve("data::[DT-0006] Invoice Number Required and Bounded")
    assert (tier, ids) == ("canonical", ["a" * 16])
    # the still disambiguates two stills that canonicalise to the same title
    assert r.resolve("domain_language::[RL-0001] Posted Invoices Are Immutable")[1] == ["c" * 16]
    assert r.resolve("data::[DT-0099] Invented By Nobody") == ("unresolved", [])


def test_bridge_concept_keys_must_resolve_to_a_graph_node(good_run):
    """The concept end of a bridge link is an endpoint too — and the tier distinguishes a title that
    merely drifted between the two artifacts from a genuinely dangling reference."""
    d = good_run
    b = _bundle(d)
    crn = structural.check(b)["bridge"]["concept_resolution"]
    assert crn["unresolved"] == 0
    assert crn["links_pct"] == 100.0
    assert crn["exact_pct"] == 100.0

    bridge = json.loads((d["run"] / "knowledge_graph" / "concept_code_bridge.json").read_text())
    bridge["links"][0]["concept"] = "data::[DT-0021] Entity: Invoice Header"   # drift, resolvable
    bridge["links"][1]["concept"] = "data::[DT-0099] Renamed By Arbitration"   # unresolvable
    (d["run"] / "knowledge_graph" / "concept_code_bridge.json").write_text(json.dumps(bridge))
    s = structural.check(_bundle(d))
    crn = s["bridge"]["concept_resolution"]
    assert crn["by_tier_keys"] == {"id_stripped": 1, "unresolved": 1, "exact": 1}
    assert crn["title_drift"] == 1
    assert crn["unresolved"] == 1
    assert crn["examples"] == ["data::[DT-0099] Renamed By Arbitration"]
    assert crn["links_with_an_unresolved_concept"] == 1
    assert crn["links_exact_pct"] == pytest.approx(33.33, abs=0.01)
    assert s["hard_failures"] == 1
    th = report.thresholds({"structural": s})
    assert th["bridge_unresolved_concept_keys"] == 1
    assert th["bridge_concept_resolution_pct"] == pytest.approx(66.67, abs=0.01)


def test_parity_numbers_and_scope(good_run):
    b = _bundle(good_run)
    p = parity.compare(b)
    assert p["status"] == "ok"
    # 3 LLM edges, all confirmed; the parser also has usp_Dup -> InvHdr which the LLM lacks
    assert p["typed"]["confirmed"] == 3
    assert p["typed"]["precision_pct"] == 100.0
    assert p["typed"]["parser_edges"] == 4           # the #tmp read is excluded, not missed
    assert p["typed"]["recall_pct"] == 75.0
    assert p["scope"]["parser_edges_excluded"] == 1
    assert "temp table" in next(iter(p["scope"]["parser_excluded_by_reason"]))
    assert p["by_relation"]["table_read"]["recall_pct"] == 50.0
    assert p["by_relation"]["proc_exec"]["recall_pct"] == 100.0


def test_parity_separates_relation_mislabelling_from_fabrication(good_run):
    """An edge whose pair the parser connects under another relation is mislabelled, not invented."""
    d = good_run
    code = json.loads((d["run"] / "knowledge_graph" / "code_graph.json").read_text())
    code["code_edges"].append({
        "src": "proc/usp_Dup.sql", "dst": "dbo.invhdr", "type": "REFERENCES", "lang": "sql",
        "kind": "table", "operation": "update", "direction": "write", "resolution": "static",
        "symbol": None, "validated": 1, "dynamic": 0, "confidence": 0.9, "verdict": "confirmed",
        "direction_verdict": None, "derived_confidence": 0.9})
    (d["run"] / "knowledge_graph" / "code_graph.json").write_text(json.dumps(code))
    p = parity.compare(_bundle(d))
    fp = p["false_positives"]
    assert fp["total"] == 1
    assert fp["relation_mislabelled"] == 1
    assert fp["pair_not_in_parser"] == 0
    assert p["pair"]["precision_pct"] == 100.0        # the pair is right, the relation is not
    assert p["typed"]["precision_pct"] == 75.0


def test_concept_coverage_full_partial_missing(good_run):
    d = good_run
    c = coverage.concept_coverage(_bundle(d), d["key"])
    assert c["status"] == "ok"
    assert c["concept_bodies_source"] == "shared_vectors.db"
    by_id = {x["id"]: x for x in c["items"]}
    assert by_id["R-1"]["degree"] == "full"
    assert by_id["R-2"]["degree"] == "partial"        # usp_Dup.sql:4 is cited by nothing
    assert by_id["R-3"]["degree"] == "missing"
    assert c["overall"]["missing"] == 1
    assert [x["id"] for x in c["missing_items"]] == ["R-3"]
    assert c["by_flag"]["dead_code"]["missing"] == 1
    assert c["location_recall"]["key_locations"] == 4


# ============================================================ edge cases
def test_missing_knowledge_graph_dir(tmp_path):
    (tmp_path / "run").mkdir()
    with pytest.raises(load.ArtifactError) as e:
        load.load_bundle(tmp_path / "run", tmp_path / "run")
    assert "export_graph.py" in str(e.value)


def test_missing_knowledge_graph_dir_exits_2(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    r = subprocess.run([sys.executable, str(CLI), "--run", str(run),
                        "--source-root", str(run), "--out-dir", str(tmp_path / "o")],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "no exported knowledge graph" in r.stderr


def test_empty_graph_reports_nulls_not_passes(tmp_path):
    root = tmp_path / "src"
    write_source(root, {"a.sql": "SELECT 1\n"})
    run = write_run(tmp_path / "run")
    b = load.load_bundle(run, root)
    assert b.is_empty
    assert any("EMPTY" in n for n in b.notes)
    s = structural.check(b)
    assert s["hard_failures"] == 0
    assert s["code_graph"]["code_objects"]["pct"] is None     # null, not 100%
    assert s["bridge"]["headline_symbol_occurrence_pct"] is None
    th = report.thresholds({"structural": s, "parity": parity.compare(b)})
    assert th["bridge_symbol_occurrence_pct"] is None
    # gating on an unavailable metric must FAIL, not silently pass
    v = report.evaluate_gates(th, {"bridge_symbol_occurrence_pct": 90}, {})
    assert v and v[0]["why"] == "metric unavailable in this run"


def test_graph_referencing_absent_files(tmp_path):
    root = tmp_path / "src"
    write_source(root, {"a.sql": "SELECT 1 FROM T\n"})
    code = {"source_files": [{"path": "a.sql", "lang": "sql"}, {"path": "gone.sql", "lang": "sql"}],
            "code_objects": [{"path": "gone.sql", "object": "dbo.x", "kind": "table"}],
            "code_edges": [{"src": "gone.sql", "dst": "dbo.t", "type": "REFERENCES", "lang": "sql",
                            "kind": "table", "operation": "select", "direction": "read",
                            "dynamic": 0, "verdict": "confirmed"}],
            "code_entities": [{"path": "nope.sql", "code_name": "X", "anchor_verdict": "confirmed"}]}
    bridge = {"links": [{"concept": "c", "symbol": "X", "match": "code_entity",
                         "code_path": "nope.sql", "code_name": "X"}]}
    run = write_run(tmp_path / "run", code=code, bridge=bridge)
    s = structural.check(load.load_bundle(run, root))
    assert s["code_graph"]["source_files"]["file_unresolved"] == 1
    assert s["code_graph"]["code_objects"]["file_unresolved"] == 1
    assert s["code_graph"]["code_edges"]["src_unresolved"] == 1
    assert s["code_graph"]["code_entities"]["file_unresolved"] == 1
    assert s["bridge"]["file_unresolved"] == 1
    assert s["bridge"]["failure_classes"]["file_unresolved"] == 1
    assert s["hard_failures"] >= 5
    assert s["verdict"] == "defects found"


def test_cyclic_same_as_and_self_loops_and_duplicates(tmp_path):
    root = tmp_path / "src"
    write_source(root, {"a.sql": "SELECT 1\n"})
    a, bb, c = "1" * 16, "2" * 16, "3" * 16
    kg = {"nodes": [{"id": x, "title": f"T{x[0]}", "block_type": "entity", "source_still": "data"}
                    for x in (a, bb, c)],
          "edges": [
              {"source": a, "target": bb, "edge_type": "same_as"},
              {"source": bb, "target": a, "edge_type": "same_as"},      # reciprocal 2-cycle
              {"source": bb, "target": c, "edge_type": "same_as"},
              {"source": c, "target": a, "edge_type": "same_as"},       # 3-cycle
              {"source": a, "target": a, "edge_type": "same_as"},       # self-loop
              {"source": a, "target": bb, "edge_type": "same_as"},      # duplicate edge
              {"source": a, "target": "ghost", "edge_type": "references"},   # dangling
          ]}
    s = structural.check(load.load_bundle(write_run(tmp_path / "run", kg=kg), root))
    cp = s["concept_graph"]
    assert cp["same_as"]["reciprocal_pairs"] >= 1
    assert cp["same_as"]["cycles_longer_than_2"] >= 1
    assert cp["integrity"]["self_loops"] == 1
    assert cp["integrity"]["duplicate_edges"] == 1
    assert cp["integrity"]["dangling_edges"] == 1
    assert s["hard_failures"] >= 3


def test_same_as_contradictions_are_flagged(tmp_path):
    """Dedup is documented as within-still; a class spanning two stills, or splitting across two
    unified concepts, is a contradictory merge."""
    root = tmp_path / "src"
    write_source(root, {"a.sql": "SELECT 1\n"})
    a, bb = "1" * 16, "2" * 16
    ua, ub = f"uc_{a}", f"uc_{bb}"
    kg = {"nodes": [
        {"id": a, "title": "X", "block_type": "entity", "source_still": "data"},
        {"id": bb, "title": "X", "block_type": "rule", "source_still": "rule"},
        {"id": ua, "node_type": "unified_concept", "title": "X", "block_type": "entity"},
        {"id": ub, "node_type": "unified_concept", "title": "X", "block_type": "rule"},
    ], "edges": [
        {"source": a, "target": bb, "edge_type": "same_as"},
        {"source": a, "target": ua, "edge_type": "CONTRIBUTES_TO"},
        {"source": bb, "target": ub, "edge_type": "CONTRIBUTES_TO"},
    ]}
    cp = structural.check(load.load_bundle(write_run(tmp_path / "run", kg=kg), root))["concept_graph"]
    assert cp["same_as"]["cross_still_classes"] == 1
    assert cp["same_as"]["split_across_unified_concepts"] == 1
    assert cp["same_as"]["block_type_conflict_classes"] == 1
    assert cp["same_as"]["contradictions"] == 2


def test_contributes_to_must_be_a_dag_and_bipartite(tmp_path):
    root = tmp_path / "src"
    write_source(root, {"a.sql": "SELECT 1\n"})
    a, bb = "1" * 16, "2" * 16
    kg = {"nodes": [
        {"id": a, "title": "X", "block_type": "entity", "source_still": "data"},
        {"id": bb, "title": "Y", "block_type": "entity", "source_still": "data"},
    ], "edges": [
        {"source": a, "target": bb, "edge_type": "CONTRIBUTES_TO"},   # target not a unified concept
        {"source": bb, "target": a, "edge_type": "CONTRIBUTES_TO"},   # => cycle
    ]}
    cp = structural.check(load.load_bundle(write_run(tmp_path / "run", kg=kg), root))["concept_graph"]
    assert cp["contributes_to"]["is_dag"] is False
    assert cp["contributes_to"]["cycles"] >= 1
    assert cp["contributes_to"]["wrong_direction"] == 2


def test_unified_concept_id_must_be_reproducible(tmp_path):
    root = tmp_path / "src"
    write_source(root, {"a.sql": "SELECT 1\n"})
    a, bb = "1" * 16, "2" * 16
    kg = {"nodes": [
        {"id": a, "title": "X", "block_type": "entity", "source_still": "data"},
        {"id": bb, "title": "X", "block_type": "entity", "source_still": "data"},
        {"id": "uc_deadbeefdeadbeef", "node_type": "unified_concept", "title": "X",
         "block_type": "entity"},
    ], "edges": [
        {"source": a, "target": "uc_deadbeefdeadbeef", "edge_type": "CONTRIBUTES_TO"},
        {"source": bb, "target": "uc_deadbeefdeadbeef", "edge_type": "CONTRIBUTES_TO"},
    ]}
    cp = structural.check(load.load_bundle(write_run(tmp_path / "run", kg=kg), root))["concept_graph"]
    assert cp["unified_ids"]["mismatched"] == 1
    assert cp["unified_ids"]["examples"][0]["expected"] == uc_id([a, bb])


def test_orphans(tmp_path):
    root = tmp_path / "src"
    write_source(root, {"a.sql": "CREATE TABLE dbo.Lonely (x INT)\n"})
    kg = {"nodes": [{"id": "1" * 16, "title": "X", "block_type": "entity", "source_still": "d"},
                    {"id": "2" * 16, "title": "Y", "block_type": "entity", "source_still": "d"}],
          "edges": []}
    code = {"source_files": [{"path": "a.sql", "lang": "sql"}],
            "code_objects": [{"path": "a.sql", "object": "dbo.lonely", "kind": "table"}],
            "code_edges": [], "code_entities": []}
    s = structural.check(load.load_bundle(write_run(tmp_path / "run", kg=kg, code=code), root))
    assert s["concept_graph"]["orphans"]["count"] == 2
    assert s["concept_graph"]["orphans"]["pct"] == 100.0
    assert s["orphans"]["orphan_code_objects"] == 1
    assert s["orphans"]["source_files_without_edges"] == 1


def test_unicode_symbol_names(tmp_path):
    root = tmp_path / "src"
    write_source(root, {"ünïcode/Ünïcode.sql": "CREATE TABLE dbo.Fakturanummer (Überweisung INT)\n",
                        "tr.sql": "-- İstanbul column: Gürkan\nSELECT Gürkan FROM T\n"})
    code = {"source_files": [{"path": "ünïcode/Ünïcode.sql", "lang": "sql"}],
            "code_objects": [{"path": "ünïcode/Ünïcode.sql", "object": "dbo.Fakturanummer",
                              "kind": "table"}],
            "code_edges": [], "code_entities": [
                {"path": "ünïcode/Ünïcode.sql", "code_name": "Überweisung",
                 "business_name": "Bank transfer", "anchor_verdict": "confirmed"},
                {"path": "tr.sql", "code_name": "Gürkan", "business_name": "Owner",
                 "anchor_verdict": "confirmed"}]}
    bridge = {"links": [
        {"concept": "data::Entität: Überweisung", "symbol": "Überweisung", "match": "code_entity",
         "code_path": "ünïcode/Ünïcode.sql", "code_name": "Überweisung"},
        {"concept": "data::Entität", "symbol": "ÜBERWEISUNG", "match": "code_entity",
         "code_path": "ünïcode/Ünïcode.sql", "code_name": "Überweisung"},
        {"concept": "data::Yok", "symbol": "日本語", "match": "code_entity",
         "code_path": "tr.sql", "code_name": "日本語"}]}
    s = structural.check(load.load_bundle(write_run(tmp_path / "run", code=code, bridge=bridge),
                                          root))
    assert s["code_graph"]["code_objects"]["pct"] == 100.0
    assert s["code_graph"]["code_entities"]["name_absent"] == 0      # casefold matches Ü/ü
    assert s["bridge"]["tiers"]["full_sub"]["passed"] == 2           # the CJK symbol is absent
    assert s["bridge"]["failure_classes"]["name_absent"] == 1


def test_case_insensitive_filename_collision_is_refused_not_guessed(tmp_path):
    root = tmp_path / "src"
    write_source(root, {"dir/Proc.sql": "SELECT A FROM X\n", "dir/proc.sql": "SELECT B FROM Y\n"})
    if len(list((root / "dir").iterdir())) < 2:
        pytest.skip("filesystem is case-insensitive; the collision cannot be created here")
    code = {"source_files": [{"path": "dir/PROC.sql", "lang": "sql"}],
            "code_objects": [{"path": "dir/PROC.sql", "object": "dbo.x", "kind": "table"}],
            "code_edges": [], "code_entities": []}
    b = load.load_bundle(write_run(tmp_path / "run", code=code), root)
    assert [sorted(g) for g in b.src.case_collisions] == []      # differ by case, not identical
    rel, why = b.src.resolve("dir/PROC.sql")
    assert rel is None and "ambiguous" in why
    s = structural.check(b)
    assert s["code_graph"]["source_files"]["file_unresolved"] == 1
    assert s["source_root"]["basename_collision_count"] == 1


def test_case_collision_resolution_is_refused_filesystem_independent(tmp_path):
    """macOS/APFS is usually case-insensitive, so the on-disk version of this case is skipped there.
    Drive the index directly so the refusal behaviour is covered on every platform."""
    root = tmp_path / "src"
    write_source(root, {"dir/Proc.sql": "SELECT A FROM X\n"})
    idx = load.SourceIndex.build(root)
    # simulate two files whose relative paths differ only by case
    idx.files["dir/proc.sql"] = root / "dir" / "Proc.sql"
    idx.by_rel_ci["dir/proc.sql"] = ["dir/Proc.sql", "dir/proc.sql"]
    idx.by_base_ci["proc.sql"] = ["dir/Proc.sql", "dir/proc.sql"]
    assert [sorted(g) for g in idx.case_collisions] == [["dir/Proc.sql", "dir/proc.sql"]]
    rel, why = idx.resolve("dir/PROC.sql")
    assert rel is None and "case-insensitive collision" in why
    rel, why = idx.resolve("PROC.SQL")
    assert rel is None and "ambiguous basename" in why
    # an exact path still resolves — only the ambiguous spelling is refused
    assert idx.resolve("dir/Proc.sql") == ("dir/Proc.sql", "exact")


def test_facts_absent_skips_parity_with_a_clear_note(good_run):
    d = good_run
    (d["run"] / "facts" / "graph.json").unlink()
    (d["run"] / "facts" / "facts.json").unlink()
    p = parity.compare(_bundle(d))
    assert p["status"] == "skipped"
    assert "facts.json is absent" in p["reason"]
    th = report.thresholds({"structural": {}, "parity": p})
    assert th["parser_confirmed_edge_precision_pct"] is None


def test_facts_present_but_graph_missing_points_at_facts_post(good_run):
    (good_run["run"] / "facts" / "graph.json").unlink()
    p = parity.compare(_bundle(good_run))
    assert p["status"] == "skipped"
    assert "facts_post.py" in p["reason"]


def test_source_revision_mismatch_is_refused(good_run, tmp_path):
    d = good_run
    (d["root"] / "proc" / "usp_Post.sql").write_text("-- edited after the run\nSELECT 2\n")
    b = _bundle(d)
    assert b.provenance.status == "mismatch"
    assert b.provenance.refuse
    assert b.provenance.digest_mismatch[0]["path"] == "proc/usp_Post.sql"
    out = tmp_path / "o"
    r = subprocess.run([sys.executable, str(CLI), "--run", str(d["run"]),
                        "--source-root", str(d["root"]), "--out-dir", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "REFUSED" in r.stderr
    got = json.loads((out / "graph_eval.json").read_text())
    assert got["refused"] is True
    assert "structural" not in got            # nothing scored against the wrong tree
    # ...and the override scores anyway, saying so
    r2 = subprocess.run([sys.executable, str(CLI), "--run", str(d["run"]),
                         "--source-root", str(d["root"]), "--out-dir", str(out),
                         "--allow-revision-mismatch"], capture_output=True, text=True)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    got2 = json.loads((out / "graph_eval.json").read_text())
    assert any("MISMATCH" in n for n in got2["notes"])
    assert "structural" in got2


def test_internally_inconsistent_run_manifest_is_a_mismatch(good_run):
    d = good_run
    (d["run"] / "run_manifest.json").write_text(json.dumps(
        {"run_identity": {"source_inventory_hash": "0" * 64}}))
    b = _bundle(d)
    assert b.provenance.status == "mismatch"
    assert "internally" in b.provenance.detail


def test_missing_inventory_is_unknown_not_a_refusal(good_run):
    (good_run["run"] / ".enumeration" / "source_inventory.json").unlink()
    b = _bundle(good_run)
    assert b.provenance.status == "unknown"
    assert not b.provenance.refuse


def test_added_source_file_is_reported_but_not_a_refusal(good_run):
    write_source(good_run["root"], {"proc/new.sql": "SELECT 1\n"})
    b = _bundle(good_run)
    assert b.provenance.status == "ok"
    assert "proc/new.sql" in b.provenance.untracked_in_source_root


def test_concept_bodies_fall_back_to_preview_with_a_warning(good_run):
    (good_run["run"] / "stills" / "shared_vectors.db").unlink()
    b = _bundle(good_run)
    assert b.concept_bodies_source == "preview"
    assert any("LOWER BOUND" in n for n in b.notes)
    c = coverage.concept_coverage(b, good_run["key"])
    assert c["concept_bodies_source"] == "preview"


# ============================================================ performance
def test_huge_graph_is_linear(tmp_path):
    """100k concept edges + 100k bridge links must stay linear in time and bounded in memory."""
    root = tmp_path / "src"
    write_source(root, {"big.sql": "CREATE TABLE dbo.T (" +
                        ", ".join(f"C{i} INT" for i in range(500)) + ")\n"})
    n_blocks = 50_000
    blocks = [f"{i:016x}" for i in range(n_blocks)]
    nodes = [{"id": bid, "title": f"T{i}", "block_type": "entity", "source_still": "data",
              "source_page": "p"} for i, bid in enumerate(blocks)]
    edges = []
    # 50k same_as edges forming 25k pairs, 50k CONTRIBUTES_TO edges onto 25k unified concepts
    for i in range(0, n_blocks, 2):
        a, bb = blocks[i], blocks[i + 1]
        u = uc_id([a, bb])
        nodes.append({"id": u, "node_type": "unified_concept", "title": f"U{i}",
                      "block_type": "entity", "source_stills": ["data"]})
        edges.append({"source": a, "target": bb, "edge_type": "same_as", "score": 0.9})
        edges.append({"source": a, "target": u, "edge_type": "CONTRIBUTES_TO"})
        edges.append({"source": bb, "target": u, "edge_type": "CONTRIBUTES_TO"})
    # pad to >100k edges with cross references
    for i in range(0, n_blocks - 1):
        edges.append({"source": blocks[i], "target": blocks[i + 1], "edge_type": "references",
                      "score": 0.8})
    assert len(edges) >= 100_000
    code = {"source_files": [{"path": "big.sql", "lang": "sql"}],
            "code_objects": [{"path": "big.sql", "object": f"dbo.C{i}", "kind": "column"}
                             for i in range(500)],
            "code_edges": [{"src": "big.sql", "dst": f"dbo.C{i}", "type": "REFERENCES",
                            "lang": "sql", "kind": "table", "operation": "select",
                            "direction": "read", "dynamic": 0, "verdict": "confirmed"}
                           for i in range(500)],
            "code_entities": []}
    bridge = {"links": [{"concept": f"data::T{i % 1000}", "symbol": f"C{i % 500}",
                         "match": "code_object", "code_path": "big.sql",
                         "code_name": f"dbo.C{i % 500}", "kind": "column"}
                        for i in range(100_000)]}
    run = write_run(tmp_path / "run", kg={"nodes": nodes, "edges": edges}, code=code, bridge=bridge)
    t0 = time.time()
    b = load.load_bundle(run, root)
    s = structural.check(b)
    dt = time.time() - t0
    assert s["concept_graph"]["edges"] >= 100_000
    assert s["concept_graph"]["same_as"]["merge_classes"] == n_blocks // 2
    assert s["concept_graph"]["unified_ids"]["mismatched"] == 0
    assert s["concept_graph"]["contributes_to"]["is_dag"] is True
    assert s["bridge"]["links"] == 100_000
    assert s["bridge"]["tiers"]["bare_sub"]["passed"] == 100_000   # memoised, not 100k file scans
    assert dt < 120, f"structural check took {dt:.1f}s on a 100k-edge graph"


# ============================================================ LLM section (mocked)
class FakeBedrock:
    """Stands in for eval/graph/judge.Bedrock. Never touches the network."""

    def __init__(self, payloads=None, bad_first=False, cost=0.001):
        self.payloads = payloads or {}
        self.bad_first = bad_first
        self.cost = cost
        self.calls = []

    def call(self, prompt, system, tag):
        self.calls.append({"prompt": prompt, "system": system, "tag": tag})
        if self.bad_first and len(self.calls) == 1:
            payload = {"verdict": "maybe", "confidence": 2.0}     # schema violations
        else:
            payload = None
            for needle, p in self.payloads.items():
                if needle in prompt:
                    payload = p
                    break
            if payload is None:
                payload = {"verdict": "correct", "confidence": 0.9, "reason": "matches"}
        return {"payload": payload, "text": "", "input_tokens": 1000, "output_tokens": 50,
                "cost_usd": self.cost, "stop_reason": "tool_use"}


def test_judge_schema_validation():
    ok = {"verdict": "correct", "confidence": 0.9, "reason": "r"}
    assert judge.validate(ok, judge.TOOL_SCHEMA) == []
    assert judge.validate({"verdict": "maybe", "confidence": 0.5, "reason": "r"},
                          judge.TOOL_SCHEMA)
    assert judge.validate({"verdict": "correct", "confidence": 7, "reason": "r"},
                          judge.TOOL_SCHEMA)
    assert judge.validate({"verdict": "correct", "confidence": 0.5}, judge.TOOL_SCHEMA)
    assert judge.validate({**ok, "extra": 1}, judge.TOOL_SCHEMA)
    assert judge.validate(None, judge.TOOL_SCHEMA) == ["not an object"]
    assert judge.validate({**ok, "confidence": True}, judge.TOOL_SCHEMA)   # bool is not a number


def test_judge_samples_caches_and_reports_a_ci(good_run, tmp_path):
    b = _bundle(good_run)
    fake = FakeBedrock(payloads={"h.Vendor": {"verdict": "too_generic", "confidence": 0.6,
                                             "reason": "shared column name"}})
    cache = tmp_path / "cache"
    r1 = judge.sample_and_judge(b, sample=3, seed=0, cache_dir=cache, client=fake)
    assert r1["status"] == "ok"
    assert r1["judged"] == 3
    assert r1["calls"] == 3 and r1["cache_hits"] == 0
    assert r1["verdicts"]["too_generic"] == 1
    a = r1["agreement"]
    assert a["point_pct"] == pytest.approx(66.67, abs=0.01)
    assert a["ci95_low_pct"] <= a["point_pct"] <= a["ci95_high_pct"]
    # second run is free and identical: content-addressed cache
    fake2 = FakeBedrock()
    r2 = judge.sample_and_judge(b, sample=3, seed=0, cache_dir=cache, client=fake2)
    assert r2["calls"] == 0 and r2["cache_hits"] == 3
    assert r2["cost_usd"] == 0.0
    assert r2["verdicts"] == r1["verdicts"]


def test_judge_retries_a_schema_violation_then_records_it(good_run, tmp_path):
    b = _bundle(good_run)
    fake = FakeBedrock(bad_first=True)
    r = judge.sample_and_judge(b, sample=1, seed=0, cache_dir=tmp_path / "c", client=fake)
    assert len(fake.calls) == 2           # one retry
    assert r["judged"] == 1

    class AlwaysBad(FakeBedrock):
        def call(self, prompt, system, tag):
            super().call(prompt, system, tag)
            return {"payload": {"nope": 1}, "text": "", "input_tokens": 1, "output_tokens": 1,
                    "cost_usd": 0.0, "stop_reason": "end_turn"}

    r2 = judge.sample_and_judge(b, sample=1, seed=0, cache_dir=tmp_path / "c2", client=AlwaysBad())
    assert r2["verdicts"].get("unparsable") == 1
    assert r2["judged"] == 0


def test_judge_respects_the_cost_budget(good_run, tmp_path):
    b = _bundle(good_run)
    fake = FakeBedrock(cost=0.5)
    r = judge.sample_and_judge(b, sample=3, seed=0, cache_dir=tmp_path / "c", client=fake,
                              max_cost=0.6)
    assert r["verdicts"].get("budget_exhausted", 0) >= 1
    assert r["cost_usd"] <= 1.0


def test_judge_uses_forced_tool_use_and_temperature_zero():
    """The tool schema and the forced choice are part of the contract: assert them directly."""
    import inspect
    src = inspect.getsource(judge.Bedrock.call)
    assert '"temperature": 0.0' in src
    assert '"toolChoice": {"tool": {"name": TOOL_NAME}}' in src
    assert set(judge.TOOL_SCHEMA["required"]) == {"verdict", "confidence", "reason"}
    assert judge.TOOL_SCHEMA["additionalProperties"] is False


def test_bootstrap_ci_edges():
    assert judge.bootstrap_ci([]) is None
    a = judge.bootstrap_ci([1] * 20, iters=500)
    assert a["point_pct"] == 100.0 and a["ci95_low_pct"] == 100.0
    b = judge.bootstrap_ci([0] * 20, iters=500)
    assert b["point_pct"] == 0.0 and b["ci95_high_pct"] == 0.0
    c1 = judge.bootstrap_ci([1, 0] * 15, iters=800, seed=3)
    c2 = judge.bootstrap_ci([1, 0] * 15, iters=800, seed=3)
    assert c1 == c2                        # seeded, reproducible


def test_no_llm_run_makes_no_bedrock_client(good_run, tmp_path, monkeypatch):
    """A deterministic run must not import boto3 or need credentials."""
    def boom(*a, **k):
        raise AssertionError("graph_eval must not create a Bedrock client without --llm")

    monkeypatch.setattr(judge.Bedrock, "client", boom)
    b = _bundle(good_run)
    structural.check(b)
    parity.compare(b)
    coverage.concept_coverage(b, good_run["key"])


# ============================================================ CLI + gating
def test_cli_end_to_end_and_gating(good_run, tmp_path):
    d = good_run
    out = tmp_path / "out"
    base = [sys.executable, str(CLI), "--run", str(d["run"]), "--source-root", str(d["root"]),
            "--key", str(d["key"]), "--out-dir", str(out)]
    r = subprocess.run(base, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads((out / "graph_eval.json").read_text())
    md = (out / "graph_eval.md").read_text()
    assert got["structural"]["verdict"] == "clean"
    assert got["thresholds"]["parser_confirmed_edge_precision_pct"] == 100.0
    assert got["bridge_llm"]["status"] == "skipped"
    assert "Knowledge-graph validation" in md
    assert "Headline" in md and "Thresholds" in md

    # a gate that passes
    r = subprocess.run(base + ["--fail-under", "parser_confirmed_edge_precision_pct=90",
                               "--fail-over", "dangling_edges=0"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    # a gate that fails
    r = subprocess.run(base + ["--fail-under", "parser_edge_recall_pct=99"],
                       capture_output=True, text=True)
    assert r.returncode == 1
    got = json.loads((out / "graph_eval.json").read_text())
    assert got["gate_violations"][0]["metric"] == "parser_edge_recall_pct"
    # an unknown metric name fails loudly rather than being ignored
    r = subprocess.run(base + ["--fail-under", "no_such_metric=1"], capture_output=True, text=True)
    assert r.returncode == 1
    assert "unknown metric name" in (out / "graph_eval.json").read_text()
    # a malformed gate is a usage error
    r = subprocess.run(base + ["--fail-under", "garbage"], capture_output=True, text=True)
    assert r.returncode != 0 and "NAME=VALUE" in (r.stderr + r.stdout)


def test_cli_without_key_skips_coverage(good_run, tmp_path):
    out = tmp_path / "o"
    r = subprocess.run([sys.executable, str(CLI), "--run", str(good_run["run"]),
                        "--source-root", str(good_run["root"]), "--out-dir", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = json.loads((out / "graph_eval.json").read_text())
    assert got["concept_coverage"]["status"] == "skipped"
    assert got["thresholds"]["key_item_coverage_any_pct"] is None


def test_list_thresholds_documents_every_gateable_metric():
    r = subprocess.run([sys.executable, str(CLI), "--list-thresholds"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    names = set(report.THRESHOLD_DOC)
    assert names <= set(r.stdout.split())
    # every name in the doc must be produced by thresholds(), and vice versa
    produced = set(report.thresholds({}))
    assert produced == names, produced.symmetric_difference(names)


def test_bad_key_does_not_lose_the_other_sections(good_run, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("this: [is not: valid: yaml")
    out = tmp_path / "o"
    r = subprocess.run([sys.executable, str(CLI), "--run", str(good_run["run"]),
                        "--source-root", str(good_run["root"]), "--key", str(bad),
                        "--out-dir", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = json.loads((out / "graph_eval.json").read_text())
    assert got["concept_coverage"]["status"] == "skipped"
    assert got["structural"]["verdict"] == "clean"
    assert got["parity"]["status"] == "ok"


def test_markdown_renders_for_an_empty_and_a_skipped_run(tmp_path):
    root = tmp_path / "src"
    write_source(root, {"a.sql": "SELECT 1\n"})
    b = load.load_bundle(write_run(tmp_path / "run"), root)
    res = {"meta": {}, "provenance": {"status": "unknown", "detail": "x"}, "counts": b.counts(),
           "structural": structural.check(b), "parity": parity.compare(b),
           "concept_coverage": {"status": "skipped", "reason": "no key"},
           "bridge_llm": {"status": "skipped", "reason": "no --llm"}, "notes": b.notes}
    res["thresholds"] = report.thresholds(res)
    md = report.to_markdown(res)
    assert "skipped" in md and "n/a" in md


# ============================================================ SourceIndex.resolve: dotfiles
def _src_with(tmp_path, names):
    root = tmp_path / "srcroot"
    for n in names:
        f = root / n
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n")
    return load.SourceIndex.build(root)


@pytest.mark.parametrize("name,expected", [
    (".gitignore", ".gitignore"),
    ("./.gitignore", ".gitignore"),
    (".gitattributes", ".gitattributes"),
    ("./src/a.cs", "src/a.cs"),
    ("src/a.cs", "src/a.cs"),
    ("/src/a.cs", "src/a.cs"),
    (".\\src\\a.cs", "src/a.cs"),
    ("././.gitignore", ".gitignore"),
    (".github/workflows/ci.yml", ".github/workflows/ci.yml"),
])
def test_resolve_keeps_the_leading_dot_of_dotfiles(tmp_path, name, expected):
    idx = _src_with(tmp_path, [".gitignore", ".gitattributes", "src/a.cs",
                               ".github/workflows/ci.yml"])
    assert idx.resolve(name) == (expected, "exact")


def test_resolve_does_not_match_a_dotfile_to_its_dotless_namesake(tmp_path):
    """The old lstrip("./") turned ".gitignore" into "gitignore" and so found nothing."""
    idx = _src_with(tmp_path, ["gitignore", "src/a.cs"])
    assert idx.resolve(".gitignore")[0] is None


def test_provenance_accepts_root_dotfiles(tmp_path):
    """Regression for the graph-eval refusal "2 inventory file(s) absent from the source root"."""
    idx = _src_with(tmp_path, [".gitignore", ".gitattributes", "src/a.cs"])
    out = tmp_path / "run"
    (out / ".enumeration").mkdir(parents=True)
    entries = [{"path": rel, "sha256": load._sha256_file(idx.files[rel])}
               for rel in (".gitignore", ".gitattributes", "src/a.cs")]
    (out / ".enumeration" / "source_inventory.json").write_text(
        json.dumps({"inventory_hash": "h", "file_count": 3, "entries": entries}))
    prov = load.read_provenance(out, idx)
    assert prov.status == "ok", prov.detail
    assert prov.missing == [] and prov.verified == 3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fixtures that build synthetic run directories on disk — no AWS, no network, no real corpus."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[2]
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))


# ----------------------------------------------------------------------------- builders
def write_source(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def write_run(run: Path, *, kg: dict | None = None, code: dict | None = None,
              bridge: dict | None = None, parser_graph: dict | None = None,
              facts: dict | None = None, inventory: dict | None = None,
              manifest: dict | None = None, concept_bodies: dict | None = None,
              make_kg_dir: bool = True) -> Path:
    if make_kg_dir:
        d = run / "knowledge_graph"
        d.mkdir(parents=True, exist_ok=True)
        (d / "knowledge_graph.json").write_text(json.dumps(
            kg if kg is not None else {"directed": True, "multigraph": False, "graph": {},
                                       "nodes": [], "edges": []}))
        (d / "code_graph.json").write_text(json.dumps(
            code if code is not None else {"source_files": [], "code_objects": [],
                                           "code_edges": [], "code_entities": []}))
        (d / "concept_code_bridge.json").write_text(json.dumps(
            bridge if bridge is not None else {"links": []}))
        (d / "index.json").write_text(json.dumps({"output_dir": str(run), "artifacts": {}}))
    if parser_graph is not None:
        (run / "facts").mkdir(parents=True, exist_ok=True)
        (run / "facts" / "graph.json").write_text(json.dumps(parser_graph))
    if facts is not None:
        (run / "facts").mkdir(parents=True, exist_ok=True)
        (run / "facts" / "facts.json").write_text(json.dumps(facts))
    if inventory is not None:
        (run / ".enumeration").mkdir(parents=True, exist_ok=True)
        (run / ".enumeration" / "source_inventory.json").write_text(json.dumps(inventory))
    if manifest is not None:
        (run / "run_manifest.json").write_text(json.dumps(manifest))
    if concept_bodies:
        import sqlite3
        (run / "stills").mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(run / "stills" / "shared_vectors.db"))
        conn.execute("CREATE TABLE unified_concepts (concept_id TEXT PRIMARY KEY, title TEXT, "
                     "body_raw TEXT, body_synthesized TEXT)")
        conn.execute("CREATE TABLE vectors (block_id TEXT PRIMARY KEY, title TEXT, body TEXT)")
        for cid, body in concept_bodies.items():
            if str(cid).startswith("uc_"):
                conn.execute("INSERT INTO unified_concepts VALUES (?,?,?,NULL)", (cid, "", body))
            else:
                conn.execute("INSERT INTO vectors VALUES (?,?,?)", (cid, "", body))
        conn.commit()
        conn.close()
    return run


def inventory_for(root: Path, rels: list[str]) -> dict:
    entries = []
    for rel in rels:
        p = root / rel
        data = p.read_bytes()
        entries.append({"path": rel, "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    blob = "".join(f"{e['path']}\t{e['size']}\t{e['sha256']}\n" for e in entries)
    return {"schema": 1, "file_count": len(entries),
            "inventory_hash": hashlib.sha256(blob.encode()).hexdigest(),
            "source_identity": "test", "entries": entries}


def uc_id(block_ids: list[str]) -> str:
    ids = sorted(block_ids)
    if len(ids) == 1:
        return f"uc_{ids[0]}"
    return "uc_" + hashlib.sha256("|".join(ids).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------- a small good run
AP_SQL = """CREATE PROCEDURE dbo.usp_Post
AS
BEGIN
    SELECT Gross FROM dbo.InvHdr WHERE Co = 1
    UPDATE dbo.InvHdr SET Status = 'A'
    EXEC dbo.usp_Dup
END
"""
DUP_SQL = """CREATE PROCEDURE dbo.usp_Dup
AS
BEGIN
    SELECT 1 FROM dbo.InvHdr
END
"""
TABLES_SQL = """CREATE TABLE dbo.InvHdr (Co INT, Gross MONEY, Status CHAR(1), Vendor VARCHAR(10))
GO
"""


@pytest.fixture
def good_run(tmp_path: Path):
    """A small, internally consistent run: 3 source files, a code graph that anchors, a concept
    graph whose same_as/CONTRIBUTES_TO invariants hold, a parser graph that mostly agrees."""
    root = tmp_path / "src"
    write_source(root, {"proc/usp_Post.sql": AP_SQL, "proc/usp_Dup.sql": DUP_SQL,
                        "tbl/Tables.sql": TABLES_SQL})
    b1, b2, b3 = "a" * 16, "b" * 16, "c" * 16
    u12, u3 = uc_id([b1, b2]), uc_id([b3])
    kg = {
        "directed": True, "multigraph": False, "graph": {},
        "nodes": [
            {"id": b1, "title": "Entity: Invoice Header", "block_type": "entity",
             "source_still": "data", "source_page": "page_000", "confidence": 5,
             "body_preview": "### Entity: Invoice Header\n- Evidence: tbl/Tables.sql#L1-L1"},
            {"id": b2, "title": "Entity: Invoice Header", "block_type": "entity",
             "source_still": "data", "source_page": "page_001", "confidence": 4,
             "body_preview": "### Entity: Invoice Header\n- Evidence: proc/usp_Post.sql#L4-L5"},
            {"id": b3, "title": "Rule: Post sets status", "block_type": "rule",
             "source_still": "rule", "source_page": "page_000", "confidence": 5,
             "body_preview": "### Rule\n- Evidence: proc/usp_Post.sql#L5-L5"},
            {"id": u12, "node_type": "unified_concept", "title": "Entity: Invoice Header",
             "block_type": "entity", "source_stills": ["data"], "contributor_count": 2},
            {"id": u3, "node_type": "unified_concept", "title": "Rule: Post sets status",
             "block_type": "rule", "source_stills": ["rule"], "contributor_count": 1},
        ],
        "edges": [
            {"source": b1, "target": b2, "edge_type": "same_as", "score": 0.93},
            {"source": b1, "target": u12, "edge_type": "CONTRIBUTES_TO"},
            {"source": b2, "target": u12, "edge_type": "CONTRIBUTES_TO"},
            {"source": b3, "target": u3, "edge_type": "CONTRIBUTES_TO"},
            {"source": b3, "target": b1, "edge_type": "references", "score": 0.8},
        ],
    }
    code = {
        "source_files": [
            {"path": "proc/usp_Post.sql", "lang": "sql", "role": "stored_procedure",
             "business_intent": "Posts an invoice."},
            {"path": "proc/usp_Dup.sql", "lang": "sql", "role": "stored_procedure",
             "business_intent": "Duplicate check."},
            {"path": "tbl/Tables.sql", "lang": "sql", "role": "table",
             "business_intent": "AP tables."},
        ],
        "code_objects": [
            {"path": "proc/usp_Post.sql", "object": "dbo.usp_post", "kind": "stored_procedure"},
            {"path": "proc/usp_Dup.sql", "object": "dbo.usp_dup", "kind": "stored_procedure"},
            {"path": "tbl/Tables.sql", "object": "dbo.invhdr", "kind": "table"},
        ],
        "code_edges": [
            {"src": "proc/usp_Post.sql", "dst": "dbo.invhdr", "type": "REFERENCES", "lang": "sql",
             "kind": "table", "operation": "select", "direction": "read", "resolution": "static",
             "symbol": None, "validated": 1, "dynamic": 0, "confidence": 0.95,
             "verdict": "confirmed", "direction_verdict": "unconfirmed", "derived_confidence": 0.95},
            {"src": "proc/usp_Post.sql", "dst": "dbo.invhdr", "type": "REFERENCES", "lang": "sql",
             "kind": "table", "operation": "update", "direction": "write", "resolution": "static",
             "symbol": None, "validated": 1, "dynamic": 0, "confidence": 0.95,
             "verdict": "confirmed", "direction_verdict": "confirmed", "derived_confidence": 0.95},
            {"src": "proc/usp_Post.sql", "dst": "dbo.usp_dup", "type": "REFERENCES", "lang": "sql",
             "kind": "stored_procedure", "operation": "exec", "direction": "call",
             "resolution": "static", "symbol": None, "validated": 1, "dynamic": 0,
             "confidence": 0.9, "verdict": "confirmed", "direction_verdict": None,
             "derived_confidence": 0.9},
        ],
        "code_entities": [
            {"path": "tbl/Tables.sql", "code_name": "Gross", "business_name": "Invoice gross amount",
             "confidence": 0.9, "anchor_verdict": "confirmed", "stills_corroborated": 1},
            {"path": "tbl/Tables.sql", "code_name": "Vendor", "business_name": "Vendor",
             "confidence": 0.9, "anchor_verdict": "confirmed", "stills_corroborated": 1},
        ],
    }
    bridge = {"links": [
        {"concept": "data::Entity: Invoice Header", "symbol": "Gross", "match": "code_entity",
         "code_path": "tbl/Tables.sql", "code_name": "Gross",
         "business_name": "Invoice gross amount"},
        {"concept": "data::Entity: Invoice Header", "symbol": "h.Vendor", "match": "code_entity",
         "code_path": "tbl/Tables.sql", "code_name": "Vendor", "business_name": "Vendor"},
        {"concept": "rule::Rule: Post sets status", "symbol": "usp_Post", "match": "code_object",
         "code_path": "proc/usp_Post.sql", "code_name": "dbo.usp_post",
         "kind": "stored_procedure"},
    ]}
    parser_graph = {
        "schema": "lean-graph/v1",
        "nodes": [
            {"id": "sql:usp_Post", "kind": "procedure", "name": "usp_Post",
             "file": "proc/usp_Post.sql"},
            {"id": "sql:usp_Dup", "kind": "procedure", "name": "usp_Dup",
             "file": "proc/usp_Dup.sql"},
            {"id": "table:InvHdr", "kind": "table", "name": "InvHdr", "file": "tbl/Tables.sql"},
            {"id": "table:#tmp", "kind": "table", "name": "#tmp", "file": None},
        ],
        "edges": [
            {"src": "sql:usp_Post", "dst": "table:InvHdr", "kind": "reads",
             "file": "proc/usp_Post.sql", "line": 4},
            {"src": "sql:usp_Post", "dst": "table:InvHdr", "kind": "writes",
             "file": "proc/usp_Post.sql", "line": 5},
            {"src": "sql:usp_Post", "dst": "sql:usp_Dup", "kind": "exec",
             "file": "proc/usp_Post.sql", "line": 6},
            {"src": "sql:usp_Dup", "dst": "table:InvHdr", "kind": "reads",
             "file": "proc/usp_Dup.sql", "line": 4},
            {"src": "sql:usp_Post", "dst": "table:#tmp", "kind": "reads",
             "file": "proc/usp_Post.sql", "line": 4},
        ],
    }
    bodies = {
        b1: "### Entity: Invoice Header\n- Evidence: tbl/Tables.sql#L1-L1\n",
        b2: "### Entity: Invoice Header\n- Evidence: proc/usp_Post.sql#L4-L5\n",
        b3: "### Rule: Post sets status\n- Evidence: proc/usp_Post.sql#L5-L5\n",
        u12: "### Entity: Invoice Header\n- Evidence: tbl/Tables.sql#L1-L1, proc/usp_Post.sql#L4-L5\n",
        u3: "### Rule: Post sets status\n- Evidence: proc/usp_Post.sql#L5-L5\n",
    }
    rels = ["proc/usp_Post.sql", "proc/usp_Dup.sql", "tbl/Tables.sql"]
    inv = inventory_for(root, rels)
    run = write_run(tmp_path / "run", kg=kg, code=code, bridge=bridge,
                    parser_graph=parser_graph, facts={"v1": {}}, inventory=inv,
                    manifest={"run_identity": {"source_inventory_hash": inv["inventory_hash"]}},
                    concept_bodies=bodies)
    key = tmp_path / "key.yaml"
    key.write_text(json.dumps({"corpus": "t", "items": [
        {"id": "R-1", "type": "rule", "statement": "Posting sets the status.",
         "locations": [{"file": "proc/usp_Post.sql", "start_line": 5, "end_line": 5}],
         "evidence_class": "Verified", "flags": []},
        {"id": "R-2", "type": "rule", "statement": "Two-location rule.",
         "locations": [{"file": "proc/usp_Post.sql", "start_line": 4, "end_line": 5},
                       {"file": "proc/usp_Dup.sql", "start_line": 4, "end_line": 4}],
         "evidence_class": "Verified", "flags": []},
        {"id": "R-3", "type": "state", "statement": "Nothing cites this.",
         "locations": [{"file": "proc/usp_Dup.sql", "start_line": 1, "end_line": 1}],
         "evidence_class": "Unknown", "flags": ["dead_code"]},
    ]}))
    return {"run": run, "root": root, "key": key, "blocks": (b1, b2, b3), "unified": (u12, u3),
            "inventory": inv, "tmp": tmp_path}

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Python code-graph rig and its Tree-sitter facts. No AWS and no network.

Bedrock is replaced with a fake that returns a canned reply; the facts come from
runtimes/facts/treesitter_facts.py run on runtimes/facts/tests/fixtures/py-sample. What is under
test: `.py` is mapped to the python prompt, the python reply goes through the same CLR-shaped
disposal as C#/VB, and Tree-sitter facts confirm model edges and backfill the ones it missed
(calls, imports incl. external libraries, SQL-string tables, code objects), tagged `facts:py_*`.

Run:  python3 -m pytest -q runtimes/mcp-server/tests/test_code_graph_python.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

MCP = Path(__file__).resolve().parents[1]
FACTS = MCP.parent / "facts"
PY = FACTS / "tests" / "fixtures" / "py-sample"
sys.path.insert(0, str(MCP))
sys.path.insert(0, str(FACTS))

import bedrock_client  # noqa: E402
import code_graph  # noqa: E402


@pytest.fixture(scope="module")
def py_facts():
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_python")
    import treesitter_facts
    rels = sorted(str(p.relative_to(PY)) for p in PY.rglob("*.py"))
    return treesitter_facts.extract(PY, rels)


SERVICE = (PY / "shop" / "service.py").read_text()
REPO_SRC = (PY / "shop" / "repository.py").read_text()

# What the model returns for shop/service.py: it finds some relations and misses others.
SERVICE_REPLY = {
    "file_role": "service",
    "module": None,
    "business_intent": "Places orders, holding large ones for review.",
    "usings": ["logging", "shop.repository"],
    "declarations": [{"name": "OrderService", "kind": "class", "base": None, "implements": []}],
    "calls": [
        {"caller": "OrderService.place", "callee": "OrderRepository.load", "receiver": "self.repo",
         "confidence": 0.9},
        {"caller": "OrderService.place", "callee": "approve", "receiver": None, "confidence": 0.9},
        {"caller": "OrderService.place", "callee": "ship_it", "receiver": None, "confidence": 0.3},
    ],
    "data_refs": [],
    "references": [],
    "business_entities": [],
}

REPO_REPLY = {
    "file_role": "repository", "module": None, "business_intent": "Loads and saves orders.",
    "usings": ["sqlite3"],
    "declarations": [{"name": "OrderRepository", "kind": "class"}],
    "calls": [],
    "data_refs": [{"name": "orders", "kind": "table", "operation": "select", "direction": "read",
                   "via": "inline_sql", "resolution": "static", "confidence": 0.9}],
    "references": [], "business_entities": [],
}


def _by(node, etype):
    return {code_graph._normalize_dst(e["dst"]): e for e in node["edges"] if e["type"] == etype}


def test_python_is_a_code_graph_language_with_its_own_prompt():
    assert code_graph.CODE_EXTS["py"] == "python"
    system, user = code_graph._LANG_PROMPTS["python"]
    assert "Python" in system
    for key in ('"usings"', '"calls"', '"data_refs"', '"declarations"', "external libraries"):
        assert key in user
    assert "{source}" in user and "{path}" in user
    user.format(path="a.py", source="x = 1")          # the template formats (braces escaped)


def test_python_reply_is_disposed_like_the_clr_rigs_without_facts():
    node = code_graph._assemble("shop/service.py", "python", SERVICE_REPLY, SERVICE, "page_000", "m",
                                code_graph.FactsIndex(None))
    calls = _by(node, "CALLS")
    assert calls["load"]["verdict"] == "confirmed"           # present in the source
    assert calls["ship_it"]["verdict"] == "unconfirmed"       # absent: kept and flagged
    assert set(_by(node, "IMPORTS")) == {"logging", "repository"}
    assert not any((e.get("symbol") or "").startswith("facts:") for e in node["edges"])


def test_tree_sitter_facts_confirm_and_backfill_python_edges(py_facts):
    idx = code_graph.FactsIndex(py_facts)
    node = code_graph._assemble("shop/service.py", "python", SERVICE_REPLY, SERVICE, "page_000", "m", idx)
    calls = _by(node, "CALLS")
    assert calls["load"]["direction_verdict"] == "parser_confirmed"
    assert calls["approve"]["direction_verdict"] == "parser_confirmed"
    # missed by the model, found by the parser
    assert calls["_hold"]["symbol"] == "facts:py_call" and calls["_hold"]["verdict"] == "confirmed"
    assert calls["record"]["symbol"] == "facts:py_call"
    imps = [e for e in node["edges"] if e["type"] == "IMPORTS"]
    logging_edges = [e for e in imps if code_graph._normalize_dst(e["dst"]) == "logging"]
    assert len(logging_edges) == 1 and logging_edges[0]["direction_verdict"] == "parser_confirmed"
    assert any(e["dst"] == "shop.models" and e["symbol"] == "facts:py_import" for e in imps)
    # code objects the model did not declare
    names = {d["name"] for d in node["defines"]}
    assert {"OrderService", "OrderService.place", "AuditTrail", "approve", "main"} <= names


def test_external_library_imports_and_sql_tables_are_backfilled(py_facts):
    idx = code_graph.FactsIndex(py_facts)
    node = code_graph._assemble("shop/repository.py", "python", REPO_REPLY, REPO_SRC, "page_000", "m", idx)
    imps = {e["dst"]: e for e in node["edges"] if e["type"] == "IMPORTS"}
    assert imps["requests"]["symbol"] == "facts:py_import_external"
    assert imps["sqlite3"]["direction_verdict"] == "parser_confirmed"
    refs = {(code_graph._normalize_dst(e["dst"]), e["operation"], e["direction"]): e
            for e in node["edges"] if e["type"] == "REFERENCES"}
    assert refs[("orders", "select", "read")]["direction_verdict"] == "parser_confirmed"
    assert refs[("orders", "update", "write")]["symbol"] == "facts:py_inline_sql"
    assert refs[("order_audit", "insert", "write")]["symbol"] == "facts:py_inline_sql"


def test_strip_comments_python_hash_and_single_quotes():
    src = "x = '# not a comment'  # orders table\ny = \"a#b\"\n# only_comment_table\nz = 'it\\'s' # c"
    out = code_graph.strip_comments(src, "python").splitlines()
    assert out[0].rstrip() == "x = '# not a comment'"
    assert out[1] == 'y = "a#b"'
    assert out[2] == ""
    assert out[3].rstrip() == "z = 'it\\'s'"
    # the other languages are unchanged: a single quote is still a VB comment opener
    assert code_graph.strip_comments("x = 1 ' note", "vbnet").rstrip() == "x = 1"


def test_build_code_graph_persists_python_relations(tmp_path, monkeypatch, py_facts):
    pages = tmp_path / ".pages" / "page_000"
    pages.mkdir(parents=True)
    body = "\n".join(f"{i + 1:5d} | {ln}" for i, ln in enumerate(REPO_SRC.splitlines()))
    (pages / "concat.txt").write_text(
        "[FILE: shop/repository.py KIND: raw]\n" + body + "\n[END FILE: shop/repository.py]\n")
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "facts.json").write_text(json.dumps(py_facts))
    prompts = []

    def fake_call(llm, model_id, system, user, max_tokens=0, temperature=0.0):
        prompts.append((system, user))
        return bedrock_client.ConverseResult(output_text=json.dumps(REPO_REPLY), input_tokens=10,
                                             output_tokens=20, stop_reason="end_turn")

    monkeypatch.setattr(code_graph.bedrock_client, "create_llm_client", lambda profile, region: object())
    monkeypatch.setattr(code_graph.bedrock_client, "call_llm", fake_call)
    db = tmp_path / "shared_vectors.db"
    stats = code_graph.build_code_graph(str(tmp_path / ".pages"), str(db), min_chars=100)
    assert stats["files"] == 1 and stats["ok"] == 1 and stats["errors"] == 0
    assert stats["parser_facts"]["present"] is True
    assert "Python" in prompts[0][0] and "```python" in prompts[0][1]
    conn = sqlite3.connect(str(db))
    edges = conn.execute("SELECT dst, type, symbol FROM code_edges").fetchall()
    objs = {r[0] for r in conn.execute("SELECT object FROM code_objects")}
    lang = conn.execute("SELECT lang FROM source_files").fetchone()[0]
    conn.close()
    assert lang == "python"
    assert ("requests", "IMPORTS", "facts:py_import_external") in edges
    assert any(t == "CALLS" and s == "facts:py_call" for _, t, s in edges)
    assert {"orderrepository", "orderrepository.load", "orderrepository.save_status"} <= objs

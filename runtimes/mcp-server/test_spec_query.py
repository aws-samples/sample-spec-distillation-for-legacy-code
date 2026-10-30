"""
test_spec_query.py — tests for the spec-query consumption server's read modules.

No mocking and no network: graph_query and dependency_query are pure reads over a
completed run's SQLite + JSON. Tests build a minimal synthetic run on a tmp_path, so
they do not depend on any particular distillation output being present.

Run with:
    python3 -m pytest test_spec_query.py -v
"""

import json
import sys
import sqlite3

import pytest

import dependency_query as dq
import graph_query as gq


# ── Fixture: a minimal but structurally real completed run ───────────────────


def _make_run(tmp_path, with_code_graph=True):
    """Build a run directory with the tables/files the read modules require.

    Shape mirrors a real run: two concepts, one anchoring to a code object and one
    whose only symbol is method-level (so it cannot anchor) — the two cases the
    `resolved` contract exists to distinguish.
    """
    run = tmp_path / "run"
    stills = run / "stills"
    stills.mkdir(parents=True)

    db = stills / "shared_vectors.db"
    conn = sqlite3.connect(db)
    # Schemas are copied from the SOURCE that creates them, never from a sample
    # database — an artifact is one instance and may be stale, while these are the
    # definitions the pipeline will actually produce:
    #   unified_concepts  knowledge.py:357
    #   code_objects / code_edges / code_entities  code_graph.py:198-211
    #   symbol_backmap    symbol_backmap.py:138
    conn.executescript(
        """
        CREATE TABLE unified_concepts (
            concept_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            block_type TEXT NOT NULL,
            confidence INTEGER NOT NULL DEFAULT 0,
            contributor_count INTEGER NOT NULL DEFAULT 1,
            source_stills TEXT NOT NULL DEFAULT '',
            source_pages TEXT NOT NULL DEFAULT '',
            body_raw TEXT NOT NULL,
            body_synthesized TEXT,
            contributor_ids TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE code_objects (
            path TEXT, object TEXT, kind TEXT, PRIMARY KEY (path, object)
        );
        CREATE TABLE code_edges (
            src TEXT, dst TEXT, type TEXT, lang TEXT, kind TEXT, operation TEXT,
            direction TEXT, resolution TEXT, symbol TEXT,
            validated INTEGER, dynamic INTEGER, confidence REAL,
            verdict TEXT, direction_verdict TEXT, derived_confidence REAL
        );
        CREATE TABLE code_entities (
            path TEXT, code_name TEXT, business_name TEXT, confidence REAL,
            anchor_verdict TEXT, stills_corroborated INTEGER,
            PRIMARY KEY (path, code_name)
        );
        CREATE TABLE symbol_backmap (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_key TEXT, still TEXT, new_value TEXT,
            observed_value TEXT, observed_source TEXT, observed_line INTEGER,
            transform_type TEXT, rationale TEXT, confidence REAL,
            resolver TEXT, ts REAL
        );
        """
    )
    # A bare symbol (`Widget`) does not match a schema-qualified object (`dbo.widget`)
    # directly — in a real run it anchors via the back-map, which records the
    # source-faithful dotted token. Mirroring that here keeps the fixture honest about
    # how anchoring actually succeeds.
    conn.execute(
        "INSERT INTO symbol_backmap (still, new_value, observed_value) VALUES (?,?,?)",
        ("data", "Widget", "dbo.Widget"))
    conn.executemany(
        "INSERT INTO unified_concepts VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("uc_anchored", "Entity: Widget", "entity", 5, 2,
             '["data"]', '["page_000"]', "widget body", None,
             '["blk_data_1", "blk_data_2"]'),
            ("uc_methodonly", "Rule: Widget Total Rounds Half Up", "rule", 4, 1,
             '["rule"]', '["page_000"]', "rounding body", None,
             '["blk_rule_1"]'),
        ],
    )
    if with_code_graph:
        conn.executemany(
            "INSERT INTO code_objects VALUES (?,?,?)",
            [("db/Tables.sql", "dbo.widget", "table"),
             ("src/WidgetPost.sql", "dbo.usp_widgetpost", "stored_procedure")],
        )
        conn.execute(
            "INSERT INTO code_edges (src, dst, type, lang, kind, direction, "
            "confidence, derived_confidence) VALUES (?,?,?,?,?,?,?,?)",
            ("src/WidgetPost.sql", "dbo.widget", "REFERENCES", "sql",
             "table", "read", 0.99, 0.99),
        )
    conn.commit()
    conn.close()

    # Cross-still corroboration lives only in the graph JSON (no edges table exists).
    (stills / "knowledge_graph.json").write_text(json.dumps({
        "nodes": [
            {"id": "blk_data_1", "source_still": "data", "title": "Entity: Widget"},
            {"id": "blk_data_2", "source_still": "data", "title": "Entity: Widget dup"},
            {"id": "blk_rule_1", "source_still": "rule", "title": "Rule: rounding"},
            {"id": "blk_iface_1", "source_still": "interface", "title": "Obligation: rounding"},
            {"id": "blk_excep_1", "source_still": "exception", "title": "Exception: rounding"},
        ],
        "edges": [
            {"edge_type": "CONTRIBUTES_TO", "source": "blk_data_1", "target": "uc_anchored"},
            {"edge_type": "same_as", "source": "blk_data_1", "target": "blk_data_2",
             "score": 0.91},
            # the rounding rule corroborated by two other independent stills
            {"edge_type": "references", "source": "blk_rule_1", "target": "blk_iface_1",
             "score": 0.93},
            {"edge_type": "references", "source": "blk_rule_1", "target": "blk_excep_1",
             "score": 0.87},
        ],
    }))

    (stills / "concept_symbols.json").write_text(json.dumps({
        "by_concept": {
            "data::Entity: Widget": {
                "still": "data", "title": "Entity: Widget",
                "symbols": ["Widget"],              # anchors to dbo.widget
            },
            "rule::Rule: Widget Total Rounds Half Up": {
                "still": "rule", "title": "Rule: Widget Total Rounds Half Up",
                "symbols": ["ComputeWidgetTotal"],  # method-level: cannot anchor
            },
        },
        "by_symbol": {"widget": ["data::Entity: Widget"]},
    }))
    return run, str(db), stills / "concept_symbols.json"


@pytest.fixture
def run(tmp_path):
    run_dir, db, syms = _make_run(tmp_path)
    return {"dir": str(run_dir), "db": db, "sym": json.loads(syms.read_text())}


# ── The contract that must never regress ─────────────────────────────────────


def test_unresolved_returns_none_not_empty_list(run):
    """A concept that cannot anchor must NOT look like 'nothing depends on it'.

    This is the whole reason the `resolved` field exists: an agent reading `[]` as
    'safe to change' is a worse failure than no answer at all.
    """
    r = dq.query_by_title(run["db"], run["sym"], "Rule: Widget Total Rounds Half Up")

    assert r["resolved"] is False
    assert r["requires"] is None, "must be None, never [] — [] reads as 'no deps'"
    assert r["required_by"] is None
    assert r["reason"]
    assert r["graph_scope"]


def test_resolved_answer_keeps_its_shape(run):
    """The success path is unchanged apart from gaining resolved=True.

    Guards the viewer's /api/dependencies, which reads these same keys.
    """
    r = dq.query_by_title(run["db"], run["sym"], "Entity: Widget", still="data")

    assert r["resolved"] is True
    assert "dbo.widget" in r["anchors"]
    assert isinstance(r["requires"], list)
    assert isinstance(r["required_by"], list)
    # the stored proc reads the table, so it depends on this concept
    assert any("WidgetPost" in d["object"] for d in r["required_by"])


def test_genuine_empty_is_distinguishable_from_unresolved(run):
    """resolved=True with [] is a real 'none found'; the pair must differ."""
    ok = dq.query_by_title(run["db"], run["sym"], "Entity: Widget", still="data")
    bad = dq.query_by_title(run["db"], run["sym"], "Rule: Widget Total Rounds Half Up")

    assert ok["resolved"] is True and ok["requires"] == []
    assert bad["resolved"] is False and bad["requires"] is None


def test_missing_concept_also_marked_unresolved(run):
    r = dq.query_by_title(run["db"], run["sym"], "No Such Concept Anywhere")
    assert r["resolved"] is False
    assert r["requires"] is None
    assert "error" in r


def test_no_code_graph_is_unresolved_not_empty(tmp_path):
    """A run whose code-graph phase never ran must report unresolved, not 'none'."""
    run_dir, db, syms = _make_run(tmp_path, with_code_graph=False)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE code_edges")
    conn.commit()
    conn.close()

    r = dq.query_by_title(db, json.loads(syms.read_text()), "Entity: Widget")
    assert r["resolved"] is False
    assert r["requires"] is None


# ── graph_query ──────────────────────────────────────────────────────────────


def test_convergence_counts_only_other_stills(run):
    """Corroboration means agreement from an independently-run lens.

    The rounding rule is found by `rule` and referenced from `interface` and
    `exception`, so convergence is 2 — its own still does not count toward it.
    """
    out = gq.query(run["dir"], "rounds half up")
    c = out["concepts"][0]

    assert c["convergence"]["count"] == 2
    assert set(c["convergence"]["stills"]) == {"interface", "exception"}
    assert c["convergence"]["scores"] == [0.93, 0.87]  # descending
    assert "rule" not in c["convergence"]["stills"]


def test_results_ordered_by_convergence(run):
    """Best-evidenced concept leads, so a caller reading the first hit gets the
    most independently corroborated one rather than an arbitrary row."""
    out = gq.query(run["dir"], "")
    counts = [c["convergence"]["count"] for c in out["concepts"]]
    assert counts == sorted(counts, reverse=True)


def test_merged_concept_reports_contributor_count(run):
    """contributor_count > 1 must stay visible: dedup is type-blind at 0.85, so a
    caller needs to see that a concept is an aggregate rather than one finding."""
    out = gq.query(run["dir"], "widget", block_type="entity")
    assert out["concepts"][0]["contributor_count"] == 2


def test_filters(run):
    assert gq.query(run["dir"], "", block_type="rule")["total_matched"] == 1
    assert gq.query(run["dir"], "", still="data")["total_matched"] == 1
    assert gq.query(run["dir"], "nothing matches this")["total_matched"] == 0


def test_body_omitted_unless_requested(run):
    assert "body" not in gq.query(run["dir"], "widget")["concepts"][0]
    assert gq.query(run["dir"], "widget", include_body=True)["concepts"][0]["body"]


def test_get_concept_by_id(run):
    c = gq.get_concept(run["dir"], "uc_anchored")
    assert c["title"] == "Entity: Widget"
    assert c["body"] == "widget body"
    assert "error" in gq.get_concept(run["dir"], "uc_does_not_exist")


def test_stats(run):
    s = gq.stats(run["dir"])
    assert s["concepts"] == 2
    assert s["concepts_merged_from_multiple_observations"] == 1
    assert s["by_block_type"] == {"entity": 1, "rule": 1}
    assert s["edge_types"]["references"] == 2


# ── path resolution ──────────────────────────────────────────────────────────


def test_resolves_by_convention_without_run_json(run):
    """A run with no run.json is still queryable via layout convention."""
    p = gq.resolve_paths(run["dir"])
    assert p["vectors_db"].endswith("stills/shared_vectors.db")
    assert p["graph_json"]


def test_bad_run_dir_raises_usable_error(tmp_path):
    with pytest.raises(gq.RunNotUsable):
        gq.resolve_paths(str(tmp_path / "nonexistent"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(gq.RunNotUsable):
        gq.resolve_paths(str(tmp_path / "empty"))


# ── stdout isolation (MCP_NOTES.md G2) ───────────────────────────────────────
# On stdio transport, stdout IS the JSON-RPC channel. The pipeline modules this
# server reuses print progress to stdout, so a tool body that lets a print escape
# corrupts the protocol stream. These guard the decorator that prevents it.


def _load_spec_server():
    """Import spec_tooling (holds quiet/err); no mcp dependency."""
    return pytest.importorskip("spec_tooling")


def test_quiet_sends_tool_stdout_to_stderr(capsys):
    """A print inside a tool body must not reach stdout."""
    import asyncio

    ss = _load_spec_server()

    @ss.quiet
    async def noisy():
        print("[knowledge] Processing still: data")   # the real offender's format
        return "result"

    assert asyncio.run(noisy()) == "result"
    cap = capsys.readouterr()
    assert cap.out == "", f"stdout leaked: {cap.out!r} — would corrupt JSON-RPC"
    assert "Processing still" in cap.err, "output lost entirely; it should go to stderr"


def test_quiet_preserves_signature_for_schema_generation():
    """FastMCP builds each tool's input schema by introspecting the function.

    If the decorator hid the real parameters, every tool would advertise an empty
    schema and agents could not call it correctly.
    """
    import inspect

    ss = _load_spec_server()

    @ss.quiet
    async def sample(run_dir: str, query: str = "", limit: int = 10) -> str:
        return ""

    params = inspect.signature(sample).parameters
    assert list(params) == ["run_dir", "query", "limit"]
    assert params["limit"].default == 10
    assert sample.__name__ == "sample"


def test_quiet_restores_stdout_after_an_exception():
    """A failing tool must not leave stdout redirected for the next call."""
    import asyncio

    ss = _load_spec_server()
    original = sys.stdout

    @ss.quiet
    async def boom():
        raise ValueError("tool failed")

    with pytest.raises(ValueError):
        asyncio.run(boom())
    assert sys.stdout is original, "stdout left redirected — later calls would be silenced"


def test_every_registered_tool_is_quiet():
    """Catches a new tool added without the stdout guard, in any tools_*.py module."""
    import re
    from pathlib import Path

    here = Path(__file__).parent
    mods = sorted(here.glob("tools_*.py"))
    assert mods, "no tools_*.py modules found — auto-discovery would load nothing"

    bad = []
    for mod in mods:
        src = mod.read_text(encoding="utf-8")
        for name in re.findall(r"(?m)^\s*async def (\w+)\(", src):
            if f"@quiet\n    async def {name}(" not in src:
                bad.append(f"{mod.name}:{name}")
    assert not bad, f"tools missing the @quiet stdout guard: {bad}"


def test_every_tool_module_registers():
    """Auto-discovery requires each tools_*.py to expose register(server)."""
    from pathlib import Path

    here = Path(__file__).parent
    missing = [m.name for m in sorted(here.glob("tools_*.py"))
               if "def register(server)" not in m.read_text(encoding="utf-8")]
    assert not missing, f"tools_*.py without register(server): {missing}"

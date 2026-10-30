# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""End to end: build_key CLI, determinism, planted problems, review sheet, decisions,
filtered key, and eval/score.py scoring the filtered key. No AWS (score.py runs --no-llm)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
import sys
from html.parser import HTMLParser

import pytest
import yaml

import kg_testutil as U

BUILD = U.KG / "build_key.py"
REVIEW = U.KG / "review.py"
SRC = U.FIX / "mini-cs"


def run(*args, check=True):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, cwd=U.REPO)
    if check and r.returncode != 0:
        raise AssertionError(r.stdout + r.stderr)
    return r


def build(tmp_path, name="key.yaml", *extra):
    out = tmp_path / name
    run(BUILD, "--source", SRC, "--commit", "abc1234", "--repo-url", "https://example.invalid/mini",
        "--source-license", "MIT", "--corpus", "mini-cs", "--out", out, "--date", "2026-10-08",
        "--docs", "README.md", *extra)
    return out


@pytest.fixture(scope="module")
def key(tmp_path_factory):
    return build(tmp_path_factory.mktemp("kg"))


def test_key_schema_and_metadata(key):
    d = yaml.safe_load(key.read_text())
    m = d["metadata"]
    assert m["repo_url"] == "https://example.invalid/mini" and m["commit"] == "abc1234"
    assert m["builder"]["name"] == "eval/keygen" and m["generated"] == "2026-10-08"
    assert m["reviewer"] is None and m["counts"]["total"] == len(d["items"])
    assert set(m["counts"]["by_source"]) == {"harvest", "tests", "docs"}
    assert m["counts"]["by_status"] == {"unreviewed": len(d["items"])}
    for it in d["items"]:
        assert {"id", "type", "statement", "locations", "evidence_class", "flags", "status",
                "source", "provenance"} <= set(it)
        for l in it["locations"]:
            assert (SRC / l["file"]).is_file() and 1 <= l["start_line"] <= l["end_line"]
    text = key.read_text()
    assert text.startswith("# Copyright Amazon.com, Inc. or its affiliates.")
    assert "SPDX-License-Identifier: MIT-0" in text.splitlines()[1]
    assert "No output of the pipeline under test was read" in text


def test_same_inputs_give_byte_identical_key(tmp_path, key):
    again = build(tmp_path, "again.yaml")
    assert again.read_bytes() == key.read_bytes()


def test_score_py_reads_the_draft_key(key, tmp_path):
    sys.path.insert(0, str(U.EVAL))
    import score
    k = score.load_key(key)
    assert len(k["items"]) == len(yaml.safe_load(key.read_text())["items"])
    assert all(it["citations"] for it in k["items"])


# --------------------------------------------------------------------------- planted problems
def test_plant_is_reproducible_and_merged(tmp_path):
    a = build(tmp_path, "pa.yaml", "--plant", "6", "--seed", "7", "--plant-out", tmp_path / "ma",
              "--mutations-out", tmp_path / "ma.yaml")
    b = build(tmp_path, "pb.yaml", "--plant", "6", "--seed", "7", "--plant-out", tmp_path / "mb")
    ma = (tmp_path / "ma" / "mutations.yaml").read_bytes()
    assert ma == (tmp_path / "mb" / "mutations.yaml").read_bytes() == (tmp_path / "ma.yaml").read_bytes()
    da, db = yaml.safe_load(a.read_text()), yaml.safe_load(b.read_text())
    da["metadata"]["inputs"]["planted"].pop("mutated_tree")
    db["metadata"]["inputs"]["planted"].pop("mutated_tree")
    assert da == db
    planted = [i for i in da["items"] if i["status"] == "planted"]
    assert len(planted) == da["metadata"]["inputs"]["planted"]["applied"] == 6
    assert all(i["source"][0].startswith("planted:") and i["flags"] for i in planted)
    # harvested coordinates are mutated-tree coordinates
    tree = tmp_path / "ma" / "corpus"
    for it in da["items"]:
        for l in it["locations"]:
            assert (tree / l["file"]).is_file()


# --------------------------------------------------------------------------- review sheet
class _Collect(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items, self.radios, self.scripts = 0, 0, []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div" and a.get("class") == "item":
            self.items += 1
        if tag == "input" and a.get("type") == "radio":
            self.radios += 1
        if tag == "script":
            self._in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.scripts.append(data)


def test_html_sheet_renders_every_item_with_controls_and_excerpts(key, tmp_path):
    html_p, csv_p = tmp_path / "r.html", tmp_path / "r.csv"
    run(REVIEW, "--key", key, "--source", SRC, "--html", html_p, "--csv", csv_p)
    n = len(yaml.safe_load(key.read_text())["items"])
    p = _Collect()
    p.feed(html_p.read_text())
    assert p.items == n and p.radios == 3 * n
    js = "".join(p.scripts)
    assert "exportDecisions" in js and "localStorage" in js
    page = html_p.read_text()
    assert "<span class='ln'>38</span>" in page                  # line-numbered excerpt
    assert "http://" not in page.split("<main>")[0] and "<link" not in page   # self-contained
    rows = list(csv.DictReader(io.StringIO(csv_p.read_text())))
    assert len(rows) == n and rows[0]["decision"] == ""


def test_html_sheet_javascript_parses(key, tmp_path):
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    html_p = tmp_path / "r.html"
    run(REVIEW, "--key", key, "--html", html_p)
    p = _Collect()
    p.feed(html_p.read_text())
    js = tmp_path / "sheet.js"
    js.write_text(max(p.scripts, key=len))
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_decisions_apply_filter_and_score(key, tmp_path):
    d = yaml.safe_load(key.read_text())
    items = d["items"]
    acc = next(i for i in items if i["statement"].startswith("Order quantity must be greater than zero"))
    ed = next(i for i in items if i["statement"].startswith("Max lines is fixed at 50"))
    rej = next(i for i in items if "allows anonymous access" in i["statement"])
    dec = {"key": str(key), "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest(),
           "reviewer": "tester", "date": "2026-10-09",
           "decisions": {acc["id"]: {"decision": "accept"},
                         ed["id"]: {"decision": "edit", "statement": "An order holds at most 50 lines.",
                                    "note": "plain English"},
                         rej["id"]: {"decision": "reject"}}}
    dp = tmp_path / "decisions.json"
    dp.write_text(json.dumps(dec))
    reviewed = tmp_path / "reviewed.yaml"
    out = json.loads(run(REVIEW, "--key", key, "--apply", dp, "--out", reviewed).stdout)
    assert (out["accepted"], out["edited"], out["rejected"]) == (1, 1, 1)
    r = yaml.safe_load(reviewed.read_text())
    by = {i["id"]: i for i in r["items"]}
    assert by[acc["id"]]["status"] == "accepted"
    assert by[ed["id"]]["statement"] == "An order holds at most 50 lines."
    assert by[ed["id"]]["original_statement"].startswith("Max lines")
    assert by[rej["id"]]["status"] == "rejected"
    assert r["metadata"]["reviewer"] == "tester" and r["metadata"]["reviewed"] == "2026-10-09"
    assert r["metadata"]["counts"]["by_status"]["unreviewed"] == len(items) - 3

    scored = tmp_path / "scored.yaml"
    run(REVIEW, "--key", reviewed, "--filter-out", scored)
    s = yaml.safe_load(scored.read_text())
    assert sorted(i["id"] for i in s["items"]) == sorted([acc["id"], ed["id"]])

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\n## Rules\n"
                    "- Quantity must be greater than zero when a line is added (src/Domain/Order.cs:38).\n"
                    "- An order holds at most 50 lines (src/Domain/Order.cs:21).\n"
                    "- Orders can be exported to CSV (src/Domain/Order.cs:500).\n")
    outdir = tmp_path / "score"
    run(U.EVAL / "score.py", "--spec", spec, "--key", scored, "--source-root", SRC, "--no-llm",
        "--out-dir", outdir)
    sj = json.loads((outdir / "score.json").read_text())
    rc = sj["recall"]
    assert rc["full"] + rc["partial"] + rc["missed"] == 2       # only the reviewed items count
    blob = json.dumps(sj)
    assert acc["id"] in blob and ed["id"] in blob and rej["id"] not in blob
    assert sj["recall"]["any_match_pct"] == 100.0


def test_csv_decisions_round_trip(key, tmp_path):
    csv_p = tmp_path / "r.csv"
    run(REVIEW, "--key", key, "--csv", csv_p)
    rows = list(csv.DictReader(io.StringIO(csv_p.read_text())))
    rows[0]["decision"] = "accept"
    rows[1]["decision"] = "edit"
    rows[1]["edited_statement"] = "Edited in a spreadsheet."
    rows[2]["decision"] = "r"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    filled = tmp_path / "filled.csv"
    filled.write_text(buf.getvalue())
    out = tmp_path / "rev.yaml"
    run(REVIEW, "--key", key, "--apply", filled, "--reviewer", "sheet", "--date", "2026-10-10", "--out", out)
    r = yaml.safe_load(out.read_text())
    by = {i["id"]: i for i in r["items"]}
    assert by[rows[0]["id"]]["status"] == "accepted"
    assert by[rows[1]["id"]]["statement"] == "Edited in a spreadsheet."
    assert by[rows[2]["id"]]["status"] == "rejected"
    assert r["metadata"]["reviewer"] == "sheet"


def test_llm_draft_requires_a_cap(tmp_path):
    r = run(BUILD, "--source", SRC, "--out", tmp_path / "k.yaml", "--llm-draft", check=False)
    assert r.returncode == 2 and "--max-usd" in r.stderr

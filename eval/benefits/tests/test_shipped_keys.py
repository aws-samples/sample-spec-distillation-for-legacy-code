# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The shipped JC benefit keys load (fix 4). No AWS.

Regression: eval/keys/jc-changeorders/impact.yaml (``scenarios:`` with bucketed
locations) loaded 0 changes and field-app.yaml (``requirements:`` with ``kind``) loaded
0 items; both evaluators exited 0 with null metrics and the suite said "ok". A loadable
copy existed only as an undocumented hand conversion outside eval/keys/.
The loaders now normalise the shipped schema themselves, and an empty key is an error.
"""
from __future__ import annotations

import subprocess
import sys

import pytest
import yaml

from conftest_benefits import BENEFITS, REPO, TOY, no_aws  # noqa: F401  (fixture)

import common
import fieldapp_eval
import impact_eval

KEYS = REPO / "eval" / "keys" / "jc-changeorders"
NORMALISED = REPO / "eval" / "runs" / "_keys-normalized-jc"


def _locset(locs):
    return sorted((l["file"], l["start_line"], l["end_line"], l.get("bucket")) for l in locs)


def test_shipped_impact_key_loads_all_scenarios():
    cs = impact_eval.load_changes(KEYS / "impact.yaml")
    assert len(cs) == 10
    assert all(c["affected_locations"] for c in cs)
    first = cs[0]
    assert first["id"] == "IMP-01"
    assert {l["bucket"] for l in first["affected_locations"]} == {"must_change", "must_review"}
    assert all("reason" in l and "why" not in l for l in first["affected_locations"])
    assert first["affected_rules"] == ["BR-009", "BR-010"]
    assert first["affected_reports"] == [615]
    # the prose contract note becomes the identifiers it names, and is kept verbatim
    assert first["affected_contracts"] == ["requiresExecApproval"]
    assert first["contract_note"].startswith("Yes.")
    by_id = {c["id"]: c for c in cs}
    assert by_id["IMP-06"]["affected_contracts"] == []       # "No."
    assert by_id["IMP-10"]["affected_contracts"] == ["JOB_CLOSED"]


@pytest.mark.skipif(not (NORMALISED / "impact.changes.yaml").is_file(),
                    reason="hand-normalised reference copy not present")
def test_loader_reproduces_the_hand_normalised_locations():
    """Same locations, buckets, rules and reports as the working hand conversion."""
    mine = {c["id"]: c for c in impact_eval.load_changes(KEYS / "impact.yaml")}
    ref = yaml.safe_load((NORMALISED / "impact.changes.yaml").read_text())["changes"]
    assert set(mine) == {c["id"] for c in ref}
    for c in ref:
        m = mine[c["id"]]
        assert _locset(m["affected_locations"]) == _locset(c["affected_locations"]), c["id"]
        assert m["affected_rules"] == c["affected_rules"]
        assert m["affected_reports"] == c["affected_reports"]


def test_shipped_fieldapp_key_loads_all_requirements():
    items = fieldapp_eval.load_items(KEYS / "field-app.yaml")
    assert len(items) == 14
    assert items[0]["id"] == "FA-01" and items[0]["category"] == "payload"   # kind -> category
    assert all(it["requirement"] and it["locations"] for it in items)


@pytest.mark.skipif(not (NORMALISED / "field-app.items.yaml").is_file(),
                    reason="hand-normalised reference copy not present")
def test_fieldapp_loader_matches_the_hand_normalised_items():
    mine = fieldapp_eval.load_items(KEYS / "field-app.yaml")
    ref = yaml.safe_load((NORMALISED / "field-app.items.yaml").read_text())["items"]
    assert [(i["id"], i["category"], " ".join(i["requirement"].split())) for i in mine] == \
           [(i["id"], i["category"], " ".join(i["requirement"].split())) for i in ref]


def test_toy_keys_still_load():
    assert len(impact_eval.load_changes(TOY / "impact.yaml")) >= 2
    assert len(fieldapp_eval.load_items(TOY / "field-app.yaml")) >= 3


@pytest.mark.parametrize("loader,body", [
    (impact_eval.load_changes, "changes: []\n"),
    (impact_eval.load_changes, "corpus: x\nsomething_else: [1, 2]\n"),
    (fieldapp_eval.load_items, "items: []\n"),
    (fieldapp_eval.load_items, "corpus: x\nversion: 1\n"),
])
def test_a_key_that_loads_nothing_is_an_error(loader, body, tmp_path):
    k = tmp_path / "key.yaml"
    k.write_text(body)
    with pytest.raises(SystemExit) as e:
        loader(k)
    assert e.value.code == common.EXIT_BAD_KEY


def test_empty_key_fails_the_cli_before_any_call(tmp_path):
    """The process must exit non-zero, so run_suite records the stage as failed, not ok."""
    k = tmp_path / "empty.yaml"
    k.write_text("scenarios: []\n")
    p = subprocess.run([sys.executable, str(BENEFITS / "impact_eval.py"), "--impact", str(k),
                        "--source-root", str(TOY / "src"), "--out", str(tmp_path / "o"),
                        "--max-usd", "0"], capture_output=True, text=True)
    assert p.returncode == common.EXIT_BAD_KEY
    assert "loaded 0 changes" in p.stderr
    assert not (tmp_path / "o" / "impact.json").exists()

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The judge output contract and its dependency-free validator."""
from __future__ import annotations

import pytest

from judge import schema as sch

GOOD = {"judgments": [
    {"finding_id": "F1", "verdict": "matched",
     "matches": [{"key_id": "K-01", "degree": "full"}], "note": "", "confidence": "high"},
    {"finding_id": "F2", "verdict": "uncertain", "note": "evidence not shown"},
]}

BAD_CASES = [
    ("missing judgments", {}),
    ("judgments not a list", {"judgments": {"F1": "matched"}}),
    ("missing finding_id", {"judgments": [{"verdict": "matched"}]}),
    ("missing verdict", {"judgments": [{"finding_id": "F1"}]}),
    ("unknown verdict", {"judgments": [{"finding_id": "F1", "verdict": "probably"}]}),
    ("unknown degree", {"judgments": [{"finding_id": "F1", "verdict": "matched",
                                       "matches": [{"key_id": "K", "degree": "sort of"}]}]}),
    ("extra property", {"judgments": [{"finding_id": "F1", "verdict": "matched", "score": 0.9}]}),
    ("extra property in match", {"judgments": [{"finding_id": "F1", "verdict": "matched",
                                                "matches": [{"key_id": "K", "degree": "full",
                                                             "why": "x"}]}]}),
    ("finding_id not a string", {"judgments": [{"finding_id": 1, "verdict": "matched"}]}),
    ("matches not a list", {"judgments": [{"finding_id": "F1", "verdict": "matched",
                                           "matches": {"key_id": "K"}}]}),
    ("match missing key_id", {"judgments": [{"finding_id": "F1", "verdict": "matched",
                                             "matches": [{"degree": "full"}]}]}),
    ("note too long", {"judgments": [{"finding_id": "F1", "verdict": "matched",
                                      "note": "x" * 401}]}),
    ("top level is a list", [{"finding_id": "F1", "verdict": "matched"}]),
    ("top level is a string", "matched"),
]


def test_good_output_validates():
    sch.validate(GOOD)


@pytest.mark.parametrize("name,obj", BAD_CASES, ids=[c[0] for c in BAD_CASES])
def test_bad_output_raises(name, obj):
    with pytest.raises(sch.SchemaError):
        sch.validate(obj)


def test_error_message_names_the_path():
    with pytest.raises(sch.SchemaError, match=r"\$\.judgments\[0\]\.verdict"):
        sch.validate({"judgments": [{"finding_id": "F1", "verdict": "nope"}]})


def test_empty_judgment_list_is_valid_shape():
    sch.validate({"judgments": []})


def test_uncertain_is_an_allowed_verdict():
    assert "uncertain" in sch.VERDICTS
    sch.validate({"judgments": [{"finding_id": "F1", "verdict": "uncertain"}]})


def test_booleans_are_not_strings():
    with pytest.raises(sch.SchemaError):
        sch.validate({"judgments": [{"finding_id": True, "verdict": "matched"}]})


def test_schema_hash_is_stable_and_short():
    h1, h2 = sch.schema_hash(), sch.schema_hash()
    assert h1 == h2 and len(h1) == 16


def test_tool_config_shape():
    tc = sch.tool_config()
    assert tc["toolChoice"] == {"tool": {"name": sch.TOOL_NAME}}
    spec = tc["tools"][0]["toolSpec"]
    assert spec["name"] == sch.TOOL_NAME
    assert spec["inputSchema"]["json"] is sch.JUDGE_OUTPUT_SCHEMA


def test_canonical_is_order_independent():
    assert sch.canonical({"a": 1, "b": 2}) == sch.canonical({"b": 2, "a": 1})


# --------------------------- cross-check against a real JSON Schema implementation
def test_builtin_validator_agrees_with_jsonschema():
    jsonschema = pytest.importorskip("jsonschema")
    cases = [GOOD] + [obj for _, obj in BAD_CASES]
    for obj in cases:
        mine_ok = True
        try:
            sch.validate(obj)
        except sch.SchemaError:
            mine_ok = False
        theirs_ok = True
        try:
            jsonschema.validate(obj, sch.JUDGE_OUTPUT_SCHEMA)
        except jsonschema.ValidationError:
            theirs_ok = False
        assert mine_ok == theirs_ok, (obj, mine_ok, theirs_ok)


# ----------------------------------------------------- null optional normalisation
def test_null_note_is_dropped_and_then_validates():
    raw = {"judgments": [{"finding_id": "F1", "verdict": "uncertain", "note": None}]}
    with pytest.raises(sch.SchemaError):      # the validator itself stays strict
        sch.validate(raw)
    fixed = sch.drop_null_optionals(raw)
    assert "note" not in fixed["judgments"][0]
    sch.validate(fixed)
    assert raw["judgments"][0]["note"] is None   # the input is not mutated


def test_every_optional_property_may_be_null():
    raw = {"judgments": [{"finding_id": "F1", "verdict": "uncertain", "note": None,
                          "confidence": None, "matches": None}]}
    fixed = sch.drop_null_optionals(raw)
    assert fixed == {"judgments": [{"finding_id": "F1", "verdict": "uncertain"}]}
    sch.validate(fixed)


@pytest.mark.parametrize("field", ["verdict", "finding_id"])
def test_null_on_a_required_field_still_fails(field):
    j = {"finding_id": "F1", "verdict": "matched", "note": None}
    j[field] = None
    fixed = sch.drop_null_optionals({"judgments": [j]})
    assert field in fixed["judgments"][0]       # required keys are never dropped
    with pytest.raises(sch.SchemaError):
        sch.validate(fixed)


def test_null_required_field_in_a_match_still_fails():
    raw = {"judgments": [{"finding_id": "F1", "verdict": "matched",
                          "matches": [{"key_id": "K-01", "degree": None}]}]}
    with pytest.raises(sch.SchemaError):
        sch.validate(sch.drop_null_optionals(raw))


def test_wrong_verdict_value_is_not_remapped():
    raw = {"judgments": [{"finding_id": "F1", "verdict": "matches"}]}
    assert sch.drop_null_optionals(raw) == raw
    with pytest.raises(sch.SchemaError):
        sch.validate(sch.drop_null_optionals(raw))


def test_unknown_null_property_is_not_hidden():
    raw = {"judgments": [{"finding_id": "F1", "verdict": "matched", "score": None}]}
    with pytest.raises(sch.SchemaError):
        sch.validate(sch.drop_null_optionals(raw))


def test_normalising_leaves_the_contract_and_its_hash_alone():
    before = sch.schema_hash()
    sch.drop_null_optionals(GOOD)
    assert sch.schema_hash() == before
    assert sch.drop_null_optionals(GOOD) == GOOD

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Content-addressed judge cache."""
from __future__ import annotations

from judge.cache import JudgeCache

PAYLOAD = {"prompt": "p", "model": "m", "schema": "s", "temperature": 0.0}
VALUE = {"judgments": [{"finding_id": "F1", "verdict": "matched"}]}


def test_miss_then_hit(tmp_path):
    c = JudgeCache(tmp_path)
    k = c.key(PAYLOAD)
    assert c.get(k) is None and c.misses == 1 and c.hits == 0
    c.put(k, VALUE, {"tag": "t"})
    assert c.writes == 1
    assert c.get(k) == VALUE and c.hits == 1


def test_a_second_cache_object_sees_the_entry(tmp_path):
    k = JudgeCache(tmp_path).key(PAYLOAD)
    JudgeCache(tmp_path).put(k, VALUE)
    assert JudgeCache(tmp_path).get(k) == VALUE


def test_any_knob_change_is_a_miss(tmp_path):
    c = JudgeCache(tmp_path)
    c.put(c.key(PAYLOAD), VALUE)
    for field, new in (("prompt", "p2"), ("model", "m2"), ("schema", "s2"), ("temperature", 0.3)):
        assert c.get(c.key({**PAYLOAD, field: new})) is None, field


def test_key_is_order_independent(tmp_path):
    c = JudgeCache(tmp_path)
    a = c.key({"prompt": "p", "model": "m"})
    b = c.key({"model": "m", "prompt": "p"})
    assert a == b


def test_disabled_cache_neither_reads_nor_writes(tmp_path):
    hot = JudgeCache(tmp_path)
    k = hot.key(PAYLOAD)
    hot.put(k, VALUE)
    cold = JudgeCache(tmp_path, enabled=False)
    assert cold.get(k) is None
    cold.put(k, {"judgments": []})
    assert hot.get(k) == VALUE          # unchanged on disk
    assert cold.writes == 0


def test_corrupt_entry_is_a_miss_not_a_crash(tmp_path):
    c = JudgeCache(tmp_path)
    k = c.key(PAYLOAD)
    p = c.path_for(k)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ this is not json")
    assert c.get(k) is None


def test_wrong_format_version_is_a_miss(tmp_path):
    import json
    c = JudgeCache(tmp_path)
    k = c.key(PAYLOAD)
    p = c.path_for(k)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"format": 0, "value": VALUE}))
    assert c.get(k) is None


def test_stats(tmp_path):
    c = JudgeCache(tmp_path)
    k = c.key(PAYLOAD)
    c.get(k)
    c.put(k, VALUE)
    c.get(k)
    s = c.stats()
    assert s == {"enabled": True, "root": str(tmp_path), "hits": 1, "misses": 1,
                 "writes": 1, "hit_rate_pct": 50.0}


def test_no_partial_files_are_left_behind(tmp_path):
    c = JudgeCache(tmp_path)
    c.put(c.key(PAYLOAD), VALUE)
    assert not list(tmp_path.rglob("*.tmp"))
    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_entries_are_sharded_by_prefix(tmp_path):
    c = JudgeCache(tmp_path)
    k = c.key(PAYLOAD)
    c.put(k, VALUE)
    assert c.path_for(k).parent.name == k[:2]

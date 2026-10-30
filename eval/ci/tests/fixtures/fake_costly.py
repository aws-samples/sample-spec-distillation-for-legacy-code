#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test double for an evaluator that spends money: writes a token log the suite will charge.

It never calls Bedrock; it writes the same JSONL record shape that
``eval/bedrock_util.py`` writes, into its own output directory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--usd", type=float, default=0.5, help="cost to report")
    ap.add_argument("--calls", type=int, default=1)
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    with open(a.out_dir / "bedrock_calls.jsonl", "a") as f:
        for i in range(a.calls):
            # the tag carries the output dir so records from two stages are distinct,
            # the way real per-call timestamps are
            f.write(json.dumps({"ts": f"2026-01-01T00:00:{i:02d}+00:00",
                                "tag": f"fake:{a.out_dir.name}",
                                "model": "fake", "input_tokens": 1000, "output_tokens": 100,
                                "cost_usd": round(a.usd / a.calls, 6)}) + "\n")
    (a.out_dir / "result.json").write_text(json.dumps({"spent_usd": a.usd}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

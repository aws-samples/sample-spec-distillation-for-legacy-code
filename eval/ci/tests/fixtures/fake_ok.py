#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test double for an evaluator that succeeds. Writes result.json with chosen metrics."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--value", type=float, default=90.0)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--spec", type=Path)
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "result.json").write_text(json.dumps({
        "method": "deterministic" if a.no_llm else "llm",
        "recall": {"any_match_pct": a.value, "strict_full_pct": a.value - 5},
        "citations": {"validity_pct": 100.0},
    }, indent=2))
    print(json.dumps({"value": a.value}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

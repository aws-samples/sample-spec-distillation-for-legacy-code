# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Content-addressed judge cache: hash(prompt + model + schema + knobs) -> verdicts.

Re-scoring the same spec against the same key is then free and byte-identical,
which is what makes a published number reproducible months later. The cache key
covers everything that could change a verdict, so a changed prompt, model,
schema or temperature is a miss rather than a stale hit.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .schema import canonical

CACHE_FORMAT = 2


class JudgeCache:
    """Filesystem cache under ``root`` (default ``eval/judge/cache/``).

    ``enabled=False`` (the ``--no-cache`` path) neither reads nor writes, so a
    run can be forced to hit Bedrock for real.
    """

    def __init__(self, root: Path | str, enabled: bool = True):
        self.root = Path(root)
        self.enabled = bool(enabled)
        self.hits = 0
        self.misses = 0
        self.writes = 0

    # ------------------------------------------------------------------ keying
    @staticmethod
    def key(payload: dict) -> str:
        """sha256 over the canonical form of everything that affects the verdict."""
        return hashlib.sha256(canonical({"format": CACHE_FORMAT, **payload}).encode()).hexdigest()

    def path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    # ------------------------------------------------------------------- access
    def get(self, key: str):
        if not self.enabled:
            self.misses += 1
            return None
        p = self.path_for(key)
        if not p.exists():
            self.misses += 1
            return None
        try:
            rec = json.loads(p.read_text())
        except Exception:
            # A corrupt entry is a miss, not a crash; it gets overwritten.
            self.misses += 1
            return None
        if rec.get("format") != CACHE_FORMAT or "value" not in rec:
            self.misses += 1
            return None
        self.hits += 1
        return rec["value"]

    def contains_any(self, keys) -> bool:
        """True if any of ``keys`` is a readable entry. Touches no hit/miss counters, so
        the cost plan can ask "would this call be free?" without skewing the stats."""
        if not self.enabled:
            return False
        for k in keys:
            if self._read(k) is not None:
                return True
        return False

    def get_any(self, keys):
        """First readable entry among ``keys`` (in order) as ``(value, key)``, else
        ``(None, None)``. Counts exactly one hit or one miss, however many keys it tried."""
        if not self.enabled:
            self.misses += 1
            return None, None
        for k in keys:
            v = self._read(k)
            if v is not None:
                self.hits += 1
                return v, k
        self.misses += 1
        return None, None

    def _read(self, key: str):
        p = self.path_for(key)
        if not p.exists():
            return None
        try:
            rec = json.loads(p.read_text())
        except Exception:
            return None
        if rec.get("format") != CACHE_FORMAT or "value" not in rec:
            return None
        return rec["value"]

    def put(self, key: str, value, meta: dict | None = None) -> None:
        if not self.enabled:
            return
        p = self.path_for(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"format": CACHE_FORMAT, "key": key,
               "created": datetime.now(timezone.utc).isoformat(),
               "meta": meta or {}, "value": value}
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rec, indent=1, sort_keys=True))
        tmp.replace(p)  # atomic: concurrent scorers never see a half-written entry
        self.writes += 1

    # -------------------------------------------------------------------- stats
    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"enabled": self.enabled, "root": str(self.root),
                "hits": self.hits, "misses": self.misses, "writes": self.writes,
                "hit_rate_pct": round(100.0 * self.hits / total, 1) if total else None}

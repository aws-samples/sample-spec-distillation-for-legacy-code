#!/usr/bin/env python3
"""
id_vending_machine.py — the single, authoritative source of unique concept IDs.

WHY THIS EXISTS
---------------
Human-facing concept IDs (RL-0001, DT-0042, ...) were previously minted by a
PER-FILE counter in format_still.py that reset to 0001 for every file. Result:
the same ID (e.g. RL-0001) was reused for many DIFFERENT concepts across the
files of a still, corrupting the 14k+ in-text cross-references that treat an ID
as a unique handle.

This module replaces that with a deterministic VENDING MACHINE: a caller asks for
an ID for a category (e.g. "RL") and is assigned a unique one. Uniqueness and the
lock are owned here; every issuance is audit-logged with provenance.

DESIGN CONTRACT
---------------
- SINGLE SOURCE OF TRUTH: a SQLite file is the authority. In-process we cache one
  machine per db path (a true singleton per file); cross-process safety comes from
  SQLite locking (BEGIN IMMEDIATE + busy_timeout).
- IDEMPOTENT / STABLE ACROSS RE-RUNS: vending is keyed on a stable `claim_key`
  (recommended: sha256(still + title + statement)[:16]). Re-requesting the same
  claim returns the SAME id (reused=True) — so re-formatting a corpus does NOT
  churn IDs. This is what makes it deterministic, not merely monotonic.
- FULL AUDIT: every call (new or reused) is logged with caller identity, call
  site, pipeline phase, and a free-form evidence blob.
- NEVER REUSES A NUMBER: a sequence value, once handed out, is never issued again
  for that category.

SCHEMA (in the vending db, default <output>/ids.db)
    id_sequences(category PK, last_seq)
    id_claims(category, claim_key, issued_id, seq, first_ts, PRIMARY KEY(category, claim_key))
    id_vend_log(id PK, category, claim_key, issued_id, reused, caller, call_site,
                phase, evidence, ts)

Public API:
    vm = get_vending_machine(db_path)
    res = vm.claim(category, claim_key, caller, phase, evidence=None, call_site="")
        -> {"id","category","seq","reused","claim_key"}
    make_claim_key(still, title, statement) -> str   # the recommended key basis
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time

_PAD = 4  # minimum zero-pad width for the numeric part (RL-0001)

# in-process singleton registry: resolved_db_path -> IdVendingMachine
_MACHINES: dict[str, "IdVendingMachine"] = {}
_REGISTRY_LOCK = threading.Lock()

# The default machine location, bound ONCE by the orchestrator (e.g. the format
# driver) via configure(). Consumers then call get_default() and never need to
# know or compute where the ledger lives — they just walk up and ask.
_DEFAULT_DB_PATH: str | None = None


def make_claim_key(still: str, title: str, statement: str) -> str:
    """Stable idempotency key for a concept: sha256(still\\ntitle\\nstatement)[:16].

    Title should include any [XX-NNNN] prefix stripped BEFORE calling if you want
    the key to survive renumbering; callers here pass the human title text they
    consider identity-bearing. Keeping it deterministic is the only requirement.
    """
    h = hashlib.sha256()
    h.update((still or "").strip().encode("utf-8"))
    h.update(b"\n")
    h.update((title or "").strip().encode("utf-8"))
    h.update(b"\n")
    h.update((statement or "").strip().encode("utf-8"))
    return h.hexdigest()[:16]


class IdVendingMachine:
    """Owns the lock and uniqueness for one vending db file."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        import os as _os
        _parent = _os.path.dirname(_os.path.abspath(db_path))
        if _parent:
            _os.makedirs(_parent, exist_ok=True)  # ensure the ledger's dir exists
        self._lock = threading.Lock()  # serialize in-process access to the connection
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    def _ensure_schema(self):
        c = self._conn
        c.execute("CREATE TABLE IF NOT EXISTS id_sequences("
                  "category TEXT PRIMARY KEY, last_seq INTEGER NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS id_claims("
                  "category TEXT, claim_key TEXT, issued_id TEXT, seq INTEGER, "
                  "first_ts REAL, PRIMARY KEY(category, claim_key))")
        c.execute("CREATE TABLE IF NOT EXISTS id_vend_log("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, claim_key TEXT, "
                  "issued_id TEXT, reused INTEGER, caller TEXT, call_site TEXT, "
                  "phase TEXT, evidence TEXT, ts REAL)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_vendlog_issued ON id_vend_log(issued_id)")
        c.commit()

    def claim(self, category: str, claim_key: str, caller: str = "",
              phase: str = "", evidence=None, call_site: str = "") -> dict:
        """Assign (or return the existing) unique ID for (category, claim_key).

        Atomic per call (BEGIN IMMEDIATE). Idempotent on claim_key. Always logs.
        """
        if not category or not claim_key:
            raise ValueError("category and claim_key are required")
        category = category.strip()
        ev = evidence if isinstance(evidence, str) else json.dumps(evidence or {})
        now = time.time()

        with self._lock:
            c = self._conn
            try:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute(
                    "SELECT issued_id, seq FROM id_claims WHERE category=? AND claim_key=?",
                    (category, claim_key),
                ).fetchone()
                if row is not None:
                    issued_id, seq = row[0], row[1]
                    reused = True
                else:
                    c.execute("INSERT OR IGNORE INTO id_sequences(category, last_seq) VALUES(?, 0)",
                              (category,))
                    c.execute("UPDATE id_sequences SET last_seq = last_seq + 1 WHERE category=?",
                              (category,))
                    seq = c.execute("SELECT last_seq FROM id_sequences WHERE category=?",
                                    (category,)).fetchone()[0]
                    issued_id = f"{category}-{seq:0{_PAD}d}"
                    c.execute(
                        "INSERT INTO id_claims(category, claim_key, issued_id, seq, first_ts) "
                        "VALUES(?,?,?,?,?)", (category, claim_key, issued_id, seq, now))
                    reused = False
                c.execute(
                    "INSERT INTO id_vend_log(category, claim_key, issued_id, reused, caller, "
                    "call_site, phase, evidence, ts) VALUES(?,?,?,?,?,?,?,?,?)",
                    (category, claim_key, issued_id, 1 if reused else 0, caller,
                     call_site, phase, ev, now))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return {"id": issued_id, "category": category, "seq": seq,
                "reused": reused, "claim_key": claim_key}

    def peek(self, category: str) -> int:
        """Current last_seq for a category (0 if none)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seq FROM id_sequences WHERE category=?", (category,)).fetchone()
        return row[0] if row else 0

    def stats(self) -> dict:
        with self._lock:
            seqs = {cat: n for cat, n in self._conn.execute(
                "SELECT category, last_seq FROM id_sequences ORDER BY category")}
            claims = self._conn.execute("SELECT COUNT(*) FROM id_claims").fetchone()[0]
            logs = self._conn.execute("SELECT COUNT(*) FROM id_vend_log").fetchone()[0]
        return {"sequences": seqs, "total_claims": claims, "total_log_rows": logs}


def get_vending_machine(db_path: str) -> IdVendingMachine:
    """Return the process-singleton machine for this db file (created on first use)."""
    import os
    key = os.path.abspath(db_path)
    with _REGISTRY_LOCK:
        vm = _MACHINES.get(key)
        if vm is None:
            vm = IdVendingMachine(db_path)
            _MACHINES[key] = vm
        return vm


def configure(db_path: str) -> None:
    """Bind the default vending-machine location for this run. Called ONCE by the
    orchestrator that knows the run root. Consumers use get_default() thereafter."""
    global _DEFAULT_DB_PATH
    _DEFAULT_DB_PATH = db_path


def get_default() -> IdVendingMachine:
    """Return the run-scoped machine. Consumers call this and stay ignorant of where
    the ledger lives — the location is resolved from the run manifest (single source
    of run-scoped paths). An explicit configure() override wins, for tests/standalone."""
    if _DEFAULT_DB_PATH:  # explicit override (tests / standalone)
        return get_vending_machine(_DEFAULT_DB_PATH)
    import run_manager
    return get_vending_machine(run_manager.resolve("output:stills/ids.db"))


# ── CLI (inspection / manual claim) ──────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="ID vending machine (deterministic, audited).")
    ap.add_argument("db_path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("claim")
    pc.add_argument("category")
    pc.add_argument("claim_key")
    pc.add_argument("--caller", default="cli")
    pc.add_argument("--phase", default="manual")
    pc.add_argument("--evidence", default="")
    sub.add_parser("stats")
    ps = sub.add_parser("peek"); ps.add_argument("category")
    args = ap.parse_args()

    vm = get_vending_machine(args.db_path)
    if args.cmd == "claim":
        print(json.dumps(vm.claim(args.category, args.claim_key, caller=args.caller,
                                  phase=args.phase, evidence=args.evidence), indent=2))
    elif args.cmd == "stats":
        print(json.dumps(vm.stats(), indent=2))
    elif args.cmd == "peek":
        print(vm.peek(args.category))

#!/usr/bin/env python3
"""
run_manager.py — instance run manifest + runtime base-path resolution.

GOVERNING PRINCIPLE (System/Class vs Instance)
----------------------------------------------
Framework code (this repo) is System/Class level: GENERIC, with zero baked-in
awareness of any specific run/project. A single run is Instance level: its
`run.json` holds the instance/project details. Framework code only ever sees
instance data at runtime, in memory, injected through this module. Nothing here
hard-codes an instance path, target name, profile, or secret.

THREE ENVIRONMENT BASES, everything else is (base, relative)
------------------------------------------------------------
Only three roots are environment-specific; every path of consequence is stored
as "base:relative" and resolved against a base at runtime, so an instance is
portable across environments (the sibling-vs-nested-source problem disappears):

  framework : the operational-code root. SELF-LOCATING from __file__.
  output    : the instance/run root (the dir holding run.json). SELF-LOCATING by
              discovery (walk up to find run.json) or set explicitly.
  source    : the raw source tree. ENV-BOUND at runtime (DISTILL_SOURCE_BASE /
              explicit bind / clarifying question). MAY be absent.

Requesting a path whose base is unbound raises UnresolvedBase(base) — callers turn
that into a clarifying question, never a guess.

PUBLIC API
----------
  set_config(run_root, ...) -> dict     # create/merge+write run.json, bind bases
  load(path) -> dict                    # discover+load run.json (walk up), bind
  get_config() -> dict                  # bound config (or resolve via env), else raise
  resolve("output:stills/ids.db") -> abspath   # base+relative -> absolute
  bind_source(path)                     # explicitly bind the source base
  run_id() -> str

Manifests are instance data and belong under output/ (gitignored), never in the
repo. Secrets are NEVER written (curated env allowlist + defensive secret regex).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 1

DEFAULT_PATHS = {
    "stills_dir": "output:stills",
    "pages_dir": "output:.pages",
    "ids_db": "output:stills/ids.db",
    "shared_vectors_db": "output:stills/shared_vectors.db",
    "viewer_dir": "output:viewer",
}

# Curated env snapshot: allowlist + prefixes, minus a defensive secret filter.
_ENV_ALLOW = {"AWS_PROFILE", "AWS_DEFAULT_REGION", "AWS_REGION"}
_ENV_PREFIXES = ("DISTILL_",)
_SECRET_RE = re.compile(r"(?i)(secret|token|key|password|credential|passwd)")


class UnresolvedBase(Exception):
    """Raised when a path references an environment base that is not bound."""

    def __init__(self, base: str):
        self.base = base
        super().__init__(
            f"unresolved environment base '{base}': it is not bound in this "
            f"environment. Provide it (env/CLI) or ask the operator — do not guess.")


# In-process binding (the runtime singleton). Instance identity lives in the file;
# these are the resolved, env-specific roots for THIS process.
_STATE = {
    "config": None,        # loaded run.json dict
    "run_root": None,      # 'output' base (abspath)
    "source_base": None,   # 'source' base (abspath) or None
}


def _framework_base() -> str:
    """Repo root, self-located from this file (…/runtimes/mcp-server/run_manager.py)."""
    return str(Path(__file__).resolve().parents[2])


def _tool_version() -> str:
    vf = Path(_framework_base()) / "VERSION"
    try:
        return vf.read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


def _env_snapshot() -> dict:
    out = {}
    for k, v in os.environ.items():
        if _SECRET_RE.search(k):
            continue
        if k in _ENV_ALLOW or any(k.startswith(p) for p in _ENV_PREFIXES):
            out[k] = v
    return out


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)  # atomic on same filesystem


def _run_json_path(run_root: str) -> Path:
    return Path(run_root) / "run.json"


def set_config(run_root: str, project_label: str = "", source_base: str = "",
               paths: dict | None = None, params: dict | None = None) -> dict:
    """Create or MERGE the instance manifest and bind bases. Non-destructive:
    an existing run_id/created_at is preserved; paths/params are merged, not
    clobbered. Writes run.json atomically."""
    run_root = str(Path(run_root).resolve())
    rj = _run_json_path(run_root)

    if rj.exists():
        cfg = json.loads(rj.read_text(encoding="utf-8"))
    else:
        cfg = {
            "run_id": str(uuid.uuid4()),
            "schema_version": SCHEMA_VERSION,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "paths": dict(DEFAULT_PATHS),
            "params": {},
        }

    # Merge (never clobber identity)
    cfg.setdefault("run_id", str(uuid.uuid4()))
    cfg.setdefault("schema_version", SCHEMA_VERSION)
    cfg.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    cfg["tool_version"] = _tool_version()
    cfg["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if project_label:
        cfg["project_label"] = project_label
    merged_paths = dict(DEFAULT_PATHS)
    merged_paths.update(cfg.get("paths", {}))
    if paths:
        merged_paths.update(paths)
    cfg["paths"] = merged_paths
    if params:
        cfg.setdefault("params", {}).update(params)
    cfg["env_snapshot"] = _env_snapshot()
    # Record the source location as a NON-AUTHORITATIVE provenance hint (e.g. the
    # origin env's path). BIND the source base only if it actually exists in THIS
    # env — otherwise it stays unbound so resolve('source:...') raises for a
    # clarifying question instead of returning a dead path.
    sb = source_base or os.environ.get("DISTILL_SOURCE_BASE", "")
    if sb:
        # Store LITERALLY — this may be an origin-env path that must not be
        # resolved against the current filesystem (would mangle it).
        cfg["source_base_hint"] = sb

    _atomic_write_json(rj, cfg)

    _STATE["config"] = cfg
    _STATE["run_root"] = run_root
    # Bind only if the source actually exists in THIS env; else leave unbound so
    # resolve('source:...') raises UnresolvedBase for a clarifying question.
    _STATE["source_base"] = str(Path(sb).resolve()) if (sb and Path(sb).exists()) else None
    return cfg


def load(path: str) -> dict:
    """Discover run.json by walking up from `path` (file or dir) and bind it."""
    p = Path(path).resolve()
    if p.is_file():
        p = p.parent
    for cand in [p, *p.parents]:
        rj = cand / "run.json"
        if rj.exists():
            cfg = json.loads(rj.read_text(encoding="utf-8"))
            _STATE["config"] = cfg
            _STATE["run_root"] = str(cand)
            env_sb = os.environ.get("DISTILL_SOURCE_BASE", "")
            _STATE["source_base"] = str(Path(env_sb).resolve()) if env_sb else None
            return cfg
    raise FileNotFoundError(f"no run.json found walking up from {path}")


def get_config() -> dict:
    """Return the bound config; if none, try DISTILL_RUN_ROOT; else raise."""
    if _STATE["config"] is not None:
        return _STATE["config"]
    env_root = os.environ.get("DISTILL_RUN_ROOT")
    if env_root and _run_json_path(env_root).exists():
        return load(env_root)
    raise RuntimeError(
        "run_manager is not configured for this process. Call set_config()/load(), "
        "or set DISTILL_RUN_ROOT to an instance dir containing run.json.")


def bind_source(path: str) -> None:
    """Explicitly bind the source base for this process (runtime, not persisted)."""
    _STATE["source_base"] = str(Path(path).resolve())


def _base_root(base: str) -> str | None:
    if base == "framework":
        return _framework_base()
    if base == "output":
        if _STATE["run_root"] is None:
            get_config()  # attempt env/binding
        return _STATE["run_root"]
    if base == "source":
        return _STATE["source_base"] or os.environ.get("DISTILL_SOURCE_BASE") or None
    raise ValueError(f"unknown base '{base}' (expected framework|output|source)")


def resolve(tagged_path: str) -> str:
    """Resolve 'base:relative' (or a bare relative, assumed 'output') to an
    absolute path. Raises UnresolvedBase if the base is not bound."""
    if ":" in tagged_path and not tagged_path[1:3] == ":\\":  # avoid windows drive
        base, rel = tagged_path.split(":", 1)
    else:
        base, rel = "output", tagged_path
    root = _base_root(base)
    if not root:
        raise UnresolvedBase(base)
    return str(Path(root) / rel) if rel else str(root)


def run_id() -> str:
    return get_config().get("run_id", "")


# ── CLI (inspect / init) ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Inspect or initialize a run manifest.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("show"); ps.add_argument("path")
    pr = sub.add_parser("resolve"); pr.add_argument("path"); pr.add_argument("tagged")
    pi = sub.add_parser("init")
    pi.add_argument("run_root")
    pi.add_argument("--project-label", default="")
    pi.add_argument("--source-base", default="")
    args = ap.parse_args()

    if args.cmd == "show":
        print(json.dumps(load(args.path), indent=2))
    elif args.cmd == "resolve":
        load(args.path)
        print(resolve(args.tagged))
    elif args.cmd == "init":
        cfg = set_config(args.run_root, project_label=args.project_label,
                         source_base=args.source_base)
        print(json.dumps(cfg, indent=2))

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test data sets for the launcher: build an answer key from a run's own SOURCE (code + tests)
with eval/keygen/build_key.py, let a person review it, and hand the reviewed key to Full
evaluation.

Independence rule: the data set is built from the code and its tests only, never from anything
the pipeline produced. The source directory is refused when it is (or contains, or sits inside)
a run's output directory, and build_key.py is given only --source and an --out inside the data
set directory, never a spec, viewer or other output path.

Layout: <runs-dir>/_datasets/<run_id>/<dataset_id>/
    dataset.json            job state, options, source root, counts
    build.log               build_key.py output
    ANSWER_KEY.draft.yaml   build_key.py output (status: unreviewed everywhere)
    decisions.json          review decisions {id: {decision, statement?, reviewer, at}}
    ANSWER_KEY.reviewed.yaml  review.py apply_decisions(draft, decisions)
    ANSWER_KEY.yaml         review.py filter_key(reviewed): accepted + edited (+ planted)
    suite.yaml              ad-hoc run_suite.py config (from eval/ci/suites/eshoponweb.yaml)
Nothing is ever written into a run directory, so imported runs stay read-only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

DATASETS_DIRNAME = "_datasets"
DATASET_FIELDS = {"ai_suggestions", "max_usd"}
REVIEW_FIELDS = {"decisions", "approve", "reviewer"}
REVIEW_ACTIONS = {"approve": "accept", "edit": "edit", "reject": "reject"}
# {"approve": "corroborated"}: every undecided item confirmed by both code and tests;
# {"approve": "all"}: every undecided item (a rejected or edited item keeps its decision)
BULK_APPROVALS = ("corroborated", "all")
ACTIVE = {"building"}
MAX_DECISIONS = 2000
MAX_STATEMENT = 2000
MAX_REVIEWER = 80
PAGE_SIZE = 25
AI_MAX_USD_CAP = 10.0
ID_RX = re.compile(r"^[A-Za-z]{1,8}-[A-Za-z0-9]{1,32}$")
INDEPENDENCE = ("Built from this code and its tests, not from the generated spec, so it can fairly "
                "grade the spec.")
# source kind (first part of an item's source) -> the plain word the UI shows
SOURCE_WORDS = {"harvest": "code", "tests": "tests", "llm-draft": "AI", "docs": "docs", "planted": "planted"}
STATUS_OF = {"accept": "approved", "edit": "edited", "reject": "rejected"}


class DatasetError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    tmp.replace(p)


def looks_like_output(d: Path) -> bool:
    """A run_distillation.sh output directory (or its viewer) rather than source code."""
    return any((d / f).exists() for f in ("run_status.json", "run_manifest.json", "launcher.json",
                                          "bottled_spec", "master_blend")) or \
        ((d / "index.html").is_file() and (d / "pages").is_dir())


def independence_refusal(src: Path, run_dir: Path, runs_dir: Path) -> str | None:
    """Why `src` may not be used to build a test data set for the run in `run_dir` (None = ok)."""
    src, run_dir, runs_dir = src.resolve(), run_dir.resolve(), runs_dir.resolve()
    clone = run_dir / ".source"
    if src == clone or src.is_relative_to(clone):
        return None                                   # the run's own copy of the source
    if src == run_dir or src.is_relative_to(run_dir):
        return "it is the run's own output (spec, viewer, ...)"
    if run_dir.is_relative_to(src) or runs_dir.is_relative_to(src):
        return "it contains the run's output directory, so the generated spec would be read"
    if src.is_relative_to(runs_dir):
        return "it is inside the launcher's runs directory (pipeline output)"
    if looks_like_output(src) or (src / "viewer").is_dir() and (src / "stills").is_dir():
        return "it looks like a pipeline output directory, not source code"
    return None


class Dataset:
    FIELDS = ("dataset_id", "run_id", "state", "created", "started", "finished", "error",
              "source_root", "ai_suggestions", "max_usd", "counts", "exit_code", "build_argv")

    def __init__(self, dataset_id: str, run_id: str, out_dir: Path, source_root: str,
                 ai_suggestions: bool = False, max_usd: float | None = None):
        self.dataset_id, self.run_id, self.dir = dataset_id, run_id, out_dir
        self.source_root, self.ai_suggestions, self.max_usd = source_root, ai_suggestions, max_usd
        self.state = "building"
        self.created = _now()
        self.started = self.finished = self.error = None
        self.counts: dict | None = None
        self.exit_code: int | None = None
        self.build_argv: list | None = None
        self._cache: tuple | None = None

    draft = property(lambda self: self.dir / "ANSWER_KEY.draft.yaml")
    reviewed = property(lambda self: self.dir / "ANSWER_KEY.reviewed.yaml")
    scored = property(lambda self: self.dir / "ANSWER_KEY.yaml")
    decisions_path = property(lambda self: self.dir / "decisions.json")
    suite_path = property(lambda self: self.dir / "suite.yaml")
    log = property(lambda self: self.dir / "build.log")

    def save(self) -> None:
        _write_json(self.dir / "dataset.json", {k: getattr(self, k) for k in self.FIELDS})

    @classmethod
    def load(cls, d: Path) -> "Dataset | None":
        x = _read_json(d / "dataset.json")
        if not isinstance(x, dict) or "dataset_id" not in x:
            return None
        ds = cls(x["dataset_id"], x.get("run_id") or "", d, x.get("source_root") or "")
        for k in cls.FIELDS:
            if k in x:
                setattr(ds, k, x[k])
        return ds

    # items + decisions -------------------------------------------------------------------------
    def items(self) -> list[dict]:
        import yaml
        try:
            st = self.draft.stat()
        except OSError:
            return []
        if self._cache and self._cache[0] == (st.st_mtime_ns, st.st_size):
            return self._cache[1]
        d = yaml.safe_load(self.draft.read_text(encoding="utf-8")) or {}
        items = [it for it in d.get("items") or [] if isinstance(it, dict) and "id" in it]
        self._cache = ((st.st_mtime_ns, st.st_size), items)
        return items

    def decisions(self) -> dict:
        d = _read_json(self.decisions_path)
        return d if isinstance(d, dict) else {}


def review_status(it: dict, dec: dict) -> str:
    x = dec.get(str(it["id"]))
    if x:
        return STATUS_OF.get(x.get("decision"), "unreviewed")
    return "planted" if it.get("status") == "planted" else "unreviewed"


def source_words(it: dict) -> list[str]:
    return sorted({SOURCE_WORDS.get(s.split(":")[0], s.split(":")[0]) for s in it.get("source") or []})


def summarize(items: list[dict], dec: dict) -> dict:
    by_status = {"unreviewed": 0, "approved": 0, "edited": 0, "rejected": 0}
    by_source: dict = {}
    by_type: dict = {}
    corroborated = corroborated_approved = 0
    for it in items:
        s = review_status(it, dec)
        by_status[s] = by_status.get(s, 0) + 1
        for w in source_words(it):
            by_source[w] = by_source.get(w, 0) + 1
        by_type[it.get("type")] = by_type.get(it.get("type"), 0) + 1
        if "corroborated" in (it.get("flags") or []):
            corroborated += 1
            corroborated_approved += s in ("approved", "edited")
    approved = by_status["approved"] + by_status["edited"]
    bulk_all = sum(1 for it in items if (dec.get(str(it["id"])) or {}).get("bulk") == "all"
                   and review_status(it, dec) == "approved")
    return {"bulk_approved": bulk_all, "total": len(items), "by_status": by_status, "by_source": dict(sorted(by_source.items())),
            "by_type": dict(sorted(by_type.items())), "corroborated": corroborated,
            "corroborated_approved": corroborated_approved, "approved": approved,
            "reviewed": approved + by_status["rejected"]}


def excerpt(source_root: str, loc: dict, context: int = 1, cap: int = 10) -> dict | None:
    """Line-numbered lines of one location, read path-safely from the run's source tree."""
    if not source_root:
        return None
    rel = str(loc.get("file") or "")
    if not rel or rel.startswith(("/", "\\")) or ".." in re.split(r"[\\/]", rel) or "\x00" in rel:
        return None
    root = Path(source_root).resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        return None
    try:
        a, b = int(loc.get("start_line") or 1), int(loc.get("end_line") or loc.get("start_line") or 1)
    except (TypeError, ValueError):
        return None
    lines = p.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n").split("\n")
    lo, hi = max(1, a - context), min(len(lines), max(a, b) + context)
    if hi - lo + 1 > cap:
        hi = lo + cap - 1
    return {"file": rel, "start_line": a, "end_line": b,
            "lines": [{"n": n, "text": lines[n - 1][:300], "cited": a <= n <= b} for n in range(lo, hi + 1)]}


class DatasetManager:
    """One test data set build per run at a time; state persisted under <runs-dir>/_datasets."""

    def __init__(self, runs_dir: Path, repo: Path, *, python: str = sys.executable,
                 build_timeout_s: int = 900, ai_max_usd_cap: float = AI_MAX_USD_CAP):
        self.root = Path(runs_dir).resolve() / DATASETS_DIRNAME
        self.runs_dir = Path(runs_dir).resolve()
        self.repo = Path(repo).resolve()
        self.python = python
        self.build_timeout_s = build_timeout_s
        self.ai_max_usd_cap = ai_max_usd_cap
        self.lock = threading.RLock()
        self.by_run: dict[str, Dataset] = {}
        self.review_lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.root.is_dir():
            return
        for rd in sorted(x for x in self.root.iterdir() if x.is_dir()):
            for d in sorted((x for x in rd.iterdir() if x.is_dir()), key=lambda x: x.name, reverse=True):
                ds = Dataset.load(d)
                if ds is None:
                    continue
                if ds.state in ACTIVE:
                    ds.state, ds.error = "interrupted", "launcher restarted while this build was active"
                    ds.finished = ds.finished or _now()
                    ds.save()
                self.by_run[rd.name] = ds
                break

    def get(self, run_id: str) -> Dataset | None:
        return self.by_run.get(run_id)

    def ready(self, run_id: str) -> Dataset | None:
        ds = self.by_run.get(run_id)
        return ds if ds is not None and ds.state == "ready" and ds.draft.is_file() else None

    # build -------------------------------------------------------------------------------------
    def validate_build(self, body) -> dict:
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise DatasetError(400, "request body must be a JSON object")
        unknown = sorted(set(body) - DATASET_FIELDS)
        if unknown:
            raise DatasetError(400, f"unknown field(s): {', '.join(unknown)}", fields=unknown)
        ai = body.get("ai_suggestions", False)
        if not isinstance(ai, bool):
            raise DatasetError(400, "ai_suggestions must be true or false", field="ai_suggestions")
        out = {"ai_suggestions": ai, "max_usd": None}
        if ai:
            m = body.get("max_usd")
            if isinstance(m, bool) or not isinstance(m, (int, float)) or not (m > 0):
                raise DatasetError(400, "max_usd (a number > 0) is required with ai_suggestions",
                                   field="max_usd")
            if m > self.ai_max_usd_cap:
                raise DatasetError(400, f"max_usd {m} is over the ${self.ai_max_usd_cap:g} cap for AI "
                                        "suggestions", field="max_usd")
            out["max_usd"] = float(m)
        elif body.get("max_usd") is not None:
            raise DatasetError(400, "max_usd only applies with ai_suggestions: true", field="max_usd")
        return out

    def build_argv(self, ds: Dataset, corpus: str) -> list[str]:
        argv = [self.python, str(self.repo / "eval" / "keygen" / "build_key.py"),
                "--source", ds.source_root, "--out", str(ds.draft), "--corpus", corpus,
                "--date", time.strftime("%Y-%m-%d")]
        if ds.ai_suggestions:
            argv += ["--llm-draft", "--max-usd", f"{ds.max_usd:.4f}"]
        return argv

    def start(self, run_id: str, run_dir: Path, source_root: str | None, opts: dict,
              corpus: str, env: dict | None = None) -> Dataset:
        if not source_root:
            raise DatasetError(409, "this run's source code is not available to the launcher (no "
                                    "source copy recorded), so a test data set cannot be built",
                               reason="source_unavailable")
        src = Path(source_root)
        if not src.is_dir():
            raise DatasetError(409, f"this run's source code is no longer available ({source_root} is "
                                    "missing), so a test data set cannot be built",
                               reason="source_unavailable")
        why = independence_refusal(src, run_dir, self.runs_dir)
        if why:
            raise DatasetError(409, f"refused: the test data set must be built from source code, and "
                                    f"this run's source directory is not usable: {why}. {INDEPENDENCE}",
                               reason="independence")
        with self.lock:
            cur = self.by_run.get(run_id)
            if cur is not None and cur.state in ACTIVE:
                raise DatasetError(409, f"a test data set for run {run_id} is already being built "
                                        f"({cur.dataset_id}); one build per run at a time",
                                   dataset_id=cur.dataset_id, state=cur.state)
            dsid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
            ds = Dataset(dsid, run_id, self.root / run_id / dsid, str(src.resolve()),
                         opts["ai_suggestions"], opts["max_usd"])
            ds.dir.mkdir(parents=True, exist_ok=True)
            # review decisions survive a rebuild: item ids are stable hashes
            if cur is not None and cur.decisions_path.is_file():
                _write_json(ds.decisions_path, cur.decisions())
            ds.build_argv = self.build_argv(ds, corpus)
            ds.started = _now()
            ds.save()
            self.by_run[run_id] = ds
        threading.Thread(target=self._run_build, args=(ds, dict(env or os.environ)), daemon=True).start()
        return ds

    def _run_build(self, ds: Dataset, env: dict) -> None:
        rc = None
        err = None
        try:
            with open(ds.log, "ab") as log:
                log.write(f"===== launcher: test data set build {_now()} =====\n$ "
                          f"{' '.join(ds.build_argv)}\n".encode())
                log.flush()
                try:
                    rc = subprocess.run(ds.build_argv, cwd=str(self.repo), env=env, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT,
                                        timeout=self.build_timeout_s).returncode
                except subprocess.TimeoutExpired:
                    err = f"build timed out after {self.build_timeout_s}s"
        except Exception as e:                                # never leave the lock held
            err = f"{type(e).__name__}: {e}"
        with self.lock:
            ds.exit_code, ds.finished = rc, _now()
            if err is None and rc == 0 and ds.draft.is_file():
                try:
                    self._refresh_outputs(ds)
                    ds.state = "ready"
                except Exception as e:
                    ds.state, ds.error = "failed", f"{type(e).__name__}: {e}"
            else:
                ds.state = "failed"
                ds.error = err or f"build_key.py exited {rc}" + ("" if ds.draft.is_file() else " without a key")
            ds.save()

    # review ------------------------------------------------------------------------------------
    def _review_mod(self):
        p = str(self.repo / "eval" / "keygen")
        if p not in sys.path:
            sys.path.insert(0, p)
        import review                                         # noqa: E402  (eval/keygen/review.py)
        return review

    def _refresh_outputs(self, ds: Dataset) -> None:
        """review.py apply (draft + decisions -> reviewed) and filter (-> scored key)."""
        review = self._review_mod()
        d = review.load(ds.draft)
        dec = ds.decisions()
        reviewers = sorted({x.get("reviewer") for x in dec.values() if x.get("reviewer")})
        when = max((x.get("at") or "" for x in dec.values()), default="")[:10] or None
        d, _rep = review.apply_decisions(d, {"decisions": dec}, ", ".join(reviewers) or None, when)
        review.write(ds.reviewed, d)
        review.write(ds.scored, review.filter_key(d))
        ds.counts = summarize(ds.items(), dec)

    def validate_review(self, body) -> dict:
        if not isinstance(body, dict):
            raise DatasetError(400, "request body must be a JSON object")
        unknown = sorted(set(body) - REVIEW_FIELDS)
        if unknown:
            raise DatasetError(400, f"unknown field(s): {', '.join(unknown)}", fields=unknown)
        rv = body.get("reviewer")
        if rv is not None:
            if not isinstance(rv, str) or len(rv) > MAX_REVIEWER:
                raise DatasetError(400, f"reviewer must be text of at most {MAX_REVIEWER} characters",
                                   field="reviewer")
            rv = re.sub(r"[\x00-\x1f\x7f]", "", rv).strip() or None
        bulk = body.get("approve")
        if bulk is not None and bulk not in BULK_APPROVALS:
            raise DatasetError(400, 'approve must be "corroborated" (approve every item confirmed by '
                                    'both code and tests) or "all" (approve every item not already '
                                    'rejected)', field="approve")
        decs = body.get("decisions") or []
        if not isinstance(decs, list) or len(decs) > MAX_DECISIONS:
            raise DatasetError(400, f"decisions must be a list of at most {MAX_DECISIONS}", field="decisions")
        if not decs and bulk is None:
            raise DatasetError(400, 'nothing to do: give decisions or {"approve": "corroborated" | "all"}')
        out = []
        for i, x in enumerate(decs):
            if not isinstance(x, dict) or set(x) - {"id", "action", "statement"}:
                raise DatasetError(400, f"decisions[{i}] must be {{id, action, statement?}}", index=i)
            iid, act = x.get("id"), x.get("action")
            if not isinstance(iid, str) or not ID_RX.match(iid):
                raise DatasetError(400, f"decisions[{i}].id is not an item id", index=i)
            if act not in REVIEW_ACTIONS:
                raise DatasetError(400, f"decisions[{i}].action must be approve, reject or edit", index=i)
            st = x.get("statement")
            if act == "edit":
                if not isinstance(st, str) or not st.strip() or len(st) > MAX_STATEMENT:
                    raise DatasetError(400, f"decisions[{i}].statement (1-{MAX_STATEMENT} characters) is "
                                            "required to edit", index=i)
                st = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", st).strip()
            elif st is not None:
                raise DatasetError(400, f"decisions[{i}].statement only applies to edit", index=i)
            out.append({"id": iid, "decision": REVIEW_ACTIONS[act], "statement": st})
        return {"decisions": out, "approve": bulk, "reviewer": rv}

    def review(self, run_id: str, body) -> dict:
        req = self.validate_review(body)
        ds = self.ready(run_id)
        if ds is None:
            raise DatasetError(409, "this run has no finished test data set to review; build one first")
        with self.review_lock:
            items = ds.items()
            ids = {str(it["id"]): it for it in items}
            unknown = [x["id"] for x in req["decisions"] if x["id"] not in ids]
            if unknown:
                raise DatasetError(400, f"unknown item id(s): {', '.join(unknown[:10])}", ids=unknown[:50])
            dec = ds.decisions()
            todo = list(req["decisions"])
            if req["approve"] in BULK_APPROVALS:
                # bulk: only items nobody has decided on yet; a rejected (or edited) item keeps
                # its decision, so approving in bulk twice changes nothing the second time
                every = req["approve"] == "all"
                todo = [{"id": str(it["id"]), "decision": "accept", "statement": None,
                         "bulk": req["approve"]}
                        for it in items if (every or "corroborated" in (it.get("flags") or []))
                        and str(it["id"]) not in dec] + todo
            changed = 0
            now = _now()
            for x in todo:
                new = {"decision": x["decision"]}
                if x["decision"] == "edit":
                    new["statement"] = x["statement"]
                old = dec.get(x["id"]) or {}
                if old.get("decision") == new["decision"] and old.get("statement") == new.get("statement"):
                    continue                                  # idempotent: same decision again
                dec[x["id"]] = {**new, "reviewer": req["reviewer"], "at": now,
                                **({"bulk": x["bulk"]} if x.get("bulk") else {})}
                changed += 1
            if changed:
                _write_json(ds.decisions_path, dec)
            with self.lock:
                self._refresh_outputs(ds)
                ds.save()
        return {"changed": changed, "counts": ds.counts}

    # views -------------------------------------------------------------------------------------
    def status(self, run_id: str, tail: int = 20) -> dict:
        from_tail = []
        ds = self.by_run.get(run_id)
        if ds is None:
            return {"run_id": run_id, "state": "none", "independence": INDEPENDENCE}
        if ds.log.is_file():
            from_tail = ds.log.read_text(errors="replace").splitlines()[-max(1, min(tail, 200)):]
        out = {k: getattr(ds, k) for k in ("dataset_id", "run_id", "state", "created", "started",
                                           "finished", "error", "ai_suggestions", "max_usd")}
        out["counts"] = ds.counts if ds.state == "ready" else None
        out["independence"] = INDEPENDENCE
        out["log_tail"] = from_tail
        out["key"] = self.key_info(run_id)
        return out

    def key_info(self, run_id: str) -> dict | None:
        """What Full evaluation would score against for this run, with its plain-language label."""
        ds = self.ready(run_id)
        if ds is None or not ds.counts:
            return None
        c = ds.counts
        if c["approved"] > 0:
            label = f"Using this run's test data set ({c['approved']} approved items)"
            left = c["total"] - c["reviewed"]
            if left:
                label += f"; {left} not yet reviewed are left out"
            bulk = c.get("bulk_approved") or 0
            note = (f"Answer key approved in bulk (not individually reviewed): {bulk} of {c['approved']} "
                    'approved rules were approved with "Approve all"' if bulk else None)
            return {"id": "dataset", "provisional": False, "label": label, "approved": c["approved"],
                    "bulk_approved": bulk, "note": note,
                    "total": c["total"], "path": str(ds.scored)}
        return {"id": "dataset", "provisional": True, "approved": 0, "total": c["total"],
                "label": f"Provisional — test data set not yet reviewed ({c['approved']} of {c['total']} "
                         "items approved)", "path": str(ds.draft)}

    def list_items(self, run_id: str, *, status: str = "", source: str = "", q: str = "",
                   page: int = 1) -> dict:
        ds = self.ready(run_id)
        if ds is None:
            raise DatasetError(409, "this run has no finished test data set yet")
        if status not in ("", "needs_review", "unreviewed", "approved", "edited", "rejected",
                          "corroborated", "reviewed"):
            raise DatasetError(400, "status must be needs_review, approved, edited, rejected, "
                                    "corroborated or reviewed", field="status")
        src_ok = {"", "code", "tests", "ai", "docs", "planted"}
        if source.lower() not in src_ok:
            raise DatasetError(400, "source must be code, tests or ai", field="source")
        if len(q) > 200:
            raise DatasetError(400, "q is too long", field="q")
        dec = ds.decisions()
        ql = q.lower().strip()
        sel = []
        for it in ds.items():
            st = review_status(it, dec)
            corr = "corroborated" in (it.get("flags") or [])
            if status in ("needs_review", "unreviewed") and st != "unreviewed":
                continue
            if status in ("approved",) and st not in ("approved", "edited"):
                continue
            if status in ("edited", "rejected") and st != status:
                continue
            if status == "reviewed" and st == "unreviewed":
                continue
            if status == "corroborated" and not corr:
                continue
            words = source_words(it)
            if source and source.lower() not in [w.lower() for w in words]:
                continue
            x = dec.get(str(it["id"])) or {}
            stmt = x.get("statement") if x.get("decision") == "edit" and x.get("statement") else it.get("statement")
            if ql and ql not in f"{stmt} {it.get('statement')} {it['id']}".lower():
                continue
            sel.append((it, st, corr, words, stmt, x))
        page = max(1, page)
        n = len(sel)
        chunk = sel[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        out = []
        for it, st, corr, words, stmt, x in chunk:
            locs = it.get("locations") or []
            out.append({"id": it["id"], "type": it.get("type"), "statement": stmt,
                        "original_statement": it.get("statement") if stmt != it.get("statement") else None,
                        "sources": words, "detectors": it.get("source") or [], "corroborated": corr,
                        "evidence_class": it.get("evidence_class"), "status": st,
                        "reviewer": x.get("reviewer"), "reviewed_at": x.get("at"),
                        "locations": [{"file": l.get("file"), "start_line": l.get("start_line"),
                                       "end_line": l.get("end_line")} for l in locs],
                        "excerpts": [e for e in (excerpt(ds.source_root, l) for l in locs[:2]) if e]})
        return {"run_id": run_id, "dataset_id": ds.dataset_id, "page": page, "page_size": PAGE_SIZE,
                "total": n, "pages": max(1, -(-n // PAGE_SIZE)), "items": out, "counts": ds.counts,
                "independence": INDEPENDENCE}

    # evaluation suite --------------------------------------------------------------------------
    def suite_config(self, run_id: str, run_dir: Path, template: Path) -> tuple[Path, dict]:
        """Write an ad-hoc run_suite.py config for this run's data set (from a template suite):
        the deterministic sanity stage plus the keyed score stages, judge_runs 1."""
        import yaml
        ds = self.ready(run_id)
        info = self.key_info(run_id)
        if ds is None or info is None:
            raise DatasetError(409, "this run has no finished test data set")
        t = yaml.safe_load(template.read_text(encoding="utf-8")) or {}
        corpus = f"dataset-{run_id}"[:64]
        keep = []
        for st in t.get("stages") or []:
            ev = st.get("evaluator")
            if ev == "sanity" and str(st.get("path", "")).strip() == "${corpus}":
                keep.append(dict(st))
            elif ev == "score":
                s = dict(st)
                s["corpus"] = corpus
                keep.append(s)
        ids = {s["id"] for s in keep}
        cfg = {
            "suite": corpus, "schema_version": t.get("schema_version", 1),
            "description": f"Spec recall and citation checks of run {run_id} against its own test data "
                           f"set ({'provisional, not yet reviewed' if info['provisional'] else 'reviewed'}). "
                           + INDEPENDENCE,
            "vars": {"corpus": ds.source_root, "run": str(run_dir), "key": info["path"]},
            "budget_usd": t.get("budget_usd", 3.0), "judge_runs": 1,
            "corpora": {corpus: {"source_root": "${corpus}", "key": "${key}", "exclude": []}},
            "stages": keep,
            "thresholds": [x for x in t.get("thresholds") or [] if x.get("stage") in ids],
        }
        ds.suite_path.write_text("# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\n"
                                 "# SPDX-License-Identifier: MIT-0\n# Generated by the launcher "
                                 f"from {template.name}; do not edit.\n"
                                 + yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        return ds.suite_path, info

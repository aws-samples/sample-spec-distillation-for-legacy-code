# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Git repository input (source_fetch.py + DISTILL_GIT_URL in run_distillation.sh), against local
bare repositories created in the test (no network, no AWS)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import source_fetch as SF  # noqa: E402
import source_inventory as SI  # noqa: E402
from gitfixture import Remote, git  # noqa: E402
from test_pipeline_reuse import ALWAYS, STAGES, rig  # noqa: E402,F401  (rig is a pytest fixture)

A_PY = "def a():\n    # DataProgram entry point\n    return 1\n"


@pytest.fixture()
def remote(tmp_path):
    r = Remote(tmp_path / "git")
    r.c1 = r.commit({"app/a.py": A_PY, "app/b.py": "def b():\n    return 2\n", "docs/notes.txt": "n\n"}, "one")
    r.tag("v1")
    r.c2 = r.commit({"app/b.py": "def b():\n    return 22\n"}, "two")
    r.checkout("feature", create=True)
    r.c3 = r.commit({"app/c.py": "def c():\n    return 3\n"}, "feature", branch="feature")
    r.checkout("main")
    return r


def _fetch(remote, dest, **kw):
    return SF.fetch(remote.url, kw.pop("ref", None), kw.pop("subdir", None), dest, log=lambda m: None, **kw)


# ── source_fetch unit level ─────────────────────────────────────────────────────────────────
def test_default_head_branch_tag_and_sha(remote, tmp_path):
    p = _fetch(remote, tmp_path / "d")
    assert p["commit"] == remote.c2 and p["ref_resolved"] == "HEAD" and p["ref_requested"] is None
    assert (tmp_path / "d" / "app" / "b.py").read_text().endswith("22\n")
    p = _fetch(remote, tmp_path / "d", ref="feature")
    assert p["commit"] == remote.c3 and p["ref_resolved"] == "refs/heads/feature"
    assert (tmp_path / "d" / "app" / "c.py").is_file()
    p = _fetch(remote, tmp_path / "d", ref="v1")                      # annotated tag → peeled commit
    assert p["commit"] == remote.c1 and p["ref_resolved"] == "refs/tags/v1"
    assert not (tmp_path / "d" / "app" / "c.py").exists()
    p = _fetch(remote, tmp_path / "d2", ref=remote.c1)                 # full SHA, fetched directly
    assert p["commit"] == remote.c1 and p["ref_resolved"] is None and p["ref_requested"] == remote.c1
    assert (tmp_path / "d2" / "app" / "b.py").read_text().endswith("2\n")
    p = _fetch(remote, tmp_path / "d3", ref=remote.c1[:10])            # abbreviated SHA
    assert p["commit"] == remote.c1
    # shallow: only one commit present
    assert git(tmp_path / "d", "rev-list", "--count", "HEAD") == "1"


def test_subdir_and_reuse(remote, tmp_path):
    p = _fetch(remote, tmp_path / "d", subdir="app")
    assert p["target"] == str((tmp_path / "d" / "app").resolve()) and p["subdir"] == "app"
    assert p["reused_checkout"] is False
    p2 = _fetch(remote, tmp_path / "d", subdir="app")
    assert p2["reused_checkout"] is True and p2["commit"] == p["commit"]
    # a dirty checkout is not reused
    (tmp_path / "d" / "app" / "b.py").write_text("tampered\n")
    p3 = _fetch(remote, tmp_path / "d", subdir="app")
    assert p3["reused_checkout"] is False and (tmp_path / "d" / "app" / "b.py").read_text().endswith("22\n")


@pytest.mark.parametrize("subdir,msg", [("nope", "does not exist"), ("../x", "invalid subdir"),
                                        ("/etc", "invalid subdir"), (".git", "into .git")])
def test_subdir_errors(remote, tmp_path, subdir, msg):
    with pytest.raises(SF.FetchError, match=msg):
        _fetch(remote, tmp_path / "d", subdir=subdir)


def test_unknown_ref_unreachable_and_empty(remote, tmp_path):
    with pytest.raises(SF.FetchError, match="unknown ref 'nope'"):
        _fetch(remote, tmp_path / "d", ref="nope")
    with pytest.raises(SF.FetchError, match="unknown ref"):
        _fetch(remote, tmp_path / "d", ref="0" * 40)
    with pytest.raises(SF.FetchError, match="not reachable"):
        SF.fetch((tmp_path / "missing.git").as_uri(), None, None, tmp_path / "d", log=lambda m: None)
    with pytest.raises(SF.FetchError, match="not reachable"):
        SF.fetch(str(tmp_path / "missing.git"), None, None, tmp_path / "d", log=lambda m: None)
    empty = Remote(tmp_path / "empty")
    with pytest.raises(SF.FetchError, match="is empty"):
        SF.fetch(empty.url, None, None, tmp_path / "d", log=lambda m: None)
    with pytest.raises(SF.FetchError, match="invalid git ref"):
        _fetch(remote, tmp_path / "d", ref="--upload-pack=evil")


def test_size_limit(remote, tmp_path):
    remote.commit({"blob.bin": os.urandom(400_000)}, "big")
    with pytest.raises(SF.FetchError, match="size limit"):
        _fetch(remote, tmp_path / "d", max_mb=0.1)
    assert _fetch(remote, tmp_path / "d2", max_mb=5)["checkout_mb"] > 0.3


@pytest.mark.parametrize("url", ["https://user:ghp_SECRET@github.com/o/r.git",
                                 "https://ghp_SECRET@github.com/o/r.git",
                                 "https://oauth2:SECRET@gitlab.example.com/o/r.git",
                                 "ssh://git:SECRET@host/o/r.git"])
def test_credentialed_urls_rejected_without_echoing_the_secret(url):
    with pytest.raises(SF.FetchError) as e:
        SF.classify_url(url)
    assert "SECRET" not in str(e.value)
    assert "SSH key" in str(e.value) and "credential helper" in str(e.value)


def test_url_forms():
    assert SF.classify_url("git@github.com:org/repo.git")["kind"] == "ssh"
    assert SF.classify_url("ssh://git@host:2222/org/repo.git")["display"] == "ssh://host:2222/org/repo.git"
    assert SF.classify_url("https://github.com/org/repo.git")["kind"] == "https"
    for bad in ("http://host/r.git", "git://host/r.git", "ftp://host/r", "-oProxyCommand=x", ""):
        with pytest.raises(SF.FetchError):
            SF.classify_url(bad)


def test_error_classification():
    assert "authentication failed" in SF.classify_git_error("git@host: Permission denied (publickey).", "h:r")
    assert "authentication failed" in SF.classify_git_error(
        "fatal: could not read Username for 'https://h': terminal prompts disabled", "h")
    assert "not reachable" in SF.classify_git_error(
        "ssh: Could not resolve hostname nohost: nodename nor servname provided", "h")
    assert SF._scrub("fatal: unable to access 'https://u:tok@h/r/'", "") == "fatal: unable to access 'https://h/r/'"


def test_enumeration_excludes_dot_git(remote, tmp_path):
    _fetch(remote, tmp_path / "d")
    cands, excl, _ = SI.enumerate_source(str(tmp_path / "d"))
    assert not any(c.startswith(".git") or "/.git/" in c for c in cands)
    assert (".git/", "VCS metadata (.git)") in excl
    assert not any(p.startswith(".git/") and p != ".git/" for p, _ in excl)   # pruned, not walked
    assert "app/a.py" in cands


def test_cli_prints_only_stripped_url(remote, tmp_path, capsys):
    rc = SF.main(["fetch", "--url", remote.url, "--dest", str(tmp_path / "d"),
                  "--provenance-out", str(tmp_path / "prov.json")])
    assert rc == 0
    prov = json.loads((tmp_path / "prov.json").read_text())
    assert prov["commit"] == remote.c2 and prov["url"] == remote.url
    rc = SF.main(["fetch", "--url", "https://x:SECRET@example.com/r.git", "--dest", str(tmp_path / "e")])
    err = capsys.readouterr().err
    assert rc == 2 and "SECRET" not in err and "source_fetch: ERROR" in err


# ── run_distillation.sh with DISTILL_GIT_URL (LLM stages stubbed) ────────────────────────────
def _git_run(rig, remote, **env):
    env.setdefault("DISTILL_GIT_URL", remote.url)
    return rig["run"](DISTILL_TARGET="", **env)


def test_pipeline_git_provenance_reuse_and_invalidation(rig, remote):
    rc, out, calls = _git_run(rig, remote, DISTILL_GIT_SUBDIR="app")
    assert rc == 0, out[-4000:]
    assert "Distillation complete." in out
    for s in STAGES:
        assert s in calls, (s, calls)
    o = rig["out"]
    man = json.loads((o / "run_manifest.json").read_text())
    assert man["source"]["commit"] == remote.c2 and man["source"]["subdir"] == "app"
    assert man["source"]["url"] == remote.url and "run_identity" in man
    inv = json.loads((o / ".enumeration" / "source_inventory.json").read_text())
    assert inv["source_provenance"]["commit"] == remote.c2
    assert inv["config"]["git_commit"] == remote.c2 and inv["config"]["git_subdir"] == "app"
    assert sorted(e["path"] for e in inv["entries"]) == ["a.py", "b.py"]       # subdir only
    assert f"commit {remote.c2}" in out

    # same SHA → every stage reused, checkout reused
    rc, out, calls = _git_run(rig, remote, DISTILL_GIT_SUBDIR="app")
    assert rc == 0, out[-4000:]
    assert set(calls) <= ALWAYS, calls
    assert "reusing existing checkout" in out and "Using existing enumeration" in out

    # a new commit outside the analysed subfolder: the source identity (SHA) changes, so Phase 0/1
    # re-enumerate and re-page, but the pages are byte-identical, so the content-keyed LLM stages
    # (run identity = page palm + inventory hash) are reused at $0; provenance moves to the new SHA
    c4 = remote.commit({"docs/notes.txt": "changed\n"}, "docs only")
    rc, out, calls = _git_run(rig, remote, DISTILL_GIT_SUBDIR="app")
    assert rc == 0, out[-4000:]
    assert "Source identity changed" in out
    assert set(calls) <= ALWAYS, calls
    assert json.loads((o / "run_manifest.json").read_text())["source"]["commit"] == c4
    inv = json.loads((o / ".enumeration" / "source_inventory.json").read_text())
    assert inv["config"]["git_commit"] == c4

    # a new commit that changes analysed code invalidates every stage
    c5 = remote.commit({"app/b.py": "def b():\n    return 5\n"}, "code change")
    rc, out, calls = _git_run(rig, remote, DISTILL_GIT_SUBDIR="app")
    assert rc == 0, out[-4000:]
    assert "Source identity changed" in out and "changed      1: b.py" in out
    for s in STAGES:
        assert s in calls, (s, calls)
    assert json.loads((o / "run_manifest.json").read_text())["source"]["commit"] == c5
    assert "return 5" in "".join(p.read_text() for p in (o / ".pages").glob("page_*/concat.txt"))


def test_pipeline_git_whole_repo_excludes_dot_git(rig, remote):
    rc, out, _ = _git_run(rig, remote, DISTILL_GIT_REF="v1")
    assert rc == 0, out[-4000:]
    o = rig["out"]
    cands = (o / ".enumeration" / "candidates.txt").read_text().splitlines()
    assert "app/a.py" in cands and not any(".git/" in c for c in cands)
    assert ".git/\tVCS metadata (.git)" in (o / ".enumeration" / "excluded.tsv").read_text()
    prov = json.loads((o / "source_provenance.json").read_text())
    assert prov["commit"] == remote.c1 and prov["ref_resolved"] == "refs/tags/v1"


def test_pipeline_git_errors(rig, remote):
    rc, out, calls = rig["run"](DISTILL_GIT_URL=remote.url)          # DISTILL_TARGET also set by rig
    assert rc == 1 and "mutually exclusive" in out and calls == []
    rc, out, calls = _git_run(rig, remote, DISTILL_GIT_URL="https://bob:SECRET@example.com/r.git")
    assert rc == 1 and calls == [] and "SECRET" not in out
    st = json.loads((rig["out"] / "run_status.json").read_text())
    assert st["status"] == "failed" and "credentials" in st["failed_stages"][0]
    assert "SECRET" not in json.dumps(st)
    rc, out, _ = _git_run(rig, remote, DISTILL_GIT_REF="nope")
    assert rc == 1 and "unknown ref 'nope'" in out
    rc, out, _ = _git_run(rig, remote, DISTILL_GIT_SUBDIR="missing")
    assert rc == 1 and "subdir 'missing' does not exist" in out


def test_budget_resume_command_pins_the_commit(rig, remote):
    rc, out, _ = _git_run(rig, remote, DISTILL_GIT_REF="main", DISTILL_MAX_USD="0.05")
    assert rc == 6, out[-4000:]
    cmd = json.loads((rig["out"] / "run_status.json").read_text())["resume_command"]
    assert f"DISTILL_GIT_REF={remote.c2}" in cmd and "DISTILL_GIT_REF_REQUESTED=main" in cmd
    assert "DISTILL_TARGET=" not in cmd and "DISTILL_GIT_COMMIT" not in cmd

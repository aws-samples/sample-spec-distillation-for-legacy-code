#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""source_fetch.py: git repository input for run_distillation.sh (DISTILL_GIT_URL).

Clones a repository shallowly into `<output>/.source/` and prints the directory to analyse (the
clone, or a subfolder of it) plus provenance: the URL with any userinfo stripped, the ref that was
requested, the resolved commit SHA and the subfolder.

  source_fetch.py fetch --url URL [--ref REF] [--subdir DIR] --dest <output>/.source
                        [--max-mb 500] [--provenance-out <output>/source_provenance.json]
  source_fetch.py check-url URL        (validate only; exit 0 or 2 with the error)

URL forms: https://host/path, ssh://[user@]host/path, scp-like user@host:path, file:///path, or a
local path to a repository. Plain http:// and git:// are refused (unauthenticated, unencrypted).
A URL that carries credentials (https://user:token@host/..., https://token@host/...) is refused:
use an SSH key (ssh-agent) or a git credential helper. Authentication is whatever the user's git
already has; prompts are disabled (GIT_TERMINAL_PROMPT=0, ssh BatchMode=yes) so a missing
credential fails fast instead of hanging.

REF: a branch, a tag or a commit SHA (7-40 hex). Default: the remote HEAD. A full 40-character SHA
is fetched directly (`git fetch --depth 1 origin <sha>`); an abbreviated SHA needs a full fetch.

Reuse: when `<dest>` already holds a clean checkout of the resolved commit it is reused without
fetching. Otherwise `<dest>` is replaced. The fetch is aborted once `<dest>` grows past --max-mb
(DISTILL_GIT_MAX_MB, default 500), and the checked-out tree is checked against the same limit.

Errors exit 2 with a one-line message on stderr that starts with "source_fetch: ERROR". The raw
URL is never printed (only the stripped form), so a token cannot leak into logs.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

EXIT_ERROR = 2
DEFAULT_MAX_MB = 500.0
NET_TIMEOUT_S = int(os.environ.get("DISTILL_GIT_TIMEOUT_S", "900") or 900)
SHA_FULL = re.compile(r"^[0-9a-f]{40}$")
SHA_ABBREV = re.compile(r"^[0-9a-f]{7,39}$")
# Conservative ref grammar: no leading "-" (option injection), no "..", no control characters.
REF_RX = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/+@-]{0,199}$")
SCP_RX = re.compile(r"^(?P<user>[A-Za-z0-9._-]+@)?(?P<host>[A-Za-z0-9.-]+):(?P<path>(?!//).+)$")


class FetchError(Exception):
    """A user-facing error (message is safe to print: no credentials)."""


# ── URL validation ──────────────────────────────────────────────────────────────────────────
CRED_HINT = ("Remove the credentials from the URL and use an SSH key (ssh-agent, ssh://... or "
             "git@host:path) or a git credential helper (git config credential.helper) instead.")


BROWSE_HOSTS = ("github.com", "gitlab.com")
GITLAB_HOSTS = ("gitlab.com",)
# GitHub web pages under org/repo/ that are not folders (a pasted PR/issue page means "this repo").
GITHUB_UI_PAGES = {"pulls", "pull", "issues", "actions", "releases", "tags", "branches", "wiki",
                   "settings", "security", "pulse", "graphs", "network", "compare", "projects",
                   "discussions", "milestones", "labels", "commits", "activity", "deployments",
                   "packages", "stargazers", "forks", "watchers"}


def normalize_browse_url(url: str, ref: str | None = None, subdir: str | None = None) -> tuple[str, str | None, str | None]:
    """Turn a web *browse* URL into (clone URL, ref, subdir).

    People paste what the browser shows: https://github.com/org/repo/tree/main/src/App or
    https://gitlab.example/group/repo/-/tree/dev/lib. git cannot clone those. For known hosts,
    split off the ref and folder; values the caller already supplied always win. Anything that
    is not a recognisable browse URL is returned unchanged.

    A ref containing '/' (feature/x) cannot be told apart from a folder in a browse URL: the
    first segment after tree/ is taken as the ref. Type the full ref in the ref field and the
    matching segments are removed from the folder."""
    if not isinstance(url, str) or "://" not in url:
        return url, ref, subdir
    if any(c in url.strip() for c in "\n\r\t\0 "):
        return url, ref, subdir          # urlsplit would silently drop these; classify_url refuses
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or host not in BROWSE_HOSTS:
        return url, ref, subdir
    segs = [x for x in parts.path.split("/") if x]
    if "-" in segs:                      # GitLab: group/sub/repo/-/tree/<ref>/<path>
        i = segs.index("-")
        repo, rest = segs[:i], segs[i + 1:]
    elif host in GITLAB_HOSTS:           # GitLab without /-/: nested groups, the whole path is the repo
        repo, rest = segs, []
    elif len(segs) >= 2:                 # GitHub: org/repo/<tree|blob>/<ref>/<path>
        repo, rest = segs[:2], segs[2:]
    else:
        return url, ref, subdir
    if not repo or (not rest and not parts.query and not parts.fragment and not parts.path.endswith("/")):
        return url, ref, subdir          # already a plain clone URL
    b_ref, b_path = None, []
    if rest and rest[0] in ("tree", "blob") and len(rest) >= 2:
        b_ref, b_path = rest[1], rest[2:]
        if rest[0] == "blob" and b_path:  # a file link: analyse its folder
            b_path = b_path[:-1]
        if isinstance(ref, str) and "/" in ref.strip("/"):
            # a slash ref typed by the user: drop its segments from the browse path
            rsegs = [x for x in ref.strip("/").split("/") if x]
            if rest[1:1 + len(rsegs)] == rsegs:
                b_path = rest[1 + len(rsegs):]
                if rest[0] == "blob" and b_path:
                    b_path = b_path[:-1]
    elif rest and rest[0] == "commit" and len(rest) >= 2:
        b_ref = rest[1]                   # a commit page: that commit, whole repository
    elif rest and ("-" in segs or rest[0] in GITHUB_UI_PAGES):
        pass                              # another web page of the repo (PRs, issues, ...): the repo root
    else:                                # org/repo/some/folder (not a real browse URL, but common)
        b_path = rest
    repo_path = "/" + "/".join(repo)
    clone = urlunsplit(("https", parts.netloc, repo_path.removesuffix(".git") + ".git", "", ""))
    return clone, (ref or b_ref or None), (subdir or ("/".join(b_path) if b_path else None))


def classify_url(url: str) -> dict:
    """Validate a repository location -> {kind: https|ssh|file|path, url, display}.

    `url` is what git is given (a local path becomes a file:// URL so --depth works); `display`
    is safe to print and record (userinfo stripped). Raises FetchError."""
    if not isinstance(url, str) or not url.strip():
        raise FetchError("git URL is empty")
    url = url.strip()
    if url.startswith("-") or any(c in url for c in "\n\r\t\0 "):
        raise FetchError("git URL contains characters that are not allowed (leading '-' or whitespace)")
    if "://" in url:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme in ("http", "git"):
            raise FetchError(f"{scheme}:// URLs are not allowed (unencrypted); use https:// or ssh://")
        if scheme not in ("https", "ssh", "git+ssh", "file"):
            raise FetchError(f"unsupported URL scheme {scheme}:// (use https, ssh, file or a local path)")
        host = parts.hostname or ""
        display_netloc = host + (f":{parts.port}" if parts.port else "")
        if scheme == "https" and (parts.username or parts.password or "@" in parts.netloc):
            raise FetchError("the git URL contains credentials (user/token before '@'); refusing to use it. "
                             + CRED_HINT)
        if scheme in ("ssh", "git+ssh") and parts.password:
            raise FetchError("the git URL contains a password; refusing to use it. " + CRED_HINT)
        if scheme == "file":
            if parts.netloc not in ("", "localhost"):
                raise FetchError("file:// URLs must be local (file:///path)")
            if not parts.path:
                raise FetchError("file:// URL has no path")
            return {"kind": "file", "url": url, "display": urlunsplit(("file", "", parts.path, "", ""))}
        if not host:
            raise FetchError("git URL has no host")
        user = f"{parts.username}@" if (scheme != "https" and parts.username) else ""
        display = urlunsplit((scheme, display_netloc, parts.path, "", ""))
        return {"kind": "https" if scheme == "https" else "ssh",
                "url": urlunsplit((scheme, user + display_netloc, parts.path, parts.query, "")),
                "display": display}
    pre = url.split("/", 1)[0]
    if "@" in pre and ":" in pre.rsplit("@", 1)[0] and not url.startswith(("/", ".", "~")):
        # user:password@host:path — SCP_RX would read "user" as the host and echo the password
        raise FetchError("the git URL contains a password; refusing to use it. " + CRED_HINT)
    m = SCP_RX.match(url)
    if m and not url.startswith(("/", ".", "~")) and "/" not in m.group("host"):
        if ":" in (m.group("user") or ""):
            raise FetchError("the git URL contains a password; refusing to use it. " + CRED_HINT)
        return {"kind": "ssh", "url": url, "display": f"{m.group('host')}:{m.group('path')}"}
    # local path
    p = Path(os.path.expanduser(url))
    if not p.is_absolute():
        p = p.resolve()
    if not p.exists():
        raise FetchError(f"repository not reachable: local path {p} does not exist")
    if not p.is_dir():
        raise FetchError(f"repository not reachable: local path {p} is a file, not a git repository folder")
    return {"kind": "path", "url": p.resolve().as_uri(), "display": str(p.resolve())}


def check_ref(ref: str | None) -> str:
    if ref is None or ref == "":
        return ""
    if not isinstance(ref, str) or not REF_RX.match(ref) or ".." in ref or ref.endswith((".", "/", ".lock")):
        raise FetchError(f"invalid git ref {ref!r} (branch, tag or commit SHA expected)")
    return ref


def check_subdir(subdir: str | None) -> str:
    if subdir is None:
        return ""
    if not isinstance(subdir, str):
        raise FetchError("subdir must be a string")
    s = subdir.strip().strip("/")
    if s in ("", "."):
        return ""
    parts = s.split("/")
    if subdir.strip().startswith("/") or any(p in ("..", "") for p in parts) or "\0" in s:
        raise FetchError(f"invalid subdir {subdir!r}: use a relative path inside the repository, without '..'")
    if parts[0] == ".git":
        raise FetchError("subdir may not point into .git/")
    return s


# ── git plumbing ────────────────────────────────────────────────────────────────────────────
_SSH_CFG: list[bool] = []


def _ssh_command_configured() -> bool:
    """True when the user set core.sshCommand (we must not override it)."""
    if not _SSH_CFG:
        try:
            p = subprocess.run(["git", "config", "--get", "core.sshCommand"], capture_output=True,
                               text=True, timeout=10)
            _SSH_CFG.append(bool(p.stdout.strip()))
        except (OSError, subprocess.TimeoutExpired):
            _SSH_CFG.append(False)
    return _SSH_CFG[0]


def _git_env() -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"          # never prompt for a username/password
    if not env.get("GIT_SSH_COMMAND") and not env.get("GIT_SSH") and not _ssh_command_configured():
        # fail fast instead of prompting for a passphrase/host key; ssh-agent keys still work
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=20"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["LC_ALL"] = "C"                        # stable stderr text for classification
    return env


def _scrub(text: str, secret_url: str) -> str:
    """Remove anything URL-like that git may echo, keeping messages credential-free."""
    if secret_url:
        text = text.replace(secret_url, "<repo>")
    return re.sub(r"(https?://)[^/@\s]+@", r"\1", text)


def _git(args: list[str], cwd: str | None = None, url: str = "", timeout: int = NET_TIMEOUT_S
         ) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=cwd, env=_git_env(), capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        raise FetchError("git is not installed (dnf install -y git)")
    except subprocess.TimeoutExpired:
        raise FetchError(f"git {args[0]} timed out after {timeout}s (repository not reachable?)")


def classify_git_error(stderr: str, display: str) -> str:
    """One clear sentence for a failed ls-remote/fetch."""
    s = stderr.lower()
    if any(k in s for k in ("permission denied (publickey", "authentication failed",
                            "could not read username", "could not read password",
                            "terminal prompts disabled", "invalid username or password",
                            "access denied", "http basic: access denied", "403")):
        return (f"authentication failed for {display}. The launcher uses your existing git setup: "
                "load an SSH key into ssh-agent (ssh-add) or configure a git credential helper, "
                "then retry. Credentials are never accepted in the URL.")
    if any(k in s for k in ("could not resolve host", "could not resolve hostname", "name or service not known",
                            "connection refused", "connection timed out", "network is unreachable",
                            "no route to host", "operation timed out", "failed to connect")):
        return f"repository not reachable: {display} (host unreachable or DNS failure)"
    if any(k in s for k in ("does not appear to be a git repository", "repository not found",
                            "not found", "no such file or directory", "not a git repository")):
        return (f"repository not reachable: {display} (no repository at that location, or no "
                "access to it)")
    first = next((ln.strip() for ln in stderr.splitlines() if ln.strip().lower().startswith(("fatal", "error"))),
                 stderr.strip().splitlines()[-1] if stderr.strip() else "unknown git error")
    return f"git failed for {display}: {first[:300]}"


def ls_remote(src: dict) -> list[tuple[str, str]]:
    p = _git(["ls-remote", src["url"]], url=src["url"])
    if p.returncode != 0:
        raise FetchError(classify_git_error(_scrub(p.stderr, src["url"]), src["display"]))
    out = []
    for ln in p.stdout.splitlines():
        if "\t" in ln:
            sha, name = ln.split("\t", 1)
            out.append((sha.strip(), name.strip()))
    return out


def resolve_ref(refs: list[tuple[str, str]], ref: str, display: str) -> tuple[str, str]:
    """(resolved ref name, sha) for a branch/tag/HEAD; ('', '') when ref looks like a SHA that is
    not a ref name (the caller fetches it by SHA)."""
    if not refs:
        raise FetchError(f"repository {display} is empty (no commits)")
    table = dict((n, s) for s, n in refs)
    if not ref:
        if "HEAD" in table:
            return "HEAD", table["HEAD"]
        for cand in ("refs/heads/main", "refs/heads/master"):
            if cand in table:
                return cand, table[cand]
        raise FetchError(f"repository {display} has no HEAD; pass DISTILL_GIT_REF (branch, tag or SHA)")
    for cand in ([ref] if ref.startswith("refs/") else [f"refs/heads/{ref}", f"refs/tags/{ref}"]):
        if cand + "^{}" in table:          # annotated tag: the peeled commit
            return cand, table[cand + "^{}"]
        if cand in table:
            return cand, table[cand]
    if SHA_FULL.match(ref) or SHA_ABBREV.match(ref):
        return "", ""
    raise FetchError(f"unknown ref {ref!r} in {display} (no such branch or tag)")


def dir_size(path: str | Path, skip_git: bool = False) -> int:
    total = 0
    for dp, dns, fns in os.walk(path):
        if skip_git and ".git" in dns:
            dns.remove(".git")
        for f in fns:
            try:
                total += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return total


def _fetch_with_limit(dest: Path, args: list[str], src: dict, max_bytes: int) -> subprocess.CompletedProcess:
    """git fetch, killed as soon as <dest> grows past max_bytes."""
    proc = subprocess.Popen(["git", *args], cwd=str(dest), env=_git_env(), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    over = threading.Event()

    def watch():
        while proc.poll() is None:
            if dir_size(dest) > max_bytes:
                over.set()
                proc.kill()
                return
            time.sleep(0.25)

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        out, err = proc.communicate(timeout=NET_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise FetchError(f"git fetch timed out after {NET_TIMEOUT_S}s for {src['display']}")
    t.join(timeout=2)
    if over.is_set():
        raise FetchError(f"repository {src['display']} is over the size limit of "
                         f"{max_bytes / 1e6:g} MB (DISTILL_GIT_MAX_MB); clone it on the host, prune it, "
                         "and pass DISTILL_TARGET, or raise DISTILL_GIT_MAX_MB")
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _head(dest: Path) -> str:
    p = _git(["rev-parse", "HEAD"], cwd=str(dest), timeout=60)
    return p.stdout.strip() if p.returncode == 0 else ""


def _reusable(dest: Path, sha: str) -> bool:
    """A clean checkout of exactly `sha` (full or abbreviated) already sits in dest."""
    if not (dest / ".git").is_dir():
        return False
    head = _head(dest)
    if not head or not head.startswith(sha):
        return False
    st = _git(["status", "--porcelain", "--untracked-files=all"], cwd=str(dest), timeout=120)
    return st.returncode == 0 and st.stdout.strip() == ""


def fetch(url: str, ref: str | None, subdir: str | None, dest: str | Path,
          max_mb: float = DEFAULT_MAX_MB, log=lambda m: print(m, file=sys.stderr)) -> dict:
    """Clone/reuse -> provenance dict (target = directory to analyse). Raises FetchError."""
    url, ref, subdir = normalize_browse_url(url, ref, subdir)
    src = classify_url(url)
    ref = check_ref(ref)
    subdir = check_subdir(subdir)
    if not (max_mb and max_mb > 0):
        raise FetchError("DISTILL_GIT_MAX_MB must be > 0")
    max_bytes = int(max_mb * 1e6)
    dest = Path(dest).resolve()

    log(f"source_fetch: repository {src['display']} ref={ref or '<remote HEAD>'}"
        + (f" subdir={subdir}" if subdir else ""))
    refs = ls_remote(src)
    ref_name, sha = resolve_ref(refs, ref, src["display"])
    want = sha or ref                       # a SHA (full or abbreviated) when ref_name == ""
    reused = _reusable(dest, want)
    if reused:
        sha = _head(dest)
        log(f"source_fetch: reusing existing checkout at {sha[:12]} in {dest}")
    else:
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for args in (["init", "-q"], ["remote", "add", "origin", src["url"]]):
            p = _git(args, cwd=str(dest), timeout=60)
            if p.returncode != 0:
                raise FetchError(f"git {args[0]} failed: {_scrub(p.stderr, src['url']).strip()[:300]}")
        if ref_name:
            target = [ref_name] if ref_name != "HEAD" else ["HEAD"]
            fargs = ["fetch", "-q", "--depth", "1", "--no-tags", "origin", *target]
        elif SHA_FULL.match(want):
            fargs = ["fetch", "-q", "--depth", "1", "--no-tags", "origin", want]
        else:
            fargs = ["fetch", "-q", "--no-tags", "origin", "+refs/heads/*:refs/remotes/origin/*",
                     "+refs/tags/*:refs/tags/*"]
        p = _fetch_with_limit(dest, fargs, src, max_bytes)
        if p.returncode != 0 and not ref_name and SHA_FULL.match(want):
            # server refuses fetch-by-SHA: full fetch, then look for the commit
            p = _fetch_with_limit(dest, ["fetch", "-q", "--no-tags", "origin",
                                         "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*"],
                                  src, max_bytes)
            fetched_by_sha = False
        else:
            fetched_by_sha = not ref_name and bool(SHA_FULL.match(want))
        err = _scrub(p.stderr, src["url"])
        if p.returncode != 0:
            low = err.lower()
            if not ref_name and any(k in low for k in ("not our ref", "couldn't find remote ref",
                                                       "no such remote ref", "unadvertised object")):
                raise FetchError(f"unknown ref {ref!r} in {src['display']} (commit not found)")
            raise FetchError(classify_git_error(err, src["display"]))
        commit = "FETCH_HEAD" if (ref_name or fetched_by_sha) else want
        rp = _git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"], cwd=str(dest), timeout=60)
        if rp.returncode != 0 or not rp.stdout.strip():
            raise FetchError(f"unknown ref {ref!r} in {src['display']} (commit not found)")
        sha = rp.stdout.strip()
        co = _git(["-c", "advice.detachedHead=false", "checkout", "-q", "--detach", sha], cwd=str(dest),
                  timeout=NET_TIMEOUT_S)
        if co.returncode != 0:
            raise FetchError(f"git checkout of {sha[:12]} failed: {_scrub(co.stderr, src['url']).strip()[:300]}")
        log(f"source_fetch: checked out {sha} into {dest}")
    tree = dir_size(dest, skip_git=True)
    if tree > max_bytes:
        raise FetchError(f"repository {src['display']} checkout is {tree / 1e6:.1f} MB, over the size "
                         f"limit of {max_mb:g} MB (DISTILL_GIT_MAX_MB)")
    target = dest / subdir if subdir else dest
    if subdir:
        try:
            inside = target.resolve().is_relative_to(dest)
        except OSError:
            inside = False
        if not inside or not target.is_dir():
            tops = sorted(p.name + "/" for p in dest.iterdir() if p.is_dir() and p.name != ".git")[:20]
            raise FetchError(f"subdir {subdir!r} does not exist in {src['display']} at {sha[:12]} "
                             f"(top-level folders: {', '.join(tops) or 'none'})")
    return {"type": "git", "url": src["display"], "ref_requested": ref or None,
            "ref_resolved": ref_name or None, "commit": sha, "subdir": subdir or None,
            "clone_dir": str(dest), "target": str(target), "reused_checkout": reused,
            "checkout_mb": round(tree / 1e6, 3),
            "fetched": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Git repository input for the distillation pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--url", required=True)
    f.add_argument("--ref", default="")
    f.add_argument("--subdir", default="")
    f.add_argument("--dest", required=True)
    f.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB)
    f.add_argument("--requested-ref", default="",
                   help="ref to record as requested when --ref is a pinned SHA (launcher, resume)")
    f.add_argument("--provenance-out", default="")
    c = sub.add_parser("check-url")
    c.add_argument("url")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "check-url":
            print(json.dumps({k: v for k, v in classify_url(a.url).items() if k != "url"}))
            return 0
        prov = fetch(a.url, a.ref, a.subdir, a.dest, a.max_mb)
        if a.requested_ref and a.requested_ref != (a.ref or ""):
            prov["ref_pinned"] = prov["ref_requested"]
            prov["ref_requested"] = a.requested_ref
    except FetchError as e:
        print(f"source_fetch: ERROR {e}", file=sys.stderr)
        return EXIT_ERROR
    if a.provenance_out:
        Path(a.provenance_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.provenance_out).write_text(json.dumps(prov, indent=2), encoding="utf-8")
    print(json.dumps(prov))
    return 0


if __name__ == "__main__":
    sys.exit(main())

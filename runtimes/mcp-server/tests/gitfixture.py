# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Local git repositories for tests (no network): a working repo plus a bare 'remote'."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    p = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                        "-c", "init.defaultBranch=main", *args], cwd=str(cwd), env=env,
                       capture_output=True, text=True, check=True)
    return p.stdout.strip()


class Remote:
    """work/ (where commits are made) pushes to remote.git (what the pipeline clones)."""

    def __init__(self, root: Path):
        self.root = root
        self.work = root / "work"
        self.bare = root / "remote.git"
        self.work.mkdir(parents=True)
        git(self.work, "init", "-q")
        git(root, "init", "-q", "--bare", str(self.bare))
        git(self.work, "remote", "add", "origin", str(self.bare))

    @property
    def url(self) -> str:
        return self.bare.as_uri()

    def commit(self, files: dict[str, str | bytes | None], msg: str = "c", branch: str = "main") -> str:
        for rel, body in files.items():
            p = self.work / rel
            if body is None:
                p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(body, bytes):
                p.write_bytes(body)
            else:
                p.write_text(body)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "--allow-empty", "-m", msg)
        git(self.work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return git(self.work, "rev-parse", "HEAD")

    def tag(self, name: str) -> None:
        git(self.work, "tag", "-a", name, "-m", name)
        git(self.work, "push", "-q", "origin", f"refs/tags/{name}")

    def checkout(self, branch: str, create: bool = False) -> None:
        git(self.work, "checkout", "-q", *(["-b"] if create else []), branch)

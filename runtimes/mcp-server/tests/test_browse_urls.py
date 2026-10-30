# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import source_fetch as sf

def n(u, r=None, s=None): return sf.normalize_browse_url(u, r, s)

def test_github_tree_with_folder():
    assert n("https://github.com/dotnet-architecture/eShopOnWeb/tree/main/src/ApplicationCore") == \
        ("https://github.com/dotnet-architecture/eShopOnWeb.git", "main", "src/ApplicationCore")

def test_github_tree_ref_only():
    assert n("https://github.com/dotnet-architecture/eShopOnWeb/tree/main") == \
        ("https://github.com/dotnet-architecture/eShopOnWeb.git", "main", None)

def test_github_folder_without_tree():
    assert n("https://github.com/dotnet-architecture/eShopOnWeb/src/ApplicationCore") == \
        ("https://github.com/dotnet-architecture/eShopOnWeb.git", None, "src/ApplicationCore")

def test_github_blob_uses_parent_folder():
    assert n("https://github.com/o/r/blob/dev/src/App/Program.cs") == ("https://github.com/o/r.git", "dev", "src/App")

def test_gitlab_dash_tree():
    assert n("https://gitlab.com/grp/sub/repo/-/tree/feature-x/lib") == \
        ("https://gitlab.com/grp/sub/repo.git", "feature-x", "lib")

def test_explicit_fields_win():
    assert n("https://github.com/o/r/tree/main/a", "v2", "b") == ("https://github.com/o/r.git", "v2", "b")

def test_plain_and_other_urls_unchanged():
    for u in ("https://github.com/o/r.git", "https://github.com/o/r", "git@github.com:o/r.git",
              "https://example.com/o/r/tree/main", "file:///tmp/x"):
        assert n(u)[0] == u

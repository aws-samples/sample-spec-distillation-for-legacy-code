# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Viewer hardening: unsafe link targets are neutralised and every page carries a CSP that
allows only the inline code present at generation time."""
from __future__ import annotations

import base64
import hashlib
import html as htmllib
import re
import sys
from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))

import viewer_security as vs  # noqa: E402


def _hash(src: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(src.encode()).digest()).decode() + "'"


def test_unsafe_links_neutralised_safe_links_kept():
    html = ('<a href="javascript:alert(1)">a</a><a href=" JaVa&#115;cript:x">b</a>'
            '<a href="data:text/html,x">c</a><a href="https://aws.amazon.com">d</a>'
            '<a href="#sec">e</a><a href="pages/x.html#L3">f</a><a href="mailto:a@b.c">g</a>')
    out = vs.sanitize_hrefs(html)
    assert out.count('href="#"') == 3
    for keep in ('https://aws.amazon.com', '#sec', 'pages/x.html#L3', 'mailto:a@b.c'):
        assert f'href="{keep}"' in out


def test_csp_hashes_inline_scripts_and_handlers_only():
    page = ('<html><head><title>t</title></head><body>'
            '<button onclick="go(this)">x</button><script>let a=1;</script>'
            '<script src="../assets/x.js"></script></body></html>')
    out = vs.harden(page)
    csp = htmllib.unescape(re.search(r'Content-Security-Policy" content="([^"]+)"', out).group(1))
    assert _hash("let a=1;") in csp and _hash("go(this)") in csp
    assert "'unsafe-inline'" not in csp.split("style-src")[0]       # scripts: hashes only
    assert "connect-src 'self'" in csp and "object-src 'none'" in csp


def test_harden_is_idempotent():
    page = "<html><head></head><body><script>x()</script></body></html>"
    once = vs.harden(page)
    assert vs.harden(once) == once
    assert once.count("Content-Security-Policy") == 1


def test_fragment_without_head_only_gets_link_sanitising():
    frag = '<p><a href="javascript:x">l</a></p>'
    assert vs.harden(frag) == '<p><a href="#">l</a></p>'

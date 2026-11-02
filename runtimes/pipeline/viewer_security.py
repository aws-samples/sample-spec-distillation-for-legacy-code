# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Hardening applied to every generated viewer HTML file.

The viewer renders text that comes from analysed code and from the model, so both are untrusted.
Two passes run on the final HTML:

1. sanitize_hrefs: any link whose target is not http(s), mailto, an in-page anchor or a relative
   path (for example javascript: or data:) is replaced with "#".
2. add_csp: a Content-Security-Policy <meta> tag allows only the inline scripts and inline event
   handlers present in the file when it was generated (by SHA-256 hash), plus same-origin scripts
   and the pinned marked script. An injected <script> or javascript: link does not run, and
   connect-src 'self' keeps fetches on the serving origin.
"""
from __future__ import annotations

import base64
import hashlib
import html
import re

MARKED_URL = "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"

_HREF = re.compile(r'(\bhref=)"([^"]*)"', re.I)
_SAFE_TARGET = re.compile(r"^(?:https?:|mailto:|#|/|\.{1,2}/|[^:]*$)", re.I)
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\b[^>]*>", re.I | re.S)
_HANDLER = re.compile(r'\bon[a-z]+="([^"]*)"', re.I)
_CSP_META = re.compile(r'<meta http-equiv="Content-Security-Policy"[^>]*>\s*', re.I)


def safe_href(url: str) -> bool:
    """True when url is a link target the viewer may render as clickable."""
    compact = re.sub(r"[\x00-\x20]", "", html.unescape(url))
    return bool(_SAFE_TARGET.match(compact))


def sanitize_hrefs(text: str) -> str:
    return _HREF.sub(lambda m: m.group(0) if safe_href(m.group(2)) else f'{m.group(1)}"#"', text)


def _sha256(source: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode() + "'"


def csp_for(text: str) -> str:
    """The policy for one HTML document, built from the inline code it contains."""
    hashes = {_sha256(s) for s in _INLINE_SCRIPT.findall(text)}
    for value in _HANDLER.findall(text):
        # Handlers appear both as static attributes and inside JS string literals that build
        # markup; hash the attribute value as the browser will see it in either form.
        for form in {html.unescape(value), html.unescape(value).replace("\\'", "'")}:
            hashes.add(_sha256(form))
    script_src = " ".join(["'self'", "'unsafe-hashes'", *sorted(hashes), MARKED_URL])
    return (f"default-src 'none'; script-src {script_src}; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'self'; "
            "base-uri 'none'; form-action 'none'; object-src 'none'")


def add_csp(text: str) -> str:
    """Insert (or refresh) the CSP meta tag right after <head>. Non-documents pass through."""
    text = _CSP_META.sub("", text)
    m = re.search(r"<head[^>]*>", text, re.I)
    if not m:
        return text
    meta = f'<meta http-equiv="Content-Security-Policy" content="{html.escape(csp_for(text))}">'
    return text[:m.end()] + meta + text[m.end():]


def harden(text: str) -> str:
    return add_csp(sanitize_hrefs(text))

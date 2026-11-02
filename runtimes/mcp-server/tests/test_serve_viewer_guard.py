# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""serve_viewer.py accepts API calls only from its own loopback origin, like the launcher.
No AWS: viewer_api is replaced by a stub."""
from __future__ import annotations

import http.client
import sys
import threading
import types
from http.server import HTTPServer
from pathlib import Path

import pytest

MCP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP))


@pytest.fixture
def server(tmp_path, monkeypatch):
    stub = types.ModuleType("viewer_api")
    stub.translate = lambda code, language: {"ok": True}
    monkeypatch.setitem(sys.modules, "viewer_api", stub)
    sys.modules.pop("serve_viewer", None)
    import serve_viewer
    (tmp_path / "index.html").write_text("<html></html>")
    serve_viewer.ViewerHandler.viewer_dir = str(tmp_path)
    srv = HTTPServer(("127.0.0.1", 0), serve_viewer.ViewerHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _req(port, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    return r.status, {k.lower(): v for k, v in r.getheaders()}


JSON = {"Content-Type": "application/json"}
BODY = '{"code": "x", "language": "vb"}'


def test_same_origin_json_post_allowed(server):
    status, hdrs = _req(server, "POST", "/api/translate", {**JSON, "Origin": f"http://127.0.0.1:{server}"}, BODY)
    assert status == 200 and "access-control-allow-origin" not in hdrs


@pytest.mark.parametrize("headers,expected", [
    ({**JSON, "Origin": "https://evil.example"}, 403),          # cross-origin page
    ({"Content-Type": "text/plain"}, 415),                     # simple form post, no preflight
    ({**JSON, "Host": "evil.example"}, 403),                    # DNS rebinding
])
def test_cross_site_posts_refused(server, headers, expected):
    assert _req(server, "POST", "/api/translate", headers, BODY)[0] == expected


def test_preflight_refused(server):
    status, hdrs = _req(server, "OPTIONS", "/api/translate", {"Origin": "https://evil.example"})
    assert status == 403 and "access-control-allow-origin" not in hdrs


def test_static_files_still_served(server):
    assert _req(server, "GET", "/")[0] == 200

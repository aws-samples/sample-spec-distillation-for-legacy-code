#!/usr/bin/env python3
"""
serve_viewer.py — HTTP server for the viewer chat panel.

Serves the viewer/ directory statically AND provides a /api/chat endpoint
for RAG queries against the vector store.

Usage:
    python serve_viewer.py <viewer_dir> <vector_db_path> [--port 8000]

The viewer's chat panel makes POST requests to /api/chat with:
    {"question": "...", "top_k": 15}

Returns:
    {"answer": "...", "sources": [...]}
"""

import argparse
import ipaddress
import json
import os
import sys
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import unquote, urlparse
import threading

# Add this directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import viewer_api  # the chat/validate/dependencies/translate logic (torch deferred inside)


def _bare_host(host: str) -> str:
    """Host header without its port ("[::1]:8080" -> "[::1]", "localhost:8080" -> "localhost")."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _is_loopback(host: str) -> bool:
    h = host.strip().strip("[]").lower()
    if h in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


class ViewerHandler(SimpleHTTPRequestHandler):
    """Serves static files + /api/chat and /api/validate endpoints."""

    vector_db_path = ""
    viewer_dir = ""
    source_root = ""  # Path to local source clone
    repo_url = ""  # GitHub repo base URL for evidence links

    def end_headers(self):
        """Forbid caching of viewer assets.

        The shell loads each page into an iframe, so a browser-cached page survives a normal
        reload of the shell: regenerating the viewer appears to do nothing, and you end up
        reading yesterday's spec while believing it is today's. During a demo that is worse
        than a visible error. These are local files on localhost — there is nothing to gain
        by caching them.
        """
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def _guard(self, write: bool = False) -> bool:
        """Same protections as the launcher: only a loopback Host (blocks DNS rebinding), no
        cross-origin callers (a page in another tab cannot drive the API), and JSON bodies only
        for POST (a plain HTML form cannot send one without a CORS preflight, which is refused)."""
        host = self.headers.get("Host") or ""
        if not _is_loopback(_bare_host(host)):
            self.send_error(403, "Forbidden host")
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            self.send_error(403, "Cross-origin requests are not allowed")
            return False
        if write and (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            self.send_error(415, "Content-Type must be application/json")
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        if urlparse(self.path).path == "/api/eval":
            # The Evaluation page's data: the run directory is the viewer directory's parent.
            try:
                result = viewer_api.eval_summary(str(Path(self.viewer_dir).resolve().parent))
                body, status = json.dumps(result).encode("utf-8"), 200
            except Exception as e:
                body, status = json.dumps({"error": f"evaluation summary failed: {type(e).__name__}"}).encode(), 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        if not self._guard(write=True):
            return
        if self.path == "/api/chat":
            self._handle_chat()
        elif self.path == "/api/validate":
            self._handle_validate()
        elif self.path == "/api/translate":
            self._handle_translate()
        elif self.path == "/api/dependencies":
            self._handle_dependencies()
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        """Refuse CORS preflights: the API is for the viewer's own origin only."""
        self.send_error(403, "Cross-origin requests are not allowed")

    def _handle_chat(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        question = data.get("question", "").strip()
        if not question:
            self.send_error(400, "Missing 'question' field")
            return

        top_k = data.get("top_k", 15)

        # Return failures AS JSON. An unhandled exception kills the connection, the browser's
        # r.json() throws, and the chat panel falls into its catch-all — which says "start the
        # server" even though the server is what served the page. viewer_api.chat maps the real
        # cause (expired credentials, no Bedrock access, missing vector store) into {"error": ...}.
        result = viewer_api.chat(self.vector_db_path, question, top_k)
        self._json_response(result)

    def _handle_validate(self):
        """Validate a block against its source evidence, or (when source is
        unavailable) against sibling blocks from the distilled corpus.

        Expects: {"block": "...", "title": "...", "evidence": "path#L1-L47, ..."}
        Returns: {"answer": "...", "source_content": "...", "cross_refs": [...]}
        """
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        result = viewer_api.validate(
            self.vector_db_path, self.source_root, self.repo_url,
            data.get("block", ""), data.get("title", ""), data.get("evidence", ""),
        )
        self._json_response(result)

    def _handle_dependencies(self):
        """Return code-dependency-derived prerequisites for a business concept.

        Expects: {"title": "...", "still": "..."}
        Returns: {requires: [...], required_by: [...], anchors: [...], ...}
        """
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        title = data.get("title", "")
        still = data.get("still", "") or None
        if not title:
            self._json_response({"error": "Missing 'title' field"})
            return

        result = viewer_api.dependencies(self.vector_db_path, title, still)
        self._json_response(result)

    def _handle_translate(self):
        """Translate a formal predicate block into a target programming language."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        code_block = data.get("code", "").strip()
        language = data.get("language", "").strip()

        if not code_block or not language:
            self._json_response({"error": "Missing 'code' or 'language' field"}, 400)
            return

        result = viewer_api.translate(code_block, language)
        self._json_response(result, 500 if "error" in result else 200)

    def _json_response(self, data: dict, status: int = 200):
        """Send a JSON response."""
        response = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def translate_path(self, path):
        """Serve files from viewer_dir, refusing any path that escapes it."""
        path = unquote(urlparse(path).path)
        if path == "/":
            path = "/index.html"
        root = Path(self.viewer_dir).resolve()
        target = (root / path.lstrip("/")).resolve()
        if target != root and root not in target.parents:
            # Not an existing file, so SimpleHTTPRequestHandler answers 404.
            return str(root / "__outside_viewer_dir__")
        return str(target)

    def log_message(self, format, *args):
        """Quieter logging."""
        if "/api/" in str(args[0]):
            sys.stderr.write(f"[chat] {args[0]}\n")


def main():
    parser = argparse.ArgumentParser(description="Serve viewer with chat API")
    parser.add_argument("viewer_dir", help="Path to the viewer/ directory")
    parser.add_argument("vector_db_path", help="Path to shared_vectors.db")
    parser.add_argument("--source-root", "-s", default="", help="Path to local source clone (enables /api/validate)")
    parser.add_argument("--repo-url", "-r", default="", help="GitHub repo URL (e.g. https://github.com/org/repo/blob/main/)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    ViewerHandler.vector_db_path = str(Path(args.vector_db_path).resolve())
    ViewerHandler.viewer_dir = str(Path(args.viewer_dir).resolve())
    ViewerHandler.source_root = str(Path(args.source_root).resolve()) if args.source_root else ""
    ViewerHandler.repo_url = args.repo_url.rstrip("/") + "/" if args.repo_url else ""

    server = HTTPServer(("localhost", args.port), ViewerHandler)
    print(f"Serving viewer at http://localhost:{args.port}")
    print(f"  Viewer: {args.viewer_dir}")
    print(f"  Vector DB: {args.vector_db_path}")
    if ViewerHandler.source_root:
        print(f"  Source root: {args.source_root} (validation enabled)")
    else:
        print(f"  Source root: not set (validation disabled — use --source-root)")
    if ViewerHandler.repo_url:
        print(f"  Repo URL: {ViewerHandler.repo_url}")
    print(f"  Chat API: http://localhost:{args.port}/api/chat")
    print(f"  Validate API: http://localhost:{args.port}/api/validate")
    print(f"  Translate API: http://localhost:{args.port}/api/translate")
    print(f"  Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

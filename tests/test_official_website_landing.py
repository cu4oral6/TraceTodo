from __future__ import annotations

import asyncio
import gzip
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from trace_memory.api.app import (
    DASHBOARD_HTML_PATH,
    LANDING_HTML_PATH,
    PWA_SW_JS,
    SCENES_DIR,
    _load_landing_html_cached,
    create_fastapi_app,
)
from trace_memory.config import TraceMemoryConfig
from trace_memory.container import AppContainer


REPO_ROOT = Path(__file__).resolve().parents[1]

_SERVER_CODE = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from trace_memory.api.app import run_web_server
from trace_memory.config import TraceMemoryConfig
from trace_memory.container import AppContainer
root = Path(sys.argv[2])
cfg = TraceMemoryConfig(
    default_space="default",
    data_dir=root / "data",
    knowledge_dir=root / "knowledge",
    auth_required=False,
    rate_limit_per_minute=10000,
)
container = AppContainer.build(cfg)
run_web_server(container, host="127.0.0.1", port=int(sys.argv[3]))
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestOfficialWebsiteLanding(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_container(self, *, auth_required: bool = False) -> AppContainer:
        cfg = TraceMemoryConfig(
            default_space="default",
            data_dir=self.root / ("data_auth" if auth_required else "data_open"),
            knowledge_dir=self.root / ("know_auth" if auth_required else "know_open"),
            auth_required=auth_required,
            rate_limit_per_minute=200,
        )
        return AppContainer.build(cfg)

    def test_fastapi_serves_landing_routes_with_gzip_and_304_and_keeps_root_dashboard(self) -> None:
        container = self._build_container(auth_required=True)
        try:
            app = create_fastapi_app(container)

            async def _call_asgi(path: str, req_headers: list[tuple[bytes, bytes]]) -> tuple[int, dict[str, str], bytes]:
                status_code = 0
                resp_headers: dict[str, str] = {}
                chunks: list[bytes] = []

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(msg):
                    nonlocal status_code, resp_headers
                    if msg["type"] == "http.response.start":
                        status_code = msg["status"]
                        resp_headers = {
                            k.decode("latin1").lower(): v.decode("latin1")
                            for k, v in msg.get("headers", [])
                        }
                    elif msg["type"] == "http.response.body":
                        chunks.append(msg.get("body", b""))

                scope = {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": path,
                    "raw_path": path.encode("ascii"),
                    "query_string": b"",
                    "headers": req_headers,
                    "client": ("127.0.0.1", 51234),
                    "server": ("127.0.0.1", 18090),
                }
                await app(scope, receive, send)
                return status_code, resp_headers, b"".join(chunks)

            raw_landing, expected_etag = _load_landing_html_cached()
            self.assertEqual(raw_landing, LANDING_HTML_PATH.read_bytes())

            for route in ("/home", "/welcome", "/official"):
                with self.subTest(route=route):
                    code, hdrs, gz_body = asyncio.run(
                        _call_asgi(route, [(b"accept-encoding", b"gzip, deflate, br")])
                    )
                    self.assertEqual(code, 200)
                    self.assertIn("text/html", hdrs.get("content-type", ""))
                    self.assertEqual(hdrs.get("content-encoding"), "gzip")
                    self.assertEqual(hdrs.get("cache-control"), "no-cache")
                    self.assertEqual(hdrs.get("etag"), expected_etag)
                    self.assertEqual(gzip.decompress(gz_body), raw_landing)

                    code_304, hdrs_304, body_304 = asyncio.run(
                        _call_asgi(
                            route,
                            [
                                (b"accept-encoding", b"gzip"),
                                (b"if-none-match", expected_etag.encode("latin1")),
                            ],
                        )
                    )
                    self.assertEqual(code_304, 304)
                    self.assertEqual(body_304, b"")
                    self.assertEqual(hdrs_304.get("etag"), expected_etag)

            # Root path continues returning dashboard.html with 200 on server
            code_root, _, gz_root = asyncio.run(
                _call_asgi("/", [(b"accept-encoding", b"gzip")])
            )
            self.assertEqual(code_root, 200)
            self.assertEqual(gzip.decompress(gz_root), DASHBOARD_HTML_PATH.read_bytes())
        finally:
            container.close()

    def test_zero_dep_server_serves_landing_routes_and_304(self) -> None:
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-c", _SERVER_CODE, str(REPO_ROOT / "src"), str(self.root), str(port)],
            env=dict(os.environ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def _stop_proc() -> None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)

        self.addCleanup(_stop_proc)
        deadline = time.time() + 15
        ready = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    ready = True
                    break
            except OSError:
                if proc.poll() is not None:
                    self.fail("test server exited early")
                time.sleep(0.15)
        self.assertTrue(ready, "test server did not start")

        expected_bytes = LANDING_HTML_PATH.read_bytes()
        for route in ("/home", "/welcome", "/official"):
            with self.subTest(route=route):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=5) as r:
                    self.assertEqual(r.status, 200)
                    self.assertIn("text/html", r.headers["Content-Type"])
                    self.assertEqual(r.headers["Cache-Control"], "no-cache")
                    etag = r.headers["ETag"]
                    self.assertTrue(etag.startswith('W/"'))
                    self.assertEqual(r.read(), expected_bytes)

                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{route}",
                    headers={"If-None-Match": etag},
                )
                try:
                    urllib.request.urlopen(req, timeout=5)
                    self.fail("expected 304")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 304)

    def test_landing_html_invariants_zero_cdn_and_valid_scenes(self) -> None:
        html = LANDING_HTML_PATH.read_text(encoding="utf-8")
        required_markers = [
            'id="landingAuthModal"',
            "/api/auth/login",
            "/api/auth/register",
            "/api/auth/status",
            "sanitizeNextPath",
            'id="pane-quadrant"',
            'id="pane-calendar"',
            'id="pane-focus"',
            'id="pane-graph"',
            'id="landingGraphCanvas"',
            "LANDING_SCENES",
            'id="mcpExampleBox"',
            'id="heroGuestBtn"',
            'id="navDownloadAppBtn"',
            'id="heroDownloadAppCta"',
            "https://github.com/cu4oral6/TraceTodo/releases",
            "[hidden] { display: none !important; }",
            "tm_auth_token",
            "tm_guest_mode",
            "GalaxyFX",
            'id="landingBlackHoleDemoBtn"',
            "triggerLandingBlackHoleDemo",
            "☀ Dream恒星",
            "黑洞（拖入吞噬）",
            "虫洞出口",
            "startLandingBlackHoleDevour",
        ]
        for marker in required_markers:
            self.assertIn(marker, html, f"Missing expected marker in landing.html: {marker}")

        for forbidden_cdn in (
            "fonts.googleapis.com",
            "fonts.gstatic.com",
            "cdn.jsdelivr.net",
            "unpkg.com",
            "cdnjs.cloudflare.com",
        ):
            self.assertNotIn(forbidden_cdn, html)

        # Every referenced scene wallpaper exists on disk
        referenced_scenes = set(re.findall(r"/scenes/([a-zA-Z0-9_]+\.jpg)", html))
        self.assertGreaterEqual(len(referenced_scenes), 8)
        for fname in referenced_scenes:
            self.assertTrue((SCENES_DIR / fname).is_file(), f"Missing scene file: {fname}")

        self.assertIn("'/home'", PWA_SW_JS)

    def test_dashboard_html_auth_redirect_gate_and_safety_invariants(self) -> None:
        html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
        for marker in (
            "window.__tmRedirectingToLanding = true",
            "share_token",
            "tm_guest_mode",
            "redirectToLanding('logged_out=1')",
            "redirectToLanding('auth=login&reason=expired')",
            'href="/home"',
            'id="topAppDownloadLink"',
            'id="prefsAppDownloadCard"',
            "https://github.com/cu4oral6/TraceTodo/releases",
            "fetchOpts.keepalive = true",
        ):
            self.assertIn(marker, html, f"Missing expected marker in dashboard.html: {marker}")

        self.assertNotIn("if (!confirm(", html)
        self.assertNotIn('<div class="rail-tip-box">', html)
        self.assertNotIn("/api/bootstrap?space_id=${encodeURIComponent(space())}", html)


if __name__ == "__main__":
    unittest.main()

"""Tests for Cosmic Memory Galaxy (Dream Stars + Mini-Galaxies + Black Hole / Wormhole) and Rounded App Icons."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from trace_memory.api.app import create_fastapi_app
from trace_memory.config import TraceMemoryConfig
from trace_memory.container import AppContainer


REPO_ROOT = Path(__file__).resolve().parent.parent
API_DIR = REPO_ROOT / "src" / "trace_memory" / "api"


class AppIconAssetsAndEndpointsTest(unittest.TestCase):
    def test_web_and_native_icons_have_zero_white_corners(self) -> None:
        from PIL import Image
        import numpy as np

        app_icon = Image.open(API_DIR / "app_icon.png").convert("RGBA")
        self.assertEqual(app_icon.size, (512, 512))
        arr = np.array(app_icon)
        for cy, cx in [(0, 0), (0, 511), (511, 0), (511, 511)]:
            self.assertEqual(int(arr[cy, cx, 3]), 0, f"Corner ({cy},{cx}) should be transparent")

        mask_outer = np.zeros((512, 512), dtype=bool)
        mask_outer[:45, :45] = True
        mask_outer[:45, 467:] = True
        mask_outer[467:, :45] = True
        mask_outer[467:, 467:] = True
        bright_corners = arr[
            mask_outer
            & (arr[:, :, 3] > 0)
            & (arr[:, :, 0] > 210)
            & (arr[:, :, 1] > 210)
            & (arr[:, :, 2] > 210)
        ]
        self.assertEqual(len(bright_corners), 0)

        maskable = Image.open(API_DIR / "app_icon_maskable.png")
        self.assertEqual(maskable.mode, "RGB")
        self.assertEqual(maskable.size, (512, 512))
        m_arr = np.array(maskable)
        for cy, cx in [(0, 0), (0, 511), (511, 0), (511, 511)]:
            r, g, b = (int(m_arr[cy, cx, 0]), int(m_arr[cy, cx, 1]), int(m_arr[cy, cx, 2]))
            self.assertLess(max(r, g, b), 210, f"Maskable corner ({cy},{cx}) looks white: {(r,g,b)}")

        # Apple icons
        icns_path = REPO_ROOT / "apple-app" / "Config" / "AppIcon.icns"
        self.assertTrue(icns_path.is_file())
        self.assertGreater(icns_path.stat().st_size, 10_000)
        ios_1024 = Image.open(
            REPO_ROOT / "apple-app" / "Config" / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon-1024.png"
        )
        self.assertEqual(ios_1024.mode, "RGB")
        self.assertEqual(ios_1024.size, (1024, 1024))

        # Android icons
        android_xxxhdpi = REPO_ROOT / "android-app" / "app" / "src" / "main" / "res" / "mipmap-xxxhdpi"
        and_launcher = Image.open(android_xxxhdpi / "ic_launcher.png").convert("RGBA")
        self.assertEqual(and_launcher.size, (192, 192))
        self.assertEqual(and_launcher.getpixel((0, 0))[3], 0)
        and_round = Image.open(android_xxxhdpi / "ic_launcher_round.png").convert("RGBA")
        self.assertEqual(and_round.size, (192, 192))
        self.assertEqual(and_round.getpixel((0, 0))[3], 0)

    def test_icon_endpoints_and_manifest_serve_new_assets(self) -> None:
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = TraceMemoryConfig(
                default_space="dev",
                data_dir=base / "data",
                knowledge_dir=base / "knowledge",
            )
            container = AppContainer.build(cfg)
            try:
                app = create_fastapi_app(container)
                client = TestClient(app)

                expected_icon = (API_DIR / "app_icon.png").read_bytes()
                expected_maskable = (API_DIR / "app_icon_maskable.png").read_bytes()

                res_icon = client.get("/icon.png")
                self.assertEqual(res_icon.status_code, 200)
                self.assertEqual(res_icon.headers.get("content-type"), "image/png")
                self.assertEqual(res_icon.content, expected_icon)

                res_fav = client.get("/favicon.ico")
                self.assertEqual(res_fav.status_code, 200)
                self.assertEqual(res_fav.content, expected_icon)

                res_apple = client.get("/apple-touch-icon.png")
                self.assertEqual(res_apple.status_code, 200)
                self.assertEqual(res_apple.content, expected_maskable)

                res_mask = client.get("/icon-maskable.png")
                self.assertEqual(res_mask.status_code, 200)
                self.assertEqual(res_mask.content, expected_maskable)

                res_manifest = client.get("/manifest.webmanifest")
                self.assertEqual(res_manifest.status_code, 200)
                manifest = json.loads(res_manifest.text)
                srcs = [item["src"] for item in manifest["icons"]]
                self.assertIn("/icon.png?v=4", srcs)
                self.assertIn("/icon-maskable.png?v=4", srcs)
            finally:
                container.close()


class CosmicMemoryGalaxyUiTest(unittest.TestCase):
    def test_dashboard_contains_galaxy_stars_blackhole_and_wormhole_without_mutation(self) -> None:
        html = (API_DIR / "dashboard.html").read_text(encoding="utf-8")
        for marker in (
            "🌌 记忆星系",
            "☀ Dream恒星",
            "isDreamStarNode",
            "drawDreamStar",
            "drawMiniGalaxy",
            "drawBlackHole",
            "drawWormhole",
            "getCosmicPortals",
            "stepCosmicAnimations",
            "startBlackHoleDevour",
            "devourNodeById",
            "triggerDemoBlackHoleDevour",
            'id="graphBlackHoleDemoBtn"',
            "仅演示不删数据",
            "border-radius: 22.4%",
            "/icon.png?v=4",
            "/apple-touch-icon.png?v=4",
        ):
            self.assertIn(marker, html, f"Missing expected marker in dashboard.html: {marker}")

        # Verify Black Hole devour animation never calls delete/forget endpoints
        bh_start = html.index("function startBlackHoleDevour(")
        bh_end = html.index("function drawDreamStar(", bh_start)
        bh_block = html[bh_start:bh_end]
        self.assertNotIn("/api/memories/forget", bh_block)
        self.assertNotIn("DELETE", bh_block)
        self.assertNotIn("apiPost", bh_block)

        landing = (API_DIR / "landing.html").read_text(encoding="utf-8")
        self.assertIn("/icon.png?v=4", landing)
        self.assertIn("/apple-touch-icon.png?v=4", landing)
        self.assertIn("border-radius: 22.4%", landing)


if __name__ == "__main__":
    unittest.main()

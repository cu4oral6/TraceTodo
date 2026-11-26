"""Regression coverage for the Memory Galaxy controls and visual-only demo."""

from pathlib import Path
import unittest


API_DIR = Path(__file__).resolve().parent.parent / "src" / "trace_memory" / "api"


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
            'id="graphDensityBtn"',
            "toggleGraphDensity()",
            "keepGraphClearOfPortals",
        ):
            self.assertIn(marker, html, f"Missing expected marker in dashboard.html: {marker}")

        # Verify Black Hole devour animation never calls delete/forget endpoints
        bh_start = html.index("function startBlackHoleDevour(")
        bh_end = html.index("function drawDreamStar(", bh_start)
        bh_block = html[bh_start:bh_end]
        self.assertNotIn("/api/memories/forget", bh_block)
        self.assertNotIn("DELETE", bh_block)
        self.assertNotIn("apiPost", bh_block)

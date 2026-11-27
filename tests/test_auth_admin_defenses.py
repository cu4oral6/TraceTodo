"""Tests for multi-user authentication, admin console, and low-cost spam/quota defenses."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from trace_memory.api.app import ApiDispatcher
from trace_memory.config import TraceMemoryConfig
from trace_memory.container import AppContainer
from trace_memory.llm.mock_provider import DeterministicLLMProvider, SemanticHashEmbedder
from trace_memory.llm.protocol import ExtractionBatch


class _CountingLLM(DeterministicLLMProvider):
    def __init__(self, *, empty_candidates: bool = False) -> None:
        super().__init__()
        self.extract_calls = 0
        self.ask_calls = 0
        self.empty_candidates = empty_candidates

    def extract_candidates(self, note_text: str, default_event_time: str, existing_memories=None):
        self.extract_calls += 1
        if self.empty_candidates:
            return ExtractionBatch(candidates=(), llm_calls=1, tokens_used=12)
        return super().extract_candidates(note_text, default_event_time, existing_memories)

    def synthesize_answer(self, question, query_mode, memories, notes, history_versions):
        self.ask_calls += 1
        return super().synthesize_answer(question, query_mode, memories, notes, history_versions)


class _CountingEmbedder(SemanticHashEmbedder):
    def __init__(self, dimension: int = 128) -> None:
        super().__init__(dimension=dimension)
        self.embed_calls = 0

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        return super().embed(text)


class TestAuthAdminAndDefenses(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.counting_llm = _CountingLLM()
        self.counting_embedder = _CountingEmbedder()
        self.app = AppContainer.build(
            TraceMemoryConfig(
                default_space="default",
                data_dir=base / "data",
                knowledge_dir=base / "knowledge",
                auth_required=True,
                allow_registration=True,
                rate_limit_per_minute=20,
                duplicate_window_seconds=120,
                default_daily_note_limit=5,
                default_daily_llm_limit=6,
                default_daily_token_limit=50000,
                max_note_chars=120,
            ),
            llm_override=self.counting_llm,
            embedder_override=self.counting_embedder,
        )
        self.api = ApiDispatcher(self.app)

    def tearDown(self) -> None:
        self.app.close()
        self._tmp.cleanup()

    def test_auth_required_isolation_and_scoped_supervision_share(self) -> None:
        # 1. Anonymous user cannot read private routes, while health & auth status remain public
        c_h, _ = self.api.dispatch("GET", "/api/health")
        self.assertEqual(c_h, 200)
        c_anon, body_anon = self.api.dispatch("GET", "/api/tasks/matrix")
        self.assertEqual(c_anon, 401)
        self.assertEqual(body_anon["code"], "UNAUTHORIZED")

        # 2. First registered user becomes initial admin bound to default space
        c_reg1, reg1 = self.api.dispatch(
            "POST",
            "/api/auth/register",
            body={"username": "admin_owner", "password": "secret123", "display_name": "站长"},
        )
        self.assertEqual(c_reg1, 200)
        self.assertEqual(reg1["user"]["role"], "admin")
        self.assertEqual(reg1["user"]["space_id"], "default")
        admin_tok = reg1["token"]

        # Admin creates a private personal task and a supervised task
        self.api.dispatch(
            "POST",
            "/api/tasks/save",
            body={"title": "管理员私人隐私待办", "supervision_enabled": False},
            auth_token=admin_tok,
        )
        c_sup_task, _ = self.api.dispatch(
            "POST",
            "/api/tasks/save",
            body={"title": "公开冲刺里程碑任务", "supervision_enabled": True, "assignee": "Xander"},
            auth_token=admin_tok,
        )
        self.assertEqual(c_sup_task, 200)

        # 3. Second registered user gets normal role + isolated space
        c_reg2, reg2 = self.api.dispatch(
            "POST",
            "/api/auth/register",
            body={"username": "bob_user", "password": "password123"},
        )
        self.assertEqual(c_reg2, 200)
        self.assertEqual(reg2["user"]["role"], "user")
        self.assertNotEqual(reg2["user"]["space_id"], "default")
        bob_tok = reg2["token"]

        # Bob CANNOT peek into admin's `default` space or call admin endpoints
        c_cross, err_cross = self.api.dispatch(
            "GET",
            "/api/tasks/matrix",
            {"space_id": ["default"]},
            auth_token=bob_tok,
        )
        self.assertEqual(c_cross, 403)
        self.assertEqual(err_cross["code"], "PERMISSION_DENIED")

        c_bob_adm, _ = self.api.dispatch("GET", "/api/admin/overview", auth_token=bob_tok)
        self.assertEqual(c_bob_adm, 403)

        # 4. Scoped supervision share token allows viewing/nudging ONLY supervision board
        c_sh, sh = self.api.dispatch("POST", "/api/supervision/share", auth_token=admin_tok)
        self.assertEqual(c_sh, 200)
        share_tok = sh["share_token"]

        c_sb, sb = self.api.dispatch(
            "GET",
            "/api/supervision/board",
            {"share_token": [share_tok]},
        )
        self.assertEqual(c_sb, 200)
        titles = [t["title"] for t in sb["tasks"]]
        self.assertIn("公开冲刺里程碑任务", titles)
        self.assertNotIn("管理员私人隐私待办", titles)

        # Share token MUST NOT grant access to full matrix or private memories
        c_denied_mat, _ = self.api.dispatch(
            "GET",
            "/api/tasks/matrix",
            {"share_token": [share_tok]},
        )
        self.assertEqual(c_denied_mat, 401)

        # 5. Logout invalidates token
        self.api.dispatch("POST", "/api/auth/logout", auth_token=bob_tok)
        c_after_out, _ = self.api.dispatch("GET", "/api/tasks/matrix", auth_token=bob_tok)
        self.assertEqual(c_after_out, 401)

    def test_admin_console_user_management_and_dynamic_settings(self) -> None:
        _, adm = self.api.dispatch(
            "POST",
            "/api/auth/register",
            body={"username": "root_adm", "password": "password123"},
        )
        _, usr = self.api.dispatch(
            "POST",
            "/api/auth/register",
            body={"username": "alice_u", "password": "password123"},
        )
        adm_tok = adm["token"]
        usr_tok = usr["token"]
        alice_id = usr["user"]["user_id"]

        # Admin closes public registration
        c_set, set_res = self.api.dispatch(
            "POST",
            "/api/admin/settings",
            body={"allow_registration": False, "max_note_chars": 500},
            auth_token=adm_tok,
        )
        self.assertEqual(c_set, 200)
        self.assertFalse(set_res["settings"]["allow_registration"])

        # Public registration is now denied, while admin-create still works
        c_pub_reg, _ = self.api.dispatch(
            "POST",
            "/api/auth/register",
            body={"username": "stranger99", "password": "password123"},
        )
        self.assertEqual(c_pub_reg, 403)

        c_adm_crt, adm_crt = self.api.dispatch(
            "POST",
            "/api/admin/users/create",
            body={"username": "invited01", "password": "password123"},
            auth_token=adm_tok,
        )
        self.assertEqual(c_adm_crt, 200)
        self.assertEqual(adm_crt["user"]["username"], "invited01")

        # Disabling Alice immediately revokes her active session and blocks re-login
        c_dis, dis = self.api.dispatch(
            "POST",
            "/api/admin/users/update",
            body={"user_id": alice_id, "status": "disabled"},
            auth_token=adm_tok,
        )
        self.assertEqual(c_dis, 200)
        self.assertEqual(dis["user"]["status"], "disabled")

        c_alice_api, _ = self.api.dispatch("GET", "/api/tasks/matrix", auth_token=usr_tok)
        self.assertEqual(c_alice_api, 401)
        c_alice_login, _ = self.api.dispatch(
            "POST",
            "/api/auth/login",
            body={"username": "alice_u", "password": "password123"},
        )
        self.assertEqual(c_alice_login, 403)

        # Admin cannot disable last active admin
        c_self_dis, _ = self.api.dispatch(
            "POST",
            "/api/admin/users/update",
            body={"user_id": adm["user"]["user_id"], "status": "disabled"},
            auth_token=adm_tok,
        )
        self.assertEqual(c_self_dis, 400)

        # Overview + maintenance
        c_ov, ov = self.api.dispatch("GET", "/api/admin/overview", auth_token=adm_tok)
        self.assertEqual(c_ov, 200)
        self.assertEqual(ov["users"]["total"], 3)
        self.assertEqual(ov["users"]["disabled"], 1)

        c_maint, _ = self.api.dispatch("POST", "/api/admin/maintenance", auth_token=adm_tok)
        self.assertEqual(c_maint, 200)

    def test_low_cost_defenses_noise_dedup_quota_and_selective_vector(self) -> None:
        _, adm = self.api.dispatch(
            "POST",
            "/api/auth/register",
            body={"username": "owner_u", "password": "password123"},
        )
        _, usr = self.api.dispatch(
            "POST",
            "/api/auth/register",
            body={"username": "spammer", "password": "password123"},
        )
        tok = usr["token"]
        sp = usr["user"]["space_id"]

        # 1. Junk / gibberish / greetings must trigger 0 LLM calls and 0 Embed calls!
        spam_inputs = ["哈哈", "在吗", "123456", "!!!", "aaaaaaaaaa", "qwertyuiop", "嗯"]
        for spam in spam_inputs:
            c, res = self.api.dispatch(
                "POST",
                "/api/notes/record",
                body={"content": spam},
                auth_token=tok,
            )
            self.assertEqual(c, 200)
            self.assertEqual(res["extraction_status"], "filtered_noise")
            self.assertTrue(res["filtered"])

        self.assertEqual(self.counting_llm.extract_calls, 0)
        self.assertEqual(self.counting_embedder.embed_calls, 0)
        self.assertEqual(len(self.app.note_repo.list_notes(sp)), 0)

        # Trivial ask consumes 0 LLM calls & 0 Embed calls
        c_ask, ask_res = self.api.dispatch(
            "POST",
            "/api/ask",
            body={"question": "哈哈"},
            auth_token=tok,
        )
        self.assertEqual(c_ask, 200)
        self.assertFalse(ask_res["has_sufficient_evidence"])
        self.assertTrue(ask_res["filtered"])
        self.assertEqual(self.counting_llm.ask_calls, 0)
        self.assertEqual(self.counting_embedder.embed_calls, 0)

        # Oversized note rejected cleanly before LLM/Embedding
        c_big, _ = self.api.dispatch(
            "POST",
            "/api/notes/record",
            body={"content": "写代码" * 60},
            auth_token=tok,
        )
        self.assertEqual(c_big, 400)
        self.assertEqual(self.counting_llm.extract_calls, 0)

        # Valid short task ("买牛奶", 3 chars) MUST pass through normally!
        c_ok, ok_res = self.api.dispatch(
            "POST",
            "/api/notes/record",
            body={"content": "今天下班买牛奶"},
            auth_token=tok,
        )
        self.assertEqual(c_ok, 200)
        self.assertEqual(ok_res["extraction_status"], "completed")
        self.assertEqual(self.counting_llm.extract_calls, 1)

        # Immediate duplicate submission must be caught without spending a 2nd LLM call!
        c_dup, dup_res = self.api.dispatch(
            "POST",
            "/api/notes/record",
            body={"content": "今天下班买牛奶"},
            auth_token=tok,
        )
        self.assertEqual(c_dup, 200)
        self.assertEqual(dup_res["extraction_status"], "filtered_noise")
        self.assertEqual(self.counting_llm.extract_calls, 1)

        # Verify blocked_noise_count recorded in admin overview
        _, ov = self.api.dispatch("GET", "/api/admin/overview", auth_token=adm["token"])
        self.assertGreaterEqual(ov["today_usage"]["blocked_noise_count"], len(spam_inputs) + 2)

        # 2. Daily quota limit enforcement (user limit is 5 valid notes)
        for i in range(4):
            c_n, _ = self.api.dispatch(
                "POST",
                "/api/notes/record",
                body={"content": f"明天必须推进第{i + 2}项接口测试任务"},
                auth_token=tok,
            )
            self.assertEqual(c_n, 200)

        c_over, over_body = self.api.dispatch(
            "POST",
            "/api/notes/record",
            body={"content": "第6条超出每日限额的有效任务"},
            auth_token=tok,
        )
        self.assertEqual(c_over, 429)
        self.assertEqual(over_body["code"], "QUOTA_EXCEEDED")

    def test_selective_note_vectorization_skips_embedding_when_zero_candidates(self) -> None:
        empty_llm = _CountingLLM(empty_candidates=True)
        embedder = _CountingEmbedder()
        with tempfile.TemporaryDirectory() as tmp2:
            base2 = Path(tmp2)
            app2 = AppContainer.build(
                TraceMemoryConfig(
                    default_space="s2",
                    data_dir=base2 / "data",
                    knowledge_dir=base2 / "knowledge",
                ),
                llm_override=empty_llm,
                embedder_override=embedder,
            )
            try:
                res = app2.ingest_service.record_note("s2", "今天天气真好风吹着很舒服")
                self.assertEqual(res.extraction_status, "completed")
                self.assertEqual(len(res.changes), 0)
                self.assertEqual(empty_llm.extract_calls, 1)
                self.assertEqual(embedder.embed_calls, 0)
            finally:
                app2.close()

    def test_fastapi_auth_and_payload_limit_integration(self) -> None:
        try:
            from fastapi.testclient import TestClient
            from trace_memory.api.app import create_fastapi_app
        except ImportError:
            self.skipTest("fastapi not installed")

        client = TestClient(create_fastapi_app(self.app))
        self.assertEqual(client.get("/api/tasks/matrix").status_code, 401)

        reg = client.post(
            "/api/auth/register",
            json={"username": "web_admin", "password": "password123"},
        )
        self.assertEqual(reg.status_code, 200)
        tok = reg.json()["token"]

        ok_mat = client.get("/api/tasks/matrix", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(ok_mat.status_code, 200)

        # Oversized HTTP body (> max_request_body_bytes = 131072) rejected with 413
        huge = client.post(
            "/api/notes/record",
            headers={"Authorization": f"Bearer {tok}"},
            json={"content": "x" * 140000},
        )
        self.assertEqual(huge.status_code, 413)

        # Dashboard HTML exposes auth & admin sections
        html = client.get("/").text
        self.assertIn("panel-admin", html)
        self.assertIn("authModal", html)

        # Remote `/mcp` ASGI endpoint enforces Bearer auth
        self.assertEqual(client.get("/mcp").status_code, 200)
        unauth_mcp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertEqual(unauth_mcp.status_code, 401)
        ok_mcp = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {tok}"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        self.assertEqual(ok_mcp.status_code, 200)
        self.assertGreaterEqual(len(ok_mcp.json()["result"]["tools"]), 7)

    def test_rate_limiter_triggers_429_cleanly(self) -> None:
        _, adm = self.api.dispatch(
            "POST",
            "/api/auth/register",
            body={"username": "rl_admin", "password": "password123"},
        )
        adm_tok = adm["token"]
        self.api.dispatch(
            "POST",
            "/api/admin/settings",
            body={"rate_limit_per_minute": 3},
            auth_token=adm_tok,
        )
        _, usr = self.api.dispatch(
            "POST",
            "/api/auth/register",
            body={"username": "rl_user", "password": "password123"},
        )
        usr_tok = usr["token"]
        for idx in range(3):
            c, _ = self.api.dispatch(
                "POST",
                "/api/tasks/save",
                body={"title": f"限流测试任务{idx}"},
                auth_token=usr_tok,
            )
            self.assertEqual(c, 200)
        c_blocked, body_blocked = self.api.dispatch(
            "POST",
            "/api/tasks/save",
            body={"title": "限流测试任务第4次"},
            auth_token=usr_tok,
        )
        self.assertEqual(c_blocked, 429)
        self.assertEqual(body_blocked["code"], "RATE_LIMIT_EXCEEDED")

    def test_remote_mcp_http_auth_space_isolation_defenses_and_bridge(self) -> None:
        import io
        import json
        from trace_memory.mcp_server.server import serve_mcp_remote_bridge

        # Bootstrap admin + regular user Bob
        _, adm = self.api.dispatch(
            "POST",
            "/api/auth/register",
            body={"username": "mcp_admin", "password": "password123"},
        )
        self.api.dispatch(
            "POST",
            "/api/tasks/save",
            body={"title": "管理员专属机密任务"},
            auth_token=adm["token"],
        )
        _, bob = self.api.dispatch(
            "POST",
            "/api/auth/register",
            body={"username": "mcp_bob", "password": "password123"},
        )
        bob_tok = bob["token"]
        bob_space = bob["user"]["space_id"]

        # 1. Anonymous remote MCP call rejected with 401
        init_bytes = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ).encode("utf-8")
        c_unauth, _ = self.api.dispatch_mcp_http(init_bytes)
        self.assertEqual(c_unauth, 401)

        # 2. Bob attempting cross-space override (`?space_id=default`) gets 403
        c_cross, _ = self.api.dispatch_mcp_http(
            init_bytes,
            query_params={"space_id": ["default"]},
            headers={"Authorization": f"Bearer {bob_tok}"},
        )
        self.assertEqual(c_cross, 403)

        # 3. Noise input over remote MCP `tm_record` returns filtered_noise at 0 LLM/Embed cost
        llm_before = self.counting_llm.extract_calls
        embed_before = self.counting_embedder.embed_calls
        noise_call = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "tm_record", "arguments": {"content": "哈哈哈"}},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        c_noise, noise_resp = self.api.dispatch_mcp_http(
            noise_call, headers={"Authorization": f"Bearer {bob_tok}"}
        )
        self.assertEqual(c_noise, 200)
        self.assertFalse(noise_resp["result"]["isError"])
        self.assertEqual(noise_resp["result"]["structuredContent"]["extraction_status"], "filtered_noise")
        self.assertEqual(self.counting_llm.extract_calls, llm_before)
        self.assertEqual(self.counting_embedder.embed_calls, embed_before)

        # 4. End-to-end stdio->remote bridge via opener forwarding to dispatch_mcp_http
        def _fake_opener(url: str, hdrs: dict[str, str], payload: bytes) -> tuple[int, str]:
            status, obj = self.api.dispatch_mcp_http(payload, headers=hdrs)
            return status, ("" if obj is None else json.dumps(obj, ensure_ascii=False))

        req_lines = [
            {"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "tm_record",
                    "arguments": {"content": "明天必须完成远程 MCP 对接测试"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {"name": "tm_tasks", "arguments": {"sort_by": "priority"}},
            },
        ]
        in_buf = io.StringIO("\n".join(json.dumps(x, ensure_ascii=False) for x in req_lines) + "\n")
        out_buf = io.StringIO()
        serve_mcp_remote_bridge(
            "http://example.invalid:18090",
            auth_token=bob_tok,
            stdin=in_buf,
            stdout=out_buf,
            opener=_fake_opener,
        )
        bridge_resps = [json.loads(ln) for ln in out_buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(bridge_resps), 3)
        bob_tasks = bridge_resps[2]["result"]["structuredContent"]["sorted_tasks"]
        self.assertEqual(len(bob_tasks), 1)
        self.assertEqual(bob_tasks[0]["space_id"], bob_space)
        self.assertNotIn("管理员专属机密任务", json.dumps(bob_tasks, ensure_ascii=False))

    def test_register_captcha_challenge_and_anti_replay(self) -> None:
        import base64
        import re

        env_cfg = TraceMemoryConfig.from_env({})
        self.assertTrue(env_cfg.register_captcha_required)

        with tempfile.TemporaryDirectory() as cap_tmp:
            cap_base = Path(cap_tmp)
            cap_app = AppContainer.build(
                TraceMemoryConfig(
                    default_space="cap_space",
                    data_dir=cap_base / "data",
                    knowledge_dir=cap_base / "knowledge",
                    auth_required=True,
                    register_captcha_required=True,
                ),
                llm_override=self.counting_llm,
                embedder_override=self.counting_embedder,
            )
            try:
                cap_api = ApiDispatcher(cap_app)
                _, st = cap_api.dispatch("GET", "/api/auth/status")
                self.assertTrue(st["register_captcha_required"])

                # 1. Missing captcha must be rejected
                c_miss, miss_body = cap_api.dispatch(
                    "POST",
                    "/api/auth/register",
                    body={"username": "cap_user1", "password": "password123"},
                )
                self.assertEqual(c_miss, 400)
                self.assertIn("验证码", miss_body["error"])

                # 2. Issue captcha & verify SVG + PNG payloads
                c_cap1, cap1 = cap_api.dispatch("GET", "/api/auth/captcha")
                self.assertEqual(c_cap1, 200)
                self.assertTrue(cap1["captcha_id"].startswith("cap_"))
                self.assertTrue(cap1["image_svg"].startswith("data:image/svg+xml;base64,"))
                self.assertTrue(cap1["image_png"].startswith("data:image/png;base64,"))
                png_raw = base64.b64decode(cap1["image_png"].split(",", 1)[1])
                self.assertTrue(png_raw.startswith(b"\x89PNG\r\n\x1a\n"))

                svg_xml1 = base64.b64decode(cap1["image_svg"].split(",", 1)[1]).decode("utf-8")
                true_code1 = "".join(re.findall(r">([A-Z2-9])</text>", svg_xml1))
                self.assertEqual(len(true_code1), 4)

                # 3. Wrong code fails AND immediately consumes captcha_id (anti-brute-force)
                c_wrong, _ = cap_api.dispatch(
                    "POST",
                    "/api/auth/register",
                    body={
                        "username": "cap_user1",
                        "password": "password123",
                        "captcha_id": cap1["captcha_id"],
                        "captcha_code": "ZZZZ" if true_code1 != "ZZZZ" else "AAAA",
                    },
                )
                self.assertEqual(c_wrong, 400)

                c_replay_after_wrong, replay_err = cap_api.dispatch(
                    "POST",
                    "/api/auth/register",
                    body={
                        "username": "cap_user1",
                        "password": "password123",
                        "captcha_id": cap1["captcha_id"],
                        "captcha_code": true_code1,
                    },
                )
                self.assertEqual(c_replay_after_wrong, 400)
                self.assertIn("过期或失效", replay_err["error"])

                # 4. Fresh captcha succeeds (case-insensitive) and cannot be replayed
                _, cap2 = cap_api.dispatch("GET", "/api/auth/captcha")
                svg_xml2 = base64.b64decode(cap2["image_svg"].split(",", 1)[1]).decode("utf-8")
                true_code2 = "".join(re.findall(r">([A-Z2-9])</text>", svg_xml2))
                c_ok, ok_body = cap_api.dispatch(
                    "POST",
                    "/api/auth/register",
                    body={
                        "username": "cap_user1",
                        "password": "password123",
                        "captcha_id": cap2["captcha_id"],
                        "captcha_code": true_code2.lower(),
                    },
                )
                self.assertEqual(c_ok, 200)
                self.assertIn("token", ok_body)

                c_replay2, _ = cap_api.dispatch(
                    "POST",
                    "/api/auth/register",
                    body={
                        "username": "cap_user2",
                        "password": "password123",
                        "captcha_id": cap2["captcha_id"],
                        "captcha_code": true_code2,
                    },
                )
                self.assertEqual(c_replay2, 400)
            finally:
                cap_app.close()


if __name__ == "__main__":
    unittest.main()

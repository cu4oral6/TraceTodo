"""User accounts, session authentication, supervision share tokens, and Admin console service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from typing import Any
import uuid

from trace_memory.config import TraceMemoryConfig
from trace_memory.domain.notes import BEIJING_TZ, beijing_now, new_id, to_beijing, utc_now_iso
from trace_memory.domain.proposals import (
    NotFoundError,
    PermissionDeniedError,
    UnauthorizedError,
    ValidationError,
)
from trace_memory.services.captcha_generator import (
    generate_captcha_code,
    render_captcha_png_data_url,
    render_captcha_svg_data_url,
)
from trace_memory.services.guard_service import (
    RateLimitAndQuotaService,
    SystemSettingsStore,
    today_utc_date,
)
from trace_memory.storage.database import Database


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\-\u4e00-\u9fff]{3,32}$")


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_password(password: str, iterations: int = 200_000) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, iter_str, salt_hex, hash_hex = stored_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iter_str),
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


@dataclass(frozen=True)
class UserAccount:
    user_id: str
    username: str
    display_name: str
    role: str  # "admin" | "user"
    status: str  # "active" | "disabled"
    space_id: str
    daily_note_limit: int
    daily_llm_limit: int
    daily_token_limit: int
    created_at: str
    updated_at: str
    last_login_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "status": self.status,
            "space_id": self.space_id,
            "daily_note_limit": self.daily_note_limit,
            "daily_llm_limit": self.daily_llm_limit,
            "daily_token_limit": self.daily_token_limit,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_login_at": self.last_login_at,
        }


class AuthService:
    """Manages user registration, login, session verification, and scoped supervision shares."""

    def __init__(
        self,
        db: Database,
        config: TraceMemoryConfig,
        settings: SystemSettingsStore,
        guard: RateLimitAndQuotaService,
    ) -> None:
        self.db = db
        self.config = config
        self.settings = settings
        self.guard = guard
        self._ensure_captcha_table()
        self.ensure_bootstrap_admin()

    def _ensure_captcha_table(self) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_captchas (
                    captcha_id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    client_ip TEXT NOT NULL DEFAULT '',
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def issue_captcha(self, client_ip: str = "127.0.0.1") -> dict[str, Any]:
        self.guard.check_rate_limit(
            f"captcha:{client_ip}", limit=30, window_seconds=60, action_label="获取图形验证码"
        )
        now_ts = time.time()
        code = generate_captcha_code(4)
        captcha_id = new_id("cap")
        expires_in = 300
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM auth_captchas WHERE expires_at < ?", (now_ts,))
            conn.execute(
                """
                INSERT INTO auth_captchas (captcha_id, code_hash, client_ip, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    captcha_id,
                    _sha256_hex(code.upper()),
                    client_ip[:64],
                    now_ts + expires_in,
                    utc_now_iso(),
                ),
            )
        return {
            "captcha_id": captcha_id,
            "image_svg": render_captcha_svg_data_url(code),
            "image_png": render_captcha_png_data_url(code),
            "expires_in": expires_in,
        }

    def verify_and_consume_captcha(
        self,
        captcha_id: str,
        captcha_code: str,
        *,
        client_ip: str = "127.0.0.1",
    ) -> None:
        cid = captcha_id.strip()
        raw_code = captcha_code.strip().upper()
        if not cid or not raw_code:
            raise ValidationError("请输入图形验证码")
        now_ts = time.time()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT code_hash, expires_at FROM auth_captchas WHERE captcha_id = ?",
                (cid,),
            ).fetchone()
            if row is not None:
                conn.execute("DELETE FROM auth_captchas WHERE captcha_id = ?", (cid,))
        if row is None or float(row["expires_at"]) < now_ts:
            self.guard.log_security_event(
                action="auth_register_captcha",
                status="failed",
                detail={"reason": "expired_or_missing"},
                client_ip=client_ip,
            )
            raise ValidationError("验证码已过期或失效，请点击验证码图片刷新重试")
        if not hmac.compare_digest(str(row["code_hash"]), _sha256_hex(raw_code)):
            self.guard.log_security_event(
                action="auth_register_captcha",
                status="failed",
                detail={"reason": "mismatch"},
                client_ip=client_ip,
            )
            raise ValidationError("图形验证码不正确，请重新输入")

    @staticmethod
    def _row_to_user(row: Any) -> UserAccount:
        return UserAccount(
            user_id=str(row["user_id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=str(row["role"]),
            status=str(row["status"]),
            space_id=str(row["space_id"]),
            daily_note_limit=int(row["daily_note_limit"]),
            daily_llm_limit=int(row["daily_llm_limit"]),
            daily_token_limit=int(row["daily_token_limit"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_login_at=str(row["last_login_at"]),
        )

    def has_users(self) -> bool:
        row = self.db.query_one("SELECT 1 AS ok FROM users LIMIT 1")
        return row is not None

    def get_user_by_id(self, user_id: str) -> UserAccount | None:
        row = self.db.query_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return self._row_to_user(row) if row else None

    def get_user_by_username(self, username: str) -> UserAccount | None:
        norm = username.strip().lower()
        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (norm,))
        return self._row_to_user(row) if row else None

    def ensure_bootstrap_admin(self) -> UserAccount | None:
        uname = self.config.admin_username.strip().lower()
        pw = self.config.admin_password
        if not uname or not pw:
            return None
        existing = self.get_user_by_username(uname)
        if existing is not None:
            return existing
        if not _USERNAME_RE.match(uname) or len(pw) < 6:
            return None
        default_space_taken = self.db.query_one(
            "SELECT 1 FROM users WHERE space_id = ?", (self.config.default_space,)
        )
        space_id = (
            self.config.default_space
            if default_space_taken is None
            else f"u_{uuid.uuid4().hex[:10]}"
        )
        now = utc_now_iso()
        user_id = new_id("usr")
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id, username, display_name, password_hash, role, status,
                    space_id, daily_note_limit, daily_llm_limit, daily_token_limit,
                    created_at, updated_at, last_login_at
                ) VALUES (?, ?, ?, ?, 'admin', 'active', ?, ?, ?, ?, ?, ?, '')
                """,
                (
                    user_id,
                    uname,
                    "管理员",
                    hash_password(pw),
                    space_id,
                    max(self.config.default_daily_note_limit, 500),
                    max(self.config.default_daily_llm_limit, 500),
                    max(self.config.default_daily_token_limit, 500000),
                    now,
                    now,
                ),
            )
        return self.get_user_by_id(user_id)

    def _validate_credentials(self, username: str, password: str) -> str:
        uname = username.strip().lower()
        if not _USERNAME_RE.match(uname):
            raise ValidationError("用户名需为 3–32 位字母、数字、下划线或中文")
        if len(password) < 6 or len(password) > 128:
            raise ValidationError("密码长度需在 6 到 128 位之间")
        return uname

    def _issue_session(self, user: UserAccount, device_label: str = "") -> dict[str, Any]:
        raw_token = f"tm_{uuid.uuid4().hex}{uuid.uuid4().hex[:16]}"
        token_hash = _sha256_hex(raw_token)
        now_dt = beijing_now().replace(microsecond=0)
        exp_dt = now_dt + timedelta(hours=max(1, int(self.config.session_ttl_hours)))
        now_iso = now_dt.isoformat()
        exp_iso = exp_dt.isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO user_sessions
                    (token_hash, user_id, device_label, expires_at, created_at, last_used_at, revoked)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (token_hash, user.user_id, device_label[:64], exp_iso, now_iso, now_iso),
            )
            conn.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE user_id = ?",
                (now_iso, now_iso, user.user_id),
            )
        usage = self.guard.get_usage(f"space:{user.space_id}")
        return {
            "token": raw_token,
            "expires_at": exp_iso,
            "user": user.to_dict(),
            "quota": usage,
        }

    def register(
        self,
        username: str,
        password: str,
        display_name: str = "",
        device_label: str = "web",
        client_ip: str = "127.0.0.1",
        captcha_id: str = "",
        captcha_code: str = "",
    ) -> dict[str, Any]:
        self.guard.check_rate_limit(
            f"register:{client_ip}", limit=12, window_seconds=60, action_label="注册请求"
        )
        if self.config.register_captcha_required or captcha_id.strip() or captcha_code.strip():
            self.verify_and_consume_captcha(
                captcha_id,
                captcha_code,
                client_ip=client_ip,
            )
        uname = self._validate_credentials(username, password)
        cfg = self.settings.get_all()
        now = utc_now_iso()
        user_id = new_id("usr")
        d_name = display_name.strip()[:40] or uname

        with self.db.transaction() as conn:
            cnt_row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
            first_user = int(cnt_row["cnt"] or 0) == 0 if cnt_row is not None else True
            if not first_user and not bool(cfg.get("allow_registration", True)):
                raise PermissionDeniedError("当前系统已关闭公开注册，请联系管理员创建账号")

            dup_row = conn.execute("SELECT 1 FROM users WHERE username = ?", (uname,)).fetchone()
            if dup_row is not None:
                raise ValidationError(f"用户名 '{uname}' 已被注册")

            role = "admin" if first_user else "user"
            space_taken = conn.execute(
                "SELECT 1 FROM users WHERE space_id = ?", (self.config.default_space,)
            ).fetchone()
            space_id = (
                self.config.default_space
                if (first_user and space_taken is None)
                else f"u_{uuid.uuid4().hex[:10]}"
            )
            note_lim = int(cfg["default_daily_note_limit"]) * (5 if role == "admin" else 1)
            llm_lim = int(cfg["default_daily_llm_limit"]) * (5 if role == "admin" else 1)
            tok_lim = int(cfg["default_daily_token_limit"]) * (5 if role == "admin" else 1)

            conn.execute(
                """
                INSERT INTO users (
                    user_id, username, display_name, password_hash, role, status,
                    space_id, daily_note_limit, daily_llm_limit, daily_token_limit,
                    created_at, updated_at, last_login_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, '')
                """,
                (
                    user_id,
                    uname,
                    d_name,
                    hash_password(password),
                    role,
                    space_id,
                    note_lim,
                    llm_lim,
                    tok_lim,
                    now,
                    now,
                ),
            )
        user = self.get_user_by_id(user_id)
        assert user is not None
        self.guard.log_security_event(
            action="auth_register",
            status="ok",
            actor_user_id=user.user_id,
            space_id=user.space_id,
            detail={"username": uname, "role": role, "bootstrap_admin": first_user},
            client_ip=client_ip,
        )
        return self._issue_session(user, device_label=device_label)

    def login(
        self,
        username: str,
        password: str,
        device_label: str = "web",
        client_ip: str = "127.0.0.1",
    ) -> dict[str, Any]:
        self.guard.check_rate_limit(
            f"login:{client_ip}", limit=20, window_seconds=60, action_label="登录尝试"
        )
        uname = username.strip().lower()
        if not uname or not password:
            raise ValidationError("请输入用户名和密码")
        self.guard.check_rate_limit(
            f"login_user:{uname}", limit=10, window_seconds=300, action_label="该账号登录尝试"
        )

        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (uname,))
        if row is None or not verify_password(password, str(row["password_hash"])):
            self.guard.log_security_event(
                action="auth_login",
                status="failed",
                detail={"username": uname, "reason": "invalid_credentials"},
                client_ip=client_ip,
            )
            raise UnauthorizedError("用户名或密码错误")

        user = self._row_to_user(row)
        if user.status != "active":
            self.guard.log_security_event(
                action="auth_login",
                status="blocked_disabled",
                actor_user_id=user.user_id,
                space_id=user.space_id,
                detail={"username": uname},
                client_ip=client_ip,
            )
            raise PermissionDeniedError("该账号已被管理员停用")

        self.guard.log_security_event(
            action="auth_login",
            status="ok",
            actor_user_id=user.user_id,
            space_id=user.space_id,
            detail={"username": uname, "device": device_label},
            client_ip=client_ip,
        )
        return self._issue_session(user, device_label=device_label)

    def authenticate(self, raw_token: str) -> UserAccount | None:
        token = raw_token.strip()
        if not token:
            return None
        row = self.db.query_one(
            """
            SELECT s.expires_at, s.revoked, s.last_used_at, u.*
            FROM user_sessions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.token_hash = ?
            """,
            (_sha256_hex(token),),
        )
        if row is None or int(row["revoked"]) != 0:
            return None
        if str(row["status"]) != "active":
            return None
        try:
            exp_dt = to_beijing(datetime.fromisoformat(str(row["expires_at"])))
            if exp_dt <= beijing_now():
                return None
        except Exception:
            return None

        # Throttle session `last_used_at` updates to at most once every 60s
        try:
            last_dt = to_beijing(datetime.fromisoformat(str(row["last_used_at"])))
            if (beijing_now() - last_dt).total_seconds() >= 60:
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE user_sessions SET last_used_at = ? WHERE token_hash = ?",
                        (utc_now_iso(), _sha256_hex(token)),
                    )
        except Exception:
            pass
        return self._row_to_user(row)

    def logout(self, raw_token: str) -> bool:
        token = raw_token.strip()
        if not token:
            return False
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE user_sessions SET revoked = 1 WHERE token_hash = ?",
                (_sha256_hex(token),),
            )
            return cur.rowcount > 0

    def change_password(
        self,
        user: UserAccount,
        old_password: str,
        new_password: str,
        *,
        device_label: str = "web",
        client_ip: str = "",
    ) -> dict[str, Any]:
        if len(new_password) < 6 or len(new_password) > 128:
            raise ValidationError("新密码长度需在 6 到 128 位之间")
        row = self.db.query_one("SELECT password_hash FROM users WHERE user_id = ?", (user.user_id,))
        if row is None or not verify_password(old_password, str(row["password_hash"])):
            raise UnauthorizedError("原密码校验失败")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE user_id = ?",
                (hash_password(new_password), utc_now_iso(), user.user_id),
            )
            cur = conn.execute(
                "UPDATE user_sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0",
                (user.user_id,),
            )
            revoked_count = int(cur.rowcount or 0)
        self.guard.log_security_event(
            action="auth_change_password",
            status="ok",
            actor_user_id=user.user_id,
            space_id=user.space_id,
            detail={"revoked_sessions": revoked_count},
            client_ip=client_ip,
        )
        fresh = self._issue_session(user, device_label=device_label)
        return {
            "updated": True,
            "revoked_sessions": revoked_count,
            "token": fresh["token"],
            "expires_at": fresh["expires_at"],
        }

    def revoke_user_sessions(self, user_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE user_sessions SET revoked = 1 WHERE user_id = ?", (user_id,))

    def create_supervision_share(self, space_id: str, owner_user_id: str = "", label: str = "好友监督") -> dict[str, Any]:
        raw_share = f"sup_{uuid.uuid4().hex[:24]}"
        share_id = new_id("shr")
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO supervision_shares
                    (share_id, token_hash, space_id, owner_user_id, label, revoked, created_at)
                VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (share_id, _sha256_hex(raw_share), space_id, owner_user_id, label[:40], now),
            )
        return {
            "share_id": share_id,
            "share_token": raw_share,
            "space_id": space_id,
            "label": label[:40],
        }

    def list_supervision_shares(self, space_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            """
            SELECT share_id, space_id, owner_user_id, label, revoked, created_at
            FROM supervision_shares
            WHERE space_id = ? AND revoked = 0
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (space_id,),
        )
        return [
            {
                "share_id": str(r["share_id"]),
                "space_id": str(r["space_id"]),
                "owner_user_id": str(r["owner_user_id"]),
                "label": str(r["label"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    def revoke_supervision_share(self, space_id: str, share_id: str = "") -> int:
        with self.db.transaction() as conn:
            if share_id.strip():
                cur = conn.execute(
                    "UPDATE supervision_shares SET revoked = 1 WHERE space_id = ? AND share_id = ? AND revoked = 0",
                    (space_id, share_id.strip()),
                )
            else:
                cur = conn.execute(
                    "UPDATE supervision_shares SET revoked = 1 WHERE space_id = ? AND revoked = 0",
                    (space_id,),
                )
            return int(cur.rowcount or 0)

    def resolve_supervision_share(self, raw_share_token: str) -> dict[str, Any] | None:
        token = raw_share_token.strip()
        if not token:
            return None
        row = self.db.query_one(
            """
            SELECT share_id, space_id, owner_user_id, label
            FROM supervision_shares
            WHERE token_hash = ? AND revoked = 0
            """,
            (_sha256_hex(token),),
        )
        if row is None:
            return None
        return {
            "share_id": str(row["share_id"]),
            "space_id": str(row["space_id"]),
            "owner_user_id": str(row["owner_user_id"]),
            "label": str(row["label"]),
        }


class AdminService:
    """System-wide metrics, user/quota administration, runtime defense tuning, and audit visibility."""

    def __init__(
        self,
        db: Database,
        config: TraceMemoryConfig,
        settings: SystemSettingsStore,
        auth: AuthService,
        guard: RateLimitAndQuotaService,
    ) -> None:
        self.db = db
        self.config = config
        self.settings = settings
        self.auth = auth
        self.guard = guard

    @staticmethod
    def require_admin(user: UserAccount | None) -> UserAccount:
        if user is None:
            raise UnauthorizedError("访问后台管理需要管理员登录")
        if user.role != "admin":
            raise PermissionDeniedError("仅管理员可执行此操作")
        return user

    def get_overview(self, actor: UserAccount | None) -> dict[str, Any]:
        self.require_admin(actor)
        today = today_utc_date()
        user_stats = self.db.query_one(
            """
            SELECT
                COUNT(*) AS total_users,
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_users,
                SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END) AS disabled_users,
                SUM(CASE WHEN role = 'admin' THEN 1 ELSE 0 END) AS admin_users
            FROM users
            """
        )
        content_stats = self.db.query_one(
            """
            SELECT
                (SELECT COUNT(*) FROM raw_notes WHERE is_forgotten = 0) AS raw_notes_total,
                (SELECT COUNT(*) FROM memories WHERE status = 'active') AS active_memories,
                (SELECT COUNT(*) FROM memories WHERE status = 'pending_review') AS pending_memories,
                (SELECT COUNT(*) FROM memories WHERE status = 'active' AND memory_type = 'task'
                    AND json_extract(payload_json, '$.task_status') != 'done') AS open_tasks,
                (SELECT COUNT(*) FROM memories WHERE status = 'active' AND memory_type = 'task'
                    AND json_extract(payload_json, '$.task_status') = 'done') AS done_tasks
            """
        )
        today_usage = self.db.query_one(
            """
            SELECT
                COALESCE(SUM(notes_count), 0) AS notes_count,
                COALESCE(SUM(blocked_noise_count), 0) AS blocked_noise_count,
                COALESCE(SUM(llm_calls), 0) AS llm_calls,
                COALESCE(SUM(embed_calls), 0) AS embed_calls,
                COALESCE(SUM(tokens_used), 0) AS tokens_used
            FROM daily_usage
            WHERE usage_date = ?
            """,
            (today,),
        )
        daily_series = [
            {
                "date": str(r["usage_date"]),
                "notes_count": int(r["notes_count"]),
                "blocked_noise_count": int(r["blocked_noise_count"]),
                "llm_calls": int(r["llm_calls"]),
                "embed_calls": int(r["embed_calls"]),
                "tokens_used": int(r["tokens_used"]),
            }
            for r in self.db.query_all(
                """
                SELECT
                    usage_date,
                    COALESCE(SUM(notes_count), 0) AS notes_count,
                    COALESCE(SUM(blocked_noise_count), 0) AS blocked_noise_count,
                    COALESCE(SUM(llm_calls), 0) AS llm_calls,
                    COALESCE(SUM(embed_calls), 0) AS embed_calls,
                    COALESCE(SUM(tokens_used), 0) AS tokens_used
                FROM daily_usage
                GROUP BY usage_date
                ORDER BY usage_date DESC
                LIMIT 7
                """
            )
        ]

        db_size_bytes = 0
        try:
            if self.config.sqlite_path.exists():
                db_size_bytes = int(self.config.sqlite_path.stat().st_size)
        except OSError:
            pass

        audit_logs = [
            {
                "log_id": str(r["log_id"]),
                "actor_user_id": str(r["actor_user_id"]),
                "space_id": str(r["space_id"]),
                "action": str(r["action"]),
                "status": str(r["status"]),
                "detail": json.loads(str(r["detail_json"] or "{}")),
                "client_ip": str(r["client_ip"]),
                "created_at": str(r["created_at"]),
            }
            for r in self.db.query_all(
                "SELECT * FROM security_audit_logs ORDER BY created_at DESC LIMIT 40"
            )
        ]

        recent_traces = [
            {
                "trace_id": str(r["trace_id"]),
                "space_id": str(r["space_id"]),
                "operation": str(r["operation"]),
                "input_summary": str(r["input_summary"])[:80],
                "llm_calls": int(r["llm_calls"]),
                "tokens_used": int(r["tokens_used"]),
                "duration_ms": round(float(r["duration_ms"]), 1),
                "created_at": str(r["created_at"]),
            }
            for r in self.db.query_all(
                "SELECT * FROM trace_runs ORDER BY created_at DESC LIMIT 40"
            )
        ]

        return {
            "today": today,
            "runtime": {
                "auth_required": bool(self.config.auth_required),
                "vector_backend": self.config.vector_backend,
                "llm_provider": self.config.llm_provider,
                "llm_model": self.config.llm_model,
                "db_size_bytes": db_size_bytes,
            },
            "users": {
                "total": int(user_stats["total_users"] or 0) if user_stats else 0,
                "active": int(user_stats["active_users"] or 0) if user_stats else 0,
                "disabled": int(user_stats["disabled_users"] or 0) if user_stats else 0,
                "admins": int(user_stats["admin_users"] or 0) if user_stats else 0,
            },
            "content": {
                "raw_notes_total": int(content_stats["raw_notes_total"] or 0) if content_stats else 0,
                "active_memories": int(content_stats["active_memories"] or 0) if content_stats else 0,
                "pending_memories": int(content_stats["pending_memories"] or 0) if content_stats else 0,
                "open_tasks": int(content_stats["open_tasks"] or 0) if content_stats else 0,
                "done_tasks": int(content_stats["done_tasks"] or 0) if content_stats else 0,
            },
            "today_usage": {
                "notes_count": int(today_usage["notes_count"] or 0) if today_usage else 0,
                "blocked_noise_count": int(today_usage["blocked_noise_count"] or 0) if today_usage else 0,
                "llm_calls": int(today_usage["llm_calls"] or 0) if today_usage else 0,
                "embed_calls": int(today_usage["embed_calls"] or 0) if today_usage else 0,
                "tokens_used": int(today_usage["tokens_used"] or 0) if today_usage else 0,
            },
            "daily_series": daily_series,
            "settings": self.settings.get_all(),
            "audit_logs": audit_logs,
            "recent_traces": recent_traces,
        }

    def _enrich_user_rows(self, rows: list[Any], today: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in rows:
            u = AuthService._row_to_user(r)
            usage = self.guard.get_usage(f"space:{u.space_id}", usage_date=today)
            counts = self.db.query_one(
                """
                SELECT
                    (SELECT COUNT(*) FROM raw_notes WHERE space_id = ? AND is_forgotten = 0) AS notes_total,
                    (SELECT COUNT(*) FROM memories WHERE space_id = ? AND status = 'active') AS memories_total,
                    (SELECT COUNT(*) FROM memories WHERE space_id = ? AND status = 'active'
                        AND memory_type = 'task' AND json_extract(payload_json, '$.task_status') != 'done') AS open_tasks
                """,
                (u.space_id, u.space_id, u.space_id),
            )
            item = u.to_dict()
            item["today_usage"] = usage
            item["counts"] = {
                "notes_total": int(counts["notes_total"] or 0) if counts else 0,
                "memories_total": int(counts["memories_total"] or 0) if counts else 0,
                "open_tasks": int(counts["open_tasks"] or 0) if counts else 0,
            }
            out.append(item)
        return out

    def list_users(self, actor: UserAccount | None) -> list[dict[str, Any]]:
        self.require_admin(actor)
        today = today_utc_date()
        rows = self.db.query_all("SELECT * FROM users ORDER BY created_at ASC")
        return self._enrich_user_rows(rows, today)

    def list_users_page(
        self,
        actor: UserAccount | None,
        *,
        q: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.require_admin(actor)
        today = today_utc_date()
        lim = max(1, min(200, int(limit or 20)))
        off = max(0, int(offset or 0))
        clean_q = (q or "").strip()

        where_sql = ""
        params: list[Any] = []
        if clean_q:
            escaped = clean_q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pat = f"%{escaped}%"
            where_sql = (
                "WHERE (LOWER(username) LIKE ? ESCAPE '\\' "
                "OR LOWER(display_name) LIKE ? ESCAPE '\\' "
                "OR LOWER(space_id) LIKE ? ESCAPE '\\')"
            )
            params.extend([pat, pat, pat])

        cnt_row = self.db.query_one(
            f"SELECT COUNT(*) AS cnt FROM users {where_sql}",
            tuple(params),
        )
        total = int(cnt_row["cnt"] or 0) if cnt_row else 0
        rows = self.db.query_all(
            f"SELECT * FROM users {where_sql} ORDER BY created_at ASC LIMIT ? OFFSET ?",
            tuple(params) + (lim, off),
        )
        out = self._enrich_user_rows(rows, today)
        return {
            "users": out,
            "total": total,
            "limit": lim,
            "offset": off,
            "has_more": (off + len(out)) < total,
        }

    def create_user(
        self,
        actor: UserAccount | None,
        *,
        username: str,
        password: str,
        display_name: str = "",
        role: str = "user",
        daily_note_limit: int | None = None,
        daily_llm_limit: int | None = None,
        daily_token_limit: int | None = None,
    ) -> dict[str, Any]:
        admin = self.require_admin(actor)
        uname = self.auth._validate_credentials(username, password)
        if role not in ("admin", "user"):
            raise ValidationError("role 必须为 admin 或 user")
        if self.auth.get_user_by_username(uname) is not None:
            raise ValidationError(f"用户名 '{uname}' 已存在")

        cfg = self.settings.get_all()
        now = utc_now_iso()
        user_id = new_id("usr")
        space_id = f"u_{uuid.uuid4().hex[:10]}"

        def _parse_lim(raw: Any, default_val: int, min_val: int, name: str) -> int:
            if raw is None or raw == "":
                return max(min_val, int(default_val))
            if isinstance(raw, bool):
                raise ValidationError(f"{name} 必须为正整数")
            try:
                return max(min_val, int(raw))
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{name} 必须为正整数") from exc

        note_lim = _parse_lim(daily_note_limit, int(cfg["default_daily_note_limit"]), 1, "daily_note_limit")
        llm_lim = _parse_lim(daily_llm_limit, int(cfg["default_daily_llm_limit"]), 1, "daily_llm_limit")
        tok_lim = _parse_lim(daily_token_limit, int(cfg["default_daily_token_limit"]), 100, "daily_token_limit")

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id, username, display_name, password_hash, role, status,
                    space_id, daily_note_limit, daily_llm_limit, daily_token_limit,
                    created_at, updated_at, last_login_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, '')
                """,
                (
                    user_id,
                    uname,
                    display_name.strip()[:40] or uname,
                    hash_password(password),
                    role,
                    space_id,
                    note_lim,
                    llm_lim,
                    tok_lim,
                    now,
                    now,
                ),
            )
        self.guard.log_security_event(
            action="admin_create_user",
            status="ok",
            actor_user_id=admin.user_id,
            space_id=space_id,
            detail={"target_user_id": user_id, "username": uname, "role": role},
        )
        created = self.auth.get_user_by_id(user_id)
        assert created is not None
        return created.to_dict()

    def update_user(
        self,
        actor: UserAccount | None,
        target_user_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        admin = self.require_admin(actor)
        target = self.auth.get_user_by_id(target_user_id)
        if target is None:
            raise NotFoundError(f"用户 '{target_user_id}' 不存在")

        new_role = str(patch.get("role", target.role))
        new_status = str(patch.get("status", target.status))
        if new_role not in ("admin", "user"):
            raise ValidationError("role 必须为 admin 或 user")
        if new_status not in ("active", "disabled"):
            raise ValidationError("status 必须为 active 或 disabled")

        # Prevent removing/disabling the last active admin
        if target.role == "admin" and target.status == "active" and (new_role != "admin" or new_status != "active"):
            row = self.db.query_one(
                "SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin' AND status = 'active' AND user_id != ?",
                (target.user_id,),
            )
            if int(row["cnt"] or 0) == 0:
                raise ValidationError("无法禁用或降级最后一位活跃管理员")

        new_display = str(patch.get("display_name", target.display_name)).strip()[:40] or target.username

        def _parse_upd_lim(key: str, default_val: int, min_val: int) -> int:
            raw = patch.get(key, default_val)
            if raw is None or raw == "":
                return max(min_val, int(default_val))
            if isinstance(raw, bool):
                raise ValidationError(f"{key} 必须为正整数")
            try:
                return max(min_val, int(raw))
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{key} 必须为正整数") from exc

        new_note_lim = _parse_upd_lim("daily_note_limit", target.daily_note_limit, 1)
        new_llm_lim = _parse_upd_lim("daily_llm_limit", target.daily_llm_limit, 1)
        new_tok_lim = _parse_upd_lim("daily_token_limit", target.daily_token_limit, 100)
        new_pw = str(patch.get("new_password", "") or "")
        if new_pw and (len(new_pw) < 6 or len(new_pw) > 128):
            raise ValidationError("重置密码长度需在 6 到 128 位之间")

        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE users SET
                    display_name = ?,
                    role = ?,
                    status = ?,
                    daily_note_limit = ?,
                    daily_llm_limit = ?,
                    daily_token_limit = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    new_display,
                    new_role,
                    new_status,
                    new_note_lim,
                    new_llm_lim,
                    new_tok_lim,
                    now,
                    target.user_id,
                ),
            )
            if new_pw:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE user_id = ?",
                    (hash_password(new_pw), target.user_id),
                )
        if new_status == "disabled" or new_pw:
            self.auth.revoke_user_sessions(target.user_id)
        if new_status == "disabled":
            self.auth.revoke_supervision_share(target.space_id)

        self.guard.log_security_event(
            action="admin_update_user",
            status="ok",
            actor_user_id=admin.user_id,
            space_id=target.space_id,
            detail={
                "target_user_id": target.user_id,
                "role": new_role,
                "status": new_status,
                "password_reset": bool(new_pw),
            },
        )
        updated = self.auth.get_user_by_id(target.user_id)
        assert updated is not None
        return updated.to_dict()

    def update_settings(self, actor: UserAccount | None, patch: dict[str, Any]) -> dict[str, Any]:
        admin = self.require_admin(actor)
        updated = self.settings.update(patch)
        self.guard.log_security_event(
            action="admin_update_settings",
            status="ok",
            actor_user_id=admin.user_id,
            detail={k: v for k, v in patch.items() if k in SystemSettingsStore.EDITABLE_KEYS},
        )
        return updated

    def run_maintenance(self, actor: UserAccount | None) -> dict[str, Any]:
        admin = self.require_admin(actor)
        now_iso = utc_now_iso()
        with self.db.transaction() as conn:
            cur_sess = conn.execute(
                "DELETE FROM user_sessions WHERE revoked = 1 OR expires_at <= ?",
                (now_iso,),
            )
            pruned_sessions = int(cur_sess.rowcount or 0)
            cur_audit = conn.execute(
                """
                DELETE FROM security_audit_logs
                WHERE log_id NOT IN (
                    SELECT log_id FROM security_audit_logs ORDER BY created_at DESC LIMIT 2000
                )
                """
            )
            pruned_audit = int(cur_audit.rowcount or 0)
            cur_traces = conn.execute(
                """
                DELETE FROM trace_runs
                WHERE trace_id NOT IN (
                    SELECT trace_id FROM trace_runs ORDER BY created_at DESC LIMIT 2000
                )
                """
            )
            pruned_traces = int(cur_traces.rowcount or 0)
        conn = self.db._get_conn()
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.OperationalError:
            row = None
        size_bytes = int(self.config.sqlite_path.stat().st_size) if self.config.sqlite_path.exists() else 0
        self.guard.log_security_event(
            action="admin_db_maintenance",
            status="ok",
            actor_user_id=admin.user_id,
            detail={
                "db_size_bytes": size_bytes,
                "pruned_sessions": pruned_sessions,
                "pruned_audit_logs": pruned_audit,
                "pruned_traces": pruned_traces,
            },
        )
        return {
            "wal_checkpoint": list(row) if row else [],
            "db_size_bytes": size_bytes,
            "pruned_sessions": pruned_sessions,
            "pruned_audit_logs": pruned_audit,
            "pruned_traces": pruned_traces,
        }

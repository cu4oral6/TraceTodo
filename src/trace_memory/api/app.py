"""REST API Router, FastAPI adapter, and zero-dependency HTTP server for Trace-Memory Console."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from fastapi import FastAPI, Request, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.middleware.gzip import GZipMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover
    FastAPI = Any  # type: ignore[misc,assignment]
    Request = Any  # type: ignore[misc,assignment]
    Response = Any  # type: ignore[misc,assignment]
    CORSMiddleware = Any  # type: ignore[misc,assignment]
    GZipMiddleware = Any  # type: ignore[misc,assignment]
    HTMLResponse = Any  # type: ignore[misc,assignment]
    JSONResponse = Any  # type: ignore[misc,assignment]
    StreamingResponse = Any  # type: ignore[misc,assignment]
    _HAS_FASTAPI = False


PWA_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <image href="/icon.png?v=4" width="512" height="512"/>
</svg>"""

PWA_MANIFEST_JSON = json.dumps(
    {
        "name": "有迹 · 四象限记忆待办",
        "short_name": "有迹",
        "description": "AI 记忆驱动的艾森豪威尔四象限待办与长期记忆中心",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f2d37",
        "theme_color": "#143c4a",
        "icons": [
            {"src": "/icon.png?v=4", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icon-maskable.png?v=4", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            {"src": "/icon.svg?v=4", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        ],
    },
    ensure_ascii=False,
)

PWA_SW_JS = """const CACHE_NAME = 'trace-todo-shell-v5';
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE_NAME).then((c) => c.addAll(['/', '/home', '/manifest.webmanifest', '/icon.png?v=4', '/icon-maskable.png?v=4', '/icon.svg?v=4'])));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.pathname === '/' || url.pathname === '/home' || url.pathname === '/welcome' || url.pathname === '/official' || url.pathname === '/manifest.webmanifest' || url.pathname === '/icon.png' || url.pathname === '/icon-maskable.png' || url.pathname === '/apple-touch-icon.png' || url.pathname === '/icon.svg') {
    e.respondWith(fetch(e.request).then((res) => {
      const clone = res.clone();
      caches.open(CACHE_NAME).then((c) => c.put(e.request, clone));
      return res;
    }).catch(() => caches.match(e.request)));
  }
});
"""

from trace_memory.container import AppContainer
from trace_memory.domain.memories import MemoryEvidence
from trace_memory.domain.notes import RawNote, beijing_now, beijing_now_iso, new_id, utc_now_iso
from trace_memory.domain.proposals import (
    ChangeProposal,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitError,
    RelationSpec,
    TraceMemoryError,
    UnauthorizedError,
    ValidationError,
    VersionConflictError,
)
from trace_memory.domain.relations import extract_relations_from_markdown
from trace_memory.services.auth_service import UserAccount


DASHBOARD_HTML_PATH = Path(__file__).with_name("dashboard.html")
LANDING_HTML_PATH = Path(__file__).with_name("landing.html")
APP_ICON_PNG_PATH = Path(__file__).with_name("app_icon.png")
APP_ICON_MASKABLE_PNG_PATH = Path(__file__).with_name("app_icon_maskable.png")
SCENES_DIR = Path(__file__).with_name("scenes")
def _scene_file(filename: str) -> Path | None:
    """Resolve a scene image against the on-disk directory at request time.

    The dashboard HTML is hot-reloaded from disk, so a whitelist frozen at process
    start would 404 newly merged scenes until the server is restarted.
    """
    stem = filename[:-4] if filename.endswith(".jpg") else ""
    if not stem or not all(c.isascii() and (c.isalnum() or c == "_") for c in stem):
        return None
    path = SCENES_DIR / filename
    return path if path.is_file() else None


def _scene_etag(path: Path) -> str:
    st = path.stat()
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'

_DASHBOARD_CACHE: dict[str, Any] = {
    "key": None,
    "bytes": b"",
    "etag": "",
}
_LANDING_CACHE: dict[str, Any] = {
    "key": None,
    "bytes": b"",
    "etag": "",
}

_GZIP_SKIP_PREFIXES = ("/api/sync/stream", "/api/media/file", "/scenes/")
_GZIP_SKIP_EXACT = frozenset({"/icon.png", "/icon-maskable.png", "/apple-touch-icon.png", "/favicon.ico"})


def _load_html_cached(html_path: Path, cache: dict[str, Any]) -> tuple[bytes, str]:
    """Return `(utf8_bytes, weak_etag)` for `html_path`, cached by `(path, mtime_ns, size)`."""
    st = html_path.stat()
    cache_key = (str(html_path), st.st_mtime_ns, st.st_size)
    if cache["key"] == cache_key:
        return cache["bytes"], cache["etag"]
    raw_bytes = html_path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()[:16]
    etag = f'W/"{digest}"'
    cache["key"] = cache_key
    cache["bytes"] = raw_bytes
    cache["etag"] = etag
    return raw_bytes, etag


def _load_dashboard_html_cached() -> tuple[bytes, str]:
    """Return `(utf8_bytes, weak_etag)` for `DASHBOARD_HTML_PATH`, cached by `(path, mtime_ns, size)`."""
    return _load_html_cached(DASHBOARD_HTML_PATH, _DASHBOARD_CACHE)


def _load_landing_html_cached() -> tuple[bytes, str]:
    """Return `(utf8_bytes, weak_etag)` for `LANDING_HTML_PATH`, cached by `(path, mtime_ns, size)`."""
    return _load_html_cached(LANDING_HTML_PATH, _LANDING_CACHE)


def _normalize_etag_token(token: str) -> str:
    t = token.strip()
    if t.startswith(("W/", "w/")):
        t = t[2:].strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1]
    if t.endswith("-gzip"):
        t = t[:-5]
    return t


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Return True when an `If-None-Match` header matches `etag` (weak comparison per RFC 9110)."""
    if not if_none_match:
        return False
    raw = if_none_match.strip()
    if not raw:
        return False
    if raw == "*":
        return True
    target = _normalize_etag_token(etag)
    if not target:
        return False
    for candidate in raw.split(","):
        if _normalize_etag_token(candidate) == target:
            return True
    return False


def _extract_token(
    query_params: dict[str, list[str]],
    headers: dict[str, str] | None = None,
) -> str:
    """Extract Bearer/X-Auth-Token strictly from HTTP headers (never URL query strings)."""
    _ = query_params
    if headers:
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
        auth = lowered.get("authorization", "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        x_tok = lowered.get("x-auth-token", "").strip()
        if x_tok:
            return x_tok
    return ""


def _resolve_client_ip(
    headers: dict[str, str] | None,
    direct_ip: str,
    trust_proxy_headers: bool,
) -> str:
    """Only trust X-Forwarded-For / X-Real-IP when explicitly behind a trusted reverse proxy."""
    fallback = (direct_ip or "127.0.0.1").strip() or "127.0.0.1"
    if not trust_proxy_headers or not headers:
        return fallback
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    xff = lowered.get("x-forwarded-for", "").strip()
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    x_real = lowered.get("x-real-ip", "").strip()
    if x_real:
        return x_real
    return fallback


def _safe_int(
    raw: Any,
    *,
    name: str,
    default: int | None = None,
    min_val: int | None = None,
    max_val: int | None = None,
    strict: bool = False,
) -> int | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        raise ValidationError(f"参数 {name} 必须为整数")
    try:
        val = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"参数 {name} 必须为整数") from exc
    if strict:
        if min_val is not None and val < min_val:
            raise ValidationError(f"参数 {name} 不能小于 {min_val}")
        if max_val is not None and val > max_val:
            raise ValidationError(f"参数 {name} 不能大于 {max_val}")
        return val
    if min_val is not None and val < min_val:
        val = min_val
    if max_val is not None and val > max_val:
        val = max_val
    return val


def _safe_float(
    raw: Any,
    *,
    name: str,
    default: float,
    min_val: float | None = None,
    max_val: float | None = None,
) -> float:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        raise ValidationError(f"参数 {name} 必须为数值")
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"参数 {name} 必须为数值") from exc
    if not math.isfinite(val):
        raise ValidationError(f"参数 {name} 必须为有限数值")
    if min_val is not None and val < min_val:
        val = min_val
    if max_val is not None and val > max_val:
        val = max_val
    return val


def _safe_bool(raw: Any, *, default: bool = True) -> bool:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


class ApiDispatcher:
    """Framework-neutral JSON API controller shared by FastAPI and built-in HTTP server."""

    _PUBLIC_PATHS = frozenset(
        {
            "/api/health",
            "/api/auth/status",
            "/api/auth/captcha",
            "/api/auth/login",
            "/api/auth/register",
            "/api/bootstrap",
        }
    )

    def __init__(self, container: AppContainer) -> None:
        self.container = container

    def _requested_space(self, query_params: dict[str, list[str]], body: dict[str, Any] | None = None) -> str:
        if body and body.get("space_id"):
            return str(body["space_id"]).strip()
        vals = query_params.get("space_id")
        if vals and vals[0].strip():
            return vals[0].strip()
        return ""

    def _space(self, query_params: dict[str, list[str]], body: dict[str, Any] | None = None) -> str:
        return self._requested_space(query_params, body) or self.container.config.default_space

    def _resolve_auth_and_space(
        self,
        path: str,
        qp: dict[str, list[str]],
        payload: dict[str, Any],
        token: str,
    ) -> tuple[UserAccount | None, str, dict[str, Any] | None]:
        req_space = self._requested_space(qp, payload)
        share_token = str(payload.get("share_token") or (qp.get("share_token") or [""])[0] or "").strip()

        if path in ("/api/supervision/board", "/api/supervision/nudge") and share_token:
            share_info = self.container.auth_service.resolve_supervision_share(share_token)
            if share_info is None:
                raise UnauthorizedError("好友监督分享链接无效或已失效")
            return None, share_info["space_id"], share_info

        user: UserAccount | None = None
        if token:
            user = self.container.auth_service.authenticate(token)
            if user is None and path not in self._PUBLIC_PATHS:
                raise UnauthorizedError("登录会话已过期或失效，请重新登录")

        must_auth = (
            self.container.config.auth_required
            or path.startswith("/api/admin/")
            or path
            in (
                "/api/auth/me",
                "/api/auth/logout",
                "/api/auth/change-password",
                "/api/supervision/share",
                "/api/supervision/shares",
                "/api/supervision/share/revoke",
            )
        )
        if must_auth and path not in self._PUBLIC_PATHS and user is None:
            raise UnauthorizedError("请先登录账号")

        if user is not None:
            if user.role == "admin":
                eff_space = req_space or user.space_id
            else:
                if path == "/api/bootstrap":
                    eff_space = user.space_id
                elif req_space and req_space != user.space_id:
                    raise PermissionDeniedError("无权访问其他用户的记忆空间")
                else:
                    eff_space = user.space_id
            return user, eff_space, None

        return None, req_space or self.container.config.default_space, None

    def _check_rate_limit(
        self,
        *,
        user: UserAccount | None,
        space_id: str,
        client_ip: str,
    ) -> None:
        bucket = f"usr:{user.user_id}" if user else f"ip:{client_ip}:sp:{space_id}"
        self.container.quota_service.check_rate_limit(bucket, action_label="操作请求")

    def _check_daily_quota(
        self,
        *,
        user: UserAccount | None,
        space_id: str,
        require_note_slot: bool = False,
        require_llm_slot: bool = False,
    ) -> None:
        if not require_note_slot and not require_llm_slot:
            return
        note_lim = user.daily_note_limit if user else None
        llm_lim = user.daily_llm_limit if user else None
        tok_lim = user.daily_token_limit if user else None
        self.container.quota_service.check_daily_quota(
            f"space:{space_id}",
            note_limit=note_lim,
            llm_limit=llm_lim,
            token_limit=tok_lim,
            require_note_slot=require_note_slot,
            require_llm_slot=require_llm_slot,
        )

    def _enforce_rate_and_quota(
        self,
        *,
        user: UserAccount | None,
        space_id: str,
        client_ip: str,
        require_note_slot: bool = False,
        require_llm_slot: bool = False,
    ) -> None:
        self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
        self._check_daily_quota(
            user=user,
            space_id=space_id,
            require_note_slot=require_note_slot,
            require_llm_slot=require_llm_slot,
        )

    def resolve_media_file(
        self,
        query_params: dict[str, list[str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, bytes]:
        """Serve raw binary image/thumbnail with Bearer auth OR HMAC-signed URL verification."""
        qp = query_params or {}
        media_id = (qp.get("id") or [""])[0].strip()
        req_space = (qp.get("space_id") or [""])[0].strip() or self.container.config.default_space
        sig = (qp.get("sig") or [""])[0].strip()
        is_thumb = (qp.get("thumb") or [""])[0].strip().lower() in ("1", "true", "yes")

        if not media_id:
            return 400, "application/json; charset=utf-8", json.dumps({"error": "id is required"}).encode("utf-8")

        try:
            if sig and self.container.media_service.verify_media_sig(req_space, media_id, sig):
                eff_space = req_space
            else:
                token = _extract_token(qp, headers)
                _, eff_space, _ = self._resolve_auth_and_space("/api/media/file", qp, {}, token)
            img_bytes, mime = self.container.media_service.read_binary_file(
                eff_space, media_id, thumb=is_thumb
            )
            return 200, mime, img_bytes
        except UnauthorizedError as exc:
            return 401, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode("utf-8")
        except PermissionDeniedError as exc:
            return 403, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode("utf-8")
        except NotFoundError as exc:
            return 404, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode("utf-8")

    def _build_health_payload(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg_settings = settings if settings is not None else self.container.settings_store.get_all()
        return {
            "status": "ok",
            "default_space": self.container.config.default_space,
            "vector_backend": self.container.vector_store.backend_name,
            "llm_provider": self.container.llm.provider_name,
            "vision_model": self.container.config.vision_model,
            "auth_required": bool(self.container.config.auth_required),
            "allow_registration": bool(cfg_settings["allow_registration"]),
            "register_captcha_required": bool(self.container.config.register_captcha_required),
            "has_users": self.container.auth_service.has_users(),
            "noise_filter_enabled": bool(cfg_settings["noise_filter_enabled"]),
            "timezone": "Asia/Shanghai (UTC+8)",
            "server_time": beijing_now_iso(),
            "async_ingest_ready": self.container.ingest_worker.is_running,
            "daily_scheduler_ready": self.container.daily_scheduler.is_running,
        }

    def _build_auth_status_payload(
        self,
        user: UserAccount | None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg_settings = settings if settings is not None else self.container.settings_store.get_all()
        quota = (
            self.container.quota_service.get_usage(f"space:{user.space_id}")
            if user is not None
            else None
        )
        return {
            "auth_required": bool(self.container.config.auth_required),
            "allow_registration": bool(cfg_settings["allow_registration"]),
            "register_captcha_required": bool(self.container.config.register_captcha_required),
            "has_users": self.container.auth_service.has_users(),
            "authenticated": user is not None,
            "user": user.to_dict() if user is not None else None,
            "quota": quota,
            "defense": {
                "noise_filter_enabled": bool(cfg_settings["noise_filter_enabled"]),
                "max_note_chars": int(cfg_settings["max_note_chars"]),
                "rate_limit_per_minute": int(cfg_settings["rate_limit_per_minute"]),
            },
        }

    def _build_sync_state_payload(
        self, space_id: str, *, include_recent_jobs: bool = False
    ) -> dict[str, Any]:
        st = self.container.sync_tracker.get_state(space_id)
        ing = self.container.job_repo.get_space_status(space_id)
        st["pending_ingest"] = int(ing.get("pending", 0))
        st["last_completed_at"] = str(ing.get("last_completed_at", ""))
        if include_recent_jobs:
            st["recent_jobs"] = ing.get("recent_jobs", [])
        return st

    def dispatch(
        self,
        method: str,
        path: str,
        query_params: dict[str, list[str]] | None = None,
        body: dict[str, Any] | None = None,
        auth_token: str = "",
        headers: dict[str, str] | None = None,
        client_ip: str = "127.0.0.1",
    ) -> tuple[int, dict[str, Any]]:
        qp = query_params or {}
        payload = body or {}
        eff_token = auth_token.strip() or _extract_token(qp, headers)

        try:
            user, space_id, share_info = self._resolve_auth_and_space(path, qp, payload, eff_token)

            if method == "GET" and path == "/api/bootstrap":
                settings = self.container.settings_store.get_all()
                can_read_space = (user is not None) or (not self.container.config.auth_required)
                return 200, {
                    "health": self._build_health_payload(settings=settings),
                    "auth": self._build_auth_status_payload(user, settings=settings),
                    "preferences": (
                        self.container.preferences_service.get_preferences(space_id)
                        if can_read_space
                        else None
                    ),
                    "sync_state": (
                        self._build_sync_state_payload(space_id)
                        if can_read_space
                        else None
                    ),
                }

            if method == "GET" and path == "/api/health":
                return 200, self._build_health_payload()

            if method == "GET" and path == "/api/preferences":
                return 200, self.container.preferences_service.get_preferences(space_id)

            if method == "POST" and path == "/api/preferences":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                updated_prefs = self.container.preferences_service.update_preferences(space_id, payload)
                return 200, updated_prefs

            if method == "GET" and path == "/api/sync/state":
                return 200, self._build_sync_state_payload(space_id)

            if method == "GET" and path == "/api/auth/status":
                return 200, self._build_auth_status_payload(user)

            if method == "GET" and path == "/api/auth/captcha":
                return 200, self.container.auth_service.issue_captcha(client_ip=client_ip)

            if method == "POST" and path == "/api/auth/register":
                out = self.container.auth_service.register(
                    username=str(payload.get("username", "")),
                    password=str(payload.get("password", "")),
                    display_name=str(payload.get("display_name", "")),
                    device_label=str(payload.get("device_label", "web")),
                    client_ip=client_ip,
                    captcha_id=str(payload.get("captcha_id", "")),
                    captcha_code=str(payload.get("captcha_code", "")),
                )
                return 200, out

            if method == "POST" and path == "/api/auth/login":
                out = self.container.auth_service.login(
                    username=str(payload.get("username", "")),
                    password=str(payload.get("password", "")),
                    device_label=str(payload.get("device_label", "web")),
                    client_ip=client_ip,
                )
                return 200, out

            if method == "POST" and path == "/api/auth/logout":
                revoked = self.container.auth_service.logout(eff_token)
                return 200, {"logged_out": revoked}

            if method == "GET" and path == "/api/auth/me":
                assert user is not None
                return 200, {
                    "user": user.to_dict(),
                    "quota": self.container.quota_service.get_usage(f"space:{user.space_id}"),
                }

            if method == "POST" and path == "/api/auth/change-password":
                assert user is not None
                out = self.container.auth_service.change_password(
                    user,
                    old_password=str(payload.get("old_password", "")),
                    new_password=str(payload.get("new_password", "")),
                    device_label=str(payload.get("device_label", "web")),
                    client_ip=client_ip,
                )
                return 200, out

            if method == "POST" and path == "/api/supervision/share":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                owner_id = user.user_id if user else ""
                share = self.container.auth_service.create_supervision_share(
                    space_id=space_id,
                    owner_user_id=owner_id,
                    label=str(payload.get("label", "好友监督")),
                )
                share["share_query"] = f"?tab=supervision&share_token={share['share_token']}"
                return 200, share

            if method == "GET" and path == "/api/supervision/shares":
                shares = self.container.auth_service.list_supervision_shares(space_id=space_id)
                return 200, {"shares": shares}

            if method == "POST" and path == "/api/supervision/share/revoke":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                revoked_cnt = self.container.auth_service.revoke_supervision_share(
                    space_id=space_id,
                    share_id=str(payload.get("share_id", "")),
                )
                return 200, {"revoked": revoked_cnt}

            # Admin Management Endpoints (strictly requires role='admin')
            if method == "GET" and path == "/api/admin/overview":
                return 200, self.container.admin_service.get_overview(user)

            if method == "GET" and path == "/api/admin/users":
                q_str = qp.get("q", [""])[0].strip()
                lim = _safe_int(qp.get("limit", ["50"])[0], name="limit", default=50, min_val=1, max_val=200)
                off = _safe_int(qp.get("offset", ["0"])[0], name="offset", default=0, min_val=0, max_val=100000)
                return 200, self.container.admin_service.list_users_page(
                    user, q=q_str, limit=int(lim or 50), offset=int(off or 0)
                )

            if method == "POST" and path == "/api/admin/users/create":
                created = self.container.admin_service.create_user(
                    user,
                    username=str(payload.get("username", "")),
                    password=str(payload.get("password", "")),
                    display_name=str(payload.get("display_name", "")),
                    role=str(payload.get("role", "user")),
                    daily_note_limit=payload.get("daily_note_limit"),
                    daily_llm_limit=payload.get("daily_llm_limit"),
                    daily_token_limit=payload.get("daily_token_limit"),
                )
                return 200, {"user": created}

            if method == "POST" and path == "/api/admin/users/update":
                updated = self.container.admin_service.update_user(
                    user,
                    target_user_id=str(payload.get("user_id", "")),
                    patch=payload,
                )
                return 200, {"user": updated}

            if method == "GET" and path == "/api/admin/settings":
                self.container.admin_service.require_admin(user)
                return 200, {"settings": self.container.settings_store.get_all()}

            if method == "POST" and path == "/api/admin/settings":
                updated_settings = self.container.admin_service.update_settings(user, payload)
                return 200, {"settings": updated_settings}

            if method == "POST" and path == "/api/admin/maintenance":
                return 200, self.container.admin_service.run_maintenance(user)

            if method == "GET" and path == "/api/tasks/matrix":
                inc_done = qp.get("include_done", ["false"])[0].lower() == "true"
                sort_by = qp.get("sort_by", ["priority"])[0]
                matrix = self.container.task_matrix_service.build_matrix_analysis(
                    space_id=space_id, include_done=inc_done
                )
                if sort_by != "priority":
                    matrix["sorted_tasks"] = self.container.task_matrix_service.list_sorted_tasks(
                        space_id=space_id,
                        task_status="all" if inc_done else "todo",
                        sort_by=sort_by,
                    )
                return 200, matrix

            if method == "POST" and path == "/api/tasks/transition":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                mem_id = str(payload.get("memory_id", ""))
                action = str(payload.get("action", "complete_task"))
                exp_ver = _safe_int(payload.get("expected_version"), name="expected_version", min_val=1)
                undo_next_id = str(payload.get("undo_next_task_id", "") or "")
                out = self.container.task_matrix_service.transition_task_state_with_repeat(
                    space_id=space_id,
                    memory_id=mem_id,
                    action=action,
                    expected_version=exp_ver,
                    undo_next_task_id=undo_next_id,
                )
                return 200, out

            if method == "POST" and path == "/api/tasks/quadrant":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                mem_id = str(payload.get("memory_id", ""))
                exp_ver = _safe_int(payload.get("expected_version"), name="expected_version", min_val=1)
                imp_v = _safe_int(payload.get("importance"), name="importance", min_val=1, max_val=5)
                urg_v = _safe_int(payload.get("urgency"), name="urgency", min_val=1, max_val=5)
                res = self.container.task_matrix_service.update_task_quadrant_or_priority(
                    space_id=space_id,
                    memory_id=mem_id,
                    expected_version=exp_ver,
                    importance=imp_v,
                    urgency=urg_v,
                    quadrant=payload.get("quadrant"),
                    due_at=payload.get("due_at"),
                )
                return 200, res.to_dict()

            if method == "POST" and path == "/api/tasks/save":
                self._enforce_rate_and_quota(
                    user=user, space_id=space_id, client_ip=client_ip, require_note_slot=True
                )
                res = self.container.task_matrix_service.save_task_card(
                    space_id=space_id,
                    data=payload,
                    actor_id=user.username if user else "user",
                )
                if res.status != "idempotent_replay":
                    self.container.quota_service.record_usage(
                        f"space:{space_id}", notes=1, embed_calls=2
                    )
                return 200, res.to_dict()

            if method == "POST" and path == "/api/tasks/decompose":
                self._enforce_rate_and_quota(
                    user=user, space_id=space_id, client_ip=client_ip, require_llm_slot=True
                )
                title_str = str(payload.get("title", "")).strip()
                summary_str = str(payload.get("summary", "")).strip()
                guard_dec = self.container.input_guard.inspect_note(title_str)
                if not guard_dec.allowed:
                    raise ValidationError(guard_dec.reason)
                out = self.container.task_matrix_service.decompose_task_with_memory(
                    space_id=space_id,
                    title=title_str,
                    summary=summary_str,
                    memory_id=str(payload.get("memory_id", "")),
                )
                self.container.quota_service.record_usage(
                    f"space:{space_id}",
                    llm_calls=int(out.get("llm_calls", 0) or 0),
                    tokens_used=int(out.get("tokens_used", 0) or 0),
                )
                return 200, out

            if method == "POST" and path == "/api/tasks/subtasks/toggle":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                exp_ver = _safe_int(payload.get("expected_version"), name="expected_version", min_val=1)
                out = self.container.task_matrix_service.toggle_subtask(
                    space_id=space_id,
                    memory_id=str(payload.get("memory_id", "")),
                    subtask_id=str(payload.get("subtask_id", "")),
                    done=bool(payload.get("done", False)),
                    expected_version=exp_ver,
                )
                return 200, out

            if method == "POST" and path == "/api/tasks/rollover":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                rolled = self.container.task_matrix_service.apply_auto_rollover(space_id=space_id)
                return 200, {"rolled_over": rolled, "count": len(rolled)}

            if method == "POST" and path == "/api/tasks/refresh-status":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                report = self.container.daily_scheduler.run_once(space_id=space_id)
                sync_state = self.container.sync_tracker.get_state(space_id)
                report["sync_version"] = sync_state["sync_version"]
                return 200, report

            if method == "POST" and path == "/api/tasks/focus":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                exp_ver = _safe_int(payload.get("expected_version"), name="expected_version", min_val=1)
                elapsed_s = _safe_int(
                    payload.get("elapsed_seconds", 0), name="elapsed_seconds", default=0, min_val=0, max_val=86400
                )
                ev_str = str(payload.get("event", "complete_session"))
                reason_str = str(payload.get("reason", ""))[:200]
                res = self.container.task_matrix_service.record_focus_event(
                    space_id=space_id,
                    memory_id=str(payload.get("memory_id", "")),
                    event=ev_str,
                    reason=reason_str,
                    elapsed_seconds=int(elapsed_s or 0),
                    expected_version=exp_ver,
                )
                if res.memory is not None:
                    plan_m = int(res.memory.payload.get("focus_minutes", 25) or 25)
                    self.container.focus_service.insert_session_row(
                        space_id=space_id,
                        task_memory_id=res.memory.memory_id,
                        task_title=res.memory.title,
                        mode=str(payload.get("mode", "pomodoro")),
                        planned_minutes=plan_m,
                        duration_seconds=int(elapsed_s or 0) or (plan_m * 60),
                        status="completed" if ev_str == "complete_session" else "aborted",
                        abort_reason=reason_str,
                        actor_id=user.username if user else "user",
                    )
                return 200, res.to_dict()

            if method == "GET" and path == "/api/focus/overview":
                lim = _safe_int(qp.get("limit", ["50"])[0], name="limit", default=50, min_val=1, max_val=200)
                off = _safe_int(qp.get("offset", ["0"])[0], name="offset", default=0, min_val=0, max_val=100000)
                date_q = qp.get("date", [""])[0].strip()
                return 200, self.container.focus_service.get_overview(
                    space_id=space_id, limit=int(lim or 50), offset=int(off or 0), focus_date=date_q
                )

            if method == "POST" and path == "/api/focus/record":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                exp_ver = _safe_int(payload.get("expected_version"), name="expected_version", min_val=1)
                plan_min = _safe_int(payload.get("planned_minutes", 25), name="planned_minutes", default=25, min_val=1, max_val=240)
                dur_sec = _safe_int(payload.get("duration_seconds", 0), name="duration_seconds", default=0, min_val=0, max_val=86400)
                out_focus = self.container.focus_service.record_session(
                    space_id=space_id,
                    mode=str(payload.get("mode", "pomodoro")),
                    status=str(payload.get("status", "completed")),
                    planned_minutes=int(plan_min or 25),
                    duration_seconds=int(dur_sec or 0),
                    task_memory_id=str(payload.get("task_memory_id", "")),
                    task_title=str(payload.get("task_title", "")),
                    abort_reason=str(payload.get("abort_reason", "")),
                    note=str(payload.get("note", "")),
                    started_at=str(payload.get("started_at", "")),
                    ended_at=str(payload.get("ended_at", "")),
                    expected_version=exp_ver,
                    complete_task=bool(payload.get("complete_task", False)),
                    actor_id=user.username if user else "user",
                )
                return 200, out_focus

            if method == "POST" and path == "/api/focus/delete":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                deleted = self.container.focus_service.delete_session(
                    space_id=space_id,
                    session_id=str(payload.get("session_id", "")),
                )
                return 200, {"deleted": deleted}

            if method == "GET" and path == "/api/calendar/overview":
                cal_view = qp.get("view", ["week"])[0].strip()
                anchor_d = qp.get("anchor_date", [""])[0].strip()
                inc_done = qp.get("include_done", ["true"])[0].strip().lower() not in ("false", "0", "no")
                return 200, self.container.calendar_service.get_overview(
                    space_id=space_id,
                    view=cal_view,
                    anchor_date=anchor_d,
                    include_done=inc_done,
                )

            if method == "POST" and path == "/api/calendar/schedule":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                out_sched = self.container.calendar_service.schedule_slot(
                    space_id=space_id,
                    payload=payload,
                    actor_id=user.username if user else "user",
                )
                return 200, out_sched

            if method == "GET" and path == "/api/calendar/holidays/status":
                return 200, self.container.holiday_manager.get_status()

            if method == "POST" and path == "/api/calendar/holidays/sync":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                sync_report = self.container.holiday_manager.sync_remote_years(force=True)
                if sync_report.get("updated"):
                    self.container.sync_tracker.bump(space_id, reason="holiday_sync")
                return 200, sync_report

            if method == "GET" and path == "/api/countdowns/list":
                return 200, self.container.countdown_service.list_countdowns(space_id=space_id)

            if method == "POST" and path == "/api/countdowns/save":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                saved_cd = self.container.countdown_service.save_countdown(space_id=space_id, data=payload)
                return 200, saved_cd

            if method == "POST" and path == "/api/countdowns/delete":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                del_ok = self.container.countdown_service.delete_countdown(
                    space_id=space_id,
                    countdown_id=str(payload.get("countdown_id", "")),
                )
                return 200, {"deleted": del_ok}

            if method == "GET" and path == "/api/memos/list":
                lim = _safe_int(qp.get("limit", ["80"])[0], name="limit", default=80, min_val=1, max_val=200)
                off = _safe_int(qp.get("offset", ["0"])[0], name="offset", default=0, min_val=0, max_val=100000)
                q_str = qp.get("q", [""])[0].strip()
                tag_str = qp.get("tag", [""])[0].strip()
                pin_only = qp.get("pinned_only", ["false"])[0].strip().lower() in ("true", "1", "yes")
                return 200, self.container.memo_journal_service.list_memos(
                    space_id=space_id,
                    q=q_str,
                    tag=tag_str,
                    pinned_only=pin_only,
                    limit=int(lim or 80),
                    offset=int(off or 0),
                )

            if method == "POST" and path == "/api/memos/save":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                actor_name = user.username if user else "user"
                return 200, self.container.memo_journal_service.save_memo(
                    space_id=space_id,
                    payload=payload,
                    actor_id=actor_name,
                )

            if method == "POST" and path == "/api/memos/pin":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                pinned_memo = self.container.memo_journal_service.toggle_pin(
                    space_id=space_id,
                    note_id=str(payload.get("note_id", "")),
                    pinned=bool(payload.get("pinned", True)),
                )
                return 200, {"memo": pinned_memo}

            if method == "GET" and path == "/api/journal/overview":
                j_date = qp.get("date", [""])[0].strip()
                j_lim = _safe_int(qp.get("limit", ["30"])[0], name="limit", default=30, min_val=1, max_val=120)
                j_off = _safe_int(qp.get("offset", ["0"])[0], name="offset", default=0, min_val=0, max_val=100000)
                return 200, self.container.memo_journal_service.get_journal_overview(
                    space_id=space_id,
                    date_str=j_date,
                    limit=int(j_lim or 30),
                    offset=int(j_off or 0),
                )

            if method == "POST" and path == "/api/journal/save":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                actor_name = user.username if user else "user"
                return 200, self.container.memo_journal_service.save_journal(
                    space_id=space_id,
                    payload=payload,
                    actor_id=actor_name,
                )

            if method == "POST" and path == "/api/journal/ai-summary":
                self._enforce_rate_and_quota(user=user, space_id=space_id, client_ip=client_ip)
                return 200, self.container.memo_journal_service.generate_ai_daily_summary(
                    space_id=space_id,
                    date_str=str(payload.get("date") or payload.get("journal_date") or ""),
                    draft_content=str(payload.get("draft_content", "")),
                    mood=str(payload.get("mood", "")),
                    trigger_dream=bool(payload.get("trigger_dream", False)),
                )

            if method == "GET" and path == "/api/supervision/board":
                assignee = qp.get("assignee", [""])[0]
                board = self.container.task_matrix_service.build_supervision_board(
                    space_id=space_id, assignee_filter=assignee
                )
                if share_info is not None:
                    board["shared_mode"] = True
                    board["share_label"] = share_info.get("label", "好友监督")
                return 200, board

            if method == "POST" and path == "/api/supervision/nudge":
                self._enforce_rate_and_quota(user=user, space_id=space_id, client_ip=client_ip)
                res = self.container.task_matrix_service.add_supervision_nudge(
                    space_id=space_id,
                    memory_id=str(payload.get("memory_id", "")),
                    kind=str(payload.get("kind", "cheer")),
                    from_user=str(payload.get("from_user", "监工好友"))[:32],
                    message=str(payload.get("message", ""))[:120],
                )
                return 200, res.to_dict()

            if method == "POST" and path == "/api/voice/transcribe":
                self._enforce_rate_and_quota(
                    user=user, space_id=space_id, client_ip=client_ip, require_llm_slot=True
                )
                tr_out = self.container.voice_service.transcribe_short_audio(
                    audio_base64=str(payload.get("audio_base64", "")),
                    mime_type=str(payload.get("mime_type", "audio/webm")),
                    language=str(payload.get("language", "zh-CN")),
                    hint_text=str(payload.get("hint_text", "")),
                )
                if int(tr_out.get("tokens_used", 0) or 0) > 0:
                    self.container.quota_service.record_usage(
                        f"space:{space_id}", llm_calls=1, tokens_used=int(tr_out["tokens_used"])
                    )
                if bool(payload.get("auto_ingest", False)) and tr_out.get("text"):
                    actor_name = user.username if user else "user"
                    if self.container.ingest_worker.is_running:
                        ing_res = self.container.ingest_service.enqueue_note(
                            space_id=space_id,
                            content=str(tr_out["text"]),
                            source_type="web",
                            actor_id=actor_name,
                        )
                        if ing_res.extraction_status == "queued":
                            self.container.ingest_worker.notify()
                    else:
                        ing_res = self.container.ingest_service.record_note(
                            space_id=space_id,
                            content=str(tr_out["text"]),
                            source_type="web",
                            actor_id=actor_name,
                        )
                    tr_out["ingest"] = ing_res.to_dict()
                return 200, tr_out

            if method == "POST" and path == "/api/media/upload":
                self._enforce_rate_and_quota(
                    user=user, space_id=space_id, client_ip=client_ip, require_note_slot=False
                )
                w = _safe_int(payload.get("width", 0), name="width", default=0, min_val=0, max_val=20000)
                h = _safe_int(payload.get("height", 0), name="height", default=0, min_val=0, max_val=20000)
                media_obj = self.container.media_service.upload_image(
                    space_id=space_id,
                    image_base64=str(payload.get("image_base64", "")),
                    thumb_base64=str(payload.get("thumb_base64", "")),
                    width=int(w or 0),
                    height=int(h or 0),
                    note_id=str(payload.get("note_id", "")),
                    vision_hint=str(payload.get("vision_hint", "")),
                    analyze_immediately=bool(payload.get("analyze_immediately", False)),
                )
                return 200, {"media": media_obj, "used_bytes": self.container.media_service.get_space_used_bytes(space_id)}

            if method == "GET" and path == "/api/media/list":
                lim = _safe_int(qp.get("limit", ["60"])[0], name="limit", default=60, min_val=1, max_val=200)
                items = self.container.media_service.list_for_space(space_id, limit=int(lim or 60))
                return 200, {
                    "space_id": space_id,
                    "count": len(items),
                    "used_bytes": self.container.media_service.get_space_used_bytes(space_id),
                    "quota_bytes": int(self.container.config.space_media_quota_mb) * 1024 * 1024,
                    "media": items,
                }

            if method == "POST" and path == "/api/media/delete":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                deleted = self.container.media_service.delete_media(
                    space_id=space_id, media_id=str(payload.get("media_id", ""))
                )
                return 200, {"deleted": deleted}

            if method == "POST" and path == "/api/notes/record":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                content = str(payload.get("content", "")).strip()
                raw_media_ids = payload.get("media_ids") or []
                media_ids = [
                    str(m).strip()
                    for m in (raw_media_ids if isinstance(raw_media_ids, list) else [])
                    if str(m).strip()
                ][:12]
                if not content and not media_ids:
                    raise ValidationError("content is required")
                idem = str(payload.get("idempotency_key", "")).strip()
                actor_name = user.username if user else "user"

                # Fast length & noise check BEFORE consuming note/LLM quota (skip noise filter when images attached)
                if media_ids:
                    guard_allowed = True
                    if len(content) > int(self.container.settings_store.get_all().get("max_note_chars", 2000)):
                        raise ValidationError("单条记录字数超出上限")
                else:
                    guard_res = self.container.input_guard.inspect_note(content)
                    guard_allowed = guard_res.allowed
                    if guard_allowed and not idem:
                        is_dup, dup_reason = self.container.quota_service.check_duplicate_note(space_id, content)
                        if is_dup:
                            self.container.quota_service.record_usage(f"space:{space_id}", blocked_noise=1)
                            self.container.quota_service.log_security_event(
                                action="ingest_duplicate_blocked",
                                status="duplicate",
                                actor_user_id=user.user_id if user else actor_name,
                                space_id=space_id,
                                detail={"preview": content[:40]},
                                client_ip=client_ip,
                            )
                            return 200, {
                                "note": RawNote(
                                    note_id="filtered_duplicate",
                                    space_id=space_id,
                                    content=content,
                                    source_type="web",
                                    actor_id=actor_name,
                                ).to_dict(),
                                "extraction_status": "filtered_noise",
                                "filtered": True,
                                "filter_reason": dup_reason,
                                "changes": [],
                                "error_message": dup_reason,
                                "trace_id": "",
                            }

                if guard_allowed:
                    self._check_daily_quota(
                        user=user,
                        space_id=space_id,
                        require_note_slot=True,
                        require_llm_slot=True,
                    )

                sync_flag = qp.get("sync", [""])[0].strip().lower() in ("1", "true", "yes")
                async_q = qp.get("async", [""])[0].strip().lower() in ("1", "true", "yes")
                async_param = payload.get("async_mode")
                if sync_flag or async_param is False:
                    use_async = False
                elif async_param is True or async_q:
                    use_async = True
                else:
                    use_async = self.container.ingest_worker.is_running

                if use_async:
                    if not self.container.ingest_worker.is_running:
                        self.container.ingest_worker.start()
                    res_ing = self.container.ingest_service.enqueue_note(
                        space_id=space_id,
                        content=content,
                        source_type="web",
                        actor_id=actor_name,
                        event_time=str(payload.get("event_time", "")),
                        idempotency_key=idem,
                        media_ids=media_ids,
                    )
                    out_dict = res_ing.to_dict()
                    if media_ids:
                        out_dict["media"] = self.container.media_service.list_for_notes(
                            space_id, [res_ing.note.note_id]
                        ).get(res_ing.note.note_id, [])
                    if res_ing.extraction_status == "queued":
                        self.container.ingest_worker.notify()
                        return 202, out_dict
                    return 200, out_dict

                res_ing = self.container.ingest_service.record_note(
                    space_id=space_id,
                    content=content,
                    source_type="web",
                    actor_id=actor_name,
                    event_time=str(payload.get("event_time", "")),
                    idempotency_key=idem,
                    media_ids=media_ids,
                )
                out_dict = res_ing.to_dict()
                if media_ids:
                    out_dict["media"] = self.container.media_service.list_for_notes(
                        space_id, [res_ing.note.note_id]
                    ).get(res_ing.note.note_id, [])
                return 200, out_dict

            if method == "GET" and path == "/api/notes/ingest-status":
                wait_ms = int(
                    _safe_int(
                        qp.get("wait_ms", ["0"])[0],
                        name="wait_ms",
                        default=0,
                        min_val=0,
                        max_val=12000,
                    )
                    or 0
                )
                since_ver = int(
                    _safe_int(
                        qp.get("since_version", ["-1"])[0],
                        name="since_version",
                        default=-1,
                        min_val=-1,
                    )
                    or -1
                )
                job_id = qp.get("job_id", [""])[0].strip()
                status_obj = self.container.job_repo.get_space_status(space_id)
                sync_st = self.container.sync_tracker.get_state(space_id)
                cur_ver = int(sync_st.get("sync_version", 0))

                if wait_ms > 0:
                    deadline = time.monotonic() + (wait_ms / 1000.0)
                    init_completed_at = str(status_obj.get("last_completed_at", ""))
                    while True:
                        already_done = False
                        if job_id:
                            j_probe = self.container.job_repo.get_job(job_id)
                            if j_probe is not None and j_probe["space_id"] == space_id:
                                already_done = j_probe["status"] in ("completed", "failed", "cancelled")
                        elif since_ver >= 0 and cur_ver > since_ver and int(status_obj.get("pending", 0)) == 0:
                            already_done = True
                        elif int(status_obj.get("pending", 0)) == 0 and since_ver < 0:
                            already_done = True
                        elif (
                            not job_id
                            and str(status_obj.get("last_completed_at", "")) != init_completed_at
                        ):
                            already_done = True

                        rem = deadline - time.monotonic()
                        if already_done or rem <= 0.005:
                            break
                        if int(status_obj.get("pending", 0)) <= 0 and since_ver < 0:
                            break
                        wait_base = since_ver if (since_ver >= 0 and cur_ver <= since_ver) else cur_ver
                        sync_st = self.container.sync_tracker.wait_for_update(
                            space_id, wait_base, timeout_seconds=rem
                        )
                        cur_ver = int(sync_st.get("sync_version", 0))
                        status_obj = self.container.job_repo.get_space_status(space_id)

                status_obj["sync_version"] = int(sync_st.get("sync_version", 0))
                if job_id:
                    job_obj = self.container.job_repo.get_job(job_id)
                    if job_obj is not None and job_obj["space_id"] == space_id:
                        status_obj["job"] = job_obj
                return 200, status_obj

            if method == "GET" and path == "/api/notes/list":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                q = qp.get("q", [""])[0].strip()[:300]
                lim = _safe_int(qp.get("limit", ["40"])[0], name="limit", default=40, min_val=1, max_val=200)
                if q:
                    hits = self.container.note_repo.search_bm25(space_id=space_id, query=q, limit=int(lim or 40))
                    notes_out = [
                        {**n.to_dict(), "bm25_score": round(score, 4)}
                        for n, score in hits
                    ]
                else:
                    notes_out = [
                        n.to_dict()
                        for n in self.container.note_repo.list_notes(space_id=space_id, limit=int(lim or 40))
                    ]
                media_by_note = self.container.media_service.list_for_notes(
                    space_id, [n["note_id"] for n in notes_out]
                )
                for n_obj in notes_out:
                    n_obj["media"] = media_by_note.get(n_obj["note_id"], [])
                return 200, {"space_id": space_id, "count": len(notes_out), "notes": notes_out}

            if method == "POST" and path == "/api/notes/forget":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                note_id = str(payload.get("note_id", ""))
                span = str(payload.get("span", ""))
                out = self.container.ingest_service.forget_note(
                    space_id=space_id, note_id=note_id, span_to_forget=span
                )
                return 200, out

            if method == "GET" and path == "/api/memories":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                memory_type = qp.get("memory_type", [""])[0]
                limit = _safe_int(
                    qp.get("limit", [None])[0],
                    name="limit",
                    default=30,
                    min_val=1,
                    max_val=100,
                    strict=True,
                )
                assert limit is not None
                offset = _safe_int(
                    qp.get("offset", [None])[0],
                    name="offset",
                    default=0,
                    min_val=0,
                    max_val=100_000,
                    strict=True,
                )
                assert offset is not None
                items, total = self.container.memory_repo.page_active_memories(
                    space_id, memory_type, limit, offset
                )
                return 200, {
                    "memories": [item.to_dict() for item in items],
                    "has_more": offset + len(items) < total,
                    "total": total,
                }

            if method == "GET" and path == "/api/memories/search":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                q = qp.get("q", [""])[0][:500]
                mtype = qp.get("memory_type", [""])[0]
                top_k = _safe_int(
                    qp.get("top_k", [None])[0],
                    name="top_k",
                    default=20,
                    min_val=1,
                    max_val=100,
                    strict=True,
                )
                assert top_k is not None
                return 200, self.container.retrieval_service.search(
                    space_id=space_id, query=q, memory_type=mtype, top_k=top_k
                )

            if method == "GET" and path == "/api/memories/detail":
                ident = qp.get("id", [""])[0]
                detail = self.container.retrieval_service.read_memory_detail(space_id, ident)
                src_nids = [str(s.get("note_id", "")) for s in detail.get("sources", []) if s.get("note_id")]
                media_grouped = self.container.media_service.list_for_notes(space_id, src_nids)
                flat_media: list[dict[str, Any]] = []
                for nid in src_nids:
                    flat_media.extend(media_grouped.get(nid, []))
                detail["media"] = flat_media
                try:
                    detail["markdown"] = self.container.markdown_sync.render_memory_markdown(
                        space_id, ident
                    )["markdown"]
                except TraceMemoryError:
                    detail["markdown"] = ""
                return 200, detail

            if method == "GET" and path == "/api/memories/markdown":
                ident = qp.get("id", [""])[0]
                return 200, self.container.markdown_sync.render_memory_markdown(space_id, ident)

            if method == "POST" and path == "/api/memories/markdown/save":
                self._enforce_rate_and_quota(
                    user=user, space_id=space_id, client_ip=client_ip, require_note_slot=True
                )
                mem_id = str(payload.get("memory_id", "")).strip()
                md_text = str(payload.get("markdown", ""))
                if not mem_id or not md_text.strip():
                    raise ValidationError("memory_id and markdown are required")
                exp_v = _safe_int(
                    payload.get("expected_version"), name="expected_version", min_val=1, strict=True
                )
                can_export_disk = (not self.container.config.auth_required) or (
                    user is not None and user.role == "admin"
                )
                res = self.container.markdown_sync.apply_markdown_edit(
                    space_id=space_id,
                    identifier=mem_id,
                    markdown_text=md_text,
                    expected_version=exp_v,
                    actor_id=user.username if user else "markdown_editor",
                    export_to_disk=can_export_disk,
                )
                self.container.quota_service.record_usage(f"space:{space_id}", notes=1, embed_calls=2)
                return 200, res.to_dict()

            if method == "POST" and path == "/api/memories/propose":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                action = str(payload.get("action", "update"))
                if action not in ("create", "update", "forget", "approve", "reject"):
                    raise ValidationError(f"不支持的操作类型: {action}")
                title_txt = str(payload.get("title", "")).strip()
                raw_summary = str(payload.get("summary", "")).strip()
                summary_txt = raw_summary or title_txt or f"Web {action}"
                cfg = self.container.settings_store.get_all()
                max_chars = int(cfg.get("max_note_chars", 2000))
                if len(title_txt) > 200 or len(raw_summary) > max_chars:
                    raise ValidationError(f"标题或摘要超出长度上限（摘要上限 {max_chars} 字）")

                raw_inner_payload = payload.get("payload")
                if raw_inner_payload is not None and not isinstance(raw_inner_payload, dict):
                    raise ValidationError("参数 payload 必须为 JSON 对象")
                inner_payload = dict(raw_inner_payload or {})
                top_event_time = str(payload.get("event_time", "") or "").strip()
                if top_event_time and not str(inner_payload.get("event_time", "")).strip():
                    inner_payload["event_time"] = top_event_time

                raw_tags = payload.get("tags")
                if raw_tags is not None and not isinstance(raw_tags, list):
                    raise ValidationError("参数 tags 必须为数组")
                clean_tags = tuple(str(t).strip()[:32] for t in (raw_tags or [])[:20] if str(t).strip())

                has_relations_field = "relations" in payload
                raw_relations = payload.get("relations")
                if raw_relations is not None and not isinstance(raw_relations, list):
                    raise ValidationError("参数 relations 必须为数组")
                rel_specs: list[RelationSpec] = []
                if action in ("create", "update"):
                    for r_obj in raw_relations or []:
                        if not isinstance(r_obj, dict):
                            raise ValidationError("relations 每一项必须为对象")
                        if str(r_obj.get("target") or "").strip():
                            rel_specs.append(
                                RelationSpec(
                                    relation_type=str(r_obj.get("relation_type") or "relates_to").strip()[:48],
                                    target=str(r_obj.get("target")).strip()[:120],
                                    context=str(r_obj.get("context") or "").strip()[:160],
                                )
                            )
                    if not has_relations_field and raw_summary:
                        for rt, tgt, ctx in extract_relations_from_markdown(raw_summary):
                            rel_specs.append(RelationSpec(relation_type=rt, target=tgt, context=ctx))

                exp_v = _safe_int(
                    payload.get("expected_version"), name="expected_version", min_val=1, strict=True
                )
                idem_key = str(payload.get("idempotency_key", "")).strip()
                prop = ChangeProposal(
                    space_id=space_id,
                    action=action,
                    memory_type=str(payload.get("memory_type", "semantic")),
                    target_memory_id=str(payload.get("target_memory_id", "")),
                    expected_version=exp_v,
                    idempotency_key=idem_key,
                    actor_id=user.username if user else "user",
                    origin="user_direct",
                    title=title_txt,
                    summary=raw_summary,
                    payload=inner_payload,
                    event_time=top_event_time,
                    permalink=str(payload.get("permalink", ""))[:120],
                    canonical_key=str(payload.get("canonical_key", ""))[:120],
                    tags=clean_tags,
                    lock_for_user=bool(payload.get("lock_for_user", True)),
                    evidences=(),
                    relations=tuple(rel_specs),
                    replace_relations=has_relations_field,
                    reason=str(payload.get("reason", "Web console mutation"))[:200],
                )
                is_replay = bool(
                    idem_key and self.container.trace_repo.get_idempotent_result(space_id, idem_key) is not None
                )
                if is_replay:
                    res = self.container.change_service.apply(prop)
                    return 200, res.to_dict()

                if action != "create":
                    any_mem = self.container.memory_repo.get_any_space(prop.target_memory_id)
                    if any_mem is not None and any_mem.space_id != space_id:
                        raise PermissionDeniedError("无权访问其他记忆空间的记忆")
                    target_mem = self.container.memory_repo.get(
                        space_id, prop.target_memory_id, include_forgotten=True
                    )
                    if target_mem is None:
                        raise NotFoundError(f"Memory {prop.target_memory_id!r} not found in space {space_id!r}")
                    if target_mem.status == "forgotten":
                        if action == "forget":
                            return 200, self.container.change_service.apply(prop).to_dict()
                        raise ValidationError("已遗忘的记忆不可恢复或修改")
                    if exp_v is not None and exp_v != target_mem.version:
                        raise VersionConflictError(target_mem.memory_id, exp_v, target_mem.version)

                if action != "forget":
                    self._check_daily_quota(
                        user=user, space_id=space_id, require_note_slot=True, require_llm_slot=False
                    )
                    note_body = (
                        f"{title_txt}：{summary_txt}"
                        if (title_txt and title_txt not in summary_txt)
                        else summary_txt
                    )
                    src_note = RawNote(
                        note_id=new_id("note"),
                        space_id=space_id,
                        content=f"[Web Edit] {note_body}",
                        source_type="web",
                        actor_id=user.username if user else "user",
                    )
                    self.container.note_repo.save(src_note)
                    self.container.vector_lifecycle.sync_note(src_note)
                    prop = replace(
                        prop,
                        evidences=(
                            MemoryEvidence(note_id=src_note.note_id, note_revision=1, quote=summary_txt[:160]),
                        ),
                    )
                res = self.container.change_service.apply(prop)
                if action != "forget" and res.status != "idempotent_replay":
                    self.container.quota_service.record_usage(
                        f"space:{space_id}", notes=1, embed_calls=2
                    )
                return 200, res.to_dict()

            if method == "GET" and path == "/api/graph":
                seed = qp.get("seed", [""])[0]
                if seed:
                    return 200, self.container.retrieval_service.build_graph_context(
                        space_id=space_id, seed_identifier_or_query=seed
                    )
                all_mems = [
                    m.to_dict()
                    for m in self.container.memory_repo.list_memories(
                        space_id=space_id, status="active", limit=100
                    )
                ]
                all_rels = [
                    r.to_dict()
                    for r in self.container.relation_repo.list_all(space_id)
                    if r.to_target_text != "[FORGOTTEN]"
                ]
                return 200, {"nodes": [{"memory": m, "hop": 0} for m in all_mems], "edges": all_rels}

            if method == "GET" and path == "/api/graph/canvas":
                seed = qp.get("seed", [""])[0]
                memory_type = qp.get("memory_type", [""])[0]
                subject = qp.get("subject", [""])[0]
                max_hops = _safe_int(
                    qp.get("max_hops", [None])[0], name="max_hops", default=2, min_val=1, max_val=4
                )
                max_nodes = _safe_int(
                    qp.get("max_nodes", [None])[0], name="max_nodes", default=120, min_val=1, max_val=250
                )
                include_semantic = _safe_bool(qp.get("include_semantic", [None])[0], default=True)
                semantic_threshold = _safe_float(
                    qp.get("semantic_threshold", [None])[0],
                    name="semantic_threshold",
                    default=0.52,
                    min_val=0.25,
                    max_val=0.98,
                )
                max_sem_per_node = _safe_int(
                    qp.get("max_semantic_per_node", [None])[0],
                    name="max_semantic_per_node",
                    default=3,
                    min_val=1,
                    max_val=8,
                )
                include_unresolved = _safe_bool(qp.get("include_unresolved", [None])[0], default=True)
                include_co_evidence = _safe_bool(qp.get("include_co_evidence", [None])[0], default=True)
                return 200, self.container.retrieval_service.build_graph_canvas(
                    space_id=space_id,
                    seed=seed,
                    memory_type=memory_type,
                    subject=subject,
                    max_hops=int(max_hops or 2),
                    max_nodes=int(max_nodes or 120),
                    include_semantic=include_semantic,
                    semantic_threshold=semantic_threshold,
                    max_semantic_per_node=int(max_sem_per_node or 3),
                    include_unresolved=include_unresolved,
                    include_co_evidence=include_co_evidence,
                )

            if method == "POST" and path == "/api/ask":
                self._check_rate_limit(user=user, space_id=space_id, client_ip=client_ip)
                question = str(payload.get("question", "")).strip()
                if not question:
                    raise ValidationError("question is required")
                raw_top_k = payload.get("top_k")
                ask_top_k: int | None = None
                if raw_top_k is not None and str(raw_top_k).strip() != "":
                    if isinstance(raw_top_k, bool):
                        raise ValidationError("参数 top_k 必须为正整数")
                    try:
                        parsed_tk = int(raw_top_k)
                    except (TypeError, ValueError) as exc:
                        raise ValidationError("参数 top_k 必须为正整数") from exc
                    if parsed_tk < 1:
                        raise ValidationError("参数 top_k 必须大于或等于 1")
                    ask_top_k = min(25, parsed_tk)
                guard_ask = self.container.input_guard.inspect_ask(question)
                if guard_ask.allowed:
                    self._check_daily_quota(
                        user=user, space_id=space_id, require_llm_slot=True
                    )
                return 200, self.container.ask_service.ask(
                    space_id=space_id, question=question, top_k=ask_top_k
                )

            if method == "POST" and path == "/api/dream":
                self._enforce_rate_and_quota(
                    user=user, space_id=space_id, client_ip=client_ip, require_llm_slot=True
                )

                def _dream_opt_pos_int(raw_v: Any, name: str, cap: int) -> int | None:
                    if raw_v is None or (isinstance(raw_v, str) and not raw_v.strip()):
                        return None
                    if isinstance(raw_v, bool):
                        raise ValidationError(f"参数 {name} 必须为正整数")
                    try:
                        parsed_v = int(raw_v)
                    except (TypeError, ValueError) as exc:
                        raise ValidationError(f"参数 {name} 必须为正整数") from exc
                    if parsed_v < 1:
                        raise ValidationError(f"参数 {name} 必须大于或等于 1")
                    return min(cap, parsed_v)

                max_calls = _dream_opt_pos_int(payload.get("max_llm_calls"), "max_llm_calls", 50)
                max_tokens = _dream_opt_pos_int(payload.get("max_tokens"), "max_tokens", 200_000)
                return 200, self.container.dream_service.run_dream(
                    space_id=space_id,
                    max_llm_calls=max_calls,
                    max_tokens=max_tokens,
                )

            if method == "GET" and path == "/api/markdown/bundle":
                mems = self.container.memory_repo.list_memories(
                    space_id=space_id, status="active", limit=500
                )
                files = [
                    {
                        "path": f"{m.permalink}.md",
                        "title": m.title,
                        "memory_type": m.memory_type,
                        "markdown": self.container.markdown_sync.render_memory_markdown(
                            space_id, m.memory_id
                        )["markdown"],
                    }
                    for m in mems
                ]
                return 200, {"space_id": space_id, "count": len(files), "files": files}

            if method == "POST" and path == "/api/markdown/export":
                if self.container.config.auth_required and (user is None or user.role != "admin"):
                    raise PermissionDeniedError("多租户模式下服务器本地目录导出仅限管理员，请使用「打包下载 Markdown」")
                return 200, self.container.markdown_sync.export_space(space_id)

            if method == "POST" and path == "/api/markdown/sync":
                if self.container.config.auth_required and (user is None or user.role != "admin"):
                    raise PermissionDeniedError("多租户模式下服务器磁盘同步仅限管理员，普通用户请直接在线编辑 Markdown")
                return 200, self.container.markdown_sync.sync_external_edits(space_id)

            if method == "GET" and path == "/api/traces":
                traces = self.container.trace_repo.list_traces(space_id=space_id, limit=30)
                notes = [
                    n.to_dict()
                    for n in self.container.note_repo.list_notes(space_id=space_id, limit=30)
                ]
                return 200, {"traces": traces, "recent_notes": notes}

            if method == "POST" and path == "/api/seed-demo":
                self._enforce_rate_and_quota(
                    user=user,
                    space_id=space_id,
                    client_ip=client_ip,
                    require_note_slot=True,
                    require_llm_slot=True,
                )
                return 200, self._seed_demo_data(space_id)

            return 404, {"error": f"Endpoint not found: {method} {path}"}

        except UnauthorizedError as exc:
            return 401, {"error": str(exc), "code": "UNAUTHORIZED"}
        except RateLimitError as exc:
            return 429, {
                "error": str(exc),
                "code": "RATE_LIMIT_EXCEEDED",
                "retry_after": exc.retry_after_seconds,
            }
        except QuotaExceededError as exc:
            return 429, {"error": str(exc), "code": "QUOTA_EXCEEDED"}
        except PermissionDeniedError as exc:
            return 403, {"error": str(exc), "code": "PERMISSION_DENIED"}
        except NotFoundError as exc:
            return 404, {"error": str(exc), "code": "NOT_FOUND"}
        except VersionConflictError as exc:
            return 409, {
                "error": str(exc),
                "code": "VERSION_CONFLICT",
                "expected": exc.expected,
                "actual": exc.actual,
            }
        except ValidationError as exc:
            return 400, {"error": str(exc), "code": "VALIDATION_ERROR"}
        except TraceMemoryError as exc:
            return 400, {"error": str(exc)}

    def _seed_demo_data(self, space_id: str) -> dict[str, Any]:
        demo_inputs = [
            ("我平时很喜欢喝美式咖啡，但是晚上八点以后不喝咖啡。", ""),
            ("我非常偏好高可靠架构演进与向量检索性能优化。", ""),
            ("2025年3月我住在杭州。", "2025-03-10T10:00:00+00:00"),
            ("我现在搬到上海定居了。", "2026-10-01T10:00:00+00:00"),
            ("明天必须修复线上微信支付回调超时 bug [[facts/当前居住城市]]", ""),
            ("今天下午完成 Agent 融合记忆项目核心面试复盘", ""),
            ("下个月规划向量检索索引与双链图谱架构演进方案 [[preferences/关于高可靠架构演进与向量检索性能优化的偏好]]", ""),
            ("今天下午帮同事转发技术分享会议通知链接", ""),
            ("有空的时候顺便整理去年的电影片单与书架", ""),
            ("上周参加了云栖技术架构交流复盘会，收获很多。", ""),
        ]
        created = 0
        for text, ev_time in demo_inputs:
            key = "demo:" + hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
            res = self.container.ingest_service.record_note(
                space_id=space_id,
                content=text,
                source_type="web",
                event_time=ev_time,
                idempotency_key=key,
            )
            if res.extraction_status != "idempotent_replay":
                created += 1

        # Seed one rich DuoDo-style checklist + supervised demo task idempotently
        today_due = beijing_now().replace(hour=21, minute=0, second=0, microsecond=0).isoformat()
        rich_res = self.container.task_matrix_service.save_task_card(
            space_id=space_id,
            data={
                "title": "完成周六技术分享 PPT 与现场 Demo 联调",
                "summary": "结合高可靠架构偏好准备分享材料，并提前测试离线与真机演示",
                "importance": 5,
                "urgency": 4,
                "due_at": today_due,
                "is_all_day": False,
                "color_theme": "lavender",
                "task_mode": "multiplayer",
                "supervision_enabled": True,
                "assignee": "self",
                "repeat_rule": "never",
                "reminder_offset_minutes": 15,
                "auto_rollover": True,
                "focus_lock_enabled": True,
                "focus_minutes": 25,
                "duration_minutes": 60,
                "subtasks": [
                    {"id": "demo_sub_1", "title": "梳理四象限与长期记忆架构主线", "done": True},
                    {"id": "demo_sub_2", "title": "录制 Mac + iPhone 局域网同步演示", "done": False},
                    {"id": "demo_sub_3", "title": "核对 Q&A 关键数据与溯源证据链路", "done": False},
                ],
                "idempotency_key": "demo:duodo_rich_task_v1",
            },
        )
        if rich_res.status != "idempotent_replay":
            created += 1

        # Seed sample Pomodoro & Stopwatch focus sessions idempotently
        demo_task_id = rich_res.memory.memory_id if rich_res.memory else ""
        self.container.focus_service.insert_session_row(
            space_id=space_id,
            session_id=f"fcs_demo_pomo_{hashlib.md5(space_id.encode('utf-8')).hexdigest()[:8]}",
            task_memory_id=demo_task_id,
            task_title="完成周六技术分享 PPT 与现场 Demo 联调",
            mode="pomodoro",
            planned_minutes=25,
            duration_seconds=1500,
            status="completed",
        )
        self.container.focus_service.insert_session_row(
            space_id=space_id,
            session_id=f"fcs_demo_sw_{hashlib.md5(space_id.encode('utf-8')).hexdigest()[:8]}",
            task_memory_id="",
            task_title="深度阅读向量检索论文（自由专注）",
            mode="stopwatch",
            planned_minutes=0,
            duration_seconds=1080,
            status="completed",
        )

        # Seed showcase Solar/Lunar Countdowns & Anniversary idempotently
        now_d = beijing_now().date()
        sp_hash = hashlib.md5(space_id.encode("utf-8")).hexdigest()[:8]
        self.container.countdown_service.save_countdown(
            space_id=space_id,
            data={
                "countdown_id": f"cd_demo_cny_{sp_hash}",
                "allow_custom_id": True,
                "title": "春节团圆",
                "kind": "countdown",
                "calendar_type": "lunar",
                "lunar_year": now_d.year,
                "lunar_month": 1,
                "lunar_day": 1,
                "repeat_rule": "yearly",
                "icon": "🧧",
                "color_theme": "crimson",
                "pinned": True,
            },
        )
        self.container.countdown_service.save_countdown(
            space_id=space_id,
            data={
                "countdown_id": f"cd_demo_next_{sp_hash}",
                "allow_custom_id": True,
                "title": "里程碑版本发布",
                "kind": "countdown",
                "calendar_type": "solar",
                "target_date": (now_d + timedelta(days=1)).isoformat(),
                "repeat_rule": "none",
                "icon": "🚀",
                "color_theme": "lavender",
                "pinned": True,
            },
        )
        self.container.countdown_service.save_countdown(
            space_id=space_id,
            data={
                "countdown_id": f"cd_demo_anniv_{sp_hash}",
                "allow_custom_id": True,
                "title": "开启双链记忆之旅",
                "kind": "anniversary",
                "calendar_type": "solar",
                "target_date": (now_d - timedelta(days=78)).isoformat(),
                "repeat_rule": "yearly",
                "icon": "🌱",
                "color_theme": "mint",
                "pinned": False,
            },
        )

        # Seed showcase Pinned Memo + Today's Mood Journal idempotently
        existing_memos = self.container.memo_journal_service.list_memos(space_id=space_id, pinned_only=True)
        if existing_memos["pinned_count"] == 0:
            self.container.memo_journal_service.save_memo(
                space_id=space_id,
                payload={
                    "title": "有迹 防臃肿设计备忘",
                    "content": "坚持「一套可追溯记忆内核 + 5 栏收敛导航 + 可插拔开关 + 磁盘轻附件」。\n- 支持 **Markdown** 与置顶瀑布流\n- #架构灵感 #防臃肿",
                    "pinned": True,
                    "tags": ["架构灵感", "防臃肿"],
                    "extract_memory": False,
                },
            )
        today_journal = self.container.memo_journal_service.get_journal_overview(
            space_id=space_id, date_str=now_d.isoformat()
        )
        if today_journal["current_entry"] is None:
            self.container.memo_journal_service.save_journal(
                space_id=space_id,
                payload={
                    "journal_date": now_d.isoformat(),
                    "mood": "focused",
                    "title": f"{now_d.isoformat()} 每日复盘",
                    "content": "今天把待办、日历时间轴与番茄钟串联在同一个记忆空间里，节奏感明显更稳了。 #每日复盘",
                    "extract_memory": False,
                },
            )
        return {"seeded_notes": created, "space_id": space_id}

    def dispatch_mcp_http(
        self,
        raw_bytes: bytes,
        query_params: dict[str, list[str]] | None = None,
        headers: dict[str, str] | None = None,
        client_ip: str = "127.0.0.1",
    ) -> tuple[int, Any]:
        """Handle authenticated Streamable HTTP JSON-RPC 2.0 MCP requests (`/mcp`)."""
        from trace_memory.mcp_server.server import MCPToolDispatcher, handle_jsonrpc_message

        qp = query_params or {}
        token = _extract_token(qp, headers)
        try:
            user, space_id, _ = self._resolve_auth_and_space("/mcp", qp, {}, token)
        except UnauthorizedError as exc:
            return 401, {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": str(exc)}}
        except PermissionDeniedError as exc:
            return 403, {"jsonrpc": "2.0", "id": None, "error": {"code": -32003, "message": str(exc)}}

        if not raw_bytes.strip():
            return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Empty body"}}

        try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}

        mcp_disp = MCPToolDispatcher(
            self.container,
            trusted_space_id=space_id,
            actor_id=user.username if user else "mcp_client",
            actor_user_id=user.user_id if user else "",
            enforce_remote_guards=True,
            client_ip=client_ip,
            daily_note_limit=user.daily_note_limit if user else None,
            daily_llm_limit=user.daily_llm_limit if user else None,
            daily_token_limit=user.daily_token_limit if user else None,
        )

        if isinstance(parsed, list):
            if len(parsed) > 10:
                return 400, {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Batch size exceeds maximum of 10 requests"},
                }
            responses = []
            for item in parsed:
                if isinstance(item, dict):
                    res = handle_jsonrpc_message(mcp_disp, item)
                    if res is not None:
                        responses.append(res)
                else:
                    responses.append(
                        {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
                    )
            if not responses:
                return 202, None
            return 200, responses

        if not isinstance(parsed, dict):
            return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

        resp = handle_jsonrpc_message(mcp_disp, parsed)
        if resp is None:
            return 202, None
        return 200, resp


def create_fastapi_app(container: AppContainer) -> Any:
    """Create a FastAPI ASGI app when `fastapi` is installed in the environment."""
    if not _HAS_FASTAPI:
        raise RuntimeError("FastAPI is not installed")

    @asynccontextmanager
    async def _lifespan(_app: Any):
        try:
            import anyio

            limiter = anyio.to_thread.current_default_thread_limiter()
            if limiter.total_tokens < container.config.api_threadpool_tokens:
                limiter.total_tokens = container.config.api_threadpool_tokens
        except Exception:
            pass
        container.ingest_worker.start()
        container.daily_scheduler.start()
        try:
            yield
        finally:
            container.daily_scheduler.stop()
            container.ingest_worker.stop()

    class _SelectiveGZipMiddleware:
        """GZip compress HTML/JSON >= 1 KB while skipping SSE streams and binary images."""

        def __init__(self, app_inner: Any) -> None:
            self.app_inner = app_inner
            try:
                self.gzip_app = GZipMiddleware(app_inner, minimum_size=1000, compresslevel=6)
            except TypeError:  # pragma: no cover - older Starlette without compresslevel
                self.gzip_app = GZipMiddleware(app_inner, minimum_size=1000)

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope.get("type") == "http":
                req_path = scope.get("path", "")
                if req_path in _GZIP_SKIP_EXACT or req_path.startswith(_GZIP_SKIP_PREFIXES):
                    await self.app_inner(scope, receive, send)
                    return
            await self.gzip_app(scope, receive, send)

    dispatcher = ApiDispatcher(container)
    app = FastAPI(title="Trace-Memory Console & API", version="0.1.0", lifespan=_lifespan)
    app.add_middleware(_SelectiveGZipMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _serve_cached_html(request: Request, loader: Any) -> Response:
        html_bytes, etag = loader()
        resp_headers = {
            "Cache-Control": "no-cache",
            "ETag": etag,
        }
        if _etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=resp_headers)
        return Response(
            content=html_bytes,
            media_type="text/html; charset=utf-8",
            headers=resp_headers,
        )

    @app.get("/", response_class=HTMLResponse)
    @app.get("/index.html", response_class=HTMLResponse)
    @app.get("/console", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        return _serve_cached_html(request, _load_dashboard_html_cached)

    @app.get("/home", response_class=HTMLResponse)
    @app.get("/welcome", response_class=HTMLResponse)
    @app.get("/official", response_class=HTMLResponse)
    async def landing_page(request: Request) -> Response:
        return _serve_cached_html(request, _load_landing_html_cached)

    @app.get("/manifest.webmanifest")
    async def manifest() -> Response:
        return Response(content=PWA_MANIFEST_JSON, media_type="application/manifest+json")

    @app.get("/icon.png")
    @app.get("/favicon.ico")
    async def icon_png() -> Response:
        if APP_ICON_PNG_PATH.is_file():
            return Response(content=APP_ICON_PNG_PATH.read_bytes(), media_type="image/png", headers={"Cache-Control": "no-cache"})
        return Response(content=PWA_ICON_SVG, media_type="image/svg+xml")

    @app.get("/apple-touch-icon.png")
    @app.get("/icon-maskable.png")
    async def icon_maskable_png() -> Response:
        target = APP_ICON_MASKABLE_PNG_PATH if APP_ICON_MASKABLE_PNG_PATH.is_file() else APP_ICON_PNG_PATH
        if target.is_file():
            return Response(content=target.read_bytes(), media_type="image/png", headers={"Cache-Control": "no-cache"})
        return Response(content=PWA_ICON_SVG, media_type="image/svg+xml")

    @app.get("/icon.svg")
    async def icon_svg() -> Response:
        return Response(content=PWA_ICON_SVG, media_type="image/svg+xml")

    @app.get("/scenes/{filename}")
    async def scene_image(filename: str, request: Request) -> Response:
        path = _scene_file(filename)
        if path is not None:
            etag = _scene_etag(path)
            headers = {"Cache-Control": "no-cache", "ETag": etag}
            if _etag_matches(request.headers.get("if-none-match"), etag):
                return Response(status_code=304, headers=headers)
            return Response(content=path.read_bytes(), media_type="image/jpeg", headers=headers)
        return JSONResponse(content={"error": "scene not found"}, status_code=404)

    @app.get("/sw.js")
    async def service_worker() -> Response:
        return Response(content=PWA_SW_JS, media_type="application/javascript")

    @app.get("/mcp")
    async def mcp_info() -> JSONResponse:
        return JSONResponse(
            content={
                "name": "trace-memory-mcp",
                "transport": "streamable-http-jsonrpc",
                "endpoint": "/mcp",
                "auth_required": bool(container.config.auth_required),
                "auth_header": "Authorization: Bearer <token>",
            },
            status_code=200,
        )

    @app.post("/mcp")
    async def mcp_entry(request: Request) -> Response:
        qp: dict[str, list[str]] = {}
        for k, v in request.query_params.multi_items():
            qp.setdefault(k, []).append(v)
        hdrs = {k: v for k, v in request.headers.items()}
        direct_ip = request.client.host if request.client else "127.0.0.1"
        client_ip = _resolve_client_ip(hdrs, direct_ip, container.config.trust_proxy_headers)

        raw_bytes = await request.body()
        max_bytes = max(1024, int(container.config.max_request_body_bytes))
        if len(raw_bytes) > max_bytes:
            return JSONResponse(
                content={"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Payload too large"}},
                status_code=413,
            )
        code, resp_obj = await asyncio.to_thread(
            dispatcher.dispatch_mcp_http,
            raw_bytes,
            query_params=qp,
            headers=hdrs,
            client_ip=client_ip,
        )
        if code == 202 and resp_obj is None:
            return Response(status_code=202)
        return JSONResponse(content=resp_obj, status_code=code)

    @app.get("/api/media/file")
    async def api_media_file(request: Request) -> Response:
        qp: dict[str, list[str]] = {}
        for k, v in request.query_params.multi_items():
            qp.setdefault(k, []).append(v)
        hdrs = {k: v for k, v in request.headers.items()}
        code, mime, raw = await asyncio.to_thread(dispatcher.resolve_media_file, qp, hdrs)
        return Response(
            content=raw,
            status_code=code,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=3600" if code == 200 else "no-store",
            },
        )

    @app.get("/api/sync/stream")
    async def api_sync_stream(request: Request) -> Response:
        qp: dict[str, list[str]] = {}
        for k, v in request.query_params.multi_items():
            qp.setdefault(k, []).append(v)
        hdrs = {k: v for k, v in request.headers.items()}
        eff_token = _extract_token(qp, hdrs)
        try:
            _, space_id, _ = dispatcher._resolve_auth_and_space("/api/sync/stream", qp, {}, eff_token)
        except UnauthorizedError as exc:
            return JSONResponse(content={"error": str(exc), "code": "AUTH_REQUIRED"}, status_code=401)
        except PermissionDeniedError as exc:
            return JSONResponse(content={"error": str(exc)}, status_code=403)

        since_raw = qp.get("since", ["0"])[0]
        try:
            known_ver = int(since_raw)
        except ValueError:
            known_ver = 0

        async def _event_gen():
            nonlocal known_ver
            init_st = await asyncio.to_thread(
                dispatcher._build_sync_state_payload, space_id, include_recent_jobs=True
            )
            known_ver = max(known_ver, int(init_st["sync_version"]))
            yield f"event: sync_state\ndata: {json.dumps(init_st, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                st = await asyncio.to_thread(
                    container.sync_tracker.wait_for_update, space_id, known_ver, 15.0
                )
                if await request.is_disconnected():
                    break
                cur_ver = int(st["sync_version"])
                if cur_ver > known_ver:
                    full_st = await asyncio.to_thread(
                        dispatcher._build_sync_state_payload, space_id, include_recent_jobs=True
                    )
                    known_ver = max(cur_ver, int(full_st["sync_version"]))
                    yield f"event: space_updated\ndata: {json.dumps(full_st, ensure_ascii=False)}\n\n"
                else:
                    yield ": keepalive\n\n"

        return StreamingResponse(
            _event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.api_route("/api/{rest_of_path:path}", methods=["GET", "POST"])
    async def api_entry(request: Request, rest_of_path: str) -> JSONResponse:
        qp: dict[str, list[str]] = {}
        for k, v in request.query_params.multi_items():
            qp.setdefault(k, []).append(v)
        hdrs = {k: v for k, v in request.headers.items()}
        direct_ip = request.client.host if request.client else "127.0.0.1"
        client_ip = _resolve_client_ip(hdrs, direct_ip, container.config.trust_proxy_headers)

        body: dict[str, Any] = {}
        if request.method == "POST":
            raw_bytes = await request.body()
            is_media_route = rest_of_path in ("voice/transcribe", "media/upload")
            max_bytes = max(
                1024,
                int(container.config.max_media_body_bytes if is_media_route else container.config.max_request_body_bytes),
            )
            if len(raw_bytes) > max_bytes:
                return JSONResponse(
                    content={"error": f"请求体过大（上限 {max_bytes} 字节）", "code": "PAYLOAD_TOO_LARGE"},
                    status_code=413,
                )
            if raw_bytes.strip():
                try:
                    body = json.loads(raw_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return JSONResponse(content={"error": "Invalid JSON body"}, status_code=400)
                if not isinstance(body, dict):
                    return JSONResponse(content={"error": "JSON body must be an object"}, status_code=400)
        status_code, payload = await asyncio.to_thread(
            dispatcher.dispatch,
            request.method,
            f"/api/{rest_of_path}",
            qp,
            body,
            headers=hdrs,
            client_ip=client_ip,
        )
        return JSONResponse(content=payload, status_code=status_code)

    return app


def run_web_server(container: AppContainer, host: str = "127.0.0.1", port: int = 18090) -> None:
    """Start FastAPI/Uvicorn if installed, otherwise start built-in ThreadingHTTPServer."""
    try:
        import uvicorn

        app = create_fastapi_app(container)
        uvicorn.run(app, host=host, port=port)
        return
    except ImportError:
        pass

    dispatcher = ApiDispatcher(container)

    class _Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_cors()
            self.end_headers()

        def _serve_cached_html(self, loader: Any) -> None:
            html_bytes, etag = loader()
            if _etag_matches(self.headers.get("If-None-Match"), etag):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self._send_cors()
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self._write_raw(
                200,
                "text/html; charset=utf-8",
                html_bytes,
                extra_headers={"Cache-Control": "no-cache", "ETag": etag},
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html", "/console"):
                self._serve_cached_html(_load_dashboard_html_cached)
                return
            if parsed.path in ("/home", "/welcome", "/official"):
                self._serve_cached_html(_load_landing_html_cached)
                return
            if parsed.path == "/manifest.webmanifest":
                self._write_raw(200, "application/manifest+json", PWA_MANIFEST_JSON.encode("utf-8"))
                return
            if parsed.path in ("/icon.png", "/favicon.ico"):
                if APP_ICON_PNG_PATH.is_file():
                    self._write_raw(200, "image/png", APP_ICON_PNG_PATH.read_bytes())
                else:
                    self._write_raw(200, "image/svg+xml", PWA_ICON_SVG.encode("utf-8"))
                return
            if parsed.path in ("/apple-touch-icon.png", "/icon-maskable.png"):
                target = APP_ICON_MASKABLE_PNG_PATH if APP_ICON_MASKABLE_PNG_PATH.is_file() else APP_ICON_PNG_PATH
                if target.is_file():
                    self._write_raw(200, "image/png", target.read_bytes())
                else:
                    self._write_raw(200, "image/svg+xml", PWA_ICON_SVG.encode("utf-8"))
                return
            if parsed.path == "/icon.svg":
                self._write_raw(200, "image/svg+xml", PWA_ICON_SVG.encode("utf-8"))
                return
            if parsed.path == "/sw.js":
                self._write_raw(200, "application/javascript", PWA_SW_JS.encode("utf-8"))
                return
            if parsed.path.startswith("/scenes/"):
                fname = parsed.path[len("/scenes/"):]
                path = _scene_file(fname)
                if path is not None:
                    etag = _scene_etag(path)
                    if _etag_matches(self.headers.get("If-None-Match"), etag):
                        self.send_response(HTTPStatus.NOT_MODIFIED)
                        self._send_cors()
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("ETag", etag)
                        self.end_headers()
                        return
                    self._write_raw(
                        200,
                        "image/jpeg",
                        path.read_bytes(),
                        extra_headers={"Cache-Control": "no-cache", "ETag": etag},
                    )
                else:
                    self._write_json(404, {"error": "scene not found"})
                return
            if parsed.path == "/mcp":
                self._write_json(
                    200,
                    {
                        "name": "trace-memory-mcp",
                        "transport": "streamable-http-jsonrpc",
                        "endpoint": "/mcp",
                        "auth_required": bool(container.config.auth_required),
                        "auth_header": "Authorization: Bearer <token>",
                    },
                )
                return

            if parsed.path == "/api/media/file":
                qp_m = parse_qs(parsed.query, keep_blank_values=True)
                hdrs_m = {k: v for k, v in self.headers.items()}
                code_m, mime_m, raw_m = dispatcher.resolve_media_file(qp_m, hdrs_m)
                self._write_raw(
                    code_m,
                    mime_m,
                    raw_m,
                    extra_headers={
                        "X-Content-Type-Options": "nosniff",
                        "Cache-Control": "private, max-age=3600" if code_m == 200 else "no-store",
                    },
                )
                return

            if parsed.path == "/api/sync/stream":
                qp_s = parse_qs(parsed.query, keep_blank_values=True)
                hdrs_s = {k: v for k, v in self.headers.items()}
                eff_tok = _extract_token(qp_s, hdrs_s)
                try:
                    _, sp_s, _ = dispatcher._resolve_auth_and_space("/api/sync/stream", qp_s, {}, eff_tok)
                except UnauthorizedError as exc:
                    self._write_json(401, {"error": str(exc), "code": "AUTH_REQUIRED"})
                    return
                except PermissionDeniedError as exc:
                    self._write_json(403, {"error": str(exc)})
                    return
                try:
                    known_v = int((qp_s.get("since") or ["0"])[0])
                except ValueError:
                    known_v = 0
                self.send_response(HTTPStatus.OK)
                self._send_cors()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    init_payload = dispatcher._build_sync_state_payload(sp_s, include_recent_jobs=True)
                    known_v = max(known_v, int(init_payload["sync_version"]))
                    self.wfile.write(
                        f"event: sync_state\ndata: {json.dumps(init_payload, ensure_ascii=False)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()
                    while not container.ingest_worker._stop_event.is_set():
                        st_s = container.sync_tracker.wait_for_update(sp_s, known_v, 15.0)
                        cur_v = int(st_s["sync_version"])
                        if cur_v > known_v:
                            full_s = dispatcher._build_sync_state_payload(sp_s, include_recent_jobs=True)
                            known_v = max(cur_v, int(full_s["sync_version"]))
                            self.wfile.write(
                                f"event: space_updated\ndata: {json.dumps(full_s, ensure_ascii=False)}\n\n".encode("utf-8")
                            )
                        else:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                return

            qp = parse_qs(parsed.query, keep_blank_values=True)
            hdrs = {k: v for k, v in self.headers.items()}
            client_ip = _resolve_client_ip(
                hdrs, str(self.client_address[0]), container.config.trust_proxy_headers
            )
            code, resp = dispatcher.dispatch(
                "GET", parsed.path, qp, None, headers=hdrs, client_ip=client_ip
            )
            self._write_json(code, resp)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            qp = parse_qs(parsed.query, keep_blank_values=True)
            hdrs = {k: v for k, v in self.headers.items()}
            client_ip = _resolve_client_ip(
                hdrs, str(self.client_address[0]), container.config.trust_proxy_headers
            )
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                self._write_json(400, {"error": "Invalid Content-Length"})
                return
            is_media_route = parsed.path in ("/api/voice/transcribe", "/api/media/upload")
            max_bytes = max(
                1024,
                int(container.config.max_media_body_bytes if is_media_route else container.config.max_request_body_bytes),
            )
            if length > max_bytes:
                self._write_json(413, {"error": f"请求体过大（上限 {max_bytes} 字节）", "code": "PAYLOAD_TOO_LARGE"})
                return
            raw_bytes = self.rfile.read(length) if length > 0 else b""
            if parsed.path == "/mcp":
                code, resp_obj = dispatcher.dispatch_mcp_http(
                    raw_bytes, query_params=qp, headers=hdrs, client_ip=client_ip
                )
                if code == 202 and resp_obj is None:
                    self._write_raw(202, "application/json; charset=utf-8", b"")
                else:
                    self._write_raw(
                        code,
                        "application/json; charset=utf-8",
                        json.dumps(resp_obj, ensure_ascii=False).encode("utf-8"),
                    )
                return

            try:
                raw = raw_bytes.decode("utf-8") if raw_bytes else "{}"
                body = json.loads(raw) if raw.strip() else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._write_json(400, {"error": "Invalid JSON body"})
                return
            if not isinstance(body, dict):
                self._write_json(400, {"error": "JSON body must be an object"})
                return
            code, resp = dispatcher.dispatch(
                "POST", parsed.path, qp, body, headers=hdrs, client_ip=client_ip
            )
            self._write_json(code, resp)

        def _send_cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, X-Auth-Token, Mcp-Session-Id",
            )

        def _write_raw(
            self,
            code: int,
            content_type: str,
            data: bytes,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(code)
            self._send_cors()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _write_json(self, code: int, data: dict[str, Any]) -> None:
            self._write_raw(
                code,
                "application/json; charset=utf-8",
                json.dumps(data, ensure_ascii=False).encode("utf-8"),
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    container.ingest_worker.start()
    container.daily_scheduler.start()
    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"Trace-Memory Web Console running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    finally:
        container.daily_scheduler.stop()
        container.ingest_worker.stop()

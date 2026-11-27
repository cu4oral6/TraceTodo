"""Explicit configuration and Composition Root settings for Trace-Memory.

Design principle (adopted from Basic Memory):
Only composition roots (CLI, MCP, Web API, or test fixtures) read environment variables
via `TraceMemoryConfig.from_env()`. All lower layers receive `TraceMemoryConfig` explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


def _load_dotenv_file(dotenv_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not dotenv_path.is_file():
        return values
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class TraceMemoryConfig:
    """Immutable runtime configuration passed down from composition roots."""

    default_space: str = "default"
    data_dir: Path = Path("./data")
    knowledge_dir: Path = Path("./knowledge")

    # Vector Store settings ("sqlite" | "qdrant")
    vector_backend: str = "sqlite"
    qdrant_path: Path = Path("./data/qdrant")
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    embedding_dim: int = 384

    # LLM & Embedding settings ("mock" | "openai")
    llm_provider: str = "mock"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    vision_model: str = "qwen3.8-omni-flash"
    asr_model: str = "whisper-1"
    embedding_model: str = "text-embedding-3-small"
    llm_timeout_seconds: float = 30.0
    # Minimum cosine score for hits that only match semantically (no BM25 keyword hit).
    # Real embedding models (e.g. text-embedding-v3) have a ~0.35-0.48 noise floor on gibberish
    # or unrelated queries vs >=0.54 for true semantic recall, so pure-vector hits below this
    # floor are treated as noise. 0 disables the gate.
    vector_only_min_score: float = 0.0

    # ReMe-style Dream consolidation budget guard
    dream_max_llm_calls: int = 5
    dream_max_tokens: int = 8000

    # Controlled GraphRAG link expansion budget
    graph_max_hops: int = 2
    graph_max_nodes: int = 10
    graph_max_chars: int = 4000

    # Web console host/port
    web_host: str = "127.0.0.1"
    web_port: int = 18090

    # Multi-user authentication & bootstrap admin settings
    auth_required: bool = False
    allow_registration: bool = True
    register_captcha_required: bool = False
    admin_username: str = ""
    admin_password: str = ""
    session_ttl_hours: int = 720
    judge_demo_enabled: bool = False

    # Low-cost defense & quota defaults
    noise_filter_enabled: bool = True
    max_note_chars: int = 2000
    min_meaningful_chars: int = 3
    rate_limit_per_minute: int = 25
    duplicate_window_seconds: int = 120
    default_daily_note_limit: int = 60
    default_daily_llm_limit: int = 80
    default_daily_token_limit: int = 100000
    max_request_body_bytes: int = 131072
    max_media_body_bytes: int = 4194304
    space_media_quota_mb: int = 50
    trust_proxy_headers: bool = False

    # Concurrency & Async Ingest Queue settings
    ingest_worker_count: int = 96
    ingest_space_concurrency: int = 8
    llm_max_concurrency: int = 80
    ingest_max_retries: int = 3
    api_threadpool_tokens: int = 64
    # "v2" = parallel extraction + ordered per-space commit pump; "v1" = legacy barrier worker pool
    ingest_pipeline: str = "v2"
    # Seconds an extracted job waits behind a slow earlier job before an unrelated one may skip ahead (0 = never)
    commit_deadline_seconds: float = 10.0
    # Recent raw-note context window passed to extraction (reference / supplement resolution)
    context_notes: int = 20
    context_chars: int = 2000
    context_note_chars: int = 150
    context_hours: float = 24.0
    vector_worker_count: int = 8
    # Extra API keys pooled with `openai_api_key` (429 on one key -> immediate retry on another)
    openai_api_keys: tuple[str, ...] = ()
    llm_key_cooldown_seconds: float = 15.0

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "trace_memory.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        dotenv_path: Path | None = None,
    ) -> TraceMemoryConfig:
        if dotenv_path is None:
            cwd_env = Path(".env")
            pkg_root_env = Path(__file__).resolve().parents[2] / ".env"
            dotenv_path = cwd_env if cwd_env.is_file() else pkg_root_env
        file_vars = _load_dotenv_file(dotenv_path) if dotenv_path else {}
        env = dict(file_vars)
        env.update(os.environ if environ is None else environ)

        def _get(key: str, default: str) -> str:
            val = env.get(key)
            return val if val is not None and val != "" else default

        data_dir = Path(_get("TRACE_MEMORY_DATA_DIR", "./data"))
        knowledge_dir = Path(_get("TRACE_MEMORY_KNOWLEDGE_DIR", "./knowledge"))
        qdrant_path = Path(_get("TRACE_MEMORY_QDRANT_PATH", str(data_dir / "qdrant")))

        return cls(
            default_space=_get("TRACE_MEMORY_DEFAULT_SPACE", "default"),
            data_dir=data_dir,
            knowledge_dir=knowledge_dir,
            vector_backend=_get("TRACE_MEMORY_VECTOR_BACKEND", "sqlite").lower(),
            qdrant_path=qdrant_path,
            qdrant_url=env.get("TRACE_MEMORY_QDRANT_URL", ""),
            qdrant_api_key=env.get("TRACE_MEMORY_QDRANT_API_KEY", ""),
            embedding_dim=int(_get("TRACE_MEMORY_EMBEDDING_DIM", "384")),
            llm_provider=_get("TRACE_MEMORY_LLM_PROVIDER", "mock").lower(),
            openai_base_url=_get("TRACE_MEMORY_OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_api_key=env.get("TRACE_MEMORY_OPENAI_API_KEY", ""),
            llm_model=_get("TRACE_MEMORY_LLM_MODEL", "gpt-4o-mini"),
            vision_model=_get("TRACE_MEMORY_VISION_MODEL", "qwen3.8-omni-flash"),
            asr_model=_get("TRACE_MEMORY_ASR_MODEL", "whisper-1"),
            embedding_model=_get("TRACE_MEMORY_EMBEDDING_MODEL", "text-embedding-3-small"),
            vector_only_min_score=float(
                _get(
                    "TRACE_MEMORY_VECTOR_ONLY_MIN_SCORE",
                    "0.50" if _get("TRACE_MEMORY_LLM_PROVIDER", "mock").lower() == "openai" else "0",
                )
            ),
            dream_max_llm_calls=int(_get("TRACE_MEMORY_DREAM_MAX_LLM_CALLS", "5")),
            dream_max_tokens=int(_get("TRACE_MEMORY_DREAM_MAX_TOKENS", "8000")),
            graph_max_hops=int(_get("TRACE_MEMORY_GRAPH_MAX_HOPS", "2")),
            graph_max_nodes=int(_get("TRACE_MEMORY_GRAPH_MAX_NODES", "10")),
            graph_max_chars=int(_get("TRACE_MEMORY_GRAPH_MAX_CHARS", "4000")),
            web_host=_get("TRACE_MEMORY_WEB_HOST", "127.0.0.1"),
            web_port=int(_get("TRACE_MEMORY_WEB_PORT", "18090")),
            auth_required=_as_bool(env.get("TRACE_MEMORY_AUTH_REQUIRED"), False),
            allow_registration=_as_bool(env.get("TRACE_MEMORY_ALLOW_REGISTRATION"), True),
            register_captcha_required=_as_bool(env.get("TRACE_MEMORY_REGISTER_CAPTCHA_REQUIRED"), True),
            admin_username=_get("TRACE_MEMORY_ADMIN_USERNAME", ""),
            admin_password=env.get("TRACE_MEMORY_ADMIN_PASSWORD", ""),
            session_ttl_hours=int(_get("TRACE_MEMORY_SESSION_TTL_HOURS", "720")),
            judge_demo_enabled=_as_bool(env.get("TRACE_MEMORY_JUDGE_DEMO_ENABLED"), False),
            noise_filter_enabled=_as_bool(env.get("TRACE_MEMORY_NOISE_FILTER_ENABLED"), True),
            max_note_chars=int(_get("TRACE_MEMORY_MAX_NOTE_CHARS", "2000")),
            min_meaningful_chars=int(_get("TRACE_MEMORY_MIN_MEANINGFUL_CHARS", "3")),
            rate_limit_per_minute=int(_get("TRACE_MEMORY_RATE_LIMIT_PER_MINUTE", "25")),
            duplicate_window_seconds=int(_get("TRACE_MEMORY_DUPLICATE_WINDOW_SECONDS", "120")),
            default_daily_note_limit=int(_get("TRACE_MEMORY_DAILY_NOTE_LIMIT", "60")),
            default_daily_llm_limit=int(_get("TRACE_MEMORY_DAILY_LLM_LIMIT", "80")),
            default_daily_token_limit=int(_get("TRACE_MEMORY_DAILY_TOKEN_LIMIT", "100000")),
            max_request_body_bytes=int(_get("TRACE_MEMORY_MAX_REQUEST_BODY_BYTES", "131072")),
            max_media_body_bytes=int(_get("TRACE_MEMORY_MAX_MEDIA_BODY_BYTES", "4194304")),
            space_media_quota_mb=max(5, int(_get("TRACE_MEMORY_SPACE_MEDIA_QUOTA_MB", "50"))),
            trust_proxy_headers=_as_bool(env.get("TRACE_MEMORY_TRUST_PROXY_HEADERS"), False),
            ingest_worker_count=max(1, int(_get("TRACE_MEMORY_INGEST_WORKERS", "96"))),
            ingest_space_concurrency=max(1, int(_get("TRACE_MEMORY_INGEST_SPACE_CONCURRENCY", "8"))),
            llm_max_concurrency=max(1, int(_get("TRACE_MEMORY_LLM_MAX_CONCURRENCY", "80"))),
            ingest_max_retries=max(1, int(_get("TRACE_MEMORY_INGEST_MAX_RETRIES", "3"))),
            api_threadpool_tokens=max(8, int(_get("TRACE_MEMORY_API_THREADS", "64"))),
            llm_timeout_seconds=max(3.0, float(_get("TRACE_MEMORY_LLM_TIMEOUT", "15"))),
            ingest_pipeline=_get("TRACE_MEMORY_INGEST_PIPELINE", "v2").lower(),
            commit_deadline_seconds=max(0.0, float(_get("TRACE_MEMORY_COMMIT_DEADLINE_S", "10"))),
            context_notes=max(0, int(_get("TRACE_MEMORY_CONTEXT_NOTES", "20"))),
            context_chars=max(0, int(_get("TRACE_MEMORY_CONTEXT_CHARS", "2000"))),
            context_note_chars=max(20, int(_get("TRACE_MEMORY_CONTEXT_NOTE_CHARS", "150"))),
            context_hours=max(0.0, float(_get("TRACE_MEMORY_CONTEXT_HOURS", "24"))),
            vector_worker_count=max(1, int(_get("TRACE_MEMORY_VECTOR_WORKERS", "8"))),
            openai_api_keys=tuple(
                k.strip() for k in env.get("TRACE_MEMORY_OPENAI_API_KEYS", "").split(",") if k.strip()
            ),
            llm_key_cooldown_seconds=max(1.0, float(_get("TRACE_MEMORY_LLM_KEY_COOLDOWN_S", "15"))),
        )

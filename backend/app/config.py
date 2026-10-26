"""全局配置。

为什么所有阈值都放这里：PRD 的验收标准（P0-05 TTFT、P0-06 路由、§7.3 Grader 三态阈值）
都是**可调参数**而不是硬编码常量。集中在一处，评审与调参时只改一个文件，
业务代码里出现魔法数字即视为不通过。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录的绝对路径。相对路径（DATA_DIR、sqlite 文件）一律以它为基准，
# 否则 `uvicorn --app-dir backend`（CWD=项目根）与 `cd backend && uvicorn`（CWD=backend）
# 会落到两个不同的 data/ 目录，出现「上传的 PDF 找不到」这类幽灵问题。
BACKEND_ROOT: Path = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """环境变量配置。字段名即环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 数据库 ----------------
    # 默认 SQLite 开箱即跑；换成 postgresql+asyncpg://... 即切 Postgres + pgvector。
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/app.db"

    # ---------------- LLM ----------------
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    # 空字符串 = 演示模式（MockGenerator 确定性生成），顶栏必须显示黄标（P0-12③）
    LLM_API_KEY: str = ""
    # SPEC §6 成本模型：路由/评估用轻量模型，AST 生成用重模型，两个独立配置项
    LLM_MODEL_ROUTER: str = "gpt-4o-mini"
    LLM_MODEL_GENERATOR: str = "gpt-4o"
    LLM_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0)
    LLM_MAX_RETRIES: int = Field(default=3, ge=1, le=5)

    # ---------------- 检索 / Grader（§7.2 §7.3）----------------
    GRADER_MATCH_THRESHOLD: float = 0.55
    GRADER_SUPPORT_THRESHOLD: float = 0.35
    GRADER_PARTIAL_THRESHOLD: float = 0.30
    RETRIEVAL_TOP_K: int = Field(default=8, ge=1, le=50)
    RETRIEVAL_CANDIDATE_LIMIT: int = Field(default=2000, ge=1)
    BM25_WEIGHT: float = 0.35
    VECTOR_WEIGHT: float = 0.45
    SECTION_BONUS_WEIGHT: float = 0.15
    PAGE_BONUS_WEIGHT: float = 0.05
    BM25_K1: float = 1.5
    BM25_B: float = 0.75

    # ---------------- Embedding ----------------
    # local = 确定性哈希向量（默认，零外部依赖，符合 P0-12② 确定性要求）
    EMBEDDING_MODE: Literal["local", "remote"] = "local"
    EMBEDDING_DIM: int = Field(default=1536, ge=8)
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    # ---------------- 文件与存储 ----------------
    DATA_DIR: Path = Path("./data")
    MAX_UPLOAD_MB: int = Field(default=50, ge=1)
    MAX_PDF_PAGES: int = Field(default=600, ge=1)
    # PRD Q4：保留最近 20 个版本，超出后最老版本仍可查看但不可 patch
    ARTIFACT_VERSION_KEEP: int = Field(default=20, ge=1)

    # ---------------- 日志 / CORS ----------------
    LOG_LEVEL: str = "INFO"
    # 用逗号分隔字符串而不是 list[str]：pydantic-settings 对 list 字段默认按 JSON 解析，
    # 逗号分隔的朴素写法会直接抛 JSONDecodeError，踩过坑所以显式用 str + property 拆分。
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"
    APP_VERSION: str = "0.1.0"

    # ---------------- 派生属性 ----------------
    @property
    def demo_mode(self) -> bool:
        """未配置 API Key 即为演示模式。空串与纯空白都算未配置。"""
        return not self.LLM_API_KEY.strip()

    @property
    def cors_origins(self) -> list[str]:
        """CORS 白名单。空项自动剔除，避免 ["", "http://..."] 这种脏数据。"""
        return [item.strip() for item in self.CORS_ORIGINS.split(",") if item.strip()]

    @property
    def resolved_data_dir(self) -> Path:
        """DATA_DIR 的绝对路径（相对路径以 backend/ 为基准）。"""
        raw = Path(self.DATA_DIR)
        return raw if raw.is_absolute() else (BACKEND_ROOT / raw).resolve()

    @property
    def upload_dir(self) -> Path:
        return self.resolved_data_dir / "uploads"

    @property
    def artifact_dir(self) -> Path:
        return self.resolved_data_dir / "artifacts"

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

    @property
    def database_backend(self) -> str:
        """用于 /api/health 的可观测字段，例如 'sqlite+aiosqlite'。

        只做字符串展示，不参与任何业务分支（分支收敛点见 db/session.py）。
        """
        return self.DATABASE_URL.split(":", 1)[0]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例。测试通过 reset_settings_cache() + monkeypatch 环境变量切换配置。"""
    return Settings()


def reset_settings_cache() -> None:
    """仅供测试使用：清掉 lru_cache，让下次 get_settings() 重新读环境变量。"""
    get_settings.cache_clear()

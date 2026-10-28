"""全局配置。

为什么所有阈值都放这里：PRD 的验收标准（P0-05 TTFT、P0-06 路由、§7.3 Grader 三态阈值）
都是**可调参数**而不是硬编码常量。集中在一处，评审与调参时只改一个文件，
业务代码里出现魔法数字即视为不通过。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录的绝对路径。相对路径（DATA_DIR、sqlite 文件）一律以它为基准，
# 否则 `uvicorn --app-dir backend`（CWD=项目根）与 `cd backend && uvicorn`（CWD=backend）
# 会落到两个不同的 data/ 目录，出现「上传的 PDF 找不到」这类幽灵问题。
BACKEND_ROOT: Path = Path(__file__).resolve().parents[1]

# 允许在 `LLM_MODEL_ARTIFACT_BY_TYPE` 里出现的形态键。与 `schemas/artifacts.py` 的
# 判别字段取值一致；不在这里的键一律拒绝，因为「配了却没生效」比「启动失败」难查得多。
ARTIFACT_TYPES: frozenset[str] = frozenset({"ppt", "anim_2d", "scene_3d"})


def _parse_model_overrides(raw: str) -> dict[str, str]:
    """解析 `LLM_MODEL_ARTIFACT_BY_TYPE`（`k=v,k=v`）成分形态档位表。

    规则：
    - 逗号分隔，每项 `type=model`；
    - 键必须在 `ARTIFACT_TYPES` 内，否则抛 `ValueError`（fail fast：拼错的键被静默
      忽略，正是「配了却没生效」的经典形态，IMPL-NOTES E11 教训 1）；
    - 值允许空串，语义是「该形态跟随全局档位」；
    - 空串/纯空白输入返回空表（全部走全局档位）。

    纯函数、无副作用：校验（`Settings` 构造时）与读取（`artifact_model_for`）各调一次，
    这样「校验通过」与「实际使用」保证是同一套解析逻辑。
    """
    overrides: dict[str, str] = {}
    for item in (raw or "").split(","):
        piece = item.strip()
        if not piece:
            continue
        key, sep, value = piece.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(
                f"LLM_MODEL_ARTIFACT_BY_TYPE 的每一项应为 type=model，实际是 {piece!r}"
            )
        if key not in ARTIFACT_TYPES:
            raise ValueError(
                f"LLM_MODEL_ARTIFACT_BY_TYPE 的键只能是 {sorted(ARTIFACT_TYPES)}，收到 {key!r}"
            )
        overrides[key] = value.strip()
    return overrides


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
    # 三个独立配置项，语义各不相同，**不要合并**：
    # - ROUTER：轻量任务（正文流式 / 形态路由 / 相关性评估）。TTFT 靠这一档。
    # - ARTIFACT：结构化 AST 生成（`complete_json`）。实测 flash 与 v4-pro 的契约
    #   通过率相同而耗时 55.5s → 10.1s，所以默认走轻量档，见 IMPL-NOTES E12。
    # - GENERATOR：重档。既是 ARTIFACT 的回退目标（ARTIFACT 置空即跟随它），
    #   也是将来更复杂任务的落点。**默认值刻意不改**：README / .env.example /
    #   两篇 HTML 文档都把它描述为「复杂 JSON AST 用大模型」，改默认值会让那几处
    #   同时变成假话，且失去「留一个重档配置」的能力。
    LLM_MODEL_ROUTER: str = "gpt-4o-mini"
    LLM_MODEL_GENERATOR: str = "gpt-4o"
    # 空串 = 跟随 LLM_MODEL_GENERATOR（即换档前的行为），这是全局回退开关
    LLM_MODEL_ARTIFACT: str = "deepseek-flash"
    # 分形态覆盖，形如 "anim_2d=deepseek-v4-pro,scene_3d=deepseek-v4-pro"。
    # 默认留空（全局单档）：2D/3D 实测零契约失败，没有回退需求；预置分形态等于
    # 「没测就假设」。拼错的键会在构造 Settings 时抛 ValueError，不许静默忽略 ——
    # 「配了却没生效」正是 IMPL-NOTES E11 教训 1 的重演。
    LLM_MODEL_ARTIFACT_BY_TYPE: str = ""
    LLM_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0)
    LLM_MAX_RETRIES: int = Field(default=3, ge=1, le=5)

    # ---------------- 产物生成执行策略 ----------------
    # 这几个开关的存在理由都是「回退不依赖代码回滚」（PRD §5.3）：出问题时改一行
    # 环境变量就能回到已知可用状态，不需要发版。
    #
    # 关闭并行 = 回到改动前的串行路径（正文流完再生成产物）。默认开启。
    ARTIFACT_PARALLEL_ENABLED: bool = True
    # fork 时刻。`after_first_token`（默认）在首个 token 产出后才发产物请求 ——
    # TTFT 在产物请求发出之前就已测量完毕，所以「不影响 TTFT」是结构上可证的，
    # 不是靠调度运气。`immediate` 能再省约 3.9s，但它把 TTFT 交给「provider 侧并发
    # 是否排队」这个未验证假设，必须先做 A/B（TTFT P50/P90 + T4 P90 三条同时满足）
    # 才允许写进 .env，不进代码默认值。
    ARTIFACT_FORK_MODE: Literal["after_first_token", "immediate"] = "after_first_token"
    # 进程内产物 LLM 并发安全阀（0 = 不限）。定位是「多标签页同时提问」的兜底，
    # 不是调参旋钮：单回合最多 2 个并发请求，真正的限流应对是 429 → 串行降级。
    ARTIFACT_MAX_CONCURRENCY: int = Field(default=8, ge=0)
    # 产物侧遇 429 时，在正文结束后串行重试的次数（0 = 不重试，直接降级 mock）。
    ARTIFACT_RATE_LIMIT_RETRY: int = Field(default=1, ge=0, le=3)
    # 后置确定性页码重绑定（`agent_engine._rebind_spec_source_pages`）的开关。
    # 它会**改写用户可见的 source_ref / source_page**，所以按本节的既定原则留一个
    # 「改环境变量就回退」的开关，而不是只能回滚代码。
    # 关掉后回到改动前的行为：页码只由模型给出、再由 `_clamp_spec_pages` 剥离越界值。
    # 已知用途之一：想**单独**验证钳制层（QA 的钳制攻击用例）时，必须关掉上游这一层，
    # 否则「越界页码被剥成章节号」会被「越界页码被修正为真实页码」提前改写。
    CITATION_REBIND_ENABLED: bool = True

    # ---------------- 检索 / Grader（§7.2 §7.3）----------------
    GRADER_MATCH_THRESHOLD: float = 0.55
    GRADER_SUPPORT_THRESHOLD: float = 0.35
    GRADER_PARTIAL_THRESHOLD: float = 0.30
    # P0-05④ 的真正判据：问句实词在 Top-N 切片里的命中率门槛。
    # 为什么不能只调 GRADER_PARTIAL_THRESHOLD：融合分数是「相对归一化」，无关问题实测能拿到
    # 0.30~0.40，而真正的中间态问题只有 0.36，靠分数分不开（见 retriever._grade 注释）。
    # 0.45 的取值依据：实测 18 条无关问题最高 0.40（「矩阵特征值的几何意义」命中「特征」「意义」
    # 两个词面巧合），16 条相关问题最低 0.50，门槛落在两者正中，两侧各留 0.05 余量。
    GRADER_CONTENT_HIT_MIN_RATIO: float = Field(default=0.45, gt=0.0, le=1.0)
    # 参与实词命中率计算的切片数（Top-N 并集）。只取 top1 会让「答案分散在两段」的
    # 相关问题被误判为未覆盖；取太多又会把偶然字面重合算进来。
    GRADER_CONTENT_HIT_WINDOW: int = Field(default=3, ge=1, le=8)
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

    # ---------------- 校验器 ----------------
    @field_validator("LLM_MODEL_ARTIFACT_BY_TYPE")
    @classmethod
    def _validate_model_overrides(cls, value: str) -> str:
        """构造期就校验分形态档位表：宁可启动失败，也不要「以为回退了、其实没退」。"""
        _parse_model_overrides(value)
        return value

    # ---------------- 派生属性 ----------------
    @property
    def demo_mode(self) -> bool:
        """未配置 API Key 即为演示模式。空串与纯空白都算未配置。"""
        return not self.LLM_API_KEY.strip()

    @property
    def artifact_model(self) -> str:
        """产物生成的全局档位。空串 = 跟随 `LLM_MODEL_GENERATOR`（换档前行为）。"""
        return self.LLM_MODEL_ARTIFACT.strip() or self.LLM_MODEL_GENERATOR

    def artifact_model_for(self, artifact_type: str | None) -> str:
        """按形态取档位：分形态覆盖 > 全局档位 > `LLM_MODEL_GENERATOR`。

        业务代码不得自己拼字符串或读 `LLM_MODEL_ARTIFACT`：回退链只有一处实现，
        否则「改全局档位」和「改某个形态」会变成两套互相打架的逻辑。
        """
        overrides = _parse_model_overrides(self.LLM_MODEL_ARTIFACT_BY_TYPE)
        override = overrides.get(str(artifact_type or "").strip(), "")
        return override.strip() or self.artifact_model

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

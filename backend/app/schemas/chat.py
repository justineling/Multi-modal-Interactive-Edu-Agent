"""对话请求与 SSE 事件契约（§5.4）。

8 类事件的字段名与表格逐字一致，全部 snake_case。这是前后端唯一的手写契约，
前端 `types/events.ts` 与之一一对应；改名等于破坏协议，必须同步改前端。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.artifacts import ArtifactSpec, ArtifactType, Citation
from app.schemas.common import canonical_json
from app.schemas.patch import NODE_PATH_RE, PatchOp

# 每 15s 无事件时发一行注释保活（§5.4 规则④）
SSE_PING_INTERVAL_SECONDS: float = 15.0

SseEventName = Literal[
    "turn_start",
    "retrieval",
    "token",
    "modality_decision",
    "artifact",
    "citations",
    "error",
    "turn_end",
]

# 事件顺序固定（§5.4 规则②）。error 可插入任意位置，但之后仍必须补发 turn_end。
EVENT_ORDER: tuple[SseEventName, ...] = (
    "turn_start",
    "retrieval",
    "modality_decision",
    "token",
    "artifact",
    "citations",
    "turn_end",
)

GraderStatus = Literal["SYLLABUS_MATCH", "PARTIAL_OR_BEYOND", "NO_MATCH"]
StreamMode = Literal["syllabus_strict", "hybrid_extended"]
ErrorStage = Literal["retrieval", "route", "generate", "validate", "patch"]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Anchor(BaseModel):
    """画布节点锚点。P0-10①：点击元素后前端回传，输入框出现 chip。"""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1)
    node_path: str = Field(pattern=NODE_PATH_RE)
    node_label: str = ""
    page: int | None = None
    section_code: str | None = None


class ChatRequest(BaseModel):
    """POST /api/chat/stream 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=2000)
    anchor: Anchor | None = None
    # 用户显式指定形态时 100% 遵循（P0-06②）
    artifact_type_override: ArtifactType | None = None
    # PRD Q2 默认 syllabus_strict：考试不答偏优先
    mode: StreamMode = "syllabus_strict"
    # 无锚点时用于「接着上一张画布继续改」
    artifact_id: str | None = None
    history_limit: int = Field(default=8, ge=0, le=50)


# --------------------------------------------------------------------------- #
# 8 类事件
# --------------------------------------------------------------------------- #
class RetrievedChunk(_Event):
    chunk_id: str
    section_code: str
    section_title: str
    page_start: int
    page_end: int
    score: float
    snippet: str


class TurnStartEvent(_Event):
    turn_id: str
    workspace_id: str
    mode: StreamMode
    demo_mode: bool
    has_anchor: bool
    artifact_type_hint: ArtifactType | None = None


class RetrievalEvent(_Event):
    grader_status: GraderStatus
    section_scope: str | None = None
    top_score: float
    chunks: list[RetrievedChunk] = Field(default_factory=list)


class TokenEvent(_Event):
    delta: str


class ModalityDecisionEvent(_Event):
    artifact_type: Literal["ppt", "anim_2d", "scene_3d", "none"]
    action: Literal["create_artifact", "patch_artifact", "none"]
    reason: str
    user_override: bool = False


class ArtifactEvent(_Event):
    """create 与 patch 共用一个事件名，用 action 判别。

    为什么不拆成两个事件名：前端 useArtifactPatch 的归约逻辑只需处理一个事件，
    少一个分支就少一处「新增/更新走错路径」的 bug。
    """

    action: Literal["create", "patch"]
    artifact_id: str
    artifact_type: ArtifactType
    version: int = Field(ge=1)
    title: str | None = None
    spec: ArtifactSpec | None = None
    base_version: int | None = None
    ops: list[PatchOp] | None = None

    @model_validator(mode="after")
    def _action_payload_consistent(self) -> "ArtifactEvent":
        if self.action == "create":
            if self.spec is None:
                raise ValueError("action=create 必须携带完整 spec")
            if self.ops is not None or self.base_version is not None:
                raise ValueError("action=create 不应携带 ops / base_version")
        else:
            if not self.ops:
                raise ValueError("action=patch 必须携带非空 ops")
            if self.base_version is None:
                raise ValueError("action=patch 必须携带 base_version")
            if self.spec is not None:
                raise ValueError("action=patch 不应携带完整 spec（会退化成全量重生成）")
        return self


class CitationsEvent(_Event):
    citations: list[Citation] = Field(default_factory=list)


class ErrorEvent(_Event):
    code: str
    message: str
    stage: ErrorStage
    retryable: bool
    attempt: int = Field(ge=0)


class TurnEndEvent(_Event):
    turn_id: str
    duration_ms: int
    ttft_ms: int
    version: int | None = None
    demo_mode: bool


SSE_EVENT_MODELS: dict[str, type[_Event]] = {
    "turn_start": TurnStartEvent,
    "retrieval": RetrievalEvent,
    "token": TokenEvent,
    "modality_decision": ModalityDecisionEvent,
    "artifact": ArtifactEvent,
    "citations": CitationsEvent,
    "error": ErrorEvent,
    "turn_end": TurnEndEvent,
}


def encode_sse(event: SseEventName, payload: BaseModel | dict[str, Any]) -> str:
    """把一个事件序列化成 SSE 帧。

    data 必须是**单行紧凑 JSON**（§5.4 规则①）：canonical_json 用
    separators=(",", ":") 且会转义字符串里的换行，因此天然单行。
    """
    body = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return f"event: {event}\ndata: {canonical_json(body)}\n\n"


def sse_ping() -> str:
    """保活注释行。注释行不会被前端解析为事件，只用来撑住代理的空闲超时。"""
    return ": ping\n\n"


__all__ = [
    "Anchor",
    "ArtifactEvent",
    "ChatRequest",
    "CitationsEvent",
    "EVENT_ORDER",
    "ErrorEvent",
    "ErrorStage",
    "GraderStatus",
    "ModalityDecisionEvent",
    "RetrievalEvent",
    "RetrievedChunk",
    "SSE_EVENT_MODELS",
    "SSE_PING_INTERVAL_SECONDS",
    "SseEventName",
    "StreamMode",
    "TokenEvent",
    "TurnEndEvent",
    "TurnStartEvent",
    "encode_sse",
    "sse_ping",
]

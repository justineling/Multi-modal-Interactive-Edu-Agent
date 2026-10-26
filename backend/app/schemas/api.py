"""HTTP 层 DTO（§9.2 的 `data` 部分）。

为什么单独一层 DTO 而不是直接返回 ORM 对象：
1. ORM 里有 `stored_path`（服务器绝对路径）这类**不该出网**的字段；
2. 契约字段名由团队裁定（例如 `WorkspaceOut.updated_at` 映射自 `last_active_at`），
   DTO 是唯一能把「契约名」与「库内名」解耦的地方；
3. 时间统一走 `as_utc`，前端拿到的永远是带 Z 的 ISO 串。

字段名严格 snake_case，与前端 `types/api.ts` 同名同形（§9.4）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import as_utc, canonical_tree


class WorkspaceOut(BaseModel):
    """契约字段 `updated_at` = 库内 `last_active_at`（团队裁定的对外名）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class TocNodeOut(BaseModel):
    """目录树节点。`start_page`/`end_page` 为 1 基、闭区间。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    title: str
    level: int = Field(ge=1, le=4)
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    children: list["TocNodeOut"] = Field(default_factory=list)


class DocumentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str
    filename: str
    status: str
    progress: int
    page_count: int
    toc_tree: list[dict[str, Any]] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime


class DocumentStatusOut(BaseModel):
    """轮询接口的轻量返回体：前端每 1.5s 拉一次，不带 toc_tree 省流量。"""

    model_config = ConfigDict(extra="forbid")

    status: str
    progress: int
    error_code: str | None = None
    error_message: str | None = None


class ChunkOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    document_id: str
    section_code: str
    section_title: str
    level: int
    chunk_index: int
    page_start: int
    page_end: int
    content: str
    char_len: int


class MessageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str
    role: str
    content_markdown: str
    knowledge_scope: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    anchor: dict[str, Any] | None = None
    artifact_id: str | None = None
    artifact_version: int | None = None
    demo_mode: bool = False
    created_at: datetime


class ArtifactOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str
    title: str
    artifact_type: str
    version: int
    spec: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ArtifactVersionOut(BaseModel):
    """历史版本。

    `patchable` 由 §10 第 3 条决定：超出 `ARTIFACT_VERSION_KEEP` 的旧版本
    「仍可查看但不可 patch」。前端据此禁用「以此版本继续追问」按钮。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    artifact_id: str
    version: int
    spec: dict[str, Any]
    patch_ops: list[dict[str, Any]] | None = None
    origin_turn_id: str | None = None
    patchable: bool = True
    created_at: datetime


class DeletedOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: bool = True


# --------------------------------------------------------------------------- #
# ORM → DTO 转换（唯一入口，避免各 API 文件各写一遍字段映射）
# --------------------------------------------------------------------------- #
def workspace_out(workspace: Any) -> WorkspaceOut:
    return WorkspaceOut(
        id=workspace.id,
        title=workspace.title,
        created_at=as_utc(workspace.created_at) or workspace.created_at,
        updated_at=as_utc(workspace.last_active_at) or workspace.last_active_at,
    )


def document_out(document: Any) -> DocumentOut:
    return DocumentOut(
        id=document.id,
        workspace_id=document.workspace_id,
        filename=document.filename,
        status=document.status,
        progress=document.progress,
        page_count=document.page_count,
        toc_tree=list(document.toc_tree or []),
        error_code=document.error_code,
        error_message=document.error_message,
        created_at=as_utc(document.created_at) or document.created_at,
    )


def chunk_out(chunk: Any) -> ChunkOut:
    return ChunkOut(
        id=chunk.id,
        document_id=chunk.document_id,
        section_code=chunk.section_code,
        section_title=chunk.section_title,
        level=chunk.level,
        chunk_index=chunk.chunk_index,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        content=chunk.content,
        char_len=chunk.char_len,
    )


def message_out(message: Any) -> MessageOut:
    return MessageOut(
        id=message.id,
        workspace_id=message.workspace_id,
        role=message.role,
        content_markdown=message.content_markdown,
        knowledge_scope=message.knowledge_scope,
        citations=list(message.citations or []),
        anchor=message.anchor,
        artifact_id=message.artifact_id,
        artifact_version=message.artifact_version,
        demo_mode=message.demo_mode,
        created_at=as_utc(message.created_at) or message.created_at,
    )


def artifact_out(artifact: Any, *, spec: dict[str, Any] | None = None, version: int | None = None) -> ArtifactOut:
    """`spec`/`version` 可覆盖：查看历史版本时用版本行里的 spec，而元信息仍取 artifact 行。

    spec 一律经 `canonical_tree` 规范化（键排序 + 深拷贝），依据 IMPL-NOTES §E8：
    1. 与 SSE 的 `artifact` 事件（`canonical_json`，sort_keys=True）**键序**一致 ——
       客户端做等值比较不会被键序坑；
    2. 深拷贝让 DTO 与 ORM 对象解耦，读接口不可能反向污染 session 里的历史版本行。

    **但「键序一致」不等于「逐字节一致」**（QA 2026-10-26 实测，IMPL-NOTES E12 ⑦ 第 4 条）：
    SSE 的 `spec` 是 pydantic 联合模型，序列化会把 `position: [0, 1, 0]` 变成
    `[0.0, 1.0, 0.0]`；而这里直接透传库里存的原始 dict（`canonical_tree` 只排键、不做类型
    归一化）。所以 `position` / `scale` / `rotation` / `target` / 2D 的 `x` `y` 这类 float
    字段，两个出口的**文本表示**可能不同（值相同）。前端是 JS，`JSON.stringify` 对二者
    输出相同，**用户不可见**；`ArtifactEvent` 也不带 fingerprint，所以不影响 P0-10② 与
    指纹自检。要真正逐字节一致，得让落库前先过 `model_dump()` —— 但那会按字段声明顺序
    重排键，与「保持键序」的既有约定冲突，不值得为一条用户看不见的差异去动。
    """
    return ArtifactOut(
        id=artifact.id,
        workspace_id=artifact.workspace_id,
        title=artifact.title,
        artifact_type=artifact.artifact_type,
        version=version if version is not None else artifact.version,
        spec=canonical_tree(spec if spec is not None else (artifact.spec_json or {})),
        created_at=as_utc(artifact.created_at) or artifact.created_at,
        updated_at=as_utc(artifact.updated_at) or artifact.updated_at,
    )


def artifact_version_out(row: Any, *, current_version: int, keep: int) -> ArtifactVersionOut:
    return ArtifactVersionOut(
        id=row.id,
        artifact_id=row.artifact_id,
        version=row.version,
        spec=canonical_tree(row.spec_json or {}),
        patch_ops=row.patch_ops,
        origin_turn_id=row.origin_turn_id,
        patchable=row.version > current_version - keep,
        created_at=as_utc(row.created_at) or row.created_at,
    )


__all__ = [
    "ArtifactOut",
    "ArtifactVersionOut",
    "ChunkOut",
    "DeletedOut",
    "DocumentOut",
    "DocumentStatusOut",
    "MessageOut",
    "TocNodeOut",
    "WorkspaceOut",
    "artifact_out",
    "artifact_version_out",
    "chunk_out",
    "document_out",
    "message_out",
    "workspace_out",
]

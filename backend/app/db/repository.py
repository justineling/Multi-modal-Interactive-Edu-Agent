"""数据访问封装。

为什么不让 API 层直接写 SQL/ORM：三层好处
1. 双模式兼容——任何一处裸 SQL 用了 PG 语法，切 SQLite 就炸；
2. 时间统一——SQLite 读回的 DateTime 是 naive，集中在这里补 UTC tzinfo，
   业务层永远不会踩「naive 与 aware 比较抛 TypeError」；
3. 错误码统一——require_* 系列直接把「查不到」翻译成 §9.1 的 404 码。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Artifact,
    ArtifactVersion,
    Document,
    DocumentChunk,
    Message,
    Workspace,
    utcnow,
)
from app.errors import AppError, ErrorCode


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite 存 ISO 字符串，读回来是 naive；统一按 UTC 补 tzinfo。

    禁止在业务层做时区换算——一旦两处各自补 tz，前端就会看到相差 8 小时的时间。
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# --------------------------------------------------------------------------- #
# workspaces
# --------------------------------------------------------------------------- #
async def create_workspace(session: AsyncSession, title: str) -> Workspace:
    workspace = Workspace(title=title.strip() or "未命名工作区")
    session.add(workspace)
    await session.flush()
    return workspace


async def get_workspace(session: AsyncSession, workspace_id: str) -> Workspace | None:
    return await session.get(Workspace, workspace_id)


async def require_workspace(session: AsyncSession, workspace_id: str) -> Workspace:
    workspace = await get_workspace(session, workspace_id)
    if workspace is None:
        raise AppError(ErrorCode.WORKSPACE_NOT_FOUND)
    return workspace


async def list_workspaces(session: AsyncSession, *, limit: int = 50) -> list[Workspace]:
    """按最近活跃排序：刷新页面后恢复最近会话（P0-01②）依赖这个顺序。"""
    rows = (
        await session.execute(select(Workspace).order_by(Workspace.last_active_at.desc()).limit(limit))
    ).scalars().all()
    return list(rows)


async def touch_workspace(session: AsyncSession, workspace_id: str) -> None:
    workspace = await require_workspace(session, workspace_id)
    workspace.last_active_at = utcnow()


async def rename_workspace(session: AsyncSession, workspace_id: str, title: str) -> Workspace:
    workspace = await require_workspace(session, workspace_id)
    workspace.title = title.strip() or workspace.title
    return workspace


async def delete_workspace(session: AsyncSession, workspace_id: str) -> None:
    """删除工作区。切片/产物/消息靠 FK ON DELETE CASCADE 连带删除（P0-01③）。"""
    workspace = await require_workspace(session, workspace_id)
    await session.delete(workspace)
    await session.flush()


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
async def create_document(
    session: AsyncSession,
    *,
    workspace_id: str,
    filename: str,
    stored_path: str | None = None,
    page_count: int = 0,
) -> Document:
    document = Document(
        workspace_id=workspace_id,
        filename=filename,
        stored_path=stored_path,
        page_count=page_count,
        status="pending",
        progress=0,
    )
    session.add(document)
    await session.flush()
    return document


async def get_document(session: AsyncSession, document_id: str) -> Document | None:
    return await session.get(Document, document_id)


async def require_document(session: AsyncSession, document_id: str) -> Document:
    document = await get_document(session, document_id)
    if document is None:
        raise AppError(ErrorCode.DOCUMENT_NOT_FOUND)
    return document


async def list_documents(session: AsyncSession, workspace_id: str) -> list[Document]:
    rows = (
        await session.execute(
            select(Document)
            .where(Document.workspace_id == workspace_id)
            .order_by(Document.created_at.asc())
        )
    ).scalars().all()
    return list(rows)


async def update_document_status(
    session: AsyncSession,
    document_id: str,
    *,
    status: str | None = None,
    progress: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    page_count: int | None = None,
    parser_mode: str | None = None,
) -> Document:
    """状态机唯一写入口。把 pending→parsing→ready/failed 的写入收在一处，
    避免多个任务分支各写一半字段，出现「status=ready 但 progress 还是 40%」这种脏状态。"""
    document = await require_document(session, document_id)
    if status is not None:
        document.status = status
    if progress is not None:
        document.progress = max(0, min(100, progress))
    if error_code is not None:
        document.error_code = error_code
    if error_message is not None:
        document.error_message = error_message
    if page_count is not None:
        document.page_count = page_count
    if parser_mode is not None:
        document.parser_mode = parser_mode
    return document


async def save_toc_tree(session: AsyncSession, document_id: str, toc_tree: list[dict[str, Any]]) -> None:
    document = await require_document(session, document_id)
    document.toc_tree = toc_tree


# --------------------------------------------------------------------------- #
# document_chunks
# --------------------------------------------------------------------------- #
async def bulk_insert_chunks(session: AsyncSession, chunks: Sequence[DocumentChunk]) -> int:
    """批量插切片。逐条 add 而非 add_all 的差别不大，但 flush 一次能把 2000 条压到
    一两个事务往返，这是 P0-02「60 秒内 ready」的关键路径之一。"""
    for chunk in chunks:
        if not chunk.section_code:
            # 兜底断言：P0-04① 要求 100% 命中，任何空值都是上游 bug，就地炸掉比入库更好
            raise AppError(ErrorCode.PDF_PARSE_FAILED, "切片缺少 section_code（不允许空值）")
        if chunk.char_len == 0:
            chunk.char_len = len(chunk.content)
        session.add(chunk)
    await session.flush()
    return len(chunks)


async def list_chunks_by_document(session: AsyncSession, document_id: str) -> list[DocumentChunk]:
    rows = (
        await session.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
        )
    ).scalars().all()
    return list(rows)


async def list_chunks_by_ids(session: AsyncSession, chunk_ids: Iterable[str]) -> list[DocumentChunk]:
    ids = list(chunk_ids)
    if not ids:
        return []
    rows = (
        await session.execute(select(DocumentChunk).where(DocumentChunk.id.in_(ids)))
    ).scalars().all()
    return list(rows)


async def list_chunks_by_section_prefix(
    session: AsyncSession, workspace_id: str, section_prefix: str, *, limit: int = 2000
) -> list[DocumentChunk]:
    rows = (
        await session.execute(
            select(DocumentChunk)
            .where(
                DocumentChunk.workspace_id == workspace_id,
                DocumentChunk.section_code.like(f"{section_prefix}%"),
            )
            .order_by(DocumentChunk.chunk_index.asc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def count_chunks(session: AsyncSession, document_id: str) -> int:
    total = await session.scalar(
        select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )
    return int(total or 0)


async def delete_chunks_by_document(session: AsyncSession, document_id: str) -> None:
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))


# --------------------------------------------------------------------------- #
# artifacts & versions
# --------------------------------------------------------------------------- #
async def create_artifact(
    session: AsyncSession,
    *,
    workspace_id: str,
    title: str,
    artifact_type: str,
    spec_json: dict[str, Any],
    anchor_section: str | None = None,
) -> Artifact:
    artifact = Artifact(
        workspace_id=workspace_id,
        title=title,
        artifact_type=artifact_type,
        version=1,
        spec_json=spec_json,
        anchor_section=anchor_section,
    )
    session.add(artifact)
    await session.flush()
    return artifact


async def get_artifact(session: AsyncSession, artifact_id: str) -> Artifact | None:
    return await session.get(Artifact, artifact_id)


async def require_artifact(session: AsyncSession, artifact_id: str) -> Artifact:
    artifact = await get_artifact(session, artifact_id)
    if artifact is None:
        raise AppError(ErrorCode.ARTIFACT_NOT_FOUND)
    return artifact


async def list_artifacts(session: AsyncSession, workspace_id: str, *, limit: int = 50) -> list[Artifact]:
    rows = (
        await session.execute(
            select(Artifact)
            .where(Artifact.workspace_id == workspace_id)
            .order_by(Artifact.updated_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def apply_artifact_patch(
    session: AsyncSession,
    *,
    artifact: Artifact,
    spec_json: dict[str, Any],
    patch_ops: list[dict[str, Any]] | None,
    origin_turn_id: str | None = None,
) -> ArtifactVersion:
    """把一次 patch 落库：artifacts 快照前移 + 追加一条不可变版本。

    两件事必须同事务：先追加版本再更新快照，任何一步失败都不能留下
    「version=4 但 versions 表里没有第 4 版」的破洞（P0-11①）。
    """
    new_version = artifact.version + 1
    artifact.version = new_version
    artifact.spec_json = spec_json
    artifact.updated_at = utcnow()

    version_row = ArtifactVersion(
        artifact_id=artifact.id,
        version=new_version,
        spec_json=spec_json,
        patch_ops=patch_ops,
        origin_turn_id=origin_turn_id,
    )
    session.add(version_row)
    await session.flush()
    return version_row


async def append_artifact_version(
    session: AsyncSession,
    *,
    artifact_id: str,
    spec_json: dict[str, Any],
    patch_ops: list[dict[str, Any]] | None = None,
    origin_turn_id: str | None = None,
) -> ArtifactVersion:
    """仅追加版本行（创建 artifact 时写 version=1）。append-only：没有 UPDATE/DELETE 入口。"""
    version_row = ArtifactVersion(
        artifact_id=artifact_id,
        version=1,
        spec_json=spec_json,
        patch_ops=patch_ops,
        origin_turn_id=origin_turn_id,
    )
    session.add(version_row)
    await session.flush()
    return version_row


async def list_artifact_versions(session: AsyncSession, artifact_id: str) -> list[ArtifactVersion]:
    """按版本号升序返回。前端 VersionMenu 直接渲染，不需要二次排序。"""
    rows = (
        await session.execute(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact_id)
            .order_by(ArtifactVersion.version.asc())
        )
    ).scalars().all()
    return list(rows)


async def get_artifact_version(
    session: AsyncSession, artifact_id: str, version: int
) -> ArtifactVersion | None:
    return (
        await session.execute(
            select(ArtifactVersion).where(
                ArtifactVersion.artifact_id == artifact_id,
                ArtifactVersion.version == version,
            )
        )
    ).scalar_one_or_none()


async def delete_artifact(session: AsyncSession, artifact_id: str) -> None:
    """删除整个 Artifact（版本随 FK 级联删除）。这是唯一允许让历史版本消失的路径。"""
    artifact = await require_artifact(session, artifact_id)
    await session.delete(artifact)
    await session.flush()


# --------------------------------------------------------------------------- #
# messages
# --------------------------------------------------------------------------- #
async def add_message(
    session: AsyncSession,
    *,
    workspace_id: str,
    role: str,
    content_markdown: str = "",
    knowledge_scope: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    anchor: dict[str, Any] | None = None,
    artifact_id: str | None = None,
    artifact_version: int | None = None,
    demo_mode: bool = False,
) -> Message:
    message = Message(
        workspace_id=workspace_id,
        role=role,
        content_markdown=content_markdown,
        knowledge_scope=knowledge_scope,
        citations=citations or [],
        anchor=anchor,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        demo_mode=demo_mode,
    )
    session.add(message)
    await session.flush()
    return message


async def list_messages(
    session: AsyncSession, workspace_id: str, *, limit: int = 100
) -> list[Message]:
    """按时间升序返回最近 limit 条（用于刷新后恢复会话）。"""
    rows = (
        await session.execute(
            select(Message)
            .where(Message.workspace_id == workspace_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(reversed(rows))

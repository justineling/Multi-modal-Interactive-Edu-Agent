"""SQLAlchemy 2.0 声明式模型（双模式同一套）。

§4.1 列出了 7 张表（文档标题写「6 张表」，实际表格是 7 行：workspaces / documents /
document_chunks / chunk_vectors / artifacts / artifact_versions / messages）。
按表格实际定义实现，见汇报中的偏差说明。

双模式兼容的三个硬规则（§4.2）：
1. UUID 一律 String(36) + Python 侧 uuid4()。不用 gen_random_uuid()，SQLite 没这个函数。
2. JSON 一律 sqlalchemy.JSON。禁止写 JSONB —— 那是 PG 专属类型，SQLite 直接建表失败。
3. 时间一律 DateTime(timezone=True) + 应用层写 UTC aware 值。SQLite 读回来是 naive，
   由 repository 出口统一补 tzinfo，业务层不做时区换算。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def new_uuid() -> str:
    """存储主键。允许用 uuid4：它只做行标识，不参与 AST 内容，不影响生成确定性。"""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """统一用 aware UTC 写库，避免 naive/aware 混用导致比较时抛 TypeError。"""
    return datetime.now(timezone.utc)


class Workspace(Base):
    """对话工作区。P0-01：workspace 即隔离单位，删除后其切片与 artifact 不可再被检索。"""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # 用于「刷新后恢复最近会话」排序（P0-01②）
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    documents: Mapped[list["Document"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan", passive_deletes=True
    )
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan", passive_deletes=True
    )
    messages: Mapped[list["Message"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan", passive_deletes=True
    )


class Document(Base):
    """上传的 PDF 及其解析状态机（pending→parsing→ready/failed/cancelled）。"""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # pending | parsing | ready | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    toc_tree: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    # regex | page_fallback —— 让「章节号识别降级」可观测（§7.1）
    parser_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    workspace: Mapped[Workspace] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("idx_doc_ws", "workspace_id"),)


class DocumentChunk(Base):
    """按章节切片的文本块。section_code 禁止为空（P0-04① 100% 命中）。"""

    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    # NOT NULL：识别失败也必须落到 page_fallback 的 "P0001" 这类编码，永不为空
    section_code: Mapped[str] = mapped_column(String(50), nullable=False)
    section_title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # chunk_index 保证同分排序稳定（§7.2 阶段 4）
    chunk_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_len: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("idx_chunk_ws_section", "workspace_id", "section_code"),
        Index("idx_chunk_doc_index", "document_id", "chunk_index"),
    )


class ChunkVector(Base):
    """切片向量。vector 列存 JSON 数组文本，是两种模式的**规范数据源**。

    Postgres 模式下 PgVectorStore 会额外维护 embedding_vec vector(1536) 作为索引镜像，
    但重建索引随时可以从本表重放，所以本表永远是真相。
    """

    __tablename__ = "chunk_vectors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    chunk_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    document_id: Mapped[str] = mapped_column(String(36), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, default=1536, nullable=False)
    vector: Mapped[str] = mapped_column(Text, nullable=False)
    # 换 embedding 模型时可据此只重建失效向量
    model: Mapped[str] = mapped_column(String(80), default="local-hash-1536", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("idx_vec_ws", "workspace_id"),
        UniqueConstraint("chunk_id", name="uq_vec_chunk"),
    )


class Artifact(Base):
    """画布产物。spec_json 是当前版本的冗余快照（读快），真相在 artifact_versions。"""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # ppt | anim_2d | scene_3d
    artifact_type: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 后续追问的默认检索范围（§4.1）
    anchor_section: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    workspace: Mapped[Workspace] = relationship(back_populates="artifacts")
    versions: Mapped[list["ArtifactVersion"]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("idx_art_ws", "workspace_id"),)


class ArtifactVersion(Base):
    """append-only 版本表（P0-11③：历史版本不可被覆盖或删除）。

    刻意不设 onupdate、不提供 UPDATE/DELETE 的 repository 方法：append-only 靠
    「没有写入口」保证，而不是靠调用方自觉。
    """

    __tablename__ = "artifact_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # 产生该版本时的 PatchOp 列表；全量生成时为 None
    patch_ops: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    origin_turn_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    artifact: Mapped[Artifact] = relationship(back_populates="versions")

    __table_args__ = (UniqueConstraint("artifact_id", "version", name="uq_ver"),)


class Message(Base):
    """对话消息。支撑 P0-01② 会话恢复与 P0-12 演示模式可追溯。"""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    # user | assistant
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # syllabus_strict | hybrid_extended | none
    knowledge_scope: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # [{label, section_code, page_start, page_end}]
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    # {artifact_id, node_path, node_label, page, section_code}
    anchor: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    artifact_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    demo_mode: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    workspace: Mapped[Workspace] = relationship(back_populates="messages")

    __table_args__ = (Index("idx_msg_ws_created", "workspace_id", "created_at"),)


__all__ = [
    "Artifact",
    "ArtifactVersion",
    "ChunkVector",
    "Document",
    "DocumentChunk",
    "Message",
    "Workspace",
    "new_uuid",
    "utcnow",
]

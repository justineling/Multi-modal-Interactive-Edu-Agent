"""数据层测试：建表、MemoryVectorStore 增删查、工厂选择。

对应 A2 完成定义：6+1 张表建表成功；MemoryVectorStore 的 upsert/search/delete 单测通过；
build_vector_store() 在 DATABASE_URL 两种取值下返回不同实现且不抛错。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from app.config import Settings
from app.db import repository
from app.db.models import DocumentChunk
from app.db.session import get_session_factory
from app.db.vector_store import (
    EmbeddingStore,
    MemoryVectorStore,
    PgVectorStore,
    VectorItem,
    build_vector_store,
    _to_asyncpg_dsn,
)
from app.errors import AppError, ErrorCode

EXPECTED_TABLES = {
    "workspaces",
    "documents",
    "document_chunks",
    "chunk_vectors",
    "artifacts",
    "artifact_versions",
    "messages",
}


async def test_init_db_creates_all_tables(db_env: Path) -> None:
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        ).scalars().all()
    assert EXPECTED_TABLES <= set(rows)


async def test_sqlite_foreign_keys_enabled(db_env: Path) -> None:
    """foreign_keys=ON 必须生效，否则 P0-01③ 的级联删除只是纸面约定。"""
    factory = get_session_factory()
    async with factory() as session:
        enabled = (await session.execute(text("PRAGMA foreign_keys"))).scalar()
    assert int(enabled or 0) == 1


async def _seed_chunks(factory: Any) -> tuple[str, str, list[str]]:
    """建一个 workspace + 一份文档 + 两个不同章节的切片。"""
    async with factory() as session:
        workspace = await repository.create_workspace(session, "向量测试")
        document = await repository.create_document(
            session, workspace_id=workspace.id, filename="sample.pdf"
        )
        chunks = [
            DocumentChunk(
                document_id=document.id,
                workspace_id=workspace.id,
                section_code="4.1",
                section_title="Malayan Union",
                level=2,
                chunk_index=0,
                page_start=112,
                page_end=114,
                content="Malayan Union 由英国在 1946 年成立。",
            ),
            DocumentChunk(
                document_id=document.id,
                workspace_id=workspace.id,
                section_code="4.2",
                section_title="马来亚联合邦",
                level=2,
                chunk_index=1,
                page_start=118,
                page_end=120,
                content="马来亚联合邦在 1948 年成立。",
            ),
        ]
        await repository.bulk_insert_chunks(session, chunks)
        await session.commit()
        return workspace.id, document.id, [chunk.id for chunk in chunks]


async def test_memory_vector_store_roundtrip(db_env: Path) -> None:
    factory = get_session_factory()
    workspace_id, document_id, chunk_ids = await _seed_chunks(factory)

    store = MemoryVectorStore(dim=4, session_factory=factory)
    await store.initialize()
    await store.upsert_vectors(
        [
            VectorItem(
                chunk_id=chunk_ids[0],
                workspace_id=workspace_id,
                document_id=document_id,
                vector=[1.0, 0.0, 0.0, 0.0],
                model="test",
            ),
            VectorItem(
                chunk_id=chunk_ids[1],
                workspace_id=workspace_id,
                document_id=document_id,
                vector=[0.0, 1.0, 0.0, 0.0],
                model="test",
            ),
        ]
    )

    hits = await store.search_similar(workspace_id, [0.9, 0.1, 0.0, 0.0], top_k=2)
    assert [hit.chunk_id for hit in hits] == [chunk_ids[0], chunk_ids[1]]
    assert hits[0].score > hits[1].score
    assert hits[0].workspace_id == workspace_id

    # 章节前缀过滤：只应命中 4.1
    filtered = await store.search_similar(
        workspace_id, [0.0, 1.0, 0.0, 0.0], top_k=2, section_prefix="4.1"
    )
    assert [hit.chunk_id for hit in filtered] == [chunk_ids[0]]

    # 新实例必须能从库里重建索引（进程重启后仍可检索）
    reloaded = MemoryVectorStore(dim=4, session_factory=factory)
    await reloaded.initialize()
    assert (await reloaded.search_similar(workspace_id, [1.0, 0.0, 0.0, 0.0], top_k=1))[
        0
    ].chunk_id == chunk_ids[0]

    # 删除整篇文档后不可再被检索
    await reloaded.delete_by_document(document_id)
    assert await reloaded.search_similar(workspace_id, [1.0, 0.0, 0.0, 0.0], top_k=2) == []


async def test_memory_vector_store_upsert_is_idempotent(db_env: Path) -> None:
    """同一 chunk_id 重复 upsert 只能有一行（uq_vec_chunk），否则检索会出现重复命中。"""
    factory = get_session_factory()
    workspace_id, document_id, chunk_ids = await _seed_chunks(factory)
    store = MemoryVectorStore(dim=4, session_factory=factory)
    await store.initialize()

    item = VectorItem(
        chunk_id=chunk_ids[0],
        workspace_id=workspace_id,
        document_id=document_id,
        vector=[1.0, 0.0, 0.0, 0.0],
        model="test",
    )
    await store.upsert_vectors([item])
    await store.upsert_vectors(
        [
            VectorItem(
                chunk_id=chunk_ids[0],
                workspace_id=workspace_id,
                document_id=document_id,
                vector=[0.0, 1.0, 0.0, 0.0],
                model="test",
            )
        ]
    )

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT COUNT(*) FROM chunk_vectors WHERE chunk_id = :cid"),
                {"cid": chunk_ids[0]},
            )
        ).scalar()
    assert count == 1

    hits = await store.search_similar(workspace_id, [0.0, 1.0, 0.0, 0.0], top_k=1)
    assert hits[0].chunk_id == chunk_ids[0]


async def test_empty_section_code_rejected(db_env: Path) -> None:
    """P0-04①：section_code 不允许为空，入库前就该炸。"""
    factory = get_session_factory()
    async with factory() as session:
        workspace = await repository.create_workspace(session, "空章节")
        document = await repository.create_document(
            session, workspace_id=workspace.id, filename="x.pdf"
        )
        with pytest.raises(AppError) as excinfo:
            await repository.bulk_insert_chunks(
                session,
                [
                    DocumentChunk(
                        document_id=document.id,
                        workspace_id=workspace.id,
                        section_code="",
                        chunk_index=0,
                        page_start=1,
                        page_end=1,
                        content="内容",
                    )
                ],
            )
    assert excinfo.value.code is ErrorCode.PDF_PARSE_FAILED


def test_build_vector_store_selects_implementation() -> None:
    sqlite_store = build_vector_store(Settings(DATABASE_URL="sqlite+aiosqlite:///./data/app.db"))
    postgres_store = build_vector_store(
        Settings(DATABASE_URL="postgresql+asyncpg://user:pass@127.0.0.1:5432/edu")
    )
    assert isinstance(sqlite_store, MemoryVectorStore)
    assert isinstance(postgres_store, PgVectorStore)
    # 协议实现检查：业务代码只依赖 EmbeddingStore
    assert isinstance(sqlite_store, EmbeddingStore)
    assert isinstance(postgres_store, EmbeddingStore)


def test_asyncpg_dsn_strips_sqlalchemy_driver() -> None:
    assert (
        _to_asyncpg_dsn("postgresql+asyncpg://user:pass@127.0.0.1:5432/edu")
        == "postgresql://user:pass@127.0.0.1:5432/edu"
    )

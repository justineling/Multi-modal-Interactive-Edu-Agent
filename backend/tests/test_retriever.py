"""混合检索与 Grader 测试（P0-04③ / P0-05④）。

数据全部来自真实样本 PDF 的解析 + 切片 + 向量化，测的是端到端可用性，
而不是「mock 掉检索器后断言它被调用了」这种假测试。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.db import repository
from app.db.models import DocumentChunk
from app.db.session import get_session_factory
from app.db.vector_store import VectorItem, get_vector_store
from app.services.chunker import chunk_document
from app.services.embedding import get_embedding_provider
from app.services.pdf_parser import parse_pdf
from app.services.retriever import (
    NO_MATCH,
    PARTIAL_OR_BEYOND,
    SYLLABUS_MATCH,
    Retriever,
    extract_section_code,
    get_retriever,
)


async def _seed_workspace(title: str, pdf_path: Path) -> SimpleNamespace:
    """解析 → 切片 → 入库 → 向量化，返回可用的检索上下文。"""
    parsed = parse_pdf(pdf_path)
    drafts = chunk_document(parsed)
    factory = get_session_factory()
    store = get_vector_store()
    await store.initialize()
    provider = get_embedding_provider()

    async with factory() as session:
        workspace = await repository.create_workspace(session, title)
        document = await repository.create_document(
            session, workspace_id=workspace.id, filename=pdf_path.name, page_count=parsed.page_count
        )
        rows = [
            DocumentChunk(
                **draft.to_model_kwargs(document_id=document.id, workspace_id=workspace.id)
            )
            for draft in drafts
        ]
        await repository.bulk_insert_chunks(session, rows)
        await repository.save_toc_tree(session, document.id, list(parsed.toc_tree))
        await session.commit()
        chunk_ids = [row.id for row in rows]

    vectors = await provider.embed([draft.content for draft in drafts])
    await store.upsert_vectors(
        [
            VectorItem(
                chunk_id=chunk_id,
                workspace_id=workspace.id,
                document_id=document.id,
                vector=vector,
                model=provider.model_name,
            )
            for chunk_id, vector in zip(chunk_ids, vectors)
        ]
    )
    return SimpleNamespace(
        workspace_id=workspace.id,
        document_id=document.id,
        parsed=parsed,
        chunk_ids=chunk_ids,
        store=store,
        provider=provider,
    )


@pytest.fixture()
async def seeded(db_env: Path, sample_pdf_path: Path) -> SimpleNamespace:
    return await _seed_workspace("Sejarah Form 4", sample_pdf_path)


@pytest.fixture()
def retriever(seeded: SimpleNamespace) -> Retriever:
    return Retriever(seeded.store, seeded.provider)


# --------------------------------------------------------------------------- #
# P0-04③：Top-5 同章 + 页码正确
# --------------------------------------------------------------------------- #
async def test_top5_all_from_same_chapter(seeded: SimpleNamespace, retriever: Retriever) -> None:
    result = await retriever.retrieve(seeded.workspace_id, "Malayan Union 苏丹")

    assert len(result.chunks) >= 5
    chapters = {chunk.section_code.split(".")[0] for chunk in result.chunks[:5]}
    assert chapters == {"4"}, f"Top-5 应全部来自第 4 章，实际 {chapters}"


async def test_top_pages_are_traceable(seeded: SimpleNamespace, retriever: Retriever) -> None:
    result = await retriever.retrieve(seeded.workspace_id, "Malayan Union 苏丹")
    sections = {section.code: section for section in seeded.parsed.sections}

    for chunk in result.chunks:
        section = sections[chunk.section_code]
        assert section.start_page <= chunk.page_start
        assert chunk.page_end <= section.end_page
        assert chunk.content.strip(), "命中结果必须带正文（供后续生成引用）"


async def test_section_scope_filter(seeded: SimpleNamespace, retriever: Retriever) -> None:
    result = await retriever.retrieve(seeded.workspace_id, "讲了什么", section_code="4.2")
    assert result.chunks
    assert all(chunk.section_code.startswith("4.2") for chunk in result.chunks)


async def test_strict_section_locks_scope_for_anchor_turn(
    seeded: SimpleNamespace, retriever: Retriever
) -> None:
    """带锚点的追问必须锁死在被点中的小节（§6.2），否则引用页码会跟节点对不上。"""
    result = await retriever.retrieve(
        seeded.workspace_id, "这里再细一点", section_code="4.1.3", page=76, strict_section=True
    )
    assert result.chunks
    assert all(chunk.section_code.startswith("4.1.3") for chunk in result.chunks)
    assert result.grader_status == SYLLABUS_MATCH


async def test_scores_are_sorted_desc(seeded: SimpleNamespace, retriever: Retriever) -> None:
    result = await retriever.retrieve(seeded.workspace_id, "Malayan Union 苏丹")
    scores = [chunk.score for chunk in result.chunks]
    assert scores == sorted(scores, reverse=True)
    assert result.top_score == scores[0]


# --------------------------------------------------------------------------- #
# P0-01①：workspace 隔离（绝不串台）
# --------------------------------------------------------------------------- #
async def test_workspace_isolation(
    seeded: SimpleNamespace, retriever: Retriever, sample_pdf_path: Path
) -> None:
    # 同一个 PDF 传进两个 workspace：内容完全一样，但检索结果必须严格隔离
    other = await _seed_workspace("另一本教材", sample_pdf_path)

    result = await retriever.retrieve(seeded.workspace_id, "Malayan Union 苏丹")
    other_result = await retriever.retrieve(other.workspace_id, "Malayan Union 苏丹")

    seeded_ids = set(seeded.chunk_ids)
    other_ids = set(other.chunk_ids)
    assert all(chunk.chunk_id in seeded_ids for chunk in result.chunks)
    assert all(chunk.chunk_id in other_ids for chunk in other_result.chunks)
    assert not (seeded_ids & other_ids)


async def test_unknown_workspace_returns_no_match(db_env: Path) -> None:
    retriever = get_retriever()
    result = await retriever.retrieve("not-a-workspace", "Malayan Union 苏丹")
    assert result.chunks == ()
    assert result.grader_status == NO_MATCH


# --------------------------------------------------------------------------- #
# §7.3 Grader 三态
# --------------------------------------------------------------------------- #
async def test_grader_syllabus_match(seeded: SimpleNamespace, retriever: Retriever) -> None:
    result = await retriever.retrieve(seeded.workspace_id, "4.1 Malayan Union 苏丹")
    assert result.grader_status == SYLLABUS_MATCH
    assert result.top_score >= 0.55


async def test_grader_syllabus_match_by_section_anchor(
    seeded: SimpleNamespace, retriever: Retriever
) -> None:
    """「学习 4.1」这种没有词面重叠的问句，靠元数据锚定也要判为严格匹配。"""
    result = await retriever.retrieve(seeded.workspace_id, "学习 4.1")
    assert result.grader_status == SYLLABUS_MATCH
    assert all(chunk.section_code.startswith("4.1") for chunk in result.chunks[:2])


async def test_grader_partial_or_beyond(seeded: SimpleNamespace, retriever: Retriever) -> None:
    """有明确词面命中但没点名章节 → 中间态，回复要走双层结构。"""
    result = await retriever.retrieve(seeded.workspace_id, "Malayan Union 苏丹")
    assert result.grader_status == PARTIAL_OR_BEYOND
    assert 0.30 <= result.top_score < 0.55


async def test_grader_no_match(seeded: SimpleNamespace, retriever: Retriever) -> None:
    """P0-05④：无关问题必须判 NO_MATCH，而不是硬凑一段带页码的回答。"""
    result = await retriever.retrieve(seeded.workspace_id, "量子纠缠的退相干时间是多少")
    assert result.grader_status == NO_MATCH
    assert result.top_score < 0.30


# --------------------------------------------------------------------------- #
# 章节号抽取
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("学习 4.1", "4.1"),
        ("4.1 讲了什么", "4.1"),
        ("出 5 道关于 4.2 的随堂测验", "4.2"),
        ("讲讲 4.1.2 的苏丹权力", "4.1.2"),
        ("1945 到 1948 发生了什么", None),
        ("帮我梳理要点", None),
    ],
)
def test_extract_section_code(text: str, expected: str | None) -> None:
    assert extract_section_code(text) == expected


async def test_bm25_prefers_exact_term_over_unrelated(
    seeded: SimpleNamespace, retriever: Retriever
) -> None:
    """同一 workspace 内，「Perjanjian 1948」应把 4.2.2 顶到第一。"""
    result = await retriever.retrieve(seeded.workspace_id, "Perjanjian 1948", section_code="4.2")
    assert result.chunks[0].section_code == "4.2.2"

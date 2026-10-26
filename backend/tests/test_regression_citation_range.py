"""页码引用后校验的回归用例（P0-05④「禁止编造页码」的最后一道闸门）。

背景：真实 DeepSeek 链路首次跑通后发现，模型习惯输出 `[p.74-75]` 这种**范围**写法，
而 `_PAGE_CITATION_RE` 当时只写了 `\\[p\\.(\\d+)\\]`（单页），于是：
1. 范围引用一条都匹配不上；
2. 「正文引用了检索集外页码」这道后校验被**静默跳过** —— 模型写 `[p.999-1000]` 也拦不住，
   而日志里连一条 `invalid_citation` 都不会出现（这比误报更危险：没人会发现它坏了）。

本文件只测 `_drop_invalid_citations` 这一个纯函数，不碰检索、不碰 LLM、不碰数据库：
它不依赖任何外部状态，构造 `RetrievalResult` 就足够把四种情形钉死。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.config import get_settings
from app.schemas.artifacts import Citation
from app.services.agent_engine import AgentEngine
from app.services.retriever import SYLLABUS_MATCH, RetrievalResult, RetrievedChunk


def _engine() -> AgentEngine:
    """只构造一个能调纯方法的引擎。

    依赖全部塞成占位对象：本文件测的 `_drop_invalid_citations` 不碰 retriever / llm /
    session / vector store，真去初始化它们会连数据库和向量库，把一个纯函数测试变成集成测试。
    """
    return AgentEngine(
        settings=get_settings(),
        session_factory=lambda: None,  # type: ignore[arg-type]
        retriever=object(),  # type: ignore[arg-type]
        llm=object(),  # type: ignore[arg-type]
        store=object(),
    )


def _result(*chunks: RetrievedChunk) -> RetrievalResult:
    return RetrievalResult(
        chunks=chunks,
        grader_status=SYLLABUS_MATCH,
        section_scope="4.1",
        top_score=0.62,
        query_terms=("苏丹",),
    )


def _chunk(page_start: int, page_end: int) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chunk-{page_start}-{page_end}",
        section_code="4.1.3",
        section_title="Reaksi Masyarakat",
        page_start=page_start,
        page_end=page_end,
        score=0.62,
        snippet="1946 年 3 月，全马马来人代表大会在吉隆坡召开。",
        content="1946 年 3 月，全马马来人代表大会在吉隆坡召开。",
    )


def _invalid_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """从捕获的日志里挑出 `invalid_citation` 事件，返回它们的 extra 字段。"""
    return [
        record.extra_fields  # type: ignore[attr-defined]
        for record in caplog.records
        if getattr(record, "event", None) == "invalid_citation"
    ]


def test_legal_page_range_is_kept(caplog: pytest.LogCaptureFixture) -> None:
    """合法的范围引用 `[p.76-77]` 不得被误判 —— 这是修正则最容易踩的坑。

    `page_end=77` 只在切片区间里出现，不在任何 chunk 的 page_start 上；
    若按「page_start 集合」判合法就会把这条真引用误杀。
    """
    caplog.set_level(logging.WARNING, logger="edu")
    result = _result(_chunk(76, 77))
    citations = [Citation(label="Reaksi Masyarakat（p.76-77）", section_code="4.1.3", page_start=76, page_end=77)]

    kept = _engine()._drop_invalid_citations(  # noqa: SLF001 - 被测私有方法
        "马来社会强烈反对该计划 [p.76-77]。", citations, result
    )

    assert kept == citations, "合法的范围引用被误判为编造"
    assert _invalid_events(caplog) == [], "合法引用不应产生 invalid_citation 告警"


def test_fabricated_page_range_is_flagged_and_citation_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """编造的范围页码 `[p.999-1000]` 必须被识别，且对应引用被剔除。

    修复前：正则匹配不到 `[p.999-1000]`，`invalid` 恒为空集，
    既不打日志也不剔除引用 —— 这就是「编造页码拦不住」的具体形态。
    """
    caplog.set_level(logging.WARNING, logger="edu")
    result = _result(_chunk(74, 75))
    fabricated = Citation(label="编造的出处", section_code="4.1.3", page_start=999, page_end=1000)

    kept = _engine()._drop_invalid_citations(  # noqa: SLF001 - 被测私有方法
        "苏丹的权力被大幅削减 [p.999-1000]。", [fabricated], result
    )

    assert kept == [], "引用端点落在检索集之外，必须被剔除"
    events = _invalid_events(caplog)
    assert len(events) == 1, f"应恰好产生一条 invalid_citation 告警，实际 {len(events)} 条"
    assert events[0]["invalid_pages"] == [999, 1000], (
        f"两个越界端点都应被记录，实际 {events[0]['invalid_pages']}"
    )
    assert events[0]["allowed"] == [74, 75], "告警里应带上合法页码区间，便于线上排查"


def test_half_fabricated_range_counts_as_invalid(caplog: pytest.LogCaptureFixture) -> None:
    """「真页码 + 凑数尾巴」`[p.76-1000]`：只要有一个端点越界，整条引用即视为编造。

    这是模型编页码最常见的形态（前半段抄课本、后半段瞎凑），
    做「部分合法」的宽容等于给它留后门。
    """
    caplog.set_level(logging.WARNING, logger="edu")
    result = _result(_chunk(76, 77))

    kept = _engine()._drop_invalid_citations(  # noqa: SLF001 - 被测私有方法
        "代表大会在吉隆坡召开 [p.76-1000]。", [], result
    )

    assert kept == []
    events = _invalid_events(caplog)
    assert len(events) == 1
    assert events[0]["invalid_pages"] == [1000], "越界的那个端点必须被记下来"


def test_single_page_marker_still_supported(caplog: pytest.LogCaptureFixture) -> None:
    """单页写法 `[p.76]` 是旧行为，改正则时不能把它改坏。"""
    caplog.set_level(logging.WARNING, logger="edu")
    result = _result(_chunk(76, 77))

    legal = _engine()._drop_invalid_citations("课本原文 [p.76]。", [], result)  # noqa: SLF001
    assert legal == []
    assert _invalid_events(caplog) == []

    caplog.clear()
    _engine()._drop_invalid_citations("凭空一句 [p.999]。", [], result)  # noqa: SLF001
    events = _invalid_events(caplog)
    assert len(events) == 1
    assert events[0]["invalid_pages"] == [999]

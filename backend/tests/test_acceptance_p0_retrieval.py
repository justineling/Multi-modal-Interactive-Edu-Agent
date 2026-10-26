"""验收测试 · 检索与「课本依据」红线。

**本文件验证 PRD 的哪几条**：
- P0-05④：检索无命中必须降级为「课本未覆盖」，禁止编造页码 —— 无关问题必须判 `NO_MATCH`
- P0-05②：`syllabus_strict` 回答里每条事实性陈述都带 `[p.页码]`
- P0-05③：`hybrid_extended` 必须显式声明「含超纲内容」并用可区分的形式标出拓展部分
- P0-04①：全库不存在 `section_code` 为空的切片（含 page_fallback 降级场景）
- P0-01①③：workspace 隔离（同一 PDF 两个 workspace 检索结果零交集）与删除后不可检索
- P0-03③ 的兜底：章节号识别失败时走 `page_fallback`，切片仍带非空 `section_code`
- IMPL-NOTES §E1/§E2/§E3 三条「实施期已修正」的回归

数据全部来自 `scripts/make_sample_pdf.py` 生成的真实 PDF，经 HTTP 接口上传解析，
不走任何内部捷径 —— 这样测的是用户实际走的那条路。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from acceptance_support import (
    BRACKET_PAGE_RE,
    PAGE_MARKER_RE,
    UNRELATED_QUERIES,
    build_pdf_without_headings,
    fact_lines,
    first_event,
    reply_text,
    seed_workspace_with_pdf,
    sse_events,
    upload_pdf,
    wait_terminal,
)
from app.db.models import DocumentChunk
from app.db.session import get_session_factory
from app.services.retriever import get_retriever

RELATED_QUERY = "4.1 Malayan Union 对苏丹的影响"
EXPLICIT_SECTION_QUERY = "学习 4.1"
NO_MATCH_QUERY = "量子纠缠的退相干时间是多少"


@pytest.fixture()
async def seeded(client: Any, sample_pdf_path: Path) -> dict[str, Any]:
    """一个已上传 113 页样本教材并解析完成的 workspace。"""
    return await seed_workspace_with_pdf(client, sample_pdf_path)


# --------------------------------------------------------------------------- #
# P0-05④：无关问题不得被当成「课本有覆盖」
# --------------------------------------------------------------------------- #
async def test_unrelated_queries_must_be_no_match(seeded: dict[str, Any]) -> None:
    """IMPL-NOTES §E1 的回归：至少 14 条无关问题都不能漏判成 PARTIAL_OR_BEYOND。

    漏判的后果不是「分数难看」，而是用户问「怎么做红烧肉」会收到一份
    「【课本依据】+ [p.105]」的历史课 PPT —— 这正是 P0-05④ 明令禁止的编造页码。
    """
    retriever = get_retriever()
    misclassified: list[tuple[str, str, float]] = []

    for query in UNRELATED_QUERIES:
        result = await retriever.retrieve(seeded["workspace_id"], query)
        if result.grader_status != "NO_MATCH":
            misclassified.append((query, result.grader_status, round(result.top_score, 4)))

    assert not misclassified, (
        f"{len(misclassified)}/{len(UNRELATED_QUERIES)} 条无关问题被判定为「课本有覆盖」：\n"
        + "\n".join(f"  - {q} → {status}（top_score={score}）" for q, status, score in misclassified)
        + "\nP0-05④ 要求这些必须全部是 NO_MATCH。"
    )


# 「留出集」：以下两组问句**不参与调参**，专门用来抓「把阈值调到刚好通过那 14 条」的过拟合。
# 只测一组正例（或只测一组反例）都能被作弊通过：全判 NO_MATCH 能过反例，全判有覆盖能过正例，
# 所以必须成对存在。
HELDOUT_UNRELATED: tuple[str, ...] = (
    "怎么用微波炉做芝士蛋糕",
    "请解释一下傅里叶变换的物理意义",
    "C++ 的虚函数表是怎么实现的",
    "帮我翻译一句法语问候语",
    "如何训练一只金毛不咬人",
    "新能源汽车电池的回收流程是怎样的",
    "股票市盈率和市净率有什么区别",
    "慢性胃炎应该注意哪些饮食",
    "推荐几个适合跑步的鞋子品牌",
    "太阳系里木星有多少颗卫星",
    "怎么给孩子讲解一元一次方程",
    "如何办理护照和签证",
)

# 全部来自样本教材第 4 章的真实内容（Malayan Union / 苏丹权力 / 马来亚联合邦），
# 判成 NO_MATCH 就是「该覆盖却没覆盖」——比误判更伤，用户会觉得教材白传了。
HELDOUT_COVERED: tuple[str, ...] = (
    "Malayan Union 是怎么成立的",
    "苏丹在 Malayan Union 时期的权力有什么变化",
    "马来亚联合邦恢复了什么",
    "马来人为什么反对 Malayan Union",
)


async def test_heldout_unrelated_queries_are_no_match(seeded: dict[str, Any]) -> None:
    """P0-05④ 的留出集：12 条未参与调参的无关问题必须全部判 NO_MATCH。"""
    retriever = get_retriever()
    leaked: list[tuple[str, str, float]] = []

    for query in HELDOUT_UNRELATED:
        result = await retriever.retrieve(seeded["workspace_id"], query)
        if result.grader_status != "NO_MATCH":
            leaked.append((query, result.grader_status, round(result.top_score, 4)))

    assert not leaked, (
        f"留出集里 {len(leaked)}/{len(HELDOUT_UNRELATED)} 条无关问题被判为「课本有覆盖」"
        "（说明阈值是对着固定 14 条调出来的，没有泛化）：\n"
        + "\n".join(f"  - {q} → {status}（top_score={score}）" for q, status, score in leaked)
    )


async def test_heldout_covered_queries_are_not_no_match(seeded: dict[str, Any]) -> None:
    """P0-05④ 的另一半：把阈值调「严」不能以牺牲真问题为代价。

    这四条问的都是样本教材第 4 章明确写了的内容（见 scripts/make_sample_pdf.py）。
    如果它们被判 NO_MATCH，用户上传了教材却被回答「课本未覆盖」—— 这比误判成「有覆盖」
    更伤，因为它把产品最核心的价值（按课本作答）直接抹掉了。
    """
    retriever = get_retriever()
    missed: list[tuple[str, str, float]] = []

    for query in HELDOUT_COVERED:
        result = await retriever.retrieve(seeded["workspace_id"], query)
        if result.grader_status == "NO_MATCH":
            missed.append((query, result.grader_status, round(result.top_score, 4)))

    assert not missed, (
        f"{len(missed)}/{len(HELDOUT_COVERED)} 条「课本确实写了」的问题被判成未覆盖：\n"
        + "\n".join(f"  - {q} → {status}（top_score={score}）" for q, status, score in missed)
    )


async def test_no_match_turn_has_no_page_number_and_no_artifact(
    client: Any, seeded: dict[str, Any]
) -> None:
    """NO_MATCH 的完整回合：正文说「课本未覆盖」，且**任何地方都不出现 p.N**。"""
    events = await sse_events(
        client, {"workspace_id": seeded["workspace_id"], "query": NO_MATCH_QUERY}
    )
    retrieval = first_event(events, "retrieval")
    assert retrieval["grader_status"] == "NO_MATCH"

    decision = first_event(events, "modality_decision")
    assert decision["action"] == "none", "无课本依据时不得生成产物（P0-05④）"
    assert decision["artifact_type"] == "none"
    assert not [name for name, _ in events if name == "artifact"]

    reply = reply_text(events)
    assert "课本未覆盖" in reply
    assert not PAGE_MARKER_RE.findall(reply), f"未覆盖时出现了页码：{PAGE_MARKER_RE.findall(reply)}"


# --------------------------------------------------------------------------- #
# P0-05② / P0-05③：双模式正文的硬性结构
# --------------------------------------------------------------------------- #
async def test_syllabus_strict_fact_lines_all_carry_page_number(
    client: Any, seeded: dict[str, Any]
) -> None:
    """严格模式下逐条检查：每个事实条目都必须以 [p.页码] 结尾。"""
    events = await sse_events(
        client,
        {"workspace_id": seeded["workspace_id"], "query": RELATED_QUERY, "mode": "syllabus_strict"},
    )
    assert first_event(events, "retrieval")["grader_status"] == "SYLLABUS_MATCH"

    lines = fact_lines(reply_text(events))
    assert lines, "严格模式下必须给出带页码的事实条目，而不是空回答"
    missing = [line for line in lines if not BRACKET_PAGE_RE.search(line)]
    assert not missing, f"这些事实条目缺少 [p.页码]：{missing}"

    # 页码必须落在真实切片范围内（不许凭空造）
    allowed = set()
    for chunk in first_event(events, "retrieval")["chunks"]:
        allowed.update(range(chunk["page_start"], chunk["page_end"] + 1))
    cited = {int(match) for line in lines for match in BRACKET_PAGE_RE.findall(line)}
    assert cited <= allowed, f"正文引用了检索结果里不存在的页码：{sorted(cited - allowed)}"


async def test_hybrid_extended_reply_declares_and_marks_extended_part() -> None:
    """P0-05③ 的契约级验证：双层回答必须以「含超纲内容」开头，并把拓展部分放进引用块。

    这里直接构造 `PARTIAL_OR_BEYOND` 的检索结果调用 `compose_reply`，
    避免把断言绑死在「某条问句当前恰好落在中间态」这种脆弱的检索调参上。
    """
    from app.services.agent_engine import AgentEngine, TurnRequest
    from app.services.mock_generator import build_context
    from app.services.retriever import PARTIAL_OR_BEYOND, RetrievalResult, RetrievedChunk
    from app.services.router import route

    chunk = RetrievedChunk(
        chunk_id="chunk-1",
        section_code="4.1.2",
        section_title="Kuasa Sultan",
        page_start=74,
        page_end=75,
        score=0.42,
        snippet="在 Malayan Union 宪制下，苏丹仅保留宗教与习俗事务的权限，政治权力被大幅削减。",
        content="在 Malayan Union 宪制下，苏丹仅保留宗教与习俗事务的权限，政治权力被大幅削减。",
    )
    result = RetrievalResult(
        chunks=(chunk,),
        grader_status=PARTIAL_OR_BEYOND,
        section_scope="4.1",
        top_score=0.42,
        query_terms=("苏丹",),
    )
    context = build_context(
        query="Malayan Union 苏丹的权力有什么变化",
        section_code="4.1.2",
        section_title="Kuasa Sultan",
        texts=(chunk.content,),
        pages=(74,),
    )
    request = TurnRequest(workspace_id="ws-1", query="Malayan Union 苏丹的权力有什么变化")

    reply, scope = AgentEngine().compose_reply(request, result, route(request.query), context)

    assert scope == "hybrid_extended"
    assert reply.splitlines()[0].startswith("含超纲内容"), (
        f"P0-05③ 要求开头声明含超纲内容，实际首行：{reply.splitlines()[0]!r}"
    )
    assert "> **深度拓展" in reply, "拓展部分必须用视觉上可区分的引用块标出"
    assert "【课本依据】" in reply, "双层结构必须保留课本依据部分"
    # 拓展段落本身不得携带页码（否则等于把超纲内容伪装成课本原文）
    extended = reply.split("> **深度拓展", 1)[1]
    assert not PAGE_MARKER_RE.findall(extended), "深度拓展段落不应带课本页码"


async def test_compose_reply_no_match_never_prints_pages() -> None:
    """P0-05④ 的契约级验证：NO_MATCH 分支的正文里一个页码都不能有。"""
    from app.services.agent_engine import AgentEngine, TurnRequest
    from app.services.mock_generator import build_context
    from app.services.retriever import NO_MATCH, RetrievalResult
    from app.services.router import route

    result = RetrievalResult(
        chunks=(), grader_status=NO_MATCH, section_scope=None, top_score=0.0, query_terms=()
    )
    context = build_context(
        query="如何做红烧肉", section_code="", section_title="", texts=(), pages=()
    )
    request = TurnRequest(workspace_id="ws-1", query="如何做红烧肉")

    reply, scope = AgentEngine().compose_reply(request, result, route(request.query), context)

    assert scope == "none"
    assert "课本未覆盖" in reply
    assert not PAGE_MARKER_RE.findall(reply), f"未覆盖回答里出现了页码：{PAGE_MARKER_RE.findall(reply)}"


# --------------------------------------------------------------------------- #
# IMPL-NOTES §E2 / §E3 的回归
# --------------------------------------------------------------------------- #
async def test_explicit_section_question_is_syllabus_match(seeded: dict[str, Any]) -> None:
    """§E2：「学习 4.1」没有词面重叠，但用户点名了课本真实存在的一节 → 严格匹配。

    这条是 SPEC §1.2 的招牌场景（目录树精确锚定），判成 NO_MATCH 等于打死主演示路径。
    """
    retriever = get_retriever()
    result = await retriever.retrieve(seeded["workspace_id"], EXPLICIT_SECTION_QUERY)

    assert result.grader_status == "SYLLABUS_MATCH", (
        f"「{EXPLICIT_SECTION_QUERY}」被判为 {result.grader_status}（top_score={result.top_score:.4f}）"
    )
    assert result.chunks[0].section_code.startswith("4.1")
    # 引用页码必须来自真实切片
    assert all(chunk.page_start >= 1 for chunk in result.chunks)


async def test_anchor_strict_section_locks_retrieval_to_subsection(
    seeded: dict[str, Any],
) -> None:
    """§E3：带 4.1.3 锚点追问时，结果必须全部落在 4.1.3（含其子节），不得混入 4.1.1。

    不锁定的话，用户点了 p.76 的节点却看到 p.70 的引用 —— 引用与画布对不上。
    """
    retriever = get_retriever()
    result = await retriever.retrieve(
        seeded["workspace_id"],
        "这里再细一点",
        section_code="4.1.3",
        page=76,
        strict_section=True,
    )

    assert result.chunks, "锚点追问必须有检索结果"
    codes = {chunk.section_code for chunk in result.chunks}
    assert all(code == "4.1.3" or code.startswith("4.1.3.") for code in codes), (
        f"锚定 4.1.3 却检索到其它小节：{sorted(codes)}"
    )
    assert "4.1.1" not in codes and "4.1.2" not in codes
    assert result.grader_status == "SYLLABUS_MATCH"


# --------------------------------------------------------------------------- #
# P0-01① / P0-01③：隔离与删除
# --------------------------------------------------------------------------- #
async def test_workspace_isolation_and_delete(
    client: Any, sample_pdf_path: Path
) -> None:
    """同一份 PDF 上传到两个 workspace：检索结果零交集；删掉一个后另一个不受影响。"""
    first = await seed_workspace_with_pdf(client, sample_pdf_path, title="教材 A")
    second = await seed_workspace_with_pdf(client, sample_pdf_path, title="教材 B")

    retriever = get_retriever()
    first_ids = {c.chunk_id for c in (await retriever.retrieve(first["workspace_id"], RELATED_QUERY)).chunks}
    second_ids = {
        c.chunk_id for c in (await retriever.retrieve(second["workspace_id"], RELATED_QUERY)).chunks
    }
    assert first_ids and second_ids
    assert not (first_ids & second_ids), f"两个 workspace 检索到同一批切片：{sorted(first_ids & second_ids)}"

    # 删除 A：它的切片既不在库里，也不可再被检索
    deleted = await client.delete(f"/api/workspaces/{first['workspace_id']}")
    assert deleted.status_code == 200

    factory = get_session_factory()
    async with factory() as session:
        remaining = (
            await session.execute(
                select(DocumentChunk).where(DocumentChunk.workspace_id == first["workspace_id"])
            )
        ).scalars().all()
    assert remaining == [], f"删除 workspace 后仍残留 {len(remaining)} 条切片（P0-01③）"

    gone = await retriever.retrieve(first["workspace_id"], RELATED_QUERY)
    assert gone.chunks == ()
    assert gone.grader_status == "NO_MATCH"

    # B 不受影响
    still_there = await retriever.retrieve(second["workspace_id"], RELATED_QUERY)
    assert {c.chunk_id for c in still_there.chunks} == second_ids


# --------------------------------------------------------------------------- #
# P0-04① + P0-03③ 兜底：全库无空 section_code
# --------------------------------------------------------------------------- #
async def test_no_chunk_in_library_has_empty_section_code(
    client: Any, seeded: dict[str, Any], tmp_path: Path
) -> None:
    """P0-04① 的硬指标：`section_code` 命中率 100%，**包含**章节号识别失败的降级文档。"""
    plain_pdf = build_pdf_without_headings(tmp_path / "no_headings.pdf", pages=6)
    fallback_doc = await upload_pdf(
        client, seeded["workspace_id"], "no_headings.pdf", plain_pdf.read_bytes()
    )
    status = await wait_terminal(client, fallback_doc["id"])
    assert status["status"] == "ready", f"无章节号的 PDF 也应能解析（走 page_fallback）：{status}"

    factory = get_session_factory()
    async with factory() as session:
        chunks = (await session.execute(select(DocumentChunk))).scalars().all()

    assert chunks, "样本教材 + 降级文档都必须产出切片"
    empty = [chunk.id for chunk in chunks if not (chunk.section_code or "").strip()]
    assert not empty, f"存在 {len(empty)} 条 section_code 为空的切片（P0-04① 不通过）"

    fallback_chunks = [chunk for chunk in chunks if chunk.document_id == fallback_doc["id"]]
    assert fallback_chunks, "降级文档必须切出切片"
    assert all(chunk.section_code.startswith("P") for chunk in fallback_chunks), (
        f"page_fallback 的 section_code 应形如 P0001，实际：{sorted({c.section_code for c in fallback_chunks})}"
    )
    assert all(chunk.page_start >= 1 and chunk.page_end >= chunk.page_start for chunk in fallback_chunks)

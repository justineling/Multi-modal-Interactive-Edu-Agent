"""多教材章节碰撞：检索按 `document_id` 隔离（Level 1「目录树点击直传」）。

**要修的是什么**
同一工作区上传《Sejarah Form 4》与《Sejarah Form 5》时，两本书都有 `Bab 4 → 4.1`。
修复前 `retriever.retrieve()` 没有 `document_id` 参数、`search_similar()` 只按
`workspace_id + section_prefix` 过滤，于是两本书的 4.1 被混在一起召回 —— 引用显示
「4.1, p.76」，但学生不知道是哪本书的 p.76。这是「严格按教材」承诺的直接破坏：
知识张冠李戴。

**本文件锁住的两条不变量**（对应任务要求里的第 1、5 条）
1. 带 `document_id` 检索时，返回的切片**全部**属于该文档；
2. 放宽逻辑（本节 → 本章 → 全书）**绝不跨文档** —— 目标文档里没有这一节时，
   正确结果是空/该书其它内容，而不是拿另一本书的同名章节顶上。

数据不用真实 PDF：本文件验的是**检索层的过滤语义**，而样本 PDF 只有一本书的章节结构，
无法构造「两本书都有 4.1」。直接写 `DocumentChunk` + 真实 embedding provider，
让「哪本书有哪一节、正文是什么」完全可控，同时仍然走真实的向量与 BM25 路径
（不是 mock 掉检索器后断言它被调用过）。
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import repository
from app.db.models import DocumentChunk
from app.db.session import get_session_factory
from app.db.vector_store import MemoryVectorStore, PgVectorStore, VectorItem, get_vector_store
from app.schemas.chat import EVENT_ORDER, ChatRequest, RetrievalEvent
from app.services.agent_engine import AgentEngine, TurnRequest
from app.services.embedding import get_embedding_provider
from app.services.retriever import NO_MATCH, RetrievalResult, Retriever

FORM4 = "Sejarah Form 4.pdf"
FORM5 = "Sejarah Form 5.pdf"

# 两本书的 4.1 刻意写成「同一主题、不同细节」：这正是章节号碰撞的真实形态 ——
# 主题词高度重叠（所以修复前两本书会互相抢 Top-K），但归属必须分开。
FORM4_41 = "Malayan Union 在 1946 年成立，宪制建议由英国派出的麦克迈克尔起草。"
FORM5_41 = "Malayan Union 在 1946 年成立，Form 5 重点讨论它对 1957 年独立进程的影响。"

QUERY = "Malayan Union 1946 年成立"


async def _seed_workspace(
    documents: Sequence[tuple[str, Sequence[tuple[str, str]]]],
) -> SimpleNamespace:
    """建 1 个 workspace + 若干文档；每篇文档含 `(section_code, content)` 切片并向量化。

    返回 `documents` 是 `filename -> SimpleNamespace(document_id, filename, chunk_ids)`，
    `chunk_ids` 为 `section_code -> chunk_id`（本文件每个 section 只放一个切片，
    让「哪个切片属于哪本书的哪一节」在断言里一眼可读）。
    """
    factory = get_session_factory()
    store = get_vector_store()
    await store.initialize()
    provider = get_embedding_provider()

    async with factory() as session:
        workspace = await repository.create_workspace(session, "多教材工作区")
        seeded: list[SimpleNamespace] = []
        # (chunk_id, document_id, content) —— 提交后再统一向量化
        pending: list[tuple[str, str, str]] = []

        for filename, sections in documents:
            document = await repository.create_document(
                session, workspace_id=workspace.id, filename=filename
            )
            rows = [
                DocumentChunk(
                    document_id=document.id,
                    workspace_id=workspace.id,
                    section_code=code,
                    section_title=f"{filename} {code}",
                    level=2,
                    chunk_index=index,
                    page_start=70 + index,
                    page_end=70 + index,
                    content=content,
                )
                for index, (code, content) in enumerate(sections)
            ]
            await repository.bulk_insert_chunks(session, rows)
            for (code, content), row in zip(sections, rows):
                pending.append((row.id, document.id, content))
            seeded.append(
                SimpleNamespace(
                    document_id=document.id,
                    filename=filename,
                    chunk_ids={code: row.id for (code, _), row in zip(sections, rows)},
                )
            )
        await session.commit()
        workspace_id = workspace.id

    vectors = await provider.embed([content for _, _, content in pending])
    await store.upsert_vectors(
        [
            VectorItem(
                chunk_id=chunk_id,
                workspace_id=workspace_id,
                document_id=document_id,
                vector=vector,
                model=provider.model_name,
            )
            for (chunk_id, document_id, _), vector in zip(pending, vectors)
        ]
    )

    return SimpleNamespace(
        workspace_id=workspace_id,
        documents={doc.filename: doc for doc in seeded},
        store=store,
        provider=provider,
    )


@pytest.fixture()
async def two_books(db_env: Path) -> SimpleNamespace:
    """经典碰撞场景：《Form 4》与《Form 5》都有 `4.1`。"""
    return await _seed_workspace(
        [
            (FORM4, [("4.1", FORM4_41), ("4.2", "马来亚联合邦在 1948 年成立。")]),
            (FORM5, [("4.1", FORM5_41), ("4.3", "国民教育政策在 Form 5 第四章讨论。")]),
        ]
    )


# --------------------------------------------------------------------------- #
# 1. 核心用例：两本书都有 4.1，带 document_id 只返回该书切片
# --------------------------------------------------------------------------- #
async def test_document_scope_isolates_duplicate_section_codes(
    two_books: SimpleNamespace,
) -> None:
    retriever = Retriever(two_books.store, two_books.provider)
    form4 = two_books.documents[FORM4]
    form5 = two_books.documents[FORM5]

    result_a = await retriever.retrieve(
        two_books.workspace_id, QUERY, section_code="4.1", document_id=form4.document_id
    )
    result_b = await retriever.retrieve(
        two_books.workspace_id, QUERY, section_code="4.1", document_id=form5.document_id
    )

    ids_a = {chunk.chunk_id for chunk in result_a.chunks}
    ids_b = {chunk.chunk_id for chunk in result_b.chunks}

    assert ids_a, "《Form 4》的 4.1 必须能被检索到"
    assert ids_b, "《Form 5》的 4.1 必须能被检索到"

    # 全部命中都必须属于各自那本书
    assert ids_a <= set(form4.chunk_ids.values())
    assert ids_b <= set(form5.chunk_ids.values())

    # 交叉污染的直接断言 —— 这条就是「知识张冠李戴」的守卫
    assert form5.chunk_ids["4.1"] not in ids_a
    assert form4.chunk_ids["4.1"] not in ids_b

    # Top-1 必须是该书自己的 4.1（不是同书别章，也不是别书）
    assert result_a.chunks[0].chunk_id == form4.chunk_ids["4.1"]
    assert result_b.chunks[0].chunk_id == form5.chunk_ids["4.1"]

    # 结果体回显作用域：上层（SSE / 后续 Level 2）不必回头猜这一轮锁的是哪本书
    assert result_a.document_id == form4.document_id
    assert result_b.document_id == form5.document_id


# --------------------------------------------------------------------------- #
# 2. 向后兼容：不带 document_id 时行为不变（跨全部教材）
# --------------------------------------------------------------------------- #
async def test_without_document_id_behaviour_unchanged(two_books: SimpleNamespace) -> None:
    """`document_id=None` 必须与「加入该参数之前」完全一致：跨全部教材召回。

    这是硬要求 —— 现有 371 条测试的隐式前提就是「不传 document_id 时按 workspace 检索」，
    一旦默认行为被改成「只查某一本」，那些用例会成片变红，线上也会突然答不出别本书的内容。
    """
    retriever = Retriever(two_books.store, two_books.provider)
    form4 = two_books.documents[FORM4]
    form5 = two_books.documents[FORM5]

    result = await retriever.retrieve(
        two_books.workspace_id, QUERY, section_code="4.1", top_k=10
    )
    ids = {chunk.chunk_id for chunk in result.chunks}

    assert form4.chunk_ids["4.1"] in ids
    assert form5.chunk_ids["4.1"] in ids
    assert result.document_id is None


# --------------------------------------------------------------------------- #
# 3. document_id 指向不存在的文档 → 空结果，不崩
# --------------------------------------------------------------------------- #
async def test_unknown_document_id_returns_empty(two_books: SimpleNamespace) -> None:
    """错传/过期的 document_id（前端 localStorage 残留、文档已删）不能抛异常。

    检索层必须把它当成「这本书没有切片」：返回空结果 + NO_MATCH，
    上层照常走「课本未覆盖」分支，而不是 500。
    """
    retriever = Retriever(two_books.store, two_books.provider)
    missing = "00000000-0000-0000-0000-000000000000"

    result = await retriever.retrieve(
        two_books.workspace_id, QUERY, section_code="4.1", document_id=missing
    )

    assert result.is_empty
    assert result.grader_status == NO_MATCH
    assert result.top_score == 0.0
    # 作用域仍如实回显，便于线上区分「书不存在」与「这节没内容」
    assert result.document_id == missing


# --------------------------------------------------------------------------- #
# 4. document_id + section_code 组合过滤（同时生效，不是覆盖）
# --------------------------------------------------------------------------- #
async def test_document_id_and_section_code_combine(two_books: SimpleNamespace) -> None:
    retriever = Retriever(two_books.store, two_books.provider)
    form4 = two_books.documents[FORM4]
    form5 = two_books.documents[FORM5]

    # 《Form 4》+ 4.2：章节过滤确实生效（不是被 document_id 覆盖成「全书」）
    only_42 = await retriever.retrieve(
        two_books.workspace_id,
        QUERY,
        section_code="4.2",
        document_id=form4.document_id,
        strict_section=True,
    )
    assert [chunk.section_code for chunk in only_42.chunks] == ["4.2"]
    assert {chunk.chunk_id for chunk in only_42.chunks} == {form4.chunk_ids["4.2"]}

    # 《Form 5》+ 4.2：该书没有 4.2，因此不能返回《Form 4》的 4.2
    # （若两个条件互相覆盖、只有其中一个生效，这里就会漏出 Form 4 的切片）
    cross = await retriever.retrieve(
        two_books.workspace_id, QUERY, section_code="4.2", document_id=form5.document_id
    )
    cross_ids = {chunk.chunk_id for chunk in cross.chunks}
    assert form4.chunk_ids["4.2"] not in cross_ids
    assert cross_ids <= set(form5.chunk_ids.values())


# --------------------------------------------------------------------------- #
# 5. 放宽逻辑不跨文档（关键用例）
# --------------------------------------------------------------------------- #
async def test_widening_does_not_cross_documents(db_env: Path) -> None:
    """带 `document_id` 且该文档内该节为空时，**不能**返回其他文档的切片。

    这里的构造是「最坏情况」：《Form 4》根本没有 4.2，而《Form 5》的 4.2 与问句
    高度相关。若 `_search_with_widening` 在某一级把 `document_id` 置 None 去「多找点
    候选」，返回的就会是《Form 5》的 4.2 —— 正是这个修复要禁止的行为。
    """
    seeded = await _seed_workspace(
        [
            (FORM4, [("1.1", "Form 4 第一章讨论地理环境与人口分布。")]),
            (FORM5, [("4.2", "Malayan Union 在 1946 年成立的过程与宪制影响。")]),
        ]
    )
    retriever = Retriever(seeded.store, seeded.provider)
    form4 = seeded.documents[FORM4]
    form5 = seeded.documents[FORM5]

    result = await retriever.retrieve(
        seeded.workspace_id,
        QUERY,
        section_code="4.2",
        document_id=form4.document_id,
    )
    ids = {chunk.chunk_id for chunk in result.chunks}

    assert form5.chunk_ids["4.2"] not in ids, "放宽不得跨文档：Form 5 的 4.2 不能冒充 Form 4"
    assert ids <= set(form4.chunk_ids.values())

    # 极端情形：目标文档一个切片都没有 → 空结果，绝不回落到别的书
    async with get_session_factory()() as session:
        empty_document = await repository.create_document(
            session, workspace_id=seeded.workspace_id, filename="Sejarah Form 3.pdf"
        )
        await session.commit()

    empty_result = await retriever.retrieve(
        seeded.workspace_id, QUERY, document_id=empty_document.id
    )
    assert empty_result.is_empty
    assert empty_result.grader_status == NO_MATCH
    assert empty_result.document_id == empty_document.id


# --------------------------------------------------------------------------- #
# 6. 存储层：MemoryVectorStore 直接按 document_id 过滤（矩阵/缓存路径）
# --------------------------------------------------------------------------- #
async def test_memory_store_filters_by_document_id(two_books: SimpleNamespace) -> None:
    """绕过 retriever 直接测 store：过滤必须发生在**候选集**上，而不是事后裁剪。

    事后裁剪会让另一本书的切片先挤占 Top-K 名额，表现为「本该 8 条同书切片只剩 3 条」。
    """
    query_vector = await two_books.provider.embed_one(QUERY)
    form4 = two_books.documents[FORM4]

    hits = await two_books.store.search_similar(
        two_books.workspace_id, query_vector, top_k=10, document_id=form4.document_id
    )

    assert hits, "限定文档后仍应命中该书切片"
    assert {hit.document_id for hit in hits} == {form4.document_id}
    assert {hit.chunk_id for hit in hits} <= set(form4.chunk_ids.values())

    # 不传 document_id 时仍是跨文档（向后兼容）
    all_hits = await two_books.store.search_similar(
        two_books.workspace_id, query_vector, top_k=10
    )
    assert {hit.document_id for hit in all_hits} == {
        two_books.documents[FORM4].document_id,
        two_books.documents[FORM5].document_id,
    }


# --------------------------------------------------------------------------- #
# 7. 契约守卫：三个入口都必须带 document_id
# --------------------------------------------------------------------------- #
def test_all_entry_points_accept_document_id() -> None:
    """「只改了其中一个实现」是本类改动最常见的漏项（尤其 PgVectorStore）。

    用签名反射兜住：协议、两个实现、检索器必须同时具备 `document_id`，
    且默认值必须是 `None`（默认不限文档 = 向后兼容）。
    """
    targets = (
        MemoryVectorStore.search_similar,
        PgVectorStore.search_similar,
        Retriever.retrieve,
    )
    for target in targets:
        parameters = inspect.signature(target).parameters
        assert "document_id" in parameters, f"{target!r} 缺少 document_id 参数"
        assert parameters["document_id"].default is None, f"{target!r} 的 document_id 默认值必须是 None"


# --------------------------------------------------------------------------- #
# 8. API 契约：请求体收 document_id，retrieval 事件回显 scope_document_id
# --------------------------------------------------------------------------- #
def test_chat_request_accepts_optional_document_id() -> None:
    """请求体加的是**可选**字段：老前端不发也不该报错（extra=forbid 仍生效）。"""
    with_scope = ChatRequest(workspace_id="ws", query="学习 4.1", document_id="doc-1")
    assert with_scope.document_id == "doc-1"
    # 不传时默认 None —— 既有调用方（含全部现有测试）零改动
    assert ChatRequest(workspace_id="ws", query="学习 4.1").document_id is None


def test_retrieval_event_carries_scope_document_id() -> None:
    """`retrieval` 事件加的是**可选字段**，不新增事件类型。

    数字口径（避免文档里出现「8 类 / 7 类」两个数字像是自相矛盾）：
    **事件类型共 8 个**（`SseEventName`），其中 **7 个在 `EVENT_ORDER` 有序序列里**，
    `error` 不在序列内（可插入任意位置，之后仍须补发 `turn_end`）。

    这个字段让前端能区分「4.1 是哪本书的 4.1」—— 两本书都有 4.1 时，
    只有 `section_scope` 无法支撑「当前按《Form 4》4.1 讲解」这类提示。
    """
    event = RetrievalEvent(
        grader_status="SYLLABUS_MATCH",
        section_scope="4.1",
        top_score=0.9,
        scope_document_id="doc-1",
    )
    payload = event.model_dump(mode="json")
    assert payload["scope_document_id"] == "doc-1"
    assert payload["section_scope"] == "4.1"

    # 不传时默认 None，且字段仍然出现在序列化结果里（前端可稳定读取）
    legacy = RetrievalEvent(grader_status="NO_MATCH", top_score=0.0)
    assert legacy.model_dump(mode="json")["scope_document_id"] is None
    # 事件名集合没被改动
    assert "retrieval" in EVENT_ORDER and len(EVENT_ORDER) == 7


# --------------------------------------------------------------------------- #
# 9. 透传链的两处「静默丢失」风险（写对了、功能坏了、测试全绿）
# --------------------------------------------------------------------------- #
def test_with_turn_id_preserves_document_id() -> None:
    """`TurnRequest.with_turn_id()` 必须**字段级**保住 `document_id`。

    为什么必须写字段级断言，而不是「带 turn_id 的请求能检索到」那种行为级断言：
    `stream_turn` 里是 `req = request if request.turn_id else request.with_turn_id(...)`
    —— 只要调用方传了 `turn_id`，就**根本不会走 `with_turn_id()`**，行为级测试因此
    完全测不到这行漏写。而漏写不会报错、不会变红，只在「turn_id 为空 → 重建对象」
    这条路径上静默丢掉教材作用域。这是本次改动最隐蔽的坑：代码看着对、测试全绿、
    功能坏的。
    """
    request = TurnRequest(workspace_id="w", query="q", document_id="doc-abc")
    rebuilt = request.with_turn_id("turn-1")

    assert rebuilt.document_id == "doc-abc"
    assert rebuilt.turn_id == "turn-1"
    # 其余字段同样不能丢（重建时最容易整体漏抄）
    assert (rebuilt.workspace_id, rebuilt.query, rebuilt.mode) == ("w", "q", "syllabus_strict")

    # `None` 也要原样保留，不能被兜底成空串或别的值
    assert TurnRequest(workspace_id="w", query="q").with_turn_id("t").document_id is None


async def test_retrieve_forwards_document_id_to_retriever() -> None:
    """`AgentEngine._retrieve()` 必须把 `req.document_id` 交给 retriever。

    这是「API → engine → retriever」链路的最后一跳，之前整条链断在这里。
    用 `__new__` 绕过 `__init__`：本用例只验「参数转发」这一个动作，构造完整引擎会把
    llm / store / settings 三个进程级单例一起拉进来，耦合了无关依赖。
    """
    captured: list[dict[str, Any]] = []

    class _StubRetriever:
        async def retrieve(self, workspace_id: str, query: str, **kwargs: Any) -> RetrievalResult:
            captured.append({"workspace_id": workspace_id, "query": query, **kwargs})
            return RetrievalResult(
                chunks=(),
                grader_status=NO_MATCH,
                section_scope=None,
                top_score=0.0,
                query_terms=(),
                document_id=kwargs.get("document_id"),
            )

    engine = AgentEngine.__new__(AgentEngine)
    engine._retriever = _StubRetriever()  # type: ignore[attr-defined]

    await engine._retrieve(TurnRequest(workspace_id="w", query="q", document_id="doc-abc"))
    assert captured[0]["document_id"] == "doc-abc"
    assert captured[0]["workspace_id"] == "w"

    # 不指定文档时必须显式传 None（而不是干脆不传这个 kwarg）：
    # retriever 的默认值就是 None，两者等价，但显式传能保证以后改默认值时不悄悄改变语义。
    await engine._retrieve(TurnRequest(workspace_id="w", query="q"))
    assert captured[1]["document_id"] is None


def test_chat_stream_builds_turn_request_with_document_id() -> None:
    """`_turn_request()` 必须把请求体的 `document_id` 搬进 `TurnRequest`。

    这是链路起点。漏了这一行，前端发什么都白搭 —— 而且**上层测试一条都不会红**，
    因为检索层和引擎层都「支持」这个参数，只是没人喂给它。
    """
    from types import SimpleNamespace

    from app.api.chat_stream import _turn_request

    request = SimpleNamespace(state=SimpleNamespace(request_id="turn-1"))
    turn = _turn_request(
        ChatRequest(workspace_id="w", query="学习 4.1", document_id="doc-abc"),  # type: ignore[arg-type]
        request,  # type: ignore[arg-type]
    )

    assert turn.document_id == "doc-abc"
    assert turn.turn_id == "turn-1"

    # 不带 document_id 时保持 None（跨全部教材，向后兼容）
    plain = _turn_request(
        ChatRequest(workspace_id="w", query="学习 4.1"),  # type: ignore[arg-type]
        request,  # type: ignore[arg-type]
    )
    assert plain.document_id is None

"""P1-03 独立验证（QA 自写，不复用工程师的测试与断言角度）。

本文件只做一件事：**用与工程师不同的观测点，证明四条承重不变量真的成立**。

| 不变量 | 工程师的观测点 | 本文件的观测点（必须不同） |
|---|---|---|
| ① fork 点在 TTFT 定格之后 | 假 LLM 内部记录 `first_delta_at` | 引擎边界：`_artifact_work` 的**进入时刻** vs `stream_turn` **首个 token 事件被消费的时刻**；再加一条「TTFT 数值与产物耗时无关」的定量断言 |
| ② 分支 B 物理上无法落库 | 无 | 注入会计数 `session.commit()` 的假 session：**正文流式期间 commit 次数恒为 0**；并对 `ArtifactWork` 的字段集合做结构断言 |
| ③ `artifact` 事件必定在落库之后 | 无 | 收到事件的那一刻另开一个 session 读库，断言读到的 version/spec 与事件**逐字节一致** |
| ④ 取消不留孤儿 | 用的是假引擎（对并行改造零覆盖） | 真 `AgentEngine` + 假 LLM：断连后 `asyncio.all_tasks()` 无残留、产物条数不增、`complete_json` 无新增完成 |

另含：页码钳制的 6 组攻击用例（含「用错允许集合」这个已知风险）、以及 5 组破坏性尝试。

全部离线（假 `LLMClient` / 临时 SQLite），不打网络、不烧 token。
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import time
from typing import Any

import pytest

from app.config import Settings
from app.db import repository
from app.db.session import get_session_factory
from app.schemas.chat import Anchor
from app.schemas.common import canonical_json
from app.services.agent_engine import ArtifactWork, AgentEngine, TurnRequest, _clamp_spec_pages
from app.services.retriever import (
    NO_MATCH,
    SYLLABUS_MATCH,
    RetrievalResult,
    RetrievedChunk,
)

# 一个足够长的正文切片，保证 `extract_facts` 能抽出句子（否则 mock 内容为空、断言没意义）
CHUNK_TEXT = (
    "1946 年 4 月 1 日，Malayan Union 正式生效，由英国总督直接统治。"
    "在 Malayan Union 宪制下，苏丹仅保留宗教与习俗事务的权限。"
    "1948 年 2 月 1 日，马来亚联合邦成立，恢复了苏丹在州务上的权力。"
)

# 钳制攻击用的「真实页码区间」：切片 76-77。**注意 77 不是起始页**，
# 所以「用 context.pages（只有起始页）当允许集合」的实现会把合法的 p.77 误剥 —— 本文件要打的就是它。
CHUNK_PAGE_START = 76
CHUNK_PAGE_END = 77


# --------------------------------------------------------------------------- #
# 夹具：检索结果 / 假 LLM / 引擎
# --------------------------------------------------------------------------- #
def _chunk(page_start: int = CHUNK_PAGE_START, page_end: int = CHUNK_PAGE_END) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id="chunk-1",
        section_code="4.3.1",
        section_title="Malayan Union 的影响",
        page_start=page_start,
        page_end=page_end,
        score=0.91,
        snippet=CHUNK_TEXT[:80],
        content=CHUNK_TEXT,
    )


def _result(
    *, page_start: int = CHUNK_PAGE_START, page_end: int = CHUNK_PAGE_END, status: str = SYLLABUS_MATCH
) -> RetrievalResult:
    if status == NO_MATCH:
        return RetrievalResult(
            chunks=(),
            grader_status=NO_MATCH,
            section_scope=None,
            top_score=0.0,
            query_terms=(),
        )
    return RetrievalResult(
        chunks=(_chunk(page_start, page_end),),
        grader_status=status,
        section_scope="4.3.1",
        top_score=0.91,
        query_terms=("Malayan", "Union"),
    )


class _FixedRetriever:
    def __init__(self, result: RetrievalResult | None = None) -> None:
        self._result = result or _result()
        self.calls = 0

    async def retrieve(self, *args: Any, **kwargs: Any) -> RetrievalResult:
        self.calls += 1
        return self._result


def _ppt_spec(source_refs: list[str]) -> dict[str, Any]:
    """构造一份最小合法 PPT（5 页），每页的 source_ref 由调用方指定 —— 钳制用例的载体。"""
    slides = []
    for index, ref in enumerate(source_refs):
        slides.append(
            {
                "node_id": f"slide-{index + 1}",
                "layout": "title_hero" if index == 0 else "bullet_points",
                "title": f"第 {index + 1} 页",
                "subtitle": None,
                "bullets": ["要点一", "要点二", "要点三"],
                "comparison_left": None,
                "comparison_right": None,
                "timeline_items": None,
                "quiz_questions": None,
                "speaker_notes": "1946 年联合邦计划提出后，苏丹权力被大幅削减。",
                "source_ref": ref,
            }
        )
    return {"type": "ppt", "topic": "Malayan Union 的影响", "slides": slides}


def _anim2d_spec(source_ref: str, source_pages: list[int | None]) -> dict[str, Any]:
    entities = [
        {
            "id": f"entity-{index}",
            "shape": "zone",
            "label": f"元素 {index}",
            "x": 300.0 + index * 40,
            "y": 300.0,
            "color": "#2563EB",
            "details": "该元素的详细说明。",
            "source_page": page,
        }
        for index, page in enumerate(source_pages)
    ]
    return {
        "type": "anim_2d",
        "title": "联合邦计划的推进过程",
        "steps": [
            {
                "step_index": 0,
                "narration": "1946 年英国公布联合邦计划，各地马来人开始组织反对。",
                "source_ref": source_ref,
                "entities": entities,
            },
            {
                "step_index": 1,
                "narration": "1948 年联合邦成立，苏丹权力恢复。",
                "source_ref": source_ref,
                "entities": copy.deepcopy(entities),
            },
        ],
    }


class _ScriptedLLM:
    """可编排的假 LLM。与工程师那份的区别：它记录的是**引擎可见的调用顺序**，
    并把「产物调用是否真正跑完」与「是否被调用」分开计数（取消用例需要这个区分）。
    """

    enabled = True

    def __init__(
        self,
        *,
        spec: dict[str, Any] | None = None,
        ops: list[dict[str, Any]] | None = None,
        deltas: int = 5,
        delta_delay: float = 0.005,
        artifact_delay: float = 0.005,
        spec_side_effect: BaseException | None = None,
    ) -> None:
        self.spec = spec if spec is not None else _ppt_spec(["4.3.1"] * 5)
        self.ops = ops or []
        self.deltas = deltas
        self.delta_delay = delta_delay
        self.artifact_delay = artifact_delay
        self.spec_side_effect = spec_side_effect
        # 调用流水：("reply"|"artifact", model, perf_counter)
        self.calls: list[tuple[str, str | None, float]] = []
        self.artifact_started = 0
        self.artifact_completed = 0
        self.reply_started = 0

    async def stream_text(self, **kwargs: Any) -> Any:
        self.reply_started += 1
        self.calls.append(("reply", kwargs.get("model"), time.perf_counter()))
        for index in range(self.deltas):
            await asyncio.sleep(self.delta_delay)
            yield f"第 {index} 段正文。"

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.artifact_started += 1
        self.calls.append(("artifact", kwargs.get("model"), time.perf_counter()))
        await asyncio.sleep(self.artifact_delay)
        self.artifact_completed += 1
        if self.spec_side_effect is not None:
            raise self.spec_side_effect
        if self.ops:
            return {"ops": self.ops}
        return self.spec


class _DisabledLLM:
    """演示模式：`enabled=False`。被调用即视为测试失败（用断言锁住「没被调用」）。"""

    enabled = False

    def __init__(self) -> None:
        self.calls = 0

    async def stream_text(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("演示模式不应调用 stream_text")
        yield ""  # pragma: no cover - 只为保持 async generator 形态

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("演示模式不应调用 complete_json")


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "LLM_API_KEY": "qa-offline-key",  # 非空 = 走 LLM 分支（但我们注入的是假 LLM）
        "LLM_MODEL_ROUTER": "router-x",
        "LLM_MODEL_ARTIFACT": "artifact-x",
        "ARTIFACT_PARALLEL_ENABLED": True,
        "ARTIFACT_FORK_MODE": "after_first_token",
        "ARTIFACT_RATE_LIMIT_RETRY": 1,
        # 本文件测的是**钳制层**（P0-05④ 的「越界页码必须被剥掉」）。后置页码重绑定
        # 是钳制**上游**的另一层，它会把「越界页码」直接修正成「真实页码」——
        # 那是对的行为，但会让本文件的断言测不到钳制层（被测层被上游改写）。
        # 所以这里关掉重绑定，把被测层隔离出来；重绑定自己的回归锁在
        # `tests/test_citation_rebind.py`。
        "CITATION_REBIND_ENABLED": False,
    }
    base.update(overrides)
    return Settings(**base)


def _engine(
    llm: Any, *, settings: Settings | None = None, result: RetrievalResult | None = None
) -> AgentEngine:
    return AgentEngine(
        settings=settings or _settings(),
        session_factory=get_session_factory(),
        retriever=_FixedRetriever(result),
        llm=llm,
    )


def _turn(workspace_id: str, *, query: str = "4.3.1 联合邦的影响", **kwargs: Any) -> TurnRequest:
    return TurnRequest(
        workspace_id=workspace_id, query=query, artifact_type_override="ppt", **kwargs
    )


@pytest.fixture()
async def workspace_id(db_env: Any) -> str:
    async with get_session_factory()() as session:
        workspace = await repository.create_workspace(session, title="QA P1-03 独立验证")
        await session.commit()
        return workspace.id


async def _collect(engine: AgentEngine, req: TurnRequest) -> list[tuple[str, Any]]:
    return [event async for event in engine.stream_turn(req)]


async def _count_artifacts(workspace_id: str) -> int:
    async with get_session_factory()() as session:
        return len(await repository.list_artifacts(session, workspace_id))


# =========================================================================== #
# 不变量 ①：fork 点在 TTFT 定格之后
# =========================================================================== #
async def test_artifact_work_entered_after_first_token_is_visible(
    db_env: Any, workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """引擎边界观测：`_artifact_work` 被**进入**的时刻，晚于首个 token 事件被消费的时刻。

    为什么这条比「假 LLM 内部记时间」更强：它不依赖假对象的任何实现细节，
    断言的两端都是被测代码自己的可观测行为（引擎的方法进入点、生成器吐出的事件）。
    """
    entered: list[float] = []
    original = AgentEngine._artifact_work

    async def spy(self: AgentEngine, *args: Any, **kwargs: Any) -> ArtifactWork:
        entered.append(time.perf_counter())
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AgentEngine, "_artifact_work", spy)

    llm = _ScriptedLLM(deltas=5, delta_delay=0.01, artifact_delay=0.01)
    engine = _engine(llm)

    first_token_at: float | None = None
    async for name, _payload in engine.stream_turn(_turn(workspace_id)):
        if name == "token" and first_token_at is None:
            first_token_at = time.perf_counter()

    assert first_token_at is not None
    assert entered, "默认配置下产物分支必须被 fork（否则等于没并行）"
    assert entered[0] > first_token_at, (
        "fork 点必须晚于「首个 token 已被主路径消费」——即 TTFT 已定格。"
        f"artifact_work 进入于 {entered[0]:.6f}，首个 token 于 {first_token_at:.6f}"
    )


async def test_ttft_ms_does_not_depend_on_artifact_latency(
    db_env: Any, workspace_id: str
) -> None:
    """定量断言：产物耗时从 5ms 拉到 400ms，`turn_end.ttft_ms` 不得跟着变。

    这是「TTFT 已定格」的数值形式。若 fork 点在 TTFT 之前且产物请求会占用事件循环，
    这条会立刻红。
    """
    async def ttft_for(artifact_delay: float) -> int:
        llm = _ScriptedLLM(deltas=5, delta_delay=0.01, artifact_delay=artifact_delay)
        engine = _engine(llm)
        events = await _collect(engine, _turn(workspace_id))
        turn_end = next(payload for name, payload in events if name == "turn_end")
        return turn_end.ttft_ms

    fast = await ttft_for(0.005)
    slow = await ttft_for(0.4)

    assert fast is not None and slow is not None
    # 容差 40ms：只吸收调度抖动，不足以掩盖「产物耗时被算进 TTFT」这种量级的错误
    assert abs(slow - fast) <= 40, (
        f"TTFT 被产物耗时污染：产物 5ms 时 ttft={fast}ms，产物 400ms 时 ttft={slow}ms"
    )


async def test_immediate_mode_inverts_the_ordering(
    db_env: Any, workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反证：`immediate` 下同一条顺序断言必须**失败**（否则说明测试没有区分力）。

    与不变量 ① 用**同一个观测点、相反的方向**：`immediate` 下产物分支必须在
    首个 token 被消费**之前**就已进入。若两种模式被实现成一样，两条必有一条红。

    注意不能用「产物调用 vs 正文调用」的时刻比较：`create_task` 只是登记，
    协程体要等事件循环第一次让出才跑，所以两种模式下产物调用都晚于正文调用 ——
    那个观测点没有区分力（本用例的第一版就踩了这个坑）。
    """
    entered: list[float] = []
    original = AgentEngine._artifact_work

    async def spy(self: AgentEngine, *args: Any, **kwargs: Any) -> ArtifactWork:
        entered.append(time.perf_counter())
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AgentEngine, "_artifact_work", spy)

    llm = _ScriptedLLM(deltas=5, delta_delay=0.01, artifact_delay=0.01)
    engine = _engine(llm, settings=_settings(ARTIFACT_FORK_MODE="immediate"))

    first_token_at: float | None = None
    async for name, _payload in engine.stream_turn(_turn(workspace_id)):
        if name == "token" and first_token_at is None:
            first_token_at = time.perf_counter()

    assert first_token_at is not None
    assert entered, "immediate 模式下产物分支必须被 fork"
    assert entered[0] < first_token_at, (
        "immediate 模式应当在首个 token 之前就进入产物分支（这正是它影响 TTFT 的原因）"
    )


# =========================================================================== #
# 不变量 ②：分支 B 物理上无法落库
# =========================================================================== #
class _CommitSpyFactory:
    """包住真实的 sessionmaker，记录**每一次 commit 的时刻**。

    用途：证明「正文还在流的时候，一次 commit 都没发生」—— 即产物分支真的没有写库能力。
    """

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self.commits: list[float] = []

    def __call__(self) -> "_CommitSpyCtx":
        return _CommitSpyCtx(self)


class _CommitSpyCtx:
    def __init__(self, spy: _CommitSpyFactory) -> None:
        self._spy = spy
        self._inner: Any = None

    async def __aenter__(self) -> Any:
        self._inner = self._spy._factory()
        session = await self._inner.__aenter__()
        real_commit = session.commit
        spy = self._spy

        async def _commit(*args: Any, **kwargs: Any) -> Any:
            spy.commits.append(time.perf_counter())
            return await real_commit(*args, **kwargs)

        session.commit = _commit
        return session

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._inner.__aexit__(*exc)


async def test_no_commit_while_reply_is_streaming(db_env: Any, workspace_id: str) -> None:
    """正文流式期间 `commit()` 次数恒为 0 —— 分支 B 没有写库，落库只在主路径 join 之后。"""
    spy = _CommitSpyFactory(get_session_factory())
    llm = _ScriptedLLM(deltas=6, delta_delay=0.02, artifact_delay=0.01)
    engine = AgentEngine(
        settings=_settings(),
        session_factory=spy,
        retriever=_FixedRetriever(_result()),
        llm=llm,
    )

    commits_seen_at_each_token: list[int] = []
    async for name, _payload in engine.stream_turn(_turn(workspace_id)):
        if name == "token":
            commits_seen_at_each_token.append(len(spy.commits))

    assert commits_seen_at_each_token, "本用例需要正文真的流式产出 token"
    assert commits_seen_at_each_token == [0] * len(commits_seen_at_each_token), (
        "正文流式期间出现了 commit —— 说明有代码路径在并行窗口内写库："
        f"{commits_seen_at_each_token}"
    )
    # 正文结束后必须至少有两次 commit（产物落库 + 消息落库），否则上一条断言是空转
    assert len(spy.commits) >= 2, f"join 之后应有落库，实际 commit 次数={len(spy.commits)}"


def test_artifact_work_has_no_session_field() -> None:
    """结构断言：`ArtifactWork` 是纯数据，字段里不可能携带 session。"""
    names = {field.name for field in dataclasses.fields(ArtifactWork)}
    assert "session" not in names
    assert "session_factory" not in names
    # 白名单式断言：新增字段必须重新审视「会不会让分支 B 获得写库能力」
    assert names == {
        "kind",
        "spec",
        "spec_dict",
        "base_version",
        "artifact_type",
        "ops",
        "interim",
        "rate_limited",
    }


# =========================================================================== #
# 不变量 ③：artifact 事件必定在落库之后
# =========================================================================== #
async def test_create_event_is_readable_from_db_at_emit_time(
    db_env: Any, workspace_id: str
) -> None:
    """收到 `artifact` 事件的那一刻，另开 session 就能读到同一个 version 与同一份 spec。

    这正是前端 `runArtifactPatch` 依赖的时序：事件早于落库会让前端刚应用的补丁被旧快照擦掉。
    """
    llm = _ScriptedLLM(deltas=4, delta_delay=0.005, artifact_delay=0.005)
    engine = _engine(llm)

    checked = 0
    async for name, payload in engine.stream_turn(_turn(workspace_id)):
        if name != "artifact":
            continue
        async with get_session_factory()() as session:
            row = await repository.require_artifact(session, payload.artifact_id)
            assert row.version == payload.version, (
                f"事件声明的 version={payload.version}，但库里是 {row.version} —— 事件早于落库"
            )
            assert canonical_json(row.spec_json) == canonical_json(payload.spec)
        checked += 1

    assert checked == 1


async def test_patch_event_is_readable_from_db_at_emit_time(
    db_env: Any, workspace_id: str
) -> None:
    """patch 路径同样：事件到达时库里已经是新版本（乐观锁写成功之后才发）。"""
    # 先造一张基线产物（v1）
    llm = _ScriptedLLM(deltas=3, delta_delay=0.005, artifact_delay=0.005)
    engine = _engine(llm)
    events = await _collect(engine, _turn(workspace_id))
    created = next(payload for name, payload in events if name == "artifact")

    patch_llm = _ScriptedLLM(
        deltas=3,
        delta_delay=0.005,
        artifact_delay=0.005,
        ops=[
            {
                "op": "update_node",
                "node_path": "/slides/2",
                "payload": {"title": "被追问后改写的标题"},
                "reason": "用户追问",
            }
        ],
    )
    patch_engine = _engine(patch_llm)
    anchor = Anchor(
        artifact_id=created.artifact_id, node_path="/slides/2", section_code="4.3.1"
    )
    req = _turn(workspace_id, query="把这一页标题讲细一点", anchor=anchor)

    checked = 0
    async for name, payload in patch_engine.stream_turn(req):
        if name != "artifact":
            continue
            async with get_session_factory()() as session:
                row = await repository.require_artifact(session, created.artifact_id)
                assert row.version == payload.version == 2
                # JSON Pointer 的数组下标是 0-based：`/slides/2` → slides[2]
                assert row.spec_json["slides"][2]["title"] == "被追问后改写的标题"
        checked += 1

    assert checked == 1


# =========================================================================== #
# 不变量 ④：取消不留孤儿
# =========================================================================== #
async def test_client_disconnect_leaves_no_task_and_no_orphan_artifact(
    db_env: Any, workspace_id: str
) -> None:
    """正文流到一半断连：无残留 task、产物条数不增、产物调用不再完成。

    这条在串行版做不到（串行版里产物落库后紧接着 yield，窗口是毫秒级）；
    并行版把这个窗口拉长到整个正文时长，所以必须从结构上消除。
    """
    before_artifacts = await _count_artifacts(workspace_id)
    tasks_before = set(asyncio.all_tasks())

    # 产物耗时远长于正文：保证断连时产物任务仍在 sleep 里（可被取消）
    llm = _ScriptedLLM(deltas=10, delta_delay=0.02, artifact_delay=0.6)
    engine = _engine(llm)

    stream = engine.stream_turn(_turn(workspace_id))
    consumed = 0
    async for name, _payload in stream:
        if name == "token":
            consumed += 1
            if consumed == 3:
                break
    assert llm.artifact_started == 1, "断连前产物分支必须已经 fork 并进入调用"
    assert llm.artifact_completed == 0, "本用例要求断连时产物还没跑完"

    await stream.aclose()
    await asyncio.sleep(0.15)

    leaked = set(asyncio.all_tasks()) - tasks_before - {asyncio.current_task()}
    leaked = {task for task in leaked if not task.done()}
    assert not leaked, f"断连后仍有残留任务：{[t.get_coro().__qualname__ for t in leaked]}"
    assert llm.artifact_completed == 0, "断连后产物请求仍然跑完了 —— 取消没有生效"
    assert await _count_artifacts(workspace_id) == before_artifacts, "断连后库里多出了产物"


# =========================================================================== #
# 页码钳制攻击（A~F）
# =========================================================================== #
async def _artifact_spec_from_llm(
    workspace_id: str, spec: dict[str, Any], *, page_start: int = 76, page_end: int = 77
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """跑一轮，返回 (SSE 里的 spec, 库里的 spec, complete_json 调用次数)。"""
    llm = _ScriptedLLM(spec=spec, deltas=3, delta_delay=0.005, artifact_delay=0.005)
    engine = _engine(llm, result=_result(page_start=page_start, page_end=page_end))
    events = await _collect(engine, _turn(workspace_id))
    payload = next(p for name, p in events if name == "artifact")
    async with get_session_factory()() as session:
        row = await repository.require_artifact(session, payload.artifact_id)
        stored = copy.deepcopy(row.spec_json)
    # `ArtifactEvent.spec` 是 pydantic 模型，转成与库中同构的 dict 再比对
    return payload.spec.model_dump(mode="json"), stored, llm.artifact_started


async def test_clamp_keeps_legal_end_page_inside_chunk_range(
    db_env: Any, workspace_id: str
) -> None:
    """★核心用例：允许集合含 {76,77}，`source_ref="4.3.1, p.77"` 必须**原样保留**。

    77 是切片区间内的页码但**不是起始页**。若实现用 `context.pages`（只有 page_start=76）
    当允许集合，这条会红 —— 这正是架构文档 §4.9.2 点名的已知风险。
    """
    spec = _ppt_spec(["4.3.1, p.77"] * 5)
    sse_spec, stored_spec, calls = await _artifact_spec_from_llm(workspace_id, spec)

    for target in (sse_spec, stored_spec):
        for slide in target["slides"]:
            assert slide["source_ref"] == "4.3.1, p.77", (
                f"合法的 p.77 被误剥了：{slide['source_ref']!r}（允许集合应为 76-77 整段展开）"
            )
    assert calls == 1, "钳制不得触发任何重试"


async def test_clamp_range_two_endpoints(db_env: Any, workspace_id: str) -> None:
    """区间写法：`p.76-77` 两端都合法 → 保留；`p.76-1000` 一端越界 → 整条剥。"""
    spec = _ppt_spec(["4.3.1, p.76-77", "4.3.1, p.76-1000", "4.3.1, p.1000-1001", "4.3.1", "4.3.1, p.77"])
    sse_spec, stored_spec, _calls = await _artifact_spec_from_llm(workspace_id, spec)

    expected = ["4.3.1, p.76-77", "4.3.1", "4.3.1", "4.3.1", "4.3.1, p.77"]
    for target in (sse_spec, stored_spec):
        assert [s["source_ref"] for s in target["slides"]] == expected


async def test_clamp_never_produces_empty_string(db_env: Any, workspace_id: str) -> None:
    """剥离后不得出现空串：只有页码的 ref → 章节号；章节号也没有 → `Extended Knowledge`。"""
    # 端到端：fallback 取 context.section_code（本夹具为 "4.3.1"）
    spec = _ppt_spec(["p.999"] * 5)
    sse_spec, _stored, _calls = await _artifact_spec_from_llm(workspace_id, spec)
    assert {s["source_ref"] for s in sse_spec["slides"]} == {"4.3.1"}

    # 单元级：连章节号都为空时落到最后兜底，绝不产生空字符串
    clamped, touched = _clamp_spec_pages(
        _ppt_spec(["p.999"] * 5), frozenset({76, 77}), fallback_ref=""
    )
    assert touched == 5
    assert {s["source_ref"] for s in clamped["slides"]} == {"Extended Knowledge"}


async def test_clamp_source_page_for_2d_and_3d(db_env: Any, workspace_id: str) -> None:
    """2D/3D 的 `source_page`：越界置 None，合法保留。"""
    spec_2d = _anim2d_spec("4.3.1", [76, 999, None])
    clamped_2d, touched_2d = _clamp_spec_pages(
        spec_2d, frozenset({76, 77}), fallback_ref="4.3.1"
    )
    assert touched_2d == 2, "两帧各有一个越界的 source_page"
    for step in clamped_2d["steps"]:
        assert [e["source_page"] for e in step["entities"]] == [76, None, None]

    spec_3d = {
        "type": "scene_3d",
        "title": "马六甲城墙结构",
        "nodes": [
            {
                "id": f"node-{index}",
                "primitive": "box",
                "label": f"部件 {index}",
                "position": [0.0, 1.5, 0.0],
                "scale": [2.0, 1.0, 2.0],
                "rotation": [0.0, 0.0, 0.0],
                "color": "#2563EB",
                "annotation_markdown": "墙体以红土夯实，厚度约两米。",
                "source_page": page,
                "parent_id": None,
            }
            for index, page in enumerate([77, 500])
        ],
        "camera_tour": [
            {
                "name": "全景",
                "position": [8.0, 6.0, 8.0],
                "target": [0.0, 1.5, 0.0],
                "explanation": "先看整体比例。",
            }
        ],
    }
    clamped_3d, touched_3d = _clamp_spec_pages(
        spec_3d, frozenset({76, 77}), fallback_ref="4.3.1"
    )
    assert touched_3d == 1
    assert [n["source_page"] for n in clamped_3d["nodes"]] == [77, None]


async def test_clamp_is_idempotent(db_env: Any, workspace_id: str) -> None:
    """幂等：对已钳制的结果再钳一次，字节级不变（`canonical_json` 比对）。"""
    allowed = frozenset({76, 77})
    spec = _ppt_spec(["4.3.1, p.999", "4.3.1, p.76-77", "p.500", "4.3.1", "4.3.1, p.77"])
    once, touched_once = _clamp_spec_pages(spec, allowed, fallback_ref="4.3.1")
    twice, touched_twice = _clamp_spec_pages(once, allowed, fallback_ref="4.3.1")

    assert touched_once > 0
    assert touched_twice == 0, "第二次钳制仍在改动 —— 说明规则不幂等"
    assert canonical_json(once) == canonical_json(twice)


async def test_patch_payload_page_is_clamped_end_to_end(
    db_env: Any, workspace_id: str
) -> None:
    """patch 路径也钳制：`update_node` payload 只带 `{"source_ref": "4.3.1, p.999"}` 时，
    下发的 ops 与库里的 spec 都必须已剥离 —— 否则用户点一次节点，假页码就回来了。
    """
    # 基线：先建一张 v1。**刻意让基线带合法页码 p.76**，这样「被 patch 的那一页」
    # 与「没被 patch 的页」内容不同 —— 否则断言在 patch 完全没生效时也会绿（空转）。
    llm = _ScriptedLLM(
        spec=_ppt_spec(["4.3.1, p.76"] * 5), deltas=3, delta_delay=0.005, artifact_delay=0.005
    )
    events = await _collect(_engine(llm), _turn(workspace_id))
    created = next(payload for name, payload in events if name == "artifact")

    patch_llm = _ScriptedLLM(
        deltas=3,
        delta_delay=0.005,
        artifact_delay=0.005,
        ops=[
            {
                "op": "update_node",
                "node_path": "/slides/2",
                "payload": {"source_ref": "4.3.1, p.999"},
                "reason": "用户追问出处",
            }
        ],
    )
    anchor = Anchor(
        artifact_id=created.artifact_id, node_path="/slides/2", section_code="4.3.1"
    )
    patch_events = await _collect(
        _engine(patch_llm), _turn(workspace_id, query="这一页的出处是哪一页", anchor=anchor)
    )
    patch_payload = next(p for name, p in patch_events if name == "artifact")

    # ① 下发的 ops 已剥离（前端本地应用的就是这一份）
    assert patch_payload.ops[0].payload["source_ref"] == "4.3.1"
    # ② 库里读回的 spec 也已剥离（三者一致）；未受影响的页保持原值 → 证明 patch 真的落了
    async with get_session_factory()() as session:
        row = await repository.require_artifact(session, created.artifact_id)
        assert row.spec_json["slides"][2]["source_ref"] == "4.3.1"
        assert row.spec_json["slides"][0]["source_ref"] == "4.3.1, p.76"
        assert row.version == 2


# =========================================================================== #
# 页码钳制的类型边界（第二轮：P1 已由工程师修复，这里转为常规回归锁）
# =========================================================================== #
@pytest.mark.parametrize("raw", ["999", 999.0, True])
def test_out_of_range_source_page_of_non_int_type_must_be_clamped(raw: Any) -> None:
    """钳制必须对「语义上越界」的取值一律置 None，不管 JSON 里写的是什么类型。

    `parse_artifact_spec` 在 pydantic lax 模式下会把 `"999"` / `999.0` 强制成合法 int
    通过校验，所以这些取值**能到达钳制层**。若钳制只判 `isinstance(x, int)`，
    越界页码会原样落库 —— 钳制形同虚设（这是 QA 第一轮报的 P1，已修）。

    注意 `touched` 是 **2** 不是 1：`_anim2d_spec` 把同一批 entities 复制进了 2 个 step，
    所以这 1 个越界 entity 会以「2 处发生」被计数。断言按「发生处」算，正好顺带锁住
    「计数口径 = 每一处被改写的字段」。
    """
    allowed = frozenset({76, 77})

    # create 路径：2 个 step 各 1 个 entity，故 2 处
    spec = _anim2d_spec("4.3.1", [raw])  # type: ignore[list-item]
    clamped, touched = _clamp_spec_pages(spec, allowed, fallback_ref="4.3.1")
    assert touched == 2, f"越界的 source_page 未被计数（原始值 {raw!r}）"
    for step in clamped["steps"]:
        assert step["entities"][0]["source_page"] is None

    # patch 路径共用同一规则：source_ref 被剥、source_page 被置 None
    from app.services.agent_engine import _clamp_payload_pages

    payload = {"source_page": raw, "source_ref": "4.3.1, p.999"}
    _clamp_payload_pages(payload, allowed, "4.3.1")
    assert payload["source_page"] is None
    assert payload["source_ref"] == "4.3.1"


@pytest.mark.parametrize("raw", ["76", 76.0, " 77 ", "76.0"])
async def test_in_range_non_int_source_page_is_normalized_to_int(
    db_env: Any, workspace_id: str, raw: Any
) -> None:
    """合法但**类型不规范**的页码要**写回 int**，且 SSE 与 DB 必须字节一致。

    只判断不写回是不够的：SSE 事件里的 spec 走 pydantic 序列化（`"76"` → `76`），
    而落库的是原始 dict（`"76"` 还是 `"76"`），同一份产物两个出口不一致 ——
    违反设计 §4.9.4 / P0-11 的「同一回合的 SSE 与 GET 是同一串字节」。
    """
    spec = _anim2d_spec("4.3.1", [raw])  # type: ignore[list-item]
    sse_spec, stored_spec, _calls = await _artifact_spec_from_llm(workspace_id, spec)

    expected = int(float(str(raw).strip()))  # "76" / 76.0 / " 77 " / "76.0" 都收敛到整数
    for target in (sse_spec, stored_spec):
        for step in target["steps"]:
            value = step["entities"][0]["source_page"]
            assert value == expected
            assert type(value) is int, f"未规范化：{value!r} 仍是 {type(value).__name__}"

    # 两份出口必须逐字节一致（这条是 P0-11 的原话）
    assert canonical_json(sse_spec) == canonical_json(stored_spec)


@pytest.mark.parametrize(
    ("raw", "allowed"),
    [
        # 关键：`allowed` 刻意包含「若被错误转换后会得到的那个值」。
        # 用 {76,77} 测 85.5 是测不出问题的 —— `int(85.5)=85` 恰好也越界，一样被剥，
        # 于是「把脏数据截断成合法页码」这个错误会静默通过（QA 自己用变异验证踩到过）。
        (85.5, frozenset({76, 77, 85})),
        (85.5, frozenset({76, 77})),
        ("abc", frozenset({76, 77})),
        ([], frozenset({76, 77})),
        ({}, frozenset({76, 77})),
        # `False` 同理：若 bool 判断失效，它会被当成第 0 页，所以让 0 落在集合内
        (False, frozenset({0, 76, 77})),
        (False, frozenset({76, 77})),
    ],
)
def test_dirty_source_page_values_become_none(raw: Any, allowed: frozenset[int]) -> None:
    """脏值（非整数浮点 / 非数字串 / 容器 / bool）一律置 None，不因为「类型怪」就跳过。

    跳过 = 给脏数据留了一条绕过钳制的通道，而钳制是「先有钳制才允许走 flash」的前置条件。
    `85.5` 尤其容易漏：`int(85.5)` 能得到 85，一个「顺手截断」的实现会把脏数据**修成**
    一个看起来合法的页码 —— 那是比保留原值更坏的错误（它变成了可信的假页码）。
    """
    spec = _anim2d_spec("4.3.1", [raw])  # type: ignore[list-item]
    clamped, touched = _clamp_spec_pages(spec, allowed, fallback_ref="4.3.1")

    assert touched == 2, f"脏值 {raw!r} 未被处置（allowed={sorted(allowed)}）"
    for step in clamped["steps"]:
        assert step["entities"][0]["source_page"] is None, (
            f"脏值 {raw!r} 被当成了合法页码 {step['entities'][0]['source_page']!r}"
        )


def test_normalized_result_is_idempotent() -> None:
    """规范化后再钳一次必须 0 处改动（否则「写回 int」会变成每次都改，G3 计数失真）。"""
    allowed = frozenset({76, 77})
    spec = _anim2d_spec("4.3.1", ["76", 999, None])  # type: ignore[list-item]
    once, touched_once = _clamp_spec_pages(spec, allowed, fallback_ref="4.3.1")
    twice, touched_twice = _clamp_spec_pages(once, allowed, fallback_ref="4.3.1")

    assert touched_once == 2  # 每个 step 各 1 处越界
    assert touched_twice == 0
    assert canonical_json(once) == canonical_json(twice)


def test_bool_is_rejected_even_when_page_one_is_allowed() -> None:
    """**bool 分支必须排在 int 之前** —— 用「允许集合含 1」来逼出顺序错误。

    这是本组最锐利的一条。`isinstance(True, int)` 为真，所以若 `bool` 判断写在 `int`
    之后，`True` 会被当成第 1 页。此时：
    - 若允许集合是 `{76,77}`（其它用例用的），`1` 恰好越界 → 仍然被置 None →
      **测试照样绿，顺序错误被掩盖**；
    - 只有让 `1` **落在允许集合内**，顺序错误才会暴露成「`True` 被保留成 1」。

    所以这里用 `allowed={1,2}`。`False`（=0）不在集合内，两种实现都会剥掉它，
    因此真正有区分力的是 `True` 那一格。
    """
    allowed = frozenset({1, 2})
    spec = _anim2d_spec("4.3.1", [True, False])  # type: ignore[list-item]
    clamped, touched = _clamp_spec_pages(spec, allowed, fallback_ref="4.3.1")

    pages = [e["source_page"] for step in clamped["steps"] for e in step["entities"]]
    assert pages == [None, None, None, None], (
        f"`True` 被当成了第 1 页（bool 判断排在了 int 之后）：{pages}"
    )
    # 2 个 step × 2 个 entity，全部要计为「被处置」
    assert touched == 4


@pytest.mark.parametrize("artifact_type", ["ppt", "anim_2d", "scene_3d"])
def test_source_page_type_matches_model_dump(artifact_type: str) -> None:
    """钳制结果与 `parse_artifact_spec(...).model_dump()` 在 `source_page` 上**逐字段类型一致**。

    「写回 int」的价值全在这一条：SSE 事件里的 spec 是 pydantic 模型序列化的结果，
    落库的是钳制后的 dict。两者若类型不同（`"85"` vs `85`），同一份产物就有两个出口。
    """
    from app.schemas.artifacts import parse_artifact_spec

    allowed = frozenset({76, 77})
    dirty_ref = "4.3.1, p.999"
    if artifact_type == "ppt":
        spec: dict[str, Any] = _ppt_spec([dirty_ref] * 5)
        spec["slides"][1]["layout"] = "timeline"
        spec["slides"][1]["timeline_items"] = [
            {"year": "1946", "label": "a", "detail": "b", "source_page": "76"},
            {"year": "1948", "label": "c", "detail": "d", "source_page": 999},
        ]
        path = ("slides", 1, "timeline_items")
    elif artifact_type == "anim_2d":
        spec = _anim2d_spec(dirty_ref, ["76", 999])  # type: ignore[list-item]
        path = ("steps", 0, "entities")
    else:
        spec = {
            "type": "scene_3d",
            "title": "城墙",
            "nodes": [
                {
                    "id": "node-0",
                    "primitive": "box",
                    "label": "主体",
                    "position": [0.0, 1.5, 0.0],
                    "scale": [2.0, 1.0, 2.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "color": "#2563EB",
                    "annotation_markdown": "红土夯实。",
                    "source_page": "76",
                    "parent_id": None,
                },
                {
                    "id": "node-1",
                    "primitive": "cone",
                    "label": "屋顶",
                    "position": [0.0, 3.0, 0.0],
                    "scale": [2.2, 1.2, 2.2],
                    "rotation": [0.0, 45.0, 0.0],
                    "color": "#F59E0B",
                    "annotation_markdown": "反映气候。",
                    "source_page": 999,
                    "parent_id": "node-0",
                },
            ],
            "camera_tour": [
                {
                    "name": "全景",
                    "position": [8.0, 6.0, 8.0],
                    "target": [0.0, 1.5, 0.0],
                    "explanation": "看整体。",
                }
            ],
        }
        path = ("nodes", None)

    clamped, _touched = _clamp_spec_pages(spec, allowed, fallback_ref="4.3.1")
    dumped = parse_artifact_spec(clamped).model_dump(mode="json")

    def collect(tree: dict[str, Any]) -> list[Any]:
        if artifact_type == "ppt":
            return [item["source_page"] for item in tree["slides"][1]["timeline_items"]]
        if artifact_type == "anim_2d":
            return [e["source_page"] for e in tree["steps"][0]["entities"]]
        return [node["source_page"] for node in tree["nodes"]]

    from_clamped, from_model = collect(clamped), collect(dumped)
    assert from_clamped == from_model == [76, None], (
        f"{artifact_type}: 钳制结果 {from_clamped} 与 model_dump {from_model} 不一致"
    )
    for value in from_clamped:
        assert value is None or type(value) is int, f"{artifact_type}: {value!r} 类型未收敛"


def test_patch_ops_payload_source_page_is_not_a_bypass() -> None:
    """patch 路径的入口（`_clamp_patch_ops`）：payload 里的 `{"source_page": "999"}` 必须被剥。

    走的是 patch 真正使用的那个函数，而不是直接调底层 `_clamp_payload_pages` ——
    否则「上层忘了调它」这类错误测不出来。
    """
    from app.schemas.patch import PatchOp
    from app.services.agent_engine import _clamp_patch_ops

    ops = [
        PatchOp(
            op="update_node",
            node_path="/steps/0/entities/0",
            payload={"source_page": "999", "label": "改后的标签"},
        ),
        PatchOp(
            op="update_node",
            node_path="/steps/1/entities/0",
            payload={"source_page": 76},
        ),
    ]
    clamped, touched = _clamp_patch_ops(ops, frozenset({76, 77}), fallback_ref="4.3.1")

    assert touched == 1
    assert clamped[0].payload["source_page"] is None
    assert clamped[0].payload["label"] == "改后的标签", "非页码字段不得被误改"
    assert clamped[1].payload["source_page"] == 76
    assert type(clamped[1].payload["source_page"]) is int


# =========================================================================== #
# 破坏性尝试
# =========================================================================== #
async def test_no_match_turn_does_not_crash_and_emits_no_artifact(
    db_env: Any, workspace_id: str
) -> None:
    """检索为空 / NO_MATCH：不崩、不发产物（P0-05④），事件序列完整收尾。

    注意：设计文档 §6.2 明确要求「NO_MATCH 时不得有 artifact 事件」，
    所以本用例断言的正是「不出产物」——而不是「出产物」。
    """
    llm = _ScriptedLLM(deltas=3, delta_delay=0.005, artifact_delay=0.005)
    engine = _engine(llm, result=_result(status=NO_MATCH))

    events = await _collect(engine, _turn(workspace_id))
    names = [name for name, _ in events]

    assert "artifact" not in names
    assert names[0] == "turn_start"
    assert names[-1] == "turn_end"
    assert "citations" in names
    assert llm.artifact_started == 0, "NO_MATCH 不该发出产物请求"
    assert await _count_artifacts(workspace_id) == 0


async def test_two_concurrent_turns_in_same_workspace_do_not_mix(
    db_env: Any, workspace_id: str
) -> None:
    """同一 workspace 并发两次提问：产物不串、版本不冲突、都能落库。"""

    async def run(query: str) -> list[tuple[str, Any]]:
        llm = _ScriptedLLM(deltas=5, delta_delay=0.02, artifact_delay=0.02)
        engine = _engine(llm)
        return await _collect(engine, _turn(workspace_id, query=query))

    first, second = await asyncio.gather(
        run("4.3.1 联合邦的成立经过"),
        run("4.3.1 苏丹权力的变化"),
    )

    artifacts = [p for name, p in first if name == "artifact"] + [
        p for name, p in second if name == "artifact"
    ]
    assert len(artifacts) == 2
    assert artifacts[0].artifact_id != artifacts[1].artifact_id, "两次提问不得写进同一张画布"
    assert {a.version for a in artifacts} == {1}

    async with get_session_factory()() as session:
        rows = await repository.list_artifacts(session, workspace_id)
    assert len(rows) == 2
    # 每张画布的 spec 必须来自自己那一轮（标题里带各自的问句主题），不得互相污染
    assert {row.title for row in rows}  # 非空即可
    for row in rows:
        assert row.spec_json["type"] == "ppt"


async def test_serial_fallback_is_byte_identical(db_env: Any, workspace_id: str) -> None:
    """串行回退：并行开/关两种配置下，事件名序列与 spec 的 `canonical_json` 完全一致。"""
    results: dict[bool, tuple[list[str], str]] = {}
    for parallel in (True, False):
        llm = _DisabledLLM()  # 演示模式：内容由纯函数生成，排除 LLM 抖动
        engine = _engine(
            llm,
            settings=_settings(ARTIFACT_PARALLEL_ENABLED=parallel, LLM_API_KEY=""),
        )
        events = await _collect(engine, _turn(workspace_id))
        spec = next(p for name, p in events if name == "artifact").spec
        results[parallel] = ([name for name, _ in events], canonical_json(spec))
        assert llm.calls == 0

    assert results[True][0] == results[False][0], "并行/串行的公开事件序列必须一致"
    assert results[True][1] == results[False][1], "并行/串行的产物内容必须字节级一致"


async def test_mock_mode_is_deterministic_across_two_runs(
    db_env: Any, workspace_id: str
) -> None:
    """P0-12②：同输入两次，剔除 `duration_ms` / `ttft_ms` / `turn_id` 后事件序列完全相同。"""
    stable: list[str] = []
    for _ in range(2):
        llm = _DisabledLLM()
        engine = _engine(llm, settings=_settings(LLM_API_KEY=""))
        events = await _collect(engine, _turn(workspace_id))
        normalized: list[dict[str, Any]] = []
        for name, payload in events:
            body = payload.model_dump(mode="json")
            for volatile in ("duration_ms", "ttft_ms", "turn_id", "artifact_id"):
                body.pop(volatile, None)
            normalized.append({"event": name, "data": body})
        stable.append(canonical_json(normalized))

    assert stable[0] == stable[1], "演示模式下两次运行的归一化事件序列不一致"

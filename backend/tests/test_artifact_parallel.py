"""并行化（T02）：fork 时机、join 顺序、串行回退、错误归因、测量约定。

**全部离线**：假 `LLMClient` 注入，不打真实网络、不烧 token。

本文件守的是什么（为什么值得单独一个文件）：

1. **「产物先算完，也必须最后发」**。并行之后产物事件可能比正文先就绪，
   若允许它插进 token 流中间，前端会先收到 artifact 再收 token —— §5.4 的顺序契约
   当场失效。所以顺序断言是本文件的第一组。
2. **「TTFT 不受产物影响」必须是结构性的**。默认 fork 点在 `ttft_ms` 定格之后，
   测试用「记录调用时刻」来证明，而不是「跑一遍看着挺快」。同一个断言在
   `immediate` 模式下**必须失败**，否则说明测试没有区分力。
3. **测量约定（设计 §3.3.4 MC-1–MC-4）**。这些约定被破坏时**不会报错**，
   只会让 `flash_quality_check` 的指标静默变成 0% —— 看起来全绿，实际什么都没测到。
   所以每条约定在这里各配一条断言。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.config import Settings, reset_settings_cache
from app.db import repository
from app.db.session import get_session_factory
from app.schemas.artifacts import parse_artifact_spec
from app.services import agent_engine as engine_module
from app.services.agent_engine import AgentEngine, TurnRequest
from app.services.llm_client import RateLimitedError
from app.services.retriever import SYLLABUS_MATCH, RetrievalResult, RetrievedChunk

CHUNK_TEXT = (
    "1946 年 4 月 1 日，Malayan Union 正式生效，由英国总督直接统治。"
    "在 Malayan Union 宪制下，苏丹仅保留宗教与习俗事务的权限。"
    "1948 年 2 月 1 日，马来亚联合邦成立，恢复了苏丹在州务上的权力。"
)


def _retrieval() -> RetrievalResult:
    chunk = RetrievedChunk(
        chunk_id="chunk-1",
        section_code="4.1",
        section_title="Malayan Union",
        page_start=76,
        page_end=77,
        score=0.9,
        snippet=CHUNK_TEXT[:80],
        content=CHUNK_TEXT,
    )
    return RetrievalResult(
        chunks=(chunk,),
        grader_status=SYLLABUS_MATCH,
        section_scope="4.1",
        top_score=0.9,
        query_terms=("Malayan", "Union"),
    )


class _FakeRetriever:
    """只回一份固定检索结果，把测试与真实检索/评分完全解耦。"""

    def __init__(self) -> None:
        self.calls = 0

    async def retrieve(self, *args: Any, **kwargs: Any) -> RetrievalResult:
        self.calls += 1
        return _retrieval()


class FakeLLM:
    """假 LLM：可控的 delta 节奏、可控的产物耗时、可注入的异常。

    记录**每次调用的时刻**，TTFT 保护与「断连后不再发请求」都靠这份时间线判定。
    """

    enabled = True

    def __init__(
        self,
        *,
        deltas: int = 6,
        delta_delay: float = 0.01,
        artifact_delay: float = 0.05,
        spec_error: BaseException | None = None,
        raise_after_deltas: int | None = None,
    ) -> None:
        self.deltas = deltas
        self.delta_delay = delta_delay
        self.artifact_delay = artifact_delay
        self.spec_error = spec_error
        self.raise_after_deltas = raise_after_deltas
        # [(kind, model, perf_counter)]，kind ∈ {"reply", "artifact"}
        self.calls: list[tuple[str, str | None, float]] = []
        self.first_delta_at: float | None = None
        self.artifact_calls = 0

    async def stream_text(self, **kwargs: Any):
        self.calls.append(("reply", kwargs.get("model"), time.perf_counter()))
        for index in range(self.deltas):
            if self.raise_after_deltas is not None and index >= self.raise_after_deltas:
                from app.errors import AppError, ErrorCode

                raise AppError(ErrorCode.LLM_UPSTREAM_ERROR, "假 LLM 中断")
            await asyncio.sleep(self.delta_delay)
            if index == 0:
                # 记在 **yield 之前、sleep 之后**：这才是「第一个 delta 真正产出」的时刻。
                # 若记在 sleep 之前，`immediate` 模式的反证会失效 —— 因为那时产物任务
                # 还没被调度，测出来的差异只是「谁先被 append 进 list」。
                self.first_delta_at = time.perf_counter()
            yield f"第 {index} 段。"

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.artifact_calls += 1
        self.calls.append(("artifact", kwargs.get("model"), time.perf_counter()))
        await asyncio.sleep(self.artifact_delay)
        if self.spec_error is not None:
            raise self.spec_error
        return {
            "type": "ppt",
            "topic": "Malayan Union",
            "slides": [
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
                    "source_ref": "4.1, p.76",
                }
                for index in range(5)
            ],
        }


@pytest.fixture()
async def workspace_id(db_env: Any) -> str:
    """建一个真实 workspace：产物落库有外键，假数据过不去。"""
    async with get_session_factory()() as session:
        workspace = await repository.create_workspace(session, title="并行化测试")
        await session.commit()
        return workspace.id


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "LLM_API_KEY": "test-key",  # 非空 = 非演示模式，才会走 LLM 分支
        "LLM_MODEL_ARTIFACT": "artifact-model",
        "LLM_MODEL_ROUTER": "router-model",
        "ARTIFACT_PARALLEL_ENABLED": True,
        "ARTIFACT_FORK_MODE": "after_first_token",
        "ARTIFACT_RATE_LIMIT_RETRY": 1,
    }
    base.update(overrides)
    return Settings(**base)


async def _run(engine: AgentEngine, workspace_id: str, **kwargs: Any) -> list[tuple[str, Any]]:
    request = TurnRequest(
        workspace_id=workspace_id,
        query="4.1 Malayan Union 的影响",
        artifact_type_override="ppt",
        **kwargs,
    )
    return [event async for event in engine.stream_turn(request)]


def _build(settings: Settings, llm: FakeLLM, retriever: Any = None) -> AgentEngine:
    return AgentEngine(
        settings=settings,
        session_factory=get_session_factory(),
        retriever=retriever or _FakeRetriever(),
        llm=llm,
    )


# --------------------------------------------------------------------------- #
# B1 / B2 / B3 / B4：并行真的并行 + 顺序契约
# --------------------------------------------------------------------------- #
async def test_parallel_is_actually_parallel(db_env: Any, workspace_id: str) -> None:
    """B1：总耗时 < 正文耗时 + 产物耗时（否则等于没并行）。"""
    llm = FakeLLM(deltas=8, delta_delay=0.03, artifact_delay=0.12)
    engine = _build(_settings(), llm)

    started = time.perf_counter()
    events = await _run(engine, workspace_id)
    elapsed = time.perf_counter() - started

    reply_ms = llm.deltas * llm.delta_delay
    artifact_ms = llm.artifact_delay
    assert elapsed < (reply_ms + artifact_ms) * 0.95, (
        f"并行未生效：总耗时 {elapsed * 1000:.0f}ms ≥ 串行估算 "
        f"{(reply_ms + artifact_ms) * 1000:.0f}ms"
    )
    assert any(name == "artifact" for name, _ in events)


async def test_artifact_always_after_last_token(db_env: Any, workspace_id: str) -> None:
    """B2：产物先算完也必须最后发（PRD ②③）。"""
    llm = FakeLLM(deltas=6, delta_delay=0.02, artifact_delay=0.001)
    engine = _build(_settings(), llm)

    names = [name for name, _ in await _run(engine, workspace_id)]

    assert names.count("artifact") == 1
    assert names.index("artifact") > max(i for i, n in enumerate(names) if n == "token")
    assert names.index("artifact") < names.index("citations") < names.index("turn_end")


async def test_event_sequence_is_stable_over_repeats(db_env: Any, workspace_id: str) -> None:
    """B4：连续 20 次的事件名序列 100% 合规（并行不得引入抖动）。"""
    engine = _build(_settings(), FakeLLM(deltas=3, delta_delay=0.001, artifact_delay=0.001))
    pattern = ("turn_start", "retrieval", "modality_decision")

    for _ in range(20):
        names = [name for name, _ in await _run(engine, workspace_id)]
        assert tuple(names[:3]) == pattern
        assert names[-3:] == ["artifact", "citations", "turn_end"]
        assert names.count("artifact") == 1


async def test_no_token_turn_still_produces_artifact(db_env: Any, workspace_id: str) -> None:
    """B8：正文一个 token 都没有时，产物分支仍要启动并正常下发。

    这是 `after_first_token` 的结构性副作用：fork 点长在「第一个 token」上，
    没有 token 就没有 fork。此时必须回落到串行路径，而不是**静默不生成产物**。
    """
    llm = FakeLLM(deltas=0, delta_delay=0.0, artifact_delay=0.001)
    engine = _build(_settings(), llm)

    names = [name for name, _ in await _run(engine, workspace_id)]

    assert "token" not in names
    assert names.count("artifact") == 1
    assert names[-1] == "turn_end"


# --------------------------------------------------------------------------- #
# B5：TTFT 保护（含反证）
# --------------------------------------------------------------------------- #
async def test_ttft_is_measured_before_artifact_request(
    db_env: Any, workspace_id: str
) -> None:
    """B5（默认模式）：产物请求发出时，首个 delta 已经到达。"""
    llm = FakeLLM(deltas=5, delta_delay=0.01, artifact_delay=0.01)
    engine = _build(_settings(), llm)

    await _run(engine, workspace_id)

    artifact_at = next(at for kind, _, at in llm.calls if kind == "artifact")
    assert llm.first_delta_at is not None
    assert llm.first_delta_at < artifact_at, "默认模式下产物请求必须在首个 delta 之后才发出"


async def test_immediate_mode_forks_before_first_token(
    db_env: Any, workspace_id: str
) -> None:
    """B5（反证）：`immediate` 下同一断言必须失败 —— 证明上一条测试有区分力。

    没有这条反证，B5 可能在「两种模式都串行」的实现下也绿，那测试就是自欺。
    """
    llm = FakeLLM(deltas=5, delta_delay=0.01, artifact_delay=0.01)
    engine = _build(_settings(ARTIFACT_FORK_MODE="immediate"), llm)

    await _run(engine, workspace_id)

    artifact_at = next(at for kind, _, at in llm.calls if kind == "artifact")
    assert llm.first_delta_at is not None
    assert artifact_at < llm.first_delta_at, "immediate 模式应当先发产物请求再收到首个 delta"


# --------------------------------------------------------------------------- #
# E1：串行回退
# --------------------------------------------------------------------------- #
async def test_parallel_disabled_uses_serial_path(
    db_env: Any, workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1：关闭并行后必须走 `_create_artifact_stream`（老路径），且事件序列不变。"""
    called: list[str] = []
    original = AgentEngine._create_artifact_stream

    async def spy(self: AgentEngine, *args: Any, **kwargs: Any):
        called.append("serial")
        async for event in original(self, *args, **kwargs):
            yield event

    monkeypatch.setattr(AgentEngine, "_create_artifact_stream", spy)

    engine = _build(_settings(ARTIFACT_PARALLEL_ENABLED=False), FakeLLM(deltas=3))
    names = [name for name, _ in await _run(engine, workspace_id)]

    assert called == ["serial"]
    assert names[-3:] == ["artifact", "citations", "turn_end"]


async def test_parallel_and_serial_produce_identical_spec(
    db_env: Any, workspace_id: str
) -> None:
    """E1/B6：同一输入下，并行与串行的事件名序列与 spec 完全一致。"""
    payloads: dict[bool, Any] = {}
    for parallel in (True, False):
        llm = FakeLLM(deltas=3, delta_delay=0.001, artifact_delay=0.001)
        engine = _build(_settings(ARTIFACT_PARALLEL_ENABLED=parallel), llm)
        events = await _run(engine, workspace_id)
        payloads[parallel] = ([name for name, _ in events], events[-3][1].spec)

    assert payloads[True][0] == payloads[False][0]
    assert payloads[True][1] == payloads[False][1]


# --------------------------------------------------------------------------- #
# C3 / C4：错误归因
# --------------------------------------------------------------------------- #
async def test_unexpected_branch_error_does_not_break_main_path(
    db_env: Any, workspace_id: str
) -> None:
    """C4/E6：分支 B 抛未预期异常 → INTERNAL_ERROR + citations + turn_end 齐全。

    串行版里这类异常会冒到 `_event_stream`，把 citations 一起带走；并行版必须兜住。
    """
    llm = FakeLLM(deltas=3, delta_delay=0.001, spec_error=RuntimeError("意外炸了"))
    engine = _build(_settings(), llm)

    events = await _run(engine, workspace_id)
    names = [name for name, _ in events]

    assert names[-2:] == ["citations", "turn_end"]
    errors = [payload for name, payload in events if name == "error"]
    assert errors and errors[0].code == "INTERNAL_ERROR"
    assert errors[0].stage == "generate"


async def test_artifact_db_failure_keeps_citations(
    db_env: Any, workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3：产物落库失败 → error 之后仍有 citations 与 turn_end（保护既有意图）。"""
    from app.errors import AppError, ErrorCode

    async def boom(*args: Any, **kwargs: Any):
        raise AppError(ErrorCode.INTERNAL_ERROR, "落库失败")

    monkeypatch.setattr(repository, "create_artifact", boom)

    engine = _build(_settings(), FakeLLM(deltas=3, delta_delay=0.001))
    events = await _run(engine, workspace_id)
    names = [name for name, _ in events]

    assert "artifact" not in names
    assert names[-2:] == ["citations", "turn_end"]
    assert any(name == "error" for name in names)


# --------------------------------------------------------------------------- #
# D1–D4：429 → 串行降级
# --------------------------------------------------------------------------- #
class _RateLimitedThenOK(FakeLLM):
    """第一次产物调用 429，之后正常 —— 用来验证「正文结束后串行重试」。"""

    def __init__(self, *, fail_times: int = 1, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail_times = fail_times

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        if self.artifact_calls < self.fail_times:
            self.artifact_calls += 1
            self.calls.append(("artifact", kwargs.get("model"), time.perf_counter()))
            raise RateLimitedError("假 429")
        return await super().complete_json(**kwargs)


async def test_rate_limit_retries_serially_and_succeeds(
    db_env: Any, workspace_id: str
) -> None:
    """D1：429 → 正文结束后串行重试一次 → 成功，且**没有** error 事件。"""
    llm = _RateLimitedThenOK(fail_times=1, deltas=3, delta_delay=0.001)
    engine = _build(_settings(), llm)

    events = await _run(engine, workspace_id)
    names = [name for name, _ in events]

    assert names.count("artifact") == 1
    assert "error" not in names, "429 属于可恢复的限流，串行重试成功时不该给用户报错"
    assert llm.artifact_calls == 2


async def test_rate_limit_exhausted_falls_back_to_mock(
    db_env: Any, workspace_id: str
) -> None:
    """D2：一直 429 → 降级 mock 产物，且 error 归因正确（MC-1 / MC-4）。"""
    llm = _RateLimitedThenOK(fail_times=99, deltas=3, delta_delay=0.001)
    engine = _build(_settings(), llm)

    events = await _run(engine, workspace_id)
    names = [name for name, _ in events]
    errors = [payload for name, payload in events if name == "error"]

    assert names.count("artifact") == 1
    assert len(errors) == 2
    # MC-4：限流那条必须是 stage="generate"，不得伪装成 "validate"（否则虚增 failed_calls）
    assert errors[0].stage == "generate"
    assert errors[0].retryable is True
    # MC-1：降级那条必须是 validate + retryable=False + 文案含「降级」，G2 就靠它识别
    assert errors[1].stage == "validate"
    assert errors[1].retryable is False
    assert "降级" in errors[1].message


async def test_rate_limit_retry_disabled(db_env: Any, workspace_id: str) -> None:
    """D4：`ARTIFACT_RATE_LIMIT_RETRY=0` → 不重试，直接降级。

    这条同时守住 `_join_artifact_work` 末尾那个 `if work.rate_limited` 分支 ——
    它在默认配置（retry=1）下看起来是死代码，但 retry=0 时是**唯一**把「deferred 的
    限流结果」转成降级的路径。删掉它，这一轮既没有产物也没有 error 事件（画布白屏）。
    """
    llm = _RateLimitedThenOK(fail_times=99, deltas=3, delta_delay=0.001)
    engine = _build(_settings(ARTIFACT_RATE_LIMIT_RETRY=0), llm)

    events = await _run(engine, workspace_id)
    names = [name for name, _ in events]
    errors = [payload for name, payload in events if name == "error"]

    assert llm.artifact_calls == 1, "retry=0 不该再发第二次产物请求"
    # 降级必须真的发生：mock 产物 + 两条 error（限流 generate / 降级 validate）
    assert names.count("artifact") == 1, "retry=0 也必须产出 mock 产物，否则画布白屏"
    assert [e.stage for e in errors] == ["generate", "validate"]
    assert "降级" in errors[1].message


async def test_rate_limit_does_not_trigger_contract_retries(
    db_env: Any, workspace_id: str
) -> None:
    """D3：429 不得被当成契约失败重试 3 次（调用次数必须是 1 + 1 次串行重试）。"""
    llm = _RateLimitedThenOK(fail_times=99, deltas=3, delta_delay=0.001)
    engine = _build(_settings(ARTIFACT_RATE_LIMIT_RETRY=0), llm)

    events = await _run(engine, workspace_id)
    validate_errors = [
        payload
        for name, payload in events
        if name == "error" and payload.stage == "validate" and payload.retryable
    ]

    assert llm.artifact_calls == 1, "429 不该进入 3 次契约重试"
    assert not validate_errors


# --------------------------------------------------------------------------- #
# 测量约定（设计 §3.3.4）—— 破坏时不会报错，只会让指标静默变 0
# --------------------------------------------------------------------------- #
def test_mc1_downgrade_message_is_stable() -> None:
    """MC-1：降级 error 的三个特征缺一不可（脚本靠它们识别 mock 降级）。"""
    engine = _build(_settings(), FakeLLM())
    work = engine._rate_limit_fallback(
        "create",
        TurnRequest(workspace_id="w", query="q"),
        _decision(),
        _context(),
        RateLimitedError("假 429"),
    )
    downgrade = work.interim[-1][1]

    assert downgrade.stage == "validate"
    assert downgrade.retryable is False
    assert "降级" in downgrade.message


def test_mc2_parse_artifact_spec_is_module_level() -> None:
    """MC-2：`parse_artifact_spec` 必须是模块级可替换属性（脚本直接赋值包住它）。"""
    assert hasattr(engine_module, "parse_artifact_spec")
    assert callable(engine_module.parse_artifact_spec)


def test_mc3_instrumented_methods_still_exist() -> None:
    """MC-3：三个方法名必须存在且是 async generator（脚本按名埋点）。"""
    import inspect

    for name in ("_reply_stream", "_create_artifact_stream", "_generate_spec_stream"):
        method = getattr(AgentEngine, name)
        assert inspect.isasyncgenfunction(method), f"{name} 必须仍是 async generator"


def test_mc4_rate_limit_error_uses_generate_stage() -> None:
    """MC-4：429 的 error 必须是 stage="generate"（用 validate 会虚增 failed_calls）。"""
    engine = _build(_settings(), FakeLLM())
    work = engine._rate_limit_fallback(
        "create",
        TurnRequest(workspace_id="w", query="q"),
        _decision(),
        _context(),
        RateLimitedError("假 429"),
    )
    rate_limited = work.interim[0][1]

    assert rate_limited.stage == "generate"
    assert rate_limited.code == "LLM_UPSTREAM_ERROR"


def test_mc5_clamp_function_is_module_level() -> None:
    """MC-5：`_clamp_spec_pages` 必须是模块级可替换属性（脚本按名包住它采「钳制前」样本）。

    破坏后果：`flash_quality_check.instrument_page_clamp` 的 `setattr` 会作用在别处，
    「钳制前」样本采不到 → 报告里前后两列变成同一份数据，**编造率永远等于 0%**。
    这类退化不报错，只会让 G3 双口径变成单口径 —— 与 MC-2 完全同型。
    """
    assert hasattr(engine_module, "_clamp_spec_pages")
    assert callable(engine_module._clamp_spec_pages)
    # 不能被改成实例方法：脚本是按模块属性赋值的
    assert not hasattr(AgentEngine, "_clamp_spec_pages")


# --------------------------------------------------------------------------- #
# 取消：断连不泄漏后台任务
# --------------------------------------------------------------------------- #
async def test_disconnect_cancels_artifact_task(db_env: Any, workspace_id: str) -> None:
    """C1：读几帧后关闭生成器 → 子任务被取消、无残留、不再发产物请求。"""
    llm = FakeLLM(deltas=50, delta_delay=0.02, artifact_delay=0.5)
    engine = _build(_settings(), llm)

    request = TurnRequest(
        workspace_id=workspace_id,
        query="4.1 Malayan Union 的影响",
        artifact_type_override="ppt",
    )
    stream = engine.stream_turn(request)
    seen = 0
    async for name, _ in stream:
        if name == "token":
            seen += 1
            if seen >= 3:
                break
    await stream.aclose()
    await asyncio.sleep(0.05)

    pending = [task for task in asyncio.all_tasks() if not task.done()]
    # 当前任务本身之外不应有残留（pytest-asyncio 自己的任务除外，故只断言「没有
    # 名字像 artifact_work 的任务」——协程名由 create_task 从 coroutine 名继承）
    leaked = [task for task in pending if "artifact_work" in str(task.get_coro())]
    assert not leaked, f"断连后仍有残留产物任务：{leaked}"


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _decision() -> Any:
    from app.services.router import route

    return route("4.1 Malayan Union 的影响", anchor=None, user_override="ppt")


def _context() -> Any:
    from app.services.mock_generator import build_context

    return build_context(
        query="4.1 Malayan Union 的影响",
        section_code="4.1",
        section_title="Malayan Union",
        texts=(CHUNK_TEXT,),
        pages=(76,),
        allowed_pages=(76, 77),
    )


def test_mock_spec_still_valid_after_refactor() -> None:
    """回归：并行重构不应改变 mock 生成的产物（契约校验仍通过）。"""
    from app.services.mock_generator import generate_spec

    spec = generate_spec("ppt", _context())
    assert parse_artifact_spec(spec).type == "ppt"


def test_settings_are_reset_between_cases() -> None:
    """防御：确认 `reset_settings_cache` 可用，避免本文件的 `Settings(...)` 污染全局单例。"""
    reset_settings_cache()
    assert Settings().LLM_MODEL_ARTIFACT

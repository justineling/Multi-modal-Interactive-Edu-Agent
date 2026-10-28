"""并行化（T02）：fork 时机、join 顺序、串行回退、错误归因、测量约定。

**全部离线**：假 `LLMClient` 注入，不打真实网络、不烧 token。

**本文件有一条 `@pytest.mark.slow`**（`test_parallel_is_actually_parallel`）：它的墙钟断言
在机器有并发负载时会假红，默认全量不跑。手动跑：`pytest -m slow tests/test_artifact_parallel.py`。
理由写在那个用例的 docstring 里，动 fork 时机 / join 顺序前必读。

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
import gc
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

    额外记录 `artifact_entered_at`（产物请求**进入**的时刻，即 `complete_json` 的第一行、
    还没 sleep）。这个时刻让「并行是否成立」可以**按先后关系**判定，而不必去比两个
    很接近的墙钟差值 —— 见 B1 的 docstring。
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
        # 产物请求**进入**的时刻（sleep 之前）。这是「分支 B 何时起步」的唯一可信信号：
        # 用它和「最后一个 token 何时 yield」比先后，与调度抖动无关。
        self.artifact_entered_at: float | None = None
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
        entered_at = time.perf_counter()
        self.artifact_entered_at = entered_at
        self.calls.append(("artifact", kwargs.get("model"), entered_at))
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


class TurnTimeline:
    """消费端观测到的一轮时间线（引擎 yield 边界处打点）。

    为什么在**消费端**打点而不是改引擎：本轮红线是「`agent_engine.py` 一行都不能动」。
    而 `stream_turn` 是 async generator，`yield` 之后到消费端收到事件之间**没有 await**
    （引擎内部下一个 await 是 `asyncio.sleep(0)`，在消费端取走事件之后才发生），
    所以消费端记下的时刻就是「引擎 yield 该事件的时刻」，误差在微秒级、且不含调度抖动
    带来的方向性偏差 —— 这正是墙钟差值做不到的。
    """

    def __init__(self) -> None:
        self.elapsed: float = 0.0
        self.last_token_at: float | None = None
        self.token_count: int = 0


async def _run_timed(
    engine: AgentEngine, workspace_id: str, **kwargs: Any
) -> tuple[list[tuple[str, Any]], TurnTimeline]:
    """同 `_run`，额外返回 `TurnTimeline`（总耗时 + 最后一个 token 的 yield 时刻）。

    **测量窗口内关掉自动 GC**，理由不是「图快」，而是「别测错东西」：
    本测试要测的是「两条支路有没有重叠」，而一次 gen2 回收能在窗口里塞进
    100–300ms 的停顿（全量跑时进程已存活几百个测试，堆大、回收更重）。
    这个停顿与并发实现无关，却会一次性吃掉整个容差 —— 实测过：同一份代码
    连跑 5 次全量，前三次 0.64/0.65/0.70s，第五次直接越过 0.82s 门槛。
    `gc.collect()` + `gc.disable()` 把这块噪声移出窗口，`finally` 保证恢复，
    断言失败时也不会把 GC 留在关闭状态污染后续测试。
    """
    request = TurnRequest(
        workspace_id=workspace_id,
        query="4.1 Malayan Union 的影响",
        artifact_type_override="ppt",
        **kwargs,
    )
    timeline = TurnTimeline()
    events: list[tuple[str, Any]] = []
    gc.collect()  # 先把待回收的对象清干净，窗口内才不会有自动回收
    gc.disable()
    try:
        started = time.perf_counter()
        async for event in engine.stream_turn(request):
            if event[0] == "token":
                timeline.token_count += 1
                timeline.last_token_at = time.perf_counter()
            events.append(event)
        timeline.elapsed = time.perf_counter() - started
    finally:
        gc.enable()
    return events, timeline


# --------------------------------------------------------------------------- #
# B1 / B2 / B3 / B4：并行真的并行 + 顺序契约
# --------------------------------------------------------------------------- #
# 并行场景的**统一时间档**（两条 B1 测试必须共用，否则「并行更快」与「串行更慢」
# 会各自漂移，两条断言合起来就不再是同一个命题）。
#
# 档位怎么定的（都是实测值，不是拍的）：
# - 一轮回合有一块**加性固定开销**：落库 + 版本行 + turn/message 写入。单跑时 0.14–0.20s，
#   全量跑时 0.24–0.35s（进程堆大、磁盘忙）。并行与串行都要付，所以它会把两侧的
#   实测值同时抬高。这决定了门槛不能写成「串行估算 × 比例」：比例式被这块开销同时
#   顶起两侧，余量会被压扁。反例（实测）：正文 8×0.05、产物 0.30 时并行实测 0.51s，
#   而 0.80×(0.40+0.30)=0.56s —— 看着有 20% 容差，实际只剩 50ms，等于把 flaky 换个数字。
# - 正文 12 × 0.05 = 0.60s；产物 0.55s，fork 点在首个 token 之后（≈0.05s），
#   所以两条支路**同时**在 0.60s 收尾 —— 并行总耗时对调度抖动最不敏感。
# - 理论差值 G = 串行 − 并行 = min(正文, 产物) = 0.55s。门槛取**理论中点**
#   「并行 + G/2 + 固定开销」= 1.12s，于是并行侧、串行侧各留 G/2 = 0.275s 余量。
#   全量实测（5 连跑）：并行 0.84–0.95s，串行 1.40–1.68s —— 两侧余量都 ≥0.16s。
_B1_DELTAS = 12
_B1_DELTA_DELAY = 0.05  # 正文 12 × 0.05 = 0.60s
_B1_ARTIFACT_DELAY = 0.55  # 产物 0.55s（fork 后 0.05s 起步 → 与正文同时收尾）
# 固定开销按**全量跑实测下界**取（单跑会低估：全量跑时进程堆大、磁盘忙，实测 0.24–0.35s）。
# 取下界而不是上界，是为了让**区分力不依赖这块开销**：最典型的坏实现是「fork 了却立刻
# await」（产物跑完再继续流正文），它的纯 sleep 下界是 正文 + 产物 = 1.15s，必须高于门槛。
# 门槛 1.12s < 1.15s，所以即使固定开销退化为 0，坏实现仍然会红。
_B1_FIXED_OVERHEAD = 0.24
_B1_SERIAL_FLOOR_RATIO = 0.80  # 反向验证：串行下界 = 串行估算 × 0.80


def _b1_llm() -> FakeLLM:
    return FakeLLM(
        deltas=_B1_DELTAS,
        delta_delay=_B1_DELTA_DELAY,
        artifact_delay=_B1_ARTIFACT_DELAY,
    )


def _serial_estimate() -> float:
    """串行估算：正文跑完再跑产物（`asyncio.sleep` 是下界，真实值只会更长）。"""
    return _B1_DELTAS * _B1_DELTA_DELAY + _B1_ARTIFACT_DELAY


def _parallel_estimate() -> float:
    """并行估算：两条支路同时跑，取较长的一条。

    产物分支在**首个 token 之后**才 fork，所以它的收尾时刻是
    `delta_delay + artifact_delay`；正文是 `deltas × delta_delay`。
    """
    reply = _B1_DELTAS * _B1_DELTA_DELAY
    artifact = _B1_DELTA_DELAY + _B1_ARTIFACT_DELAY
    return max(reply, artifact)


def _parallel_budget() -> float:
    """并行侧的墙钟门槛 = 理论并行 + 半个理论差值 + 固定开销。

    取「理论中点」而不是「串行 × 比例」，是为了让**两侧余量对称**：并行侧余量
    = G/2，串行侧余量也 = G/2。比例式做不到这点 —— 固定开销是加性的，
    比例式只会把门槛和实测值一起顶高，看着有容差、实际余量被吃掉。
    """
    parallel = _parallel_estimate()
    return parallel + (_serial_estimate() - parallel) / 2 + _B1_FIXED_OVERHEAD


@pytest.mark.slow
async def test_parallel_is_actually_parallel(db_env: Any, workspace_id: str) -> None:
    """B1：产物分支在正文**还没结束**时就已经起步（逻辑观测）+ 总耗时显著低于串行。

    断言 ①「时序关系」是主断言：并行的本质就是「产物请求先于正文结束发出」，
    这可以直接观测，不依赖任何计时精度。原来的写法只比两个很接近的墙钟差值
    （容差 18ms），全量跑时**一次 gen2 GC 回收就能吃掉整个容差** —— 那是测试的
    问题，不是实现的问题（实测：同一份代码全量连跑，前三次 0.64/0.65/0.70s，
    第五次直接越线）。所以这里把「顺序」和「耗时」拆成两条断言各管一件事。

    断言 ②「总耗时」保留，但把信号拉大：串行实测 1.40–1.68s，并行实测 0.84–0.95s，
    门槛 1.12s（理论中点），两侧余量都 ≥0.16s —— 是原来那 18ms 的十几倍。
    门槛刻意压在「坏实现的纯 sleep 下界 1.15s」之下，区分力不依赖落库开销。
    两断言分工：① 判「顺序对不对」，② 判「有没有真的省下时间」
    （顺序对但两段串行执行时，② 会红 —— 已用变异验证）。

    **为什么标 `slow`（默认不跑）**：断言 ② 是**墙钟**断言，它测的是「并行省下多少时间」，
    而任何同时占用 CPU 的东西（生成样本 PDF、另开一个 pytest、IDE 建索引、杀毒扫描）都会
    把实测值顶过 1.12s，于是红。实测全量连跑时就撞到过一次（1 failed / 404 passed），
    隔离单跑 5/5 全绿 —— 红的是测量环境，不是实现。

    处理方式是**把它移出默认全量**（`pytest.ini` 的 `addopts = -ra -m "not slow"`），
    而不是放宽预算：预算放到 1.3s 就越过了「坏实现纯 sleep 下界 1.15s」，这条断言会变成
    永远绿的摆设，区分力当场消失。两条断言都**没有删**，只是不在默认全量里跑。

    手动跑：`pytest -m slow tests/test_artifact_parallel.py`（命令行 `-m` 覆盖 ini 里的 `-m`）。
    改动了 fork 时机 / join 顺序 / `_parallel_budget` 相关代码时，必须跑这条。
    """
    llm = _b1_llm()
    engine = _build(_settings(), llm)

    events, timeline = await _run_timed(engine, workspace_id)

    # ---- 断言 ①：逻辑观测（不受调度抖动影响）----
    # 先校验场景本身成立：token 数必须等于档位里的 deltas，否则 last_token_at 记的
    # 不是「最后一个」，下面的先后比较就失去意义（比如降级到模板正文时）。
    assert timeline.token_count == _B1_DELTAS, (
        f"正文 token 数 {timeline.token_count} ≠ 档位 {_B1_DELTAS}，场景不成立"
    )
    assert timeline.last_token_at is not None, "正文一个 token 都没有，场景不成立"
    assert llm.artifact_entered_at is not None, "产物分支根本没被调用"
    assert llm.artifact_entered_at < timeline.last_token_at, (
        "产物请求在正文结束之后才发出，等于没并行："
        f"产物进入于 {llm.artifact_entered_at:.3f}s，"
        f"最后一个 token 产出 / yield 于 {timeline.last_token_at:.3f}s"
    )

    # ---- 断言 ②：墙钟（信噪比已拉大，见 docstring 与档位注释）----
    budget = _parallel_budget()
    assert timeline.elapsed < budget, (
        f"并行未生效：总耗时 {timeline.elapsed * 1000:.0f}ms ≥ 门槛 {budget * 1000:.0f}ms"
        f"（理论并行 {_parallel_estimate() * 1000:.0f}ms + 半差值 "
        f"{(_serial_estimate() - _parallel_estimate()) * 500:.0f}ms + 固定开销 "
        f"{_B1_FIXED_OVERHEAD * 1000:.0f}ms；串行估算 {_serial_estimate() * 1000:.0f}ms）"
    )
    assert any(name == "artifact" for name, _ in events)


async def test_serial_path_is_slower_than_parallel(
    db_env: Any, workspace_id: str
) -> None:
    """B1（反向验证）：关掉并行后同一场景必须**变慢**，且产物请求退到正文之后。

    没有这条，① 和 ② 有可能同时「假绿」：如果有人把并行改坏成「两段顺序执行」，
    断言 ① 仍可能恰好成立（产物任务确实 fork 了、也先于正文结束进入），
    而断言 ② 只保证「没比理论并行慢太多」，卡在门槛边缘的坏实现仍可能混过。
    这条把「串行确实更慢」变成显式断言：并行一旦退化成串行，它必红。
    """
    llm = _b1_llm()
    engine = _build(_settings(ARTIFACT_PARALLEL_ENABLED=False), llm)

    events, timeline = await _run_timed(engine, workspace_id)

    assert any(name == "artifact" for name, _ in events), "串行回退也要有产物，否则场景不成立"
    assert llm.artifact_entered_at is not None, "串行路径没调用产物"
    assert timeline.token_count == _B1_DELTAS, (
        f"正文 token 数 {timeline.token_count} ≠ 档位 {_B1_DELTAS}，场景不成立"
    )
    assert timeline.last_token_at is not None, "正文一个 token 都没有，场景不成立"

    # 逻辑方向必须相反：串行 = 正文全部 yield 完之后才发产物请求
    assert llm.artifact_entered_at > timeline.last_token_at, (
        "关闭并行后产物请求竟然先于正文结束发出，说明开关没有真正生效"
    )
    # 墙钟：串行估算的 80% 是下界（sleep 是下界，真实只会更慢）
    floor = _serial_estimate() * _B1_SERIAL_FLOOR_RATIO
    assert timeline.elapsed >= floor, (
        f"关闭并行后总耗时只有 {timeline.elapsed * 1000:.0f}ms < 下界 {floor * 1000:.0f}ms"
        f"（串行估算 {_serial_estimate() * 1000:.0f}ms）—— 串行路径没有真正串行"
    )


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

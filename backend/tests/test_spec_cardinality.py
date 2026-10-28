"""产物「数量」后校验（IMPL-NOTES E12 ⑦.3 的结构性缺口）。

**守的是什么**：`parse_artifact_spec` 只校验结构，而 `slides` / `steps` / `nodes` 的下界
都是 `min_length=1`。所以「只剩 1 页的残页课件」能通过校验、正常落库、前端正常渲染，
系统不报警、日志无异常 —— 用户看到缺页课件，系统认为它合法。**静默坏结果比报错更糟**，
本文件把这个洞钉死在生成路径上。

三层防线各测一遍，缺一层都会留下可绕过路径：

1. `test_mock_specs_*`：降级路径（mock）的三种产物数量必须达标。降级是**最后一道**，
   它若残页，前面所有校验都白做。
2. `test_*_out_of_range_*`：校验器本身的上/下界（只测下界会漏掉「模型话痨输出 30 页」）。
3. `test_engine_*`：**真实引擎链路** —— 第一次残页 → 自动重试 → 第二次达标；以及
   连续 3 次残页 → 降级 mock。这两条证明「数量失败真的复用了现有重试链路」，
   而不是新写了一条平行的重试逻辑（那样两条链路迟早漂移）。

为什么要断言**回灌给模型的反馈文本**：`feedback` 会把 `ValueError` 的 message 原样拼进
下一次请求。若 message 写成「invalid spec」，模型只能瞎猜；写成「slides 只有 3 个，
契约要求 5~8 个，请补足」它才知道改什么。这条断言把「错误信息面向模型」变成可回归的契约。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.db import repository
from app.db.session import get_session_factory
from app.schemas.artifacts import parse_artifact_spec
from app.services.agent_engine import (
    _ARTIFACT_CONTRACT,
    _SPEC_CARDINALITY,
    _cardinality_span,
    _validate_spec_cardinality,
    AgentEngine,
    TurnRequest,
)
from app.services.mock_generator import generate_spec

# 复用并行化测试里的固定检索结果与 mock 上下文：产物生成需要 `allowed_pages`
# 与 `facts`，自己再造一份只会让两个文件的夹具慢慢漂移。
from test_artifact_parallel import _context, _retrieval


# --------------------------------------------------------------------------- #
# 夹具：PPT spec 生成器 + 假 LLM + 真实 workspace
# --------------------------------------------------------------------------- #
def _ppt_spec(slide_count: int) -> dict[str, Any]:
    """构造 `slide_count` 页的**结构合法** PPT（数量由调用方决定）。

    刻意保持结构合法：这样校验失败只可能来自数量校验，不会与 `parse_artifact_spec`
    的结构错误混在一起（否则测试通过了也说明不了数量校验有效）。
    """
    return {
        "type": "ppt",
        "topic": "Malayan Union 的影响",
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
            for index in range(slide_count)
        ],
    }


class ScriptedLLM:
    """按脚本逐次返回 spec 的假 LLM，并记录每次请求的 user 文本。

    记录 `user` 是为了验证「校验失败的原因真的回灌给模型了」—— 这是重试能不能自我
    纠正的前提，光断言「重试了 1 次」证明不了这一点。
    """

    enabled = True

    def __init__(self, slide_counts: list[int]) -> None:
        self.slide_counts = slide_counts
        self.artifact_calls = 0
        self.users: list[str] = []

    async def stream_text(self, **kwargs: Any):
        for index in range(3):
            yield f"第 {index} 段。"

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.users.append(str(kwargs.get("user", "")))
        index = min(self.artifact_calls, len(self.slide_counts) - 1)
        self.artifact_calls += 1
        return _ppt_spec(self.slide_counts[index])


class _FakeRetriever:
    async def retrieve(self, *args: Any, **kwargs: Any) -> Any:
        return _retrieval()


@pytest.fixture()
async def workspace_id(db_env: Any) -> str:
    """真实 workspace：产物落库有外键约束，假数据过不去。"""
    async with get_session_factory()() as session:
        workspace = await repository.create_workspace(session, title="数量校验测试")
        await session.commit()
        return workspace.id


def _engine(llm: ScriptedLLM) -> AgentEngine:
    settings = Settings(
        LLM_API_KEY="test-key",  # 非空 = 非演示模式，才会走 LLM 分支
        LLM_MODEL_ARTIFACT="artifact-model",
        ARTIFACT_PARALLEL_ENABLED=False,  # 串行路径，事件时序确定，便于断言重试次数
    )
    return AgentEngine(
        settings=settings,
        session_factory=get_session_factory(),
        retriever=_FakeRetriever(),
        llm=llm,
    )


async def _run_turn(engine: AgentEngine, workspace_id: str) -> list[tuple[str, Any]]:
    request = TurnRequest(
        workspace_id=workspace_id,
        query="4.1 Malayan Union 的影响",
        artifact_type_override="ppt",
    )
    return [event async for event in engine.stream_turn(request)]


# --------------------------------------------------------------------------- #
# 1. 降级路径（mock）必须达标 —— 最后一道防线不能残页
# --------------------------------------------------------------------------- #
def test_mock_specs_satisfy_cardinality() -> None:
    """三种 mock 产物都要通过数量校验，且数量确实落在契约区间内。

    为什么这条放在第一位：校验器把所有不合格的输出挡掉之后，用户最终看到的**就是**
    mock 产物。如果 mock 自己残页，前面挡得再严也只是「从残页换成另一份残页」。
    """
    context = _context()
    observed: dict[str, int] = {}

    for artifact_type, (field, low, high) in _SPEC_CARDINALITY.items():
        spec = generate_spec(artifact_type, context)
        parse_artifact_spec(spec)  # 结构先过（mock 的既有保证）
        _validate_spec_cardinality(spec, artifact_type)  # 数量也必须过，越界会抛 ValueError
        count = len(spec[field])
        assert low <= count <= high, f"mock 的 {artifact_type}.{field} 是 {count}，不在 {low}~{high}"
        observed[artifact_type] = count

    # 顺带锁住实际数量：mock 改动导致数量变化时，这里会提醒复查契约是否仍然合理
    assert observed["ppt"] >= 5
    assert observed["anim_2d"] >= 2
    assert observed["scene_3d"] >= 3


# --------------------------------------------------------------------------- #
# 2 / 3. 校验器本身：下界与上界都要拦
# --------------------------------------------------------------------------- #
def test_ppt_below_lower_bound_raises_with_actionable_message() -> None:
    """4 页 → ValueError，且信息必须让模型看懂「现在几个、要几个」。"""
    spec = _ppt_spec(4)
    parse_artifact_spec(spec)  # 结构合法：证明拦截来自数量而不是结构

    with pytest.raises(ValueError) as excinfo:
        _validate_spec_cardinality(spec, "ppt")

    message = str(excinfo.value)
    assert "4" in message, f"错误信息没说清实际数量：{message}"
    assert "5~8" in message, f"错误信息没说清契约区间：{message}"
    assert "slides" in message, f"错误信息没指明是哪个字段：{message}"


def test_ppt_above_upper_bound_raises() -> None:
    """9 页 → ValueError。只测下界会漏掉「模型话痨把课件写成 30 页」这一类。"""
    spec = _ppt_spec(9)
    parse_artifact_spec(spec)

    with pytest.raises(ValueError) as excinfo:
        _validate_spec_cardinality(spec, "ppt")

    assert "9" in str(excinfo.value)
    assert "5~8" in str(excinfo.value)


@pytest.mark.parametrize("slide_count", [5, 8])
def test_boundaries_are_inclusive(slide_count: int) -> None:
    """边界值 5 与 8 必须放行（区间是闭区间，off-by-one 会误杀合法产物）。"""
    _validate_spec_cardinality(_ppt_spec(slide_count), "ppt")


def test_missing_field_counts_as_zero() -> None:
    """字段缺失/类型不对按 0 计，抛 ValueError 而不是 TypeError（不让异常裸奔）。"""
    with pytest.raises(ValueError):
        _validate_spec_cardinality({"type": "ppt", "topic": "x"}, "ppt")
    with pytest.raises(ValueError):
        _validate_spec_cardinality({"type": "ppt", "slides": "not-a-list"}, "ppt")


def test_prompt_numbers_are_derived_from_cardinality_table() -> None:
    """提示词里的数量区间必须与校验表一致（防止「提示词要 5~8、校验器放行 1 页」）。

    两者不一致时不会报错、只会让校验器与提示词各说各话 —— 所以用测试锁住派生关系。
    """
    for artifact_type in _SPEC_CARDINALITY:
        span = _cardinality_span(artifact_type)
        assert span in _ARTIFACT_CONTRACT[artifact_type], (
            f"{artifact_type} 的提示词里没有出现 {span}：数字可能被手写改过、没走派生"
        )


# --------------------------------------------------------------------------- #
# 误伤防护：校验对象必须是 spec 自己声明的形态，不是请求的形态
# --------------------------------------------------------------------------- #
def test_spec_type_wins_over_requested_type() -> None:
    """请求 ppt、模型却回了一份**合法的** anim_2d（2 帧）→ 不得误判、不得重试。

    这是真实误伤（QA 的 P1-03 用例撞出来的）：`parse_artifact_spec` 按 `spec["type"]`
    分派校验器，落库也按 `spec["type"]` 写 `artifact_type` 列。若数量校验按
    `decision.artifact_type`（请求的 ppt）去数，就会去数根本不存在的 `slides`、
    把这份合法产物判成「0 个」，白重试 3 次后降级 mock —— 比原缺陷更糟。
    """
    anim_spec = {
        "type": "anim_2d",
        "title": "联合邦计划的推进过程",
        "steps": [
            {
                "step_index": index,
                "narration": "1946 年英国公布联合邦计划。",
                "source_ref": "4.3.1, p.76",
                "entities": [
                    {
                        "id": f"entity-{entity_index}",
                        "shape": "zone",
                        "label": f"元素 {entity_index}",
                        "x": 300.0,
                        "y": 300.0,
                        "color": "#2563EB",
                        "details": "该元素的详细说明。",
                        "source_page": 76,
                    }
                    for entity_index in range(2)
                ],
            }
            for index in range(2)
        ],
    }
    parse_artifact_spec(anim_spec)

    # 请求形态是 ppt，但 spec 是 anim_2d：按 spec 自己的形态数（2 帧，合法），不抛
    _validate_spec_cardinality(anim_spec, "ppt")


# --------------------------------------------------------------------------- #
# 4 / 5. 真实引擎链路：数量失败必须复用现有重试链路
# --------------------------------------------------------------------------- #
async def test_engine_retries_once_when_first_spec_has_too_few_slides(
    workspace_id: str,
) -> None:
    """第一次 3 页 → 自动重试 → 第二次 6 页 → 正常产出，且反馈里带可执行原因。"""
    llm = ScriptedLLM([3, 6])
    engine = _engine(llm)

    events = await _run_turn(engine, workspace_id)
    names = [name for name, _ in events]

    retries = [
        payload
        for name, payload in events
        if name == "error" and payload.stage == "validate" and payload.retryable
    ]
    assert len(retries) == 1, f"应恰好重试 1 次，实际 {len(retries)} 次：{names}"
    assert retries[0].attempt == 1

    artifact = next(payload for name, payload in events if name == "artifact")
    assert len(artifact.spec.slides) == 6, "第二次输出有 6 页，应当原样采用"
    assert names[-1] == "turn_end", "重试成功后仍必须正常收尾"
    assert llm.artifact_calls == 2

    # 关键：第一次失败的原因真的回灌给模型了（否则重试只能靠运气）
    feedback = llm.users[1]
    assert "只有 3 个" in feedback and "5~8" in feedback, (
        f"回灌给模型的反馈里没有可执行原因，模型无法自我纠正：{feedback[-200:]}"
    )


async def test_engine_degrades_to_mock_after_three_bad_specs(workspace_id: str) -> None:
    """连续 3 次 3 页 → 降级 mock（6 页），并给出 retryable=False 的 error。"""
    llm = ScriptedLLM([3])  # 每次都是 3 页
    engine = _engine(llm)

    events = await _run_turn(engine, workspace_id)
    names = [name for name, _ in events]

    fatal = [
        payload
        for name, payload in events
        if name == "error" and payload.retryable is False
    ]
    assert len(fatal) == 1, f"降级必须给出 1 条不可重试的 error，实际 {len(fatal)}：{names}"
    assert "降级" in fatal[0].message
    assert fatal[0].attempt == 3
    assert llm.artifact_calls == 3, "三次都失败后才允许降级，不能提前放弃"

    artifact = next(payload for name, payload in events if name == "artifact")
    assert len(artifact.spec.slides) == len(generate_spec("ppt", _context())["slides"]), (
        "降级后的产物必须来自 mock（页数与 mock 一致），而不是把 3 页的残页原样放出去"
    )
    assert names[-1] == "turn_end"

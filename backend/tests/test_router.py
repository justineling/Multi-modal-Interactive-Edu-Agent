"""Artifact 形态路由测试（P0-06）。

§7.4 给了 10 条预置问句与期望形态，验收线是准确率 ≥80%（即 ≥8 条）。
显式覆盖必须 100% 遵循，且 `reason` 要是能直接展示给用户的人话。
"""

from __future__ import annotations

import pytest

from app.services.router import ANIM_2D, NONE, PPT, SCENE_3D, detect_override, route

# §7.4 的 10 条预置问句（逐字照抄，便于回溯验收）
PRESET_QUERIES: tuple[tuple[str, str], ...] = (
    ("4.1 讲了什么，帮我梳理要点", PPT),
    ("马来亚联邦与马来亚联合邦有什么区别", PPT),
    ("按时间顺序列出 1945–1957 的大事年表", PPT),
    ("Malayan Union 成立的原因和影响", PPT),
    ("出 5 道关于 4.2 的随堂测验", PPT),
    ("马来亚疆域从 1945 到 1948 是怎么演变的", ANIM_2D),
    ("画一下从印度到马六甲的贸易航线", ANIM_2D),
    ("桥梁受力时各方向的力如何分布", ANIM_2D),
    ("这个建筑的内部结构和剖面是怎样的", SCENE_3D),
    ("用 3D 展示这个文物的形制", SCENE_3D),
)


@pytest.mark.parametrize(("query", "expected"), PRESET_QUERIES)
def test_preset_queries_route_correctly(query: str, expected: str) -> None:
    decision = route(query)
    assert decision.artifact_type == expected, f"{query!r} → {decision.artifact_type}（期望 {expected}）"


def test_preset_accuracy_at_least_80_percent() -> None:
    correct = sum(1 for query, expected in PRESET_QUERIES if route(query).artifact_type == expected)
    assert correct >= 8, f"准确率 {correct}/10 低于 80%"


@pytest.mark.parametrize(
    ("query", "override_type"),
    [
        ("用 3D 展示这个文物的形制", SCENE_3D),
        ("把它做成 PPT", PPT),
        ("用动画演示一下这个过程", ANIM_2D),
        ("给我一份课件", PPT),
        ("3d 场景，看看内部", SCENE_3D),
    ],
)
def test_explicit_override_detected(query: str, override_type: str) -> None:
    assert detect_override(query) == override_type
    decision = route(query)
    assert decision.artifact_type == override_type
    assert decision.user_override is True


def test_explicit_request_param_wins_over_keywords() -> None:
    """用户在 UI 里显式选了形态：即使问句关键词指向别的形态，也必须遵循用户。"""
    decision = route("帮我梳理时间轴和要点", user_override=SCENE_3D)
    assert decision.artifact_type == SCENE_3D
    assert decision.user_override is True
    assert "用户显式指定" in decision.reason


def test_override_beats_keyword_score() -> None:
    decision = route("这个建筑的内部结构用 3D 展示")
    assert decision.artifact_type == SCENE_3D
    assert decision.user_override is True


@pytest.mark.parametrize(
    "query",
    [
        "4.1 讲了什么，帮我梳理要点",
        "马来亚疆域从 1945 到 1948 是怎么演变的",
        "这个建筑的内部结构和剖面是怎样的",
        "帮我看看这个",
    ],
)
def test_reason_is_human_readable(query: str) -> None:
    decision = route(query)
    assert decision.reason.strip()
    assert len(decision.reason) >= 8, "理由太短，前端展示会像占位符"
    assert "→" in decision.reason, "理由里要说明「为什么选这个形态」"


def test_fallback_for_vague_query_defaults_to_ppt() -> None:
    """全 0 分且没有过程性措辞 → 默认 ppt（PRD Q2 的保守策略）。"""
    decision = route("帮我看看这个")
    assert decision.artifact_type == PPT
    assert decision.user_override is False


def test_fallback_process_wording_goes_to_anim() -> None:
    decision = route("这件事是怎么发生的")
    assert decision.artifact_type == ANIM_2D


def test_route_never_returns_none_type() -> None:
    """路由只决定「做哪种形态」，是否真的生成由 Grader 决定（NO_MATCH 时 action=none）。"""
    for query, _ in PRESET_QUERIES:
        assert route(query).artifact_type in {PPT, ANIM_2D, SCENE_3D}
        assert route(query).artifact_type != NONE

"""Artifact 类型路由（§7.4 / P0-06）。

为什么关键词打分而不是让 LLM 判断：P0-06① 要求 10 条预置问句 ≥80% 准确，关键词方案
**可离线、可回归、零成本、毫秒级**；而 LLM 判断每次要 0.5~2s 且不稳定。LLM 只在
关键词全 0 时作为兜底（这里用结构启发式代替，避免 demo 模式依赖外部服务）。

用户显式指定（"用 3D" / "做成 PPT" / "动画演示"）优先级最高，100% 遵循（P0-06②）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.artifacts import ArtifactType

PPT = "ppt"
ANIM_2D = "anim_2d"
SCENE_3D = "scene_3d"
NONE = "none"

# §7.4 关键词词典。命中即累加，权重 = clamp(len(kw)/2, 1.0, 2.0)
_KEYWORDS: dict[str, tuple[str, ...]] = {
    PPT: (
        "梳理", "总结", "对比", "区别", "时间轴", "年表", "因果", "原因", "影响", "要点",
        "列表", "提纲", "测验", "习题", "背诵", "考试", "几点", "分类", "特征", "背景", "意义",
    ),
    ANIM_2D: (
        "演变", "疆域", "版图", "扩张", "收缩", "路线", "航线", "迁徙", "流动", "过程",
        "步骤", "受力", "方向", "轨迹", "推移", "进程", "变化", "传播", "推进", "转移",
    ),
    SCENE_3D: (
        "结构", "构造", "剖面", "内部", "组成", "部件", "地形", "三维", "立体", "空间",
        "模型", "形制", "布局", "方位", "建筑", "器物", "解剖", "层次",
    ),
}

# 显式覆盖：用户说什么就是什么（P0-06②）
_OVERRIDE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (SCENE_3D, re.compile(r"(?i)(3\s*d|三维|立体模型|场景模型)")),
    (ANIM_2D, re.compile(r"(?i)(2\s*d|动画|动态演示|推演动画)")),
    (PPT, re.compile(r"(?i)(ppt|幻灯片|slide|课件|思维导图|导图|讲义)")),
)

_LABELS: dict[str, str] = {
    PPT: "PPT 课件",
    ANIM_2D: "2D 推演动画",
    SCENE_3D: "3D 空间场景",
    NONE: "纯文字回答",
}

# 平分时的优先级：ppt 最保守（PRD Q2：默认更安全的应试形态）
_TIE_ORDER: tuple[str, ...] = (PPT, ANIM_2D, SCENE_3D)


@dataclass(frozen=True)
class RouteDecision:
    artifact_type: str
    reason: str
    user_override: bool

    @property
    def is_none(self) -> bool:
        return self.artifact_type == NONE


def detect_override(query: str) -> str | None:
    """从问句里识别显式形态要求。先匹配 3D 再 2D 再 PPT —— "用 3D 做成课件" 应判 3D。"""
    for artifact_type, pattern in _OVERRIDE_PATTERNS:
        if pattern.search(query):
            return artifact_type
    return None


def _keyword_weight(keyword: str) -> float:
    return min(2.0, max(1.0, len(keyword) / 2.0))


def _score(query: str, keywords: tuple[str, ...]) -> tuple[float, list[str]]:
    total = 0.0
    hits: list[str] = []
    for keyword in keywords:
        if keyword in query:
            total += _keyword_weight(keyword)
            hits.append(keyword)
    return total, hits


def route(
    query: str,
    *,
    anchor: object | None = None,
    user_override: ArtifactType | str | None = None,
) -> RouteDecision:
    """返回形态决策。`anchor` 存在说明是「针对某节点的追问」，但形态仍由问句决定。"""
    explicit = user_override or detect_override(query)
    if explicit:
        artifact_type = str(explicit)
        source = "用户显式指定" if user_override else "问句中显式要求"
        return RouteDecision(
            artifact_type=artifact_type,
            reason=f"{source}「{_LABELS.get(artifact_type, artifact_type)}」，已优先遵循",
            user_override=True,
        )

    scores: dict[str, float] = {}
    hits: dict[str, list[str]] = {}
    for artifact_type, keywords in _KEYWORDS.items():
        scores[artifact_type], hits[artifact_type] = _score(query, keywords)

    best_type = max(
        _TIE_ORDER,
        key=lambda candidate: (scores[candidate], -_TIE_ORDER.index(candidate)),
    )
    best_score = scores[best_type]

    if best_score <= 0.0:
        # 全 0 分：结构启发式兜底（§7.4 第 4 步）
        if any(word in query for word in ("怎么", "如何", "为什么", "过程")):
            return RouteDecision(
                artifact_type=ANIM_2D,
                reason="问句在问「怎么发生/为什么」，属于动态过程型知识点 → 2D 推演动画",
                user_override=False,
            )
        return RouteDecision(
            artifact_type=PPT,
            reason="未命中任何形态特征，按默认保守策略 → PPT 课件（便于逐条对照课本、考试不答偏）",
            user_override=False,
        )

    matched = "」「".join(hits[best_type][:3])
    reason = (
        f"问句命中「{matched}」，属于{_REASON_TEXT[best_type]} → {_LABELS[best_type]}"
    )
    return RouteDecision(artifact_type=best_type, reason=reason, user_override=False)


_REASON_TEXT: dict[str, str] = {
    PPT: "离散有序、需要逐条阅读的信息",
    ANIM_2D: "时空连续演化、有方向与运动的知识点",
    SCENE_3D: "需要深度、体积与方位关系才能理解的空间结构",
}


__all__ = [
    "ANIM_2D",
    "NONE",
    "PPT",
    "SCENE_3D",
    "RouteDecision",
    "detect_override",
    "route",
]

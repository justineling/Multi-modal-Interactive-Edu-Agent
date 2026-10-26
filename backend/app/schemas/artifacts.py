"""三种 JSON AST 契约（SPEC §5 + 架构 §5.1 扩展字段）。

这是前后端唯一的「灵魂桥梁」：LLM 只填数据，前端按 type 策略分发到确定性渲染器。
因此本文件的三条铁律：
1. **不含任何 HTML/JS**（P0-07②）：所有字符串字段递归扫描，命中注入痕迹即抛错触发重试。
2. **不含 version 字段**：版本属于 artifact 行，AST 保持纯内容，指纹才稳定。
3. **字段名与 §5.3 表逐字一致**：线上传输一律 snake_case，前端 TS 同名同形，不做映射层。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

# §9.5 调色板：mock_generator 与 LLM 输出都必须取自这里，前端 lib/theme.ts 同一份
PALETTE: tuple[str, ...] = (
    "#2563EB",  # 主蓝
    "#0EA5E9",  # 次蓝
    "#F59E0B",  # 高亮橙
    "#EF4444",  # 警示红
    "#10B981",  # 成功绿
    "#6366F1",  # 紫
    "#64748B",  # 中性灰
    "#E2E8F0",  # 浅底
)

# 小写化后比较，避免 LLM 输出 "#2563eb" 这种大小写差异被判为非法
_PALETTE_UPPER: frozenset[str] = frozenset(color.upper() for color in PALETTE)

# §5.1 + PRD P0-07② 的注入痕迹清单。任何一条命中都意味着模型试图绕过 DSL 直接给渲染代码。
_FORBIDDEN_MARKERS: tuple[str, ...] = (
    "<script",
    "<html",
    "<div",
    "<style",
    "onerror=",
    "javascript:",
    "function(",
    "=>",
    "import ",
    "three.",
)


def iter_strings(value: Any) -> Iterator[str]:
    """递归产出任意嵌套结构里的所有字符串（含 dict 的键）。

    为什么连键也要扫：LLM 有可能把注入片段塞进自定义字段名（extra=forbid 会拦住，
    但这里多做一层不花成本）。
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_strings(key)
            yield from iter_strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from iter_strings(item)


class AstModel(BaseModel):
    """所有 AST 节点的公共基类。

    extra="forbid"：未声明字段一律拒绝。LLM 很爱顺手塞 "notes"/"style" 之类的自创字段，
    放任它们会悄悄流到前端并让指纹不稳定。
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _reject_markup_injection(self) -> "AstModel":
        """P0-07②：spec 不得包含 HTML/JS 字符串。

        校验失败会让 agent_engine 带错误信息重试（§10 待明确事项 1），
        这是「LLM 裸写渲染代码」红线的最后一道闸门。
        """
        for text in iter_strings(self.model_dump(mode="python")):
            lowered = text.lower()
            for marker in _FORBIDDEN_MARKERS:
                if marker in lowered:
                    raise ValueError(f"字段值中不允许出现 HTML/JS 片段（命中 {marker!r}）")
        return self


def _check_palette(value: str) -> str:
    """颜色必须取自 §9.5 调色板。统一转大写，保证同一语义色在库里只有一种写法。"""
    normalized = value.strip().upper()
    if normalized not in _PALETTE_UPPER:
        raise ValueError(f"颜色 {value!r} 不在调色板内，允许值：{list(PALETTE)}")
    return normalized


# --------------------------------------------------------------------------- #
# 1. PPT
# --------------------------------------------------------------------------- #
class TimelineItem(AstModel):
    """时间轴条目（扩展）。timeline 布局需要结构化年份，靠 bullets 拼字符串无法渲染。"""

    year: str
    label: str
    detail: str = ""
    source_page: int | None = None


class QuizQuestion(AstModel):
    """随堂测验题目（扩展）。quiz 布局需要选项与答案索引，前端才能判对错。"""

    question: str
    options: list[str] = Field(min_length=2)
    answer_index: int = Field(ge=0)
    explanation: str = ""

    @model_validator(mode="after")
    def _answer_in_range(self) -> "QuizQuestion":
        if self.answer_index >= len(self.options):
            raise ValueError(
                f"answer_index={self.answer_index} 越界，options 只有 {len(self.options)} 项"
            )
        return self


SlideLayout = Literal[
    "title_hero",
    "bullet_points",
    "timeline",
    "comparison",
    "cause_effect",
    "quiz",
]


class SlideNode(AstModel):
    """单页幻灯片。node_id 是锚点，学生点这一页追问时前端回传 /slides/{i}。"""

    node_id: str = Field(min_length=1)
    layout: SlideLayout
    title: str
    subtitle: str | None = None
    bullets: list[str] = Field(default_factory=list)
    comparison_left: list[str] | None = None
    comparison_right: list[str] | None = None
    timeline_items: list[TimelineItem] | None = None
    quiz_questions: list[QuizQuestion] | None = None
    speaker_notes: str
    source_ref: str


class SlideDeckSpec(AstModel):
    type: Literal["ppt"] = "ppt"
    topic: str
    slides: list[SlideNode] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# 2. 2D 关键帧动画
# --------------------------------------------------------------------------- #
EntityShape = Literal["circle", "rect", "arrow", "label_box", "zone"]


class Entity2D(AstModel):
    """2D 元素。x/y 是 1000×600 逻辑画布的中心点坐标，**不是像素**（§9.5）。"""

    id: str = Field(min_length=1)
    shape: EntityShape
    label: str
    x: float
    y: float
    color: str
    details: str
    source_page: int | None = None

    @field_validator("color")
    @classmethod
    def _validate_color(cls, value: str) -> str:
        return _check_palette(value)


class KeyframeStep(AstModel):
    step_index: int = Field(ge=0)
    narration: str
    source_ref: str = ""
    entities: list[Entity2D] = Field(default_factory=list)


class Keyframe2DSpec(AstModel):
    type: Literal["anim_2d"] = "anim_2d"
    title: str
    steps: list[KeyframeStep] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# 3. 3D 空间场景
# --------------------------------------------------------------------------- #
Primitive3D = Literal["box", "sphere", "cylinder", "cone", "terrain_plane", "arrow_link"]


class Node3D(AstModel):
    """3D 节点。position/scale/rotation 是 3 元组 —— 前端 TS 必须写元组类型，不能写 number[]。"""

    id: str = Field(min_length=1)
    primitive: Primitive3D
    label: str
    position: tuple[float, float, float]
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    color: str
    annotation_markdown: str
    source_page: int | None = None
    # 部件从属关系：patch 界定子树时用来判断「哪些节点属于被锚定的部件」
    parent_id: str | None = None

    @field_validator("color")
    @classmethod
    def _validate_color(cls, value: str) -> str:
        return _check_palette(value)


class CameraPreset(AstModel):
    name: str = Field(min_length=1)
    position: tuple[float, float, float]
    target: tuple[float, float, float]
    explanation: str


class SceneGraph3DSpec(AstModel):
    type: Literal["scene_3d"] = "scene_3d"
    title: str
    nodes: list[Node3D] = Field(min_length=1)
    camera_tour: list[CameraPreset] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# 4. 判别联合与统一响应
# --------------------------------------------------------------------------- #
ArtifactType = Literal["ppt", "anim_2d", "scene_3d"]
KnowledgeScope = Literal["syllabus_strict", "hybrid_extended", "none"]
ModalityDecision = Literal["none", "create_artifact", "patch_artifact"]

ArtifactSpec = Annotated[
    Union[SlideDeckSpec, Keyframe2DSpec, SceneGraph3DSpec],
    Field(discriminator="type"),
]

SPEC_MODEL_BY_TYPE: dict[str, type[AstModel]] = {
    "ppt": SlideDeckSpec,
    "anim_2d": Keyframe2DSpec,
    "scene_3d": SceneGraph3DSpec,
}

ARTIFACT_SPEC_ADAPTER: TypeAdapter[Any] = TypeAdapter(ArtifactSpec)


def parse_artifact_spec(payload: Any) -> SlideDeckSpec | Keyframe2DSpec | SceneGraph3DSpec:
    """按 type 判别字段解析任意来源的 dict。禁止用 any 承接 spec（§5.3）。"""
    return ARTIFACT_SPEC_ADAPTER.validate_python(payload)


def spec_model_for(spec_type: str) -> type[AstModel]:
    """取某种 artifact 对应的 AST 模型类。未知类型是程序 bug，直接 KeyError 更早暴露。"""
    try:
        return SPEC_MODEL_BY_TYPE[spec_type]
    except KeyError as exc:
        raise ValueError(f"未知的 artifact 类型：{spec_type!r}") from exc


class Citation(AstModel):
    """引用条目。page_start/page_end 支撑 P0-04② 的「页码可回溯区间」。"""

    label: str
    section_code: str
    page_start: int
    page_end: int


class AgentTurnResponse(AstModel):
    """非流式接口的统一返回体（SSE 走 §5.4 的事件，不用这个）。"""

    reply_markdown: str
    knowledge_scope: KnowledgeScope
    citations: list[Citation] = Field(default_factory=list)
    modality_decision: ModalityDecision
    artifact_payload: ArtifactSpec | None = None


__all__ = [
    "ARTIFACT_SPEC_ADAPTER",
    "AgentTurnResponse",
    "ArtifactSpec",
    "ArtifactType",
    "AstModel",
    "CameraPreset",
    "Citation",
    "Entity2D",
    "EntityShape",
    "Keyframe2DSpec",
    "KeyframeStep",
    "KnowledgeScope",
    "ModalityDecision",
    "Node3D",
    "PALETTE",
    "Primitive3D",
    "QuizQuestion",
    "SPEC_MODEL_BY_TYPE",
    "SceneGraph3DSpec",
    "SlideDeckSpec",
    "SlideLayout",
    "SlideNode",
    "TimelineItem",
    "iter_strings",
    "parse_artifact_spec",
    "spec_model_for",
]

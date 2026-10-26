"""确定性 Mock 生成器（P0-12）。

三条硬约束：
1. **纯函数**：禁止 `random` / `time.*` / `uuid4()` / `datetime.now()` / 内置 `hash()`。
   同一 `(query, section_code, artifact_type)` 必然产出逐字节相同的 AST，
   `test_mock_determinism` 用指纹比对来守住这条。
2. **内容必须与章节语义相关**：从检索到的切片里抽句子填进标题/要点/旁白。
   纯占位符（"要点 1""要点 2"）虽然也能通过契约校验，但演示时一眼假。
3. **自带净化**：用户 PDF 里可能真有 `<`、`=>` 这类字符，AST 校验器会拒绝；
   mock 在抽取事实时先净化，避免「用户上传的文档把演示模式搞崩」。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.schemas.artifacts import PALETTE
from app.schemas.patch import PatchOp

# 与 schemas/artifacts.py 的注入清单保持一致（那里是硬校验，这里是提前净化）
_FORBIDDEN_MARKERS: tuple[str, ...] = (
    "script", "html", "div", "style", "onerror=", "javascript:", "function(", "=>", "import ", "three.",
)
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；\n]|(?<=[.!?])\s+")
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_MAX_FACT_LEN = 110
_MAX_FACTS = 12


@dataclass(frozen=True)
class MockContext:
    """生成所需的全部输入。全部来自检索结果，因此内容与章节强相关。"""

    query: str
    section_code: str
    section_title: str
    pages: tuple[int, ...]
    facts: tuple[str, ...]

    @property
    def topic(self) -> str:
        return self.section_title or _clip(self.query, 24) or "知识点讲解"

    @property
    def primary_page(self) -> int:
        return self.pages[0] if self.pages else 1

    @property
    def source_ref(self) -> str:
        if not self.section_code:
            return "Extended Knowledge"
        return f"{self.section_code}, p.{self.primary_page}"


def _clip(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


def _is_safe(text: str) -> bool:
    lowered = text.lower()
    if "<" in text or ">" in text:
        return False
    return not any(marker in lowered for marker in _FORBIDDEN_MARKERS)


def _sanitize(text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned or not _is_safe(cleaned):
        return ""
    return cleaned


def extract_facts(texts: Sequence[str], *, limit: int = _MAX_FACTS) -> tuple[str, ...]:
    """从切片正文抽句子作为「事实」。保持原文顺序、去重、限长，完全确定性。"""
    facts: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            cleaned = _sanitize(sentence)
            if len(cleaned) < 12:
                continue
            if cleaned in seen:
                continue
            seen.add(cleaned)
            facts.append(_clip(cleaned, _MAX_FACT_LEN))
            if len(facts) >= limit:
                return tuple(facts)
    return tuple(facts)


def build_context(
    *,
    query: str,
    section_code: str,
    section_title: str,
    texts: Sequence[str],
    pages: Sequence[int],
) -> MockContext:
    facts = extract_facts(texts)
    if not facts:
        # 检索为空也要能出东西（例如 NO_MATCH 时用户仍要求生成），但绝不编造页码
        facts = (_clip(_sanitize(query) or "本节要点", _MAX_FACT_LEN),)
    ordered_pages = tuple(sorted({int(page) for page in pages if page})) or (1,)
    return MockContext(
        query=_sanitize(query) or "本节要点",
        section_code=section_code,
        section_title=_sanitize(section_title),
        pages=ordered_pages,
        facts=facts,
    )


def _slide(
    *,
    node_id: str,
    layout: str,
    title: str,
    speaker_notes: str,
    source_ref: str,
    subtitle: str | None = None,
    bullets: list[str] | None = None,
    comparison_left: list[str] | None = None,
    comparison_right: list[str] | None = None,
    timeline_items: list[dict[str, Any]] | None = None,
    quiz_questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """按 §5.1 声明顺序构造 slide dict —— 键序与 Pydantic 字段顺序一致，指纹才稳定。"""
    return {
        "node_id": node_id,
        "layout": layout,
        "title": title,
        "subtitle": subtitle,
        "bullets": bullets or [],
        "comparison_left": comparison_left,
        "comparison_right": comparison_right,
        "timeline_items": timeline_items,
        "quiz_questions": quiz_questions,
        "speaker_notes": speaker_notes,
        "source_ref": source_ref,
    }


def _timeline_items(facts: tuple[str, ...], pages: tuple[int, ...]) -> list[dict[str, Any]]:
    """优先用正文里的真实年份；抽不到年份时退化为「阶段一/二/三」，保证布局仍可渲染。"""
    items: list[dict[str, Any]] = []
    seen_years: set[str] = set()
    for fact in facts:
        for year in _YEAR_RE.findall(fact):
            if year in seen_years:
                continue
            seen_years.add(year)
            items.append(
                {
                    "year": year,
                    "label": _clip(fact, 32),
                    "detail": _clip(fact, 90),
                    "source_page": pages[min(len(items), len(pages) - 1)],
                }
            )
            break
        if len(items) >= 4:
            break
    if len(items) >= 2:
        return items

    labels = ("阶段一", "阶段二", "阶段三")
    return [
        {
            "year": labels[index],
            "label": _clip(fact, 32),
            "detail": _clip(fact, 90),
            "source_page": pages[min(index, len(pages) - 1)],
        }
        for index, fact in enumerate(facts[:3])
    ]


def generate_ppt(context: MockContext) -> dict[str, Any]:
    """6 页 PPT，覆盖全部 6 种布局（P0-07① 要求每种布局至少渲染成功一次）。"""
    facts = context.facts
    page = context.primary_page
    source = context.source_ref
    slides: list[dict[str, Any]] = []

    slides.append(
        _slide(
            node_id="slide-1",
            layout="title_hero",
            title=context.topic,
            subtitle=f"{context.section_code} · p.{page}" if context.section_code else "拓展知识",
            speaker_notes=f"这一节我们讲「{context.topic}」。先交代背景，再拆成几个要点逐条过。",
            source_ref=source,
        )
    )
    slides.append(
        _slide(
            node_id="slide-2",
            layout="bullet_points",
            title="核心要点",
            bullets=[_clip(fact, 60) for fact in facts[:4]],
            speaker_notes="这几条是本节的骨架，考试作答时按顺序展开即可。",
            source_ref=source,
        )
    )
    slides.append(
        _slide(
            node_id="slide-3",
            layout="timeline",
            title="时间脉络",
            timeline_items=_timeline_items(facts, context.pages),
            speaker_notes="按时间轴走一遍，注意每个节点的前后因果，而不是孤立记年份。",
            source_ref=source,
        )
    )
    left = [_clip(fact, 48) for fact in facts[:2]]
    right = [_clip(fact, 48) for fact in facts[2:4]] or ["课本未展开的另一面"]
    slides.append(
        _slide(
            node_id="slide-4",
            layout="comparison",
            title="前后对照",
            comparison_left=left,
            comparison_right=right,
            speaker_notes="左右两栏对照着看，差异点就是考点。",
            source_ref=source,
        )
    )
    slides.append(
        _slide(
            node_id="slide-5",
            layout="cause_effect",
            title="原因与影响",
            bullets=[
                f"原因：{_clip(facts[0], 52)}",
                f"影响：{_clip(facts[min(1, len(facts) - 1)], 52)}",
            ],
            speaker_notes="先讲为什么会发生，再讲发生后改变了什么。",
            source_ref=source,
        )
    )
    slides.append(
        _slide(
            node_id="slide-6",
            layout="quiz",
            title="随堂测验",
            quiz_questions=_quiz(facts, context),
            speaker_notes="做完再对答案，错的题回到对应小节重看。",
            source_ref=source,
        )
    )
    return {"type": "ppt", "topic": context.topic, "slides": slides}


def _quiz(facts: tuple[str, ...], context: MockContext) -> list[dict[str, Any]]:
    """题目答案固定为第 1 项：确定性优先，演示时点开就能看到解析。"""
    questions: list[dict[str, Any]] = []
    for index in range(2):
        fact = facts[min(index, len(facts) - 1)]
        questions.append(
            {
                "question": f"关于「{_clip(context.topic, 16)}」，下列哪项符合课本描述？",
                "options": [
                    _clip(fact, 60),
                    "与课本描述相反的说法",
                    "课本中未提及的推测",
                    "以上都不正确",
                ],
                "answer_index": 0,
                "explanation": _clip(fact, 90),
            }
        )
    return questions


def generate_anim_2d(context: MockContext) -> dict[str, Any]:
    """3 个关键帧，每帧带 3 个元素（区域 + 时间标记 + 图例），坐标按 1000×600 逻辑画布。"""
    facts = context.facts
    step_count = min(3, max(2, len(facts)))
    steps: list[dict[str, Any]] = []
    years = [year for fact in facts for year in _YEAR_RE.findall(fact)]

    for step_index in range(step_count):
        fact = facts[min(step_index, len(facts) - 1)]
        stage_label = years[min(step_index, len(years) - 1)] if years else f"阶段 {step_index + 1}"
        center_x = 220.0 + 180.0 * step_index
        entities: list[dict[str, Any]] = [
            {
                "id": "entity-0",
                "shape": "zone",
                "label": stage_label,
                "x": center_x,
                "y": 320.0,
                "color": PALETTE[step_index % 3],
                "details": _clip(fact, 90),
                "source_page": context.pages[min(step_index, len(context.pages) - 1)],
            },
            {
                "id": "entity-1",
                "shape": "label_box",
                "label": stage_label,
                "x": center_x,
                "y": 120.0,
                "color": PALETTE[6],
                "details": f"时间标记：{stage_label}",
                "source_page": context.pages[min(step_index, len(context.pages) - 1)],
            },
            {
                "id": "entity-2",
                "shape": "arrow",
                "label": "推进方向",
                "x": min(center_x + 90.0, 940.0),
                "y": 320.0,
                "color": PALETTE[2],
                "details": "表示该阶段向下一阶段的推进方向。",
                "source_page": context.pages[min(step_index, len(context.pages) - 1)],
            },
        ]
        steps.append(
            {
                "step_index": step_index,
                "narration": _clip(fact, 120),
                "source_ref": f"{context.section_code}, p.{context.pages[min(step_index, len(context.pages) - 1)]}"
                if context.section_code
                else "Extended Knowledge",
                "entities": entities,
            }
        )
    return {"type": "anim_2d", "title": context.topic, "steps": steps}


def generate_scene_3d(context: MockContext) -> dict[str, Any]:
    """5 个节点（1 主体 + 1 地形 + 3 部件）+ 3 个机位，标注点均绑定课本句子。"""
    facts = context.facts
    page = context.primary_page
    primitives: tuple[tuple[str, str, tuple[float, float, float], tuple[float, float, float]], ...] = (
        ("box", "建筑主体", (0.0, 1.5, 0.0), (2.0, 1.0, 2.0)),
        ("terrain_plane", "地形基座", (0.0, 0.0, 0.0), (5.0, 1.0, 5.0)),
        ("cylinder", "左侧构件", (-0.9, 1.0, 0.9), (0.4, 1.0, 0.4)),
        ("cone", "顶部构造", (0.9, 2.4, -0.9), (0.8, 0.8, 0.8)),
        ("sphere", "核心节点", (0.0, 2.8, 0.0), (0.5, 0.5, 0.5)),
    )
    nodes: list[dict[str, Any]] = []
    for index, (primitive, label, position, scale) in enumerate(primitives):
        fact = facts[min(index, len(facts) - 1)]
        nodes.append(
            {
                "id": f"node-{index}",
                "primitive": primitive,
                "label": label,
                "position": position,
                "scale": scale,
                "rotation": (0.0, 0.0, 0.0),
                "color": PALETTE[index % 4],
                "annotation_markdown": _clip(fact, 100),
                "source_page": page,
                "parent_id": "node-0" if index >= 2 else None,
            }
        )

    camera_tour = [
        {
            "name": "全景",
            "position": (8.0, 6.0, 8.0),
            "target": (0.0, 1.5, 0.0),
            "explanation": "先看整体比例与体量关系。",
        },
        {
            "name": "剖面",
            "position": (5.0, 2.0, 0.0),
            "target": (0.0, 1.5, 0.0),
            "explanation": "沿侧面切开，看内部层次与支撑结构。",
        },
        {
            "name": "俯瞰",
            "position": (0.0, 12.0, 0.1),
            "target": (0.0, 0.0, 0.0),
            "explanation": "俯视看方位与布局，确认各部件相对位置。",
        },
    ]
    return {"type": "scene_3d", "title": context.topic, "nodes": nodes, "camera_tour": camera_tour}


def generate_spec(artifact_type: str, context: MockContext) -> dict[str, Any]:
    """按类型分发。未知类型退化为 ppt（宁可给出可用产物，也不要让演示中断）。"""
    if artifact_type == "anim_2d":
        return generate_anim_2d(context)
    if artifact_type == "scene_3d":
        return generate_scene_3d(context)
    return generate_ppt(context)


def _segments(node_path: str) -> list[str]:
    return [part for part in node_path.split("/") if part]


def _clamp_index(raw: str, length: int) -> int:
    try:
        index = int(raw)
    except (TypeError, ValueError):
        return 0
    return min(max(index, 0), max(length - 1, 0))


def generate_patch_ops(spec: dict[str, Any], *, node_path: str, context: MockContext) -> list[PatchOp]:
    """针对锚点生成 1~2 条增量指令。

    绝不整树重写：只改被点中节点自己的字段（`speaker_notes` / `narration` / `annotation_markdown`），
    这正是 P0-10② 「其余节点字节级不变」能成立的前提。
    """
    segments = _segments(node_path)
    root = segments[0] if segments else ""
    spec_type = str(spec.get("type", ""))
    expanded = _expanded_text(context)

    if spec_type == "ppt" and root == "slides":
        slides = spec.get("slides") or []
        index = _clamp_index(segments[1], len(slides)) if len(segments) > 1 else 0
        bullets = [_clip(fact, 56) for fact in context.facts[:3]]
        return [
            PatchOp(
                op="update_node",
                node_path=f"/slides/{index}",
                payload={"speaker_notes": expanded, "bullets": bullets},
                reason=f"按追问「{_clip(context.query, 20)}」细化该页讲解与要点",
            )
        ]

    if spec_type == "anim_2d" and root == "steps":
        steps = spec.get("steps") or []
        index = _clamp_index(segments[1], len(steps)) if len(segments) > 1 else 0
        if len(segments) >= 4 and segments[2] == "entities":
            step = steps[index] if index < len(steps) else {}
            entities = step.get("entities") or []
            entity_index = _clamp_index(segments[3], len(entities))
            return [
                PatchOp(
                    op="update_node",
                    node_path=f"/steps/{index}/entities/{entity_index}",
                    payload={"details": expanded},
                    reason="补充被点中元素的详细知识点解释",
                )
            ]
        return [
            PatchOp(
                op="update_node",
                node_path=f"/steps/{index}",
                payload={"narration": expanded},
                reason="细化该关键帧的旁白解说",
            )
        ]

    if spec_type == "scene_3d" and root == "nodes":
        nodes = spec.get("nodes") or []
        index = _clamp_index(segments[1], len(nodes)) if len(segments) > 1 else 0
        ops = [
            PatchOp(
                op="update_node",
                node_path=f"/nodes/{index}",
                payload={"annotation_markdown": expanded},
                reason="补充该部件的空间结构说明",
            )
        ]
        presets = spec.get("camera_tour") or []
        if presets:
            # 顺带把镜头推到「剖面」机位：学生点部件时最想看的就是内部关系
            preset_name = str(presets[min(1, len(presets) - 1)].get("name", "全景"))
            ops.append(
                PatchOp(
                    op="focus_camera",
                    node_path=f"/camera_tour/{min(1, len(presets) - 1)}",
                    payload={"preset_name": preset_name},
                    reason="把镜头切到更能看清该部件的机位",
                )
            )
        return ops

    if spec_type == "scene_3d" and root == "camera_tour":
        presets = spec.get("camera_tour") or []
        index = _clamp_index(segments[1], len(presets)) if len(segments) > 1 else 0
        preset_name = str(presets[index].get("name", "全景")) if index < len(presets) else "全景"
        return [
            PatchOp(
                op="update_node",
                node_path=f"/camera_tour/{index}",
                payload={"explanation": expanded},
                reason="补充该机位想强调的观察重点",
            ),
            PatchOp(
                op="focus_camera",
                node_path=f"/camera_tour/{index}",
                payload={"preset_name": preset_name},
                reason="切到该机位",
            ),
        ]

    # 兜底：锚点与 spec 类型不匹配（例如前端传了旧 artifact 的路径），
    # 只改第一个节点的讲解字段，保持「局部更新」语义而不是整树重写。
    if spec_type == "ppt":
        return [
            PatchOp(
                op="update_node",
                node_path="/slides/0",
                payload={"speaker_notes": expanded},
                reason="锚点失效，已回退为更新首屏讲解",
            )
        ]
    if spec_type == "anim_2d":
        return [
            PatchOp(
                op="update_node",
                node_path="/steps/0",
                payload={"narration": expanded},
                reason="锚点失效，已回退为更新首个关键帧旁白",
            )
        ]
    return [
        PatchOp(
            op="update_node",
            node_path="/nodes/0",
            payload={"annotation_markdown": expanded},
            reason="锚点失效，已回退为更新首个部件标注",
        )
    ]


def _expanded_text(context: MockContext) -> str:
    """追问后的讲解文本：围绕追问词 + 课本事实，确定性拼接。"""
    head = f"围绕「{_clip(context.query, 24)}」再展开："
    body = " ".join(_clip(fact, 80) for fact in context.facts[:2])
    return _clip(f"{head}{body}", 220)


__all__ = [
    "MockContext",
    "build_context",
    "extract_facts",
    "generate_anim_2d",
    "generate_patch_ops",
    "generate_ppt",
    "generate_scene_3d",
    "generate_spec",
]

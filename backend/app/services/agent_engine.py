"""Agent 编排：CRAG 全流程，产出 §5.4 的 8 类 SSE 事件。

一次 turn 的固定管线（顺序不可变）：
    turn_start → retrieval → modality_decision → token* → artifact? → citations → turn_end

为什么把编排放在服务层而不是 API 层：同一套逻辑要服务两个入口
（`POST /api/chat/stream` 流式、`POST /api/chat` 非流式），放在路由里必然写两遍、
然后两份实现慢慢漂移。

关键取舍：
- **token 与 artifact 分离**：正文（讲解）先流式吐，AST 后生成。这样 TTFT 只取决于
  检索 + 首句生成，不被 AST 生成（更慢）拖累，满足 P0-05① 的 3 秒。
- **校验失败发 error 但不中断**：§5.4 规则③。重试期间用户看到「已自动重试（2/3）」，
  而不是白屏。
- **NO_MATCH 不生成 artifact**：没有课本依据就不产出内容，避免「凭空造课件」，
  这是 P0-05④「禁止编造页码」的延伸。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.db import repository
from app.db.session import get_session_factory
from app.db.vector_store import get_vector_store
from app.errors import AppError, ErrorCode
from app.logging_conf import log_event
from app.schemas.artifacts import PALETTE, Citation, parse_artifact_spec
from app.schemas.chat import (
    Anchor,
    ArtifactEvent,
    CitationsEvent,
    ErrorEvent,
    ModalityDecisionEvent,
    RetrievalEvent,
    RetrievedChunk as RetrievedChunkEvent,
    TokenEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from app.schemas.patch import PatchOp, PatchRequest
from app.services.llm_client import LLMClient, RateLimitedError, get_llm_client
from app.services.mock_generator import MockContext, build_context, generate_patch_ops, generate_spec
from app.services.patch_engine import apply_patch_request
from app.services.text_signals import content_words
from app.services.retriever import (
    NO_MATCH,
    PARTIAL_OR_BEYOND,
    SYLLABUS_MATCH,
    RetrievalResult,
    RetrievedChunk,
    Retriever,
    get_retriever,
)
from app.services.router import NONE, RouteDecision, route

# 事件体既可能是 BaseModel（8 类 SSE 事件），也可能是中间产物（spec dict / ops 列表）
Event = tuple[str, Any]

_SENTENCE_SPLIT_RE = re.compile(r"[。！？；\n]|(?<=[.!?])\s+")
# 正文页码标记：**单页与范围两种写法都要认**。
# 早期只写了 `\[p\.(\d+)\]`，而真实模型（DeepSeek）习惯输出 `[p.74-75]` 这种范围写法，
# 于是整条匹配不上 —— 「正文引用了检索集外页码」这道后校验被静默跳过，
# P0-05④「禁止编造页码」的最后一道闸门形同虚设（模型写 `[p.999-1000]` 也拦不住）。
_PAGE_CITATION_RE = re.compile(r"\[p\.(\d+)(?:-(\d+))?\]")
# 正文分片长度：约等于一行中文的视觉长度，流式渲染更顺滑
TOKEN_CHUNK_SIZE = 26
MAX_REPLY_FACTS = 4

_SYSTEM_PROMPT = (
    "你是马来西亚 KSSM 中学历史课的助教。只依据给定的课本切片作答，"
    "每条事实性陈述后面必须标注页码，格式为 [p.页码]。"
    "如果切片不足以回答，明确说「课本未覆盖」，不得编造页码。"
)
_ARTIFACT_SYSTEM_PROMPT = (
    "你是教学 JSON 生成器。只输出一个 JSON 对象，不要解释、不要 Markdown 围栏。"
    "严禁输出 HTML、CSS 或 JavaScript 代码。"
    "字段名必须与用户消息里给出的契约**逐字一致**，不得自创字段、不得改名"
    "（例如不许用 heading 代替 title、不许用 section 代替 topic、"
    "不许用 content 代替 speaker_notes，也不许出现契约之外的任何键）。"
    "契约里声明为「数组或 null」的字段，用不到时写 null，不要省略整个键。"
    # P0-05④：页码只能来自用户消息里给的事实清单，不得自创、不得照抄示例。
    # 契约示例**刻意不写任何页码**（只写章节号）：示例里一旦出现具体页码，模型就会原样照抄，
    # 在真实页码不是那个数字的小节上产出「落在合法区间内、但与该页无关」的假引用 ——
    # 它既通不过人工核对，又躲得过页码钳制（钳制只判「在不在集合里」），是最坏的一种错。
    "source_ref / source_page 的页码只能取自用户消息里「课本事实」每条前面方括号中的数字，"
    "不得自行推算、不得把契约示例里的数字当页码（示例里的 source_ref 只有章节号）；"
    "没有可用页码时 source_ref 只写章节号（如 \"4.1.2\"），source_page 写 null。"
)

# 调色板拼成一行写进契约，避免模型编造 #FF0000 这类不在 §9.5 里的颜色被校验拒绝。
_PALETTE_TEXT = " ".join(PALETTE)

# 产物「数量」契约：`{形态: (顶层列表字段, 下界, 上界)}`。
#
# 为什么单独一张表而不是塞进 pydantic 模型：`parse_artifact_spec` 是所有**读取**路径的
# 共用入口（历史版本回放、版本列表、patch 基线校验、测试夹具），把 `min_length` 改成 5
# 会让库里已有的 spec 与夹具里的 1 页样例全部校验失败。数量约束只对**生成**路径成立，
# 所以放在引擎里做「生成后校验」。
#
# 为什么提示词里的数字要从这里派生（`_cardinality_span`）：提示词与校验器各写一份数字，
# 改一处忘另一处就会出现「提示词要求 5~8 页、校验器却放行 1 页」的静默不一致 —— 那正是
# 本模块要堵的那类缺陷。派生之后只有一个可改的数字源。
_SPEC_CARDINALITY: dict[str, tuple[str, int, int]] = {
    "ppt": ("slides", 5, 8),
    "anim_2d": ("steps", 2, 5),
    "scene_3d": ("nodes", 3, 8),
}


def _cardinality_span(artifact_type: str) -> str:
    """`"5~8"` 形式的数量区间，供提示词文本引用（避免数字写两份）。"""
    _, low, high = _SPEC_CARDINALITY[artifact_type]
    return f"{low}~{high}"


def _validate_spec_cardinality(spec: dict[str, Any], artifact_type: str) -> None:
    """产物数量后校验：结构合法 ≠ 内容够用。

    背景（IMPL-NOTES E12 ⑦.3）：`SlideDeckSpec.slides` / `Keyframe2DSpec.steps` /
    `SceneGraph3DSpec.nodes` 的约束都只有 `min_length=1`，所以「只剩 1 页的残页课件」
    能通过结构校验、正常落库、前端正常渲染 —— 系统不报警、日志无异常，用户看到的是
    缺页课件，而系统认为它合法。**静默坏结果比报错更糟**，所以这里把它变成显式失败。

    为什么抛 `ValueError` 而不是 `AppError` / 自定义异常：
    调用点的 `except (ValidationError, AppError, ValueError)` 会捕获它，自动复用现有的
    「重试 3 次 → 降级 mock」链路，**不需要新写重试逻辑**；同时 `feedback` 会把 message
    原样回灌给模型，所以 message 必须写成**模型能照着改**的人话（说清「哪个字段、现在几个、
    要求几个、怎么改」），而不是「invalid spec」。

    只在**生成**路径调用。降级路径（`generate_spec` → mock）不校验：mock 的三种产物
    数量都是确定的且在区间内（有测试锁着），而在这里拦一次只会把「6 页课件」换成
    「空白画布」，反而更差。

    **校验对象必须是 spec 自己声明的 `type`，不是请求的形态**：`parse_artifact_spec`
    按 `spec["type"]` 分派校验器，落库也按 `spec["type"]` 写 `artifact_type` 列。
    若按 `decision.artifact_type` 去数，一旦模型回的形态与请求不一致（例如请求 ppt、
    模型回了一份合法的 anim_2d），就会去数根本不存在的 `slides`、把合法产物判成 0 个，
    白重试 3 次后降级 —— 那是比原缺陷更糟的误伤。
    """
    spec_type = spec.get("type")
    resolved = (
        spec_type
        if isinstance(spec_type, str) and spec_type in _SPEC_CARDINALITY
        else artifact_type
    )
    rule = _SPEC_CARDINALITY.get(resolved)
    if rule is None:
        # 未知形态：`parse_artifact_spec` 已经先拒掉了，这里不猜规则、不误伤
        return
    field_name, low, high = rule
    raw = spec.get(field_name)
    # 不是 list 就当 0：结构校验已保证是 list，这里只做防御，不让 TypeError 漏出去
    count = len(raw) if isinstance(raw, list) else 0
    if low <= count <= high:
        return
    raise ValueError(
        f"{resolved} 的 {field_name} 只有 {count} 个，契约要求 {low}~{high} 个。"
        f"请补足到 {low}~{high} 个后重新输出。"
    )


# 每种产物的「字段契约」：字段名 + 类型 + 枚举取值 + 业务约束 + 一个最小合法示例。
#
# 为什么手写而不用 `model_json_schema()`：pydantic 的 schema 输出充满 $defs / anyOf /
# title 噪音，单是 PPT 就要 4k+ token，而且**看不出**「用不到的字段必须显式写 null」
# 这类关键约定（它只体现在 required 数组里，模型读不出来）。手写版约 500 token，
# 枚举取值与必填规则一眼可见。首次真实链路验证中，缺这份契约导致模型自创
# heading/section 字段、连续 3 次校验失败（错误数 15→21→26，越改越乱）后降级 mock。
_ARTIFACT_CONTRACT: dict[str, str] = {
    "ppt": (
        "【输出契约：ppt】\n"
        '顶层：{"type":"ppt","topic":"课件主题","slides":[每页对象...]}\n'
        "每页对象的字段（11 个键全部必须出现，用不到的写 null，不要省略）：\n"
        "- node_id：字符串，按 slide-1、slide-2 递增\n"
        "- layout：只能是 title_hero / bullet_points / timeline / comparison / cause_effect / quiz\n"
        "- title：字符串\n"
        "- subtitle：字符串或 null\n"
        "- bullets：字符串数组，没有就给 []\n"
        "- comparison_left：字符串数组或 null\n"
        "- comparison_right：字符串数组或 null\n"
        "- timeline_items：数组或 null，"
        '元素为 {"year":"1946","label":"...","detail":"...","source_page":null}\n'
        "- quiz_questions：数组或 null，元素为 "
        '{"question":"...","options":["选项一","选项二","选项三"],"answer_index":0,"explanation":"..."}'
        "（options 至少 2 项，answer_index 必须落在 options 下标范围内）\n"
        "- speaker_notes：字符串，教师口播讲解 2~3 句，要讲课本里的具体内容，"
        "不要写「这一节我们讲……」这类空话\n"
        "- source_ref：字符串，写本节章节号（如 \"4.1.3\"）；"
        "当该条事实标有页码时写成 \"4.1.3, p.71\"，**页码只能取自事实清单**\n"
        "业务约束：共 " + _cardinality_span("ppt") + " 页；第 1 页 layout 必须是 title_hero 且 title 用一句话概括本节主题"
        "（与 topic 一致即可）；最后 1 页 layout 必须是 quiz；"
        "中间各页在 bullet_points / timeline / comparison / cause_effect 里至少用 3 种不同取值；"
        "bullet_points 页给 3~5 条 bullets；"
        "timeline 页必须给 timeline_items 且至少 2 条，comparison 页必须给 comparison_left 与 "
        "comparison_right（各 2~4 条），quiz 页必须给 quiz_questions 且至少 2 题，"
        "其余页对应字段写 null。\n"
        # 示例**只教结构，不教数字**：曾经这里写死 p.76，模型照抄到所有小节，
        # 于是「页码缺失」变成了「页码静默错误」。页码怎么写只在上面的文字说明里讲。
        "最小合法示例（1 页，字段齐全，照此结构扩到 " + _cardinality_span("ppt") + " 页）：\n"
        '{"type":"ppt","topic":"马来亚联合邦的成立","slides":[{"node_id":"slide-1",'
        '"layout":"title_hero","title":"马来亚联合邦的成立","subtitle":"4.1",'
        '"bullets":[],"comparison_left":null,"comparison_right":null,'
        '"timeline_items":null,"quiz_questions":null,'
        '"speaker_notes":"1946 年英国提出马来亚联合邦计划，把各州与槟城、马六甲合并，'
        '并削弱苏丹在州内的主权。","source_ref":"4.1.1"}]}'
    ),
    "anim_2d": (
        "【输出契约：anim_2d】\n"
        '顶层：{"type":"anim_2d","title":"动画主题","steps":[每个关键帧...]}\n'
        "每个关键帧的字段（4 个键全部必须出现）：\n"
        "- step_index：整数，从 0 开始递增\n"
        "- narration：字符串，该帧的旁白解说 1~3 句\n"
        '- source_ref：字符串，写本节章节号（如 "4.1.3"）；'
        '当该条事实标有页码时写成 "4.1.3, p.71"，**页码只能取自事实清单**；'
        '课本未覆盖时写 "Extended Knowledge"\n'
        "- entities：数组，该帧的画面元素\n"
        "每个 entity 的字段（8 个键全部必须出现）：\n"
        "- id：字符串，同一帧内唯一（entity-0、entity-1 递增）\n"
        "- shape：只能是 circle / rect / arrow / label_box / zone\n"
        "- label：字符串，画面上的短标签\n"
        "- x、y：数字。逻辑画布 1000×600，写元素**中心点**坐标，不是像素\n"
        "- color：只能取这些值：" + _PALETTE_TEXT + "\n"
        "- details：字符串，该元素的详细说明\n"
        "- source_page：整数或 null，页码只能取自事实清单，没有就写 null\n"
        "业务约束：" + _cardinality_span("anim_2d") + " 个关键帧；每帧 2~5 个 entity；"
        "画面要体现「随时间推进」—— 同一主体在不同帧的位置或标签应发生变化。\n"
        # 示例只教结构：source_ref 只写章节号、source_page 写 null（见 ppt 契约处的说明）
        "最小合法示例（1 帧，照此扩到 " + _cardinality_span("anim_2d") + " 帧）：\n"
        '{"type":"anim_2d","title":"联合邦计划的推进过程","steps":[{"step_index":0,'
        '"narration":"1946 年英国公布联合邦计划，各地马来人开始组织反对。",'
        '"source_ref":"4.1.1","entities":[{"id":"entity-0","shape":"zone",'
        '"label":"1946","x":320.0,"y":300.0,"color":"#2563EB",'
        '"details":"计划公布阶段的舆论反应。","source_page":null}]}]}'
    ),
    "scene_3d": (
        "【输出契约：scene_3d】\n"
        '顶层：{"type":"scene_3d","title":"场景主题","nodes":[每个节点...],'
        '"camera_tour":[每个机位...]}\n'
        "每个 node 的字段（10 个键全部必须出现，用不到的写 null）：\n"
        "- id：字符串，全局唯一（node-0、node-1 递增）\n"
        "- primitive：只能是 box / sphere / cylinder / cone / terrain_plane / arrow_link\n"
        "- label：字符串\n"
        "- position：[x, y, z] 三个数字的数组（不能写成字符串，也不能写成对象）\n"
        "- scale：[x, y, z] 三个数字的数组\n"
        "- rotation：[x, y, z] 三个数字的数组\n"
        "- color：只能取这些值：" + _PALETTE_TEXT + "\n"
        "- annotation_markdown：字符串，该部件的知识点标注\n"
        "- source_page：整数或 null，页码只能取自事实清单，没有就写 null\n"
        "- parent_id：字符串或 null，表示从属于哪个节点的 id（主体写 null）\n"
        "每个机位的字段（4 个键全部必须出现）：\n"
        "- name：字符串；position：[x, y, z]；target：[x, y, z]；explanation：字符串\n"
        "业务约束：" + _cardinality_span("scene_3d") + " 个 node（1 个主体 + 若干部件，部件用 parent_id 挂到主体上）；"
        "2~4 个机位。\n"
        # 示例只教结构：source_page 写 null（见 ppt 契约处的说明）
        "最小合法示例（1 个节点 + 1 个机位，照此扩展）：\n"
        '{"type":"scene_3d","title":"马六甲城墙结构","nodes":[{"id":"node-0",'
        '"primitive":"box","label":"城墙主体","position":[0.0,1.5,0.0],'
        '"scale":[2.0,1.0,2.0],"rotation":[0.0,0.0,0.0],"color":"#2563EB",'
        '"annotation_markdown":"墙体以红土夯实，厚度约两米。","source_page":null,'
        '"parent_id":null}],"camera_tour":[{"name":"全景","position":[8.0,6.0,8.0],'
        '"target":[0.0,1.5,0.0],"explanation":"先看整体比例与体量关系。"}]}'
    ),
}

# 兜底：未知类型按 ppt 契约走（与 mock_generator.generate_spec 的退化策略一致）
_DEFAULT_ARTIFACT_CONTRACT = _ARTIFACT_CONTRACT["ppt"]

# `source_ref` 里的页码片段：`", p.76"` / `", p.76-77"` / `"p.76"`。
# 与 `_PAGE_CITATION_RE` 同族但**不同用途**：那个认的是正文里的 `[p.76]` 标记，
# 这个认的是 `source_ref` 里的裸 `p.76`，所以不能复用同一个正则。
_REF_PAGE_RE = re.compile(r"\s*,?\s*p\.\s*(\d+)(?:\s*-\s*(\d+))?")

# `source_ref` 剥空后的最后兜底。与 `MockContext.source_ref` 的既有兜底同值：
# `source_ref` 在 schema 里是无 min_length 的裸 str，空串能过校验，但 UI 上等于
# 「有出处字段却什么都不显示」，比明确写「拓展知识」更让人困惑。
_FALLBACK_SOURCE_REF = "Extended Knowledge"


def _clamp_ref(ref: str, allowed: frozenset[int], fallback: str) -> tuple[str, bool]:
    """剥离 `source_ref` 里越界的页码，返回 `(新值, 是否改动)`。

    只要**任一端点**越界就整条剥掉，不做「部分合法宽容」—— 与 `_drop_invalid_citations`
    同一口径：模型编页码的典型形态是「真页码 + 一个凑数的尾巴」，放行半个就等于放行编造。

    剥空后沿兜底链取值（`fallback` → `"Extended Knowledge"`），**绝不返回空字符串**。
    """
    if not ref:
        return ref, False
    pages: list[int] = []
    for match in _REF_PAGE_RE.finditer(ref):
        pages.append(int(match.group(1)))
        if match.group(2):
            pages.append(int(match.group(2)))
    if not pages or all(page in allowed for page in pages):
        return ref, False
    stripped = _REF_PAGE_RE.sub("", ref).strip().strip(",").strip()
    return stripped or fallback or _FALLBACK_SOURCE_REF, True


def _coerce_page(page: Any) -> int | None:
    """把 `source_page` 的各种**合法输入形态**规范化成 `int`；无法规范化返回 `None`。

    为什么需要它：schema 里 `source_page` 是 `int | None`，但 pydantic 的 lax 模式会
    放行 `"999"` / `999.0`（整数浮点）这类形态。到钳制阶段若只判 `isinstance(x, int)`，
    它们会被**整条跳过** —— 越界页码原样落库，且 SSE（走 pydantic 序列化成 `999`）
    与 DB（存原始 `"999"`）字节不一致。QA 已端到端复现。

    规则：
    - `None` → `None`（「本来就没写」，调用方据此区分「无事可做」）
    - `bool` → **非法**。Python 里 `isinstance(True, int)` 为真，不先挡掉就会把
      `True` 当成第 1 页放行 —— 这是本函数最容易写错的一处，**必须放在 `int` 之前**
    - `int` → 原值
    - `float` → 只有 `is_integer()` 才转（`999.5` 不是页码，是脏数据）
    - `str` → 去空白后按整数解析，失败再按浮点解析一次（pydantic lax 也认 `"999.0"`）
    - 其他（list / dict / 任意对象）→ 非法
    """
    if page is None:
        return None
    if isinstance(page, bool):
        return None
    if isinstance(page, int):
        return page
    if isinstance(page, float):
        return int(page) if page.is_integer() else None
    if isinstance(page, str):
        text = page.strip()
        try:
            return int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                return None
            return int(number) if number.is_integer() else None
    return None


def _fix_source_page(node: dict[str, Any], allowed: frozenset[int]) -> int:
    """把 `node["source_page"]` 规范化并钳制到位（**原地改写**），返回被钳制处数（0/1）。

    三种处置，**一个都不放行**：

    - **形态非法**（`True` / `"abc"` / `999.5` / list）→ 置 `None`，计一次钳制。
      不因为「类型怪」就跳过：跳过等于让脏数据绕过这道门，而这道门是「先有钳制才允许
      走 flash」的前置条件（设计 §4.9）。
    - **越界** → 置 `None`，计一次钳制。
    - **合法但类型不规范**（`"85"` / `85.0`）→ **写回 `int`**，不计数。
      写回是必须的：只判断不写回，SSE 走 pydantic 会序列化成 `85`、DB 存的是 `"85"`，
      同一份产物两个出口字节不一致（QA 实测复现的就是这个）。
    """
    if "source_page" not in node:
        return 0
    raw = node["source_page"]
    if raw is None:
        return 0
    coerced = _coerce_page(raw)
    if coerced is None or coerced not in allowed:
        node["source_page"] = None
        return 1
    if type(raw) is not int:
        # 只规范化、不计数：它不是越界，只是形态需要收敛，否则 SSE/DB 不一致
        node["source_page"] = coerced
    return 0


# --------------------------------------------------------------------------- #
# 后置确定性页码重绑定
# --------------------------------------------------------------------------- #
# **为什么需要这一步（实测根因）**：多页产物的 `source_ref` 页码会被**复制**。
# 自回归解码时，同一份 JSON 里重复出现的 `"4.1.3, p.71"` 会互相强化，实测一份 6 页课件
# 6 次全部写 `p.71`，而它的内容全部来自 `4.1.3`（真实页码 `p.76-77`）。这是**确定性的
# 复制**而不是概率性编造，所以加提示词规则治不了（实测 91.4% → 93.3%，已回退）。
# 能用确定性手段修正的错误，不该继续交给概率手段 —— 本段就是那个确定性手段。
#
# **判据是「页码真实性」，不是「页码分散度」**：
# - 单节聚焦（6 页内容都来自同一节）→ 重绑定后 6 页**仍然全部**是 `p.76-77`。那是**正确**
#   的，因为内容确实同源。为了「让页码看起来分散」而塞次优页码，是把对的改错。
# - 跨节概览（各页讲不同子节）→ 各页分别落到各自的切片。
# 重绑定前 `p.71` 是**假的**（它甚至不在核心命中切片里），重绑定后是**真的**。
#
# **算法两阶段**：
# 1. 内容页（`layout` 不在 `_NON_CONTENT_LAYOUTS`）用实词重叠打分，过置信度门槛的页
#    绑定命中切片的 `page_start~page_end`；
# 2. 兜底页（`title_hero` / `quiz` / 未达门槛的内容页）绑定**第一阶段命中切片的页码并集
#    跨度**。刻意**不**用「全部召回切片的跨度」：检索池里常含上一节的开头页（如 `4.1` 的
#    `p.71`），那样兜底会退化成 `p.70-77`，比现状更差。仅当所有内容页都未达门槛时，
#    才退化为召回切片全集跨度。
#
# 实词抽取一律走 `text_signals.content_words()`，**不要**在这里另写 n-gram 提取器：
# 那个函数已处理中文 bigram、虚词过滤、马来文词、双字套话剔除，是踩过坑调出来的。
_REBIND_MIN_HITS = 3
_REBIND_MIN_COVERAGE = 0.20
# 这两类页天生不含课本正文：`title_hero` 是概括标题，`quiz` 是自造题干。它们的实词量少
# 且与正文弱相关，进第一阶段只会产生噪声匹配，所以直接走兜底。
_NON_CONTENT_LAYOUTS = frozenset({"title_hero", "quiz"})


def _page_ref(section_code: str, page_start: int, page_end: int) -> str:
    """拼 `source_ref`，沿用既有格式：单页 `4.1.3, p.76`，区间 `4.1.3, p.76-77`。

    `page_start <= 0` 视为「没有可信页码」，只写章节号 —— 与契约里
    「没有可用页码时 source_ref 只写章节号」一致，绝不返回空串（空串在 UI 上等于
    「有出处字段却什么都不显示」）。
    """
    code = (section_code or "").strip() or _FALLBACK_SOURCE_REF
    if page_start <= 0:
        return code
    if page_end <= page_start:
        return f"{code}, p.{page_start}"
    return f"{code}, p.{page_start}-{page_end}"


def _as_text_list(value: Any) -> list[str]:
    """把「字符串 / 字符串数组 / None」统一成 `list[str]`（脏类型一律当空）。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _slide_semantic_text(slide: dict[str, Any]) -> str:
    """一页幻灯片的**全部语义文本**，用于实词重叠打分。

    刻意**不含** `quiz_questions`：题干是模型自造的，混进来会把 quiz 页拉向
    「看起来最像的正文」—— 而 quiz 页本来就该走兜底。
    """
    parts: list[str] = []
    parts.extend(_as_text_list(slide.get("title")))
    parts.extend(_as_text_list(slide.get("subtitle")))
    parts.extend(_as_text_list(slide.get("bullets")))
    parts.extend(_as_text_list(slide.get("comparison_left")))
    parts.extend(_as_text_list(slide.get("comparison_right")))
    for item in slide.get("timeline_items") or []:
        if isinstance(item, dict):
            parts.extend(_node_semantic_text(item, ("year", "label", "detail")))
    parts.extend(_as_text_list(slide.get("speaker_notes")))
    return "\n".join(parts)


def _node_semantic_text(node: dict[str, Any], keys: Sequence[str]) -> str:
    """2D entity / 3D node / 时间轴条目的语义文本：把指定键的文字拼起来。"""
    parts: list[str] = []
    for key in keys:
        parts.extend(_as_text_list(node.get(key)))
    return "\n".join(parts)


def _best_chunk(
    words: frozenset[str], chunk_words: Sequence[frozenset[str]]
) -> int | None:
    """返回实词命中最多的切片下标；未达置信度门槛返回 `None`。

    门槛是**双条件**，两个都要：
    - 绝对命中数 `>= _REBIND_MIN_HITS`：挡住「短文本偶然重合」（`title_hero` 只有十几个
      实词，两三个 bigram 撞上就算「命中」是噪声）；
    - 覆盖率 `>= _REBIND_MIN_COVERAGE`：挡住「长文本里只碰到几个词」。
    实测参考（真实 6 页课件，实词 / 命中 / 覆盖率）：P2 63→53→84%、P3 58→40→69%、
    P4 58→49→84%、P5 28→21→75%，四个内容页全部过门槛；P1 12→3→25%（走兜底）、
    P6 10→0→0%（走兜底）。只用一个条件都会误判 —— 若只看绝对命中数，P5 这种短页
    会被长页压过去；若只看覆盖率，两三个词的小页会凭空「100% 命中」。
    """
    if not words:
        return None
    best_index: int | None = None
    best_hit = 0
    for index, candidate in enumerate(chunk_words):
        hit = len(words & candidate)
        # 严格大于：并列时保留**检索排序更靠前**的切片（`chunks` 已按分数降序）
        if hit > best_hit:
            best_hit = hit
            best_index = index
    if best_index is None:
        return None
    if best_hit < _REBIND_MIN_HITS:
        return None
    if best_hit / len(words) < _REBIND_MIN_COVERAGE:
        return None
    return best_index


def _fallback_span(
    matched: Sequence[tuple[int, int]], chunks: Sequence[RetrievedChunk]
) -> tuple[int, int]:
    """兜底页码跨度：第一阶段命中切片的并集 → 退化为召回切片全集跨度。

    「并集」用 `min(page_start) ~ max(page_end)` 表示（见模块上方说明：刻意不用召回池
    跨度，否则会含进上一节的开头页）。
    """
    if matched:
        return min(start for start, _ in matched), max(end for _, end in matched)
    if not chunks:
        return 0, 0
    return (
        min(chunk.page_start for chunk in chunks),
        max(chunk.page_end for chunk in chunks),
    )


def _ref_page_span(ref: Any) -> tuple[int, ...]:
    """取出 `source_ref` 里的页码（`p.76` → `(76,)`，`p.76-77` → `(76, 77)`）。

    无页码（只有章节号）或形态不是字符串时返回空元组。
    """
    if not isinstance(ref, str):
        return ()
    pages: list[int] = []
    for match in _REF_PAGE_RE.finditer(ref):
        pages.append(int(match.group(1)))
        if match.group(2):
            pages.append(int(match.group(2)))
    return tuple(pages)


def _ref_is_truthful(ref: Any, page_start: int, page_end: int) -> bool:
    """既有 `source_ref` 的页码是否**全部**落在给定区间内。

    落在里面就是**真话**（哪怕只是区间里的某一页，也比笼统的整段更精确），不必改。
    重绑定的职责是修正**说错**的页码，不是把「精确且正确」重写成「笼统且正确」——
    后者会让每一页都退化成整段区间，把模型的精度白白丢掉。
    """
    pages = _ref_page_span(ref)
    if not pages:
        return False
    return all(page_start <= page <= page_end for page in pages)


def _page_is_truthful(value: Any, page_start: int, page_end: int) -> bool:
    """`source_page` 版本的同一条判断（脏类型先规范化，规范化不出来就算「不真」）。"""
    page = _coerce_page(value)
    if page is None:
        return False
    return page_start <= page <= page_end


def _rebind_spec_source_pages(
    spec: dict[str, Any],
    chunks: Sequence[RetrievedChunk],
    *,
    section_code: str = "",
) -> tuple[dict[str, Any], int]:
    """按检索切片**重算** spec 里每个节点的页码，返回 `(新 spec, 改动处数)`。

    纯函数（`copy.deepcopy` 后再改，不动入参），无随机、无状态、**幂等**：对已经正确的
    spec 再跑一次结果完全不变 —— 幂等是硬要求，因为同一份 spec 在重试 / 回放路径上
    可能被处理多次，不幂等就会「二次处理漂移」。

    不改变任何字段类型（`source_ref` 仍是 `str`、`source_page` 仍是 `int | None`），
    因此不会引入新的校验失败、不会触发 `contract_retry`。必须在
    `parse_artifact_spec` + `_validate_spec_cardinality` **之后**、
    `_clamp_spec_pages` **之前**调用：先校验再重绑定，钳制留到最后当兜底，
    确保重绑定万一引入越界页码也会被剥掉。

    `chunks` 为空时原样返回：没有切片就没有「真实页码」这个概念，绝不凭空造页码。

    覆盖范围：ppt 的 `SlideNode.source_ref` + `TimelineItem.source_page`；
    anim_2d 的 step `source_ref` + `Entity2D.source_page`；scene_3d 的
    `Node3D.source_page`。`camera_tour` 是机位描述，没有页码字段，不处理。
    """
    if not chunks:
        return spec, 0

    chunk_words = tuple(
        frozenset(content_words(chunk.content or chunk.snippet or "")) for chunk in chunks
    )
    default_code = (section_code or "").strip() or (chunks[0].section_code or "")
    result = copy.deepcopy(spec)
    spec_type = str(result.get("type") or "")
    changed = 0

    if spec_type == "ppt":
        slides = [
            slide for slide in (result.get("slides") or []) if isinstance(slide, dict)
        ]
        matches: dict[int, int] = {}
        matched_pages: list[tuple[int, int]] = []
        for index, slide in enumerate(slides):
            if str(slide.get("layout") or "") in _NON_CONTENT_LAYOUTS:
                continue
            words = frozenset(content_words(_slide_semantic_text(slide)))
            chunk_index = _best_chunk(words, chunk_words)
            if chunk_index is None:
                continue
            matches[index] = chunk_index
            chunk = chunks[chunk_index]
            matched_pages.append((chunk.page_start, chunk.page_end))

        fallback = _fallback_span(matched_pages, chunks)
        for index, slide in enumerate(slides):
            chunk_index = matches.get(index)
            if chunk_index is None:
                code, span = default_code, fallback
            else:
                chunk = chunks[chunk_index]
                code = chunk.section_code or default_code
                span = (chunk.page_start, chunk.page_end)
            ref = _page_ref(code, span[0], span[1])
            if not _ref_is_truthful(slide.get("source_ref"), span[0], span[1]):
                slide["source_ref"] = ref
                changed += 1
            # 时间轴条目自带的 `source_page` 同样是模型编的（真实样本里出现 71/72/74
            # 三个互不相邻的页码）。逐条按自己的文字打分，打不出置信度就用本页跨度起点。
            for item in slide.get("timeline_items") or []:
                if not isinstance(item, dict) or "source_page" not in item:
                    continue
                item_words = frozenset(
                    content_words(_node_semantic_text(item, ("year", "label", "detail")))
                )
                item_index = _best_chunk(item_words, chunk_words)
                page = (
                    chunks[item_index].page_start if item_index is not None else span[0]
                )
                if not _page_is_truthful(item.get("source_page"), span[0], span[1]):
                    item["source_page"] = page
                    changed += 1

    elif spec_type == "anim_2d":
        steps = [step for step in (result.get("steps") or []) if isinstance(step, dict)]
        matches: dict[int, int] = {}
        matched_pages: list[tuple[int, int]] = []
        for index, step in enumerate(steps):
            words = frozenset(content_words(_node_semantic_text(step, ("narration",))))
            chunk_index = _best_chunk(words, chunk_words)
            if chunk_index is None:
                continue
            matches[index] = chunk_index
            chunk = chunks[chunk_index]
            matched_pages.append((chunk.page_start, chunk.page_end))

        fallback = _fallback_span(matched_pages, chunks)
        for index, step in enumerate(steps):
            chunk_index = matches.get(index)
            if chunk_index is None:
                code, span = default_code, fallback
            else:
                chunk = chunks[chunk_index]
                code = chunk.section_code or default_code
                span = (chunk.page_start, chunk.page_end)
            ref = _page_ref(code, span[0], span[1])
            if not _ref_is_truthful(step.get("source_ref"), span[0], span[1]):
                step["source_ref"] = ref
                changed += 1
            for entity in step.get("entities") or []:
                if not isinstance(entity, dict) or "source_page" not in entity:
                    continue
                entity_words = frozenset(
                    content_words(_node_semantic_text(entity, ("label", "details")))
                )
                entity_index = _best_chunk(entity_words, chunk_words)
                page = (
                    chunks[entity_index].page_start
                    if entity_index is not None
                    else span[0]
                )
                if not _page_is_truthful(entity.get("source_page"), span[0], span[1]):
                    entity["source_page"] = page
                    changed += 1

    elif spec_type == "scene_3d":
        nodes = [node for node in (result.get("nodes") or []) if isinstance(node, dict)]
        matches: dict[int, int] = {}
        matched_pages: list[tuple[int, int]] = []
        for index, node in enumerate(nodes):
            words = frozenset(
                content_words(_node_semantic_text(node, ("label", "annotation_markdown")))
            )
            chunk_index = _best_chunk(words, chunk_words)
            if chunk_index is None:
                continue
            matches[index] = chunk_index
            chunk = chunks[chunk_index]
            matched_pages.append((chunk.page_start, chunk.page_end))

        fallback = _fallback_span(matched_pages, chunks)
        for index, node in enumerate(nodes):
            if "source_page" not in node:
                continue
            chunk_index = matches.get(index)
            if chunk_index is None:
                span = fallback
            else:
                chunk = chunks[chunk_index]
                span = (chunk.page_start, chunk.page_end)
            if not _page_is_truthful(node.get("source_page"), span[0], span[1]):
                node["source_page"] = span[0]
                changed += 1

    return result, changed


def _clamp_spec_pages(
    spec: dict[str, Any],
    allowed: frozenset[int],
    *,
    fallback_ref: str = "",
) -> tuple[dict[str, Any], int]:
    """把落在检索命中页之外的页码**剥离**，返回 `(新 spec, 被钳制处数)`。

    为什么是「剥离」而不是「带错误重试」：
    - 编造页码不是随机错误，重试三次大概率还是编（IMPL-NOTES E11 记录过错误数
      15→21→26 越改越乱的形态），重试只会把一次 10 秒的调用变成三十秒；
    - 剥离之后的结果与「改动前 v4-pro 的 `p.?` 行为」等价 —— 即 P0-05④ 要求下的
      「可见缺失」，而不是「静默错误」；
    - 计数 `page_clamped` 让这个缺陷可观测，不至于悄悄消失。

    本函数在 `parse_artifact_spec` **通过之后**执行，且不改变任何字段类型
    （`source_ref` 仍是字符串、`source_page` 仍是 `int | None`），因此不会引入
    新的校验失败、不会触发 `contract_retry`。

    `allowed` 必须是 `result.pages`（所有切片整段展开）**而不是** `context.pages`
    （只有每段正文的起始页）：后者会把「p.77 落在 p.76-77 切片内」这种合法页码误剥。
    同一个集合也被 `_drop_invalid_citations` 使用 —— 两个组件对「哪页合法」必须同口径。
    """
    if not allowed:
        # 没有可信页码集合时不动手：钳掉一切等于把「缺失」变成「全部缺失」，反而更差
        return spec, 0
    clamped = 0
    result = copy.deepcopy(spec)
    spec_type = str(result.get("type") or "")
    fallback = fallback_ref.strip() or _FALLBACK_SOURCE_REF

    if spec_type == "ppt":
        for slide in result.get("slides") or []:
            if not isinstance(slide, dict):
                continue
            ref = slide.get("source_ref")
            if isinstance(ref, str):
                new_ref, touched = _clamp_ref(ref, allowed, fallback)
                if touched:
                    slide["source_ref"] = new_ref
                    clamped += 1
            # 时间轴条目也带 source_page（`TimelineItem.source_page`），与 2D/3D 同规则
            for item in slide.get("timeline_items") or []:
                if isinstance(item, dict):
                    clamped += _fix_source_page(item, allowed)
    elif spec_type == "anim_2d":
        for step in result.get("steps") or []:
            for entity in (step or {}).get("entities") or []:
                if isinstance(entity, dict):
                    clamped += _fix_source_page(entity, allowed)
    elif spec_type == "scene_3d":
        for node in result.get("nodes") or []:
            if isinstance(node, dict):
                clamped += _fix_source_page(node, allowed)

    return result, clamped


def _clamp_payload_pages(payload: Any, allowed: frozenset[int], fallback: str) -> int:
    """递归钳制 patch payload 里的 `source_ref` / `source_page`（原地改写），返回改动处数。

    payload 深度很浅（≤3 层），递归比「按 op 类型逐个字段列举」更不容易漏：
    `replace_subtree` 的 payload 就是**一整个节点**，字段列举法一定漏。
    """
    if isinstance(payload, dict):
        changed = 0
        for key, value in list(payload.items()):
            if key == "source_ref" and isinstance(value, str):
                new_ref, touched = _clamp_ref(value, allowed, fallback)
                if touched:
                    payload[key] = new_ref
                    changed += 1
            elif key == "source_page":
                # 与 spec 路径共用同一个规范化+钳制函数：类型脏值（"999" / True / 999.0）
                # 在这里同样会被剥成 None，patch 不是绕过通道
                changed += _fix_source_page(payload, allowed)
            else:
                changed += _clamp_payload_pages(value, allowed, fallback)
        return changed
    if isinstance(payload, list):
        return sum(_clamp_payload_pages(item, allowed, fallback) for item in payload)
    return 0


def _clamp_patch_ops(
    ops: list[PatchOp],
    allowed: frozenset[int],
    *,
    fallback_ref: str = "",
) -> tuple[list[PatchOp], int]:
    """对一批 `PatchOp` 的 payload 做页码钳制，返回 `(ops, 被钳制处数)`。

    为什么 patch 必须和 create 用同一个钳制函数（无例外）：
    1. `update_node` 的 payload 是**局部字段 dict**（IMPL-NOTES F1），完全可能只带
       `{"source_ref": "4.3.1, p.76"}` —— 若 create 钳制而 patch 不钳制，用户点一次
       节点追问，刚剥掉的假页码就又回来了，是**用户可见的自相矛盾**；
    2. `insert_node` / `replace_subtree` 会引入整块新内容（可能是一整个节点），
       不钳制等于给模型留了一条绕过通道。

    调用点必须在 `yield "ops"` **之前**（即早于 `apply_patch_request`）：落库的版本、
    SSE 下发的 ops、前端本地应用三者必须是同一份已钳制内容，否则前端指纹自检会报
    「未声明的改动」。
    """
    if not allowed:
        return ops, 0
    fallback = fallback_ref.strip() or _FALLBACK_SOURCE_REF
    clamped = 0
    for op in ops:
        if isinstance(op.payload, dict):
            clamped += _clamp_payload_pages(op.payload, allowed, fallback)
    return ops, clamped


@dataclass
class ArtifactWork:
    """产物分支的返回值。**只是数据，不带副作用** —— 不落库、不发事件。

    为什么必须有这个值对象：分支 B（并行任务）要能「跑完再交回来」，而 async generator
    不能被两个任务同时消费（跨任务 `asend` 会抛 RuntimeError），所以分支 B 只返回数据，
    由主路径统一发事件。这条约束支撑两件事：

    1. 「artifact 事件必须在落库之后发出」（设计 §4.4）：分支 B **不持有 session 实例、
       不调用任何写路径**，唯一的写库点在 `_commit_artifact`；
    2. 「断连不产生孤儿产物」（设计 §5.2）：取消时分支 B 没有半成品数据需要回滚。

    **注意措辞**：这里的保证是「**不写**」，不是「**不能写**」。`self._session_factory`
    是引擎属性，分支 B 在语言层面当然够得着它 —— 早期文档写成「物理上无法落库」是夸大，
    已按设计 §5.2 修正。真实约束靠三件事兜底：代码评审（分支 B 里不出现写路径）、
    `_commit_artifact` 是唯一写点、以及 commit 计数测试（断连后产物条数不增）。
    夸大一个结构性保证比给一个诚实的弱保证更危险：后来人会以为不需要测试兜底。
    """

    kind: Literal["create", "patch"]
    # create：完整 AST
    spec: dict[str, Any] | None = None
    # patch：基线快照（deepcopy）+ 版本号 + 形态，供 `_commit_patch` 用
    spec_dict: dict[str, Any] | None = None
    base_version: int | None = None
    artifact_type: str | None = None
    ops: list[PatchOp] | None = None
    # 按产生顺序收集的中间事件（契约重试 / 限流 / 降级）。数量有界：
    # spec 路径最多 3 条重试 + 1 条降级，ops 路径最多 2 条重试 —— 所以用 list 而不是 queue，
    # 不需要背压（设计 §4.2）。
    interim: list[Event] = field(default_factory=list)
    rate_limited: bool = False


async def _drain_generation(gen: AsyncIterator[Event]) -> tuple[Any | None, list[Event]]:
    """把一个 `('spec'|'ops'|'error', payload)` 生成器跑完。

    返回 `(最终产物, 按序收集的中间事件)`。异常**不吞**，原样抛给调用方 ——
    `RateLimitedError` 要靠它冒到 `_artifact_work` 才能走串行降级（设计 §4.5 E5）。

    这样 `_generate_spec_stream` / `_generate_ops_stream` **保持 async generator 形态不变**
    （零改动），同时能被「一次性跑完并收集」地调用，`profile_latency` 的按名埋点仍然有效。
    """
    final: Any | None = None
    interim: list[Event] = []
    async for name, payload in gen:
        if name in ("spec", "ops"):
            final = payload
        else:
            interim.append((name, payload))
    return final, interim


def _reap_task(task: "asyncio.Task[Any]") -> None:
    """消费子任务的异常，避免退出时打 `Task exception was never retrieved` 告警。

    被取消的任务不算异常（`cancelled()` 为真时 `exception()` 会抛 CancelledError），
    所以必须先判 `cancelled()`。
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log_event("artifact_work_reaped", level=logging.WARNING, error=str(exc)[:200])


@dataclass(frozen=True)
class TurnRequest:
    """一轮对话的输入。`turn_id` 由调用方决定，便于日志串起同一次请求。"""

    workspace_id: str
    query: str
    mode: str = "syllabus_strict"
    anchor: Anchor | None = None
    artifact_type_override: str | None = None
    artifact_id: str | None = None
    turn_id: str = ""
    # 教材作用域（Level 1「目录树点击直传」）。`None` = 不限文档，跨全部教材检索。
    # 多教材同章节号（《Form 4》《Form 5》都有 `Bab 4 → 4.1`）时，没有它检索会把两本书
    # 的 4.1 混在一起召回，引用显示「4.1, p.76」却不知道是哪本书的 p.76。
    # 放在最后：frozen dataclass 里带默认值的字段必须在无默认值字段之后，且这样不破坏
    # 任何按位置构造的既有调用方。
    document_id: str | None = None

    def with_turn_id(self, turn_id: str) -> "TurnRequest":
        return TurnRequest(
            workspace_id=self.workspace_id,
            query=self.query,
            mode=self.mode,
            anchor=self.anchor,
            artifact_type_override=self.artifact_type_override,
            artifact_id=self.artifact_id,
            turn_id=turn_id,
            # 必须显式透传：漏了这行不会报错、不会让任何测试变红，只在「turn_id 为空
            # 走重建」这条路径上静默丢掉教材作用域。见 test_with_turn_id_preserves_document_id。
            document_id=self.document_id,
        )

    @property
    def is_patch_turn(self) -> bool:
        return self.anchor is not None and bool(self.anchor.artifact_id)


def _clip(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


def _sentences(text: str) -> list[str]:
    return [" ".join(part.split()) for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]


class AgentEngine:
    """依赖全部注入，便于在测试里替换 retriever / llm。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: Any | None = None,
        retriever: Retriever | None = None,
        llm: LLMClient | None = None,
        store: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_factory = session_factory or get_session_factory()
        self._retriever = retriever or get_retriever()
        self._llm = llm or get_llm_client()
        self._store = store or get_vector_store()
        # 惰性创建（见 `_artifact_slot`）：先占位，避免多事件循环复用时绑错 loop
        self._artifact_semaphore: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    async def stream_turn(self, request: TurnRequest) -> AsyncIterator[Event]:
        started = time.perf_counter()
        req = request if request.turn_id else request.with_turn_id(str(uuid.uuid4()))
        demo_mode = self._settings.demo_mode

        yield "turn_start", TurnStartEvent(
            turn_id=req.turn_id,
            workspace_id=req.workspace_id,
            mode=req.mode,  # type: ignore[arg-type]
            demo_mode=demo_mode,
            has_anchor=req.anchor is not None,
            artifact_type_hint=req.artifact_type_override,  # type: ignore[arg-type]
        )

        # ---- 1. 检索 + 评估 ----
        result: RetrievalResult
        try:
            result = await self._retrieve(req)
        except AppError as exc:
            yield "error", ErrorEvent(
                code=exc.code.value,
                message=exc.message,
                stage="retrieval",
                retryable=exc.code in (ErrorCode.LLM_TIMEOUT, ErrorCode.LLM_UPSTREAM_ERROR),
                attempt=1,
            )
            result = RetrievalResult(
                chunks=(), grader_status=NO_MATCH, section_scope=None, top_score=0.0, query_terms=()
            )
        # NO_MATCH 时**不下发候选片段**（P0-05④ 的延伸）：正文说「课本未覆盖」，若同时把
        # 8 条带 page_start/page_end 的候选塞进 `retrieval.chunks`，任何「显示相关片段」的 UI
        # 都会立刻把页码画在「课本未覆盖」旁边 —— 与 `_build_citations` 里同样的自相矛盾。
        # 候选片段仍留在服务端供 grader 诊断，用户可见面一律清空。
        # 注意：`grader_status` / `section_scope` / `top_score` 必须原样保留 ——
        # QA 的判定矩阵与诊断都依赖它们，只清 chunks。
        chunk_events: list[RetrievedChunkEvent] = (
            []
            if result.grader_status == NO_MATCH
            else [
                RetrievedChunkEvent(
                    chunk_id=chunk.chunk_id,
                    section_code=chunk.section_code,
                    section_title=chunk.section_title,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    score=round(chunk.score, 4),
                    snippet=chunk.snippet,
                )
                for chunk in result.chunks
            ]
        )
        yield "retrieval", RetrievalEvent(
            grader_status=result.grader_status,  # type: ignore[arg-type]
            section_scope=result.section_scope,
            top_score=round(result.top_score, 4),
            chunks=chunk_events,
            # 回显本次检索实际生效的教材作用域：多教材同章节号时，只给 section_scope
            # 无法说明「这一轮按哪本书的 4.1 讲的」。前端据此渲染「当前按《Form 4》4.1 讲解」。
            scope_document_id=result.document_id,
        )

        # ---- 2. 形态路由 ----
        decision = route(
            req.query, anchor=req.anchor, user_override=req.artifact_type_override
        )
        action = self._decide_action(req, result, decision)
        effective_type = NONE if action == "none" else decision.artifact_type
        yield "modality_decision", ModalityDecisionEvent(
            artifact_type=effective_type,  # type: ignore[arg-type]
            action=action,  # type: ignore[arg-type]
            reason=decision.reason,
            user_override=decision.user_override,
        )
        log_event(
            "route_decision",
            workspace_id=req.workspace_id,
            artifact_type=effective_type,
            action=action,
            user_override=decision.user_override,
        )

        context = self._build_context(req, result)

        # ---- 产物分支的 fork 决策（设计 §1.2 / §4.1）----
        # `work_task` 是**单变量**：单回合最多 2 个并发请求（正文 1 + 产物 1），
        # 结构上不可能更多。分支 B 只读不写（见 `ArtifactWork` 的 docstring）。
        wants_artifact = action in ("create_artifact", "patch_artifact")
        parallel = wants_artifact and self._settings.ARTIFACT_PARALLEL_ENABLED
        work_task: asyncio.Task[ArtifactWork] | None = None

        def _fork() -> "asyncio.Task[ArtifactWork]":
            return asyncio.create_task(
                self._artifact_work(
                    req,
                    decision,
                    context,
                    action,
                    defer_on_rate_limit=True,
                    chunks=result.chunks,
                )
            )

        try:
            if parallel and self._settings.ARTIFACT_FORK_MODE == "immediate":
                work_task = _fork()

            # ---- 3. 正文流式输出 ----
            reply_parts: list[str] = []
            ttft_ms: int | None = None
            try:
                async for delta in self._reply_stream(req, result, decision, context):
                    if not delta:
                        continue
                    if ttft_ms is None:
                        ttft_ms = int((time.perf_counter() - started) * 1000)
                        # ★ 默认 fork 点：TTFT 已在上一行定格，产物请求此刻才第一次被构造。
                        # 也就是说「产物分支不影响 TTFT」是**代码结构**保证的，
                        # 不是靠调度运气 —— 这是选 `after_first_token` 而不是 `immediate`
                        # 的全部理由（代价是产物晚 TTFT 才起步，约 3.9s）。
                        if parallel and work_task is None:
                            work_task = _fork()
                    reply_parts.append(delta)
                    yield "token", TokenEvent(delta=delta)
                    # 让出事件循环：否则 token 会在同一 tick 里全部塞进缓冲区，
                    # 前端拿到的仍是一整块，流式失去意义
                    await asyncio.sleep(0)
            except AppError as exc:
                yield "error", ErrorEvent(
                    code=exc.code.value,
                    message=exc.message,
                    stage="generate",
                    retryable=True,
                    attempt=1,
                )
            reply = "".join(reply_parts)
            knowledge_scope = self._scope_for(result)
            if ttft_ms is None:
                ttft_ms = int((time.perf_counter() - started) * 1000)

            # ---- 4. 产物：join + 按序释放 ----
            artifact_event: ArtifactEvent | None = None
            if wants_artifact:
                if work_task is None:
                    # 串行路径：并行开关关闭，或正文一个 token 都没产出（fork 点没被触发）。
                    # 走与改动前同名同形态的生成器，回退语义最直观，也保住脚本的 MC-3 埋点。
                    stream = (
                        self._create_artifact_stream(req, decision, context, result)
                        if action == "create_artifact"
                        else self._patch_artifact_stream(req, decision, context)
                    )
                    async for event in stream:
                        yield event
                        if isinstance(event[1], ArtifactEvent):
                            artifact_event = event[1]
                else:
                    work = await self._join_artifact_work(
                        work_task, req, decision, context, action, chunks=result.chunks
                    )
                    # interim 的释放点固定在「正文结束之后、commit 之前」：与串行版逐条一致
                    # （串行版里这些 error 本来就发生在正文之后），事件内容/相对顺序/相对
                    # citations 的位置都不变，只有绝对到达时刻推迟 —— 前端对 error 只做
                    # `setError`，不 abort，所以没有用户可见差异。
                    for event in work.interim:
                        yield event
                    for event in await self._commit_artifact(work, req, context):
                        yield event
                        if isinstance(event[1], ArtifactEvent):
                            artifact_event = event[1]

            # ---- 5. 引用汇总（含合法性后校验）----
            citations = self._build_citations(result)
            citations = self._drop_invalid_citations(reply, citations, result)
            yield "citations", CitationsEvent(citations=citations)

            # ---- 6. 落库 + 结束 ----
            duration_ms = int((time.perf_counter() - started) * 1000)
            await self._persist_turn(
                req, reply, knowledge_scope, citations, artifact_event, demo_mode
            )
            log_event(
                "turn_done",
                workspace_id=req.workspace_id,
                duration_ms=duration_ms,
                ttft_ms=ttft_ms,
                grader_status=result.grader_status,
                artifact_version=artifact_event.version if artifact_event else None,
                # 把生效档位与 fork 模式一起记下来：将来在目标环境复测 T1 / 排查
                # 「为什么这轮慢了」时，不必去翻当时的 .env
                artifact_model=self._settings.artifact_model_for(decision.artifact_type),
                artifact_fork_mode=self._settings.ARTIFACT_FORK_MODE,
                artifact_parallel=parallel,
            )
            yield "turn_end", TurnEndEvent(
                turn_id=req.turn_id,
                duration_ms=duration_ms,
                ttft_ms=ttft_ms,
                version=artifact_event.version if artifact_event else None,
                demo_mode=demo_mode,
            )
        finally:
            # 断连/异常时取消未完成的产物分支：httpx 请求随之取消（CancelledError 是
            # BaseException，不会被 `_post_json` / `complete_json` 的 `except Exception` 吞掉），
            # 分支 B 无副作用退出 —— 所以「断连不产生孤儿产物」是结构性的（设计 §5.2）。
            #
            # 两个易踩的坑（§5.3）：
            # 1. async generator 的 finally **可以 await，但不能 yield**（否则 RuntimeError:
            #    async generator ignored GeneratorExit）；
            # 2. 这里只能 suppress CancelledError，**不得**改成 `except Exception` ——
            #    CancelledError 不是 Exception，改了会让「子任务被取消」静默失效、断连后
            #    继续烧 token。这条是方案成立的前提，不是实现细节。
            if work_task is not None and not work_task.done():
                work_task.cancel()
                work_task.add_done_callback(_reap_task)
                with contextlib.suppress(asyncio.CancelledError):
                    await work_task

    # ------------------------------------------------------------------ #
    # 各阶段实现
    # ------------------------------------------------------------------ #
    @staticmethod
    def _scope_for(result: RetrievalResult) -> str:
        """把 Grader 三态映射到 knowledge_scope 枚举（与 §5.1 的 Literal 一致）。

        注意不能直接塞 grader_status：`PARTIAL_OR_BEYOND` 不是合法的 scope 值，
        前端按 scope 决定「超纲区」的视觉样式，值错就渲染不出区分。
        """
        if result.grader_status == NO_MATCH:
            return "none"
        if result.grader_status == SYLLABUS_MATCH:
            return "syllabus_strict"
        return "hybrid_extended"

    async def _retrieve(self, req: TurnRequest) -> RetrievalResult:
        anchor = req.anchor
        return await self._retriever.retrieve(
            req.workspace_id,
            req.query,
            section_code=anchor.section_code if anchor else None,
            page=anchor.page if anchor else None,
            # 带锚点的追问锁死在该小节内检索（§6.2）
            strict_section=bool(anchor and anchor.section_code),
            # 教材作用域：目录树点中某本书的某一节时，只在该书内检索。
            # `None` 时行为与加参数前完全一致（跨全部教材），不改变既有语义。
            document_id=req.document_id,
        )

    def _decide_action(
        self, req: TurnRequest, result: RetrievalResult, decision: RouteDecision
    ) -> str:
        if req.is_patch_turn:
            # 有锚点 = 用户点着某个节点追问，走增量 patch（P0-10）
            return "patch_artifact"
        if result.grader_status == NO_MATCH:
            # 没有课本依据就不生成产物，避免凭空造内容（P0-05④）
            return "none"
        if decision.is_none:
            return "none"
        return "create_artifact"

    def _build_context(self, req: TurnRequest, result: RetrievalResult) -> MockContext:
        texts = [chunk.content or chunk.snippet for chunk in result.chunks]
        pages = [chunk.page_start for chunk in result.chunks]
        section_code = req.anchor.section_code if req.anchor and req.anchor.section_code else None
        if section_code is None:
            section_code = result.chunks[0].section_code if result.chunks else ""
        section_title = result.chunks[0].section_title if result.chunks else ""
        return build_context(
            query=req.query,
            section_code=section_code or "",
            section_title=section_title,
            texts=texts,
            pages=pages,
            # 检索命中的**全部**页码（含切片区间），用于页码钳制与契约示例取值。
            # 注意与 `pages`（每段正文的起始页）区分：前者是「可引用范围」，
            # 后者是「这条事实来自哪一页」。
            allowed_pages=sorted(result.pages),
        )

    async def _reply_stream(
        self,
        req: TurnRequest,
        result: RetrievalResult,
        decision: RouteDecision,
        context: MockContext,
    ) -> AsyncIterator[str]:
        """正文流。演示模式走本地模板（毫秒级），有 Key 时走模型流式。"""
        if not self._llm.enabled:
            reply, _ = self.compose_reply(req, result, decision, context)
            for index in range(0, len(reply), TOKEN_CHUNK_SIZE):
                yield reply[index : index + TOKEN_CHUNK_SIZE]
            return

        prompt = self._reply_prompt(req, result, decision)
        try:
            async for delta in self._llm.stream_text(system=_SYSTEM_PROMPT, user=prompt):
                yield delta
        except AppError:
            # 模型不可用就降级到模板，保证「不白屏」（PRD §5.3）
            reply, _ = self.compose_reply(req, result, decision, context)
            for index in range(0, len(reply), TOKEN_CHUNK_SIZE):
                yield reply[index : index + TOKEN_CHUNK_SIZE]

    def compose_reply(
        self,
        req: TurnRequest,
        result: RetrievalResult,
        decision: RouteDecision,
        context: MockContext,
    ) -> tuple[str, str]:
        """确定性正文。返回 `(markdown, knowledge_scope)`。

        三种形态严格对应 §7.3 的三态：
        - SYLLABUS_MATCH → 每条事实带 [p.N]
        - PARTIAL_OR_BEYOND → 双层（课本依据 + 引用块包裹的深度拓展）
        - NO_MATCH → 明确说未覆盖，**不出现任何页码**
        """
        facts = self._fact_lines(result)
        topic = context.topic

        if result.grader_status == NO_MATCH:
            extended = self._extended_text(req, context)
            return (
                f"该问题课本未覆盖：**{topic}** 在已上传教材的切片中没有足够依据。\n\n"
                f"> **深度拓展（超出课本范围）**\n> {extended}",
                "none",
            )

        if not facts:
            # 有命中但抽不出完整句子（极短切片）：给结论 + 引用，不硬凑事实
            page = result.chunks[0].page_start if result.chunks else 1
            body = f"- 相关内容见课本 p.{page}，建议结合上下文精读。"
            return f"【课本依据】{topic}\n\n{body}", "syllabus_strict"

        lines = "\n".join(f"- {text} [p.{page}]" for text, page in facts)
        suggestion = f"\n\n**建议形态**：{decision.reason}"

        if result.grader_status == SYLLABUS_MATCH:
            return f"【课本依据】{topic}\n\n{lines}{suggestion}", "syllabus_strict"

        # PARTIAL_OR_BEYOND：双层结构，拓展部分用引用块 + 显式声明，防止考试答偏
        extended = self._extended_text(req, context)
        return (
            f"含超纲内容。\n\n【课本依据】{topic}\n\n{lines}\n\n"
            f"> **深度拓展（超出课本范围）**\n> {extended}{suggestion}",
            "hybrid_extended",
        )

    def _fact_lines(self, result: RetrievalResult) -> list[tuple[str, int]]:
        """把检索片段拆成「一句事实 + 其页码」。页码取 chunk.page_start，保证可回溯。"""
        lines: list[tuple[str, int]] = []
        for chunk in result.chunks:
            for sentence in _sentences(chunk.content or chunk.snippet):
                if len(sentence) < 12:
                    continue
                lines.append((_clip(sentence, 96), chunk.page_start))
                break  # 每个 chunk 只取首句，避免同一段反复刷屏
            if len(lines) >= MAX_REPLY_FACTS:
                break
        return lines

    def _extended_text(self, req: TurnRequest, context: MockContext) -> str:
        """拓展段落：只谈方法与延伸方向，不给页码（P0-05④）。"""
        return _clip(
            f"关于「{_clip(req.query, 24)}」，课本之外的延伸可以从三个角度展开："
            f"一是把「{context.topic}」放进更大的时间脉络里比较前后的制度差异；"
            "二是关注各方在谈判中的实际得失，而不只看条文措辞；"
            "三是查阅同时期的报刊与回忆录，观察不同立场如何叙述同一事件。"
            "以上内容属于拓展知识，考试作答请以课本页码为准。",
            320,
        )

    # ------------------------------------------------------------------ #
    # 产物分支：分支 B（可并行、无副作用）与主路径提交
    # ------------------------------------------------------------------ #
    @contextlib.asynccontextmanager
    async def _artifact_slot(self) -> AsyncIterator[None]:
        """进程级产物并发安全阀（`ARTIFACT_MAX_CONCURRENCY`，`0` = 不限）。

        惰性创建而不是在 `__init__` 里建：`AgentEngine` 是进程级单例，测试会跨多个事件循环
        复用它，提前建 `asyncio.Semaphore` 在旧版本上会绑错 loop。默认 8 时单用户永不阻塞，
        只有「8 个标签页同时提问」才排队 —— 真正的 429 应对是串行降级，不是排队
        （排队会让产物一直等到正文结束，等价于偷偷退化成串行，且日志里看不到原因）。
        """
        limit = self._settings.ARTIFACT_MAX_CONCURRENCY
        if limit <= 0:
            yield
            return
        if self._artifact_semaphore is None:
            self._artifact_semaphore = asyncio.Semaphore(limit)
        async with self._artifact_semaphore:
            yield

    async def _artifact_work(
        self,
        req: TurnRequest,
        decision: RouteDecision,
        context: MockContext,
        action: str,
        *,
        defer_on_rate_limit: bool,
        chunks: Sequence[RetrievedChunk] = (),
    ) -> ArtifactWork:
        """分支 B：把产物的**生成**部分一次跑完，返回纯数据。

        这里不持有 session、不发事件、不写库（见 `ArtifactWork` 的 docstring：保证是
        「不写」而不是「不能写」，`self._session_factory` 在语言层面仍然够得着）。
        `defer_on_rate_limit=True` 表示「正文还在流」，遇 429 立刻放弃并交回主路径 ——
        此刻重试只会拖长用户等待；主路径会在正文结束后用 `defer_on_rate_limit=False`
        再调一次，那时系统里只剩这一个请求，成功率最高。

        `chunks` 原样透传给 create 分支的生成器，供后置页码重绑定使用
        （patch 分支不需要：它不生成 spec，只生成 ops）。
        """
        kind: Literal["create", "patch"] = (
            "create" if action == "create_artifact" else "patch"
        )
        try:
            async with self._artifact_slot():
                if kind == "create":
                    return await self._work_create(req, decision, context, chunks)
                return await self._work_patch(req, context)
        except RateLimitedError as exc:
            log_event(
                "artifact_rate_limited",
                level=logging.WARNING,
                artifact_type=decision.artifact_type,
                deferred=defer_on_rate_limit,
            )
            if defer_on_rate_limit:
                return ArtifactWork(kind=kind, rate_limited=True)
            return self._rate_limit_fallback(kind, req, decision, context, exc)

    async def _work_create(
        self,
        req: TurnRequest,
        decision: RouteDecision,
        context: MockContext,
        chunks: Sequence[RetrievedChunk] = (),
    ) -> ArtifactWork:
        """create 分支的生成段。`_drain_generation` 会把中间事件收集起来，异常照抛。"""
        spec, interim = await _drain_generation(
            self._generate_spec_stream(req, decision, context, chunks)
        )
        return ArtifactWork(kind="create", spec=spec, interim=interim)

    async def _work_patch(self, req: TurnRequest, context: MockContext) -> ArtifactWork:
        """patch 分支的生成段：**只读**基线 + 生成 ops，不 apply、不落库。

        读基线失败（产物已被另一个标签页删掉是真实竞态）也在这里就地转成 error 事件，
        而不是抛异常：分支 B 抛出去会被 `_join_artifact_work` 当「未预期异常」，
        错误码就变成 INTERNAL_ERROR 了 —— 而这是可预期的 404。
        """
        assert req.anchor is not None  # 由 _decide_action 保证
        try:
            async with self._session_factory() as session:
                artifact = await repository.require_artifact(session, req.anchor.artifact_id)
                # deepcopy：这里拿到的是基线快照，不能与 ORM 对象共享嵌套结构，
                # 否则后续任何原地修改都可能顺着 identity map 影响历史版本行（P0-11③）
                spec_dict = copy.deepcopy(artifact.spec_json or {})
                base_version = artifact.version
                artifact_type = artifact.artifact_type
        except AppError as exc:
            # 就地标 stage="patch"：冒泡到 chat_stream 的兜底 except 只会硬编码成
            # "generate"，前端提示与日志都会指向「正文生成」这个根本没出错的环节。
            return ArtifactWork(
                kind="patch",
                interim=[
                    (
                        "error",
                        ErrorEvent(
                            code=exc.code.value,
                            message=exc.message,
                            stage="patch",
                            retryable=False,
                            attempt=1,
                        ),
                    )
                ],
            )

        if not self._llm.enabled:
            ops = generate_patch_ops(
                spec_dict, node_path=req.anchor.node_path, context=context
            )
            return ArtifactWork(
                kind="patch",
                spec_dict=spec_dict,
                base_version=base_version,
                artifact_type=artifact_type,
                ops=ops,
            )

        ops, interim = await _drain_generation(
            self._generate_ops_stream(
                req, spec_dict, context, artifact_type=artifact_type
            )
        )
        return ArtifactWork(
            kind="patch",
            spec_dict=spec_dict,
            base_version=base_version,
            artifact_type=artifact_type,
            ops=ops,  # type: ignore[arg-type]
            interim=interim,
        )

    def _rate_limit_fallback(
        self,
        kind: Literal["create", "patch"],
        req: TurnRequest,
        decision: RouteDecision,
        context: MockContext,
        exc: RateLimitedError,
    ) -> ArtifactWork:
        """429 串行重试也失败 → 降级 mock，并把 error 按序交给主路径。

        两条 error 的分工是**测量契约**（设计 §3.3.4 MC-1 / MC-4），不是文案偏好：
        - `stage="generate"` + `retryable=True`：这是上游限流，不是契约校验失败。
          若标成 `"validate"`，`flash_quality_check` 的 `validate_errors` 会虚增，
          G1 retry 率算错；
        - `stage="validate"` + `retryable=False` + 文案含「降级」：G2 降级率就靠这三个
          特征识别。改文案或改 stage 会让 G2 **静默变成 0%**（看起来全绿，实际没测到）。
        """
        if kind == "create":
            spec = generate_spec(decision.artifact_type, context)
            return ArtifactWork(
                kind="create",
                spec=spec,
                interim=[
                    (
                        "error",
                        ErrorEvent(
                            code=ErrorCode.LLM_UPSTREAM_ERROR.value,
                            message=f"产物生成被上游限流：{exc.message}",
                            stage="generate",
                            retryable=True,
                            attempt=1,
                        ),
                    ),
                    (
                        "error",
                        ErrorEvent(
                            code=ErrorCode.CONTRACT_VALIDATION_FAILED.value,
                            message="上游限流重试仍失败，已降级为演示生成器",
                            stage="validate",
                            retryable=False,
                            attempt=3,
                        ),
                    ),
                ],
            )
        # patch：拿不到基线（异常发生在 `_work_patch` 内部），因此只报限流、不产出 ops。
        # 这里**不发**「降级」那条 validate error：patch 路径本来就没有降级语义，
        # 硬加一条会让 G2 把「限流」也算成「降级 mock」，污染分母。
        return ArtifactWork(
            kind="patch",
            interim=[
                (
                    "error",
                    ErrorEvent(
                        code=ErrorCode.LLM_UPSTREAM_ERROR.value,
                        message=f"补丁生成被上游限流：{exc.message}",
                        stage="generate",
                        retryable=True,
                        attempt=1,
                    ),
                )
            ],
        )

    async def _join_artifact_work(
        self,
        task: "asyncio.Task[ArtifactWork]",
        req: TurnRequest,
        decision: RouteDecision,
        context: MockContext,
        action: str,
        chunks: Sequence[RetrievedChunk] = (),
    ) -> ArtifactWork:
        """等分支 B 结束，并把 429 转成「正文结束后串行重试」。

        捕获顺序**不能调换**：`CancelledError` 是 `BaseException`，`Exception` 兜住其余一切 ——
        分支 B 的任何意外都不许打断主路径（正文已经流给用户了）。

        注意这里**没有** `except RateLimitedError`：429 在 `_post_json` 层就被转成
        `RateLimitedError`，而 `_artifact_work` 已把它接住并返回
        `ArtifactWork(rate_limited=True)`（defer 时）或直接走降级（非 defer 时），
        所以这个 task **永远不会**以 429 结束。别再加一个「看着像在处理 429」的分支：
        看着有效的死代码比没有更危险，下一个人会以为 429 在这里被处理了。
        """
        kind: Literal["create", "patch"] = (
            "create" if action == "create_artifact" else "patch"
        )
        try:
            work = await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底：不许打断主路径
            log_event(
                "artifact_work_unhandled",
                level=logging.ERROR,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
            work = ArtifactWork(
                kind=kind,
                interim=[
                    (
                        "error",
                        ErrorEvent(
                            code=ErrorCode.INTERNAL_ERROR.value,
                            message="产物生成出现未预期错误，本轮未产出产物",
                            stage="generate",
                            retryable=False,
                            attempt=1,
                        ),
                    )
                ],
            )

        for _ in range(self._settings.ARTIFACT_RATE_LIMIT_RETRY):
            if not work.rate_limited:
                break
            work = await self._artifact_work(
                req,
                decision,
                context,
                action,
                defer_on_rate_limit=False,
                chunks=chunks,
            )
        if work.rate_limited:
            # **这条不是死代码**：`ARTIFACT_RATE_LIMIT_RETRY=0`（关闭重试）时循环一次都不跑，
            # `work` 仍是 defer 返回的 `rate_limited=True`，必须在这里转降级，
            # 否则本轮既没有产物也没有 error 事件（画布白屏）。测试 D4 覆盖这个分支。
            work = self._rate_limit_fallback(
                kind, req, decision, context, RateLimitedError("串行重试已用尽")
            )
        return work

    async def _commit_artifact(
        self, work: ArtifactWork, req: TurnRequest, context: MockContext
    ) -> list[Event]:
        """主路径唯一的落库 + 事件构造点。返回按序要下发的事件。

        返回 list 而不是 yield：`stream_turn` 需要在 `yield` 之后继续记录
        `artifact_event`（用于 turn_end 的 version），list 让这个顺序在调用方一目了然。
        """
        if work.kind == "create":
            return await self._commit_create(work, req, context)
        return await self._commit_patch(work, req)

    async def _commit_create(
        self, work: ArtifactWork, req: TurnRequest, context: MockContext
    ) -> list[Event]:
        spec = work.spec
        if spec is None:
            return []

        title = _clip(context.topic, 60) or "未命名产物"
        try:
            async with self._session_factory() as session:
                artifact = await repository.create_artifact(
                    session,
                    workspace_id=req.workspace_id,
                    title=title,
                    artifact_type=str(spec.get("type")),
                    spec_json=spec,
                    anchor_section=context.section_code or None,
                )
                await repository.append_artifact_version(
                    session, artifact_id=artifact.id, spec_json=spec, origin_turn_id=req.turn_id
                )
                await session.commit()
                artifact_id = artifact.id
        except AppError as exc:
            # 就地捕获，别让异常冒泡到 chat_stream 的兜底 `except AppError`：
            # 那里既标不准 stage，又会连带丢掉后面的 citations（正文已经流给用户了，
            # 出处清单不该因为产物落库失败而消失）。§5.4 规则② 要求 error 之后仍补发 turn_end，
            # 这里返回空列表只是结束本阶段，外层会照常走 citations → turn_end。
            return [
                (
                    "error",
                    ErrorEvent(
                        code=exc.code.value,
                        message=exc.message,
                        stage="generate",
                        retryable=False,
                        attempt=1,
                    ),
                )
            ]

        return [
            (
                "artifact",
                ArtifactEvent(
                    action="create",
                    artifact_id=artifact_id,
                    artifact_type=str(spec.get("type")),  # type: ignore[arg-type]
                    version=1,
                    title=title,
                    spec=spec,  # type: ignore[arg-type]
                ),
            )
        ]

    async def _create_artifact_stream(
        self,
        req: TurnRequest,
        decision: RouteDecision,
        context: MockContext,
        result: RetrievalResult,
    ) -> AsyncIterator[Event]:
        """串行路径与脚本埋点的入口。

        保留方法名与 async generator 形态是**测量约定**（设计 §3.3.4 MC-3）：
        `scripts/profile_latency.py` 按名 `getattr` 给它打 span，改成普通函数或改名
        会让阶段表整行消失 —— 而且不会报错。
        """
        work = await self._artifact_work(
            req,
            decision,
            context,
            "create_artifact",
            defer_on_rate_limit=False,
            chunks=result.chunks,
        )
        for event in work.interim:
            yield event
        for event in await self._commit_artifact(work, req, context):
            yield event

    async def _generate_spec_stream(
        self,
        req: TurnRequest,
        decision: RouteDecision,
        context: MockContext,
        chunks: Sequence[RetrievedChunk] = (),
    ) -> AsyncIterator[Event]:
        """产出 ("error"|"spec", payload)。LLM 路径带校验重试，全部失败降级 mock。

        `chunks` 是本次检索命中的切片（`result.chunks`），只用于**后置页码重绑定**：
        `source_ref` 的页码必须能落到某个真实切片上，而模型会因复制惯性把它写错。
        缺省为空元组 —— 此时不重绑定（没有切片就没有「真实页码」可绑），
        既保证历史调用点与测试无需改动，也避免「凭空造页码」。
        """
        # 历史注释曾写「若需压总耗时，方向是分批下发或换更快档位」——
        # 两件都已了结：换档已落地（IMPL-NOTES E12，55.5s→10.1s），
        # 分批下发已被实测否掉（PRD §3），**不要再往那个方向做**。
        if not self._llm.enabled:
            spec = generate_spec(decision.artifact_type, context)
            try:
                parse_artifact_spec(spec)
            except ValidationError as exc:
                yield "error", ErrorEvent(
                    code=ErrorCode.CONTRACT_VALIDATION_FAILED.value,
                    message=f"生成结果未通过契约校验：{exc.errors()[:1]}",
                    stage="validate",
                    retryable=True,
                    attempt=1,
                )
                return
            yield "spec", spec
            return

        prompt = self._artifact_prompt(req, decision, context)
        feedback = ""
        model = self._settings.artifact_model_for(decision.artifact_type)
        for attempt in range(1, 4):
            log_event(
                "artifact_generate",
                artifact_type=decision.artifact_type,
                model=model,
                attempt=attempt,
            )
            try:
                payload = await self._llm.complete_json(
                    system=_ARTIFACT_SYSTEM_PROMPT,
                    user=f"{prompt}{feedback}",
                    # 按形态取档位（分形态回退能力在这里生效）：不传的话只能拿到
                    # `complete_json` 的全局默认档，`LLM_MODEL_ARTIFACT_BY_TYPE` 就成了
                    # 「配了却没生效」的摆设（IMPL-NOTES E11 教训 1）。
                    model=model,
                )
                candidate = payload.get("spec", payload)
                parse_artifact_spec(candidate)
                # 结构合法 ≠ 内容够用：`min_length=1` 放行「1 页残页课件」，
                # 数量约束只能在这里补（见 `_validate_spec_cardinality` 的 docstring）。
                # 抛 ValueError 会被下面的 except 接住，复用「重试 3 次 → 降级 mock」。
                _validate_spec_cardinality(candidate, decision.artifact_type)
                # 顺序不可调换：**校验 → 数量 → 重绑定 → 钳制**。
                # 重绑定放在钳制之前，是因为它要把「假页码」换成「真页码」，而钳制只会
                # 把越界页码剥成章节号 —— 先钳制就没东西可绑了。钳制留在最后当兜底，
                # 确保重绑定万一引入越界页码也会被剥掉。
                # 重绑定是确定性的（实词重叠），不抛异常、不改字段类型，所以不会触发重试。
                # 开关默认开启；关掉后退回「只钳制不重绑定」的旧行为（见 config 里的说明）。
                if self._settings.CITATION_REBIND_ENABLED:
                    candidate, rebound = _rebind_spec_source_pages(
                        candidate, chunks, section_code=context.section_code
                    )
                    if rebound:
                        log_event(
                            "citation_rebound",
                            count=rebound,
                            artifact_type=decision.artifact_type,
                        )
                # 契约已通过，再做一次**确定性**页码钳制：把落在检索命中页之外的
                # 页码剥掉。放在校验之后、且不改变字段类型，所以不会触发重试
                # （见 `_clamp_spec_pages` 的 docstring：编造页码重试三次还是编）。
                candidate, clamped_pages = _clamp_spec_pages(
                    candidate,
                    frozenset(context.allowed_pages),
                    fallback_ref=context.section_code,
                )
                if clamped_pages:
                    log_event(
                        "page_clamped",
                        level=logging.WARNING,
                        count=clamped_pages,
                        artifact_type=decision.artifact_type,
                    )
            except RateLimitedError:
                # 429 不是契约失败：**必须原样上抛**，让主路径在正文结束后串行重试。
                # 若被下面的通用 except 吞掉，会变成「重试 3 次 + 每次都被限流」，
                # 既慢又烧配额，且错误归因会错标成 stage="validate"（污染 G1）。
                raise
            except (ValidationError, AppError, ValueError) as exc:
                log_event(
                    "contract_retry",
                    level=logging.WARNING,
                    attempt=attempt,
                    artifact_type=decision.artifact_type,
                    error=str(exc)[:200],
                )
                yield "error", ErrorEvent(
                    code=ErrorCode.CONTRACT_VALIDATION_FAILED.value,
                    message=f"生成结果未通过契约校验，已自动重试（{attempt}/3）",
                    stage="validate",
                    retryable=True,
                    attempt=attempt,
                )
                feedback = (
                    f"\n\n上一次输出未通过校验：{str(exc)[:400]}。"
                    "请修正后只输出一个完整 JSON 对象。"
                )
                continue
            yield "spec", candidate
            return

        # 三次都失败：降级 mock，保证画布不白屏（§10 第 1 条）
        spec = generate_spec(decision.artifact_type, context)
        yield "error", ErrorEvent(
            code=ErrorCode.CONTRACT_VALIDATION_FAILED.value,
            message="模型输出连续未通过校验，已降级为演示生成器",
            stage="validate",
            retryable=False,
            attempt=3,
        )
        yield "spec", spec

    async def _patch_artifact_stream(
        self, req: TurnRequest, decision: RouteDecision, context: MockContext
    ) -> AsyncIterator[Event]:
        """串行路径与脚本埋点的入口（形态与 create 同理，见其 docstring）。"""
        work = await self._artifact_work(
            req, decision, context, "patch_artifact", defer_on_rate_limit=False
        )
        for event in work.interim:
            yield event
        for event in await self._commit_artifact(work, req, context):
            yield event

    async def _commit_patch(self, work: ArtifactWork, req: TurnRequest) -> list[Event]:
        """patch 的落库段：apply_patch_request + 乐观锁写入 + 事件。

        `apply_patch_request` 与写库必须留在主路径（不能在分支 B）：它读的是基线快照，
        而版本号冲突的判定要基于「写入那一刻」的版本 —— 放进分支 B 会让乐观锁窗口
        多出整个正文时长。
        """
        assert req.anchor is not None  # 由 _decide_action 保证
        spec_dict = work.spec_dict
        ops = work.ops
        base_version = work.base_version
        artifact_type = work.artifact_type or ""
        if spec_dict is None or not ops or base_version is None:
            # 基线读失败 / 没有 ops / 限流放弃：`interim` 里已有对应的 error，这里什么都不发
            return []

        try:
            outcome = apply_patch_request(
                spec_dict,
                PatchRequest(
                    artifact_id=req.anchor.artifact_id, base_version=base_version, ops=ops
                ),
                current_version=base_version,
            )
        except AppError as exc:
            return [
                (
                    "error",
                    ErrorEvent(
                        code=exc.code.value,
                        message=exc.message,
                        stage="patch",
                        retryable=False,
                        attempt=1,
                    ),
                )
            ]

        try:
            async with self._session_factory() as session:
                artifact = await repository.require_artifact(session, req.anchor.artifact_id)
                await repository.apply_artifact_patch(
                    session,
                    artifact=artifact,
                    spec_json=outcome.spec,  # 必须用保持键序的 spec，不能用 model 重新 dump
                    patch_ops=[op.model_dump(mode="json") for op in ops],
                    origin_turn_id=req.turn_id,
                )
                await session.commit()
        except AppError as exc:
            # 写入失败（并发 patch 撞版本号 → PATCH_BASE_VERSION_CONFLICT、或产物被删）。
            # 同样必须就地标 stage="patch"：这是「补丁落库」环节，不是正文生成。
            return [
                (
                    "error",
                    ErrorEvent(
                        code=exc.code.value,
                        message=exc.message,
                        stage="patch",
                        retryable=False,
                        attempt=1,
                    ),
                )
            ]

        log_event(
            "patch_applied",
            workspace_id=req.workspace_id,
            artifact_id=req.anchor.artifact_id,
            version=outcome.new_version,
            changed_paths=list(outcome.changed_paths),
        )
        return [
            (
                "artifact",
                ArtifactEvent(
                    action="patch",
                    artifact_id=req.anchor.artifact_id,
                    artifact_type=artifact_type,  # type: ignore[arg-type]
                    version=outcome.new_version or base_version + 1,
                    base_version=base_version,
                    ops=ops,
                ),
            )
        ]

    async def _generate_ops_stream(
        self,
        req: TurnRequest,
        spec_dict: dict[str, Any],
        context: MockContext,
        *,
        artifact_type: str | None = None,
    ) -> AsyncIterator[Event]:
        """产出 ("error"|"ops", payload)。与 create 路径共用同一套档位与页码钳制。"""
        assert req.anchor is not None
        prompt = (
            f"当前 AST（JSON）：\n{spec_dict}\n\n"
            f"用户点击的节点路径：{req.anchor.node_path}\n"
            f"用户追问：{req.query}\n\n"
            "只输出 {\"ops\": [...]}，每个 op 形如 "
            '{"op":"update_node","node_path":"/slides/2","payload":{"title":"..."},"reason":"..."}。'
            "禁止整树重写，只改该节点自身的字段。"
        )
        feedback = ""
        model = self._settings.artifact_model_for(artifact_type)
        for attempt in range(1, 3):
            log_event(
                "artifact_generate",
                artifact_type=artifact_type or "",
                model=model,
                attempt=attempt,
                stage="patch",
            )
            try:
                payload = await self._llm.complete_json(
                    system=_ARTIFACT_SYSTEM_PROMPT,
                    user=f"{prompt}{feedback}",
                    model=model,
                )
                raw_ops = payload.get("ops", [])
                ops = [PatchOp.model_validate(item) for item in raw_ops]
                if not ops:
                    raise ValueError("模型没有返回任何 op")
                # 与 create 路径共用同一个钳制函数（架构裁定：无例外）。必须在
                # `yield "ops"` 之前完成 —— 否则落库/下发的 ops 与前端本地应用的不是
                # 同一份内容，前端指纹自检会报「未声明的改动」。
                ops, clamped_pages = _clamp_patch_ops(
                    ops,
                    frozenset(context.allowed_pages),
                    fallback_ref=context.section_code,
                )
                if clamped_pages:
                    log_event(
                        "page_clamped",
                        level=logging.WARNING,
                        count=clamped_pages,
                        stage="patch",
                    )
            except RateLimitedError:
                # 与 create 路径同理：429 不是契约失败，原样上抛交给主路径串行重试
                raise
            except (ValidationError, AppError, ValueError) as exc:
                yield "error", ErrorEvent(
                    code=ErrorCode.CONTRACT_VALIDATION_FAILED.value,
                    message=f"补丁未通过校验，已自动重试（{attempt}/2）",
                    stage="patch",
                    retryable=True,
                    attempt=attempt,
                )
                feedback = f"\n\n上一次输出未通过校验：{str(exc)[:400]}"
                continue
            yield "ops", ops
            return
        yield "ops", generate_patch_ops(spec_dict, node_path=req.anchor.node_path, context=context)

    # ------------------------------------------------------------------ #
    # 引用与持久化
    # ------------------------------------------------------------------ #
    def _build_citations(self, result: RetrievalResult) -> list[Citation]:
        """引用列表按**页码升序**输出。

        为什么不用检索得分序：`retrieval.chunks` 是「相关性排序」（该按分数），而
        `citations` 是「出处清单」（该按书页顺序）。同一份回答的引用如果按分数排，
        用户会看到 p.76 排在 p.71 前面，读起来像错乱；按页码排则天然可扫读、可打印。
        前端即使再排一次也是幂等的（同一排序键），不会互相打架。

        **NO_MATCH 时必须返回空列表**（P0-05④）：Grader 判「课本未覆盖」时，候选片段
        只是低分兜底数据，一旦进了 `citations`，前端 `CitationList` 就会把「p.1-4」
        画在用户脸上 —— 正文说「课本未覆盖」而右侧列着页码，这是最自相矛盾的一种展示，
        也正是 PRD 明令禁止的「编造页码」观感。候选片段仍保留在 `retrieval` 事件里供
        调试，但不进用户可见字段。
        """
        if result.grader_status == NO_MATCH:
            return []
        citations: list[Citation] = []
        seen: set[tuple[str, int, int]] = set()
        for chunk in result.chunks:
            key = (chunk.section_code, chunk.page_start, chunk.page_end)
            if key in seen:
                continue
            seen.add(key)
            citations.append(
                Citation(
                    label=f"{chunk.section_title or chunk.section_code}（p.{chunk.page_start}-{chunk.page_end}）",
                    section_code=chunk.section_code,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                )
            )
        citations.sort(key=lambda citation: (citation.page_start, citation.page_end, citation.section_code))
        return citations

    def _drop_invalid_citations(
        self, reply: str, citations: Sequence[Citation], result: RetrievalResult
    ) -> list[Citation]:
        """§7.3 引用合法性后校验：正文里出现但检索集里没有的页码 → 记 WARN 并剔除引用。

        范围引用（`[p.74-75]`）**两个端点都必须落在检索切片的页码区间内**才算合法；
        只要有一个端点越界（如 `[p.76-1000]`），整条引用即视为编造，不做「部分合法」的宽容 ——
        模型编页码时最常见的形态恰恰是「真页码 + 一个凑数的尾巴」，放行半个就等于放行编造。
        合法性按 chunk 的 `page_start..page_end` 整段展开后判定（`RetrievalResult.pages`），
        所以 `[p.77]` 落在 `p.76-77` 切片内是合法的，不会被误判。

        不篡改正文：流式内容用户已经看到了，事后改写会造成「界面闪变」。
        """
        allowed_pages = result.pages
        cited_pages: set[int] = set()
        for start, end in _PAGE_CITATION_RE.findall(reply):
            cited_pages.add(int(start))
            if end:
                cited_pages.add(int(end))
        invalid = {page for page in cited_pages if page not in allowed_pages}
        if invalid:
            log_event(
                "invalid_citation",
                level=logging.WARNING,
                invalid_pages=sorted(invalid),
                allowed=sorted(allowed_pages),
            )
        kept: list[Citation] = []
        for citation in citations:
            if invalid and citation.page_start in invalid:
                continue
            kept.append(citation)
        return kept

    async def _persist_turn(
        self,
        req: TurnRequest,
        reply: str,
        knowledge_scope: str,
        citations: Sequence[Citation],
        artifact_event: ArtifactEvent | None,
        demo_mode: bool,
    ) -> None:
        """落 user + assistant 两条消息（P0-01② 刷新恢复依赖它）。"""
        anchor_payload = req.anchor.model_dump(mode="json") if req.anchor else None
        try:
            async with self._session_factory() as session:
                await repository.require_workspace(session, req.workspace_id)
                await repository.add_message(
                    session,
                    workspace_id=req.workspace_id,
                    role="user",
                    content_markdown=req.query,
                    anchor=anchor_payload,
                )
                await repository.add_message(
                    session,
                    workspace_id=req.workspace_id,
                    role="assistant",
                    content_markdown=reply,
                    knowledge_scope=knowledge_scope,
                    citations=[citation.model_dump(mode="json") for citation in citations],
                    anchor=anchor_payload,
                    artifact_id=artifact_event.artifact_id if artifact_event else None,
                    artifact_version=artifact_event.version if artifact_event else None,
                    demo_mode=demo_mode,
                )
                await repository.touch_workspace(session, req.workspace_id)
                await session.commit()
        except AppError:
            # 工作区被删了（用户在对话中途删库）：消息不落库即可，不要让整轮对话失败
            log_event("turn_persist_skipped", level=logging.WARNING, workspace_id=req.workspace_id)

    # ------------------------------------------------------------------ #
    # 提示词
    # ------------------------------------------------------------------ #
    def _reply_prompt(
        self, req: TurnRequest, result: RetrievalResult, decision: RouteDecision
    ) -> str:
        blocks = [
            f"【课本切片 {index + 1}】{chunk.section_code} p.{chunk.page_start}-{chunk.page_end}\n{chunk.content or chunk.snippet}"
            for index, chunk in enumerate(result.chunks)
        ]
        anchor_text = (
            f"用户正在追问节点 {req.anchor.node_path}（{req.anchor.node_label or ''}）"
            if req.anchor
            else "用户在做全局提问"
        )
        return (
            "\n\n".join(blocks)
            + f"\n\n{anchor_text}。问题：{req.query}\n"
            f"评估结论：{result.grader_status}。请用中文回答，每条事实后标注 [p.页码]。"
        )

    def _artifact_prompt(
        self, req: TurnRequest, decision: RouteDecision, context: MockContext
    ) -> str:
        """构造产物生成提示词。

        **必须把目标形态的字段契约原文贴进来**（`_ARTIFACT_CONTRACT`）：
        只写「请输出符合 JSON Schema 的对象」而不给 Schema，模型只能靠猜字段名 ——
        实测它会输出 `title`/`section`/`heading` 这类自创字段，被 `extra="forbid"`
        逐条拒绝，重试时错误数还会越改越多（15→21→26），最终降级 mock。
        契约 + 一个最小合法示例是最省 token 的确定性约束方式。
        """
        contract = _ARTIFACT_CONTRACT.get(decision.artifact_type, _DEFAULT_ARTIFACT_CONTRACT)
        # 每条事实前面标出它的来源页：模型原先拿到的正文里**一个页码都没有**，
        # 只能省略页码或照抄契约示例，这正是 P0-05④ 在 4.1.2 之外小节全部失效的根因。
        facts = context.facts[:8]
        fact_pages = context.fact_pages
        fact_lines: list[str] = []
        for index, fact in enumerate(facts):
            page = fact_pages[index] if index < len(fact_pages) else None
            fact_lines.append(f"- [p.{page}] {fact}" if page else f"- {fact}")
        has_pages = any(
            fact_pages[index] for index in range(min(len(facts), len(fact_pages)))
        )
        # 页码说明分两种情况写：一条页码都没有时**必须明说**，否则模型会按契约里的
        # 格式提示（"4.1.3, p.71"）自行补一个数字出来 —— 那正是要杜绝的静默编造。
        #
        # 已试过并**回退**：在这里加过一条硬规则「每一页的页码必须对应它所依据的那条事实，
        # 不要所有页都写同一条事实的页码」。实测无效 —— 5 份课件「引用同一页码的条目占比」
        # 从 91.4% 变成 93.3%（去重页码均值 1.2），模型仍然一律取第一条事实的页码。
        # 别再原样加回去；真要修，方向是让 slide 与 fact 建立结构对应（例如按页分配事实），
        # 而不是继续在提示词里叮嘱。这是已知局限，已记进 IMPL-NOTES E12。
        page_rule = (
            "每条前面的方括号是该条事实的页码，source_ref / source_page 的页码只能取自这些数字；"
            "某条事实没有方括号就表示它没有可用页码，此时 source_ref 只写章节号、source_page 写 null）：\n"
            if has_pages
            else "本次检索没有给出任何页码，因此 source_ref 只写章节号（如 \"4.1.2\"）、"
            "source_page 一律写 null，不得自行填写页码）：\n"
        )
        return (
            f"目标形态：{decision.artifact_type}\n"
            f"章节：{context.section_code} {context.section_title}\n"
            f"用户诉求：{req.query}\n"
            "课本事实（只能依据以下内容生成，不得编造页码或事实；"
            + page_rule
            + "\n".join(fact_lines)
            + f"\n\n{contract}\n"
            "现在输出这一个 JSON 对象，不要任何解释文字、不要 Markdown 围栏。"
        )


_engine: AgentEngine | None = None


def get_agent_engine() -> AgentEngine:
    global _engine
    if _engine is None:
        _engine = AgentEngine()
    return _engine


def reset_agent_engine() -> None:
    """仅供测试。"""
    global _engine
    _engine = None


__all__ = ["AgentEngine", "Event", "TurnRequest", "get_agent_engine", "reset_agent_engine"]

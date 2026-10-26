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
import copy
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

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
from app.services.llm_client import LLMClient, get_llm_client
from app.services.mock_generator import MockContext, build_context, generate_patch_ops, generate_spec
from app.services.patch_engine import apply_patch_request
from app.services.retriever import (
    NO_MATCH,
    PARTIAL_OR_BEYOND,
    SYLLABUS_MATCH,
    RetrievalResult,
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
)

# 调色板拼成一行写进契约，避免模型编造 #FF0000 这类不在 §9.5 里的颜色被校验拒绝。
_PALETTE_TEXT = " ".join(PALETTE)

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
        '元素为 {"year":"1946","label":"...","detail":"...","source_page":76}\n'
        "- quiz_questions：数组或 null，元素为 "
        '{"question":"...","options":["选项一","选项二","选项三"],"answer_index":0,"explanation":"..."}'
        "（options 至少 2 项，answer_index 必须落在 options 下标范围内）\n"
        "- speaker_notes：字符串，教师口播讲解 2~3 句，要讲课本里的具体内容，"
        "不要写「这一节我们讲……」这类空话\n"
        '- source_ref：字符串，格式 "4.1.3, p.76"\n'
        "业务约束：共 5~8 页；第 1 页 layout 必须是 title_hero 且 title 用一句话概括本节主题"
        "（与 topic 一致即可）；最后 1 页 layout 必须是 quiz；"
        "中间各页在 bullet_points / timeline / comparison / cause_effect 里至少用 3 种不同取值；"
        "bullet_points 页给 3~5 条 bullets；"
        "timeline 页必须给 timeline_items 且至少 2 条，comparison 页必须给 comparison_left 与 "
        "comparison_right（各 2~4 条），quiz 页必须给 quiz_questions 且至少 2 题，"
        "其余页对应字段写 null。\n"
        "最小合法示例（1 页，字段齐全，照此结构扩到 5~8 页）：\n"
        '{"type":"ppt","topic":"马来亚联合邦的成立","slides":[{"node_id":"slide-1",'
        '"layout":"title_hero","title":"马来亚联合邦的成立","subtitle":"4.1 · p.76",'
        '"bullets":[],"comparison_left":null,"comparison_right":null,'
        '"timeline_items":null,"quiz_questions":null,'
        '"speaker_notes":"1946 年英国提出马来亚联合邦计划，把各州与槟城、马六甲合并，'
        '并削弱苏丹在州内的主权。","source_ref":"4.1.1, p.76"}]}'
    ),
    "anim_2d": (
        "【输出契约：anim_2d】\n"
        '顶层：{"type":"anim_2d","title":"动画主题","steps":[每个关键帧...]}\n'
        "每个关键帧的字段（4 个键全部必须出现）：\n"
        "- step_index：整数，从 0 开始递增\n"
        "- narration：字符串，该帧的旁白解说 1~3 句\n"
        '- source_ref：字符串，格式 "4.1.3, p.76"；课本未覆盖时写 "Extended Knowledge"\n'
        "- entities：数组，该帧的画面元素\n"
        "每个 entity 的字段（8 个键全部必须出现）：\n"
        "- id：字符串，同一帧内唯一（entity-0、entity-1 递增）\n"
        "- shape：只能是 circle / rect / arrow / label_box / zone\n"
        "- label：字符串，画面上的短标签\n"
        "- x、y：数字。逻辑画布 1000×600，写元素**中心点**坐标，不是像素\n"
        "- color：只能取这些值：" + _PALETTE_TEXT + "\n"
        "- details：字符串，该元素的详细说明\n"
        "- source_page：整数或 null\n"
        "业务约束：2~5 个关键帧；每帧 2~5 个 entity；"
        "画面要体现「随时间推进」—— 同一主体在不同帧的位置或标签应发生变化。\n"
        "最小合法示例（1 帧，照此扩到 2~5 帧）：\n"
        '{"type":"anim_2d","title":"联合邦计划的推进过程","steps":[{"step_index":0,'
        '"narration":"1946 年英国公布联合邦计划，各地马来人开始组织反对。",'
        '"source_ref":"4.1.1, p.76","entities":[{"id":"entity-0","shape":"zone",'
        '"label":"1946","x":320.0,"y":300.0,"color":"#2563EB",'
        '"details":"计划公布阶段的舆论反应。","source_page":76}]}]}'
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
        "- source_page：整数或 null\n"
        "- parent_id：字符串或 null，表示从属于哪个节点的 id（主体写 null）\n"
        "每个机位的字段（4 个键全部必须出现）：\n"
        "- name：字符串；position：[x, y, z]；target：[x, y, z]；explanation：字符串\n"
        "业务约束：3~8 个 node（1 个主体 + 若干部件，部件用 parent_id 挂到主体上）；"
        "2~4 个机位。\n"
        "最小合法示例（1 个节点 + 1 个机位，照此扩展）：\n"
        '{"type":"scene_3d","title":"马六甲城墙结构","nodes":[{"id":"node-0",'
        '"primitive":"box","label":"城墙主体","position":[0.0,1.5,0.0],'
        '"scale":[2.0,1.0,2.0],"rotation":[0.0,0.0,0.0],"color":"#2563EB",'
        '"annotation_markdown":"墙体以红土夯实，厚度约两米。","source_page":34,'
        '"parent_id":null}],"camera_tour":[{"name":"全景","position":[8.0,6.0,8.0],'
        '"target":[0.0,1.5,0.0],"explanation":"先看整体比例与体量关系。"}]}'
    ),
}

# 兜底：未知类型按 ppt 契约走（与 mock_generator.generate_spec 的退化策略一致）
_DEFAULT_ARTIFACT_CONTRACT = _ARTIFACT_CONTRACT["ppt"]


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

    def with_turn_id(self, turn_id: str) -> "TurnRequest":
        return TurnRequest(
            workspace_id=self.workspace_id,
            query=self.query,
            mode=self.mode,
            anchor=self.anchor,
            artifact_type_override=self.artifact_type_override,
            artifact_id=self.artifact_id,
            turn_id=turn_id,
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

        # ---- 3. 正文流式输出 ----
        reply_parts: list[str] = []
        ttft_ms: int | None = None
        try:
            async for delta in self._reply_stream(req, result, decision, context):
                if not delta:
                    continue
                if ttft_ms is None:
                    ttft_ms = int((time.perf_counter() - started) * 1000)
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

        # ---- 4. 产物 ----
        artifact_event: ArtifactEvent | None = None
        if action == "create_artifact":
            async for event in self._create_artifact_stream(req, decision, context, result):
                yield event
                if isinstance(event[1], ArtifactEvent):
                    artifact_event = event[1]
        elif action == "patch_artifact":
            async for event in self._patch_artifact_stream(req, decision, context):
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
        )
        yield "turn_end", TurnEndEvent(
            turn_id=req.turn_id,
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
            version=artifact_event.version if artifact_event else None,
            demo_mode=demo_mode,
        )

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

    async def _create_artifact_stream(
        self,
        req: TurnRequest,
        decision: RouteDecision,
        context: MockContext,
        result: RetrievalResult,
    ) -> AsyncIterator[Event]:
        spec: dict[str, Any] | None = None
        async for event in self._generate_spec_stream(req, decision, context):
            if event[0] == "spec":
                spec = event[1]
                continue
            yield event

        if spec is None:
            return

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
            # 这里 return 只是结束本阶段，外层会照常走 citations → turn_end。
            yield "error", ErrorEvent(
                code=exc.code.value,
                message=exc.message,
                stage="generate",
                retryable=False,
                attempt=1,
            )
            return

        yield "artifact", ArtifactEvent(
            action="create",
            artifact_id=artifact_id,
            artifact_type=str(spec.get("type")),  # type: ignore[arg-type]
            version=1,
            title=title,
            spec=spec,  # type: ignore[arg-type]
        )

    async def _generate_spec_stream(
        self, req: TurnRequest, decision: RouteDecision, context: MockContext
    ) -> AsyncIterator[Event]:
        """产出 ("error"|"spec", payload)。LLM 路径带校验重试，全部失败降级 mock。"""
        # 实测 generator 出 5 页 PPT 约 70s，若需压总耗时，方向是分批下发或换更快档位
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
        for attempt in range(1, 4):
            try:
                payload = await self._llm.complete_json(
                    system=_ARTIFACT_SYSTEM_PROMPT, user=f"{prompt}{feedback}"
                )
                candidate = payload.get("spec", payload)
                parse_artifact_spec(candidate)
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
        assert req.anchor is not None  # 由 _decide_action 保证
        artifact_id = req.anchor.artifact_id
        try:
            async with self._session_factory() as session:
                artifact = await repository.require_artifact(session, artifact_id)
                # deepcopy：这里拿到的是基线快照，不能与 ORM 对象共享嵌套结构，
                # 否则后续任何原地修改都可能顺着 identity map 影响历史版本行（P0-11③）
                spec_dict = copy.deepcopy(artifact.spec_json or {})
                base_version = artifact.version
                artifact_type = artifact.artifact_type
        except AppError as exc:
            # 读基线失败（产物已被另一个标签页删掉是真实竞态）。就地标 stage="patch"：
            # 冒泡到 chat_stream 的兜底 except 只会硬编码成 "generate"，前端提示与日志
            # 都会指向「正文生成」这个根本没出错的环节。
            yield "error", ErrorEvent(
                code=exc.code.value,
                message=exc.message,
                stage="patch",
                retryable=False,
                attempt=1,
            )
            return

        ops: list[PatchOp] | None = None
        if not self._llm.enabled:
            ops = generate_patch_ops(spec_dict, node_path=req.anchor.node_path, context=context)
        else:
            async for event in self._generate_ops_stream(req, spec_dict, context):
                if event[0] == "ops":
                    ops = event[1]  # type: ignore[assignment]
                    continue
                yield event
        if not ops:
            return

        try:
            outcome = apply_patch_request(
                spec_dict,
                PatchRequest(
                    artifact_id=req.anchor.artifact_id, base_version=base_version, ops=ops
                ),
                current_version=base_version,
            )
        except AppError as exc:
            yield "error", ErrorEvent(
                code=exc.code.value,
                message=exc.message,
                stage="patch",
                retryable=False,
                attempt=1,
            )
            return

        try:
            async with self._session_factory() as session:
                artifact = await repository.require_artifact(session, artifact_id)
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
            yield "error", ErrorEvent(
                code=exc.code.value,
                message=exc.message,
                stage="patch",
                retryable=False,
                attempt=1,
            )
            return

        log_event(
            "patch_applied",
            workspace_id=req.workspace_id,
            artifact_id=req.anchor.artifact_id,
            version=outcome.new_version,
            changed_paths=list(outcome.changed_paths),
        )
        yield "artifact", ArtifactEvent(
            action="patch",
            artifact_id=req.anchor.artifact_id,
            artifact_type=artifact_type,  # type: ignore[arg-type]
            version=outcome.new_version or base_version + 1,
            base_version=base_version,
            ops=ops,
        )

    async def _generate_ops_stream(
        self, req: TurnRequest, spec_dict: dict[str, Any], context: MockContext
    ) -> AsyncIterator[Event]:
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
        for attempt in range(1, 3):
            try:
                payload = await self._llm.complete_json(
                    system=_ARTIFACT_SYSTEM_PROMPT, user=f"{prompt}{feedback}"
                )
                raw_ops = payload.get("ops", [])
                ops = [PatchOp.model_validate(item) for item in raw_ops]
                if not ops:
                    raise ValueError("模型没有返回任何 op")
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
        return (
            f"目标形态：{decision.artifact_type}\n"
            f"章节：{context.section_code} {context.section_title}\n"
            f"用户诉求：{req.query}\n"
            "课本事实（只能依据以下内容生成，不得编造页码或事实）：\n"
            + "\n".join(f"- {fact}" for fact in context.facts[:8])
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

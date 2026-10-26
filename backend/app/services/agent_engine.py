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
from app.schemas.artifacts import Citation, parse_artifact_spec
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
_PAGE_CITATION_RE = re.compile(r"\[p\.(\d+)\]")
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
    "严禁输出 HTML、CSS 或 JavaScript 代码。颜色只能取自给定调色板。"
)


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
        yield "retrieval", RetrievalEvent(
            grader_status=result.grader_status,  # type: ignore[arg-type]
            section_scope=result.section_scope,
            top_score=round(result.top_score, 4),
            chunks=[
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
            ],
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
        async with self._session_factory() as session:
            artifact = await repository.require_artifact(session, req.anchor.artifact_id)
            # deepcopy：这里拿到的是基线快照，不能与 ORM 对象共享嵌套结构，
            # 否则后续任何原地修改都可能顺着 identity map 影响历史版本行（P0-11③）
            spec_dict = copy.deepcopy(artifact.spec_json or {})
            base_version = artifact.version
            artifact_type = artifact.artifact_type

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

        不篡改正文：流式内容用户已经看到了，事后改写会造成「界面闪变」。
        """
        allowed_pages = result.pages
        cited_pages = {int(match) for match in _PAGE_CITATION_RE.findall(reply)}
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
        return (
            f"目标形态：{decision.artifact_type}\n"
            f"章节：{context.section_code} {context.section_title}\n"
            f"用户诉求：{req.query}\n"
            f"课本事实：\n"
            + "\n".join(f"- {fact}" for fact in context.facts[:8])
            + "\n\n请输出符合该形态 JSON Schema 的完整对象（含 type 字段）。"
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

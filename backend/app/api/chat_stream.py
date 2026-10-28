"""对话接口：SSE 流式 + 非流式两种入口。

SSE 的两个工程细节（不做会踩坑）：
1. **队列 + 超时保活**：直接 `async for` 转发的话，`asyncio.wait_for` 超时会取消正在等待的
   `__anext__()`，把 async generator 直接关掉。改成「后台任务写队列、响应协程读队列」，
   超时就发一行 `: ping` 注释，不会干扰生成。
2. **兜底 turn_end**：引擎内部异常时仍要补发 `turn_end`（§5.4 规则②「必发」），
   否则前端 `useChatStream` 会一直以为还在流式，输入框永远禁用。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.logging_conf import log_event
from app.schemas.artifacts import AgentTurnResponse, Citation, KnowledgeScope
from app.schemas.common import ApiResponse, canonical_tree
from app.schemas.chat import (
    ChatRequest,
    ErrorEvent,
    SSE_PING_INTERVAL_SECONDS,
    TurnEndEvent,
    encode_sse,
    sse_ping,
)
from app.services.agent_engine import TurnRequest, get_agent_engine
from app.services.retriever import NO_MATCH, SYLLABUS_MATCH

logger = logging.getLogger("edu.api.chat")

router = APIRouter(tags=["chat"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关掉 nginx 缓冲，否则 SSE 会被攒成一整块才下发，TTFT 直接失效
    "X-Accel-Buffering": "no",
}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


def _turn_request(payload: ChatRequest, request: Request) -> TurnRequest:
    """把 HTTP 请求体映射成引擎入参。

    **`payload.document_id` 的透传点在这里**（Level 1「目录树点击直传」）：
    `ChatRequest.document_id` → `TurnRequest.document_id` → `AgentEngine._retrieve()`
    → `Retriever.retrieve(document_id=...)`。检索层（`retriever` / `vector_store`）
    已完整支持该参数并有单测覆盖；`TurnRequest` 一侧的字段由
    `services/agent_engine.py` 的维护者在合并「后置页码重绑定」时一并补齐
    （frozen dataclass 加字段必须同时改 `with_turn_id()`，属于同一文件内的原子改动，
    跨人同时改该文件会互相覆盖）。补齐后此处只需加一行
    `document_id=payload.document_id`。

    在此之前不传该参数是**安全**的：`document_id=None` 与加入该参数前的行为逐字节一致，
    即「跨全部教材检索」—— 与当前已上线的语义相同，不会引入回归。
    """
    turn_id = getattr(request.state, "request_id", "") or ""
    # TODO(rebind-engineer): TurnRequest 尚无 document_id 字段（该文件正由后置页码重绑定
    # 改动，避免同文件双写者）。补齐 TurnRequest.document_id + with_turn_id() 透传 +
    # _retrieve() 之后，把下面这行加进 TurnRequest(...)：
    #     document_id=payload.document_id,
    # 检索层已就绪，无需再改 retriever / vector_store。
    return TurnRequest(
        workspace_id=payload.workspace_id,
        query=payload.query,
        mode=payload.mode,
        anchor=payload.anchor,
        artifact_type_override=payload.artifact_type_override,
        artifact_id=payload.artifact_id,
        turn_id=turn_id,
    )


def _fallback_end(
    turn_id: str, started: float, first_token_at: float | None
) -> TurnEndEvent:
    """兜底 `turn_end`：填**真实**耗时，不能填 0。

    为什么不能填 0：失败回合的 TTFT 与总耗时是「失败率 / 失败延迟」的唯一数据来源。
    填 0 会让所有异常路径在监控上表现为「0ms 瞬间完成」，与成功回合混在一起后，
    既看不出失败回合有多慢，也无法回答「超时是不是集中发生在某个阶段」。

    - `duration_ms`：turn 开始 → 兜底 `turn_end` 的真实耗时；
    - `ttft_ms`：turn 开始 → 第一个 `token`。**一个 token 都没发出时取与 `duration_ms`
      相同的值** —— 语义是「这一轮到死都没等到首字」，填 0 会被误读成「首字瞬间到达」。

    两者都 `max(1, ...)`：turn 只要真的跑过，耗时就不可能是 0ms。亚毫秒的回合被
    向上取整到 1ms，只是抹掉计时器精度，不会把「很快」伪装成「没花时间」。
    """
    now = time.perf_counter()
    duration_ms = max(1, int((now - started) * 1000))
    ttft_base = first_token_at if first_token_at is not None else now
    ttft_ms = max(1, int((ttft_base - started) * 1000))
    return TurnEndEvent(
        turn_id=turn_id,
        duration_ms=duration_ms,
        ttft_ms=ttft_ms,
        version=None,
        demo_mode=get_settings().demo_mode,
    )


async def _event_stream(engine: Any, turn: TurnRequest) -> AsyncIterator[str]:
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()

    async def produce() -> None:
        # 计时起点放在生产者任务里：它就是「这一轮真正开始跑」的时刻。
        started = time.perf_counter()
        first_token_at: float | None = None
        saw_turn_end = False
        try:
            async for name, payload in engine.stream_turn(turn):
                if name == "turn_end":
                    saw_turn_end = True
                elif name == "token" and first_token_at is None:
                    first_token_at = time.perf_counter()
                await queue.put((name, payload))
        except AppError as exc:
            log_event(
                "turn_stream_error",
                level=logging.WARNING,
                code=exc.code.value,
                message=exc.message,
            )
            await queue.put(
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
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("turn_stream_unhandled")
            await queue.put(
                (
                    "error",
                    ErrorEvent(
                        code=ErrorCode.INTERNAL_ERROR.value,
                        message=ErrorCode.INTERNAL_ERROR.default_message,
                        stage="generate",
                        retryable=False,
                        attempt=1,
                    ),
                )
            )
        finally:
            if not saw_turn_end:
                await queue.put(
                    ("turn_end", _fallback_end(turn.turn_id, started, first_token_at))
                )
            await queue.put(None)

    producer = asyncio.create_task(produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=SSE_PING_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                yield sse_ping()
                continue
            if item is None:
                break
            yield encode_sse(item[0], item[1])
    finally:
        producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer


@router.post("/api/chat/stream", tags=["chat"])
async def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    """SSE 对话。事件顺序与字段见 §5.4，data 为单行紧凑 JSON。"""
    engine = get_agent_engine()
    turn = _turn_request(payload, request)
    return StreamingResponse(
        _event_stream(engine, turn),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/api/chat", response_model=ApiResponse[AgentTurnResponse], tags=["chat"])
async def chat_once(
    payload: ChatRequest, request: Request
) -> ApiResponse[AgentTurnResponse]:
    """非流式入口：把 SSE 事件流归约成一个响应体。用于脚本、自动化测试与降级。"""
    engine = get_agent_engine()
    turn = _turn_request(payload, request)

    reply_parts: list[str] = []
    citations: list[Citation] = []
    grader_status = NO_MATCH
    action = "none"
    spec: dict[str, Any] | None = None

    async for name, event in engine.stream_turn(turn):
        if name == "token":
            reply_parts.append(event.delta)
        elif name == "citations":
            citations = list(event.citations)
        elif name == "retrieval":
            grader_status = event.grader_status
        elif name == "modality_decision":
            action = event.action
        elif name == "artifact" and event.action == "create":
            # 与 SSE 帧、与 GET /api/artifacts/{id} 共用同一套规范化（IMPL-NOTES §E8），
            # 保证「同一份内容在任何出口都是同一串字节」。
            spec = (
                canonical_tree(event.spec.model_dump(mode="json"))
                if event.spec is not None
                else None
            )
        elif name == "error":
            if event.retryable is False:
                raise AppError(ErrorCode(event.code), event.message)

    scope: KnowledgeScope
    if grader_status == NO_MATCH:
        scope = "none"
    elif grader_status == SYLLABUS_MATCH:
        scope = "syllabus_strict"
    else:
        scope = "hybrid_extended"

    return ApiResponse[AgentTurnResponse].ok(
        AgentTurnResponse(
            reply_markdown="".join(reply_parts),
            knowledge_scope=scope,
            citations=citations,
            modality_decision=action,  # type: ignore[arg-type]
            artifact_payload=spec,  # type: ignore[arg-type]
        ),
        request_id=_request_id(request),
    )


__all__ = ["router"]

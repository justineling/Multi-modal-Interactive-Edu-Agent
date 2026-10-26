"""进程内解析任务：状态机 + 进度 + 取消。

为什么不用 Celery/Redis：MVP 是单机双进程演示，引入 broker 会让「开箱即跑」失效
（SPEC §2 的工程现实约束）。`asyncio.Task` + 独立 DB 会话足够支撑
`pending → parsing → ready|failed|cancelled` 的全部需求，且取消是**真取消**
（Celery 的 revoke 对已开跑任务往往只是标记）。

进度写入策略：解析在线程里跑（fitz 是同步 C++ 调用），线程只更新内存计数，
由一个 0.3s 周期的协程把计数落库 —— 避免在线程里操作 async session。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.db import repository
from app.db.models import DocumentChunk
from app.db.session import get_session_factory
from app.db.vector_store import VectorItem, get_vector_store
from app.errors import AppError, ErrorCode
from app.logging_conf import log_event
from app.services.chunker import chunk_document
from app.services.embedding import get_embedding_provider
from app.services.pdf_parser import ParseCancelled, parse_pdf

# 解析阶段的进度区间：解析占 5→70，切片 70→80，向量化 80→92，落库 92→100
PROGRESS_PARSE_START = 5
PROGRESS_PARSE_END = 70
PROGRESS_CHUNK_END = 80
PROGRESS_EMBED_END = 92
PROGRESS_FLUSH_INTERVAL = 0.3


@dataclass
class _TaskState:
    cancel_requested: bool = False
    pages_done: int = 0
    pages_total: int = 0
    extra: dict[str, object] = field(default_factory=dict)


class ParseTaskManager:
    """按 document_id 管理的任务表。同一文档重复上传解析时会先取消旧任务。"""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._states: dict[str, _TaskState] = {}

    def is_running(self, document_id: str) -> bool:
        task = self._tasks.get(document_id)
        return task is not None and not task.done()

    def request_cancel(self, document_id: str) -> bool:
        """标记取消。返回 True 表示确实有一个在跑的任务被标记。"""
        state = self._states.get(document_id)
        if state is None or not self.is_running(document_id):
            return False
        state.cancel_requested = True
        return True

    def start(self, document_id: str, *, workspace_id: str, stored_path: str) -> None:
        existing = self._tasks.get(document_id)
        if existing is not None and not existing.done():
            # 重复解析同一文档：先让旧任务停下，避免两个任务同时往同一 document 写切片
            self.request_cancel(document_id)
            existing.cancel()

        state = _TaskState()
        self._states[document_id] = state
        self._tasks[document_id] = asyncio.create_task(
            self._run(document_id, workspace_id=workspace_id, stored_path=stored_path, state=state),
            name=f"parse:{document_id}",
        )

    async def wait(self, document_id: str, timeout: float = 120.0) -> None:
        """测试与脚本用：等待任务结束（不抛出任务内部异常，状态已写库）。"""
        task = self._tasks.get(document_id)
        if task is None:
            return
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    async def _run(
        self, document_id: str, *, workspace_id: str, stored_path: str, state: _TaskState
    ) -> None:
        settings = get_settings()
        factory = get_session_factory()
        store = get_vector_store()
        provider = get_embedding_provider()

        log_event("doc_parse_start", workspace_id=workspace_id, document_id=document_id)
        try:
            await self._set_status(document_id, status="parsing", progress=PROGRESS_PARSE_START)

            flusher = asyncio.create_task(self._flush_progress(document_id, state))
            try:
                parsed = await asyncio.to_thread(
                    parse_pdf,
                    Path(stored_path),
                    max_pages=settings.MAX_PDF_PAGES,
                    progress_cb=self._make_progress_cb(state),
                    cancel_check=lambda: state.cancel_requested,
                )
            finally:
                flusher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await flusher

            if state.cancel_requested:
                raise ParseCancelled

            drafts = chunk_document(parsed)
            if not drafts:
                raise AppError(ErrorCode.PDF_NO_TEXT_LAYER, "未能从 PDF 中切出任何有效内容")
            await self._set_status(
                document_id,
                progress=PROGRESS_CHUNK_END,
                page_count=parsed.page_count,
                parser_mode=parsed.parser_mode,
            )

            vectors = await provider.embed([draft.content for draft in drafts])
            if state.cancel_requested:
                raise ParseCancelled
            await self._set_status(document_id, progress=PROGRESS_EMBED_END)

            async with factory() as session:
                # 重解析场景：先清掉旧切片与旧向量，避免残留导致重复命中
                await repository.delete_chunks_by_document(session, document_id)
                await session.commit()

                rows = [
                    DocumentChunk(**draft.to_model_kwargs(document_id=document_id, workspace_id=workspace_id))
                    for draft in drafts
                ]
                await repository.bulk_insert_chunks(session, rows)
                await repository.save_toc_tree(session, document_id, list(parsed.toc_tree))
                await session.commit()
                chunk_ids = [row.id for row in rows]

            await store.delete_by_document(document_id)
            await store.upsert_vectors(
                [
                    VectorItem(
                        chunk_id=chunk_id,
                        workspace_id=workspace_id,
                        document_id=document_id,
                        vector=vector,
                        model=provider.model_name,
                    )
                    for chunk_id, vector in zip(chunk_ids, vectors)
                ]
            )

            await self._set_status(document_id, status="ready", progress=100)
            log_event(
                "doc_parse_done",
                workspace_id=workspace_id,
                document_id=document_id,
                chunks=len(drafts),
                page_count=parsed.page_count,
                parser_mode=parsed.parser_mode,
            )
        except ParseCancelled:
            await self._set_status(
                document_id,
                status="cancelled",
                error_code=ErrorCode.PARSE_TASK_CANCELLED.value,
                error_message=ErrorCode.PARSE_TASK_CANCELLED.default_message,
            )
            log_event("doc_parse_cancelled", workspace_id=workspace_id, document_id=document_id)
        except AppError as exc:
            await self._set_status(
                document_id,
                status="failed",
                error_code=exc.code.value,
                error_message=exc.message,
            )
            log_event(
                "doc_parse_fail",
                level=logging.ERROR,
                workspace_id=workspace_id,
                document_id=document_id,
                code=exc.code.value,
                message=exc.message,
            )
        except asyncio.CancelledError:
            # 任务被强制取消（例如应用关闭）：把状态落到 cancelled，不要留在 parsing
            await self._set_status(
                document_id,
                status="cancelled",
                error_code=ErrorCode.PARSE_TASK_CANCELLED.value,
                error_message=ErrorCode.PARSE_TASK_CANCELLED.default_message,
            )
            raise
        except Exception as exc:  # 兜底：任何未预期异常都要落到 failed，不能让状态卡在 parsing
            await self._set_status(
                document_id,
                status="failed",
                error_code=ErrorCode.PDF_PARSE_FAILED.value,
                error_message=f"{ErrorCode.PDF_PARSE_FAILED.default_message}：{exc}",
            )
            log_event(
                "doc_parse_fail",
                level=logging.ERROR,
                workspace_id=workspace_id,
                document_id=document_id,
                code=ErrorCode.PDF_PARSE_FAILED.value,
                error=str(exc),
            )
        finally:
            self._states.pop(document_id, None)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _make_progress_cb(state: _TaskState):
        def _cb(done: int, total: int) -> None:
            state.pages_done = done
            state.pages_total = total

        return _cb

    async def _flush_progress(self, document_id: str, state: _TaskState) -> None:
        """把线程里的页计数周期性落库，让前端进度条真的在动。"""
        while True:
            await asyncio.sleep(PROGRESS_FLUSH_INTERVAL)
            if state.pages_total <= 0:
                continue
            ratio = state.pages_done / state.pages_total
            progress = int(
                PROGRESS_PARSE_START + ratio * (PROGRESS_PARSE_END - PROGRESS_PARSE_START)
            )
            await self._set_status(document_id, progress=progress)

    @staticmethod
    async def _set_status(
        document_id: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        page_count: int | None = None,
        parser_mode: str | None = None,
    ) -> None:
        factory = get_session_factory()
        try:
            async with factory() as session:
                await repository.update_document_status(
                    session,
                    document_id,
                    status=status,
                    progress=progress,
                    error_code=error_code,
                    error_message=error_message,
                    page_count=page_count,
                    parser_mode=parser_mode,
                )
                await session.commit()
        except AppError:
            # 文档已被删除（用户上传后立刻删）：任务没有继续的意义，安静退出
            log_event("doc_parse_target_missing", level=logging.WARNING, document_id=document_id)


_manager: ParseTaskManager | None = None


def get_task_manager() -> ParseTaskManager:
    global _manager
    if _manager is None:
        _manager = ParseTaskManager()
    return _manager


def reset_task_manager() -> None:
    """仅供测试：丢弃任务表（不取消已跑任务，测试里请先 wait）。"""
    global _manager
    _manager = None


__all__ = ["ParseTaskManager", "get_task_manager", "reset_task_manager"]

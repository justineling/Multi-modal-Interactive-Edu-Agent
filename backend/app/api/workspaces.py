"""工作区、文档上传与解析状态接口（P0-01 / P0-02 / P0-03 / P0-04）。

端点契约由团队裁定，前端 `api/client.ts` 按同一份实现 —— 改路径/字段必须先报 team-lead。

上传流程刻意做成「同步体检 + 异步解析」两段：
体检（扩展名/大小/加密/页数）在请求内完成，因为 P0-02② 要求这几类错误**立刻**返回
明确错误码；真正的文本抽取、章节识别、向量化才丢给后台任务（P0-02① 60 秒预算）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import repository
from app.db.session import get_session
from app.db.vector_store import get_vector_store
from app.errors import AppError, ErrorCode
from app.logging_conf import log_event
from app.schemas.api import (
    ChunkOut,
    DeletedOut,
    DocumentOut,
    DocumentStatusOut,
    MessageOut,
    WorkspaceOut,
    chunk_out,
    document_out,
    message_out,
    workspace_out,
)
from app.schemas.common import ApiResponse
from app.services.pdf_parser import inspect_pdf
from app.services.tasks import get_task_manager

logger = logging.getLogger("edu.api.workspaces")

router = APIRouter(tags=["workspaces"])

PDF_SUFFIXES = (".pdf",)


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)


class WorkspaceRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


# --------------------------------------------------------------------------- #
# Workspaces
# --------------------------------------------------------------------------- #
@router.post("/api/workspaces", response_model=ApiResponse[WorkspaceOut], tags=["workspaces"])
async def create_workspace(
    payload: WorkspaceCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[WorkspaceOut]:
    workspace = await repository.create_workspace(session, payload.title)
    await session.flush()
    return ApiResponse[WorkspaceOut].ok(workspace_out(workspace), request_id=_request_id(request))


@router.get("/api/workspaces", response_model=ApiResponse[list[WorkspaceOut]], tags=["workspaces"])
async def list_workspaces(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[WorkspaceOut]]:
    workspaces = await repository.list_workspaces(session)
    return ApiResponse[list[WorkspaceOut]].ok(
        [workspace_out(workspace) for workspace in workspaces], request_id=_request_id(request)
    )


@router.get("/api/workspaces/{workspace_id}", response_model=ApiResponse[WorkspaceOut], tags=["workspaces"])
async def get_workspace(
    workspace_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[WorkspaceOut]:
    workspace = await repository.require_workspace(session, workspace_id)
    return ApiResponse[WorkspaceOut].ok(workspace_out(workspace), request_id=_request_id(request))


@router.patch("/api/workspaces/{workspace_id}", response_model=ApiResponse[WorkspaceOut], tags=["workspaces"])
async def rename_workspace(
    workspace_id: str,
    payload: WorkspaceRename,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[WorkspaceOut]:
    workspace = await repository.rename_workspace(session, workspace_id, payload.title)
    await session.flush()
    return ApiResponse[WorkspaceOut].ok(workspace_out(workspace), request_id=_request_id(request))


@router.delete(
    "/api/workspaces/{workspace_id}", response_model=ApiResponse[DeletedOut], tags=["workspaces"]
)
async def delete_workspace(
    workspace_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DeletedOut]:
    """删除工作区。切片/产物/消息由 FK CASCADE 连带删除（P0-01③）。"""
    workspace = await repository.require_workspace(session, workspace_id)
    documents = await repository.list_documents(session, workspace_id)

    # 先停掉在跑的解析任务，否则任务会在删除后继续写切片（外键报错或写进孤儿行）
    manager = get_task_manager()
    store = get_vector_store()
    for document in documents:
        manager.request_cancel(document.id)

    for document in documents:
        await store.delete_by_document(document.id)

    await session.delete(workspace)
    await session.flush()
    return ApiResponse[DeletedOut].ok(DeletedOut(deleted=True), request_id=_request_id(request))


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
@router.post(
    "/api/workspaces/{workspace_id}/documents",
    response_model=ApiResponse[DocumentOut],
    tags=["documents"],
)
async def upload_document(
    workspace_id: str,
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DocumentOut]:
    settings = get_settings()
    await repository.require_workspace(session, workspace_id)

    filename = (file.filename or "").strip() or "upload.pdf"
    if not filename.lower().endswith(PDF_SUFFIXES):
        raise AppError(ErrorCode.UNSUPPORTED_FILE_TYPE, f"仅支持 PDF 文件，收到 {filename!r}")

    content = await file.read()
    if not content:
        raise AppError(ErrorCode.PDF_EMPTY, "上传的文件是空的")
    if len(content) > settings.max_upload_bytes:
        actual_mb = len(content) / (1024 * 1024)
        raise AppError(
            ErrorCode.UNSUPPORTED_FILE_TYPE,
            f"文件 {actual_mb:.1f}MB，超过上限 {settings.MAX_UPLOAD_MB}MB",
        )

    document = await repository.create_document(
        session, workspace_id=workspace_id, filename=filename, page_count=0
    )
    await session.flush()

    target = settings.upload_dir / f"{document.id}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

    try:
        page_count = inspect_pdf(target, max_pages=settings.MAX_PDF_PAGES)
    except AppError as exc:
        # 体检失败：把已落盘的文件删掉，别留垃圾；错误码原样抛给前端
        target.unlink(missing_ok=True)
        document.status = "failed"
        document.error_code = exc.code.value
        document.error_message = exc.message
        await session.flush()
        raise

    document.stored_path = str(target)
    document.page_count = page_count
    await session.flush()

    # 事务先提交，后台任务才能在自己的会话里看到这条 document
    await session.commit()

    get_task_manager().start(
        document.id, workspace_id=workspace_id, stored_path=str(target)
    )
    log_event(
        "doc_upload",
        workspace_id=workspace_id,
        document_id=document.id,
        page_count=page_count,
        size_bytes=len(content),
    )
    return ApiResponse[DocumentOut].ok(document_out(document), request_id=_request_id(request))


@router.get(
    "/api/workspaces/{workspace_id}/documents",
    response_model=ApiResponse[list[DocumentOut]],
    tags=["documents"],
)
async def list_documents(
    workspace_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[DocumentOut]]:
    await repository.require_workspace(session, workspace_id)
    documents = await repository.list_documents(session, workspace_id)
    return ApiResponse[list[DocumentOut]].ok(
        [document_out(document) for document in documents], request_id=_request_id(request)
    )


@router.get("/api/documents/{document_id}", response_model=ApiResponse[DocumentOut], tags=["documents"])
async def get_document(
    document_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DocumentOut]:
    document = await repository.require_document(session, document_id)
    return ApiResponse[DocumentOut].ok(document_out(document), request_id=_request_id(request))


@router.get(
    "/api/documents/{document_id}/status",
    response_model=ApiResponse[DocumentStatusOut],
    tags=["documents"],
)
async def get_document_status(
    document_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DocumentStatusOut]:
    document = await repository.require_document(session, document_id)
    return ApiResponse[DocumentStatusOut].ok(
        DocumentStatusOut(
            status=document.status,
            progress=document.progress,
            error_code=document.error_code,
            error_message=document.error_message,
        ),
        request_id=_request_id(request),
    )


@router.post(
    "/api/documents/{document_id}/cancel",
    response_model=ApiResponse[DocumentOut],
    tags=["documents"],
)
async def cancel_document(
    document_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DocumentOut]:
    """取消解析。任务标记取消后由任务自身把状态写为 cancelled；已在解析的页会立即停下。"""
    document = await repository.require_document(session, document_id)
    if document.status in ("ready", "failed", "cancelled"):
        # 幂等：已完成/已失败/已取消的文档再点一次取消，不应报错（前端按钮可能重复点击）
        return ApiResponse[DocumentOut].ok(document_out(document), request_id=_request_id(request))

    get_task_manager().request_cancel(document_id)
    await repository.update_document_status(
        session,
        document_id,
        status="cancelled",
        error_code=ErrorCode.PARSE_TASK_CANCELLED.value,
        error_message=ErrorCode.PARSE_TASK_CANCELLED.default_message,
    )
    await session.flush()
    return ApiResponse[DocumentOut].ok(document_out(document), request_id=_request_id(request))


@router.delete(
    "/api/documents/{document_id}", response_model=ApiResponse[DeletedOut], tags=["documents"]
)
async def delete_document(
    document_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DeletedOut]:
    document = await repository.require_document(session, document_id)
    get_task_manager().request_cancel(document_id)
    await get_vector_store().delete_by_document(document_id)

    stored_path = Path(document.stored_path) if document.stored_path else None
    await session.delete(document)
    await session.flush()

    if stored_path is not None:
        stored_path.unlink(missing_ok=True)
    return ApiResponse[DeletedOut].ok(DeletedOut(deleted=True), request_id=_request_id(request))


@router.get(
    "/api/documents/{document_id}/chunks",
    response_model=ApiResponse[list[ChunkOut]],
    tags=["documents"],
)
async def list_document_chunks(
    document_id: str,
    request: Request,
    section_code: str | None = Query(default=None, description="章节号前缀，如 4.1"),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ChunkOut]]:
    """切片预览。`section_code` 按前缀匹配（4.1 会带出 4.1.1、4.1.2）。"""
    await repository.require_document(session, document_id)
    chunks = await repository.list_chunks_by_document(session, document_id)
    if section_code:
        chunks = [chunk for chunk in chunks if chunk.section_code.startswith(section_code)]
    return ApiResponse[list[ChunkOut]].ok(
        [chunk_out(chunk) for chunk in chunks[:limit]], request_id=_request_id(request)
    )


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
@router.get(
    "/api/workspaces/{workspace_id}/messages",
    response_model=ApiResponse[list[MessageOut]],
    tags=["messages"],
)
async def list_workspace_messages(
    workspace_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[MessageOut]]:
    """刷新页面后恢复会话（P0-01②）。按 created_at 升序返回。"""
    await repository.require_workspace(session, workspace_id)
    messages = await repository.list_messages(session, workspace_id, limit=limit)
    return ApiResponse[list[MessageOut]].ok(
        [message_out(message) for message in messages], request_id=_request_id(request)
    )


__all__ = ["router"]

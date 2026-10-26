"""FastAPI 应用入口：CORS、request_id 上下文、全局异常处理器、建表、路由挂载。

异常处理器的价值：让「所有失败」都长成 §9.2 的同一个形状
`{code, data, message, request_id}`。前端 client.ts 只写一次解包逻辑，
不需要为 404/422/500 各写一套判断。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import artifacts as artifacts_api
from app.api import chat_stream as chat_api
from app.api import workspaces as workspaces_api
from app.config import get_settings
from app.db.session import dispose_engine, init_db
from app.db.vector_store import get_vector_store
from app.errors import AppError, ErrorCode, error_payload
from app.logging_conf import (
    configure_logging,
    log_event,
    reset_request_id,
    set_request_id,
)
from app.schemas.common import ApiResponse

logger = logging.getLogger("edu.main")


class HealthPayload(BaseModel):
    """`/api/health` 的 data。

    demo_mode 放在最外层显眼位置：前端顶栏黄标（P0-12③）在启动时就要知道要不要显示，
    不能等到第一次对话才发现没有 Key。
    """

    model_config = ConfigDict(extra="forbid")

    demo_mode: bool
    app_version: str
    database_backend: str
    embedding_mode: str
    vector_store: str
    artifact_types: list[str]


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动：建表 + 加载向量索引；关闭：释放连接池。

    向量索引在启动时全量加载而不是「解析时顺手填内存」：否则重启后已入库的
    文档就检索不到了，表现为「重启前能问、重启后答课本未覆盖」。
    """
    configure_logging()
    settings = get_settings()
    await init_db()
    store = get_vector_store()
    await store.initialize()
    log_event(
        "app_start",
        demo_mode=settings.demo_mode,
        database_backend=settings.database_backend,
        embedding_mode=settings.EMBEDDING_MODE,
    )
    try:
        yield
    finally:
        await dispose_engine()
        log_event("app_stop")


app = FastAPI(
    title="多模态沉浸式智能助教 · API",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-request-id"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    """给每个请求分配 request_id 并塞进日志上下文。

    优先复用前端/网关传来的 x-request-id：跨进程排查时同一个 id 能串起前后端日志。
    """
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    token = set_request_id(request_id)
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    finally:
        reset_request_id(token)
    response.headers["x-request-id"] = request_id
    return response


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


# --------------------------------------------------------------------------- #
# 全局异常处理器：所有失败统一成 §9.2 形状
# --------------------------------------------------------------------------- #
@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    log_event(
        "app_error",
        level=logging.WARNING,
        code=exc.code.value,
        message=exc.message,
        details=exc.details,
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=error_payload(exc.code, exc.message, _request_id(request)),
    )


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体不合法 → REQUEST_INVALID(400)。

    把 Pydantic 的定位信息压成一句中文提示：前端直接展示即可，
    不需要自己再解析 loc 数组。
    """
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ())) or "body"
    message = f"请求参数不合法：{location} {first.get('msg', '')}".strip()
    log_event("request_invalid", level=logging.WARNING, message=message)
    return JSONResponse(
        status_code=ErrorCode.REQUEST_INVALID.http_status,
        content=error_payload(ErrorCode.REQUEST_INVALID, message, _request_id(request)),
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """框架层 4xx（如路由不存在）/5xx 也走统一包装，避免前端拿到裸 detail。"""
    code = ErrorCode.REQUEST_INVALID if exc.status_code < 500 else ErrorCode.INTERNAL_ERROR
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(code, str(exc.detail), _request_id(request)),
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """兜底。异常堆栈只进日志，不回给前端（可能含 SQL 与路径信息）。"""
    logger.exception("unhandled_error", exc_info=exc)
    return JSONResponse(
        status_code=ErrorCode.INTERNAL_ERROR.http_status,
        content=error_payload(ErrorCode.INTERNAL_ERROR, None, _request_id(request)),
    )


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.get("/api/health", response_model=ApiResponse[HealthPayload], tags=["meta"])
async def health(request: Request) -> ApiResponse[HealthPayload]:
    """健康检查。同时充当「前端启动时探测演示模式」的接口。"""
    settings = get_settings()
    store = get_vector_store()
    return ApiResponse[HealthPayload].ok(
        HealthPayload(
            demo_mode=settings.demo_mode,
            app_version=__version__,
            database_backend=settings.database_backend,
            embedding_mode=settings.EMBEDDING_MODE,
            vector_store=type(store).__name__,
            artifact_types=["ppt", "anim_2d", "scene_3d"],
        ),
        request_id=_request_id(request),
    )


app.include_router(workspaces_api.router)
app.include_router(artifacts_api.router)
app.include_router(chat_api.router)

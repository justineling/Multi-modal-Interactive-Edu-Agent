"""JSON 行日志与 request_id 上下文。

为什么不用默认的文本日志：§9.3 要求每条日志可被机器筛选（event / request_id /
workspace_id / duration_ms）。JSON 行格式让 `grep '"event":"patch_applied"'` 直接可用，
也让 P0 验收时能一条命令统计 contract_retry 次数。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

# ContextVar 而不是全局变量：FastAPI 的 async 并发下，全局变量会被并发请求互相覆盖，
# 导致 A 请求的日志带上 B 请求的 request_id。
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_workspace_id_var: ContextVar[str] = ContextVar("workspace_id", default="")

_LOGGER_NAME = "edu"
_configured = False


def set_request_id(value: str) -> Token[str]:
    return _request_id_var.set(value)


def reset_request_id(token: Token[str]) -> None:
    _request_id_var.reset(token)


def get_request_id() -> str:
    return _request_id_var.get()


def set_workspace_id(value: str) -> Token[str]:
    return _workspace_id_var.set(value)


def reset_workspace_id(token: Token[str]) -> None:
    _workspace_id_var.reset(token)


def get_workspace_id() -> str:
    return _workspace_id_var.get()


class JsonLineFormatter(logging.Formatter):
    """把 LogRecord 压成单行 JSON。单行是关键——多行会破坏日志按行切分的工具链。"""

    def format(self, record: logging.LogRecord) -> str:
        extra_fields: dict[str, Any] = getattr(record, "extra_fields", {}) or {}
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "event": getattr(record, "event", None) or record.getMessage(),
            "request_id": getattr(record, "request_id", None) or get_request_id(),
            "workspace_id": getattr(record, "workspace_id", None) or get_workspace_id() or None,
            "duration_ms": getattr(record, "duration_ms", None),
            "extra": extra_fields,
        }
        if record.exc_info:
            payload["extra"] = {**extra_fields, "exc": self.formatException(record.exc_info)}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(level: str | None = None) -> None:
    """幂等配置：重复调用不会叠加 handler（否则同一行日志会打印多次）。"""
    global _configured
    if _configured:
        return

    from app.config import get_settings

    resolved_level = (level or get_settings().LOG_LEVEL or "INFO").upper()
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonLineFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved_level, logging.INFO))

    # uvicorn 自带的 access 日志是纯文本，会污染 JSON 行；我们的 request 中间件已经记了
    # 更有信息量的字段，这里直接压到 WARNING。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    _configured = True


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    duration_ms: float | None = None,
    workspace_id: str | None = None,
    **fields: Any,
) -> None:
    """记录一条结构化事件日志。

    event 命名见 §9.3：doc_parse_start/done/fail、toc_fallback、retrieval_done、
    route_decision、contract_retry、patch_applied、invalid_citation、llm_call。
    """
    logging.getLogger(_LOGGER_NAME).log(
        level,
        event,
        extra={
            "event": event,
            "duration_ms": duration_ms,
            "workspace_id": workspace_id,
            "extra_fields": fields,
        },
    )


def truncate(text: str | None, limit: int = 200) -> str:
    """§9.3 红线：日志里 chunk 正文不得超过 200 字，也不得出现完整 PDF 文本。"""
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"

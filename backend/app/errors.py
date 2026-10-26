"""错误码与异常。

为什么单独成文件：§9.1 规定错误码是**前后端唯一共享的字面量集合**（前端 api/client.ts
镜像同一份）。放在 errors.py 一处定义，避免各 API 模块各写一套字符串导致文案与 HTTP 状态
对不上（前端以 HTTP 判失败、以 code 判文案）。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """§9.1 错误码枚举。str 混入让它可以被 JSON 直接序列化。"""

    OK = "OK"

    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"

    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    PDF_ENCRYPTED = "PDF_ENCRYPTED"
    PDF_EMPTY = "PDF_EMPTY"
    PDF_NO_TEXT_LAYER = "PDF_NO_TEXT_LAYER"
    PDF_PARSE_FAILED = "PDF_PARSE_FAILED"
    PARSE_TASK_CANCELLED = "PARSE_TASK_CANCELLED"

    CONTRACT_VALIDATION_FAILED = "CONTRACT_VALIDATION_FAILED"

    PATCH_PATH_INVALID = "PATCH_PATH_INVALID"
    PATCH_BASE_VERSION_CONFLICT = "PATCH_BASE_VERSION_CONFLICT"

    RETRIEVAL_EMPTY = "RETRIEVAL_EMPTY"

    LLM_UPSTREAM_ERROR = "LLM_UPSTREAM_ERROR"
    LLM_TIMEOUT = "LLM_TIMEOUT"

    # 扩展码（架构文档 §9.1 表未列）：请求体不合法时需要一个「客户端错误」码，
    # 复用 CONTRACT_VALIDATION_FAILED(502) 会把客户端问题报成服务端问题。
    REQUEST_INVALID = "REQUEST_INVALID"

    INTERNAL_ERROR = "INTERNAL_ERROR"

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS.get(self, 500)

    @property
    def default_message(self) -> str:
        return _MESSAGES.get(self, "")


# 与 §9.1 表格逐行对应。新增码必须同时在这里与前端 api/client.ts 登记。
_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.OK: 200,
    ErrorCode.WORKSPACE_NOT_FOUND: 404,
    ErrorCode.DOCUMENT_NOT_FOUND: 404,
    ErrorCode.ARTIFACT_NOT_FOUND: 404,
    ErrorCode.UNSUPPORTED_FILE_TYPE: 400,
    ErrorCode.PDF_ENCRYPTED: 400,
    ErrorCode.PDF_EMPTY: 400,
    ErrorCode.PDF_NO_TEXT_LAYER: 400,
    ErrorCode.PDF_PARSE_FAILED: 500,
    ErrorCode.PARSE_TASK_CANCELLED: 409,
    ErrorCode.CONTRACT_VALIDATION_FAILED: 502,
    ErrorCode.PATCH_PATH_INVALID: 400,
    ErrorCode.PATCH_BASE_VERSION_CONFLICT: 409,
    ErrorCode.RETRIEVAL_EMPTY: 200,
    ErrorCode.LLM_UPSTREAM_ERROR: 502,
    ErrorCode.LLM_TIMEOUT: 504,
    ErrorCode.REQUEST_INVALID: 400,
    ErrorCode.INTERNAL_ERROR: 500,
}

_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.OK: "",
    ErrorCode.WORKSPACE_NOT_FOUND: "工作区不存在或已删除",
    ErrorCode.DOCUMENT_NOT_FOUND: "文档不存在",
    ErrorCode.ARTIFACT_NOT_FOUND: "该画布产物不存在",
    ErrorCode.UNSUPPORTED_FILE_TYPE: "仅支持 PDF 文件",
    ErrorCode.PDF_ENCRYPTED: "该 PDF 已加密，请先解除密码保护",
    ErrorCode.PDF_EMPTY: "该 PDF 没有可用页面",
    ErrorCode.PDF_NO_TEXT_LAYER: "该 PDF 无文本层（疑似扫描件），暂不支持",
    ErrorCode.PDF_PARSE_FAILED: "文档解析失败，请重试",
    ErrorCode.PARSE_TASK_CANCELLED: "解析已取消",
    ErrorCode.CONTRACT_VALIDATION_FAILED: "生成结果未通过契约校验，已自动重试",
    ErrorCode.PATCH_PATH_INVALID: "锚点路径非法",
    ErrorCode.PATCH_BASE_VERSION_CONFLICT: "画布已被更新，请刷新后重试",
    ErrorCode.RETRIEVAL_EMPTY: "课本未覆盖该问题",
    ErrorCode.LLM_UPSTREAM_ERROR: "模型服务返回异常",
    ErrorCode.LLM_TIMEOUT: "模型响应超时",
    ErrorCode.REQUEST_INVALID: "请求参数不合法",
    ErrorCode.INTERNAL_ERROR: "服务内部错误",
}


class AppError(Exception):
    """所有业务异常的基类。

    message 允许覆盖默认中文提示（例如上传超限时给出「文件 62MB，超过 50MB 上限」这类
    具体原因）；http_status 允许覆盖，但默认取错误码对应值，保证 §9.2 的一致性。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code: ErrorCode = code
        self.message: str = message if message else code.default_message
        self.http_status: int = http_status if http_status is not None else code.http_status
        self.details: dict[str, Any] = details or {}
        super().__init__(self.message)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"AppError(code={self.code.value!r}, message={self.message!r})"


class ContractValidationError(AppError):
    """AST / PatchOp 契约校验失败。

    单独成类是为了让 agent_engine 能精确捕获「可重试」的失败（§5.4 规则③：
    校验失败发 error{retryable:true} 但不中断流），而不会把 404 之类的错误也重试。
    """

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.CONTRACT_VALIDATION_FAILED, message, details=details)


def error_payload(
    code: ErrorCode,
    message: str | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """§9.2 统一响应包装（失败态）：code != OK 时 data 恒为 null。

    刻意不塞 details —— 前端 client.ts 按固定四字段解包，多字段会成为隐形契约。
    诊断信息走日志（log_event 的 extra）。
    """
    return {
        "code": code.value,
        "data": None,
        "message": message if message else code.default_message,
        "request_id": request_id,
    }

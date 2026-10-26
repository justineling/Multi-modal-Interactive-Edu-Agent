"""跨模块共享的序列化与响应包装。

canonical_json 是全项目唯一的 JSON 规范化函数。为什么必须统一：
P0-10② 的「其余节点字节级不变」是靠**指纹比对**证明的，如果各处手写
json.dumps（有的带空格、有的 sort_keys=False、有的 ensure_ascii=True），
同一份内容会算出不同指纹，测试会随机挂、patch 会被误判为「改动了非锚定节点」。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


def _json_default(value: Any) -> Any:
    """兜底序列化：让 canonical_json 能直接吃 Pydantic 模型与时间对象。"""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        # set 无序，必须先排序才能保证指纹稳定
        return sorted(value, key=str)
    raise TypeError(f"canonical_json 不支持的类型：{type(value).__name__}")


def canonical_json(obj: Any) -> str:
    """规范化 JSON：键排序 + 紧凑分隔符 + 不转义非 ASCII。

    三个参数缺一不可：
    - sort_keys=True   → 消除 dict 插入顺序差异（patch 后键序变化不应改变指纹）
    - separators       → 消除空格差异，得到最短且唯一的字节序列
    - ensure_ascii=False → 中文按 UTF-8 原样输出，避免 \\uXXXX 转义带来的体积与可读性问题
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


class ApiResponse(BaseModel, Generic[T]):
    """§9.2 统一响应包装。SSE 不走这个包装，事件体直接是 §5.4 的 data。"""

    model_config = ConfigDict(extra="forbid")

    code: str = "OK"
    data: T | None = None
    message: str = ""
    request_id: str = ""

    @classmethod
    def ok(cls, data: T | None = None, *, request_id: str = "", message: str = "") -> "ApiResponse[T]":
        return cls(code="OK", data=data, message=message, request_id=request_id)


class PageQuery(BaseModel):
    """分页参数。上限 200 防止前端一次拉全库把 SQLite 拖死。"""

    model_config = ConfigDict(extra="forbid")

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)

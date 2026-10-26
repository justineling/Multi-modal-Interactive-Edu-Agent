"""跨模块共享的序列化与响应包装。

canonical_json 是全项目唯一的 JSON 规范化函数。为什么必须统一：
P0-10② 的「其余节点字节级不变」是靠**指纹比对**证明的，如果各处手写
json.dumps（有的带空格、有的 sort_keys=False、有的 ensure_ascii=True），
同一份内容会算出不同指纹，测试会随机挂、patch 会被误判为「改动了非锚定节点」。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite 存 ISO 字符串，读回来是 naive；统一按 UTC 补 tzinfo。

    放在 schemas/common.py 而不是 repository：DTO 组装也要用它，
    而契约层不允许 import db/*（会破坏「schemas 零依赖」的分层）。
    禁止在业务层做时区换算——两处各自补 tz 会让前端看到相差 8 小时的时间。
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


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


def canonical_tree(obj: Any) -> Any:
    """把任意 JSON 结构规范成「键有序的新对象」。

    为什么 REST 出口也要规范化（不只是算指纹用）：SSE 帧走 `canonical_json`
    （sort_keys=True），而 REST 原来直接返回库内 dict 的插入顺序 —— 同一份内容在两个
    出口上字节不同。客户端做「创建时拿到的 spec」与「回读的 spec」比对（版本预览、
    断线重连校验、缓存键）时，纯键序差异会被误判成「内容被改写」。
    统一出口序列化等价于把「键序」从协议里彻底消掉：同一内容 ⟺ 同一字节串。
    （IMPL-NOTES §E8：协议出口的序列化必须统一。）

    同时它是**深拷贝**：DTO 与 ORM 对象不再共享嵌套结构，读路径不可能反向污染库内对象。
    """
    return json.loads(canonical_json(obj))


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

"""数据库连接与建表。

**本文件是全库仅有的两个「数据库模式分支点」之一**（另一个是
`db/vector_store.py::build_vector_store`）。除此之外任何文件出现模式判断即为评审不通过，
因为双模式的全部差异已经被收敛到「连接参数」与「向量实现」两处。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import BACKEND_ROOT, get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。SQLAlchemy 2.0 的 DeclarativeBase 风格。"""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


# --------------------------------------------------------------------------- #
# 模式收敛点 1：连接参数
# --------------------------------------------------------------------------- #
def _resolve_database_url(raw_url: str) -> str:
    """把 sqlite 的相对路径锚定到 backend/ 目录。

    为什么必须锚定：`uvicorn --app-dir backend` 的 CWD 是项目根，而
    `cd backend && uvicorn` 的 CWD 是 backend。若不锚定，`./data/app.db` 会变成两个
    不同的文件，表现为「上传过的 PDF 重启后不见了」。Postgres URL 原样返回。
    """
    url: URL = make_url(raw_url)
    if url.get_backend_name() != "sqlite":
        return raw_url
    database = url.database
    if not database or database == ":memory:":
        return raw_url
    path = Path(database)
    if path.is_absolute():
        return raw_url
    absolute = (BACKEND_ROOT / path).resolve()
    # 手工拼 URL 而不是 url.set(...).render_as_string()：后者会把 Windows 的
    # "D:" 百分号编码成 "D%3A"，日志与 .env 里读起来一团乱。as_posix() 保证
    # 正斜杠，SQLite 驱动在 Windows 上同样接受。
    suffix = f"?{urlencode(dict(url.query))}" if url.query else ""
    return f"{url.drivername}:///{absolute.as_posix()}{suffix}"


def _engine_kwargs(url_str: str) -> dict[str, Any]:
    """连接池参数。SQLite 与 Postgres 的唯一差异都在这里。"""
    url = make_url(url_str)
    if url.get_backend_name() == "postgresql":
        # 服务型数据库需要真实连接池 + 断线自愈
        return {"pool_size": 5, "max_overflow": 10, "pool_pre_ping": True, "pool_recycle": 1800}
    # SQLite：aiosqlite 是单文件写入，连接池开大了只会互相等锁；
    # timeout 让「解析任务写库」与「前端轮询读库」并发时不立刻抛 database is locked。
    return {"connect_args": {"timeout": 30}}


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """SQLite 专属 PRAGMA。

    两个 PRAGMA 都不是可选项：
    - journal_mode=WAL → 允许「一边写切片、一边被读」；默认 rollback journal 下
      解析长任务会把所有读请求阻塞到超时。
    - foreign_keys=ON  → SQLite 默认**不**启用外键约束，不显式开启则 §4.1 里
      所有 ON DELETE CASCADE 全是摆设，删除 workspace 后切片会变成孤儿数据
      （直接违反 P0-01③）。
    """
    if engine.url.get_backend_name() != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


# --------------------------------------------------------------------------- #
# 引擎与会话
# --------------------------------------------------------------------------- #
def get_engine() -> AsyncEngine:
    """惰性创建引擎。

    惰性而非模块级创建：测试需要在 import 之后改 DATABASE_URL 指向临时文件，
    模块级创建会把第一个测试的库地址固化到整个进程。
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        resolved = _resolve_database_url(settings.DATABASE_URL)
        _engine = create_async_engine(resolved, echo=False, future=True, **_engine_kwargs(resolved))
        _install_sqlite_pragmas(_engine)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,  # 提交后仍可读对象属性，避免 API 层二次查询
            autoflush=False,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入用的会话生成器。

    请求级事务兜底：正常路径下 API 层已显式 commit，这里的 commit 是幂等的空操作。

    **不要依赖它**——FastAPI ≥ 0.106 的 yield 依赖收尾在响应发出之后执行
    （`fastapi/routing.py::request_response` 里 `await response(scope, receive, send)`
    先跑，退出 `request_stack` 时才跑本函数 `yield` 之后的代码）。依赖它提交会导致
    「返回 200 但客户端紧接着读不到」：客户端已经拿到新 id，下一个请求用新会话查却是
    404 / 旧版本号。写接口必须自己 `await session.commit()`。

    保留它作为兜底是为了两件事：① 只读请求里没有需要提交的东西，这里的 commit 是空操作；
    ② 万一某个写接口漏了 commit，数据至少还在请求结束前落库（只是时序不对）。
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def ensure_data_dir() -> Path:
    """建好 data/、uploads/、artifacts/。启动时调用一次，避免上传时才发现目录不存在。"""
    settings = get_settings()
    for directory in (settings.resolved_data_dir, settings.upload_dir, settings.artifact_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings.resolved_data_dir


async def init_db() -> None:
    """建表。幂等：create_all 只建缺失的表，不修改已存在的表结构。"""
    # 必须 import models 才能让 Base.metadata 感知到全部表定义
    from app.db import models  # noqa: F401  pylint: disable=unused-import

    ensure_data_dir()
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    """释放连接池。测试切换临时库、以及应用优雅关闭时必须调用。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None

"""向量存储：一套协议、两种实现。

**本文件是全库仅有的两个「数据库模式分支点」之一**（另一个是 `db/session.py`）。
之所以敢把模式差异全部收在这里，是因为向量存取本来就只被 retriever 使用一次，
抽象成本极低；反过来如果让业务层判断模式，双模式兼容会渗透到每个 service。

协议刻意只有 3 个方法（§4.2），没有 initialize：初始化属于实现细节，
由 build_vector_store 的返回值（具体类）在应用启动时调用。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import ChunkVector, DocumentChunk
from app.db.session import get_session_factory
from app.logging_conf import log_event

SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True)
class VectorItem:
    """一条待写入的向量。model 字段让「换 embedding 模型后只重建失效向量」成为可能。"""

    chunk_id: str
    workspace_id: str
    document_id: str
    vector: list[float]
    model: str


@dataclass(frozen=True)
class VectorHit:
    """检索命中。score 为余弦相似度（已 L2 归一化，故等于点积），范围 [-1, 1]。"""

    chunk_id: str
    workspace_id: str
    document_id: str
    score: float


@runtime_checkable
class EmbeddingStore(Protocol):
    """§4.2 的向量存取协议。业务代码只依赖它，不依赖具体实现。"""

    async def upsert_vectors(self, items: list[VectorItem]) -> None: ...

    async def search_similar(
        self,
        workspace_id: str,
        query_vec: list[float],
        top_k: int = 10,
        section_prefix: str | None = None,
    ) -> list[VectorHit]: ...

    async def delete_by_document(self, document_id: str) -> None: ...


def _normalize(vector: Sequence[float]) -> np.ndarray:
    """L2 归一化。归一化后余弦相似度退化为点积，检索时省一次除法。

    零向量（全 0）直接原样返回，避免除零产生 NaN —— NaN 参与排序会让 Top-K 结果
    变成随机顺序，是「检索结果偶发乱序」的经典来源。
    """
    array = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    if norm == 0.0:
        return array
    return array / norm


# --------------------------------------------------------------------------- #
# 实现 1：内存暴力检索（默认，零外部依赖）
# --------------------------------------------------------------------------- #
class MemoryVectorStore:
    """numpy 暴力余弦检索。

    规模假设：单 workspace ≤ 2000 chunk（§7.2 阶段 1 的候选集上限）。
    在这个量级上 2000×1536 的矩阵乘是亚毫秒级，没必要引入 ANN 索引。
    """

    def __init__(
        self,
        *,
        dim: int = 1536,
        model: str = "local-hash-1536",
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._dim = dim
        self._model = model
        # 惰性解析 sessionmaker：提前在 __init__ 里取会把 engine 绑死在构造那一刻，
        # 测试中途 dispose_engine 之后就会拿到一个绑在已关闭 engine 上的工厂。
        self._session_factory: SessionFactory | None = session_factory
        # chunk_id → 归一化向量
        self._vectors: dict[str, np.ndarray] = {}
        # chunk_id → (workspace_id, document_id)
        self._meta: dict[str, tuple[str, str]] = {}
        # "workspace|section_prefix" → (chunk_id 列表, 堆叠矩阵)。任何写操作都清空。
        self._matrix_cache: dict[str, tuple[list[str], np.ndarray]] = {}
        self._loaded = False

    @property
    def _sessions(self) -> SessionFactory:
        """返回 async_sessionmaker（调用它才得到 AsyncSession）。"""
        if self._session_factory is None:
            self._session_factory = get_session_factory()
        return self._session_factory

    async def initialize(self) -> None:
        """把库里的向量全量读进内存。进程重启后仍能检索，不依赖「解析时顺带填内存」。"""
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(
                        ChunkVector.chunk_id,
                        ChunkVector.workspace_id,
                        ChunkVector.document_id,
                        ChunkVector.vector,
                        ChunkVector.dim,
                    )
                )
            ).all()

        self._vectors.clear()
        self._meta.clear()
        self._matrix_cache.clear()
        for chunk_id, workspace_id, document_id, raw_vector, dim in rows:
            try:
                vector = json.loads(raw_vector)
            except (TypeError, ValueError):
                log_event("vector_row_corrupt", level=logging.WARNING, chunk_id=chunk_id)
                continue
            if len(vector) != self._dim and dim == self._dim:
                log_event("vector_dim_mismatch", level=logging.WARNING, chunk_id=chunk_id, got=len(vector))
                continue
            self._vectors[chunk_id] = _normalize(vector)
            self._meta[chunk_id] = (workspace_id, document_id)
        self._loaded = True
        log_event("vector_store_loaded", backend="memory", count=len(self._vectors))

    async def upsert_vectors(self, items: list[VectorItem]) -> None:
        if not items:
            return
        if not self._loaded:
            await self.initialize()

        chunk_ids = [item.chunk_id for item in items]
        async with self._sessions() as session:
            existing_rows = (
                await session.execute(select(ChunkVector).where(ChunkVector.chunk_id.in_(chunk_ids)))
            ).scalars().all()
            existing = {row.chunk_id: row for row in existing_rows}

            for item in items:
                payload = json.dumps(item.vector, separators=(",", ":"))
                row = existing.get(item.chunk_id)
                if row is None:
                    session.add(
                        ChunkVector(
                            chunk_id=item.chunk_id,
                            workspace_id=item.workspace_id,
                            document_id=item.document_id,
                            dim=len(item.vector),
                            vector=payload,
                            model=item.model,
                        )
                    )
                else:
                    # 就地更新同一行：chunk_id 有唯一约束，插入会冲突
                    row.workspace_id = item.workspace_id
                    row.document_id = item.document_id
                    row.dim = len(item.vector)
                    row.vector = payload
                    row.model = item.model
            await session.commit()

        for item in items:
            self._vectors[item.chunk_id] = _normalize(item.vector)
            self._meta[item.chunk_id] = (item.workspace_id, item.document_id)
        self._matrix_cache.clear()

    async def search_similar(
        self,
        workspace_id: str,
        query_vec: list[float],
        top_k: int = 10,
        section_prefix: str | None = None,
    ) -> list[VectorHit]:
        if not self._loaded:
            await self.initialize()
        if not query_vec:
            return []

        candidate_ids = await self._candidate_ids(workspace_id, section_prefix)
        if not candidate_ids:
            return []

        chunk_ids, matrix = self._matrix_for(workspace_id, section_prefix, candidate_ids)
        if matrix.size == 0:
            return []

        query = _normalize(query_vec)
        if query.shape[0] != matrix.shape[1]:
            raise ValueError(
                f"查询向量维度 {query.shape[0]} 与索引维度 {matrix.shape[1]} 不一致，"
                "请检查 EMBEDDING_MODE / EMBEDDING_DIM 配置是否与入库时相同"
            )
        scores = matrix @ query

        # 显式按 (-score, chunk_id) 排序：numpy 的 argsort 对同分不保证稳定，
        # 会让「同一问题两次检索顺序不同」，破坏演示可复现性。
        ranked = sorted(
            ((-float(score), chunk_id) for chunk_id, score in zip(chunk_ids, scores)),
            key=lambda pair: (pair[0], pair[1]),
        )
        hits: list[VectorHit] = []
        for negative_score, chunk_id in ranked[: max(1, top_k)]:
            meta = self._meta.get(chunk_id)
            if meta is None:
                continue
            hits.append(
                VectorHit(
                    chunk_id=chunk_id,
                    workspace_id=meta[0],
                    document_id=meta[1],
                    score=-negative_score,
                )
            )
        return hits

    async def delete_by_document(self, document_id: str) -> None:
        async with self._sessions() as session:
            await session.execute(delete(ChunkVector).where(ChunkVector.document_id == document_id))
            await session.commit()

        removed = [chunk_id for chunk_id, meta in self._meta.items() if meta[1] == document_id]
        for chunk_id in removed:
            self._vectors.pop(chunk_id, None)
            self._meta.pop(chunk_id, None)
        self._matrix_cache.clear()

    # ---------------- 内部 ----------------
    async def _candidate_ids(self, workspace_id: str, section_prefix: str | None) -> list[str]:
        """按 workspace（可选按章节前缀）取候选 chunk_id。

        section_prefix 走 DB 而不是内存：内存里只有向量与归属，没有 section_code，
        为省一次查询而在内存维护冗余索引，会在「切片重解析」时产生脏数据。
        """
        in_workspace = [
            chunk_id for chunk_id, meta in self._meta.items() if meta[0] == workspace_id
        ]
        if section_prefix is None or not in_workspace:
            return in_workspace

        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(DocumentChunk.id).where(
                        DocumentChunk.workspace_id == workspace_id,
                        DocumentChunk.section_code.like(f"{section_prefix}%"),
                    )
                )
            ).scalars().all()
        allowed = set(rows)
        return [chunk_id for chunk_id in in_workspace if chunk_id in allowed]

    def _matrix_for(
        self, workspace_id: str, section_prefix: str | None, chunk_ids: list[str]
    ) -> tuple[list[str], np.ndarray]:
        """把候选向量堆成一个矩阵（按需缓存）。

        缓存键带上 section_prefix 并校验候选 id 列表完全相同：只比长度会踩坑 ——
        同一 workspace 下「全量候选」与「某章节候选」长度碰巧相等时，会拿到错的矩阵，
        表现为「按章节过滤检索却返回别章内容」。
        """
        cache_key = f"{workspace_id}|{section_prefix or ''}"
        cached = self._matrix_cache.get(cache_key)
        if cached is not None and cached[0] == chunk_ids:
            return cached
        vectors: list[np.ndarray] = []
        kept: list[str] = []
        for chunk_id in chunk_ids:
            vector = self._vectors.get(chunk_id)
            if vector is None or vector.shape[0] != self._dim:
                continue
            kept.append(chunk_id)
            vectors.append(vector)
        matrix = np.vstack(vectors) if vectors else np.empty((0, self._dim), dtype=np.float32)
        self._matrix_cache[cache_key] = (kept, matrix)
        return kept, matrix


# --------------------------------------------------------------------------- #
# 实现 2：pgvector（可选，需 requirements-postgres.txt）
# --------------------------------------------------------------------------- #
class PgVectorStore:
    """Postgres + pgvector 实现。

    `chunk_vectors.vector`（TEXT）仍是规范数据源，`embedding_vec`（vector 类型）只是
    索引镜像：`initialize()` 会把缺失镜像的行补齐。这样从 SQLite 迁到 Postgres 时
    不需要重跑 embedding，也不会因为镜像损坏而丢数据。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._dsn = _to_asyncpg_dsn(settings.DATABASE_URL)
        self._dim = settings.EMBEDDING_DIM
        self._pool: Any | None = None

    async def _get_pool(self) -> Any:
        """惰性建连接池 + 惰性 import asyncpg。

        惰性 import 是硬要求：SQLite 模式下这个模块必须能在**没装 asyncpg** 的机器上
        正常 import，否则「开箱即跑」直接失效。
        """
        if self._pool is not None:
            return self._pool
        try:
            import asyncpg  # noqa: PLC0415  惰性 import，见 docstring
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError(
                "PgVectorStore 需要 asyncpg。请执行 "
                "`backend/.venv/Scripts/python.exe -m pip install -r backend/requirements-postgres.txt`，"
                "或把 DATABASE_URL 改回 sqlite+aiosqlite:///./data/app.db"
            ) from exc

        self._pool = await asyncpg.create_pool(dsn=self._dsn, min_size=1, max_size=5)
        return self._pool

    async def initialize(self) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await connection.execute(
                f"ALTER TABLE chunk_vectors ADD COLUMN IF NOT EXISTS embedding_vec vector({self._dim})"
            )
            # 从 TEXT 规范列回填索引镜像；只在镜像缺失时写，避免每次启动全表重写
            rows = await connection.fetch(
                "SELECT id, vector FROM chunk_vectors WHERE embedding_vec IS NULL"
            )
            for row in rows:
                await connection.execute(
                    "UPDATE chunk_vectors SET embedding_vec = $1::vector WHERE id = $2",
                    _to_pg_vector(json.loads(row["vector"])),
                    row["id"],
                )
            try:
                await connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_vec_ann ON chunk_vectors "
                    "USING hnsw (embedding_vec vector_cosine_ops)"
                )
            except Exception as exc:  # pragma: no cover - 老版本 pgvector 无 hnsw
                log_event("pgvector_index_skipped", level=logging.WARNING, error=str(exc))
        log_event("vector_store_loaded", backend="pgvector", dim=self._dim)

    async def upsert_vectors(self, items: list[VectorItem]) -> None:
        if not items:
            return
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                for item in items:
                    await connection.execute(
                        """
                        INSERT INTO chunk_vectors
                            (id, chunk_id, workspace_id, document_id, dim, vector, model, embedding_vec, created_at)
                        VALUES
                            (gen_random_uuid()::text, $1, $2, $3, $4, $5, $6, $7::vector, NOW())
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            workspace_id = EXCLUDED.workspace_id,
                            document_id  = EXCLUDED.document_id,
                            dim          = EXCLUDED.dim,
                            vector       = EXCLUDED.vector,
                            model        = EXCLUDED.model,
                            embedding_vec = EXCLUDED.embedding_vec
                        """,
                        item.chunk_id,
                        item.workspace_id,
                        item.document_id,
                        len(item.vector),
                        json.dumps(item.vector, separators=(",", ":")),
                        item.model,
                        _to_pg_vector(item.vector),
                    )

    async def search_similar(
        self,
        workspace_id: str,
        query_vec: list[float],
        top_k: int = 10,
        section_prefix: str | None = None,
    ) -> list[VectorHit]:
        if not query_vec:
            return []
        pool = await self._get_pool()
        sql = """
            SELECT v.chunk_id, v.workspace_id, v.document_id,
                   1 - (v.embedding_vec <=> $1::vector) AS score
              FROM chunk_vectors v
              JOIN document_chunks c ON c.id = v.chunk_id
             WHERE v.workspace_id = $2
        """
        params: list[Any] = [_to_pg_vector(query_vec), workspace_id]
        if section_prefix:
            sql += " AND c.section_code LIKE $3"
            params.append(f"{section_prefix}%")
        sql += " ORDER BY v.embedding_vec <=> $1::vector LIMIT $%d" % (len(params) + 1)
        params.append(max(1, top_k))

        async with pool.acquire() as connection:
            rows = await connection.fetch(sql, *params)
        return [
            VectorHit(
                chunk_id=row["chunk_id"],
                workspace_id=row["workspace_id"],
                document_id=row["document_id"],
                score=float(row["score"]),
            )
            for row in rows
        ]

    async def delete_by_document(self, document_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute("DELETE FROM chunk_vectors WHERE document_id = $1", document_id)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def _to_pg_vector(vector: Sequence[float]) -> str:
    """转成 pgvector 的文本字面量 '[1,2,3]'。

    手写而不是依赖 pgvector 包的 register_vector：这样只要装了 asyncpg 就能跑，
    少一个可选的编译型依赖（Windows 上尤其重要）。
    """
    return "[" + ",".join(f"{float(value):.7g}" for value in vector) + "]"


def _to_asyncpg_dsn(database_url: str) -> str:
    """把 SQLAlchemy 的 postgresql+asyncpg:// 转成 asyncpg 能吃的 postgresql://。"""
    url = make_url(database_url)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


# --------------------------------------------------------------------------- #
# 模式收敛点 2：工厂
# --------------------------------------------------------------------------- #
def build_vector_store(settings: Settings | None = None) -> MemoryVectorStore | PgVectorStore:
    """按 DATABASE_URL 选实现。全库唯一决定「用哪个向量实现」的地方。"""
    resolved = settings or get_settings()
    if make_url(resolved.DATABASE_URL).get_backend_name() == "postgresql":
        return PgVectorStore(resolved)
    return MemoryVectorStore(dim=resolved.EMBEDDING_DIM, model=f"local-hash-{resolved.EMBEDDING_DIM}")


_store: MemoryVectorStore | PgVectorStore | None = None


def get_vector_store() -> MemoryVectorStore | PgVectorStore:
    """进程级单例。MemoryVectorStore 持有内存索引，每次新建会让索引反复全量加载。"""
    global _store
    if _store is None:
        _store = build_vector_store()
    return _store


def reset_vector_store() -> None:
    """仅供测试：丢弃单例，让下次按当前配置重建。"""
    global _store
    _store = None


__all__ = [
    "EmbeddingStore",
    "MemoryVectorStore",
    "PgVectorStore",
    "VectorHit",
    "VectorItem",
    "build_vector_store",
    "get_vector_store",
    "reset_vector_store",
]

"""Embedding 提供者。

默认用**本地确定性哈希向量**（§9.7），三个理由：
1. 零外部依赖 —— P0-12 要求清空 API Key 后全链路照跑，检索也必须能跑；
2. 确定性 —— 同一文本必然同一向量，`test_mock_determinism` 与回归测试才有意义；
3. 快 —— 1000 个 chunk 的向量化在百毫秒级，不拖累 P0-02 的 60 秒预算。

红线：本模块禁止 `random` / `time.*` / `uuid4()` / `datetime.now()` / 内置 `hash()`
（内置 hash 受 PYTHONHASHSEED 影响，进程重启就变）。统一 `hashlib.blake2b`。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import numpy as np

from app.config import Settings, get_settings
from app.errors import AppError, ErrorCode

_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
# 中文虚词单字：它们出现在任何句子里，纳入向量只会抬高所有文本的相似度底噪
_CJK_STOPWORDS = frozenset("的了是在和与及或也就都而及其个之对为以于")


def tokenize(text: str) -> list[str]:
    """分词：英文/数字按词、中文按「单字 + 相邻 bigram」。

    为什么中文要 bigram：中文没有空格，只切单字会让「联邦」与「联合邦」共享大量单字，
    相似度被虚词淹没。bigram 能保留「联邦/苏丹/疆域」这类术语的区分度。
    这是纯函数，retriever 的 BM25 与本模块共用，保证「同一文本在两处切出同样的词」。
    """
    lowered = text.lower()
    tokens: list[str] = _WORD_RE.findall(lowered)
    for match in _CJK_RUN_RE.finditer(lowered):
        run = match.group()
        for index, char in enumerate(run):
            if char not in _CJK_STOPWORDS:
                tokens.append(char)
            if index + 1 < len(run):
                bigram = run[index : index + 2]
                if not (bigram[0] in _CJK_STOPWORDS and bigram[1] in _CJK_STOPWORDS):
                    tokens.append(bigram)
    return tokens


def _token_index(token: str, dim: int) -> int:
    """token → 维度下标。blake2b 而非内置 hash：跨进程、跨机器稳定。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dim


def embed_text_sync(text: str, dim: int) -> list[float]:
    """§9.7 的确定性向量：`vec[blake2b(token) % dim] += tf`，最后 L2 归一化。"""
    vector = np.zeros(dim, dtype=np.float32)
    counts = Counter(tokenize(text))
    for token, freq in sorted(counts.items()):  # 排序只为可复现，浮点累加顺序固定
        vector[_token_index(token, dim)] += float(freq)
    norm = float(np.linalg.norm(vector))
    if norm > 0.0:
        vector /= norm
    return [float(value) for value in vector]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """embedding 抽象。业务只依赖它，因此换远程模型不需要改 retriever。"""

    @property
    def dim(self) -> int: ...

    @property
    def model_name(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_one(self, text: str) -> list[float]: ...


class LocalHashEmbedding:
    """本地确定性实现（默认）。async 只是为了让调用方不必区分本地/远程。"""

    def __init__(self, dim: int = 1536) -> None:
        self._dim = dim
        self._model_name = f"local-hash-{dim}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [embed_text_sync(text, self._dim) for text in texts]

    async def embed_one(self, text: str) -> list[float]:
        return embed_text_sync(text, self._dim)


class RemoteEmbedding:
    """OpenAI 兼容 `/embeddings`。仅在 `EMBEDDING_MODE=remote` 且配置了 Key 时启用。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._dim = settings.EMBEDDING_DIM
        self._model_name = settings.EMBEDDING_MODEL

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import httpx  # 惰性 import：本地模式完全不需要 httpx 参与启动路径

        if not texts:
            return []
        url = f"{self._settings.LLM_BASE_URL.rstrip('/')}/embeddings"
        payload = {"model": self._model_name, "input": list(texts)}
        headers = {"Authorization": f"Bearer {self._settings.LLM_API_KEY}"}
        try:
            async with httpx.AsyncClient(timeout=self._settings.LLM_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except httpx.TimeoutException as exc:
            raise AppError(ErrorCode.LLM_TIMEOUT, "Embedding 服务响应超时") from exc
        except Exception as exc:  # httpx.HTTPError 及其它网络异常
            raise AppError(ErrorCode.LLM_UPSTREAM_ERROR, f"Embedding 服务调用失败：{exc}") from exc

        vectors = [item["embedding"] for item in body.get("data", [])]
        if len(vectors) != len(texts):
            raise AppError(ErrorCode.LLM_UPSTREAM_ERROR, "Embedding 返回条数与请求不一致")
        for vector in vectors:
            if len(vector) != self._dim:
                raise AppError(
                    ErrorCode.LLM_UPSTREAM_ERROR,
                    f"Embedding 维度 {len(vector)} 与 EMBEDDING_DIM={self._dim} 不一致",
                )
        return vectors

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        return vectors[0]


_provider: EmbeddingProvider | None = None


def build_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    """默认本地；只有显式 `EMBEDDING_MODE=remote` 且配了 Key 才走远程（§10 第 2 条）。"""
    resolved = settings or get_settings()
    if resolved.EMBEDDING_MODE == "remote" and not resolved.demo_mode:
        return RemoteEmbedding(resolved)
    return LocalHashEmbedding(resolved.EMBEDDING_DIM)


def get_embedding_provider() -> EmbeddingProvider:
    """进程级单例：向量化是纯计算，不需要每次新建。"""
    global _provider
    if _provider is None:
        _provider = build_embedding_provider()
    return _provider


def reset_embedding_provider() -> None:
    """仅供测试。"""
    global _provider
    _provider = None


__all__ = [
    "EmbeddingProvider",
    "LocalHashEmbedding",
    "RemoteEmbedding",
    "build_embedding_provider",
    "embed_text_sync",
    "get_embedding_provider",
    "reset_embedding_provider",
    "tokenize",
]

"""混合检索 + 相关性评估（§7.2 / §7.3）。

四段式：元数据过滤 → BM25 + 向量双路打分 → 加权融合 → Top-K。
为什么必须混合：纯向量对「4.1」「1946」这类数字/编号极不敏感（哈希向量里它们只是一个
随机维度），而纯 BM25 又抓不住「苏丹权力被削减」这种换了说法的同义表述。
两者融合才能同时满足 P0-04③（Top-5 同章、页码正确）与 P0-05（按课本原话作答）。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings
from app.db import repository
from app.db.vector_store import EmbeddingStore, VectorHit
from app.db.session import get_session_factory
from app.logging_conf import log_event
from app.services.embedding import EmbeddingProvider, tokenize
from app.services.text_signals import ContentCoverage, content_hit_ratio, content_words

# 查询里的显式章节号，如 "4.1"、"4.1.2"；带点的一定比裸数字更像章节号
_SECTION_DOTTED_RE = re.compile(r"\b(\d{1,2}(?:\.\d{1,2}){1,2})\b")
# 「第五章 / 第 4 节 / 第三讲 / 第二课」——中文教材里最常见的点名方式，数字是汉字
_SECTION_CHAPTER_RE = re.compile(r"第\s*([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*[章节讲课]")
_SECTION_BARE_RE = re.compile(r"\b(\d{1,2})\b")
_CN_DIGITS: dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
# 候选集最小样本数：不足 3 条时逐级放宽检索范围（§7.2 阶段 1）
MIN_CANDIDATES = 3
SNIPPET_LEN = 160
PAGE_BONUS_WINDOW = 20

NO_MATCH = "NO_MATCH"
PARTIAL_OR_BEYOND = "PARTIAL_OR_BEYOND"
SYLLABUS_MATCH = "SYLLABUS_MATCH"


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    section_code: str
    section_title: str
    page_start: int
    page_end: int
    score: float
    snippet: str
    chunk_index: int = 0
    # 完整正文。只供服务端（mock 生成 / 提示词构造）使用，不会进 SSE 事件体
    # —— SSE 里只带 snippet，避免把整章文本推给前端。
    content: str = ""

    @property
    def pages(self) -> range:
        return range(self.page_start, self.page_end + 1)


@dataclass(frozen=True)
class RetrievalResult:
    """检索结果 + 评估结论。`grader_status` 决定后续走严格/双层/未覆盖三条分支。

    `content_hit_ratio` 是「问句实词在 Top-N 切片里的命中率」，即 Grader 判 NO_MATCH 时
    真正依据的语义信号。放进结果体是为了让误判可被观测：线上出现「该覆盖却判未覆盖」时，
    先看这个值，而不是去猜融合分数。
    """

    chunks: tuple[RetrievedChunk, ...]
    grader_status: str
    section_scope: str | None
    top_score: float
    query_terms: tuple[str, ...]
    content_hit_ratio: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.chunks

    @property
    def pages(self) -> set[int]:
        pages: set[int] = set()
        for chunk in self.chunks:
            pages.update(chunk.pages)
        return pages

    @property
    def section_codes(self) -> tuple[str, ...]:
        seen: list[str] = []
        for chunk in self.chunks:
            if chunk.section_code not in seen:
                seen.append(chunk.section_code)
        return tuple(seen)


def _cn_to_int(token: str) -> int | None:
    """汉字数字 → 整数（只覆盖章/节号会用到的 1~99）。"""
    if not token:
        return None
    if token in _CN_DIGITS:
        return _CN_DIGITS[token]
    if len(token) == 2 and token[0] == "十" and token[1] in _CN_DIGITS:
        return 10 + _CN_DIGITS[token[1]]
    if len(token) == 2 and token[1] == "十" and token[0] in _CN_DIGITS:
        return _CN_DIGITS[token[0]] * 10
    if len(token) == 3 and token[1] == "十":
        tens = _CN_DIGITS.get(token[0])
        ones = _CN_DIGITS.get(token[2])
        if tens is not None and ones is not None:
            return tens * 10 + ones
    return None


def extract_section_code(text: str) -> str | None:
    """从用户问句里抽显式章节号（"学习 4.1" / "第五章讲了什么" / "4.1 讲了什么"）。

    优先级：带点编号 > 「第X章/节/讲/课」 > 裸数字。
    - 带点最像章节号：`出 5 道关于 4.2 的随堂测验` 里 "5" 是题量、"4.2" 才是章节；
      若只按「第一个数字」取，会把检索范围锁到第 5 章，答出来的内容完全跑偏。
    - 汉字章号（"第五章"）必须单独识别：中文教材的用户不会把章号写成阿拉伯数字，
      漏了这条，`第五章讲了什么` 会退化成「课本未覆盖」——而第 5 章明明就在库里。
    """
    dotted = _SECTION_DOTTED_RE.search(text)
    if dotted:
        return dotted.group(1)
    chapter = _SECTION_CHAPTER_RE.search(text)
    if chapter:
        raw = chapter.group(1)
        number = int(raw) if raw.isdigit() else _cn_to_int(raw)
        if number is not None:
            return str(number)
    bare = _SECTION_BARE_RE.search(text)
    return bare.group(1) if bare else None


def has_explicit_section_reference(text: str) -> bool:
    """问句是否**点名**了课本的一章/一节（而不是碰巧带了个数字）。

    这条区分是 P0-05④ 的关键：`帮我写 3 首关于春天的五言绝句` 里的 "3" 只是量词，
    若把它当章节号，就会以「用户点名了第 3 章」为由判成 SYLLABUS_MATCH —— 正是
    「无关问题被当成课本覆盖」这一类误判。只有带点编号与「第X章/节」才算点名。
    """
    return bool(_SECTION_DOTTED_RE.search(text) or _SECTION_CHAPTER_RE.search(text))


def _snippet(content: str, limit: int = SNIPPET_LEN) -> str:
    flattened = " ".join(content.split())
    return flattened if len(flattened) <= limit else flattened[:limit] + "…"


def _bm25_scores(
    query_tokens: Sequence[str],
    documents: Sequence[Sequence[str]],
    *,
    k1: float,
    b: float,
) -> tuple[list[float], float]:
    """纯 Python BM25。返回 `(每篇得分, 理想得分)`。

    为什么还要返回「理想得分」：§7.2 原本写 `bm25_norm = bm25 / max(bm25)`，这是**相对**
    归一化 —— 无论查询多离谱，候选集里最高的那篇永远是 1.0，于是任何问题都能拿到
    0.35 的基础分，P0-05④ 要求的 `NO_MATCH`（课本未覆盖）**永远不可能触发**。
    改成除以 `max(理想得分, 实际最高分)`：理想得分 = 查询词全部命中且文档长度等于均长时的
    BM25 值（即 Σ idf），于是「一个词都没命中」的查询会得到接近 0 的分，语义才成立。
    """
    if not documents:
        return [], 0.0
    document_count = len(documents)
    lengths = [len(tokens) for tokens in documents]
    average_length = (sum(lengths) / document_count) if document_count else 0.0
    average_length = average_length or 1.0

    document_frequency: Counter[str] = Counter()
    for tokens in documents:
        document_frequency.update(set(tokens))

    unique_query_tokens = list(dict.fromkeys(query_tokens))
    idf: dict[str, float] = {
        token: math.log(1.0 + (document_count - frequency + 0.5) / (frequency + 0.5))
        for token, frequency in document_frequency.items()
    }

    scores: list[float] = []
    for tokens, length in zip(documents, lengths):
        term_frequency = Counter(tokens)
        score = 0.0
        for token in unique_query_tokens:
            frequency = term_frequency.get(token, 0)
            if frequency == 0:
                continue
            denominator = frequency + k1 * (1.0 - b + b * length / average_length)
            score += idf.get(token, 0.0) * (frequency * (k1 + 1.0)) / denominator
        scores.append(score)

    # 理想得分：查询词在语料中真实存在（df>0）时的上限。df=0 的词不该拉高上限，
    # 否则「问了课本里根本不存在的词」反而会把分母抬高、把分数压低到失真。
    ideal = sum(idf.get(token, 0.0) for token in unique_query_tokens if token in document_frequency)
    return scores, ideal


def _section_bonus(chunk_code: str, scope: str | None) -> float:
    """1.0 本节或子节；0.5 同章；否则 0（§7.2 阶段 3）。"""
    if not scope:
        return 0.0
    if chunk_code == scope or chunk_code.startswith(f"{scope}."):
        return 1.0
    if chunk_code.split(".")[0] == scope.split(".")[0]:
        return 0.5
    return 0.0


def _page_bonus(page_start: int, page: int | None) -> float:
    if page is None:
        return 0.0
    distance = min(abs(page_start - page), PAGE_BONUS_WINDOW)
    return 1.0 - distance / PAGE_BONUS_WINDOW


class Retriever:
    """检索器。持有 store / embedding / settings，全部依赖注入，便于单测替换。"""

    def __init__(
        self,
        store: EmbeddingStore,
        provider: EmbeddingProvider,
        *,
        settings: Settings | None = None,
        session_factory: Any | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._settings = settings or get_settings()
        self._session_factory = session_factory or get_session_factory()

    async def retrieve(
        self,
        workspace_id: str,
        query: str,
        *,
        section_code: str | None = None,
        page: int | None = None,
        top_k: int | None = None,
        strict_section: bool = False,
    ) -> RetrievalResult:
        settings = self._settings
        resolved_top_k = top_k or settings.RETRIEVAL_TOP_K
        scope = section_code or extract_section_code(query)
        query_tokens = tokenize(query)
        # 实词（主题词）是判定「课本是否覆盖」的唯一语义信号，见 text_signals 模块头注释
        query_words = content_words(query)
        # 调用方显式给了 section_code（用户点了画布节点）与问句点名章节，都算强元数据证据；
        # 裸数字不算，否则「帮我写 3 首…」会被当成「点名了第 3 章」。
        explicit_section = bool(section_code) or has_explicit_section_reference(query)
        short_terms = tuple(token for token in query_tokens if len(token) >= 2)

        hits = await self._search_with_widening(
            workspace_id, query, scope, strict_section=strict_section
        )
        if not hits:
            log_event("retrieval_done", grader_status=NO_MATCH, top_score=0.0, candidates=0)
            return RetrievalResult(
                chunks=(),
                grader_status=NO_MATCH,
                section_scope=scope,
                top_score=0.0,
                query_terms=short_terms,
                content_hit_ratio=0.0,
            )

        async with self._session_factory() as session:
            chunks = await repository.list_chunks_by_ids(session, [hit.chunk_id for hit in hits])

        by_id = {chunk.id: chunk for chunk in chunks}
        candidates: list[tuple[VectorHit, Any]] = [
            (hit, by_id[hit.chunk_id]) for hit in hits if hit.chunk_id in by_id
        ]
        if not candidates:
            return RetrievalResult(
                chunks=(),
                grader_status=NO_MATCH,
                section_scope=scope,
                top_score=0.0,
                query_terms=short_terms,
                content_hit_ratio=0.0,
            )

        documents = [tokenize(chunk.content) for _, chunk in candidates]
        bm25, ideal_bm25 = _bm25_scores(
            query_tokens, documents, k1=settings.BM25_K1, b=settings.BM25_B
        )
        denominator = max(max(bm25) if bm25 else 0.0, ideal_bm25)

        scored: list[RetrievedChunk] = []
        for (hit, chunk), bm25_score in zip(candidates, bm25):
            bm25_norm = (bm25_score / denominator) if denominator > 0 else 0.0
            vector_score = max(hit.score, 0.0)
            score = (
                settings.BM25_WEIGHT * bm25_norm
                + settings.VECTOR_WEIGHT * vector_score
                + settings.SECTION_BONUS_WEIGHT * _section_bonus(chunk.section_code, scope)
                + settings.PAGE_BONUS_WEIGHT * _page_bonus(chunk.page_start, page)
            )
            scored.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    section_code=chunk.section_code,
                    section_title=chunk.section_title,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    score=round(score, 6),
                    snippet=_snippet(chunk.content),
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                )
            )

        # 稳定排序：同分时按 chunk_index，避免同一问题两次检索顺序不同（演示可复现性）
        scored.sort(key=lambda item: (-item.score, item.chunk_index))
        top = tuple(scored[: max(1, resolved_top_k)])
        top_score = top[0].score if top else 0.0
        coverage = content_hit_ratio(
            query_words,
            [chunk.content or chunk.snippet for chunk in top[: settings.GRADER_CONTENT_HIT_WINDOW]],
        )
        grader_status = self._grade(
            top,
            top_score,
            query_tokens,
            settings,
            scope,
            coverage=coverage,
            explicit_section=explicit_section,
        )

        log_event(
            "retrieval_done",
            grader_status=grader_status,
            top_score=round(top_score, 4),
            candidates=len(candidates),
            section_scope=scope,
            content_hit_ratio=round(coverage.ratio, 4),
            content_hit_words=list(coverage.matched),
        )
        return RetrievalResult(
            chunks=top,
            grader_status=grader_status,
            section_scope=scope,
            top_score=top_score,
            query_terms=short_terms,
            content_hit_ratio=coverage.ratio,
        )

    async def _search_with_widening(
        self, workspace_id: str, query: str, scope: str | None, *, strict_section: bool = False
    ) -> list[VectorHit]:
        """元数据过滤优先，命中不足逐级放宽（本节 → 本章 → 全 workspace）。

        `strict_section=True` 时**不做放宽**：这是「用户点了某个节点」的场景，
        §6.2 明确要求「按 anchor.section_code 限定检索范围」。放宽会把同章其它小节的
        切片拉进来，导致引用页码与被点中的节点对不上（用户看到的是「p.70」而不是他点的那页）。
        """
        query_vector = await self._provider.embed_one(query)
        limit = self._settings.RETRIEVAL_CANDIDATE_LIMIT

        scopes: list[str | None] = []
        if scope:
            scopes.append(scope)
            chapter = scope.split(".")[0]
            if chapter != scope:
                scopes.append(chapter)
        scopes.append(None)

        for candidate_scope in scopes:
            hits = await self._store.search_similar(
                workspace_id, query_vector, top_k=limit, section_prefix=candidate_scope
            )
            if hits and strict_section:
                # 锚点场景：本节只要有命中就锁死在本节，不再放宽
                return hits
            if len(hits) >= MIN_CANDIDATES or candidate_scope is None:
                return hits
        return []

    @staticmethod
    def _grade(
        chunks: Sequence[RetrievedChunk],
        top_score: float,
        query_tokens: Sequence[str],
        settings: Settings,
        scope: str | None = None,
        *,
        coverage: ContentCoverage | None = None,
        explicit_section: bool = False,
    ) -> str:
        """§7.3 三态判定 + 两条补充规则（元数据锚定 / 实词覆盖门槛）。

        **补充规则一：显式章节锚定（IMPL-NOTES §E2）**
        学生说「学习 4.1」时，问句与课本正文几乎没有词面重叠，但「他点名了课本里确实
        存在的一节」本身就是强证据（SPEC §1.2 的目录树精确锚定）。若只按分数判，这条最
        重要的演示路径会被判成 NO_MATCH，然后答「课本未覆盖」—— 与产品预期完全相反。
        因此：显式章节命中 + 该节至少 1 个切片 + top1 落在该节 → 直接 SYLLABUS_MATCH。

        **补充规则二：实词覆盖门槛（P0-05④ 的真正判据）**
        为什么不能只看 `top_score`：融合分数里 BM25 是「相对归一化」，向量是哈希基线，
        两者都可能在「一个主题词都没命中」时给出 0.30~0.40 的高分（实测「怎么用 Rust
        写一个 WebAssembly 模块」0.4016 > 「Malayan Union 对马来西亚宪制发展的长期
        影响」0.3605）。用分数阈值去堵，必然同时误伤真正的中间态问题。
        改用语义信号：问句实词在 Top-N 切片里的命中率低于 `GRADER_CONTENT_HIT_MIN_RATIO`
        时，无论分数多高都判 NO_MATCH —— 因为「问句主题没在课本里出现过」这个事实，
        比任何加权分数都更接近「课本是否覆盖」的定义。
        """
        if not chunks:
            return NO_MATCH

        scope_support = 0
        if scope:
            scope_support = sum(
                1
                for chunk in chunks
                if chunk.section_code == scope or chunk.section_code.startswith(f"{scope}.")
            )
        # 裸数字（量词）不算点名，必须由 extract_section_code 的强模式确认过
        anchored = (
            explicit_section
            and scope is not None
            and scope_support >= 1
            and (
                chunks[0].section_code == scope
                or chunks[0].section_code.startswith(f"{scope}.")
            )
        )

        if anchored:
            return SYLLABUS_MATCH

        hit_ratio = coverage.ratio if coverage is not None else 0.0
        if hit_ratio < settings.GRADER_CONTENT_HIT_MIN_RATIO:
            # 实词几乎没命中：课本没有覆盖这个问题。哪怕 top_score 很高也不能放行。
            log_event(
                "grader_no_content_coverage",
                hit_ratio=round(hit_ratio, 4),
                threshold=settings.GRADER_CONTENT_HIT_MIN_RATIO,
                top_score=round(top_score, 4),
            )
            return NO_MATCH

        if top_score < settings.GRADER_PARTIAL_THRESHOLD:
            return NO_MATCH

        support_count = sum(1 for chunk in chunks if chunk.score >= settings.GRADER_SUPPORT_THRESHOLD)
        keywords = [token for token in query_tokens if len(token) >= 2]
        top_text = " ".join(chunk.snippet.lower() for chunk in chunks[:3])
        keyword_hit = any(keyword in top_text for keyword in keywords)

        if (
            top_score >= settings.GRADER_MATCH_THRESHOLD
            and support_count >= 2
            and keyword_hit
        ):
            return SYLLABUS_MATCH
        return PARTIAL_OR_BEYOND


_retriever: Retriever | None = None


def get_retriever() -> Retriever:
    """进程级单例。依赖 store 与 embedding 两个单例，重建代价高。"""
    global _retriever
    if _retriever is None:
        from app.db.vector_store import get_vector_store
        from app.services.embedding import get_embedding_provider

        _retriever = Retriever(get_vector_store(), get_embedding_provider())
    return _retriever


def reset_retriever() -> None:
    """仅供测试。"""
    global _retriever
    _retriever = None


__all__ = [
    "MIN_CANDIDATES",
    "NO_MATCH",
    "PARTIAL_OR_BEYOND",
    "SYLLABUS_MATCH",
    "RetrievalResult",
    "Retriever",
    "RetrievedChunk",
    "extract_section_code",
    "get_retriever",
    "has_explicit_section_reference",
    "reset_retriever",
]

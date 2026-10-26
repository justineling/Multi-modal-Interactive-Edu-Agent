"""按章节切片。

关键设计：**页面归属是排他的**（`ParsedDocument.page_owner` 一页只归一个章节），
所以切片天然不重叠。如果改用「父章节区间包含子章节区间」的方式切片，同一段正文会
进库两次 —— 检索时就会出现重复命中，`Top-5 全同章` 这类断言会随机失败，
而且 BM25 的文档频率统计会被同文重复污染。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.pdf_parser import ParsedDocument

# 单 chunk 目标长度：教学文本 1000~1400 字符，正好是一个小节的密度，
# 既能命中一个完整知识点，又不至于把多个知识点混进同一向量。
CHUNK_MAX_CHARS = 1200
# 章节末尾的碎块合并阈值，避免出现 20 字的「尾巴 chunk」拉低检索质量
CHUNK_MIN_CHARS = 200


@dataclass(frozen=True)
class ChunkDraft:
    """入库前的切片草稿。`page_start`/`page_end` 为 1 基闭区间，必须可回溯原 PDF。"""

    section_code: str
    section_title: str
    level: int
    chunk_index: int
    page_start: int
    page_end: int
    content: str

    @property
    def char_len(self) -> int:
        return len(self.content)

    def to_model_kwargs(self, *, document_id: str, workspace_id: str) -> dict[str, Any]:
        return {
            "document_id": document_id,
            "workspace_id": workspace_id,
            "section_code": self.section_code,
            "section_title": self.section_title,
            "level": self.level,
            "chunk_index": self.chunk_index,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "content": self.content,
            "char_len": self.char_len,
        }


def _split_long_text(text: str, max_chars: int) -> list[str]:
    """把一页正文切成不超过 max_chars 的片段。

    优先按行切（PDF 的行就是自然断点），单行超长才硬切 —— 硬切会切断词语，
    但总比产出一个 5000 字的巨块好（巨块会让向量语义糊成一团）。
    """
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    buffer: list[str] = []
    buffer_len = 0
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        while len(line) > max_chars:
            if buffer:
                pieces.append("\n".join(buffer))
                buffer, buffer_len = [], 0
            pieces.append(line[:max_chars])
            line = line[max_chars:]
        if buffer and buffer_len + len(line) + 1 > max_chars:
            pieces.append("\n".join(buffer))
            buffer, buffer_len = [], 0
        buffer.append(line)
        buffer_len += len(line) + 1
    if buffer:
        pieces.append("\n".join(buffer))
    return [piece for piece in pieces if piece.strip()]


def chunk_document(
    parsed: ParsedDocument,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    min_chars: int = CHUNK_MIN_CHARS,
) -> list[ChunkDraft]:
    """按章节顺序切片，`chunk_index` 全局递增（保证同分排序稳定）。"""
    pages_by_code: dict[str, list[int]] = {}
    for page_no, code in enumerate(parsed.page_owner, start=1):
        pages_by_code.setdefault(code, []).append(page_no)

    drafts: list[ChunkDraft] = []
    for section in parsed.sections:
        page_numbers = pages_by_code.get(section.code, [])
        if not page_numbers:
            # 章节标题与更深层标题同页时，这一页归更深层；此处无切片是正确的，
            # 不能为了「每章都有 chunk」而重复切同一页内容。
            continue

        pieces: list[tuple[int, str]] = []
        for page_no in page_numbers:
            text = parsed.pages_text[page_no - 1].strip()
            if not text:
                continue
            pieces.extend((page_no, piece) for piece in _split_long_text(text, max_chars))

        buffer: list[tuple[int, str]] = []
        buffer_len = 0

        def flush() -> None:
            nonlocal buffer, buffer_len
            if not buffer:
                return
            content = "\n".join(piece for _, piece in buffer)
            drafts.append(
                ChunkDraft(
                    section_code=section.code,
                    section_title=section.title,
                    level=section.level,
                    chunk_index=len(drafts),
                    page_start=buffer[0][0],
                    page_end=buffer[-1][0],
                    content=content,
                )
            )
            buffer, buffer_len = [], 0

        for page_no, piece in pieces:
            if buffer and buffer_len + len(piece) + 1 > max_chars:
                flush()
            buffer.append((page_no, piece))
            buffer_len += len(piece) + 1
        flush()

    drafts = _reindex(_merge_short_chunks(drafts, min_chars))

    # 兜底断言：P0-04① 要求 100% 非空，宁可在这里炸掉也不要入库脏数据
    for draft in drafts:
        if not draft.section_code.strip():
            raise AssertionError("chunker 产出空 section_code，属上游解析缺陷")
        if draft.page_start < 1 or draft.page_end < draft.page_start:
            raise AssertionError(f"页码区间非法：{draft.page_start}-{draft.page_end}")
    return drafts


def _merge_short_chunks(drafts: list[ChunkDraft], min_chars: int) -> list[ChunkDraft]:
    """把过短的尾块并入同章节的前一块。

    碎块（几十个字）的向量几乎不携带语义，却会挤占 Top-K 名额；合并后
    既保住页码区间（取 max page_end），又不跨章节。
    """
    merged: list[ChunkDraft] = []
    for draft in drafts:
        if (
            merged
            and draft.char_len < min_chars
            and merged[-1].section_code == draft.section_code
        ):
            previous = merged[-1]
            merged[-1] = ChunkDraft(
                section_code=previous.section_code,
                section_title=previous.section_title,
                level=previous.level,
                chunk_index=previous.chunk_index,
                page_start=previous.page_start,
                page_end=max(previous.page_end, draft.page_end),
                content=f"{previous.content}\n{draft.content}",
            )
            continue
        merged.append(draft)
    return merged


def _reindex(drafts: list[ChunkDraft]) -> list[ChunkDraft]:
    """合并后重排 chunk_index，保证全局连续递增（同分排序依赖它）。"""
    return [
        ChunkDraft(
            section_code=draft.section_code,
            section_title=draft.section_title,
            level=draft.level,
            chunk_index=position,
            page_start=draft.page_start,
            page_end=draft.page_end,
            content=draft.content,
        )
        for position, draft in enumerate(drafts)
    ]


__all__ = ["CHUNK_MAX_CHARS", "CHUNK_MIN_CHARS", "ChunkDraft", "chunk_document"]

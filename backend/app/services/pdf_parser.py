"""PDF 解析：逐页文本 + 章节号识别 + 目录树 + 降级兜底（§7.1）。

**为什么以 `page.get_text()` 为主、pymupdf4llm 为可选增强**：
P0-04② 要求每个 chunk 的 `page_start/page_end` 可回溯到真实页码。`pymupdf4llm` 擅长
保留标题层级，但它是「整篇 Markdown」视角，页码映射要额外对齐；而 PDF 里本来就没有
Markdown 的 `#`，章节号全靠正则识别。所以：
- 主路径 = `fitz` 逐页 `get_text("text")`，页码天然精确、速度是毫秒级；
- `use_markdown=True` 时用 `pymupdf4llm.to_markdown(page_chunks=True)` 取带 `#` 的 Markdown
  作为识别线索（默认关闭：v2 的版面模型在 CPU 上对百页文档有明显耗时，会威胁 P0-02 的 60 秒预算）。

红线：任何情况下 `section_code` 都不得为空 —— 识别失败就走 `page_fallback` 伪章节。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pymupdf 1.24+ 提供 pymupdf 模块名；旧版本只有 fitz
    import pymupdf
except ImportError:  # pragma: no cover - 兼容旧版本
    import fitz as pymupdf  # type: ignore[no-redef]

from app.errors import AppError, ErrorCode
from app.logging_conf import log_event

# 少于这个字符数就认为没有文本层（扫描件）。200 字符 ≈ 半页正文，
# 定得太低会把「只有页眉页脚的扫描件」误判为可解析。
MIN_TEXT_CHARS = 200
# 降级模式下每多少页一组
FALLBACK_GROUP_SIZE = 5
# 标题候选的最大行长：超过这个长度的行必然是正文段落
MAX_HEADING_LEN = 80

_CONTENTS_RE = re.compile(r"(?i)^\s*(contents|table\s+of\s+contents|isi\s+kandungan|daftar\s+isi)\s*$")
# 顺序即优先级：先深后浅，否则 "4.1.2 xxx" 会被 R2 截成 "4.1"
_R3 = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{1,2})\s+(\S.{0,78})$")
_R2 = re.compile(r"^(\d{1,2})\.(\d{1,2})(?!\.\d)\s+(\S.{0,78})$")
_R1 = re.compile(r"(?i)^(?:chapter|bab)\s+(\d{1,2})\b[\s:.\-]*(.*)$")
_R1B = re.compile(r"^(\d{1,2})[\.\s]+([A-Z][^.]{3,78})$")


class ParseCancelled(Exception):
    """用户取消。任务层把它翻译成 `cancelled` 状态，不算失败。"""


@dataclass(frozen=True)
class Section:
    """一个章节。`start_page`/`end_page` 为 1 基闭区间，且是**子树范围**（供目录树定位）。"""

    code: str
    title: str
    level: int
    start_page: int
    end_page: int


@dataclass(frozen=True)
class ParsedDocument:
    """解析结果。

    `pages_text[i]` 是第 i+1 页；`page_owner[i]` 是第 i+1 页归属的 `section_code`。
    两者配合 chunker 可以做到「每页只属于一个章节」，从而**不会产生跨章重复切片**
    —— 如果用「父章节区间覆盖子章节」的方式切片，同一段正文会进两次库，
    检索结果里就会出现重复命中，Top-5 同章断言直接失效。
    """

    page_count: int
    pages_text: tuple[str, ...]
    sections: tuple[Section, ...]
    page_owner: tuple[str, ...]
    toc_tree: tuple[dict[str, Any], ...]
    parser_mode: str

    @property
    def max_level(self) -> int:
        return max((section.level for section in self.sections), default=1)

    @property
    def section_count(self) -> int:
        return len(self.sections)

    def section_by_code(self, code: str) -> Section | None:
        for section in self.sections:
            if section.code == code:
                return section
        return None


@dataclass(frozen=True)
class _Heading:
    code: str
    title: str
    level: int
    page: int


# --------------------------------------------------------------------------- #
# 打开与体检
# --------------------------------------------------------------------------- #
def _open_document(source: str | Path | bytes) -> Any:
    """打开 PDF。`source` 可以是路径，也可以是**字节内容**。

    为什么必须支持 bytes（P0-02② 的孤儿文件问题）：
    PyMuPDF 的 `open(路径)` 在**打开失败**时会留下一个 OS 级文件句柄，而这个句柄的
    生命周期绑在异常的 traceback 帧上 —— 也就是说，只要 `AppError.__cause__` 还活着
    （上传接口的错误分支里必然活着），文件就删不掉，Windows 上表现为
    `PermissionError: [WinError 32]`。实测：`from None` 去掉异常链也无效，
    只有「PyMuPDF 完全不接触路径」才能让 `unlink` 立刻成功。
    因此上传体检走 `open(stream=..., filetype="pdf")`：字节由调用方从内存给，
    PyMuPDF 不打开文件，也就没有句柄可残留。
    """
    try:
        if isinstance(source, bytes):
            document = pymupdf.open(stream=source, filetype="pdf")
        else:
            document = pymupdf.open(str(source))
    except Exception as exc:
        # 扩展名对但内容坏掉 / 根本不是 PDF：给统一的可读错误，不让底层异常裸奔到 500
        raise AppError(ErrorCode.PDF_PARSE_FAILED, f"无法打开 PDF 文件：{exc}") from exc

    if document.needs_pass:
        document.close()
        raise AppError(ErrorCode.PDF_ENCRYPTED)
    if document.page_count == 0:
        document.close()
        raise AppError(ErrorCode.PDF_EMPTY)
    return document


def inspect_pdf(source: str | Path | bytes, *, max_pages: int | None = None) -> int:
    """上传时的快速体检：只读元信息，返回页数。

    放在上传接口里而不是解析任务里，是因为 P0-02② 要求「加密 / 0 页」**立刻**返回明确
    错误码；等后台任务失败了再报，用户已经看到进度条在转了。

    `source` 传 **bytes** 是上传接口的推荐用法：体检在落盘之前完成，体检失败时磁盘上
    根本没有文件，从结构上消除「失败留下孤儿文件」（见 `_open_document` 的句柄说明）。
    """
    document = _open_document(source)
    try:
        page_count = document.page_count
    finally:
        document.close()

    if max_pages is not None and page_count > max_pages:
        raise AppError(
            ErrorCode.UNSUPPORTED_FILE_TYPE,
            f"PDF 共 {page_count} 页，超过上限 {max_pages} 页",
        )
    return page_count


# --------------------------------------------------------------------------- #
# 标题识别
# --------------------------------------------------------------------------- #
def _match_heading(raw_line: str) -> tuple[int, str, str] | None:
    """单行 → (level, code, title)。返回 None 表示不是标题。"""
    line = raw_line.strip()
    if not line:
        return None
    # Markdown 标题前缀只作为线索剥掉，层级以章节号为准（PDF 本身没有 # 语义）
    line = line.lstrip("#").strip()
    if not line or len(line) > MAX_HEADING_LEN:
        return None
    if line.startswith("|") or line.endswith((".", ",", "。", "，")):
        return None

    match = _R3.match(line)
    if match:
        return 3, f"{match.group(1)}.{match.group(2)}.{match.group(3)}", match.group(4).strip()

    match = _R2.match(line)
    if match:
        return 2, f"{match.group(1)}.{match.group(2)}", match.group(3).strip()

    match = _R1.match(line)
    if match:
        return 1, match.group(1), match.group(2).strip() or f"Chapter {match.group(1)}"

    match = _R1B.match(line)
    if match:
        return 1, match.group(1), match.group(2).strip()

    return None


def _is_contents_page(text: str) -> bool:
    """目录页整页跳过：否则 "4.1 xxx ...... 12" 会被当成章节标题，页码全部错位。"""
    checked = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _CONTENTS_RE.match(stripped):
            return True
        checked += 1
        if checked >= 3:
            break
    return False


def _detect_headings(pages_text: Sequence[str]) -> list[_Heading]:
    headings: list[_Heading] = []
    for page_index, text in enumerate(pages_text):
        if _is_contents_page(text):
            continue
        page_no = page_index + 1
        for line in text.splitlines():
            matched = _match_heading(line)
            if matched is None:
                continue
            level, code, title = matched
            headings.append(_Heading(code=code, title=title, level=level, page=page_no))
    return headings


def _dedupe_headings(headings: Sequence[_Heading]) -> list[_Heading]:
    """同一 code 只保留首次出现（§7.1 第 4 步）。

    页眉里重复出现的章节名、跨页续排的小标题都会导致重复；保留首次出现才能让
    区间计算单调递增。
    """
    seen: set[str] = set()
    unique: list[_Heading] = []
    for heading in sorted(headings, key=lambda item: item.page):
        if heading.code in seen:
            continue
        seen.add(heading.code)
        unique.append(heading)
    return unique


def _fallback_sections(page_count: int) -> tuple[tuple[Section, ...], tuple[str, ...], str]:
    sections: list[Section] = []
    owners: list[str] = []
    for start in range(1, page_count + 1, FALLBACK_GROUP_SIZE):
        end = min(start + FALLBACK_GROUP_SIZE - 1, page_count)
        code = f"P{start:04d}"
        sections.append(Section(code=code, title=f"第 {start}-{end} 页", level=1, start_page=start, end_page=end))
        owners.extend([code] * (end - start + 1))
    log_event("toc_fallback", level=logging.WARNING, page_count=page_count)
    return tuple(sections), tuple(owners), "page_fallback"


def _build_sections(
    headings: Sequence[_Heading], page_count: int
) -> tuple[tuple[Section, ...], tuple[str, ...], str]:
    unique = _dedupe_headings(headings)
    has_level1 = any(heading.level == 1 for heading in unique)
    max_level = max((heading.level for heading in unique), default=1)
    # §7.1 第 7 步：识别不出三级层级或没有一级标题 → 判定识别失败，走降级
    if not unique or not has_level1 or max_level < 3:
        return _fallback_sections(page_count)

    sections: list[Section] = []
    for index, heading in enumerate(unique):
        end_page = page_count
        for later in unique[index + 1 :]:
            if later.level <= heading.level:
                end_page = max(heading.page, later.page - 1)
                break
        sections.append(
            Section(
                code=heading.code,
                title=heading.title,
                level=heading.level,
                start_page=heading.page,
                end_page=end_page,
            )
        )

    # 页面归属：每页归给「起始页 ≤ 本页」的最后一个标题（同页多标题时取更深的那个）
    owners: list[str] = []
    cursor = 0
    for page in range(1, page_count + 1):
        while cursor + 1 < len(unique) and unique[cursor + 1].page <= page:
            cursor += 1
        owners.append(unique[cursor].code)

    return tuple(sections), tuple(owners), "regex"


def _build_toc(sections: Sequence[Section]) -> tuple[dict[str, Any], ...]:
    """按文档顺序用栈把扁平的章节列表还原成树（code 前缀关系天然满足父子约束）。"""
    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    for section in sections:
        node: dict[str, Any] = {
            "code": section.code,
            "title": section.title,
            "level": section.level,
            "start_page": section.start_page,
            "end_page": section.end_page,
            "children": [],
        }
        while stack and int(stack[-1]["level"]) >= section.level:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            roots.append(node)
        stack.append(node)
    return tuple(roots)


# --------------------------------------------------------------------------- #
# Markdown 增强（可选）
# --------------------------------------------------------------------------- #
def _markdown_pages(path: str | Path, page_count: int) -> list[str] | None:
    """用 pymupdf4llm 拿带 `#` 的逐页 Markdown。失败/页数对不上就返回 None 走纯文本。"""
    try:
        import pymupdf4llm

        chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True, show_progress=False)
        texts = [str(chunk.get("text", "")) for chunk in chunks]
    except Exception as exc:  # 版面模型缺失、加密、超时……都不该让整次解析失败
        log_event("markdown_extract_failed", level=logging.WARNING, error=str(exc))
        return None
    if len(texts) != page_count:
        log_event("markdown_page_mismatch", level=logging.WARNING, got=len(texts), expected=page_count)
        return None
    return texts


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def parse_pdf(
    path: str | Path,
    *,
    max_pages: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    use_markdown: bool = False,
) -> ParsedDocument:
    """解析 PDF。

    `progress_cb(done, total)` 每页回调一次（任务层据此把进度写进库）；
    `cancel_check()` 返回 True 时立刻抛 `ParseCancelled`，让「取消」在中途生效而不是等全程跑完。
    """
    document = _open_document(path)
    pages_text: list[str] = []
    try:
        page_count = document.page_count
        if max_pages is not None and page_count > max_pages:
            raise AppError(
                ErrorCode.UNSUPPORTED_FILE_TYPE,
                f"PDF 共 {page_count} 页，超过上限 {max_pages} 页",
            )
        for index in range(page_count):
            if cancel_check is not None and cancel_check():
                raise ParseCancelled
            pages_text.append(document.load_page(index).get_text("text"))
            if progress_cb is not None:
                progress_cb(index + 1, page_count)
    finally:
        document.close()

    if sum(len(text.strip()) for text in pages_text) < MIN_TEXT_CHARS:
        # 扫描件没有文本层：与其给出一个空目录树，不如明确告诉用户不支持（PRD Q1）
        raise AppError(ErrorCode.PDF_NO_TEXT_LAYER)

    hint_pages: Sequence[str] = pages_text
    if use_markdown:
        markdown = _markdown_pages(path, page_count)
        if markdown is not None:
            hint_pages = markdown

    headings = _detect_headings(hint_pages)
    sections, page_owner, parser_mode = _build_sections(headings, page_count)
    toc_tree = _build_toc(sections)

    log_event(
        "doc_parse_done",
        page_count=page_count,
        sections=len(sections),
        max_level=max((section.level for section in sections), default=1),
        parser_mode=parser_mode,
    )
    return ParsedDocument(
        page_count=page_count,
        pages_text=tuple(pages_text),
        sections=sections,
        page_owner=page_owner,
        toc_tree=toc_tree,
        parser_mode=parser_mode,
    )


def flatten_toc(toc: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """把目录树拍平（深度优先），供「节点数统计」与前端搜索用。"""
    flat: list[dict[str, Any]] = []
    for node in toc:
        flat.append(node)
        flat.extend(flatten_toc(node.get("children", [])))
    return flat


__all__ = [
    "FALLBACK_GROUP_SIZE",
    "MIN_TEXT_CHARS",
    "ParseCancelled",
    "ParsedDocument",
    "Section",
    "flatten_toc",
    "inspect_pdf",
    "parse_pdf",
]

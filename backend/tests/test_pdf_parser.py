"""PDF 解析与切片测试（P0-03 / P0-04）。

断言全部基于 `scripts/make_sample_pdf.py` 生成的真实 PDF，而不是手搓的假数据 ——
否则「三级层级识别」这类能力根本没被验证到。
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf  # type: ignore[no-redef]

from app.errors import AppError, ErrorCode
from app.services.chunker import chunk_document
from app.services.pdf_parser import (
    ParseCancelled,
    flatten_toc,
    inspect_pdf,
    parse_pdf,
)

# 样本教材的真实结构（与 make_sample_pdf.CHAPTERS 对应）
EXPECTED_CHAPTERS = 5
EXPECTED_SECTIONS = 5 * 3
EXPECTED_SUBSECTIONS = 5 * 3 * 3


def _build_plain_pdf(path: Path, pages: int = 6) -> Path:
    """没有任何章节标题的 PDF：用来验证降级兜底。"""
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page(width=595, height=842)
        text = (
            f"这是第 {index + 1} 页的连续叙述文本，用来模拟没有章节编号的扫描件转写稿。"
            "内容只是填充，目的是让文本层足够长，从而触发解析而不是被判定为无文本层。"
        )
        page.insert_text((56, 72), text, fontname="china-s", fontsize=10)
    document.save(str(path))
    document.close()
    return path


def _build_short_text_pdf(path: Path) -> Path:
    """文本层极短：模拟扫描件（只有页眉页脚）。"""
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((56, 72), "Scan only", fontname="china-s", fontsize=10)
    document.save(str(path))
    document.close()
    return path


def _build_encrypted_pdf(path: Path) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((56, 72), "encrypted", fontname="china-s", fontsize=10)
    document.save(
        str(path),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner",
        user_pw="user",
    )
    document.close()
    return path


# --------------------------------------------------------------------------- #
# 主样本：三级层级
# --------------------------------------------------------------------------- #
def test_sample_pdf_page_count(sample_pdf_path: Path) -> None:
    assert inspect_pdf(sample_pdf_path) >= 100


def test_parse_detects_three_levels(sample_pdf_path: Path) -> None:
    parsed = parse_pdf(sample_pdf_path)

    assert parsed.parser_mode == "regex"
    assert parsed.max_level == 3, "P0-03① 要求至少识别出三级层级"
    assert parsed.section_count >= EXPECTED_CHAPTERS + EXPECTED_SECTIONS
    assert len(parsed.toc_tree) == EXPECTED_CHAPTERS

    flat = flatten_toc(list(parsed.toc_tree))
    levels = {node["level"] for node in flat}
    assert levels == {1, 2, 3}
    # P0-03②：节点数 ≥ 真实章节数 90%
    assert len(flat) >= int((EXPECTED_CHAPTERS + EXPECTED_SECTIONS + EXPECTED_SUBSECTIONS) * 0.9)


def test_toc_tree_hierarchy_and_ranges(sample_pdf_path: Path) -> None:
    parsed = parse_pdf(sample_pdf_path)
    for chapter in parsed.toc_tree:
        assert chapter["level"] == 1
        assert chapter["children"], "章下必须有节"
        assert 1 <= chapter["start_page"] <= chapter["end_page"] <= parsed.page_count
        for section in chapter["children"]:
            assert section["level"] == 2
            assert section["start_page"] >= chapter["start_page"]
            assert section["children"], "节下必须有小节"
            for sub in section["children"]:
                assert sub["level"] == 3
                assert sub["start_page"] <= sub["end_page"]


def test_page_owner_covers_every_page_without_gap(sample_pdf_path: Path) -> None:
    """页面归属必须「一页一个章节、且全覆盖」：漏页会产生检索空洞，重页会产生重复命中。"""
    parsed = parse_pdf(sample_pdf_path)
    assert len(parsed.page_owner) == parsed.page_count
    assert all(code.strip() for code in parsed.page_owner)
    known_codes = {section.code for section in parsed.sections}
    assert set(parsed.page_owner) <= known_codes


def test_chunks_have_no_empty_section_code(sample_pdf_path: Path) -> None:
    """P0-04① 硬指标：section_code 命中率 100%。"""
    parsed = parse_pdf(sample_pdf_path)
    drafts = chunk_document(parsed)

    assert drafts, "必须切出切片"
    assert all(draft.section_code.strip() for draft in drafts)
    assert [draft.chunk_index for draft in drafts] == list(range(len(drafts)))
    for draft in drafts:
        assert draft.page_start >= 1
        assert draft.page_end >= draft.page_start
        assert draft.page_end <= parsed.page_count
        assert draft.content.strip()
        assert draft.char_len == len(draft.content)


def test_chunk_pages_match_section_ranges(sample_pdf_path: Path) -> None:
    """P0-04②：每个 chunk 的页码可回溯到原 PDF 真实页码（且落在其章节区间内）。

    例外是「首个标题之前的封面/目录/前言」：§7.1 规定它们继承首个标题的 code，
    因此 page_start 允许早于该章节的 start_page，但绝不能越过它的 end_page。
    """
    parsed = parse_pdf(sample_pdf_path)
    sections = {section.code: section for section in parsed.sections}
    first_section_code = parsed.sections[0].code
    for draft in chunk_document(parsed):
        section = sections[draft.section_code]
        assert 1 <= draft.page_start <= draft.page_end <= section.end_page
        if draft.section_code != first_section_code:
            assert section.start_page <= draft.page_start


# --------------------------------------------------------------------------- #
# 降级与错误码
# --------------------------------------------------------------------------- #
def test_fallback_when_no_headings(tmp_path: Path) -> None:
    path = _build_plain_pdf(tmp_path / "plain.pdf", pages=7)
    parsed = parse_pdf(path)

    assert parsed.parser_mode == "page_fallback"
    assert parsed.max_level == 1
    assert all(section.code.startswith("P") for section in parsed.sections)
    assert all(section.code.strip() for section in parsed.sections)
    # 降级后依然要能切出非空 section_code 的切片
    drafts = chunk_document(parsed)
    assert drafts and all(draft.section_code.strip() for draft in drafts)


def test_encrypted_pdf_rejected(tmp_path: Path) -> None:
    path = _build_encrypted_pdf(tmp_path / "locked.pdf")
    with pytest.raises(AppError) as excinfo:
        parse_pdf(path)
    assert excinfo.value.code is ErrorCode.PDF_ENCRYPTED


def test_non_pdf_rejected(tmp_path: Path) -> None:
    path = tmp_path / "not_a_pdf.pdf"
    path.write_bytes(b"this is definitely not a pdf")
    with pytest.raises(AppError) as excinfo:
        parse_pdf(path)
    assert excinfo.value.code is ErrorCode.PDF_PARSE_FAILED


def test_pdf_without_text_layer_rejected(tmp_path: Path) -> None:
    path = _build_short_text_pdf(tmp_path / "scan.pdf")
    with pytest.raises(AppError) as excinfo:
        parse_pdf(path)
    assert excinfo.value.code is ErrorCode.PDF_NO_TEXT_LAYER


def test_page_limit_enforced(sample_pdf_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        inspect_pdf(sample_pdf_path, max_pages=10)
    assert excinfo.value.code is ErrorCode.UNSUPPORTED_FILE_TYPE


# --------------------------------------------------------------------------- #
# 进度与取消
# --------------------------------------------------------------------------- #
def test_progress_callback_reports_every_page(small_pdf_path: Path) -> None:
    seen: list[tuple[int, int]] = []
    parsed = parse_pdf(small_pdf_path, progress_cb=lambda done, total: seen.append((done, total)))

    assert seen, "必须逐页回调"
    assert seen[-1] == (parsed.page_count, parsed.page_count)
    assert [done for done, _ in seen] == list(range(1, parsed.page_count + 1))


def test_cancel_check_aborts_parse(small_pdf_path: Path) -> None:
    calls = {"count": 0}

    def cancel_check() -> bool:
        calls["count"] += 1
        return calls["count"] > 2

    with pytest.raises(ParseCancelled):
        parse_pdf(small_pdf_path, cancel_check=cancel_check)


def test_markdown_mode_still_parses(small_pdf_path: Path) -> None:
    """pymupdf4llm 增强路径必须「失败也不影响主流程」，这里只要求不抛异常。"""
    parsed = parse_pdf(small_pdf_path, use_markdown=True)
    assert parsed.page_count > 0
    assert parsed.sections

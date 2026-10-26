"""验收测试 · 边界、并发与失败路径（「不崩溃」那一半）。

**本文件验证 PRD 的哪几条**：
- P0-02②：非 PDF / 加密 PDF / 0 页 / 超页数 / 超大文件 → **各自明确错误码，且不 500、不崩溃、不留垃圾文件**
- P0-02③：解析中途取消 → 状态落到 `cancelled`，绝不卡在 `parsing`
- P0-01①：两个 workspace 的文档列表与产物列表互不串台
- P0-03③：目录树每个节点的 `start_page/end_page` 可定位，且切片页码落在所属节点区间内
- P0-05④：`NO_MATCH` 的整条事件流（正文 + citations + artifact + retrieval）**都不出现 `p.N`**
- P0-07②：三种形态的 spec 都不含 HTML/JS（不只是 PPT 要干净）
- P0-10①③：并发 patch 的 `base_version` 乐观锁、单批 20/21 个 op 的边界、畸形 `node_path`
- §5.4 规则②：`error` 事件之后仍必须补发 `turn_end`；客户端中途断连不得泄漏后台任务
- `ChatRequest` 的 query 长度边界（2000 通过 / 2001 拒绝）、空 workspace 提问

为什么单独成一个文件：主链路测试（test_api_smoke / test_acceptance_p0_*）验的是「正常情况
能不能跑通」，本文件验的是「异常输入会不会把服务打穿」。两类断言失败的含义完全不同，
混在一起会让失败原因难以归责。
"""

from __future__ import annotations

import asyncio
import logging
import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from acceptance_support import (
    build_encrypted_pdf,
    create_workspace,
    first_event,
    reply_text,
    scan_markup,
    scan_page_markers,
    seed_workspace_with_pdf,
    sse_events,
    upload_pdf,
    wait_terminal,
    zero_page_pdf_bytes,
)
from app.db.models import DocumentChunk
from app.db.session import get_session_factory
from app.services.pdf_parser import ParseCancelled

RELATED_QUERY = "4.1 Malayan Union 对苏丹的影响"


# --------------------------------------------------------------------------- #
# P0-02②：异常文件必须「明确错误码 + 不崩溃」
# --------------------------------------------------------------------------- #
def _set_setting(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    """改一个配置项并让 Settings 单例重建（get_settings 是 lru_cache）。"""
    from app.config import reset_settings_cache

    monkeypatch.setenv(key, value)
    reset_settings_cache()


async def _post_document(
    client: Any, workspace_id: str, filename: str, content: bytes
) -> Any:
    return await client.post(
        f"/api/workspaces/{workspace_id}/documents",
        files={"file": (filename, content, "application/pdf")},
    )


@pytest.mark.parametrize(
    ("case", "expected_code", "hint"),
    [
        ("encrypted", "PDF_ENCRYPTED", "加密 PDF 必须先解除密码保护"),
        ("zero_page", "PDF_EMPTY", "0 页 PDF 没有可用页面"),
        ("empty_file", "PDF_EMPTY", "空文件不是 PDF"),
        ("too_many_pages", "UNSUPPORTED_FILE_TYPE", "页数超上限"),
        ("too_large", "UNSUPPORTED_FILE_TYPE", "体积超上限"),
    ],
)
async def test_bad_upload_returns_explicit_error_code(
    client: Any,
    sample_pdf_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_code: str,
    hint: str,
) -> None:
    """P0-02②：五类异常文件各自给出明确错误码与中文提示，全部是 4xx，一个 500 都不许有。"""
    workspace_id = await create_workspace(client, f"异常文件-{case}")

    if case == "encrypted":
        content = build_encrypted_pdf(tmp_path / "enc.pdf").read_bytes()
        filename = "encrypted.pdf"
    elif case == "zero_page":
        content, filename = zero_page_pdf_bytes(), "zero.pdf"
    elif case == "empty_file":
        content, filename = b"", "empty.pdf"
    elif case == "too_many_pages":
        # 样本教材 113 页，把上限压到 3 页即可稳定触发（不必造一份超长 PDF）
        _set_setting(monkeypatch, "MAX_PDF_PAGES", "3")
        content, filename = sample_pdf_path.read_bytes(), "too_many_pages.pdf"
    else:  # too_large
        # 上限压到 1MB，避免真的往内存里塞 51MB
        _set_setting(monkeypatch, "MAX_UPLOAD_MB", "1")
        content, filename = b"%PDF-1.4\n" + b"0" * (1200 * 1024), "too_large.pdf"

    response = await _post_document(client, workspace_id, filename, content)

    assert response.status_code == 400, (
        f"{hint}：期望 400，实际 {response.status_code} {response.text[:200]}"
    )
    body = response.json()
    assert body["code"] == expected_code, f"{hint}：期望 {expected_code}，实际 {body}"
    assert body["data"] is None
    assert body["message"].strip(), "必须给中文提示，不能只有错误码"


async def test_corrupt_pdf_returns_clear_error_not_500(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-02②：把 txt 改名成 .pdf 上传，必须给明确错误码，**不许 500、不许抛异常、不许留垃圾文件**。

    这是最容易在演示现场发生的一种用户错误（随手改扩展名）。曾经的实现有三处问题：
    1. `_open_document` 把它归为 `PDF_PARSE_FAILED`（§9.1 映射到 500），客户端输入错误
       被报成服务端故障；
    2. 错误分支里的 `target.unlink()` 在 Windows 上抛 `PermissionError`（pymupdf 仍持有
       文件句柄），未处理异常直接穿透到 ASGI 层 —— 比 500 更糟，连统一错误包装都没了；
    3. 落盘的 `.pdf` 残留，每次失败尝试都泄漏一份磁盘。
    """
    workspace_id = await create_workspace(client, "损坏文件")

    try:
        response = await _post_document(
            client, workspace_id, "renamed_txt.pdf", b"this is plain text, not a pdf\n" * 20
        )
    except Exception as exc:  # noqa: BLE001 - ASGITransport 会把未处理异常原样抛回
        pytest.fail(
            "上传改名后的 txt 时服务端抛出未处理异常"
            f"（{type(exc).__name__}: {exc}）。P0-02② 要求返回明确错误码而不是崩溃。"
        )

    assert response.status_code < 500, (
        f"损坏 PDF 属于客户端输入错误，不应返回 {response.status_code}：{response.text[:200]}"
    )
    body = response.json()
    assert body["code"] in {"UNSUPPORTED_FILE_TYPE", "PDF_PARSE_FAILED"}, body
    assert body["message"].strip()

    uploads = Path(os.environ["DATA_DIR"]) / "uploads"
    leftovers = sorted(p.name for p in uploads.glob("*.pdf")) if uploads.exists() else []
    assert not leftovers, f"体检失败后落盘文件未清理，磁盘泄漏：{leftovers}"


async def test_broken_pdf_with_valid_header_does_not_crash(client: Any) -> None:
    """P0-02② 的硬核变体：文件头是合法的 `%PDF-1.4`、正文全是垃圾。

    这类文件能绕过「魔数前置校验」，必须真的进 PyMuPDF 才会失败 —— 也就是会走到
    `except AppError` 里那个 `unlink` 分支。要验的是：**不许抛未处理异常**
    （Windows 上 PyMuPDF 仍持有句柄，`unlink` 会抛 PermissionError），且错误码明确、
    数据库不残留半成品记录。
    """
    workspace_id = await create_workspace(client, "损坏但有头")
    content = b"%PDF-1.4\n" + b"\x00\xff garbage object stream" * 40

    try:
        response = await _post_document(client, workspace_id, "broken.pdf", content)
    except Exception as exc:  # noqa: BLE001 - 未处理异常会原样抛回测试
        pytest.fail(
            "上传「头合法、正文损坏」的 PDF 时服务端抛出未处理异常"
            f"（{type(exc).__name__}: {exc}）。清理失败不应顶替错误码（P0-02②）。"
        )

    body = response.json()
    assert body["code"] in {"PDF_PARSE_FAILED", "UNSUPPORTED_FILE_TYPE"}, body
    assert body["message"].strip(), "必须给中文提示"
    assert body["data"] is None

    # 半成品记录必须随事务回滚，不能留下 status=failed 却查不到的幽灵文档
    documents = (await client.get(f"/api/workspaces/{workspace_id}/documents")).json()["data"]
    assert documents == [], f"体检失败的文档不应留在库里：{documents}"


async def test_broken_pdf_cleanup_failure_is_observable(
    client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """体检失败后的清理是 best-effort：文件可能因 Windows 句柄未释放而删不掉（已知 P2），
    但**清理失败必须留下 `upload_cleanup_failed` WARNING 日志**，否则磁盘泄漏会变成
    完全静默的问题 —— 线上只会表现为「磁盘慢慢满」，无从排查。

    为什么不断言「一定没有残留」：PyMuPDF 释放句柄的时机取决于 CPython 的 GC，
    实测同一份代码有时能删掉、有时不能（同一套用例跑两次，一次 PASS 一次 XPASS）。
    断言「一定删掉」会变成 flaky 测试，比不测更糟。这里锁的是**可观测性契约**：
    要么文件没了，要么日志里有 `upload_cleanup_failed`，二者必居其一。
    """
    workspace_id = await create_workspace(client, "孤儿文件")

    with caplog.at_level(logging.WARNING):
        await _post_document(
            client, workspace_id, "broken.pdf", b"%PDF-1.4\n" + b"\x00\xff garbage" * 40
        )

    uploads = Path(os.environ["DATA_DIR"]) / "uploads"
    leftovers = sorted(p.name for p in uploads.glob("*.pdf")) if uploads.exists() else []
    if leftovers:
        assert "upload_cleanup_failed" in caplog.text, (
            f"落盘文件 {leftovers} 未被清理，且日志里没有 upload_cleanup_failed —— "
            "清理失败被静默吞掉了（磁盘泄漏将无法排查）"
        )


# --------------------------------------------------------------------------- #
# P0-02③：中途取消必须落到 cancelled
# --------------------------------------------------------------------------- #
async def test_cancel_during_parse_ends_in_cancelled_not_parsing(
    client: Any, small_pdf_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-02③：解析中途点取消 → 终态必须是 `cancelled`，且不能留下切片。

    用「慢速假解析器」替换真实 parse_pdf：真解析 113 页只要 0.2s，取消窗口太小，
    直接测真实路径会变成「和 CPU 抢时间」的随机测试。假解析器按 `cancel_check()`
    逐页检查，与真实实现同一契约（同样的取消回调语义），因此测的是任务的取消编排，
    而不是 fitz 的解析速度。
    """
    import app.services.tasks as tasks_module

    def _slow_parse(path, *, max_pages=None, progress_cb=None, cancel_check=None, use_markdown=False):
        for index in range(500):  # 5 秒，足够稳定地在中途取消
            if cancel_check is not None and cancel_check():
                raise ParseCancelled
            if progress_cb is not None:
                progress_cb(index + 1, 500)
            time.sleep(0.01)
        raise AssertionError("解析跑完了却没被取消：本用例的取消请求没有生效")

    monkeypatch.setattr(tasks_module, "parse_pdf", _slow_parse)

    workspace_id = await create_workspace(client, "取消解析")
    document = await upload_pdf(
        client, workspace_id, small_pdf_path.name, small_pdf_path.read_bytes()
    )

    response = await client.post(f"/api/documents/{document['id']}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["code"] == "OK"

    status = await wait_terminal(client, document["id"], timeout=30.0)
    assert status["status"] == "cancelled", (
        f"取消后终态应为 cancelled，实际 {status['status']}（P0-02③ 要求不卡在 parsing）"
    )
    assert status["error_code"] == "PARSE_TASK_CANCELLED", status
    assert status["progress"] < 100, "取消的任务不应报 100% 完成"

    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(DocumentChunk).where(DocumentChunk.document_id == document["id"])
            )
        ).scalars().all()
    assert rows == [], f"已取消的解析不应写入切片，实际残留 {len(rows)} 条"


async def test_cancel_is_idempotent_after_terminal(
    client: Any, small_pdf_path: Path
) -> None:
    """取消接口必须幂等：对已 ready 的文档再点一次取消，不应报错也不应改状态。"""
    seeded = await seed_workspace_with_pdf(client, small_pdf_path)
    assert seeded["status"]["status"] == "ready"

    response = await client.post(f"/api/documents/{seeded['document_id']}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "ready", "已完成的文档不应被取消改状态"


# --------------------------------------------------------------------------- #
# P0-01①：workspace 之间不串台
# --------------------------------------------------------------------------- #
async def test_workspaces_do_not_leak_documents_or_artifacts(
    client: Any, small_pdf_path: Path
) -> None:
    """P0-01①：两个 workspace 各自上传、各自生成产物，列表互不串台。"""
    first = await seed_workspace_with_pdf(client, small_pdf_path, title="教材 A")
    second = await seed_workspace_with_pdf(client, small_pdf_path, title="教材 B")

    await sse_events(
        client,
        {
            "workspace_id": first["workspace_id"],
            "query": "学习 1.1",
            "artifact_type_override": "ppt",
        },
    )
    await sse_events(
        client,
        {
            "workspace_id": second["workspace_id"],
            "query": "学习 1.1",
            "artifact_type_override": "scene_3d",
        },
    )

    first_documents = (await client.get(f"/api/workspaces/{first['workspace_id']}/documents")).json()["data"]
    second_documents = (await client.get(f"/api/workspaces/{second['workspace_id']}/documents")).json()["data"]
    assert {d["id"] for d in first_documents}.isdisjoint({d["id"] for d in second_documents})

    first_artifacts = (await client.get(f"/api/workspaces/{first['workspace_id']}/artifacts")).json()["data"]
    second_artifacts = (await client.get(f"/api/workspaces/{second['workspace_id']}/artifacts")).json()["data"]
    assert [a["artifact_type"] for a in first_artifacts] == ["ppt"]
    assert [a["artifact_type"] for a in second_artifacts] == ["scene_3d"]
    assert {a["id"] for a in first_artifacts}.isdisjoint({a["id"] for a in second_artifacts})


# --------------------------------------------------------------------------- #
# P0-03③：目录树节点可定位到页码区间
# --------------------------------------------------------------------------- #
def _flatten_toc(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for node in nodes:
        flat.append(node)
        flat.extend(_flatten_toc(node.get("children") or []))
    return flat


async def test_toc_nodes_are_locatable_and_match_chunk_pages(
    client: Any, small_pdf_path: Path
) -> None:
    """P0-03③：点任一目录节点能定位到页码范围，且该节点下的切片页码确实落在区间内。

    如果目录树页码和切片页码对不上，用户点 p.76 的节点却看到 p.70 的引用 ——
    「定位」就是假的。
    """
    seeded = await seed_workspace_with_pdf(client, small_pdf_path)
    document_id = seeded["document_id"]

    document = (await client.get(f"/api/documents/{document_id}")).json()["data"]
    page_count = document["page_count"]
    nodes = _flatten_toc(document["toc_tree"])
    assert nodes, "目录树不能为空"

    for node in nodes:
        assert 1 <= node["start_page"] <= node["end_page"] <= page_count, (
            f"节点 {node['code']} 的页码区间非法：{node['start_page']}-{node['end_page']}"
        )

    chunks = (await client.get(f"/api/documents/{document_id}/chunks", params={"limit": 500})).json()["data"]
    assert chunks, "必须切出切片"

    by_code = {node["code"]: node for node in nodes}
    for chunk in chunks:
        owner = by_code.get(chunk["section_code"])
        assert owner is not None, f"切片 {chunk['chunk_index']} 的 section_code {chunk['section_code']} 不在目录树里"
        assert chunk["page_start"] <= chunk["page_end"] <= owner["end_page"], (
            f"切片 {chunk['chunk_index']}（p.{chunk['page_start']}-{chunk['page_end']}）"
            f"越出了节点 {owner['code']} 的区间 {owner['start_page']}-{owner['end_page']}"
        )


# --------------------------------------------------------------------------- #
# P0-05④：NO_MATCH 整条事件流都不许出现页码
# --------------------------------------------------------------------------- #
async def test_no_match_turn_never_emits_page_marker_anywhere(
    client: Any, small_pdf_path: Path
) -> None:
    """P0-05④：无命中时，用户可见的每一处（正文 / citations / artifact）都不许出现 `p.N`。

    只检查正文是不够的：`citations` 里带 `page_start/page_end`，只要它非空，
    前端 `CitationList` 就会把「p.10」画到用户脸上。

    关于 `retrieval.chunks`：`NO_MATCH` 时后端仍会下发低分候选片段（grader 判的是
    「覆盖度」，候选集是另一回事）。当前前端只渲染 `citations`，因此这些候选不会
    露出页码 —— 本用例据此只对「用户可见字段」下红线，并把候选片段列为遗留风险，
    而不是把它当成违规来断言（否则会为了通过测试去改实现，方向反了）。
    """
    seeded = await seed_workspace_with_pdf(client, small_pdf_path)
    events = await sse_events(
        client, {"workspace_id": seeded["workspace_id"], "query": "如何做红烧肉"}
    )

    retrieval = first_event(events, "retrieval")
    assert retrieval["grader_status"] == "NO_MATCH"
    assert first_event(events, "modality_decision")["action"] == "none"
    assert first_event(events, "citations")["citations"] == [], (
        "NO_MATCH 却下发了引用条目，前端会直接渲染出页码（P0-05④）"
    )
    assert not [name for name, _ in events if name == "artifact"]

    # 用户可见字段：正文 + citations + 所有非 retrieval 事件体
    visible = [payload for name, payload in events if name != "retrieval"]
    hits: list[tuple[str, str]] = []
    for payload in visible:
        hits.extend(scan_page_markers(payload, path="$"))
    assert not hits, f"未覆盖的回合里出现了页码痕迹（P0-05④ 红线）：{hits}"

    assert "课本未覆盖" in reply_text(events)


# --------------------------------------------------------------------------- #
# P0-07②：三种形态都不许夹带 HTML/JS
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("artifact_type", "minimum"),
    [("ppt", 5), ("anim_2d", 2), ("scene_3d", 3)],
)
async def test_all_artifact_types_are_markup_free(
    client: Any, small_pdf_path: Path, artifact_type: str, minimum: int
) -> None:
    """P0-07② / P0-08 / P0-09：三种 AST 都要过契约且递归扫描不到任何 HTML/JS 片段。

    只扫 PPT 是不够的：2D 的 `narration`、3D 的 `annotation_markdown` 同样是自由文本字段，
    是最容易被塞进 `<script>` 的地方。
    """
    seeded = await seed_workspace_with_pdf(client, small_pdf_path)
    events = await sse_events(
        client,
        {
            "workspace_id": seeded["workspace_id"],
            "query": "学习 1.1 的要点",
            "artifact_type_override": artifact_type,
        },
    )
    artifact = first_event(events, "artifact")
    assert artifact["artifact_type"] == artifact_type

    detail = await client.get(f"/api/artifacts/{artifact['artifact_id']}")
    assert detail.status_code == 200
    spec = detail.json()["data"]["spec"]
    assert spec["type"] == artifact_type

    hits = scan_markup(spec)
    assert not hits, f"{artifact_type} 的 spec 里出现了 HTML/JS 片段（P0-07② 红线）：{hits}"

    key = {"ppt": "slides", "anim_2d": "steps", "scene_3d": "nodes"}[artifact_type]
    assert len(spec[key]) >= minimum, f"{artifact_type} 的 {key} 只有 {len(spec[key])} 个"


# --------------------------------------------------------------------------- #
# P0-10：并发 / 上限 / 畸形路径
# --------------------------------------------------------------------------- #
async def _make_artifact(client: Any, pdf_path: Path, override: str = "ppt") -> dict[str, Any]:
    seeded = await seed_workspace_with_pdf(client, pdf_path)
    events = await sse_events(
        client,
        {
            "workspace_id": seeded["workspace_id"],
            "query": "学习 1.1",
            "artifact_type_override": override,
        },
    )
    artifact = first_event(events, "artifact")
    detail = await client.get(f"/api/artifacts/{artifact['artifact_id']}")
    return {
        "workspace_id": seeded["workspace_id"],
        "artifact_id": artifact["artifact_id"],
        "version": detail.json()["data"]["version"],
        "spec": detail.json()["data"]["spec"],
    }


async def test_concurrent_patch_with_same_base_version(
    client: Any, small_pdf_path: Path
) -> None:
    """P0-10 / PRD Q3：两个标签页同时基于同一版本 patch，必须一个成功、一个 **409**。

    现状（缺陷）：`base_version` 乐观锁是「读 → 比 → 写」三步，中间没有原子性。
    两个请求同时读到 v1、同时通过比较，第二个写版本行时撞 `uq_ver` 唯一约束，
    抛出未处理的 `IntegrityError` —— 用户看到的是 500，而不是「画布已被更新，请刷新」。
    数据本身没丢（唯一约束兜住了），但「明确告知用户刷新」这条产品要求没做到。
    """
    deck = await _make_artifact(client, small_pdf_path)
    artifact_id = deck["artifact_id"]

    def patch_payload(marker: str) -> dict[str, Any]:
        return {
            "artifact_id": artifact_id,
            "base_version": deck["version"],
            "ops": [
                {
                    "op": "update_node",
                    "node_path": "/slides/1",
                    "payload": {"speaker_notes": f"并发写入-{marker}"},
                }
            ],
        }

    raw = await asyncio.gather(
        client.post(f"/api/artifacts/{artifact_id}/patch", json=patch_payload("A")),
        client.post(f"/api/artifacts/{artifact_id}/patch", json=patch_payload("B")),
        return_exceptions=True,
    )

    statuses: list[int | str] = []
    for result in raw:
        if isinstance(result, BaseException):
            statuses.append(f"EXC:{type(result).__name__}")
        else:
            statuses.append(result.status_code)

    assert "EXC:IntegrityError" not in statuses, (
        "并发 patch 撞唯一约束抛了未处理的 IntegrityError（应转成 409）："
        f"{[r for r in raw if isinstance(r, BaseException)]}"
    )
    assert sorted(statuses, key=str) == [200, 409], (
        f"并发 patch 期望「一个 200 + 一个 409」，实际 {statuses}"
    )
    conflict = next(r for r in raw if not isinstance(r, BaseException) and r.status_code == 409)
    assert conflict.json()["code"] == "PATCH_BASE_VERSION_CONFLICT"

    versions = (await client.get(f"/api/artifacts/{artifact_id}/versions")).json()["data"]
    assert [row["version"] for row in versions] == [1, 2], (
        f"失败的并发请求不应产生版本，实际 {[row['version'] for row in versions]}"
    )


async def test_patch_ops_limit_20_accepted_21_rejected(
    client: Any, small_pdf_path: Path
) -> None:
    """§5.2：单批最多 20 个 op —— 20 个必须成功，21 个必须被拒（且是 4xx，不是 500）。"""
    deck = await _make_artifact(client, small_pdf_path)
    artifact_id = deck["artifact_id"]

    def ops(count: int) -> list[dict[str, Any]]:
        return [
            {
                "op": "update_node",
                "node_path": "/slides/1",
                "payload": {"speaker_notes": f"第 {index} 条补充"},
                "reason": f"批量 {index}",
            }
            for index in range(count)
        ]

    twenty = await client.post(
        f"/api/artifacts/{artifact_id}/patch",
        json={"artifact_id": artifact_id, "base_version": deck["version"], "ops": ops(20)},
    )
    assert twenty.status_code == 200, f"20 个 op 应被接受：{twenty.text[:200]}"
    assert twenty.json()["data"]["version"] == deck["version"] + 1

    twenty_one = await client.post(
        f"/api/artifacts/{artifact_id}/patch",
        json={
            "artifact_id": artifact_id,
            "base_version": deck["version"] + 1,
            "ops": ops(21),
        },
    )
    assert twenty_one.status_code == 400, f"21 个 op 应被拒（400），实际 {twenty_one.status_code}"
    assert twenty_one.json()["code"] == "REQUEST_INVALID"
    # 被拒的批次不得产生新版本
    versions = (await client.get(f"/api/artifacts/{artifact_id}/versions")).json()["data"]
    assert [row["version"] for row in versions] == [1, 2]


@pytest.mark.parametrize(
    "node_path",
    [
        "//slides/2",          # 双斜杠
        "/slides/abc",         # 下标不是整数
        "/slides/-1",          # 负数下标
        "/slides/2/entities",  # slides 下没有 entities
        "/slides/2/bullets/0", # 超出契约允许的层级
        "/nodes/1",            # ppt 里没有 /nodes 列表
        "/slides/999",         # 越界
        "/slides",             # 列表级路径不能用于 update_node
        "/slides/1/" + "1/" * 40 + "1",  # 超深路径
    ],
)
async def test_malformed_node_path_is_rejected_without_500(
    client: Any, small_pdf_path: Path, node_path: str
) -> None:
    """P0-10①：畸形锚点必须翻译成 400（PATCH_PATH_INVALID / REQUEST_INVALID），绝不 500。"""
    deck = await _make_artifact(client, small_pdf_path)
    artifact_id = deck["artifact_id"]

    response = await client.post(
        f"/api/artifacts/{artifact_id}/patch",
        json={
            "artifact_id": artifact_id,
            "base_version": deck["version"],
            "ops": [
                {
                    "op": "update_node",
                    "node_path": node_path,
                    "payload": {"speaker_notes": "畸形锚点"},
                }
            ],
        },
    )

    assert response.status_code == 400, (
        f"{node_path!r} 期望 400，实际 {response.status_code}：{response.text[:200]}"
    )
    assert response.json()["code"] in {"PATCH_PATH_INVALID", "REQUEST_INVALID"}
    # 失败批次不落版本
    detail = await client.get(f"/api/artifacts/{artifact_id}")
    assert detail.json()["data"]["version"] == deck["version"]


# --------------------------------------------------------------------------- #
# §5.4 规则②：error 之后仍必须 turn_end；断连不得泄漏任务
# --------------------------------------------------------------------------- #
async def test_error_event_is_followed_by_turn_end(
    client: Any, small_pdf_path: Path
) -> None:
    """§5.4 规则②：引擎内部报错时，仍必须补发 `turn_end`。

    不补发的后果不是「少一个事件」，而是前端 `useChatStream` 永远停在流式态、
    输入框永久禁用 —— 用户只能刷新页面。

    触发方式：带一个不存在的 `artifact_id` 锚点（产物已被删除/被另一个标签页删掉，
    是真实会发生的竞态），引擎在 patch 阶段抛 ARTIFACT_NOT_FOUND。
    """
    seeded = await seed_workspace_with_pdf(client, small_pdf_path)
    events = await sse_events(
        client,
        {
            "workspace_id": seeded["workspace_id"],
            "query": "再细讲一下这一点",
            "anchor": {
                "artifact_id": "artifact-already-deleted",
                "node_path": "/slides/1",
                "node_label": "成立背景",
                "page": 12,
                "section_code": "1.1",
            },
        },
    )

    names = [name for name, _ in events]
    assert "error" in names, f"锚点指向不存在的产物时必须报错，实际事件：{names}"
    assert names[-1] == "turn_end", f"error 之后必须补发 turn_end，实际顺序：{names}"
    assert names.index("error") < names.index("turn_end")

    error = first_event(events, "error")
    assert error["code"] == "ARTIFACT_NOT_FOUND"
    assert error["retryable"] is False


async def test_client_disconnect_does_not_leak_background_task() -> None:
    """SSE 客户端中途断连（关标签页）时，后台生产者任务必须被取消。

    为什么直接测 `_event_stream` 而不是走 HTTP：`httpx.ASGITransport` 会先把整个响应体
    读完再返回给调用方，走 HTTP 根本模拟不出「读一半就断」。真正的断连语义发生在
    Starlette 对响应生成器调用 `aclose()` 的那一刻，所以直接驱动生成器更诚实。
    """
    from app.api.chat_stream import _event_stream
    from app.schemas.chat import TokenEvent
    from app.services.agent_engine import TurnRequest

    class _SlowEngine:
        """无限慢速产出 token 的假引擎，用来观察任务是否被回收。"""

        def __init__(self) -> None:
            self.closed = False

        async def stream_turn(self, turn: Any):  # noqa: ANN201
            try:
                for index in range(1000):
                    await asyncio.sleep(0.005)
                    yield "token", TokenEvent(delta=f"t{index}")
            finally:
                self.closed = True

    engine = _SlowEngine()
    baseline = {task for task in asyncio.all_tasks() if not task.done()}

    stream = _event_stream(engine, TurnRequest(workspace_id="ws-1", query="q", turn_id="turn-1"))
    received = 0
    async for _frame in stream:
        received += 1
        if received >= 3:
            break
    await stream.aclose()  # 等价于客户端断开、Starlette 关闭生成器
    await asyncio.sleep(0.1)

    leaked = {task for task in asyncio.all_tasks() if not task.done()} - baseline
    assert not leaked, f"断连后仍有 {len(leaked)} 个后台任务未结束：{[t.get_name() for t in leaked]}"
    assert engine.closed, "断连后引擎的 stream_turn 生成器没有被关闭（后台仍在生成）"


# --------------------------------------------------------------------------- #
# 输入边界：query 长度、空 workspace
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("length", "expected_status"), [(2000, 200), (2001, 400)])
async def test_query_length_boundary(
    client: Any, small_pdf_path: Path, length: int, expected_status: int
) -> None:
    """`ChatRequest.query` 的边界：2000 字符放行，2001 字符必须 400 而不是 500。"""
    seeded = await seed_workspace_with_pdf(client, small_pdf_path)
    response = await client.post(
        "/api/chat/stream",
        json={"workspace_id": seeded["workspace_id"], "query": "历" * length},
    )
    assert response.status_code == expected_status, (
        f"长度 {length} 期望 {expected_status}，实际 {response.status_code}：{response.text[:160]}"
    )
    if expected_status == 200:
        await response.aread()  # 读完释放连接
    else:
        assert response.json()["code"] == "REQUEST_INVALID"


async def test_query_on_empty_workspace_degrades_gracefully(client: Any) -> None:
    """P0-05④ 的极端场景：workspace 里一份文档都没有，提问必须给「课本未覆盖」而不是崩溃。"""
    workspace_id = await create_workspace(client, "空工作区")

    events = await sse_events(client, {"workspace_id": workspace_id, "query": "4.1 讲了什么"})

    assert first_event(events, "retrieval")["grader_status"] == "NO_MATCH"
    assert first_event(events, "retrieval")["chunks"] == []
    assert first_event(events, "modality_decision")["action"] == "none"
    assert not [name for name, _ in events if name == "artifact"]

    reply = reply_text(events)
    assert "课本未覆盖" in reply, f"空工作区应明确提示未覆盖，实际正文：{reply[:200]!r}"
    assert not scan_page_markers(reply), "空工作区不可能有页码，出现即编造"

    names = [name for name, _ in events]
    assert names[-1] == "turn_end"


async def test_unknown_workspace_returns_404_not_500(client: Any) -> None:
    """不存在的 workspace 提问：检索层应安静降级，落库层捕获 404 —— 整轮对话不失败。"""
    events = await sse_events(client, {"workspace_id": "no-such-workspace", "query": "4.1 讲了什么"})
    names = [name for name, _ in events]
    assert names[-1] == "turn_end", f"事件流必须正常收尾，实际：{names}"
    assert first_event(events, "retrieval")["grader_status"] == "NO_MATCH"

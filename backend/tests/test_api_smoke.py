"""全链路 API 冒烟（P0-01 ~ P0-12 的主链路）。

一次测试跑完：建 workspace → 上传 PDF → 轮询到 ready → SSE 对话 → 生成产物 →
连续 3 次 patch（version 1→4）→ 历史版本只读 → 消息落库。

SSE 断言覆盖 §5.4 的三条硬规则：8 类事件齐备、顺序固定、`turn_end` 必发。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.patch_engine import fingerprint

TERMINAL_STATUSES = {"ready", "failed", "cancelled"}


async def _sse_events(client: Any, payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 流解析成 [(event, data)]。注释行（`: ping`）会被忽略。"""
    events: list[tuple[str, dict[str, Any]]] = []
    async with client.stream("POST", "/api/chat/stream", json=payload) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                name: str | None = None
                data: Any = None
                for line in frame.splitlines():
                    if line.startswith("event: "):
                        name = line[len("event: ") :]
                    elif line.startswith("data: "):
                        data = json.loads(line[len("data: ") :])
                if name is not None:
                    events.append((name, data))
    return events


async def _wait_ready(client: Any, document_id: str, timeout: float = 60.0) -> dict[str, Any]:
    import asyncio
    import time

    deadline = time.perf_counter() + timeout
    last: dict[str, Any] = {}
    while time.perf_counter() < deadline:
        response = await client.get(f"/api/documents/{document_id}/status")
        assert response.status_code == 200
        last = response.json()["data"]
        if last["status"] in TERMINAL_STATUSES:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"解析超时，最后状态：{last}")


@pytest.fixture()
async def workspace_with_document(client: Any, sample_pdf_path: Path) -> dict[str, Any]:
    """建 workspace + 上传样本 PDF + 等到 ready。"""
    created = await client.post("/api/workspaces", json={"title": "Sejarah Form 4"})
    assert created.status_code == 200
    workspace = created.json()["data"]

    content = sample_pdf_path.read_bytes()
    uploaded = await client.post(
        f"/api/workspaces/{workspace['id']}/documents",
        files={"file": (sample_pdf_path.name, content, "application/pdf")},
    )
    assert uploaded.status_code == 200, uploaded.text
    document = uploaded.json()["data"]
    assert document["status"] in {"pending", "parsing"}

    status = await _wait_ready(client, document["id"])
    assert status["status"] == "ready", status
    assert status["progress"] == 100
    return {"workspace": workspace, "document": document}


# --------------------------------------------------------------------------- #
# 上传与解析
# --------------------------------------------------------------------------- #
async def test_upload_rejects_non_pdf(client: Any) -> None:
    created = await client.post("/api/workspaces", json={"title": "边界用例"})
    workspace_id = created.json()["data"]["id"]

    response = await client.post(
        f"/api/workspaces/{workspace_id}/documents",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "UNSUPPORTED_FILE_TYPE"
    assert body["data"] is None


async def test_document_toc_has_three_levels(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    document_id = workspace_with_document["document"]["id"]
    response = await client.get(f"/api/documents/{document_id}")
    assert response.status_code == 200
    document = response.json()["data"]

    assert document["status"] == "ready"
    assert document["page_count"] >= 100

    def max_level(nodes: list[dict[str, Any]]) -> int:
        best = 0
        for node in nodes:
            best = max(best, int(node["level"]), max_level(node.get("children") or []))
        return best

    assert max_level(document["toc_tree"]) == 3
    assert len(document["toc_tree"]) == 5


async def test_chunks_endpoint_filters_by_section(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    document_id = workspace_with_document["document"]["id"]
    response = await client.get(f"/api/documents/{document_id}/chunks", params={"section_code": "4.1"})
    assert response.status_code == 200
    chunks = response.json()["data"]
    assert chunks
    assert all(chunk["section_code"].startswith("4.1") for chunk in chunks)
    assert all(chunk["page_start"] >= 1 for chunk in chunks)


# --------------------------------------------------------------------------- #
# SSE 对话
# --------------------------------------------------------------------------- #
async def test_chat_stream_event_order_and_artifact(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    workspace_id = workspace_with_document["workspace"]["id"]
    events = await _sse_events(
        client,
        {"workspace_id": workspace_id, "query": "4.1 Malayan Union 对苏丹的影响", "mode": "syllabus_strict"},
    )

    names = [name for name, _ in events]
    assert names[0] == "turn_start"
    assert names[1] == "retrieval"
    assert names[2] == "modality_decision"
    assert names[-1] == "turn_end"
    assert "token" in names
    assert "artifact" in names
    assert "citations" in names
    # 顺序：artifact 必须在 citations 之前，citations 在 turn_end 之前
    assert names.index("artifact") < names.index("citations") < names.index("turn_end")

    turn_start = dict(events)["turn_start"]
    assert turn_start["demo_mode"] is True, "P0-12③：无 Key 时必须标记演示模式"
    assert turn_start["has_anchor"] is False

    retrieval = dict(events)["retrieval"]
    assert retrieval["grader_status"] in {"SYLLABUS_MATCH", "PARTIAL_OR_BEYOND", "NO_MATCH"}
    assert retrieval["chunks"], "必须带检索片段"

    artifact = dict(events)["artifact"]
    assert artifact["action"] == "create"
    assert artifact["version"] == 1
    assert artifact["spec"]["type"] in {"ppt", "anim_2d", "scene_3d"}

    citations = dict(events)["citations"]["citations"]
    assert citations, "P0-05②：严格模式下必须有引用"
    for citation in citations:
        assert citation["page_start"] >= 1
        assert citation["page_end"] >= citation["page_start"]
    # 引用是「出处清单」，必须按页码升序（retrieval.chunks 才是按相关性排的）
    assert [c["page_start"] for c in citations] == sorted(c["page_start"] for c in citations)

    turn_end = dict(events)["turn_end"]
    assert turn_end["demo_mode"] is True
    assert turn_end["ttft_ms"] < 3000, f"TTFT 超时：{turn_end['ttft_ms']}ms"
    assert turn_end["version"] == 1


async def test_anchor_turn_locks_to_subtree_and_sorts_citations(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    """带锚点追问：检索锁在被点中的小节子树（§6.2 前缀语义），引用按页码升序。"""
    workspace_id = workspace_with_document["workspace"]["id"]
    created = await _sse_events(client, {"workspace_id": workspace_id, "query": "学习 4.1"})
    artifact_id = dict(created)["artifact"]["artifact_id"]

    events = await _sse_events(
        client,
        {
            "workspace_id": workspace_id,
            "query": "这里再细一点",
            "anchor": {
                "artifact_id": artifact_id,
                "node_path": "/slides/2",
                "node_label": "时间脉络",
                "page": 76,
                "section_code": "4.1",
            },
        },
    )
    data = dict(events)
    assert data["retrieval"]["section_scope"] == "4.1"
    assert all(
        chunk["section_code"].startswith("4.1") for chunk in data["retrieval"]["chunks"]
    )
    assert data["modality_decision"]["action"] == "patch_artifact"
    assert data["artifact"]["action"] == "patch"
    assert data["artifact"]["base_version"] == 1
    assert data["artifact"]["version"] == 2

    citations = data["citations"]["citations"]
    assert citations
    assert all(citation["section_code"].startswith("4.1") for citation in citations)
    pages = [citation["page_start"] for citation in citations]
    assert pages == sorted(pages), f"引用未按页码升序：{pages}"


async def test_chat_reply_carries_page_citations(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    workspace_id = workspace_with_document["workspace"]["id"]
    events = await _sse_events(
        client, {"workspace_id": workspace_id, "query": "学习 4.1", "mode": "syllabus_strict"}
    )
    reply = "".join(data["delta"] for name, data in events if name == "token")
    assert "[p." in reply, f"P0-05② 要求每条事实带页码引用，实际正文：{reply[:200]}"
    assert dict(events)["retrieval"]["grader_status"] == "SYLLABUS_MATCH"


async def test_chat_no_match_does_not_fabricate_pages(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    workspace_id = workspace_with_document["workspace"]["id"]
    events = await _sse_events(
        client, {"workspace_id": workspace_id, "query": "量子纠缠的退相干时间是多少"}
    )
    data = dict(events)
    assert data["retrieval"]["grader_status"] == "NO_MATCH"
    assert data["modality_decision"]["action"] == "none"
    assert "artifact" not in data, "无课本依据时不得生成产物"

    reply = "".join(event["delta"] for name, event in events if name == "token")
    assert "课本未覆盖" in reply
    assert "[p." not in reply, "P0-05④：未覆盖时禁止出现页码"


async def test_non_stream_chat_returns_turn_response(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    workspace_id = workspace_with_document["workspace"]["id"]
    response = await client.post(
        "/api/chat", json={"workspace_id": workspace_id, "query": "学习 4.1"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == "OK"
    data = payload["data"]
    assert data["reply_markdown"]
    assert data["knowledge_scope"] in {"syllabus_strict", "hybrid_extended", "none"}
    assert data["citations"]
    assert data["modality_decision"] in {"none", "create_artifact", "patch_artifact"}
    assert data["artifact_payload"]["type"] == "ppt"


# --------------------------------------------------------------------------- #
# patch 与版本
# --------------------------------------------------------------------------- #
async def test_three_patches_bump_version_to_four(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    workspace_id = workspace_with_document["workspace"]["id"]
    events = await _sse_events(
        client, {"workspace_id": workspace_id, "query": "学习 4.1"}
    )
    artifact = dict(events)["artifact"]
    artifact_id = artifact["artifact_id"]

    original_spec = artifact["spec"]
    versions = []
    for index in range(3):
        response = await client.post(
            f"/api/artifacts/{artifact_id}/patch",
            json={
                "artifact_id": artifact_id,
                "base_version": index + 1,
                "ops": [
                    {
                        "op": "update_node",
                        "node_path": "/slides/1",
                        "payload": {"speaker_notes": f"第 {index + 1} 次局部重讲的讲解文本。"},
                        "reason": "细化该页讲解",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        versions.append(response.json()["data"]["version"])

    assert versions == [2, 3, 4], f"P0-11① 要求 1→2→3→4，实际 {versions}"

    latest = await client.get(f"/api/artifacts/{artifact_id}")
    latest_spec = latest.json()["data"]["spec"]
    assert latest.json()["data"]["version"] == 4
    # 只有被锚定的第 2 页变了，其余页字节级不变（P0-10②）
    assert fingerprint(latest_spec["slides"][0]) == fingerprint(original_spec["slides"][0])
    assert fingerprint(latest_spec["slides"][2]) == fingerprint(original_spec["slides"][2])
    assert latest_spec["slides"][1]["speaker_notes"].startswith("第 3 次")

    version_list = await client.get(f"/api/artifacts/{artifact_id}/versions")
    rows = version_list.json()["data"]
    assert [row["version"] for row in rows] == [1, 2, 3, 4]
    assert all(row["patchable"] is True for row in rows)

    first = await client.get(f"/api/artifacts/{artifact_id}", params={"version": 1})
    assert first.json()["data"]["spec"]["slides"][1]["speaker_notes"] == original_spec["slides"][1]["speaker_notes"]


async def test_patch_base_version_conflict(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    workspace_id = workspace_with_document["workspace"]["id"]
    events = await _sse_events(client, {"workspace_id": workspace_id, "query": "学习 4.1"})
    artifact_id = dict(events)["artifact"]["artifact_id"]

    response = await client.post(
        f"/api/artifacts/{artifact_id}/patch",
        json={
            "artifact_id": artifact_id,
            "base_version": 9,
            "ops": [
                {
                    "op": "update_node",
                    "node_path": "/slides/1",
                    "payload": {"speaker_notes": "基于过期版本的补丁"},
                }
            ],
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "PATCH_BASE_VERSION_CONFLICT"


async def test_patch_invalid_path_rejected(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    workspace_id = workspace_with_document["workspace"]["id"]
    events = await _sse_events(client, {"workspace_id": workspace_id, "query": "学习 4.1"})
    artifact_id = dict(events)["artifact"]["artifact_id"]

    response = await client.post(
        f"/api/artifacts/{artifact_id}/patch",
        json={
            "artifact_id": artifact_id,
            "base_version": 1,
            "ops": [
                {
                    "op": "update_node",
                    "node_path": "/slides/99",
                    "payload": {"speaker_notes": "越界锚点"},
                }
            ],
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "PATCH_PATH_INVALID"


# --------------------------------------------------------------------------- #
# 会话恢复与清理
# --------------------------------------------------------------------------- #
async def test_messages_persisted_for_session_restore(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    workspace_id = workspace_with_document["workspace"]["id"]
    await _sse_events(client, {"workspace_id": workspace_id, "query": "学习 4.1"})

    response = await client.get(f"/api/workspaces/{workspace_id}/messages")
    messages = response.json()["data"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content_markdown"] == "学习 4.1"
    assert messages[1]["demo_mode"] is True
    assert messages[1]["citations"]


async def test_delete_workspace_cascades(
    client: Any, workspace_with_document: dict[str, Any]
) -> None:
    workspace_id = workspace_with_document["workspace"]["id"]
    document_id = workspace_with_document["document"]["id"]

    deleted = await client.delete(f"/api/workspaces/{workspace_id}")
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True

    missing = await client.get(f"/api/workspaces/{workspace_id}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "WORKSPACE_NOT_FOUND"

    # 文档随工作区级联删除（P0-01③）
    document = await client.get(f"/api/documents/{document_id}")
    assert document.status_code == 404

"""验收测试的共享工具（不是测试文件，不会被 pytest 收集）。

三条自我约束：
1. **关键断言独立实现**：SSE 解析、字节级比对、HTML/JS 扫描都在这里重新写一遍，
   不 import `app.services.patch_engine.assert_only_subtree_changed` 之类的被测工具 ——
   用被测代码验证被测代码等于自己验自己。
2. 只依赖 HTTP 契约与 DB 只读查询，不碰 `app.services.*` 的内部实现（compose_reply 除外，
   那是「双模式回答」这条 P0 的唯一入口，必须直接验）。
3. 所有等待都有超时上限，避免 CI 卡死。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

# P0-07② 的注入痕迹清单。与实现侧刻意不同源：这里从 PRD「严禁 LLM 直接生成 HTML/CSS/JS」
# 的角度重新列举，命中任意一条即视为 spec 里混进了渲染代码。
FORBIDDEN_MARKUP: tuple[str, ...] = (
    "<script",
    "<html",
    "<div",
    "<span",
    "<style",
    "<iframe",
    "onerror=",
    "onclick=",
    "javascript:",
    "function(",
    "=>",
    "import ",
    "three.",
    "document.",
    "window.",
)

# 「课本未覆盖」的页码痕迹：任何 p.N / [p.N] / p.N-M 形式都算
PAGE_MARKER_RE = re.compile(r"p\.\s?\d+")
# 事实行的页码引用：严格模式要求 [p.页码]
BRACKET_PAGE_RE = re.compile(r"\[p\.(\d+)\]")

TERMINAL_STATUSES = frozenset({"ready", "failed", "cancelled"})

# P0-05④ 的无关问题样本：全部与马来西亚历史教材无任何语义关系，
# 覆盖中英混排、含数字、含「怎么/如何/为什么」等容易被结构启发式误命中的措辞。
UNRELATED_QUERIES: tuple[str, ...] = (
    "量子纠缠的退相干时间是多少",
    "如何做红烧肉",
    "Python 的 asyncio 事件循环怎么实现",
    "光合作用的暗反应发生在哪里",
    "微积分里的泰勒展开怎么证明",
    "推荐几部好看的科幻电影",
    "帮我写一首关于春天的五言绝句",
    "今天上海到北京的高铁几点发车",
    "心脏搭桥手术的恢复期多久",
    "怎么练习三分球投篮姿势",
    "矩阵特征值的几何意义是什么",
    "怎么用 Rust 写一个 WebAssembly 模块",
    "感冒发烧应该吃什么药",
    "为什么天空是蓝色的",
)


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #
def parse_sse_frames(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """把原始 SSE 文本解析成 [(event, data)]。

    只认 `event: ` / `data: ` 两种字段，注释行（`: ping`）自然被忽略 ——
    这与浏览器 EventSource 的行为一致，因此可以用来断言「保活行不会污染事件流」。
    """
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in raw.split("\n\n"):
        name: str | None = None
        data: str | None = None
        for line in frame.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        if name is None:
            continue
        events.append((name, json.loads(data) if data else {}))
    return events


async def sse_events(
    client: Any,
    payload: dict[str, Any],
    *,
    stop_after: int | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """跑一次 SSE 对话并返回全部事件。`stop_after` 用于「读到一半就断连」的场景。"""
    buffer = ""
    events: list[tuple[str, dict[str, Any]]] = []
    async with client.stream("POST", "/api/chat/stream", json=payload) as response:
        assert response.status_code == 200, await response.aread()
        assert response.headers["content-type"].startswith("text/event-stream")
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                name: str | None = None
                data: str | None = None
                for line in frame.splitlines():
                    if line.startswith("event:"):
                        name = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data = line[len("data:") :].strip()
                if name is None:
                    continue
                events.append((name, json.loads(data) if data else {}))
                if stop_after is not None and len(events) >= stop_after:
                    return events
    return events


def first_event(events: list[tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    """取第一个指定名字的事件体。不存在则给出带全部事件名的断言信息。"""
    for event_name, payload in events:
        if event_name == name:
            return payload
    raise AssertionError(f"事件流里没有 {name}；实际事件：{[n for n, _ in events]}")


def reply_text(events: list[tuple[str, dict[str, Any]]]) -> str:
    """把 token 事件拼回完整正文（前端渲染出来的就是这段）。"""
    return "".join(payload.get("delta", "") for name, payload in events if name == "token")


# --------------------------------------------------------------------------- #
# 文档上传与状态轮询
# --------------------------------------------------------------------------- #
async def upload_pdf(
    client: Any, workspace_id: str, filename: str, content: bytes
) -> dict[str, Any]:
    response = await client.post(
        f"/api/workspaces/{workspace_id}/documents",
        files={"file": (filename, content, "application/pdf")},
    )
    assert response.status_code == 200, f"上传失败：{response.status_code} {response.text}"
    body = response.json()
    assert body["code"] == "OK", body
    return body["data"]


async def wait_terminal(client: Any, document_id: str, timeout: float = 60.0) -> dict[str, Any]:
    """轮询到终态。超时不算通过 —— 卡在 parsing 本身就是 P0-02 的不通过。"""
    deadline = time.perf_counter() + timeout
    last: dict[str, Any] = {}
    while time.perf_counter() < deadline:
        response = await client.get(f"/api/documents/{document_id}/status")
        assert response.status_code == 200
        last = response.json()["data"]
        if last["status"] in TERMINAL_STATUSES:
            return last
        import asyncio

        await asyncio.sleep(0.02)
    raise AssertionError(f"{timeout}s 内未进入终态，最后状态：{last}")


async def create_workspace(client: Any, title: str) -> str:
    response = await client.post("/api/workspaces", json={"title": title})
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


async def seed_workspace_with_pdf(
    client: Any, pdf_path: Any, *, title: str = "Sejarah Form 4"
) -> dict[str, Any]:
    """建 workspace → 上传 PDF → 等到 ready，返回 {workspace_id, document_id, status}。"""
    workspace_id = await create_workspace(client, title)
    document = await upload_pdf(client, workspace_id, pdf_path.name, pdf_path.read_bytes())
    status = await wait_terminal(client, document["id"])
    return {
        "workspace_id": workspace_id,
        "document_id": document["id"],
        "status": status,
    }


# --------------------------------------------------------------------------- #
# 独立实现的结构比对 / 扫描
# --------------------------------------------------------------------------- #
def ordered_json(value: Any) -> str:
    """保留键序的紧凑 JSON 文本。

    为什么不 sort_keys：P0-10② 要求「字节级不变」，键序变化也算变化
    （前端 diff 会误判整页重绘）。sort_keys=True 会把这个缺陷掩盖掉。
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def replace_at_path(spec: dict[str, Any], index: int, marker: str) -> str:
    """把 /slides/{index} 挖空后序列化整棵树 —— 用来断言「其余部分一个字节都没动」。"""
    clone = json.loads(json.dumps(spec))
    clone["slides"][index] = marker
    return ordered_json(clone)


def scan_markup(value: Any, path: str = "$") -> list[tuple[str, str]]:
    """递归扫描所有字符串（含 dict 的键），返回 [(路径, 命中的标记)]。"""
    hits: list[tuple[str, str]] = []
    if isinstance(value, str):
        lowered = value.lower()
        for marker in FORBIDDEN_MARKUP:
            if marker in lowered:
                hits.append((path, marker))
    elif isinstance(value, dict):
        for key, item in value.items():
            hits.extend(scan_markup(key, f"{path}.<key>"))
            hits.extend(scan_markup(item, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            hits.extend(scan_markup(item, f"{path}[{index}]"))
    return hits


def scan_page_markers(value: Any, path: str = "$") -> list[tuple[str, str]]:
    """递归扫描所有字符串里的 `p.N` 页码痕迹，返回 [(路径, 命中片段)]。

    用于 P0-05④ 的「整条事件流都不许出现页码」：只看正文会漏掉 citations /
    artifact / retrieval 等旁路字段，而用户看到的正是这些拼起来的整个界面。
    """
    hits: list[tuple[str, str]] = []
    if isinstance(value, str):
        for match in PAGE_MARKER_RE.finditer(value):
            hits.append((path, match.group(0)))
    elif isinstance(value, dict):
        for key, item in value.items():
            hits.extend(scan_page_markers(item, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            hits.extend(scan_page_markers(item, f"{path}[{index}]"))
    return hits


def fact_lines(reply: str) -> list[str]:
    """正文里的事实条目（Markdown 无序列表项）。"""
    return [line.strip() for line in reply.splitlines() if line.strip().startswith("- ")]


# --------------------------------------------------------------------------- #
# 特殊 PDF 构造
# --------------------------------------------------------------------------- #
def zero_page_pdf_bytes() -> bytes:
    """0 页 PDF。PyMuPDF 拒绝保存 0 页文档，因此手工构造最小合法结构。"""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj\n"
        b"trailer << /Root 1 0 R /Size 3 >>\n%%EOF\n"
    )


def build_pdf_without_headings(path: Any, pages: int = 6) -> Any:
    """没有任何章节编号的 PDF：验证 P0-03 的 page_fallback 兜底路径。"""
    import pymupdf

    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page(width=595, height=842)
        page.insert_text(
            (56, 72),
            f"这是第 {index + 1} 页的连续叙述文本，用来模拟没有章节编号的扫描件转写稿。"
            "内容只是填充，目的是让文本层足够长，从而触发解析而不是被判定为无文本层。",
            fontname="china-s",
            fontsize=10,
        )
    document.save(str(path))
    document.close()
    return path


def build_encrypted_pdf(path: Any) -> Any:
    import pymupdf

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

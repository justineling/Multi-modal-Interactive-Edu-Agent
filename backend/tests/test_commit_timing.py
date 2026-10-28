"""写接口的提交时序回归：「返回 200 但紧接着读不到」。

**被测缺陷（本文件存在的唯一原因）**

`app/db/session.py::get_session` 在 `yield` **之后**才 `commit()`。FastAPI ≥ 0.106 把
yield 依赖的收尾放在响应**发出之后**执行 —— 见 `fastapi/routing.py::request_response`：

```python
async with AsyncExitStack() as request_stack:      # ← 依赖的退出栈
    async with AsyncExitStack() as function_stack:
        response = await f(request)
    await response(scope, receive, send)           # ← 先发响应
    # 退出 request_stack 时才跑 get_session 的 commit   ← 后提交
```

于是「客户端收到 200」与「数据真正 commit」之间存在一个窗口。窗口内到达的下一个请求
（新 session、新连接）读不到刚写的数据 —— 表现为 `POST /api/workspaces` 返回 200 且带回
新 id，紧接着的 `GET /api/workspaces/{id}` 却是 404 `WORKSPACE_NOT_FOUND`。

人手操作的时间间隔（> 50ms）把这个窗口盖住了；前端「建区成功后立即加载文档列表」是
程序化操作，间隔可能远小于 50ms，所以它是真会踩到的线上问题，不是理论洁癖。

**为什么断言必须挂在 `send` 上，而不是「POST 之后再来一次 GET」**

`httpx.ASGITransport` 会 `await` 整个 ASGI app —— 也就是等依赖退出栈（含 commit）跑完
才把响应交回来。用它写「POST 完再 GET」，无论修没修都是绿的，测不到任何东西。

所以本文件把探针挂在 `send` 上：响应字节交给客户端的**同一刻**，用**新会话**去读库。
这正是真实 uvicorn 下客户端紧接着发下一个请求时的可见性条件，而且完全确定 —— 修复前
100% 复现，不依赖概率。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.db import repository
from app.db.models import Artifact, Document, Workspace
from app.db.session import get_session_factory

# 建区后立即读的循环次数。100 次 × 100% 复现率，不需要靠概率撞。
CREATE_READ_ITERATIONS = 100

Checker = Callable[[dict[str, Any]], Awaitable[None]]


async def _fresh_read(model: type[Any], primary_key: str) -> Any:
    """用**新会话**读一行 —— 模拟「客户端紧接着发来的下一个请求」的可见性条件。

    必须新开会话：复用同一个 session 会命中 identity map，读回内存里的对象，
    看不见「没提交」这件事。
    """
    factory = get_session_factory()
    async with factory() as session:
        return await session.get(model, primary_key)


class ResponseMomentProbe:
    """ASGI 探针：在响应字节交给客户端的同一刻执行 `checker`。

    本身就是一个合法 ASGI app（可以直接塞给 `ASGITransport`），只拦截被 `watch()`
    点名的那一个 `METHOD path`，其余请求原样透传，零开销。
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self._method: str | None = None
        self._path: str | None = None
        self._checker: Checker | None = None
        self.failures: list[str] = []
        self.fired = 0

    def watch(self, method: str, path: str, checker: Checker) -> None:
        """指定要探针的请求与探针逻辑。`path` 必须精确匹配（含真实 id）。"""
        self._method = method.upper()
        self._path = path
        self._checker = checker
        self.failures.clear()
        self.fired = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or self._checker is None
            or scope["method"].upper() != self._method
            or scope["path"] != self._path
        ):
            await self._app(scope, receive, send)
            return

        chunks: list[bytes] = []

        async def _send(message: dict[str, Any]) -> None:
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            is_final = not message.get("more_body", False)
            # 先转发给客户端，再跑探针 —— 探针看到的就是「客户端已经拿到响应」那一刻。
            await send(message)
            if not is_final:
                return
            self.fired += 1
            try:
                payload = json.loads(b"".join(chunks) or b"null")
                await self._checker(payload)
            except Exception as exc:  # noqa: BLE001 - 探针的失败要攒起来交给断言，
                # 不能在这里抛出：抛出会顶替掉真实响应，把「数据没提交」伪装成 500。
                self.failures.append(f"{type(exc).__name__}: {exc}")

        await self._app(scope, receive, _send)

    def assert_all_passed(self, expected_firings: int, scenario: str) -> None:
        assert self.fired == expected_firings, (
            f"{scenario}：探针预期触发 {expected_firings} 次，实际 {self.fired} 次"
            "（探针没挂上，断言等于没跑）"
        )
        assert self.failures == [], (
            f"{scenario}：{len(self.failures)}/{self.fired} 次在「响应已发出」的那一刻读不到"
            f"刚写的数据 —— 写接口没有在返回前 commit。首个失败：{self.failures[0]}"
        )


@dataclass
class ProbedClient:
    """带探针的客户端。`client` 与 `probe` 必须成对使用。"""

    client: Any
    probe: ResponseMomentProbe


@pytest.fixture()
async def probed(db_env: Path) -> AsyncIterator[ProbedClient]:
    """包了 `ResponseMomentProbe` 的 httpx 客户端。

    依赖 `db_env` 拿到临时库（`client` 夹具直连原始 app，没有探针，本文件用不了它）。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app as fastapi_app

    probe = ResponseMomentProbe(fastapi_app)
    async with AsyncClient(
        transport=ASGITransport(app=probe), base_url="http://testserver"
    ) as async_client:
        yield ProbedClient(client=async_client, probe=probe)


async def _seed_artifact(
    workspace_title: str, spec: dict[str, Any]
) -> tuple[str, str]:
    """测试自己的造数：直接走仓储层建 workspace + artifact + v1 版本。

    不经过 SSE 生成（那要跑一遍完整 agent 流程，与本文件要验的东西无关）。
    这里必须自己 commit —— 造数代码不是被测对象。
    """
    factory = get_session_factory()
    async with factory() as session:
        workspace = await repository.create_workspace(session, workspace_title)
        artifact = await repository.create_artifact(
            session,
            workspace_id=workspace.id,
            title="提交时序",
            artifact_type="ppt",
            spec_json=spec,
        )
        await repository.append_artifact_version(
            session, artifact_id=artifact.id, spec_json=spec
        )
        await session.commit()
        return workspace.id, artifact.id


# --------------------------------------------------------------------------- #
# 1. 建区后立即读
# --------------------------------------------------------------------------- #
async def test_workspace_readable_at_response_moment(probed: ProbedClient) -> None:
    """`POST /api/workspaces` 的响应一发出，新会话就必须能读到这个工作区。

    修复前的实测复现率：本用例 100/100 失败（真实 uvicorn + 人手脚本约 4.3%）。
    """

    async def check(payload: dict[str, Any]) -> None:
        workspace_id = payload["data"]["id"]
        row = await _fresh_read(Workspace, workspace_id)
        assert row is not None, (
            f"建区响应已带回 id={workspace_id}，但新会话读不到这一行（未 commit）"
        )

    probed.probe.watch("POST", "/api/workspaces", check)

    for index in range(CREATE_READ_ITERATIONS):
        response = await probed.client.post(
            "/api/workspaces", json={"title": f"时序-{index}"}
        )
        assert response.status_code == 200, (
            f"第 {index} 次建区就失败了：{response.status_code} {response.text[:200]}"
        )

    probed.probe.assert_all_passed(CREATE_READ_ITERATIONS, "建区后立即读")


# --------------------------------------------------------------------------- #
# 2. 上传文档后立即查状态
# --------------------------------------------------------------------------- #
async def test_document_readable_right_after_upload(
    probed: ProbedClient, small_pdf_path: Path
) -> None:
    """上传响应一发出，新会话就必须能读到这条 document（否则轮询 `/status` 会 404）。"""
    created = await probed.client.post("/api/workspaces", json={"title": "上传时序"})
    assert created.status_code == 200
    workspace_id = created.json()["data"]["id"]

    async def check(payload: dict[str, Any]) -> None:
        document_id = payload["data"]["id"]
        row = await _fresh_read(Document, document_id)
        assert row is not None, (
            f"上传响应已带回 document_id={document_id}，但新会话读不到这一行（未 commit）"
        )

    upload_path = f"/api/workspaces/{workspace_id}/documents"
    probed.probe.watch("POST", upload_path, check)

    content = small_pdf_path.read_bytes()
    uploaded = await probed.client.post(
        upload_path, files={"file": ("small.pdf", content, "application/pdf")}
    )
    assert uploaded.status_code == 200, uploaded.text[:300]
    probed.probe.assert_all_passed(1, "上传文档后立即读")

    # 字面要求的那一步也照做一遍：返回后立刻查状态，不能 404。
    document_id = uploaded.json()["data"]["id"]
    status = await probed.client.get(f"/api/documents/{document_id}/status")
    assert status.status_code == 200, (
        f"上传返回后立即查状态得到 {status.status_code}：{status.text[:200]}"
    )


# --------------------------------------------------------------------------- #
# 3. patch 产物后立即 GET
# --------------------------------------------------------------------------- #
async def test_artifact_version_bumped_at_patch_response_moment(
    probed: ProbedClient, slide_deck_dict: dict[str, Any]
) -> None:
    """patch 响应一发出，新会话读到的版本号就必须已经是新的。"""
    _, artifact_id = await _seed_artifact("patch 时序", slide_deck_dict)

    async def check(payload: dict[str, Any]) -> None:
        expected_version = payload["data"]["version"]
        row = await _fresh_read(Artifact, artifact_id)
        assert row is not None, f"产物 {artifact_id} 在新会话里消失了"
        assert row.version == expected_version, (
            f"patch 响应说已到 v{expected_version}，新会话读到 v{row.version}（未 commit）"
        )

    probed.probe.watch("POST", f"/api/artifacts/{artifact_id}/patch", check)

    patched = await probed.client.post(
        f"/api/artifacts/{artifact_id}/patch",
        json={
            "artifact_id": artifact_id,
            "base_version": 1,
            "ops": [
                {
                    "op": "update_node",
                    "node_path": "/slides/1",
                    "payload": {"title": "改过的标题"},
                }
            ],
        },
    )
    assert patched.status_code == 200, patched.text[:300]
    probed.probe.assert_all_passed(1, "patch 后立即读")

    detail = await probed.client.get(f"/api/artifacts/{artifact_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["version"] == 2, "patch 返回后立即 GET 读到的仍是旧版本"


# --------------------------------------------------------------------------- #
# 4. 删除产物后立即 GET（同一类缺陷的删除方向）
# --------------------------------------------------------------------------- #
async def test_artifact_gone_at_delete_response_moment(
    probed: ProbedClient, slide_deck_dict: dict[str, Any]
) -> None:
    """删除响应一发出，新会话就必须读不到它 —— 否则「删了还在」会被刷新页面打回原形。"""
    _, artifact_id = await _seed_artifact("删除时序", slide_deck_dict)

    async def check(payload: dict[str, Any]) -> None:
        assert payload["data"]["deleted"] is True
        row = await _fresh_read(Artifact, artifact_id)
        assert row is None, (
            f"删除响应已返回，但新会话仍能读到产物 {artifact_id}（未 commit）"
        )

    probed.probe.watch("DELETE", f"/api/artifacts/{artifact_id}", check)

    deleted = await probed.client.delete(f"/api/artifacts/{artifact_id}")
    assert deleted.status_code == 200, deleted.text[:300]
    probed.probe.assert_all_passed(1, "删除后立即读")

    detail = await probed.client.get(f"/api/artifacts/{artifact_id}")
    assert detail.status_code == 404, (
        f"删除返回后立即 GET 得到 {detail.status_code}，应为 404"
    )

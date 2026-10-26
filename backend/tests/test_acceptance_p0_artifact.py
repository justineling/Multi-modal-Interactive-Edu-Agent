"""验收测试 · 产物契约、增量 patch、版本管理与 mock 确定性。

**本文件验证 PRD 的哪几条**：
- P0-07②：单次生成 ≥5 页，`SlideDeckSpec` 通过 Pydantic 校验，且 spec 内**不含任何 HTML/JS 字符串**
- P0-10②：patch 后非锚定节点**字节级不变**（本文件用独立实现的「挖空 + 保留键序序列化」比对，
  不复用 `patch_engine.assert_only_subtree_changed`，否则等于自己验自己）
- P0-11①②③：连续 patch 版本 1→2→3→4；历史版本可只读取回；历史版本不可被覆盖
- P0-12②：mock 产物确定性 —— **跨进程**验证（不同 `PYTHONHASHSEED` 下 AST 指纹必须一致）
- P0-06②：用户显式说「用 3D」时 100% 遵循（端到端，不只是路由器单测）

为什么要跨进程测 P0-12②：单进程内连续生成两次相等，只能证明「没有随机数」；
一旦有人误用内置 `hash()`（受 `PYTHONHASHSEED` 影响），单进程测试照样全绿，
而重启服务后产物就变了。只有换 `PYTHONHASHSEED` 起两个进程比对才能抓住。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from acceptance_support import (
    first_event,
    ordered_json,
    replace_at_path,
    scan_markup,
    seed_workspace_with_pdf,
    sse_events,
)
from app.schemas.artifacts import parse_artifact_spec

BACKEND_ROOT = Path(__file__).resolve().parents[1]
QUERY = "学习 4.1"


@pytest.fixture()
async def deck(client: Any, sample_pdf_path: Path) -> dict[str, Any]:
    """建 workspace → 上传教材 → 生成一份 PPT 产物。

    **`spec` 一律通过 `GET /api/artifacts/{id}` 取**，而不是 SSE 里的 artifact 事件。
    原因是踩过的坑：SSE 帧走 `canonical_json`（sort_keys=True），REST 出口早期直接返回
    库内插入序，两者键序不同、内容相同。拿 SSE 的 spec 与 GET 的历史版本做「字节级」
    比对，会在内容完全没变时误报「历史被改写」（详见 QA 报告 §3-B5）。
    现在 REST 出口也已统一走 `canonical_tree`，两个出口字节一致；但本文件仍坚持
    「字节级断言两端必须同出口」这条原则 —— 不把断言绑在某个序列化实现细节上。
    """
    seeded = await seed_workspace_with_pdf(client, sample_pdf_path)
    events = await sse_events(
        client, {"workspace_id": seeded["workspace_id"], "query": QUERY, "artifact_type_override": "ppt"}
    )
    artifact = first_event(events, "artifact")
    assert artifact["action"] == "create"

    detail = await client.get(f"/api/artifacts/{artifact['artifact_id']}")
    assert detail.status_code == 200
    return {
        "workspace_id": seeded["workspace_id"],
        "artifact": artifact,
        "spec": detail.json()["data"]["spec"],
        "version": detail.json()["data"]["version"],
    }


# --------------------------------------------------------------------------- #
# P0-07②：PPT 契约
# --------------------------------------------------------------------------- #
async def test_ppt_spec_is_valid_and_markup_free(deck: dict[str, Any]) -> None:
    spec = deck["spec"]
    assert spec["type"] == "ppt"
    assert len(spec["slides"]) >= 5, f"P0-07② 要求单次生成 ≥5 页，实际 {len(spec['slides'])} 页"

    # 契约校验：必须能被 Pydantic 吃下（前端渲染器的前提）
    model = parse_artifact_spec(spec)
    assert model.type == "ppt"

    # 注入扫描：独立实现的递归扫描，覆盖 dict 的键与所有嵌套字符串
    hits = scan_markup(spec)
    assert not hits, f"spec 里出现了 HTML/JS 片段（P0-07② 红线）：{hits}"

    # 6 种布局必须覆盖到（前端每种布局都要有渲染机会）
    layouts = {slide["layout"] for slide in spec["slides"]}
    assert layouts == {
        "title_hero",
        "bullet_points",
        "timeline",
        "comparison",
        "cause_effect",
        "quiz",
    }, f"布局覆盖不全：{sorted(layouts)}"


# --------------------------------------------------------------------------- #
# P0-10②：字节级不变（独立实现）
# --------------------------------------------------------------------------- #
async def test_patch_changes_only_anchored_slide_bytes(client: Any, deck: dict[str, Any]) -> None:
    """patch /slides/1 后：其余页与 topic 的 JSON 文本必须逐字节相同。"""
    artifact_id = deck["artifact"]["artifact_id"]
    before = deck["spec"]
    anchored = 1

    response = await client.post(
        f"/api/artifacts/{artifact_id}/patch",
        json={
            "artifact_id": artifact_id,
            "base_version": deck["version"],
            "ops": [
                {
                    "op": "update_node",
                    "node_path": f"/slides/{anchored}",
                    "payload": {"speaker_notes": "针对这一页的补充讲解。", "bullets": ["要点甲", "要点乙"]},
                    "reason": "锚点追问",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    # 重新读库再比对：patch 响应里的 spec 是内存对象，不能代表落库后的字节
    after = (await client.get(f"/api/artifacts/{artifact_id}")).json()["data"]["spec"]

    # 1) 挖空被锚定的那一页后，整棵树序列化结果必须完全一致（键序也算）
    assert replace_at_path(before, anchored, "<ANCHORED>") == replace_at_path(
        after, anchored, "<ANCHORED>"
    ), "非锚定节点发生了内容/键序变化（P0-10② 不通过）"

    # 2) 逐页再确认一次，失败时能直接指出是哪一页被动了
    for index, slide in enumerate(before["slides"]):
        if index == anchored:
            continue
        assert ordered_json(after["slides"][index]) == ordered_json(slide), (
            f"/slides/{index} 被意外修改（锚点是 /slides/{anchored}）"
        )

    # 3) 锚定页确实变了（否则「不变」的断言没有意义）
    assert ordered_json(after["slides"][anchored]) != ordered_json(before["slides"][anchored])
    assert after["slides"][anchored]["speaker_notes"].startswith("针对这一页")
    # 4) 字段一个都不能丢（IMPL-NOTES §F1：update_node 是字段级 merge）
    for field in ("node_id", "layout", "title", "source_ref"):
        assert after["slides"][anchored][field] == before["slides"][anchored][field]


async def test_focus_camera_patch_does_not_touch_content(
    client: Any, sample_pdf_path: Path
) -> None:
    """IMPL-NOTES §四.5 的已知正确行为：focus_camera 只走旁路，spec 必须一字未改。"""
    seeded = await seed_workspace_with_pdf(client, sample_pdf_path)
    events = await sse_events(
        client,
        {"workspace_id": seeded["workspace_id"], "query": "这个建筑的内部结构", "artifact_type_override": "scene_3d"},
    )
    artifact = first_event(events, "artifact")
    artifact_id = artifact["artifact_id"]
    detail = await client.get(f"/api/artifacts/{artifact_id}")
    before = detail.json()["data"]["spec"]

    response = await client.post(
        f"/api/artifacts/{artifact_id}/patch",
        json={
            "artifact_id": artifact_id,
            "base_version": detail.json()["data"]["version"],
            "ops": [
                {"op": "focus_camera", "node_path": "/camera_tour/1", "payload": {"preset_name": "剖面"}}
            ],
        },
    )
    assert response.status_code == 200, response.text
    after = (await client.get(f"/api/artifacts/{artifact_id}")).json()["data"]["spec"]
    assert ordered_json(after) == ordered_json(before), "focus_camera 不应改动任何内容字段"


# --------------------------------------------------------------------------- #
# P0-11：版本管理
# --------------------------------------------------------------------------- #
async def test_versions_append_only_and_old_version_not_overwritable(
    client: Any, deck: dict[str, Any]
) -> None:
    """P0-11①②③：版本单调递增、历史只读取回、历史不被后续 patch 改写。

    **基线必须从 `GET /api/artifacts/{id}` 取，不能拿 SSE 里的 spec**（踩过的坑）：
    SSE 帧走 `canonical_json`（sort_keys=True），而 GET 返回库内插入序 —— 两者键序不同、
    内容相同。拿 SSE 的 spec 与 GET 的历史版本做「字节级」比对，会在**内容完全没变**的
    情况下报「历史被改写」，是纯键序造成的假阳性（上一轮就是这么误报的）。
    所以：字节级断言两端都走同一个出口（GET），另用 sort_keys 比对确认 SSE 与 GET
    之间只存在键序差异、没有内容差异。
    """
    artifact_id = deck["artifact"]["artifact_id"]
    sse_spec = deck["artifact"]["spec"]
    latest = await client.get(f"/api/artifacts/{artifact_id}")
    original = latest.json()["data"]["spec"]
    versions: list[int] = []

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
                        "payload": {"speaker_notes": f"第 {index + 1} 次追问的讲解。"},
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        versions.append(response.json()["data"]["version"])

    assert versions == [2, 3, 4], f"P0-11① 要求 1→2→3→4，实际 {versions}"

    listing = await client.get(f"/api/artifacts/{artifact_id}/versions")
    rows = listing.json()["data"]
    assert [row["version"] for row in rows] == [1, 2, 3, 4]

    # 历史版本只读取回：v1 必须还是最初那份 spec（字节级，键序也算）
    first = await client.get(f"/api/artifacts/{artifact_id}", params={"version": 1})
    assert first.status_code == 200
    assert first.json()["data"]["version"] == 1
    assert ordered_json(first.json()["data"]["spec"]) == ordered_json(original), (
        "v1 的内容被后续 patch 改写了（P0-11③ 不通过）"
    )

    # 协议层自检：SSE 下发的 spec 与 GET 回读的 spec 只应有键序差异，不应有内容差异。
    # （这条不满足说明「创建」与「回读」两个出口内容不一致，前端做等值比较会误判。）
    assert json.dumps(sse_spec, sort_keys=True, ensure_ascii=False) == json.dumps(
        original, sort_keys=True, ensure_ascii=False
    ), "SSE 的 artifact.spec 与 GET 回读的 spec 内容不一致"

    # 尝试基于旧版本继续 patch：必须被乐观锁拒绝，且历史不被破坏
    conflict = await client.post(
        f"/api/artifacts/{artifact_id}/patch",
        json={
            "artifact_id": artifact_id,
            "base_version": 1,
            "ops": [{"op": "update_node", "node_path": "/slides/1", "payload": {"title": "覆盖历史"}}],
        },
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "PATCH_BASE_VERSION_CONFLICT"

    after_conflict = await client.get(f"/api/artifacts/{artifact_id}/versions")
    assert [row["version"] for row in after_conflict.json()["data"]] == [1, 2, 3, 4], (
        "被拒绝的 patch 不应产生新版本"
    )
    still_first = await client.get(f"/api/artifacts/{artifact_id}", params={"version": 1})
    assert ordered_json(still_first.json()["data"]["spec"]) == ordered_json(original)


async def test_old_versions_beyond_keep_window_stay_readable_but_not_patchable(
    client: Any, deck: dict[str, Any]
) -> None:
    """P0-11③ + PRD Q4：超出保留窗口（`ARTIFACT_VERSION_KEEP=20`）的老版本**仍可查看**，
    只是标记为不可再基于它 patch —— 也就是「不可 patch」不等于「被删除」。

    这条容易漏，因为 `patchable` 是个纯展示字段：一旦有人把它实现成「顺手把老版本删掉」，
    正常路径（patch 3 次）完全测不出来，只有把版本数推过窗口才会暴露。
    """
    artifact_id = deck["artifact"]["artifact_id"]
    keep = 20
    total_patches = keep + 1  # 推到 v22，让 v1/v2 落在窗口外

    version = deck["version"]
    for index in range(total_patches):
        response = await client.post(
            f"/api/artifacts/{artifact_id}/patch",
            json={
                "artifact_id": artifact_id,
                "base_version": version,
                "ops": [
                    {
                        "op": "update_node",
                        "node_path": "/slides/1",
                        "payload": {"speaker_notes": f"第 {index + 1} 次滚动更新。"},
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        version = response.json()["data"]["version"]

    rows = (await client.get(f"/api/artifacts/{artifact_id}/versions")).json()["data"]
    assert [row["version"] for row in rows] == list(range(1, total_patches + 2)), (
        f"版本必须 append-only（一条都不许删），实际 {[row['version'] for row in rows]}"
    )

    # 窗口内可 patch，窗口外只能看
    assert rows[0]["patchable"] is False, "超出保留窗口的老版本应标记为不可 patch"
    assert rows[-1]["patchable"] is True, "最新版本必须可 patch"

    # 最关键的一条：老版本内容仍读得到（不是 404），且与创建时逐字节一致
    oldest = await client.get(f"/api/artifacts/{artifact_id}", params={"version": 1})
    assert oldest.status_code == 200, "老版本只应失去 patch 能力，不应被删除"
    assert ordered_json(oldest.json()["data"]["spec"]) == ordered_json(deck["spec"]), (
        "超出保留窗口后 v1 的内容被改动了（P0-11③）"
    )


# --------------------------------------------------------------------------- #
# P0-12②：跨进程确定性
# --------------------------------------------------------------------------- #
_SUBPROCESS_SNIPPET = r"""
import hashlib, json, sys
sys.path.insert(0, r"__BACKEND_ROOT__")
from app.services.mock_generator import build_context, generate_spec

context = build_context(
    query="4.1 Malayan Union 对苏丹的影响",
    section_code="4.1",
    section_title="Malayan Union",
    texts=(
        "Malayan Union 的构想源于战后重建成本与防务需要，英国希望统一行政以降低成本。",
        "在 Malayan Union 宪制下，苏丹仅保留宗教与习俗事务的权限，政治权力被大幅削减。",
        "1946 年 3 月，全马马来人代表大会在吉隆坡召开，各地苏丹亦拒绝出席移交仪式。",
    ),
    pages=(72, 74, 76),
)
result = {}
for artifact_type in ("ppt", "anim_2d", "scene_3d"):
    payload = json.dumps(generate_spec(artifact_type, context), ensure_ascii=False, sort_keys=True)
    result[artifact_type] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
# 金丝雀：内置 hash() 受 PYTHONHASHSEED 影响，用它证明「本进程的 seed 真的生效了」。
# 少了这一行，测试可能在 PYTHONHASHSEED 被忽略的环境里「假通过」。
result["__canary__"] = str(hash("determinism-canary"))
print(json.dumps(result, sort_keys=True))
"""


def _fingerprints_in_subprocess(hash_seed: str) -> dict[str, str]:
    """在独立进程里生成三种 AST 并算指纹。`PYTHONHASHSEED` 由调用方指定。

    刻意用 `str.replace` 而不是 `.format()`：脚本正文里有 `result = {}` 这种空字典字面量，
    `.format()` 会把它当成第 0 个位置占位符，直接抛
    `IndexError: Replacement index 0 out of range for positional args tuple`。
    """
    script = _SUBPROCESS_SNIPPET.replace("__BACKEND_ROOT__", str(BACKEND_ROOT))
    env = dict(os.environ, PYTHONHASHSEED=hash_seed)
    env.pop("LLM_API_KEY", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(BACKEND_ROOT),
        timeout=120,
    )
    assert completed.returncode == 0, f"子进程失败（seed={hash_seed}）：{completed.stderr[-800:]}"
    return json.loads(completed.stdout)


def test_mock_spec_fingerprint_is_stable_across_processes() -> None:
    """P0-12②：不同 `PYTHONHASHSEED` 的两个独立进程必须产出逐字节相同的 AST。

    这条是防「内置 hash() 陷阱」的唯一有效手段：`hash()` 的结果随 hash seed 变化，
    单进程内两次调用却永远一致，所以只在进程内比对永远发现不了。
    """
    seeds = ("0", "1", "12345")
    raw = [_fingerprints_in_subprocess(seed) for seed in seeds]
    results = [{key: value for key, value in payload.items() if key != "__canary__"} for payload in raw]

    # 前置自检：确认三个子进程的 hash seed 确实不同，否则下面的相等断言毫无意义
    canaries = [payload["__canary__"] for payload in raw]
    assert len(set(canaries)) == len(canaries), (
        f"PYTHONHASHSEED 未生效（金丝雀 hash 全部相同：{canaries}），本用例将退化为无效测试"
    )

    assert results[0] == results[1] == results[2], (
        "跨进程 AST 指纹不一致，说明 mock 生成器引入了受 PYTHONHASHSEED 影响的不确定性：\n"
        + "\n".join(f"  seed={seed}: {payload}" for seed, payload in zip(seeds, results))
    )
    assert set(results[0]) == {"ppt", "anim_2d", "scene_3d"}


# --------------------------------------------------------------------------- #
# P0-06②：显式形态覆盖（端到端）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("override", "expected_type"),
    [("scene_3d", "scene_3d"), ("anim_2d", "anim_2d"), ("ppt", "ppt")],
)
async def test_explicit_artifact_type_override_is_respected(
    client: Any, sample_pdf_path: Path, override: str, expected_type: str
) -> None:
    """P0-06②：用户在 UI 里显式选了形态，产物类型必须 100% 遵循。"""
    seeded = await seed_workspace_with_pdf(client, sample_pdf_path)
    events = await sse_events(
        client,
        {
            "workspace_id": seeded["workspace_id"],
            "query": "4.1 讲了什么，帮我梳理要点",
            "artifact_type_override": override,
        },
    )
    decision = first_event(events, "modality_decision")
    assert decision["user_override"] is True
    assert decision["artifact_type"] == expected_type
    assert decision["reason"].strip(), "P0-06③ 要求给出一句话理由"

    artifact = first_event(events, "artifact")
    assert artifact["artifact_type"] == expected_type
    assert artifact["spec"]["type"] == expected_type

"""回归测试 · 版本快照别名加固（P0-11③ 的防线）与 Grader 实词覆盖门槛（P0-05④）。

本文件是 `bugfix-engineer` 对两个缺陷修复的**独立回归基线**，与 QA 的验收测试互补：

- 验收测试回答「用户能不能正常用」，本文件回答「这两个缺陷的**机制**有没有被真正堵住」。
  因此这里刻意从更底层的地方下断言（例如直接比对 ORM 对象的身份、直接读 `artifact_versions`
  行），而不是只看 HTTP 200。

- 关于 P0-11③ 的措辞校准：**「历史版本被后续 patch 改写」这个缺陷不成立**（QA 已在修复前
  代码上用 worktree 复现并确认：库内 v1 内容逐字节正确，差异纯粹是 SSE 键排序 vs REST
  插入序）。本文件第一个用例验的是**真实存在的隐患**——三处写入曾共享同一个 dict 对象，
  SQLAlchemy 的 `JSON` 类型不追踪嵌套原地修改，将来任何一次读路径原地改写都可能把历史行
  带脏；它当前没有可复现的失败用例，属于防御性加固，不能报成「修了一个 P0」。

- 第二个测试同时是**双向**的：无关问题必须全部 NO_MATCH（误判率），相关问题必须全部
  不是 NO_MATCH（漏判率）。只验一个方向很容易用「把阈值调到极端」蒙过去。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from acceptance_support import seed_workspace_with_pdf
from app.db import repository
from app.db.models import ArtifactVersion
from app.db.session import get_session_factory
from app.services.retriever import (
    NO_MATCH,
    PARTIAL_OR_BEYOND,
    SYLLABUS_MATCH,
    get_retriever,
)

# --------------------------------------------------------------------------- #
# 版本快照别名加固：v1 行必须与 artifacts 行、与调用方手里的 spec 三方隔离
# --------------------------------------------------------------------------- #
async def test_version_snapshot_is_isolated_from_artifact_spec(
    db_env: Path, slide_deck_dict: dict[str, Any]
) -> None:
    """v1 快照落库后，`artifact.spec_json` 的任何后续变更都不得影响 v1 行的内容。

    隐患机制（加固前的真实写法）：`create_artifact(spec_json=spec)` 与
    `append_artifact_version(spec_json=spec)` 把**同一个 dict 对象**分别挂在两行上，
    `apply_artifact_patch` 又把「新快照」同时赋给 `artifact.spec_json` 与新版本行。
    于是 artifacts 行、v1 行、调用方手里的 spec 三方共享嵌套结构；而 SQLAlchemy 的
    `JSON` 类型**不追踪嵌套结构的原地修改**，内存里改一个 `title` 不会被标记为 dirty，
    却已经把同一 session 里 v1 行对象的值改脏了 —— 该行只要因别的原因（`patch_ops`
    变更、同 session 的其它 flush）被写回，历史版本就被静默覆盖。

    说明：当前代码里没有任何路径会触发它（已确认 v1 落库内容逐字节正确），
    所以这是**防御性断言**而非缺陷复现。价值在于：一旦有人把 deepcopy 换回引用传递，
    这里立刻变红，而不是等某次偶发的 flush 顺序把历史改掉之后再排查。

    本用例按「先证明对象隔离，再证明行为隔离」两步验：
    1. 身份断言：两行持有的 dict 都不能是调用方那份（也不能彼此相同）；
    2. 行为断言：调用方提交前继续改写自己那份 spec（真实场景：agent_engine 会把它塞进
       SSE 事件继续流转），重新读库时 v1 行必须一字未变。
    """
    factory = get_session_factory()
    original_title = slide_deck_dict["slides"][1]["title"]

    async with factory() as session:
        workspace = await repository.create_workspace(session, "版本隔离")
        artifact = await repository.create_artifact(
            session,
            workspace_id=workspace.id,
            title="Malayan Union",
            artifact_type="ppt",
            spec_json=slide_deck_dict,
        )
        version_row = await repository.append_artifact_version(
            session, artifact_id=artifact.id, spec_json=slide_deck_dict
        )
        artifact_id = artifact.id

        # ---- 1. 身份隔离 ----
        assert artifact.spec_json is not slide_deck_dict, (
            "artifacts 行直接引用了调用方的 spec dict（没有 deepcopy）"
        )
        assert version_row.spec_json is not slide_deck_dict, (
            "v1 版本行直接引用了调用方的 spec dict（没有 deepcopy）—— 后续任何原地修改都会污染历史"
        )
        assert version_row.spec_json is not artifact.spec_json, (
            "v1 版本行与 artifacts 行共享了同一个 spec dict 对象（P0-11③ 的隐患源头）"
        )

        # ---- 2. 行为隔离：调用方提交前继续改写自己那份 ----
        slide_deck_dict["slides"][1]["title"] = "调用方后续改写"
        slide_deck_dict["topic"] = "调用方后续改写"
        await session.commit()

    async with factory() as session:
        rows = (
            await session.execute(
                select(ArtifactVersion)
                .where(ArtifactVersion.artifact_id == artifact_id)
                .order_by(ArtifactVersion.version.asc())
            )
        ).scalars().all()
        assert [row.version for row in rows] == [1], "append-only：不应凭空多出版本"
        assert rows[0].spec_json["slides"][1]["title"] == original_title, (
            "调用方改写自己的 spec 后，v1 行的内容跟着变了（P0-11③ 历史版本被覆盖）"
        )
        assert rows[0].spec_json["topic"] != "调用方后续改写", (
            "调用方改写自己的 spec 后，v1 行的 topic 跟着变了（P0-11③ 历史版本被覆盖）"
        )


async def test_create_time_spec_is_byte_identical_across_endpoints(
    client: Any, seeded: dict[str, Any]
) -> None:
    """创建产物时 SSE 下发的 spec，与随后 `GET /api/artifacts/{id}` 回读的 spec 必须逐字节相同。

    依据 IMPL-NOTES §E8「协议出口的序列化必须统一」：SSE 帧走 `canonical_json`（键排序），
    REST 出口也走 `canonical_tree`，于是同一份内容在任何出口都是同一串字节。
    这样客户端做「创建时 spec vs 回读 spec」比对（版本预览、断线重连校验、缓存键）时，
    不会把纯键序差异误判成内容变化 —— 这个坑上一轮已经踩过一次（验收测试假阳性）。

    注意：本条**不是**在断言「历史被改写」这个缺陷（该缺陷经 QA 用 worktree 复现确认不存在），
    而是把「协议层只有一种字节表示」这一标准钉死，防止将来有人在某个出口上退回裸 dict。
    """
    from acceptance_support import first_event, ordered_json, sse_events

    events = await sse_events(
        client,
        {
            "workspace_id": seeded["workspace_id"],
            "query": "学习 4.1",
            "artifact_type_override": "ppt",
        },
    )
    artifact = first_event(events, "artifact")
    assert artifact["action"] == "create"

    detail = await client.get(f"/api/artifacts/{artifact['artifact_id']}")
    assert detail.status_code == 200
    assert ordered_json(detail.json()["data"]["spec"]) == ordered_json(artifact["spec"]), (
        "SSE 事件里的 spec 与 GET 回读的 spec 字节不一致（键序不同），"
        "客户端会把纯键序差异误判成内容变化（IMPL-NOTES §E8）"
    )


async def test_history_version_is_byte_stable_after_patches(
    client: Any, db_env: Path, slide_deck_dict: dict[str, Any]
) -> None:
    """连续 patch 到 v4 后，`GET ?version=1` 必须与创建时回读的 v1 逐字节一致。

    为什么不复用 SSE 里的 artifact 事件做基线：SSE 帧走 `canonical_json`（键排序），
    与库内插入顺序天然不同，拿它做字节比对会引入与缺陷无关的噪声。这里两侧都走
    `GET /api/artifacts/{id}`，比的是「同一接口在不同时间点对同一版本给出的字节」。
    """
    factory = get_session_factory()
    async with factory() as session:
        workspace = await repository.create_workspace(session, "版本字节稳定")
        artifact = await repository.create_artifact(
            session,
            workspace_id=workspace.id,
            title="Malayan Union",
            artifact_type="ppt",
            spec_json=slide_deck_dict,
        )
        await repository.append_artifact_version(
            session, artifact_id=artifact.id, spec_json=slide_deck_dict
        )
        await session.commit()
        artifact_id = artifact.id

    created = await client.get(f"/api/artifacts/{artifact_id}")
    assert created.status_code == 200, created.text
    baseline = created.json()["data"]["spec"]

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
                        "payload": {"speaker_notes": f"第 {index + 1} 次追问"},
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["version"] == index + 2

    latest = await client.get(f"/api/artifacts/{artifact_id}")
    assert latest.json()["data"]["version"] == 4
    assert latest.json()["data"]["spec"]["slides"][1]["speaker_notes"] == "第 3 次追问"

    first = await client.get(f"/api/artifacts/{artifact_id}", params={"version": 1})
    assert first.status_code == 200
    assert first.json()["data"]["version"] == 1
    assert first.json()["data"]["spec"] == baseline, (
        "v1 的内容与创建时不一致（P0-11③ 历史版本被后续 patch 改写）"
    )

    listing = await client.get(f"/api/artifacts/{artifact_id}/versions")
    rows = listing.json()["data"]
    assert [row["version"] for row in rows] == [1, 2, 3, 4]
    assert rows[0]["spec"] == baseline, "版本列表里的 v1 快照与创建时不一致"


# --------------------------------------------------------------------------- #
# P0-05④：双向验证 —— 无关问题不许误判，相关问题不许漏判
# --------------------------------------------------------------------------- #
# 与 QA 的 UNRELATED_QUERIES 同源但独立列一遍：本文件要能单独跑通，不依赖别处的常量。
UNRELATED = (
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
    # 量词陷阱：裸数字曾被当成「点名了第 3 章」，导致以元数据锚定为由判成课本覆盖
    "帮我写 3 首关于春天的五言绝句",
)

# 相关问题：必须判 SYLLABUS_MATCH 或 PARTIAL_OR_BEYOND。
# 刻意覆盖四种「课本确实讲了」的形态：
#   ① 点名章节（元数据锚定路径）
#   ② 术语原样出现（词面命中）
#   ③ 换了说法、需要拓展（真正的中间态）
#   ④ 汉字章号（中文教材最常见的点名方式）
RELATED = (
    "4.1 Malayan Union 对苏丹的影响",
    "学习 4.1",
    "4.1.2 苏丹的权力有什么变化",
    "Malayan Union 对马来西亚宪制发展的长期影响",
    "为什么马来人会反对 Malayan Union",
    "马来亚联邦与马来亚联合邦有什么区别",
    "第五章 马来西亚的成立过程是怎样的",
    "第五章讲了什么",
    "Sejarah 这个词是什么意思",
    "史料可以分为哪几类",
)


@pytest.fixture()
async def seeded(client: Any, sample_pdf_path: Path) -> dict[str, Any]:
    return await seed_workspace_with_pdf(client, sample_pdf_path)


async def test_grader_rejects_unrelated_but_keeps_related(seeded: dict[str, Any]) -> None:
    """双向断言：14+1 条无关问题全部 NO_MATCH；10 条相关问题全部不是 NO_MATCH。

    为什么要双向：只验「无关问题不许误判」，最省事的做法是把门槛拉到无穷高 ——
    所有问题都答「课本未覆盖」，误判率 0，产品直接废掉。只验「相关问题不许漏判」同理。
    两个方向同时卡住，才说明判据真的有区分力，而不是被调到某个极端。
    """
    retriever = get_retriever()
    workspace_id = seeded["workspace_id"]

    false_positives: list[tuple[str, str, float, float]] = []
    for query in UNRELATED:
        result = await retriever.retrieve(workspace_id, query)
        if result.grader_status != NO_MATCH:
            false_positives.append(
                (query, result.grader_status, round(result.top_score, 4), round(result.content_hit_ratio, 3))
            )

    false_negatives: list[tuple[str, str, float, float]] = []
    for query in RELATED:
        result = await retriever.retrieve(workspace_id, query)
        if result.grader_status not in {SYLLABUS_MATCH, PARTIAL_OR_BEYOND}:
            false_negatives.append(
                (query, result.grader_status, round(result.top_score, 4), round(result.content_hit_ratio, 3))
            )

    assert not false_positives, (
        f"{len(false_positives)}/{len(UNRELATED)} 条无关问题被判成「课本有覆盖」"
        "（P0-05④：会导致编造页码）：\n"
        + "\n".join(
            f"  - {q} → {status}（top_score={score}, 实词命中率={ratio}）"
            for q, status, score, ratio in false_positives
        )
    )
    assert not false_negatives, (
        f"{len(false_negatives)}/{len(RELATED)} 条相关问题被判成「课本未覆盖」"
        "（漏判：主演示路径会被打死）：\n"
        + "\n".join(
            f"  - {q} → {status}（top_score={score}, 实词命中率={ratio}）"
            for q, status, score, ratio in false_negatives
        )
    )


async def test_no_match_turn_ships_no_citation_entries(client: Any, seeded: dict[str, Any]) -> None:
    """NO_MATCH 回合的 `citations` 必须为空：否则前端会把 p.N 画在「课本未覆盖」旁边。"""
    from acceptance_support import first_event, reply_text, sse_events

    events = await sse_events(
        client, {"workspace_id": seeded["workspace_id"], "query": "为什么天空是蓝色的"}
    )
    assert first_event(events, "retrieval")["grader_status"] == NO_MATCH
    assert first_event(events, "citations")["citations"] == []
    assert "课本未覆盖" in reply_text(events)

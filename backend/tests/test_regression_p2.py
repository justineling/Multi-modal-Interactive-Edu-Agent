"""P2 回归：QA 报告 §3「P2 一般」的 B7 / B8 / B9。

三条用例一一对应三个缺陷，**每条都能在修复前的代码上复现失败**：

| # | 缺陷 | 修复前行为 | 本文件的断言 |
|---|---|---|---|
| B7 | `error.stage` 硬编码 `"generate"` | patch 阶段失败报 `stage="generate"` | `stage == "patch"` |
| B8 | `NO_MATCH` 时 `retrieval.chunks` 仍下发候选 | 8 条带 `page_start/page_end` 的候选 | `chunks == []` 且 `top_score > 0` |
| B9 | 兜底 `turn_end` 的 `duration_ms` / `ttft_ms` 恒为 0 | 两个字段都是 0 | 两者都 `> 0` |

刻意与 QA 的验收用例**互补**而不是重复：
- B7 的「error 后仍补发 turn_end」已由 `test_acceptance_p0_robustness` 锁定，
  这里只补「stage 必须指向真实出错的环节」这一条；
- B8 只补「候选片段不得进 `retrieval.chunks`」，同时**反向锁住** `top_score` 不许被顺手清零 ——
  QA 的判定矩阵与诊断依赖它；
- B9 直接驱动 `_event_stream`（`ASGITransport` 会先把整个响应体读完，走 HTTP 造不出
  「流到一半炸掉」的时序），与断连用例同样的理由。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from acceptance_support import first_event, parse_sse_frames, seed_workspace_with_pdf, sse_events

# B7 用的锚点：指向一个已被删除（或被另一个标签页删掉）的产物。
# 这是真实会发生的竞态，且失败点确定地落在 patch 阶段的「读基线」这一步。
DELETED_ARTIFACT_ANCHOR: dict[str, Any] = {
    "artifact_id": "artifact-already-deleted",
    "node_path": "/slides/1",
    "node_label": "成立背景",
    "page": 12,
    "section_code": "1.1",
}

# B8 用的无关问题：取自 `acceptance_support.UNRELATED_QUERIES`，与马来西亚历史教材
# 无任何语义关系，且词面上仍有零星重合（`三/分/球`），因此 `top_score` 必然 > 0 ——
# 这正是本用例要锁的「清 chunks 但不许清分数」。
UNRELATED_QUERY = "怎么练习三分球投篮姿势"


# --------------------------------------------------------------------------- #
# B7：error 事件的 stage 必须指向真实出错的环节
# --------------------------------------------------------------------------- #
async def test_patch_stage_failure_reports_stage_patch(
    client: Any, small_pdf_path: Path
) -> None:
    """patch 阶段抛的 `AppError` 必须标 `stage="patch"`，而不是兜底的 `"generate"`。

    为什么 stage 值得单独测：前端按 stage 决定「哪一步失败」的提示文案，
    日志排查也按 stage 分桶。报成 `"generate"` 会把排查方向引到「正文生成」，
    而正文其实早已正常流给用户了。
    """
    seeded = await seed_workspace_with_pdf(client, small_pdf_path)

    events = await sse_events(
        client,
        {
            "workspace_id": seeded["workspace_id"],
            "query": "再细讲一下这一点",
            "anchor": DELETED_ARTIFACT_ANCHOR,
        },
    )

    names = [name for name, _ in events]
    error = first_event(events, "error")

    assert error["code"] == "ARTIFACT_NOT_FOUND", f"错误码不对：{error}"
    assert error["stage"] == "patch", (
        f"patch 阶段失败必须报 stage='patch'，实际 {error['stage']!r}；"
        f"报成 'generate' 会让前端提示与日志排查都指向没出错的环节"
    )

    # §5.4 规则②：error 之后仍必须补发 turn_end（就地捕获不许吞掉后续事件）
    assert names[-1] == "turn_end", f"error 之后必须补发 turn_end，实际顺序：{names}"
    assert names.index("error") < names.index("turn_end")

    # 正文已经流给用户了，出处清单不该因为产物读写失败而消失
    assert "citations" in names, f"patch 失败不应丢掉 citations 事件，实际顺序：{names}"


# --------------------------------------------------------------------------- #
# B8：NO_MATCH 时不得下发带页码的候选片段
# --------------------------------------------------------------------------- #
async def test_no_match_retrieval_chunks_are_empty_but_top_score_kept(
    client: Any, small_pdf_path: Path
) -> None:
    """`grader_status == NO_MATCH` 时 `retrieval.chunks` 必须是 `[]`。

    正文说「课本未覆盖」，若同一帧里还带着 8 条 `page_start/page_end` 的候选，
    任何「显示相关片段」的 UI 都会立刻把页码画在「课本未覆盖」旁边 —— 与
    `citations` 已经修掉的自相矛盾（P0-05④）完全同构，只是换了个字段。

    同时**反向断言** `top_score > 0`：清空 chunks 不能顺手把分数也清零。
    `grader_status` / `section_scope` / `top_score` 是判定矩阵与诊断的输入，
    动了它们等于把「诊断信息」和「用户可见信息」混为一谈。
    """
    seeded = await seed_workspace_with_pdf(client, small_pdf_path)

    events = await sse_events(
        client, {"workspace_id": seeded["workspace_id"], "query": UNRELATED_QUERY}
    )

    retrieval = first_event(events, "retrieval")

    assert retrieval["grader_status"] == "NO_MATCH", (
        f"「{UNRELATED_QUERY}」应判课本未覆盖，实际 {retrieval['grader_status']}"
    )
    assert retrieval["chunks"] == [], (
        "NO_MATCH 仍下发了候选片段，未来的「相关片段」UI 会把页码暴露在"
        f"「课本未覆盖」旁边：{[c['section_code'] for c in retrieval['chunks']]}"
    )
    assert retrieval["top_score"] > 0, (
        "top_score 被顺手清零了 —— 判定矩阵与诊断依赖它，只清 chunks，不要动分数"
    )


# --------------------------------------------------------------------------- #
# B9：兜底 turn_end 的耗时必须是真实值
# --------------------------------------------------------------------------- #
class _ExplodingEngine:
    """先吐 `tokens` 个 token，再抛 `AppError` 的假引擎。

    `tokens=0` 模拟「连首字都没等到就失败」（检索/路由阶段炸），
    `tokens=1` 模拟「首字已到、后续生成失败」，两种情况的 ttft 语义不同。
    """

    def __init__(self, tokens: int) -> None:
        self._tokens = tokens

    async def stream_turn(self, turn: Any):  # noqa: ANN201 - 与真实签名一致即可
        from app.errors import AppError, ErrorCode
        from app.schemas.chat import TokenEvent

        for index in range(self._tokens):
            await asyncio.sleep(0.02)
            yield "token", TokenEvent(delta=f"t{index}")
        await asyncio.sleep(0.02)
        raise AppError(ErrorCode.INTERNAL_ERROR, "boom")


async def _run_engine(engine: Any) -> list[tuple[str, dict[str, Any]]]:
    """驱动 `_event_stream` 直到流结束，返回解析后的事件序列。"""
    from app.api.chat_stream import _event_stream
    from app.services.agent_engine import TurnRequest

    frames = [
        frame
        async for frame in _event_stream(
            engine, TurnRequest(workspace_id="ws-1", query="q", turn_id="turn-1")
        )
    ]
    return parse_sse_frames("".join(frames))


async def test_fallback_turn_end_carries_real_timings(db_env: Path) -> None:
    """异常路径的兜底 `turn_end` 必须带真实耗时，不能是 0。

    为什么 0 是有害的而不是「无数据」：失败回合的 TTFT / 总耗时是「失败率、失败延迟」
    的唯一来源。填 0 会让所有异常路径在监控上表现为「0ms 瞬间完成」，与成功回合混在
    一起后既看不出失败回合有多慢，也答不出「超时是否集中在某个阶段」。
    """
    # 场景 1：一个 token 都没发出 —— ttft 应与 duration 同值（「到死都没等到首字」），
    # 而不是 0（0 会被误读成「首字瞬间到达」）。
    no_token_events = await _run_engine(_ExplodingEngine(tokens=0))
    no_token_end = first_event(no_token_events, "turn_end")

    assert no_token_end["duration_ms"] > 0, (
        f"兜底 turn_end 的 duration_ms 仍是 {no_token_end['duration_ms']}，"
        "异常路径的耗时统计全部失真"
    )
    assert no_token_end["ttft_ms"] > 0, (
        f"兜底 turn_end 的 ttft_ms 仍是 {no_token_end['ttft_ms']}；"
        "一个 token 都没发出时应填成与 duration_ms 相等，而不是 0"
    )
    assert no_token_end["ttft_ms"] == no_token_end["duration_ms"], (
        "没有任何 token 时 ttft 必须等于 duration（同一次测量的两端），"
        f"实际 ttft={no_token_end['ttft_ms']} duration={no_token_end['duration_ms']}"
    )

    # 场景 2：首字已到、之后才失败 —— ttft 必须是「到首字」的真实耗时，严格小于总耗时。
    with_token_events = await _run_engine(_ExplodingEngine(tokens=1))
    with_token_end = first_event(with_token_events, "turn_end")

    assert with_token_end["ttft_ms"] > 0, f"ttft_ms 仍是 0：{with_token_end}"
    assert with_token_end["ttft_ms"] < with_token_end["duration_ms"], (
        "首字之后还跑了一段才失败，ttft 必须严格小于 duration，"
        f"实际 ttft={with_token_end['ttft_ms']} duration={with_token_end['duration_ms']}"
    )
    assert with_token_end["turn_id"] == "turn-1", "兜底 turn_end 必须能对上日志里的 turn_id"

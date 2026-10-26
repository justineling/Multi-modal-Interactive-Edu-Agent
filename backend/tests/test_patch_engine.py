"""Patch 引擎测试。

核心断言是**「非锚定节点指纹不变」**（P0-10②）：对 before/after 各生成全路径指纹表，
剔除 changed_paths 及其祖先/后代后两表必须完全相等。这是唯一能自动化的「字节级不变」证明。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.errors import AppError, ContractValidationError, ErrorCode
from app.schemas.patch import PatchOp, PatchRequest
from app.services.patch_engine import (
    PatchInvariantError,
    apply_patch,
    apply_patch_request,
    assert_only_subtree_changed,
    changed_paths_between,
    ensure_base_version,
    fingerprint,
    path_fingerprints,
    resolve_target,
)


def _op(**kwargs: Any) -> PatchOp:
    return PatchOp(**kwargs)


# --------------------------------------------------------------------------- #
# 指纹本身
# --------------------------------------------------------------------------- #
def test_fingerprint_is_key_order_insensitive() -> None:
    left = {"a": 1, "b": {"c": [1, 2], "d": None}}
    right = {"b": {"d": None, "c": [1, 2]}, "a": 1}
    assert fingerprint(left) == fingerprint(right)


def test_fingerprint_is_pure_function() -> None:
    """纯函数：同一对象连续调用结果必须相同（不含随机、时间、内置 hash）。"""
    payload = {"type": "ppt", "topic": "同一内容"}
    assert fingerprint(payload) == fingerprint(payload) == fingerprint(copy.deepcopy(payload))
    # 内容变一个字符，指纹必须变
    assert fingerprint(payload) != fingerprint({**payload, "topic": "同一内容 "})


def test_path_fingerprints_cover_every_path(slide_deck_dict: dict[str, Any]) -> None:
    table = path_fingerprints(slide_deck_dict)
    assert "" in table  # 根
    assert "/topic" in table
    assert "/slides/0/node_id" in table
    assert "/slides/2/comparison_left/1" in table


# --------------------------------------------------------------------------- #
# op 1：update_node
# --------------------------------------------------------------------------- #
def test_update_node_changes_only_anchored_leaf(slide_deck_dict: dict[str, Any]) -> None:
    before = copy.deepcopy(slide_deck_dict)
    outcome = apply_patch(
        slide_deck_dict,
        [_op(op="update_node", node_path="/slides/1", payload={"title": "成立背景（重讲）"})],
    )

    assert outcome.changed_paths == ("/slides/1/title",)
    assert outcome.spec["slides"][1]["title"] == "成立背景（重讲）"
    # 原始 dict 必须未被触碰（定点写入只发生在 deepcopy 上）
    assert slide_deck_dict == before
    assert outcome.spec is not slide_deck_dict

    # 核心断言：非锚定节点字节级不变
    assert_only_subtree_changed(before, outcome.spec, outcome.changed_paths)

    # 兄弟节点指纹逐一相等（比 assert_only_subtree_changed 更直观的第二重保险）
    for index in (0, 2):
        assert fingerprint(outcome.spec["slides"][index]) == fingerprint(before["slides"][index])
    # 键序不变：patch 不能把字段重新排序，否则前端 diff 会误判整页变化
    assert list(outcome.spec["slides"][1].keys()) == list(before["slides"][1].keys())


def test_update_node_list_field(slide_deck_dict: dict[str, Any]) -> None:
    before = copy.deepcopy(slide_deck_dict)
    outcome = apply_patch(
        slide_deck_dict,
        [
            _op(
                op="update_node",
                node_path="/slides/1",
                payload={"bullets": ["战后重建需要统一行政", "英国需要更直接的控制", "抗共安全考量"]},
            )
        ],
    )
    assert outcome.changed_paths == ("/slides/1/bullets",)
    assert len(outcome.spec["slides"][1]["bullets"]) == 3
    assert_only_subtree_changed(before, outcome.spec, outcome.changed_paths)


def test_update_entity_by_pointer(keyframe_2d_dict: dict[str, Any]) -> None:
    before = copy.deepcopy(keyframe_2d_dict)
    outcome = apply_patch(
        keyframe_2d_dict,
        [
            _op(
                op="update_node",
                node_path="/steps/0/entities/1",
                payload={"label": "1945 年（战后）"},
            )
        ],
    )
    assert outcome.changed_paths == ("/steps/0/entities/1/label",)
    assert outcome.spec["steps"][0]["entities"][1]["label"] == "1945 年（战后）"
    assert_only_subtree_changed(before, outcome.spec, outcome.changed_paths)


def test_update_node_unknown_field_rejected(slide_deck_dict: dict[str, Any]) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        apply_patch(
            slide_deck_dict,
            [_op(op="update_node", node_path="/slides/1", payload={"notes": "自创字段"})],
        )
    assert excinfo.value.code is ErrorCode.CONTRACT_VALIDATION_FAILED


def test_update_node_identity_change_rejected(slide_deck_dict: dict[str, Any]) -> None:
    with pytest.raises(ContractValidationError):
        apply_patch(
            slide_deck_dict,
            [_op(op="update_node", node_path="/slides/1", payload={"node_id": "slide-99"})],
        )


# --------------------------------------------------------------------------- #
# op 2：insert_node
# --------------------------------------------------------------------------- #
def test_insert_node_shifts_siblings_and_marks_list(slide_deck_dict: dict[str, Any]) -> None:
    before = copy.deepcopy(slide_deck_dict)
    new_slide = {
        "node_id": "slide-4",
        "layout": "bullet_points",
        "title": "马来亚联合邦的成立",
        "subtitle": None,
        "bullets": ["1948 年成立", "苏丹权力恢复"],
        "comparison_left": None,
        "comparison_right": None,
        "timeline_items": None,
        "quiz_questions": None,
        "speaker_notes": "承接上一页的对比，讲回退与妥协。",
        "source_ref": "Form 4 Sejarah, Bab 4.1, p.118",
    }
    outcome = apply_patch(
        slide_deck_dict, [_op(op="insert_node", node_path="/slides/2", payload=new_slide)]
    )

    assert len(outcome.spec["slides"]) == 4
    assert outcome.spec["slides"][2]["node_id"] == "slide-4"
    # 下标平移 → 变化范围只能声明到列表层，否则平移后的兄弟会被误判为「被改动」
    assert outcome.changed_paths == ("/slides",)
    assert_only_subtree_changed(before, outcome.spec, outcome.changed_paths)
    # 列表之外的内容一字未动
    assert fingerprint(outcome.spec["topic"]) == fingerprint(before["topic"])


def test_insert_node_at_tail_allowed(slide_deck_dict: dict[str, Any]) -> None:
    new_slide = dict(slide_deck_dict["slides"][1], node_id="slide-9")
    outcome = apply_patch(
        slide_deck_dict, [_op(op="insert_node", node_path="/slides/3", payload=new_slide)]
    )
    assert len(outcome.spec["slides"]) == 4
    assert outcome.spec["slides"][3]["node_id"] == "slide-9"


def test_insert_node_duplicate_identity_rejected(slide_deck_dict: dict[str, Any]) -> None:
    duplicate = dict(slide_deck_dict["slides"][0])
    with pytest.raises(ContractValidationError) as excinfo:
        apply_patch(
            slide_deck_dict, [_op(op="insert_node", node_path="/slides/1", payload=duplicate)]
        )
    assert "冲突" in excinfo.value.message


def test_insert_node_invalid_payload_rejected(slide_deck_dict: dict[str, Any]) -> None:
    with pytest.raises(ContractValidationError):
        apply_patch(
            slide_deck_dict,
            [_op(op="insert_node", node_path="/slides/1", payload={"node_id": "slide-7"})],
        )


# --------------------------------------------------------------------------- #
# op 3：delete_node
# --------------------------------------------------------------------------- #
def test_delete_node(slide_deck_dict: dict[str, Any]) -> None:
    before = copy.deepcopy(slide_deck_dict)
    outcome = apply_patch(slide_deck_dict, [_op(op="delete_node", node_path="/slides/2")])

    assert len(outcome.spec["slides"]) == 2
    assert outcome.changed_paths == ("/slides",)
    assert_only_subtree_changed(before, outcome.spec, outcome.changed_paths)


def test_delete_last_node_rejected(slide_deck_dict: dict[str, Any]) -> None:
    single = {
        **slide_deck_dict,
        "slides": [slide_deck_dict["slides"][0]],
    }
    with pytest.raises(AppError) as excinfo:
        apply_patch(single, [_op(op="delete_node", node_path="/slides/0")])
    assert excinfo.value.code is ErrorCode.PATCH_PATH_INVALID


# --------------------------------------------------------------------------- #
# op 4：replace_subtree
# --------------------------------------------------------------------------- #
def test_replace_subtree(slide_deck_dict: dict[str, Any]) -> None:
    before = copy.deepcopy(slide_deck_dict)
    replacement = {
        **slide_deck_dict["slides"][2],
        "title": "联邦与联合邦的区别（重做整块）",
        "comparison_left": ["苏丹权力被大幅削减"],
        "comparison_right": ["苏丹权力恢复"],
    }
    outcome = apply_patch(
        slide_deck_dict,
        [_op(op="replace_subtree", node_path="/slides/2", payload=replacement)],
    )

    assert outcome.changed_paths == ("/slides/2",)
    assert outcome.spec["slides"][2]["title"].endswith("（重做整块）")
    assert_only_subtree_changed(before, outcome.spec, outcome.changed_paths)


def test_replace_subtree_rejects_list_path(slide_deck_dict: dict[str, Any]) -> None:
    with pytest.raises(AppError) as excinfo:
        apply_patch(
            slide_deck_dict,
            [_op(op="replace_subtree", node_path="/slides", payload={"topic": "整表重写"})],
        )
    assert excinfo.value.code is ErrorCode.PATCH_PATH_INVALID


# --------------------------------------------------------------------------- #
# op 5：focus_camera
# --------------------------------------------------------------------------- #
def test_focus_camera_does_not_change_content(scene_graph_3d_dict: dict[str, Any]) -> None:
    before = copy.deepcopy(scene_graph_3d_dict)
    outcome = apply_patch(
        scene_graph_3d_dict,
        [_op(op="focus_camera", node_path="/camera_tour/1", payload={"preset_name": "剖面"})],
    )

    assert outcome.focus_preset == "剖面"
    # 视角是渲染态，不是内容态：spec 必须一字未改，changed_paths 必须为空
    assert outcome.changed_paths == ()
    assert fingerprint(outcome.spec) == fingerprint(before)
    assert_only_subtree_changed(before, outcome.spec, ())


def test_focus_camera_unknown_preset_rejected(scene_graph_3d_dict: dict[str, Any]) -> None:
    with pytest.raises(ContractValidationError):
        apply_patch(
            scene_graph_3d_dict,
            [_op(op="focus_camera", node_path="/camera_tour/0", payload={"preset_name": "俯瞰"})],
        )


def test_focus_camera_on_ppt_rejected(slide_deck_dict: dict[str, Any]) -> None:
    with pytest.raises(AppError) as excinfo:
        apply_patch(
            slide_deck_dict,
            [_op(op="focus_camera", node_path="/camera_tour/0", payload={"preset_name": "全景"})],
        )
    assert excinfo.value.code is ErrorCode.PATCH_PATH_INVALID


# --------------------------------------------------------------------------- #
# 非法路径与整批回滚
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("node_path", ["/slides/99", "/slides/3", "/nodes/0"])
def test_invalid_node_path_returns_patch_path_invalid(
    slide_deck_dict: dict[str, Any], node_path: str
) -> None:
    with pytest.raises(AppError) as excinfo:
        apply_patch(
            slide_deck_dict, [_op(op="update_node", node_path=node_path, payload={"title": "x"})]
        )
    assert excinfo.value.code is ErrorCode.PATCH_PATH_INVALID


def test_resolve_target_rejects_missing_root_list(slide_deck_dict: dict[str, Any]) -> None:
    with pytest.raises(AppError) as excinfo:
        resolve_target(slide_deck_dict, "/nodes/0")
    assert excinfo.value.code is ErrorCode.PATCH_PATH_INVALID


def test_validation_failure_rolls_back_whole_batch(slide_deck_dict: dict[str, Any]) -> None:
    """一批里只要有一个 op 导致校验失败，整批作废，原 dict 一个字节都不能变。"""
    before = copy.deepcopy(slide_deck_dict)
    with pytest.raises(ContractValidationError) as excinfo:
        apply_patch(
            slide_deck_dict,
            [
                # 第一个 op 合法，第二个 op 把 bullets 写成字符串 → 校验失败
                _op(op="update_node", node_path="/slides/0", payload={"title": "先改一下"}),
                _op(op="update_node", node_path="/slides/1", payload={"bullets": "不是数组"}),
            ],
        )
    assert excinfo.value.code is ErrorCode.CONTRACT_VALIDATION_FAILED
    assert slide_deck_dict == before


# --------------------------------------------------------------------------- #
# 乐观锁
# --------------------------------------------------------------------------- #
def test_base_version_conflict(slide_deck_dict: dict[str, Any]) -> None:
    request = PatchRequest(
        artifact_id="art-1",
        base_version=2,
        ops=[_op(op="update_node", node_path="/slides/1", payload={"title": "x"})],
    )
    with pytest.raises(AppError) as excinfo:
        apply_patch_request(slide_deck_dict, request, current_version=3)
    assert excinfo.value.code is ErrorCode.PATCH_BASE_VERSION_CONFLICT


def test_base_version_match_bumps_version(slide_deck_dict: dict[str, Any]) -> None:
    request = PatchRequest(
        artifact_id="art-1",
        base_version=3,
        ops=[_op(op="update_node", node_path="/slides/1", payload={"title": "x"})],
    )
    outcome = apply_patch_request(slide_deck_dict, request, current_version=3)
    assert outcome.base_version == 3
    assert outcome.new_version == 4


def test_ensure_base_version_direct() -> None:
    ensure_base_version(1, 1)
    with pytest.raises(AppError) as excinfo:
        ensure_base_version(2, 1)
    assert excinfo.value.code is ErrorCode.PATCH_BASE_VERSION_CONFLICT


# --------------------------------------------------------------------------- #
# 断言工具自身的可靠性（负例）
# --------------------------------------------------------------------------- #
def test_assert_only_subtree_changed_detects_unrelated_change(
    slide_deck_dict: dict[str, Any],
) -> None:
    before = copy.deepcopy(slide_deck_dict)
    mutated = copy.deepcopy(slide_deck_dict)
    mutated["slides"][0]["title"] = "偷偷改掉未锚定的节点"

    with pytest.raises(PatchInvariantError) as excinfo:
        # 只声明 /slides/1 被改，但实际改了 /slides/0 → 必须被抓到
        assert_only_subtree_changed(before, mutated, ("/slides/1/title",))
    assert "/slides/0/title" in excinfo.value.paths


def test_changed_paths_between_reports_leaf_paths(slide_deck_dict: dict[str, Any]) -> None:
    mutated = copy.deepcopy(slide_deck_dict)
    mutated["slides"][1]["title"] = "改标题"
    paths = changed_paths_between(slide_deck_dict, mutated)
    assert "/slides/1/title" in paths
    assert "/slides/0/title" not in paths


def test_sibling_index_prefix_is_not_treated_as_ancestor() -> None:
    """`/slides/1` 不是 `/slides/10` 的祖先 —— 裸 startswith 会误判，导致漏检。"""
    before = {"slides": [{"title": f"t{index}"} for index in range(11)]}
    after = copy.deepcopy(before)
    after["slides"][10]["title"] = "被改动的第 11 页"

    with pytest.raises(PatchInvariantError):
        assert_only_subtree_changed(before, after, ("/slides/1/title",))

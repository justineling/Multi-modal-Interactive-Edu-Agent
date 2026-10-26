"""契约层测试：三种 AST 的正反例 + PatchOp 规则 + SSE 编码。

覆盖的每条断言都对应一个 P0 验收点，注释里标了编号，便于回溯。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.artifacts import (
    SPEC_MODEL_BY_TYPE,
    Entity2D,
    Keyframe2DSpec,
    QuizQuestion,
    SceneGraph3DSpec,
    SlideDeckSpec,
    parse_artifact_spec,
)
from app.schemas.chat import (
    EVENT_ORDER,
    ArtifactEvent,
    ChatRequest,
    ErrorEvent,
    TokenEvent,
    TurnEndEvent,
    TurnStartEvent,
    encode_sse,
)
from app.schemas.common import canonical_json
from app.schemas.patch import PatchOp, PatchRequest


# --------------------------------------------------------------------------- #
# 正例：三种 AST 都能通过校验
# --------------------------------------------------------------------------- #
def test_slide_deck_spec_valid(slide_deck_dict: dict[str, Any]) -> None:
    spec = SlideDeckSpec.model_validate(slide_deck_dict)
    assert spec.type == "ppt"
    assert len(spec.slides) == 3
    # 默认值必须落成空列表而不是 None：前端按 string[] 消费，不会判空（§5.3）
    assert spec.slides[0].bullets == []


def test_keyframe_2d_spec_valid(keyframe_2d_dict: dict[str, Any]) -> None:
    spec = Keyframe2DSpec.model_validate(keyframe_2d_dict)
    assert spec.type == "anim_2d"
    assert spec.steps[1].entities[0].x == 460.0
    # 扩展字段 source_page 必须可空且能落到具体页码
    assert spec.steps[0].entities[0].source_page == 110


def test_scene_graph_3d_spec_valid(scene_graph_3d_dict: dict[str, Any]) -> None:
    spec = SceneGraph3DSpec.model_validate(scene_graph_3d_dict)
    assert spec.type == "scene_3d"
    assert spec.nodes[0].position == (0.0, 1.5, 0.0)
    assert spec.camera_tour[1].name == "剖面"


def test_spec_models_registry_matches_discriminator() -> None:
    assert set(SPEC_MODEL_BY_TYPE) == {"ppt", "anim_2d", "scene_3d"}
    assert SPEC_MODEL_BY_TYPE["ppt"] is SlideDeckSpec


# --------------------------------------------------------------------------- #
# 反例 1：缺字段 / 错 literal / 多余字段
# --------------------------------------------------------------------------- #
def test_missing_required_field_rejected(slide_deck_dict: dict[str, Any]) -> None:
    broken = dict(slide_deck_dict)
    broken["slides"] = [dict(slide) for slide in slide_deck_dict["slides"]]
    broken["slides"][0].pop("speaker_notes")
    with pytest.raises(ValidationError) as excinfo:
        SlideDeckSpec.model_validate(broken)
    assert "speaker_notes" in str(excinfo.value)


def test_wrong_layout_literal_rejected(slide_deck_dict: dict[str, Any]) -> None:
    broken = {
        **slide_deck_dict,
        "slides": [{**slide_deck_dict["slides"][0], "layout": "mind_map"}],
    }
    with pytest.raises(ValidationError):
        SlideDeckSpec.model_validate(broken)


def test_extra_field_rejected(slide_deck_dict: dict[str, Any]) -> None:
    """extra=forbid：LLM 自创字段必须当场拒绝，否则会悄悄流到前端并让指纹漂移。"""
    broken = {**slide_deck_dict, "theme": "dark"}
    with pytest.raises(ValidationError):
        SlideDeckSpec.model_validate(broken)


# --------------------------------------------------------------------------- #
# 反例 2：HTML / JS 注入（PRD P0-07②）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload_text",
    [
        "<script>alert(1)</script>",
        "<html><body>x</body></html>",
        '<img src=x onerror="alert(1)">',
        "javascript:alert(1)",
        "<div class='slide'>",
        "<style>.a{}</style>",
        "function() { return 1 }",
        "const a = (x) => x + 1",
        "import os",
        "THREE.Mesh",
    ],
)
def test_markup_injection_rejected(slide_deck_dict: dict[str, Any], payload_text: str) -> None:
    broken = {
        **slide_deck_dict,
        "slides": [{**slide_deck_dict["slides"][0], "speaker_notes": payload_text}],
    }
    with pytest.raises(ValidationError) as excinfo:
        SlideDeckSpec.model_validate(broken)
    assert "HTML/JS" in str(excinfo.value)


def test_injection_rejected_in_nested_list(scene_graph_3d_dict: dict[str, Any]) -> None:
    """嵌套结构（节点标注）也必须被扫到，不能只查顶层字段。"""
    nodes = [dict(node) for node in scene_graph_3d_dict["nodes"]]
    nodes[1]["annotation_markdown"] = "<script>bad()</script>"
    with pytest.raises(ValidationError):
        SceneGraph3DSpec.model_validate({**scene_graph_3d_dict, "nodes": nodes})


# --------------------------------------------------------------------------- #
# 反例 3：position 不是 3 元组 / 颜色不在调色板
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_position", [[1.0, 2.0], [1.0, 2.0, 3.0, 4.0], []])
def test_position_must_be_exactly_three_numbers(
    scene_graph_3d_dict: dict[str, Any], bad_position: list[float]
) -> None:
    nodes = [dict(node) for node in scene_graph_3d_dict["nodes"]]
    nodes[0]["position"] = bad_position
    with pytest.raises(ValidationError):
        SceneGraph3DSpec.model_validate({**scene_graph_3d_dict, "nodes": nodes})


def test_color_outside_palette_rejected(scene_graph_3d_dict: dict[str, Any]) -> None:
    nodes = [dict(node) for node in scene_graph_3d_dict["nodes"]]
    nodes[0]["color"] = "#123456"
    with pytest.raises(ValidationError) as excinfo:
        SceneGraph3DSpec.model_validate({**scene_graph_3d_dict, "nodes": nodes})
    assert "调色板" in str(excinfo.value)


def test_color_case_normalized() -> None:
    """LLM 常输出小写 hex，统一转大写后入库，保证同一语义色只有一种写法。"""
    entity = Entity2D(
        id="entity-0",
        shape="circle",
        label="柔佛",
        x=10.0,
        y=20.0,
        color="#2563eb",
        details="示例",
    )
    assert entity.color == "#2563EB"


def test_quiz_answer_index_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        QuizQuestion(question="?", options=["A", "B"], answer_index=2)


# --------------------------------------------------------------------------- #
# 判别联合
# --------------------------------------------------------------------------- #
def test_discriminated_union_dispatches_by_type(
    slide_deck_dict: dict[str, Any],
    keyframe_2d_dict: dict[str, Any],
    scene_graph_3d_dict: dict[str, Any],
) -> None:
    assert isinstance(parse_artifact_spec(slide_deck_dict), SlideDeckSpec)
    assert isinstance(parse_artifact_spec(keyframe_2d_dict), Keyframe2DSpec)
    assert isinstance(parse_artifact_spec(scene_graph_3d_dict), SceneGraph3DSpec)


def test_discriminated_union_rejects_unknown_type(slide_deck_dict: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_artifact_spec({**slide_deck_dict, "type": "video"})


# --------------------------------------------------------------------------- #
# PatchOp 规则（§5.2 表格）
# --------------------------------------------------------------------------- #
def test_patch_op_update_node_valid() -> None:
    op = PatchOp(op="update_node", node_path="/slides/2", payload={"title": "新标题"})
    assert op.segments == ("slides", "2")


@pytest.mark.parametrize(
    "node_path",
    ["/slides/2/title", "/foo/1", "slides/1", "/slides/-1", "/steps/1/nodes/0", "/slides/1/2"],
)
def test_patch_op_invalid_node_path_rejected(node_path: str) -> None:
    with pytest.raises(ValidationError):
        PatchOp(op="update_node", node_path=node_path, payload={"title": "x"})


@pytest.mark.parametrize(
    "node_path",
    ["/slides/2", "/slides", "/steps/1/entities/0", "/steps/1/entities", "/camera_tour/1", "/nodes/0"],
)
def test_patch_op_accepted_node_path_shapes(node_path: str) -> None:
    op = PatchOp(op="update_node", node_path=node_path, payload={"title": "x"})
    assert op.node_path == node_path


def test_patch_op_delete_forbids_payload() -> None:
    with pytest.raises(ValidationError):
        PatchOp(op="delete_node", node_path="/slides/1", payload={"title": "x"})


def test_patch_op_insert_requires_payload() -> None:
    with pytest.raises(ValidationError):
        PatchOp(op="insert_node", node_path="/slides/1")
    with pytest.raises(ValidationError):
        PatchOp(op="insert_node", node_path="/slides/1", payload={})


def test_patch_op_payload_forbids_type_field() -> None:
    with pytest.raises(ValidationError):
        PatchOp(op="update_node", node_path="/slides/1", payload={"type": "scene_3d"})


def test_patch_op_focus_camera_requires_preset_name() -> None:
    valid = PatchOp(op="focus_camera", node_path="/camera_tour/1", payload={"preset_name": "剖面"})
    assert valid.payload == {"preset_name": "剖面"}

    with pytest.raises(ValidationError):
        PatchOp(op="focus_camera", node_path="/camera_tour/1", payload={})
    with pytest.raises(ValidationError):
        PatchOp(op="focus_camera", node_path="/slides/1", payload={"preset_name": "剖面"})
    with pytest.raises(ValidationError):
        PatchOp(
            op="focus_camera",
            node_path="/camera_tour/1",
            payload={"preset_name": "剖面", "zoom": 2},
        )


def test_patch_request_bounds() -> None:
    op = PatchOp(op="update_node", node_path="/slides/1", payload={"title": "x"})
    with pytest.raises(ValidationError):
        PatchRequest(artifact_id="a", base_version=1, ops=[])
    with pytest.raises(ValidationError):
        PatchRequest(artifact_id="a", base_version=1, ops=[op] * 21)
    assert PatchRequest(artifact_id="a", base_version=1, ops=[op]).base_version == 1


# --------------------------------------------------------------------------- #
# 序列化一致性与 SSE 协议
# --------------------------------------------------------------------------- #
def test_canonical_json_is_key_order_insensitive() -> None:
    left = {"b": 1, "a": {"d": 4, "c": 3}}
    right = {"a": {"c": 3, "d": 4}, "b": 1}
    assert canonical_json(left) == canonical_json(right)
    assert canonical_json({"t": "中文"}) == '{"t":"中文"}'


def test_sse_encode_is_single_line(slide_deck_dict: dict[str, Any]) -> None:
    """§5.4 规则①：data 必须是单行紧凑 JSON，字符串里的换行要被转义。"""
    frame = encode_sse("token", TokenEvent(delta="第一行\n第二行"))
    lines = frame.split("\n")
    assert lines[0] == "event: token"
    assert lines[1].startswith("data: ")
    assert frame.endswith("\n\n")
    # 只允许出现一行 data:，换行必须被转义成 \n 字面量
    assert sum(1 for line in lines if line.startswith("data: ")) == 1
    assert "\\n" in lines[1]


def test_sse_event_order_matches_contract() -> None:
    assert EVENT_ORDER[0] == "turn_start"
    assert EVENT_ORDER[-1] == "turn_end"
    assert "token" in EVENT_ORDER and "artifact" in EVENT_ORDER


def test_turn_start_and_end_carry_demo_mode() -> None:
    """P0-12③：demo_mode 必须在 turn_start 与 turn_end 双处携带。"""
    start = TurnStartEvent(
        turn_id="turn-1",
        workspace_id="ws-1",
        mode="syllabus_strict",
        demo_mode=True,
        has_anchor=False,
        artifact_type_hint=None,
    )
    end = TurnEndEvent(turn_id="turn-1", duration_ms=120, ttft_ms=40, version=None, demo_mode=True)
    assert start.demo_mode is True
    assert end.demo_mode is True
    assert "demo_mode" in start.model_dump()
    assert "demo_mode" in end.model_dump()


def test_artifact_event_create_requires_full_spec(slide_deck_dict: dict[str, Any]) -> None:
    created = ArtifactEvent(
        action="create",
        artifact_id="art-1",
        artifact_type="ppt",
        version=1,
        title="4.1 Malayan Union",
        spec=slide_deck_dict,
    )
    assert created.spec is not None

    with pytest.raises(ValidationError):
        ArtifactEvent(action="create", artifact_id="art-1", artifact_type="ppt", version=1)


def test_artifact_event_patch_requires_ops() -> None:
    op = PatchOp(op="update_node", node_path="/slides/1", payload={"title": "x"})
    patched = ArtifactEvent(
        action="patch",
        artifact_id="art-1",
        artifact_type="ppt",
        version=2,
        base_version=1,
        ops=[op],
    )
    assert patched.ops is not None and patched.base_version == 1

    with pytest.raises(ValidationError):
        ArtifactEvent(
            action="patch", artifact_id="art-1", artifact_type="ppt", version=2, base_version=1
        )


def test_error_event_stage_and_retryable() -> None:
    event = ErrorEvent(
        code="CONTRACT_VALIDATION_FAILED",
        message="生成结果未通过契约校验，已自动重试",
        stage="validate",
        retryable=True,
        attempt=2,
    )
    assert event.retryable is True


def test_chat_request_defaults_to_syllabus_strict() -> None:
    request = ChatRequest(workspace_id="ws-1", query="4.1 讲了什么")
    assert request.mode == "syllabus_strict"
    assert request.anchor is None


# --------------------------------------------------------------------------- #
# A1 完成定义：/api/health 必须返回 demo_mode
# --------------------------------------------------------------------------- #
async def test_health_endpoint_reports_demo_mode(client: Any) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "OK"
    assert body["data"]["demo_mode"] is True
    assert body["data"]["artifact_types"] == ["ppt", "anim_2d", "scene_3d"]
    assert response.headers["x-request-id"]

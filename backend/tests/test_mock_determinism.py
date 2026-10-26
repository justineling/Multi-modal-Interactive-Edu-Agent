"""Mock 生成器确定性测试（P0-12②）。

核心断言：同一 `(query, section_code, artifact_type)` 两次生成**指纹相等**。
指纹用与 patch 引擎同一套 `canonical_json` + sha256，因此「键序变化」也会被抓到 ——
如果 mock 里用了 set 迭代或 dict 无序构造，测试会稳定失败。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.schemas.artifacts import PALETTE, parse_artifact_spec
from app.services import mock_generator
from app.services.mock_generator import (
    build_context,
    extract_facts,
    generate_patch_ops,
    generate_spec,
)
from app.services.patch_engine import apply_patch, fingerprint

TEXTS = (
    "Malayan Union 的构想源于战后重建成本与防务需要，英国希望统一行政以降低成本。"
    "1946 年 4 月 1 日，Malayan Union 正式生效，由英国总督直接统治。",
    "在 Malayan Union 宪制下，苏丹仅保留宗教与习俗事务的权限，政治权力被大幅削减。"
    "马来社会强烈反对，各地苏丹亦拒绝出席移交仪式。",
    "1948 年 2 月 1 日，马来亚联合邦成立，恢复了苏丹在州务上的权力，同时收紧公民权。",
)
PAGES = (72, 74, 78)
QUERY = "4.1 Malayan Union 对苏丹的影响"


def _context():
    return build_context(
        query=QUERY,
        section_code="4.1",
        section_title="Malayan Union",
        texts=TEXTS,
        pages=PAGES,
    )


# --------------------------------------------------------------------------- #
# 确定性
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("artifact_type", ["ppt", "anim_2d", "scene_3d"])
def test_same_input_yields_identical_fingerprint(artifact_type: str) -> None:
    first = generate_spec(artifact_type, _context())
    second = generate_spec(artifact_type, _context())
    assert fingerprint(first) == fingerprint(second)
    # 序列化字节也必须一致（指纹相等但键序不同是不可能的，这里再确认一次意图）
    from app.schemas.common import canonical_json

    assert canonical_json(first) == canonical_json(second)


def test_context_extraction_is_deterministic() -> None:
    assert extract_facts(TEXTS) == extract_facts(TEXTS)
    assert extract_facts(TEXTS), "必须能抽出事实句，否则生成内容会变成空壳"


@pytest.mark.parametrize("artifact_type", ["ppt", "anim_2d", "scene_3d"])
def test_generated_spec_passes_contract(artifact_type: str) -> None:
    spec = generate_spec(artifact_type, _context())
    parsed = parse_artifact_spec(spec)
    assert parsed.type == artifact_type


def test_ppt_covers_all_six_layouts() -> None:
    """P0-07①：6 种布局必须都能被 mock 产出（否则演示时总有一两种看不到）。"""
    spec = generate_spec("ppt", _context())
    layouts = {slide["layout"] for slide in spec["slides"]}
    assert layouts == {
        "title_hero",
        "bullet_points",
        "timeline",
        "comparison",
        "cause_effect",
        "quiz",
    }
    assert len(spec["slides"]) >= 5


def test_ppt_content_is_query_relevant() -> None:
    """不能是纯占位符：标题/要点必须出现章节或问题里的关键词。"""
    spec = generate_spec("ppt", _context())
    joined = " ".join(
        [spec["topic"]]
        + [slide["title"] for slide in spec["slides"]]
        + [bullet for slide in spec["slides"] for bullet in slide.get("bullets") or []]
    )
    assert "Malayan Union" in joined
    assert "苏丹" in joined


def test_ppt_colors_and_pages_are_valid() -> None:
    spec = generate_spec("ppt", _context())
    assert spec["slides"][0]["source_ref"].endswith("p.72")
    for slide in spec["slides"]:
        assert slide["speaker_notes"].strip()
        assert slide["source_ref"].strip()


def test_anim2d_has_steps_and_entities() -> None:
    spec = generate_spec("anim_2d", _context())
    assert len(spec["steps"]) >= 2
    for step in spec["steps"]:
        assert step["narration"].strip()
        assert len(step["entities"]) >= 2
        for entity in step["entities"]:
            assert entity["color"] in PALETTE
            assert 0 <= entity["x"] <= 1000
            assert 0 <= entity["y"] <= 600


def test_scene3d_has_three_annotation_points_and_camera_tour() -> None:
    """P0-09②：至少 3 个可点击标注点；P0-09 机位导览至少 2 个。"""
    spec = generate_spec("scene_3d", _context())
    annotations = [node for node in spec["nodes"] if node["annotation_markdown"].strip()]
    assert len(annotations) >= 3
    assert len(spec["camera_tour"]) >= 2
    for node in spec["nodes"]:
        assert node["color"] in PALETTE
        assert len(node["position"]) == 3
        assert all(abs(value) <= 20 for value in node["position"])


# --------------------------------------------------------------------------- #
# patch 指令
# --------------------------------------------------------------------------- #
def test_patch_ops_are_deterministic() -> None:
    spec = generate_spec("ppt", _context())
    first = [op.model_dump(mode="json") for op in generate_patch_ops(spec, node_path="/slides/2", context=_context())]
    second = [op.model_dump(mode="json") for op in generate_patch_ops(spec, node_path="/slides/2", context=_context())]
    assert first == second


def test_patch_ops_only_touch_anchored_node() -> None:
    """P0-10②：mock 的 patch 只改被锚定的节点，其余节点指纹不变。"""
    spec = generate_spec("ppt", _context())
    ops = generate_patch_ops(spec, node_path="/slides/2", context=_context())

    outcome = apply_patch(spec, ops)
    assert all(path.startswith("/slides/2") for path in outcome.changed_paths)
    assert fingerprint(outcome.spec["slides"][0]) == fingerprint(spec["slides"][0])
    assert fingerprint(outcome.spec["slides"][3]) == fingerprint(spec["slides"][3])


def test_patch_ops_for_all_three_types() -> None:
    cases = [
        ("ppt", "/slides/1"),
        ("anim_2d", "/steps/1"),
        ("anim_2d", "/steps/0/entities/1"),
        ("scene_3d", "/nodes/2"),
        ("scene_3d", "/camera_tour/1"),
    ]
    for artifact_type, node_path in cases:
        spec = generate_spec(artifact_type, _context())
        ops = generate_patch_ops(spec, node_path=node_path, context=_context())
        assert ops, f"{artifact_type} {node_path} 没有产出任何 op"
        # 全部 op 必须能落到 AST 上且通过校验
        outcome = apply_patch(spec, ops)
        assert outcome.model.type == artifact_type


def test_patch_ops_fallback_when_path_mismatch() -> None:
    """锚点与 spec 类型不匹配时回退到首个节点，而不是抛错让整轮对话失败。"""
    spec = generate_spec("ppt", _context())
    ops = generate_patch_ops(spec, node_path="/nodes/3", context=_context())
    assert ops[0].node_path == "/slides/0"
    apply_patch(spec, ops)


def test_scene3d_patch_also_moves_camera() -> None:
    spec = generate_spec("scene_3d", _context())
    ops = generate_patch_ops(spec, node_path="/nodes/1", context=_context())
    assert any(op.op == "focus_camera" for op in ops)
    outcome = apply_patch(spec, ops)
    assert outcome.focus_preset is not None
    # 视角指令不改内容：spec 只应因 update_node 而变化
    assert all(path.startswith("/nodes/1") for path in outcome.changed_paths)


# --------------------------------------------------------------------------- #
# 红线：源码级检查（防止后人顺手引入不确定性）
# --------------------------------------------------------------------------- #
def test_mock_generator_has_no_nondeterminism() -> None:
    """源码级红线检查。

    用 AST 而不是字符串匹配：模块文档字符串里就写着「禁止 random / hash()」，
    字符串匹配会把自己的说明当成违规，测试变成假阳性。
    """
    tree = ast.parse(inspect.getsource(mock_generator))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"random", "time", "uuid", "os"}, f"引入了不确定性模块：{imported}"

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "hash" not in called, "禁止使用内置 hash()（受 PYTHONHASHSEED 影响）"

    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attributes & {"now", "time", "urandom", "random", "uuid4"}


def test_mock_generator_file_exists() -> None:
    assert Path(mock_generator.__file__).exists()

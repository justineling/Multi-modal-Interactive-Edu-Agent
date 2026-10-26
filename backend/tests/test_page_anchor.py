"""页码锚定：事实溯源 + 生成后钳制（P0-05④ / 设计 §4.9）。

为什么这些断言必须存在（而不是「钳制逻辑看着对就行」）：

1. **钳制会掩盖提示词缺陷**。钳制之后「编造页码」变成「没有页码」，G3 看起来达标了，
   但根因（提示词不告诉模型页码）还在。所以这里锁死两件事：钳制规则本身，
   以及「契约示例里不许出现任何页码」—— 后者是根因的可见防线，改回去必须让测试变红。
2. **允许集合用错是静默错误**。若拿 `context.pages`（每段正文的起始页）当允许集合，
   「p.77 落在 p.76-77 切片内」这种合法页码会被误剥 —— 不报错、不告警，只是页码悄悄
   少了一半。第 2 条用例就是专门防这个。
3. **patch 路径不钳制 = 假页码复活**。`update_node` 的 payload 是局部字段 dict
   （IMPL-NOTES F1），只带 `{"source_ref": "4.3.1, p.76"}` 完全合法。若只有 create 钳制，
   用户点一次节点追问，刚剥掉的假页码就回来了 —— 用户可见的自相矛盾。
"""

from __future__ import annotations

import copy
import json

import pytest

from app.services.agent_engine import (
    _ARTIFACT_CONTRACT,
    _DEFAULT_ARTIFACT_CONTRACT,
    _FALLBACK_SOURCE_REF,
    _REF_PAGE_RE,
    _clamp_patch_ops,
    _clamp_ref,
    _clamp_spec_pages,
)
from app.schemas.patch import PatchOp
from app.services.mock_generator import build_context, extract_facts, extract_facts_with_pages

# 允许集合 = 83~91（4.3.1 小节的真实页范围）。76 是旧契约示例里硬编码的那个页码，
# 在这个小节里**越界** —— 这正是「模型照抄示例」会产生的那类错。
ALLOWED_4_3_1 = frozenset(range(83, 92))


def _ppt_spec(*refs: str) -> dict:
    """构造一个只关心 `source_ref` 的最小 PPT spec（字段齐全，能过契约校验）。"""
    return {
        "type": "ppt",
        "topic": "1955 年选举",
        "slides": [
            {
                "node_id": f"slide-{index + 1}",
                "layout": "bullet_points",
                "title": f"第 {index + 1} 页",
                "subtitle": None,
                "bullets": ["要点一", "要点二", "要点三"],
                "comparison_left": None,
                "comparison_right": None,
                "timeline_items": None,
                "quiz_questions": None,
                "speaker_notes": "1955 年的选举扩大了选民范围。",
                "source_ref": ref,
            }
            for index, ref in enumerate(refs)
        ],
    }


def _anim_spec(*pages: int | None) -> dict:
    return {
        "type": "anim_2d",
        "title": "选举推进过程",
        "steps": [
            {
                "step_index": 0,
                "narration": "选民范围逐步扩大。",
                "source_ref": "4.3.1",
                "entities": [
                    {
                        "id": f"entity-{index}",
                        "shape": "circle",
                        "label": "选民",
                        "x": 320.0,
                        "y": 300.0,
                        "color": "#2563EB",
                        "details": "选民群体的变化。",
                        "source_page": page,
                    }
                    for index, page in enumerate(pages)
                ],
            }
        ],
    }


def _scene_spec(*pages: int | None) -> dict:
    return {
        "type": "scene_3d",
        "title": "1955 年选举场景",
        "nodes": [
            {
                "id": f"node-{index}",
                "primitive": "box",
                "label": "投票站",
                "position": [0.0, 1.5, 0.0],
                "scale": [2.0, 1.0, 2.0],
                "rotation": [0.0, 0.0, 0.0],
                "color": "#2563EB",
                "annotation_markdown": "投票站按选区设置。",
                "source_page": page,
                "parent_id": None,
            }
            for index, page in enumerate(pages)
        ],
        "camera_tour": [
            {
                "name": "全景",
                "position": [8.0, 6.0, 8.0],
                "target": [0.0, 1.5, 0.0],
                "explanation": "先看整体布局。",
            }
        ],
    }


# --------------------------------------------------------------------------- #
# 1. 越界剥离 + 兜底链（绝不产生空字符串）
# --------------------------------------------------------------------------- #
def test_out_of_range_page_is_stripped() -> None:
    """`4.3.1, p.76` 在允许集合 83~91 里越界 → 只留章节号。"""
    spec = _ppt_spec("4.3.1, p.76")
    clamped, count = _clamp_spec_pages(
        spec, ALLOWED_4_3_1, fallback_ref="4.3.1"
    )

    assert count == 1
    assert clamped["slides"][0]["source_ref"] == "4.3.1"
    # 原 spec 不被就地改动（钳制是纯函数，调用方拿到的两份数据互不影响）
    assert spec["slides"][0]["source_ref"] == "4.3.1, p.76"


def test_stripped_ref_never_becomes_empty_string() -> None:
    """`source_ref` 只有页码时剥空 → 走兜底链，绝不返回空串。

    空串在 schema 里合法（`source_ref` 是无 min_length 的裸 str），但 UI 上等于
    「有出处字段却什么都不显示」，比明确写「拓展知识」更让人困惑。
    """
    spec = _ppt_spec("p.76")
    clamped, count = _clamp_spec_pages(spec, ALLOWED_4_3_1, fallback_ref="4.3.1")

    assert count == 1
    assert clamped["slides"][0]["source_ref"] == "4.3.1"
    assert clamped["slides"][0]["source_ref"] != ""


def test_fallback_chain_ends_at_extended_knowledge() -> None:
    """章节号也为空时落到 `Extended Knowledge`（与 `MockContext.source_ref` 同值）。"""
    spec = _ppt_spec("p.76")
    clamped, _ = _clamp_spec_pages(spec, ALLOWED_4_3_1, fallback_ref="")

    assert clamped["slides"][0]["source_ref"] == _FALLBACK_SOURCE_REF
    assert _FALLBACK_SOURCE_REF == "Extended Knowledge"


# --------------------------------------------------------------------------- #
# 2. 区间内不误剥（专防「用错允许集合」）
# --------------------------------------------------------------------------- #
def test_in_range_page_is_kept_verbatim() -> None:
    """允许集合含 76、77 时，`4.3.1, p.77` 必须**原样保留**。

    这条防的是「拿 `context.pages`（每段正文的起始页）当允许集合」的实现错误：
    那样 `p.77` 会被误剥，而且不报错、不告警，页码只是悄悄少了一半。
    """
    allowed = frozenset({76, 77})
    spec = _ppt_spec("4.3.1, p.77")

    clamped, count = _clamp_spec_pages(spec, allowed, fallback_ref="4.3.1")

    assert count == 0
    assert clamped["slides"][0]["source_ref"] == "4.3.1, p.77"


def test_no_page_at_all_is_not_touched() -> None:
    """本来就没写页码 → 不算越界、不计入 `page_clamped`。"""
    spec = _ppt_spec("4.3.1")
    clamped, count = _clamp_spec_pages(spec, ALLOWED_4_3_1, fallback_ref="4.3.1")

    assert count == 0
    assert clamped["slides"][0]["source_ref"] == "4.3.1"


def test_empty_allowed_set_disables_clamping() -> None:
    """允许集合为空 = 「这次没有可信页码」→ 不动手（钳掉一切反而更差）。"""
    spec = _ppt_spec("4.3.1, p.76")
    clamped, count = _clamp_spec_pages(spec, frozenset(), fallback_ref="4.3.1")

    assert count == 0
    assert clamped["slides"][0]["source_ref"] == "4.3.1, p.76"


# --------------------------------------------------------------------------- #
# 3. 区间写法：两端都合法才保留
# --------------------------------------------------------------------------- #
def test_range_fully_inside_is_kept() -> None:
    allowed = frozenset({83, 84, 85})
    spec = _ppt_spec("4.3.1, p.84-85")

    clamped, count = _clamp_spec_pages(spec, allowed, fallback_ref="4.3.1")

    assert count == 0
    assert clamped["slides"][0]["source_ref"] == "4.3.1, p.84-85"


def test_range_with_one_bad_endpoint_is_stripped() -> None:
    """一端越界即整条剥离，不做「部分合法宽容」（与 `_drop_invalid_citations` 同口径）。"""
    allowed = frozenset({83, 84, 85})
    spec = _ppt_spec("4.3.1, p.84-999")

    clamped, count = _clamp_spec_pages(spec, allowed, fallback_ref="4.3.1")

    assert count == 1
    assert clamped["slides"][0]["source_ref"] == "4.3.1"


def test_ref_page_regex_matches_single_and_range() -> None:
    """正则认得单页与范围两种写法，且不会把章节号里的点当成页码。"""
    assert [m.group(0).strip(" ,") for m in _REF_PAGE_RE.finditer("4.3.1, p.76")] == ["p.76"]
    assert [m.group(0).strip(" ,") for m in _REF_PAGE_RE.finditer("4.3.1, p.76-77")] == [
        "p.76-77"
    ]
    assert list(_REF_PAGE_RE.finditer("4.3.1")) == []


def test_clamp_ref_helper_is_pure_and_idempotent() -> None:
    """纯函数 + 幂等：同一输入两次得到同一结果，第二次不再改动（P0-12②）。"""
    once, touched_once = _clamp_ref("4.3.1, p.76", ALLOWED_4_3_1, "4.3.1")
    twice, touched_twice = _clamp_ref(once, ALLOWED_4_3_1, "4.3.1")

    assert (once, touched_once) == ("4.3.1", True)
    assert (twice, touched_twice) == ("4.3.1", False)


# --------------------------------------------------------------------------- #
# 4. 2D / 3D 的 source_page
# --------------------------------------------------------------------------- #
def test_anim_2d_out_of_range_source_page_becomes_null() -> None:
    spec = _anim_spec(76, 84)
    clamped, count = _clamp_spec_pages(spec, ALLOWED_4_3_1)

    assert count == 1
    assert clamped["steps"][0]["entities"][0]["source_page"] is None
    assert clamped["steps"][0]["entities"][1]["source_page"] == 84


def test_anim_2d_null_source_page_is_not_counted() -> None:
    """契约允许 null（= 模型本来就没写），不该算成「被钳制」。"""
    spec = _anim_spec(None)
    clamped, count = _clamp_spec_pages(spec, ALLOWED_4_3_1)

    assert count == 0
    assert clamped["steps"][0]["entities"][0]["source_page"] is None


def test_scene_3d_out_of_range_source_page_becomes_null() -> None:
    spec = _scene_spec(76, 85)
    clamped, count = _clamp_spec_pages(spec, ALLOWED_4_3_1)

    assert count == 1
    assert clamped["nodes"][0]["source_page"] is None
    assert clamped["nodes"][1]["source_page"] == 85


def test_ppt_timeline_item_source_page_is_clamped() -> None:
    """`TimelineItem.source_page` 与 2D/3D 同规则（设计 §4.9.2 规则表第 2 行）。"""
    spec = _ppt_spec("4.3.1")
    spec["slides"][0]["timeline_items"] = [
        {"year": "1955", "label": "选举", "detail": "扩大选民范围", "source_page": 76}
    ]

    clamped, count = _clamp_spec_pages(spec, ALLOWED_4_3_1, fallback_ref="4.3.1")

    assert count == 1
    assert clamped["slides"][0]["timeline_items"][0]["source_page"] is None


# --------------------------------------------------------------------------- #
# 4b. 非 int 形态的 source_page（P1：钳制绕过 + SSE/DB 字节不一致）
# --------------------------------------------------------------------------- #
# 背景：`source_page` 在 schema 里是 `int | None`，但 pydantic 的 lax 模式会放行
# `"999"` / `999.0`。旧实现只判 `isinstance(page, int)`，这些形态**整条跳过钳制**，
# 越界页码原样落库；同时 SSE 走 pydantic 序列化成 `999`、DB 存原始 `"999"`，
# 同一份产物两个出口字节不一致。下面这组用例把「规范化 + 写回」一起锁住。
@pytest.mark.parametrize("raw", ["999", 999.0, True])
def test_non_int_out_of_range_source_page_is_clamped(raw: object) -> None:
    """三种「语义上越界」的非 int 形态都必须被剥成 `None` 并计数。"""
    spec = _anim_spec(raw)  # type: ignore[arg-type]
    clamped, count = _clamp_spec_pages(spec, ALLOWED_4_3_1)

    assert count == 1
    assert clamped["steps"][0]["entities"][0]["source_page"] is None


@pytest.mark.parametrize("raw", ["85", 85.0])
def test_non_int_in_range_source_page_is_normalized_to_int(raw: object) -> None:
    """区间内的非 int 形态不剥，但必须**写回 int** —— 否则 SSE 与 DB 字节不一致。

    这条是 P1 的另一半：只判断不写回，用户看到的 `source_page` 类型取决于它从哪个
    出口来（事件走 pydantic → int；库里是原始 `"85"`）。同一个值两种类型，
    前端指纹自检与「版本比对」都会莫名其妙地不等。
    """
    spec = _anim_spec(raw)  # type: ignore[arg-type]
    clamped, count = _clamp_spec_pages(spec, ALLOWED_4_3_1)
    value = clamped["steps"][0]["entities"][0]["source_page"]

    assert count == 0, "区间内的值不算越界，不该计入 page_clamped"
    assert value == 85
    assert type(value) is int, f"必须写回 int，实际是 {type(value).__name__}"


@pytest.mark.parametrize("raw", [85.5, "abc", "", [85], {"page": 85}])
def test_dirty_source_page_is_dropped_not_passed_through(raw: object) -> None:
    """无法规范化的脏值一律置 `None`：不因为「类型怪」就放行。

    `85.5` 特别重要：它不是页码，但 `int(85.5)` 会静默截断成 85 —— 一旦有人图省事
    用 `int()` 兜底，脏数据就会被「修」成一个看起来合法的页码。
    """
    spec = _anim_spec(raw)  # type: ignore[arg-type]
    clamped, count = _clamp_spec_pages(spec, ALLOWED_4_3_1)

    assert count == 1
    assert clamped["steps"][0]["entities"][0]["source_page"] is None


def test_clamped_spec_and_model_dump_agree_on_source_page() -> None:
    """钳制后的 spec 与 pydantic 序列化结果必须逐字段一致（SSE/DB 同源）。

    `ArtifactEvent.spec` 会经 pydantic 序列化后下发，而库里存的是同一个 dict ——
    如果钳制只改判断不改写回，两条路径的类型就会分叉。这里直接比对两个出口。
    """
    from app.schemas.artifacts import parse_artifact_spec

    spec = _scene_spec("85", 85.0, 999, None)  # type: ignore[arg-type]
    clamped, _ = _clamp_spec_pages(spec, ALLOWED_4_3_1)
    dumped = parse_artifact_spec(copy.deepcopy(clamped)).model_dump(mode="json")

    for index, node in enumerate(clamped["nodes"]):
        assert node["source_page"] == dumped["nodes"][index]["source_page"]
        assert type(node["source_page"]) is type(dumped["nodes"][index]["source_page"])


def test_patch_payload_non_int_source_page_is_clamped() -> None:
    """patch 不是绕过通道：payload 里的 `"999"` 同样被剥。"""
    ops = [
        PatchOp(
            op="update_node",
            node_path="/steps/0",
            payload={"source_page": "999", "source_ref": "4.3.1, p.999"},
            reason="补充出处",
        )
    ]

    clamped_ops, count = _clamp_patch_ops(ops, ALLOWED_4_3_1, fallback_ref="4.3.1")

    assert count == 2
    assert clamped_ops[0].payload == {"source_page": None, "source_ref": "4.3.1"}


def test_patch_payload_non_int_in_range_page_is_normalized() -> None:
    """patch payload 里区间内的 `"85"` 也要写回 int（与 create 路径同规则）。"""
    ops = [
        PatchOp(
            op="update_node",
            node_path="/steps/0",
            payload={"source_page": "85"},
            reason="补充出处",
        )
    ]

    clamped_ops, count = _clamp_patch_ops(ops, ALLOWED_4_3_1)

    assert count == 0
    assert clamped_ops[0].payload == {"source_page": 85}
    assert type(clamped_ops[0].payload["source_page"]) is int  # type: ignore[index]


# --------------------------------------------------------------------------- #
# 5. patch 路径同样钳制（② 的核心）
# --------------------------------------------------------------------------- #
def test_patch_update_node_payload_ref_is_clamped() -> None:
    """`update_node` 只带一个 `source_ref` 字段时也必须被钳制 —— 否则假页码会复活。"""
    ops = [
        PatchOp(
            op="update_node",
            node_path="/slides/1",
            payload={"source_ref": "4.3.1, p.76"},
            reason="补充出处",
        )
    ]

    clamped_ops, count = _clamp_patch_ops(
        ops, ALLOWED_4_3_1, fallback_ref="4.3.1"
    )

    assert count == 1
    assert clamped_ops[0].payload == {"source_ref": "4.3.1"}


def test_patch_replace_subtree_payload_is_clamped_recursively() -> None:
    """`replace_subtree` 的 payload 是整棵节点 → 必须递归，字段列举法一定漏。"""
    node = {
        "node_id": "slide-2",
        "layout": "timeline",
        "title": "第 2 页",
        "bullets": [],
        "source_ref": "4.3.1, p.76",
        "timeline_items": [
            {"year": "1955", "label": "选举", "detail": "…", "source_page": 76}
        ],
        "quiz_questions": None,
        "speaker_notes": "…",
    }
    ops = [PatchOp(op="replace_subtree", node_path="/slides/1", payload=node, reason="重写")]

    clamped_ops, count = _clamp_patch_ops(ops, ALLOWED_4_3_1, fallback_ref="4.3.1")
    payload = clamped_ops[0].payload or {}

    assert count == 2
    assert payload["source_ref"] == "4.3.1"
    assert payload["timeline_items"][0]["source_page"] is None


def test_patch_in_range_payload_is_untouched() -> None:
    ops = [
        PatchOp(
            op="update_node",
            node_path="/slides/1",
            payload={"source_ref": "4.3.1, p.85", "source_page": 85},
            reason="补充出处",
        )
    ]

    clamped_ops, count = _clamp_patch_ops(ops, ALLOWED_4_3_1, fallback_ref="4.3.1")

    assert count == 0
    assert clamped_ops[0].payload == {"source_ref": "4.3.1, p.85", "source_page": 85}


def test_patch_focus_camera_payload_is_untouched() -> None:
    """`focus_camera` 的 payload 只有 preset_name，递归遍历不应误改它。"""
    ops = [
        PatchOp(
            op="focus_camera",
            node_path="/camera_tour/0",
            payload={"preset_name": "全景"},
            reason="切换视角",
        )
    ]

    clamped_ops, count = _clamp_patch_ops(ops, ALLOWED_4_3_1)

    assert count == 0
    assert clamped_ops[0].payload == {"preset_name": "全景"}


# --------------------------------------------------------------------------- #
# 6. 契约示例里不许出现任何页码（① 的防线）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("artifact_type", ["ppt", "anim_2d", "scene_3d"])
def test_contract_examples_contain_no_page_numbers(artifact_type: str) -> None:
    """契约示例只教结构，不教数字。

    曾经示例里写死 `p.76`，模型照抄到所有小节 → 「页码缺失」变成「页码静默错误」。
    这条断言是那道修复的防线：谁把页码塞回示例，这里就会红。
    """
    contract = _ARTIFACT_CONTRACT[artifact_type]

    assert _REF_PAGE_RE.search(contract.split("最小合法示例")[-1]) is None
    assert "__EXAMPLE_PAGE__" not in contract


def test_contract_examples_still_pass_validation() -> None:
    """示例本身必须过 `parse_artifact_spec`（否则等于教模型写错格式）。"""
    from app.schemas.artifacts import parse_artifact_spec

    for artifact_type, contract in _ARTIFACT_CONTRACT.items():
        raw = contract.split("最小合法示例")[-1].split("：", 1)[-1].strip()
        payload = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        parsed = parse_artifact_spec(copy.deepcopy(payload))
        assert parsed.type == artifact_type


def test_default_contract_is_ppt() -> None:
    assert _DEFAULT_ARTIFACT_CONTRACT == _ARTIFACT_CONTRACT["ppt"]


# --------------------------------------------------------------------------- #
# 7. 事实与页码的对应关系（确定性 + 旧接口回归）
# --------------------------------------------------------------------------- #
TEXTS = (
    "Malayan Union 的构想源于战后重建成本与防务需要，英国希望统一行政以降低成本。"
    "1946 年 4 月 1 日，Malayan Union 正式生效，由英国总督直接统治。",
    "在 Malayan Union 宪制下，苏丹仅保留宗教与习俗事务的权限，政治权力被大幅削减。"
    "马来社会强烈反对，各地苏丹亦拒绝出席移交仪式。",
)
PAGES = (72, 74)


def _build(allowed: tuple[int, ...] = ()) -> object:
    return build_context(
        query="4.1 Malayan Union 对苏丹的影响",
        section_code="4.1",
        section_title="Malayan Union",
        texts=TEXTS,
        pages=PAGES,
        allowed_pages=allowed,
    )


def test_fact_pages_align_with_facts() -> None:
    """`fact_pages` 与 `facts` 逐条平行，且页码来自对应切片的起始页。"""
    context = _build(allowed=(72, 73, 74, 75))

    assert len(context.fact_pages) == len(context.facts)
    assert set(context.fact_pages) <= {72, 74}
    # 第一条事实来自第一段正文
    assert context.fact_pages[0] == 72


def test_build_context_is_deterministic() -> None:
    """两次 `build_context` 的 `facts` / `fact_pages` 完全一致（P0-12②）。"""
    first = _build(allowed=(72, 74))
    second = _build(allowed=(72, 74))

    assert first.facts == second.facts
    assert first.fact_pages == second.fact_pages
    assert first.allowed_pages == second.allowed_pages == (72, 74)


def test_allowed_pages_does_not_degrade_to_pages() -> None:
    """缺省 `allowed_pages` 时保持为空 —— **不**退化成 `pages`（见 docstring）。"""
    context = _build()

    assert context.pages == (72, 74)
    assert context.allowed_pages == ()


def test_extract_facts_output_is_unchanged() -> None:
    """`extract_facts` 是旧接口（被 mock 生成器与确定性测试消费），输出必须逐字不变。"""
    expected = (
        "Malayan Union 的构想源于战后重建成本与防务需要，英国希望统一行政以降低成本",
        "1946 年 4 月 1 日，Malayan Union 正式生效，由英国总督直接统治",
        "在 Malayan Union 宪制下，苏丹仅保留宗教与习俗事务的权限，政治权力被大幅削减",
        "马来社会强烈反对，各地苏丹亦拒绝出席移交仪式",
    )

    assert extract_facts(TEXTS) == expected
    # 与带页码版本同源：去掉页码后必须逐字相等
    assert extract_facts(TEXTS) == tuple(
        fact for fact, _ in extract_facts_with_pages(TEXTS, PAGES)
    )


def test_extract_facts_handles_missing_pages() -> None:
    """页码缺失/越界时记 `None`，绝不瞎填一个数字。"""
    facts = extract_facts_with_pages(TEXTS, ())

    assert facts
    assert all(page is None for _, page in facts)


def test_empty_retrieval_still_yields_one_fact_without_page() -> None:
    """检索为空也要能出东西，但那一条兜底事实**不带页码**（不得编造）。"""
    context = build_context(
        query="4.1 随便问问",
        section_code="4.1",
        section_title="Malayan Union",
        texts=(),
        pages=(),
        allowed_pages=(),
    )

    assert len(context.facts) == 1
    assert context.fact_pages == (None,)

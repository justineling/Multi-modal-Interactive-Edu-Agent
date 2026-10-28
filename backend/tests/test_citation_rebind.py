"""后置确定性页码重绑定：`source_ref` 的页码必须落在**真实命中**的切片上。

**这个文件守的是「页码真实性」，不是「页码分散度」。** 两者的区别是本文件最容易被
后人改错的地方，所以写在最前面：

- LLM 生成多页产物时，`source_ref` 的页码会因自回归注意力惯性被**复制**。实测一份 6 页
  课件 6 次全部写 `p.71`，而内容全部来自 `4.1.3`（真实页码 `p.76-77`）。重绑定**前**的
  `p.71` 是**假的**（它甚至不在核心命中切片里），重绑定**后**的 `p.76-77` 是**真的**。
- 因此「重绑定后 6 页仍然全部是 `p.76-77`」是**正确**结果 —— 内容确实同源。谁要是为了
  「让页码看起来分散」而往兜底里塞次优页码，就是把对的改错。本文件的
  `test_single_section_focus_all_six_slides_land_on_the_same_chunk` 会立刻发现这种改动。

覆盖的边界：
1. 单节聚焦（6 页同源）→ 全部落到同一片段；
2. 跨节概览（各页不同子节）→ 各页落到各自片段；
3. `quiz` / `title_hero` 兜底 = **内容页命中切片的并集**，**不是**召回池全集跨度；
4. 全军覆没（所有页都不到置信度）→ 才退化为召回池跨度，且不崩；
5. 幂等（对已正确的 spec 再跑一次结果不变）—— 重试 / 回放路径会重复处理同一份 spec；
6. 引擎接线：`_generate_spec_stream` 里顺序是 **校验 → 数量 → 重绑定 → 钳制**。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.services.agent_engine import (
    AgentEngine,
    TurnRequest,
    _rebind_spec_source_pages,
)
from app.services.mock_generator import MockContext
from app.services.retriever import RetrievedChunk
from app.services.router import route
from app.services.text_signals import content_words

# --------------------------------------------------------------------------- #
# 语料：四个真实形态的切片（取自 data/app.db 里 sample_textbook.pdf 的 4.1 节）
# --------------------------------------------------------------------------- #
SEC_41 = (
    "4.1 Malayan Union\n"
    "Malayan Union 于 1946 年 4 月 1 日正式生效，是英国战后重建计划的核心。"
)
SEC_411 = (
    "4.1.1 Latar Belakang Malayan Union\n"
    "Malayan Union 的构想源于战后重建成本与防务需要，英国希望统一行政以降低成本。"
    "1945 年 10 月，英国派遣特使到马来亚，以威迫方式促使各州苏丹签署新宪制协议。"
    "Malayan Union 把各州与海峡殖民地合并为单一殖民地，由英国总督直接统治。"
)
SEC_412 = (
    "4.1.2 Kuasa Sultan\n"
    "在 Malayan Union 宪制下，苏丹仅保留宗教与习俗事务的权限，政治权力被大幅削减。"
    "苏丹不再拥有对州务的实质否决权，各州主权被移交给英国总督。"
    "公民权采属地原则，宽松的归化条件使非马来人人口结构面临改变。"
)
SEC_413 = (
    "4.1.3 Reaksi Masyarakat\n"
    "马来社会强烈反对 Malayan Union，认为它削弱苏丹地位并威胁马来人政治地位。"
    "1946 年 3 月，全马马来人代表大会在吉隆坡召开，各地苏丹亦拒绝出席移交仪式。"
    "反对运动促成巫统成立，并最终迫使英国以马来亚联合邦取代 Malayan Union。"
)


def _chunk(code: str, page_start: int, page_end: int, content: str, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chunk-{code}",
        section_code=code,
        section_title=code,
        page_start=page_start,
        page_end=page_end,
        score=score,
        snippet=content[:60],
        content=content,
    )


def _pool() -> tuple[RetrievedChunk, ...]:
    """一份**真实形态**的召回池：含上一节的开头页 p.71（这是兜底最容易踩的坑）。"""
    return (
        _chunk("4.1.3", 76, 77, SEC_413, 0.59),
        _chunk("4.1.2", 74, 75, SEC_412, 0.58),
        _chunk("4.1.1", 72, 73, SEC_411, 0.56),
        _chunk("4.1", 71, 71, SEC_41, 0.38),
    )


def _slide(
    node_id: str,
    layout: str,
    *,
    title: str,
    source_ref: str,
    subtitle: str | None = None,
    bullets: list[str] | None = None,
    comparison_left: list[str] | None = None,
    comparison_right: list[str] | None = None,
    timeline_items: list[dict[str, Any]] | None = None,
    quiz_questions: list[dict[str, Any]] | None = None,
    speaker_notes: str = "",
) -> dict[str, Any]:
    """构造一页 slide dict（键齐全，与 `_ARTIFACT_CONTRACT` 的 ppt 契约一致）。"""
    return {
        "node_id": node_id,
        "layout": layout,
        "title": title,
        "subtitle": subtitle,
        "bullets": bullets or [],
        "comparison_left": comparison_left,
        "comparison_right": comparison_right,
        "timeline_items": timeline_items,
        "quiz_questions": quiz_questions,
        "speaker_notes": speaker_notes,
        "source_ref": source_ref,
    }


def _deck(slides: list[dict[str, Any]], topic: str = "Reaksi Masyarakat") -> dict[str, Any]:
    return {"type": "ppt", "topic": topic, "slides": slides}


def _refs(spec: dict[str, Any]) -> list[str]:
    return [str(slide["source_ref"]) for slide in spec["slides"]]


# --------------------------------------------------------------------------- #
# 1. 单节聚焦：6 页全部写假的 p.71，重绑定后必须**全部**是 p.76-77
# --------------------------------------------------------------------------- #
def _single_section_deck(fake_ref: str = "4.1.3, p.71") -> dict[str, Any]:
    """6 页课件，内容**全部**来自 4.1.3 —— 复现线上那份真实产物。"""
    return _deck(
        [
            _slide(
                "slide-1",
                "title_hero",
                title="Reaksi Masyarakat",
                subtitle="4.1.3",
                speaker_notes="这一节讲马来社会对 Malayan Union 的反应。",
                source_ref=fake_ref,
            ),
            _slide(
                "slide-2",
                "bullet_points",
                title="核心要点",
                bullets=[
                    "马来社会强烈反对 Malayan Union",
                    "认为它削弱苏丹地位并威胁马来人政治地位",
                    "1946 年 3 月全马马来人代表大会在吉隆坡召开",
                    "各地苏丹亦拒绝出席移交仪式",
                ],
                speaker_notes="反对运动促成巫统成立。",
                source_ref=fake_ref,
            ),
            _slide(
                "slide-3",
                "timeline",
                title="时间脉络",
                timeline_items=[
                    {
                        "year": "1946",
                        "label": "马来社会强烈反对 Malayan Union",
                        "detail": "认为它削弱苏丹地位并威胁马来人政治地位",
                        "source_page": 71,
                    },
                    {
                        "year": "3 月",
                        "label": "全马马来人代表大会在吉隆坡召开",
                        "detail": "各地苏丹亦拒绝出席移交仪式",
                        "source_page": 72,
                    },
                ],
                speaker_notes="反对运动的时间线只有两个节点。",
                source_ref=fake_ref,
            ),
            _slide(
                "slide-4",
                "comparison",
                title="前后对照",
                comparison_left=[
                    "马来社会强烈反对 Malayan Union",
                    "认为它削弱苏丹地位",
                ],
                comparison_right=[
                    "反对运动促成巫统成立",
                    "迫使英国以马来亚联合邦取代 Malayan Union",
                ],
                speaker_notes="左右两栏对照着看。",
                source_ref=fake_ref,
            ),
            _slide(
                "slide-5",
                "cause_effect",
                title="原因与影响",
                bullets=[
                    "原因：Malayan Union 威胁马来人政治地位",
                    "影响：巫统成立，马来亚联合邦取代 Malayan Union",
                ],
                speaker_notes="先讲原因，再讲影响。",
                source_ref=fake_ref,
            ),
            _slide(
                "slide-6",
                "quiz",
                title="随堂测验",
                quiz_questions=[
                    {
                        "question": "下列哪一项符合本节描述？",
                        "options": ["选项甲", "选项乙", "选项丙"],
                        "answer_index": 0,
                        "explanation": "见本节正文。",
                    }
                ],
                speaker_notes="做完再对答案。",
                source_ref=fake_ref,
            ),
        ]
    )


def test_single_section_focus_all_six_slides_land_on_the_same_chunk() -> None:
    """单节聚焦：6 页全部写假的 `p.71` → 重绑定后 6 页**全部** `4.1.3, p.76-77`。

    **页码仍然全部相同是正确结果**：内容确实同源。谁把这里改成「要求页码分散」，
    这条断言就会红 —— 那是把对的改错。
    """
    rebound, changed = _rebind_spec_source_pages(
        _single_section_deck(), _pool(), section_code="4.1.3"
    )

    assert _refs(rebound) == ["4.1.3, p.76-77"] * 6
    assert changed >= 6  # 6 处 source_ref + 2 处 timeline source_page
    # 重绑定前的假页码确实不在核心命中切片里 —— 这正是「重绑定前是假的」的定义
    assert "4.1.3, p.71" not in _refs(rebound)


def test_timeline_item_source_page_is_rebound_too() -> None:
    """时间轴条目自带的 `source_page` 同样是编的（真实样本里出现 71/72/74）。"""
    rebound, _ = _rebind_spec_source_pages(
        _single_section_deck(), _pool(), section_code="4.1.3"
    )

    pages = [item["source_page"] for item in rebound["slides"][2]["timeline_items"]]
    assert pages == [76, 76]


# --------------------------------------------------------------------------- #
# 2. 跨节概览：3 页分别对应 p.72-73 / p.74-75 / p.76-77
# --------------------------------------------------------------------------- #
def test_cross_section_deck_binds_each_slide_to_its_own_chunk() -> None:
    """跨节概览：各页讲不同子节 → 各页落到**各自的**切片，不是同一个页码。"""
    deck = _deck(
        [
            _slide(
                "slide-1",
                "title_hero",
                title="Malayan Union 的三个层面",
                subtitle="4.1",
                speaker_notes="背景、苏丹权力、社会反应。",
                source_ref="4.1, p.71",
            ),
            _slide(
                "slide-2",
                "bullet_points",
                title="背景",
                bullets=[
                    "Malayan Union 的构想源于战后重建成本与防务需要",
                    "英国希望统一行政以降低成本",
                    "1945 年 10 月英国派遣特使到马来亚",
                    "以威迫方式促使各州苏丹签署新宪制协议",
                ],
                speaker_notes="构想来自战后重建成本与防务需要。",
                source_ref="4.1, p.71",
            ),
            _slide(
                "slide-3",
                "bullet_points",
                title="苏丹权力",
                bullets=[
                    "在 Malayan Union 宪制下苏丹仅保留宗教与习俗事务的权限",
                    "政治权力被大幅削减",
                    "苏丹不再拥有对州务的实质否决权",
                    "各州主权被移交给英国总督",
                ],
                speaker_notes="公民权采属地原则，宽松的归化条件改变了人口结构。",
                source_ref="4.1, p.71",
            ),
            _slide(
                "slide-4",
                "bullet_points",
                title="社会反应",
                bullets=[
                    "马来社会强烈反对 Malayan Union",
                    "认为它削弱苏丹地位并威胁马来人政治地位",
                    "1946 年 3 月全马马来人代表大会在吉隆坡召开",
                    "各地苏丹亦拒绝出席移交仪式",
                ],
                speaker_notes="反对运动促成巫统成立。",
                source_ref="4.1, p.71",
            ),
            _slide(
                "slide-5",
                "quiz",
                title="随堂测验",
                quiz_questions=[
                    {
                        "question": "苏丹在 Malayan Union 宪制下保留了什么权限？",
                        "options": ["宗教与习俗", "防务", "外交"],
                        "answer_index": 0,
                        "explanation": "见正文。",
                    }
                ],
                speaker_notes="做完再对答案。",
                source_ref="4.1, p.71",
            ),
        ]
    )

    rebound, _ = _rebind_spec_source_pages(deck, _pool(), section_code="4.1")
    refs = _refs(rebound)

    assert refs[1] == "4.1.1, p.72-73"
    assert refs[2] == "4.1.2, p.74-75"
    assert refs[3] == "4.1.3, p.76-77"
    # 兜底页 = 内容页命中切片的并集（72~77），不是召回池全集（71~77）
    assert refs[0] == "4.1, p.72-77"
    assert refs[4] == "4.1, p.72-77"


# --------------------------------------------------------------------------- #
# 3 / 4. 兜底页：quiz 与 title_hero 必须用「内容页命中切片的并集」
# --------------------------------------------------------------------------- #
def test_quiz_falls_back_to_content_union_not_pool_span() -> None:
    """quiz 页实词重叠 0 → 绑内容页并集 `p.76-77`，**不是**召回池跨度 `p.71-77`。

    这是本功能最容易写错的一处：拿 `chunks` 全集跨度当兜底，会把 `4.1` 的开头页
    `p.71` 一起圈进去，兜底反而比现状更差。
    """
    deck = _deck(
        [
            _slide(
                "slide-1",
                "bullet_points",
                title="核心要点",
                bullets=[
                    "马来社会强烈反对 Malayan Union",
                    "认为它削弱苏丹地位并威胁马来人政治地位",
                    "1946 年 3 月全马马来人代表大会在吉隆坡召开",
                ],
                speaker_notes="反对运动促成巫统成立。",
                source_ref="4.1.3, p.71",
            ),
            _slide(
                "slide-2",
                "quiz",
                title="随堂测验",
                quiz_questions=[
                    {
                        "question": "zzz qqq xxx",
                        "options": ["aaa", "bbb"],
                        "answer_index": 0,
                        "explanation": "zzz",
                    }
                ],
                speaker_notes="zzz qqq xxx aaa bbb",
                source_ref="4.1.3, p.71",
            ),
        ]
    )

    rebound, _ = _rebind_spec_source_pages(deck, _pool(), section_code="4.1.3")

    assert _refs(rebound)[0] == "4.1.3, p.76-77"
    assert _refs(rebound)[1] == "4.1.3, p.76-77"
    assert "4.1.3, p.71-77" not in _refs(rebound)


def test_title_hero_falls_back_to_content_union() -> None:
    """`title_hero` 不进第一阶段（天生只有概括词），直接绑内容页并集。"""
    deck = _deck(
        [
            _slide(
                "slide-1",
                "title_hero",
                title="Malayan Union",
                subtitle="4.1",
                speaker_notes="本节概览。",
                source_ref="4.1, p.71",
            ),
            _slide(
                "slide-2",
                "bullet_points",
                title="苏丹权力",
                bullets=[
                    "在 Malayan Union 宪制下苏丹仅保留宗教与习俗事务的权限",
                    "政治权力被大幅削减",
                    "苏丹不再拥有对州务的实质否决权",
                ],
                speaker_notes="各州主权被移交给英国总督。",
                source_ref="4.1, p.71",
            ),
        ]
    )

    rebound, _ = _rebind_spec_source_pages(deck, _pool(), section_code="4.1")

    assert _refs(rebound)[1] == "4.1.2, p.74-75"
    # 兜底 = {74,75}，绝不是召回池的 71~77
    assert _refs(rebound)[0] == "4.1, p.74-75"


# --------------------------------------------------------------------------- #
# 5. 全军覆没：才退化为召回池跨度，且不崩
# --------------------------------------------------------------------------- #
def test_all_slides_below_confidence_degrades_to_pool_span() -> None:
    """所有页覆盖率 < 0.20 → 退化为召回池全集跨度 `p.71-77`，且不抛异常。"""
    unrelated = [
        "量子纠缠在低温超导体系中表现出宏观相干性",
        "薛定谔方程的解给出波函数的演化",
        "拓扑绝缘体的表面态受时间反演对称性保护",
    ]
    deck = _deck(
        [
            _slide(
                "slide-1",
                "bullet_points",
                title="量子纠缠",
                bullets=unrelated,
                speaker_notes="超导体系的宏观相干性。",
                source_ref="9.9, p.999",
            ),
            _slide(
                "slide-2",
                "cause_effect",
                title="波函数",
                bullets=unrelated,
                speaker_notes="薛定谔方程的解。",
                source_ref="9.9, p.999",
            ),
        ]
    )

    rebound, changed = _rebind_spec_source_pages(deck, _pool(), section_code="4.1")

    assert _refs(rebound) == ["4.1, p.71-77", "4.1, p.71-77"]
    assert changed == 2


def test_empty_chunks_leaves_spec_untouched() -> None:
    """没有切片就没有「真实页码」：原样返回，绝不凭空造页码。"""
    deck = _single_section_deck()
    rebound, changed = _rebind_spec_source_pages(deck, (), section_code="4.1.3")

    assert rebound == deck
    assert changed == 0


def test_page_already_inside_matched_chunk_is_preserved() -> None:
    """既有页码已落在命中切片内 → **保留原值**，不退化成整段区间。

    重绑定的职责是修正**说错**的页码，不是把「精确且正确」重写成「笼统且正确」。
    `p.77` 落在 `4.1.3` 的 `p.76-77` 之内，它本身就是真话；改写成 `p.76-77` 会白白
    丢掉模型的精度，而且会让每一页都长得一样 —— 那才是「看起来整齐、实际更差」。
    """
    deck = _single_section_deck("4.1.3, p.77")
    rebound, changed = _rebind_spec_source_pages(deck, _pool(), section_code="4.1.3")

    assert _refs(rebound) == ["4.1.3, p.77"] * 6
    # 只有时间轴条目的 source_page（71/72）不真，需要改；source_ref 一处都不该动
    assert changed == 2


def test_wrong_section_page_is_corrected_even_if_number_exists_elsewhere() -> None:
    """页码落在**别的**切片里也算错：`p.72` 在召回池里存在，但它属于 4.1.1，不是本节。

    这条守的是「不能只判『这个页码在不在召回池里』」—— 那样 `p.71` 会因为是 `4.1` 的
    起始页而被判合法，正是线上那份 6 页全写 `p.71` 的产物躲过钳制的原因。
    """
    deck = _deck(
        [
            _slide(
                "slide-1",
                "bullet_points",
                title="核心要点",
                bullets=[
                    "马来社会强烈反对 Malayan Union",
                    "认为它削弱苏丹地位并威胁马来人政治地位",
                    "1946 年 3 月全马马来人代表大会在吉隆坡召开",
                ],
                speaker_notes="反对运动促成巫统成立。",
                source_ref="4.1.1, p.72",
            )
        ]
    )

    rebound, _ = _rebind_spec_source_pages(deck, _pool(), section_code="4.1.3")

    assert _refs(rebound) == ["4.1.3, p.76-77"]


# --------------------------------------------------------------------------- #
# 6. 幂等：对已正确的 spec 再跑一次，结果不变
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "deck_factory",
    [_single_section_deck, lambda: _single_section_deck("4.1.3, p.76-77")],
    ids=["from-fake-ref", "already-correct"],
)
def test_rebind_is_idempotent(deck_factory: Any) -> None:
    """二次处理必须零改动 —— 重试 / 回放路径会重复跑同一个函数。

    不幂等会造成「二次处理漂移」：第一次修对，第二次又改到别处，越修越错。
    """
    pool = _pool()
    first, _ = _rebind_spec_source_pages(deck_factory(), pool, section_code="4.1.3")
    second, changed = _rebind_spec_source_pages(first, pool, section_code="4.1.3")

    assert changed == 0
    assert second == first


def test_rebind_does_not_mutate_input() -> None:
    """纯函数：入参 spec 必须原封不动（调用方可能还要拿它做别的判定）。"""
    deck = _single_section_deck()
    snapshot = _refs(deck)
    _rebind_spec_source_pages(deck, _pool(), section_code="4.1.3")

    assert _refs(deck) == snapshot


# --------------------------------------------------------------------------- #
# 2D / 3D 也覆盖：Entity2D.source_page / Node3D.source_page
# --------------------------------------------------------------------------- #
def test_anim_2d_entities_and_steps_are_rebound() -> None:
    """2D：step 的 `source_ref` 与 entity 的 `source_page` 都按同一套重叠算法绑定。"""
    spec = {
        "type": "anim_2d",
        "title": "社会反应",
        "steps": [
            {
                "step_index": 0,
                "narration": "马来社会强烈反对 Malayan Union，认为它削弱苏丹地位。",
                "source_ref": "4.1.3, p.71",
                "entities": [
                    {
                        "id": "entity-0",
                        "shape": "zone",
                        "label": "马来社会强烈反对 Malayan Union",
                        "x": 320.0,
                        "y": 300.0,
                        "color": "#2563EB",
                        "details": "认为它削弱苏丹地位并威胁马来人政治地位。",
                        "source_page": 71,
                    }
                ],
            },
            {
                "step_index": 1,
                "narration": "在 Malayan Union 宪制下苏丹仅保留宗教与习俗事务的权限。",
                "source_ref": "4.1.3, p.71",
                "entities": [
                    {
                        "id": "entity-1",
                        "shape": "label_box",
                        "label": "苏丹仅保留宗教与习俗事务的权限",
                        "x": 500.0,
                        "y": 300.0,
                        "color": "#2563EB",
                        "details": "政治权力被大幅削减，各州主权被移交给英国总督。",
                        "source_page": 71,
                    }
                ],
            },
        ],
    }

    rebound, changed = _rebind_spec_source_pages(spec, _pool(), section_code="4.1")

    assert [step["source_ref"] for step in rebound["steps"]] == [
        "4.1.3, p.76-77",
        "4.1.2, p.74-75",
    ]
    assert [step["entities"][0]["source_page"] for step in rebound["steps"]] == [76, 74]
    assert changed == 4


def test_scene_3d_nodes_are_rebound() -> None:
    """3D：`Node3D.source_page` 用 `label + annotation_markdown` 打分。"""
    spec = {
        "type": "scene_3d",
        "title": "宪制结构",
        "nodes": [
            {
                "id": "node-0",
                "primitive": "box",
                "label": "英国总督",
                "position": [0.0, 1.5, 0.0],
                "scale": [2.0, 1.0, 2.0],
                "rotation": [0.0, 0.0, 0.0],
                "color": "#2563EB",
                "annotation_markdown": "Malayan Union 把各州与海峡殖民地合并为单一殖民地。",
                "source_page": 71,
                "parent_id": None,
            },
            {
                "id": "node-1",
                "primitive": "sphere",
                "label": "苏丹",
                "position": [0.0, 0.5, 0.0],
                "scale": [1.0, 1.0, 1.0],
                "rotation": [0.0, 0.0, 0.0],
                "color": "#2563EB",
                "annotation_markdown": "苏丹不再拥有对州务的实质否决权。",
                "source_page": 71,
                "parent_id": "node-0",
            },
        ],
        "camera_tour": [
            {
                "name": "全景",
                "position": [8.0, 6.0, 8.0],
                "target": [0.0, 1.5, 0.0],
                "explanation": "先看整体比例。",
            }
        ],
    }

    rebound, _ = _rebind_spec_source_pages(spec, _pool(), section_code="4.1")

    assert [node["source_page"] for node in rebound["nodes"]] == [72, 74]
    # camera_tour 没有页码字段，不处理
    assert "source_page" not in rebound["camera_tour"][0]


# --------------------------------------------------------------------------- #
# 引擎接线：顺序必须是 校验 → 数量 → 重绑定 → 钳制
# --------------------------------------------------------------------------- #
class _StaticLLM:
    """只回一份固定 spec 的假 LLM（不打网络、不烧 token）。"""

    enabled = True

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
        self.artifact_calls = 0

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.artifact_calls += 1
        return self._spec


def _mock_context() -> MockContext:
    return MockContext(
        query="4.1 Malayan Union 的反应",
        section_code="4.1.3",
        section_title="Reaksi Masyarakat",
        pages=(76,),
        facts=("马来社会强烈反对 Malayan Union。",),
        fact_pages=(76,),
        # 召回池包含 p.71 —— 钳制放行它，所以「重绑定后变成 p.76-77」只可能是重绑定的功劳
        allowed_pages=(71, 72, 73, 74, 75, 76, 77),
    )


def _engine(spec: dict[str, Any], *, settings: Settings | None = None) -> AgentEngine:
    resolved = settings or Settings(
        LLM_API_KEY="test-key", LLM_MODEL_ARTIFACT="artifact-model"
    )
    return AgentEngine(
        settings=resolved,
        llm=_StaticLLM(spec),  # type: ignore[arg-type]
        retriever=object(),  # type: ignore[arg-type]
    )


async def _run_spec_stream(
    spec: dict[str, Any],
    chunks: tuple[RetrievedChunk, ...],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    engine = _engine(spec, settings=settings)
    decision = route("4.1 Malayan Union 的反应", anchor=None, user_override="ppt")
    request = TurnRequest(workspace_id="w", query="4.1 Malayan Union 的反应")
    events = [
        event
        async for event in engine._generate_spec_stream(
            request, decision, _mock_context(), chunks
        )
    ]
    spec_events = [payload for kind, payload in events if kind == "spec"]
    assert len(spec_events) == 1, f"应当恰好产出一份 spec，实际事件：{events}"
    return spec_events[0]


async def test_engine_rebinds_after_validation_and_before_clamp() -> None:
    """接线断言：同一份 LLM 输出，传 chunks 就重绑定，不传就保持原样。

    差异化的对照是本条的全部价值 —— 只断言「传了 chunks 之后是 p.76-77」无法排除
    「其实没接线、只是钳制碰巧改对了」。这里 `p.71` 在 `allowed_pages` 里，
    钳制**不会**动它，所以不传 chunks 时必须原样保留。
    """
    spec = _single_section_deck("4.1.3, p.71")

    without_chunks = await _run_spec_stream(spec, ())
    assert _refs(without_chunks) == ["4.1.3, p.71"] * 6

    with_chunks = await _run_spec_stream(spec, _pool())
    assert _refs(with_chunks) == ["4.1.3, p.76-77"] * 6


async def test_kill_switch_restores_old_behaviour() -> None:
    """`CITATION_REBIND_ENABLED=False` → 回到「只钳制不重绑定」的旧行为。

    这个开关的存在理由是本节既定的「回退不依赖代码回滚」：重绑定会改写用户可见的
    `source_ref`，出问题时要能改一行环境变量就退回已知可用状态。
    """
    spec = _single_section_deck("4.1.3, p.71")
    settings = Settings(
        LLM_API_KEY="test-key",
        LLM_MODEL_ARTIFACT="artifact-model",
        CITATION_REBIND_ENABLED=False,
    )

    disabled = await _run_spec_stream(spec, _pool(), settings=settings)
    assert _refs(disabled) == ["4.1.3, p.71"] * 6

    enabled = await _run_spec_stream(spec, _pool())
    assert _refs(enabled) == ["4.1.3, p.76-77"] * 6


async def test_engine_keeps_rebound_pages_inside_allowed_set() -> None:
    """重绑定 → 钳制 的组合必须稳定：重绑定产出的页码落在 `allowed_pages` 内，钳制零改动。

    钳制留在最后是兜底，正常情况下它应该**什么都不做**。若这里开始出现改动，
    说明重绑定引入了越界页码 —— 那正是「钳制放在最后」要挡的缺陷。
    """
    rebound, _ = _rebind_spec_source_pages(
        _single_section_deck(), _pool(), section_code="4.1.3"
    )
    allowed = frozenset({71, 72, 73, 74, 75, 76, 77})

    from app.services.agent_engine import _clamp_spec_pages

    clamped, count = _clamp_spec_pages(rebound, allowed, fallback_ref="4.1.3")

    assert count == 0
    assert _refs(clamped) == _refs(rebound)


# --------------------------------------------------------------------------- #
# 实词抽取必须复用 `text_signals.content_words`（不另写 n-gram 提取器）
# --------------------------------------------------------------------------- #
def test_rebind_reuses_content_words_helper() -> None:
    """评分用的实词必须与 `content_words` 同口径。

    自写提取器会把虚词、套话混进分子，让覆盖率虚高 —— 这类偏差不会报错，只会让
    重绑定悄悄绑错页码。这里用「同一段文本算出的实词集合」做交叉验证。
    """
    from app.services.agent_engine import _slide_semantic_text

    slide = _single_section_deck()["slides"][1]
    words = set(content_words(_slide_semantic_text(slide)))

    assert "马来" in words
    assert "苏丹" in words
    assert "1946" in words
    # 虚词 bigram 必须被滤掉（否则覆盖率会虚高）：`会` 是虚词字，含它的 bigram 一律不算实词
    assert "会强" not in words
    assert "的权" not in words
    assert "了吗" not in words

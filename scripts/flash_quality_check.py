#!/usr/bin/env python
"""`deepseek-flash` 能否胜任默认 generator —— 三形态产物质量与契约稳健性验证。

**这个脚本只做测量，不改任何业务逻辑。**

为什么要单独一个脚本（而不是继续往 `profile_latency.py` 里塞）
------------------------------------------------------------------
`profile_latency.py` 回答的是「时间花在哪」，测的是**一次裸调用**；
本脚本回答的是「换档之后产物还能不能用」，必须驱动**真实回合**，因为只有真实回合才会
走 `_generate_spec_stream` 的三次校验重试与降级 mock 分支。两者的问题域不同，
混在一个脚本里会让「耗时」与「通过率」两套口径互相污染。

复用 `profile_latency` 的：环境准备、语料入库、埋点（`instrument_llm` /
`instrument_engine`）、统计函数、控制台表格风格。

三层指标（**不许合并成一个「成功率」**）
----------------------------------------
只报「最终通过率」会把「3 次才过」洗成「100% 通过」，而 3 次重试是最贵的失败形态
（耗时直接翻三倍）。因此三个口径分开报：

1. **单次调用合法率** = 未触发校验错误的调用数 / 真实 HTTP 调用总数
2. **回合最终通过率** = 最终 spec 来自 LLM（重试 ≤3 次内成功）的回合数 / 总回合数
3. **降级 mock 率** = 三次都失败、退回演示生成器的回合数 / 总回合数

口径自校验：`合法调用 + 失败调用 == 真实调用总数`，脚本会在输出里断言这一点。

G3（页码可回溯性）单独一张表
----------------------------
口径**固定**为「页码落在检索命中区间内的 slide 占比」，并且**钳制前后各报一份**：
生成后的页码钳制会把「编造」变成「缺失」，只报钳制后等于用钳制掩盖提示词层的缺陷
（契约示例曾硬编码 `p.76`、事实清单不带页码）。**不要**把口径改成「可解析 `p.N` 占比」——
按那个口径 flash 是 45%、v4-pro 是 0%，会得出「flash 更好」的错误结论，因为 flash 的
高可解析率正是照抄示例里那个错页码换来的。详见 `page_breakdown` 上方的注释。

质量清单（三形态各自的结构完整性）
----------------------------------
契约通过只是及格线（`parse_artifact_spec` 已经保证），真正决定「产物能不能用」的是
结构与可回溯性。清单来源：PRD P0-07 / P0-09 / P0-04②，加上两处**对任务书的修正**：

- 2D 契约里**没有 `t` 字段**（`KeyframeStep` 只有 `step_index`），所以「关键帧时间轴
  单调递增、首帧为 0」检查的是 `step_index`，不是 `t`。
- 3D 的 `camera_tour[i].name` **不是枚举**，而是自由字符串。`focus_camera` 的旁路
  依赖 `cameraPresetByName()`（`tour.find(p => p.name === name)`）按名查找，
  所以真正的风险是**机位重名**（重名会让第二个机位永远定位不到），不是「值不在枚举内」。

用法
----
```bash
# 完整验证（11 个真实回合，约 3~4 分钟）
backend/.venv/Scripts/python.exe scripts/flash_quality_check.py

# 只验证 2D/3D（省 token）
backend/.venv/Scripts/python.exe scripts/flash_quality_check.py --only anim_2d,scene_3d

# 同一形态重复 2 次（估方差，样本翻倍）
backend/.venv/Scripts/python.exe scripts/flash_quality_check.py --runs-per-case 2 --only ppt
```
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import functools
import json
import sys
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
for _path in (BACKEND_ROOT, BACKEND_ROOT / "scripts", PROJECT_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import profile_latency as pl  # noqa: E402  复用环境准备 / 语料入库 / 埋点 / 统计

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_DATA_DIR = BACKEND_ROOT / "data" / "profile"
DEFAULT_JSON_OUT = PROJECT_ROOT / "docs" / "increments" / "flash-quality.raw.json"

# 2D 逻辑画布（§9.5：x/y 是 1000×600 逻辑画布的中心点坐标，不是像素）
CANVAS_W = 1000.0
CANVAS_H = 600.0

# 业务约束（与 `_ARTIFACT_CONTRACT` 里写给模型的数字逐条对应，改契约必须同步改这里）
PPT_MIN_SLIDES, PPT_MAX_SLIDES = 5, 8
PPT_MIN_MIDDLE_LAYOUTS = 3
PPT_BULLET_RANGE = (3, 5)
PPT_TIMELINE_MIN = 2
PPT_COMPARISON_RANGE = (2, 4)
PPT_QUIZ_MIN = 2
ANIM_MIN_STEPS, ANIM_MAX_STEPS = 2, 5
ANIM_ENTITY_RANGE = (2, 5)
SCENE_MIN_NODES, SCENE_MAX_NODES = 3, 8
SCENE_CAMERA_RANGE = (2, 4)

# 模板句特征（IMPL-NOTES E11：mock 降级后的文案是「这一节我们讲…先交代背景」）
_TEMPLATE_MARKERS: tuple[str, ...] = ("这一节我们讲", "先交代背景", "要点 1", "要点 2", "占位")


# --------------------------------------------------------------------------- #
# 用例集：同一份教材、不同小节，不跨学科
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class QualityCase:
    """一个真实回合的用例。`layout_focus` 只用于报告分组，不影响发送内容。"""

    case_id: str
    artifact_type: str
    query: str
    layout_focus: str = ""


# PPT：5 个 query，覆盖 5 类布局倾向。query 里必须带显式小节号 ——
# 否则中文问句与马来文正文词面几乎不重合，grader 会判 PARTIAL 甚至 NO_MATCH，
# 那样测到的是检索问题而不是生成问题（实测带点编号会走 anchored 短路直接 SYLLABUS_MATCH）。
PPT_CASES: tuple[QualityCase, ...] = (
    QualityCase("ppt-1", "ppt", "4.1.2 苏丹的权力被削减，前后对比一下", "comparison"),
    QualityCase("ppt-2", "ppt", "4.2.1 从联合邦成立到 1957 年独立的时间线", "timeline"),
    QualityCase("ppt-3", "ppt", "5.1.1 马来西亚构想的背景要点", "bullet_points"),
    QualityCase("ppt-4", "ppt", "4.3.1 1955 年选举为什么重要，讲清因果", "cause_effect"),
    QualityCase("ppt-5", "ppt", "1.1.2 历史的特征，出 3 道随堂测验", "quiz"),
)

# 2D：3 个 query，全部用 artifact_type_override 强制（不指望 router 每次都判对）
ANIM_CASES: tuple[QualityCase, ...] = (
    # anim-1 实测被判 NO_MATCH（4.1.1 切片不足 3 条 → 检索放宽到全章 → top1 不在 4.1.1 →
    # anchored 失效 → 中文问句与马来文正文词面不重合 → 实词命中率 < 0.45）。
    # 这是检索/Grader 行为，与模型无关，因此**保留它作为证据**，但在统计时不计入模型质量分母。
    QualityCase("anim-1", "anim_2d", "4.1.1 Malayan Union 的推进过程，做成动画演示"),
    QualityCase("anim-1b", "anim_2d", "4.1.2 苏丹权力的变化过程，做成动画演示"),
    QualityCase("anim-2", "anim_2d", "4.1.3 反对运动的演变过程，做成动画演示"),
    QualityCase("anim-3", "anim_2d", "5.1.2 马来西亚构想的谈判进程，推演动画"),
)

# 3D：3 个 query，同上
SCENE_CASES: tuple[QualityCase, ...] = (
    QualityCase("scene-1", "scene_3d", "2.3.2 传统马来屋的结构与部件，做成 3D 场景"),
    QualityCase("scene-2", "scene_3d", "2.2.2 古代港口的空间布局，3D 场景"),
    QualityCase("scene-3", "scene_3d", "2.1.2 农耕社会的层级结构，立体模型"),
)

ALL_CASES: tuple[QualityCase, ...] = PPT_CASES + ANIM_CASES + SCENE_CASES


# --------------------------------------------------------------------------- #
# 埋点：强制模型 + 统计校验错误数（E11 特征）
# --------------------------------------------------------------------------- #
def force_generator_model(client: Any, model: str) -> None:
    """把 `complete_json` 的模型强制成指定档位。

    为什么用包裹而不是改 `.env`：任务书明确要求验证阶段不动 `backend/.env`，一律用探针覆盖。
    注意 `_generate_spec_stream` / `_generate_ops_stream` 现在**会显式传** `model=`
    （分形态档位），但本 wrapper 在调用前无条件覆盖 `kwargs["model"]`，所以探针语义不变 ——
    「本次生效的产物档位」永远等于 `--generator-model`，报告里记录的是真实 payload 模型。
    """
    original = client.complete_json

    @functools.wraps(original)
    async def wrapper(**kwargs: Any) -> Any:
        kwargs["model"] = model
        return await original(**kwargs)

    client.complete_json = wrapper  # type: ignore[method-assign]


@dataclass
class ContractErrorLog:
    """一次 `parse_artifact_spec` 失败的记录。用来复现 E11 的「错误数递增」特征。"""

    attempt: int
    error_count: int
    first_error: str


def instrument_contract_validation(error_log: list[ContractErrorLog]) -> None:
    """包裹 `agent_engine.parse_artifact_spec`，记录每次校验失败的**错误条数**。

    E11 的失败特征不是「报错」而是「回灌错误后越改越乱」（15 → 21 → 26）。
    只有记下错误条数才能识别这个形态；只看成功/失败会漏掉它。
    """
    from app.services import agent_engine as engine_module
    from pydantic import ValidationError

    original = engine_module.parse_artifact_spec

    @functools.wraps(original)
    def wrapper(payload: Any) -> Any:
        try:
            return original(payload)
        except ValidationError as exc:
            errors = exc.errors()
            error_log.append(
                ContractErrorLog(
                    attempt=len(error_log) + 1,
                    error_count=len(errors),
                    first_error=str(errors[0])[:200] if errors else "",
                )
            )
            raise

    engine_module.parse_artifact_spec = wrapper  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 页码可回溯性（G3）—— 口径固定，**不许**改成「可解析 p.N 占比」
# --------------------------------------------------------------------------- #
# G3 的唯一口径是「**页码落在检索命中区间内的 slide 占比**」。
#
# 不要改成「含可解析 `p.N` 的 slide 占比」：实测 flash 在该口径下是 45%、v4-pro 是 0%，
# 于是会得出「flash 更好、门禁通过」的结论 —— **完全错误**。flash 的可解析率更高，
# 恰恰是因为它**照抄了契约示例里硬编码的 p.76**，把「明显缺失」变成了「静默编造」。
# 编造一个错页码比不写页码更严重：前者让学生查不到且不知道为什么，后者至少暴露了缺失。
#
# 另外必须**同时报钳制前与钳制后**（设计 §4.9.7 第 6 条）：生成后钳制会把「编造」变成
# 「缺失」，只看最终 spec 等于用钳制掩盖了提示词层的缺陷（示例硬编码页码 / 事实不带页码）。
@dataclass
class ClampRecord:
    """一次生成后页码钳制的记录。`before` 是**钳制前**的模型输出（深拷贝）。"""

    artifact_type: str
    before: dict[str, Any]
    clamped: int


def instrument_page_clamp(clamp_log: list[ClampRecord]) -> None:
    """包裹 `agent_engine._clamp_spec_pages`，在钳制**之前**抄一份样本。

    与 `instrument_contract_validation` 同一手法（按名替换模块属性），不改业务逻辑。
    采不到样本时（演示模式 / 三次校验全败走 mock 降级 —— 那条路径不经过钳制）
    `clamp_log` 为空，报告里「钳制前」与「钳制后」会是同一份数据，这一点由
    `page_clamped=0` 直接可读出来，不会被误读成「钳制没生效」。
    """
    from app.services import agent_engine as engine_module

    original = engine_module._clamp_spec_pages

    @functools.wraps(original)
    def wrapper(spec: Any, allowed: Any, **kwargs: Any) -> Any:
        before = copy.deepcopy(spec) if isinstance(spec, dict) else {}
        after, clamped = original(spec, allowed, **kwargs)
        clamp_log.append(
            ClampRecord(
                artifact_type=str(before.get("type") or ""),
                before=before,
                clamped=int(clamped),
            )
        )
        return after, clamped

    engine_module._clamp_spec_pages = wrapper  # type: ignore[assignment]


def _page_units(spec: dict[str, Any]) -> list[list[int]]:
    """把 spec 拆成「带页码的锚点」列表，每个锚点给出它写的全部页码。

    单位：ppt → 每个 slide 的 `source_ref`；anim_2d → 每个 entity 的 `source_page`；
    scene_3d → 每个 node 的 `source_page`。`subtitle` **不计入**（自由文案，
    按正则改它会误伤「1946 年 8 月」这类正常表述，见设计 §4.9.6）。
    """
    spec_type = str(spec.get("type") or "")
    units: list[list[int]] = []
    if spec_type == "ppt":
        for slide in spec.get("slides") or []:
            if isinstance(slide, dict):
                units.append(_extract_ref_pages(slide.get("source_ref") or ""))
    elif spec_type == "anim_2d":
        for step in spec.get("steps") or []:
            for entity in (step or {}).get("entities") or []:
                page = entity.get("source_page") if isinstance(entity, dict) else None
                units.append([int(page)] if page is not None else [])
    elif spec_type == "scene_3d":
        for node in spec.get("nodes") or []:
            page = node.get("source_page") if isinstance(node, dict) else None
            units.append([int(page)] if page is not None else [])
    return units


def page_breakdown(spec: dict[str, Any], allowed: set[int]) -> dict[str, Any]:
    """按固定口径统计：真实（页码全在区间内）/ 编造（写了但越界）/ 缺失（一个都没写）。

    区间写法（`p.84-85`）沿用 `_page_realness` 的「不做部分合法宽容」：任一端越界即整条算编造。

    另外给两个**咨询性**指标（不进 `passed`、不参与 verdict）：

    - `distinct_pages`：同一份课件里出现过的不同页码数；
    - `max_same_page_ratio`：引用同一个页码的条目占全部条目的比例（最高那一页）。

    为什么必须有这两个：G3 的「落在区间内」口径天生看不见「所有页都写同一个页码」——
    5 页全写 `p.74` 而该节区间是 72~89 时，G3 是 100%，可语义上仍然是编造（只是编在了
    区间内）。没有这两个指标，这个缺陷下次还是查不出来。
    """
    units = _page_units(spec)
    total = len(units)
    if not total:
        return {"total": 0, "real": 0, "fabricated": 0, "missing": 0,
                "real_rate": None, "fabricated_rate": None, "missing_rate": None,
                "distinct_pages": 0, "max_same_page_ratio": None}
    cited = [page for pages in units for page in pages]
    distinct_pages = len(set(cited))
    max_same_page_ratio = (
        round(max(Counter(cited).values()) / total, 3) if cited else None
    )
    if not allowed:
        # 没有可信区间就无从判真伪：如实标 None，不猜
        return {"total": total, "real": None, "fabricated": None,
                "missing": sum(1 for pages in units if not pages),
                "real_rate": None, "fabricated_rate": None, "missing_rate": None,
                "distinct_pages": distinct_pages,
                "max_same_page_ratio": max_same_page_ratio}
    real = sum(1 for pages in units if pages and all(p in allowed for p in pages))
    missing = sum(1 for pages in units if not pages)
    fabricated = total - real - missing
    return {
        "total": total,
        "real": real,
        "fabricated": fabricated,
        "missing": missing,
        "real_rate": round(real / total, 3),
        "fabricated_rate": round(fabricated / total, 3),
        "missing_rate": round(missing / total, 3),
        "distinct_pages": distinct_pages,
        "max_same_page_ratio": max_same_page_ratio,
    }


def aggregate_pages(results: Sequence[CaseResult]) -> dict[str, Any]:
    """把逐回合的钳制前后统计汇总成三行报告（钳制前 / 钳制后 / 钳制次数）。"""

    def _merge(key: str) -> dict[str, Any]:
        rows = [item.page_before if key == "before" else item.page_after for item in results]
        rows = [row for row in rows if row and row.get("total")]
        if not rows:
            return {"total": 0, "real": 0, "fabricated": 0, "missing": 0,
                    "real_rate": None, "fabricated_rate": None, "missing_rate": None}
        total = sum(int(row["total"]) for row in rows)
        real = sum(int(row.get("real") or 0) for row in rows)
        fabricated = sum(int(row.get("fabricated") or 0) for row in rows)
        missing = sum(int(row.get("missing") or 0) for row in rows)
        ratios = [
            float(row["max_same_page_ratio"])
            for row in rows
            if row.get("max_same_page_ratio") is not None
        ]
        return {
            "total": total,
            "real": real,
            "fabricated": fabricated,
            "missing": missing,
            "real_rate": round(real / total, 3) if total else None,
            "fabricated_rate": round(fabricated / total, 3) if total else None,
            "missing_rate": round(missing / total, 3) if total else None,
            # 咨询性：逐课件取均值与最高值，用来发现「全写同一页」这种区间内编造
            "distinct_pages_avg": round(
                sum(int(row.get("distinct_pages") or 0) for row in rows) / len(rows), 2
            ),
            "max_same_page_ratio_avg": round(sum(ratios) / len(ratios), 3) if ratios else None,
            "max_same_page_ratio_max": round(max(ratios), 3) if ratios else None,
            "decks": len(rows),
        }

    clamped_turns = sum(1 for item in results if item.page_clamped)
    return {
        "before": _merge("before"),
        "after": _merge("after"),
        "clamped_total": sum(item.page_clamped for item in results),
        "clamped_turns": clamped_turns,
        "turns": len(results),
    }


# --------------------------------------------------------------------------- #
# 质量清单
# --------------------------------------------------------------------------- #
def _check(condition: bool) -> bool:
    return bool(condition)


def _extract_ref_pages(source_ref: str) -> list[int]:
    """从 `source_ref`（约定格式 "4.1.3, p.76"）里抠出页码。"""
    import re

    pages: list[int] = []
    for match in re.finditer(r"p\.\s*(\d+)(?:\s*-\s*(\d+))?", str(source_ref or "")):
        pages.append(int(match.group(1)))
        if match.group(2):
            pages.append(int(match.group(2)))
    return pages


def _page_realness(pages: Sequence[int], allowed: set[int]) -> dict[str, Any]:
    """页码真实性：所有被引用的页码都必须落在检索命中页区间内。"""
    cited = [page for page in pages if page is not None]
    if not cited:
        return {"cited": [], "ok": None, "reason": "无页码可判"}
    if not allowed:
        return {"cited": sorted(set(cited)), "ok": None, "reason": "允许集为空"}
    bad = sorted({page for page in cited if page not in allowed})
    return {
        "cited": sorted(set(cited)),
        "bad": bad,
        "ok": not bad,
        "allowed_min": min(allowed),
        "allowed_max": max(allowed),
    }


def _is_template(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in _TEMPLATE_MARKERS)


def check_ppt(spec: dict[str, Any], allowed_pages: set[int]) -> dict[str, Any]:
    """PPT 结构完整性清单。每条都是可判定的布尔值，不做主观打分。"""
    slides = spec.get("slides") or []
    layouts = [str(slide.get("layout") or "") for slide in slides]
    middle = layouts[1:-1] if len(layouts) >= 2 else []
    middle_distinct = {name for name in middle if name in {
        "bullet_points", "timeline", "comparison", "cause_effect"
    }}

    bullet_ok = True
    timeline_ok = True
    comparison_ok = True
    quiz_ok = True
    for slide in slides:
        layout = str(slide.get("layout") or "")
        if layout == "bullet_points":
            count = len(slide.get("bullets") or [])
            bullet_ok = bullet_ok and PPT_BULLET_RANGE[0] <= count <= PPT_BULLET_RANGE[1]
        elif layout == "timeline":
            count = len(slide.get("timeline_items") or [])
            timeline_ok = timeline_ok and count >= PPT_TIMELINE_MIN
        elif layout == "comparison":
            left = slide.get("comparison_left") or []
            right = slide.get("comparison_right") or []
            comparison_ok = comparison_ok and (
                PPT_COMPARISON_RANGE[0] <= len(left) <= PPT_COMPARISON_RANGE[1]
                and PPT_COMPARISON_RANGE[0] <= len(right) <= PPT_COMPARISON_RANGE[1]
            )
        elif layout == "quiz":
            count = len(slide.get("quiz_questions") or [])
            quiz_ok = quiz_ok and count >= PPT_QUIZ_MIN

    node_ids = [str(slide.get("node_id") or "") for slide in slides]
    notes = [str(slide.get("speaker_notes") or "") for slide in slides]
    ref_pages: list[int] = []
    for slide in slides:
        ref_pages.extend(_extract_ref_pages(slide.get("source_ref") or ""))

    checks = {
        "页数 5~8": _check(PPT_MIN_SLIDES <= len(slides) <= PPT_MAX_SLIDES),
        "首页 title_hero": _check(layouts[:1] == ["title_hero"]),
        "末页 quiz": _check(layouts[-1:] == ["quiz"]),
        f"中间页 ≥{PPT_MIN_MIDDLE_LAYOUTS} 种布局": _check(
            len(middle_distinct) >= PPT_MIN_MIDDLE_LAYOUTS
        ),
        "bullet_points 页 3~5 条": _check(bullet_ok),
        "timeline 页 ≥2 条": _check(timeline_ok),
        "comparison 页左右各 2~4 条": _check(comparison_ok),
        "quiz 页 ≥2 题": _check(quiz_ok),
        "node_id 唯一且非空": _check(
            all(node_ids) and len(set(node_ids)) == len(node_ids)
        ),
        "speaker_notes 非空且非模板句": _check(
            all(len(note) >= 20 and not _is_template(note) for note in notes)
        ),
    }
    realness = _page_realness(ref_pages, allowed_pages)
    # PPT 的 source_ref 是**必填**字段，且契约规定的格式就是 "4.1.3, p.76"。
    # 解析不出页码（例如模型写 `p.?`、或页码被钳制剥掉）不算「通过」，而是「页码不可回溯」——
    # P0-05④ 要求每条事实都能追到具体页，缺失与写错是同一类缺陷。
    #
    # 注意：**这一项不是 G3**。G3 的固定口径是「页码落在检索命中区间内的占比」，
    # 由 `page_breakdown` / `print_page_table` 单独统计（钳制前后各一份）。
    # 两者不要互相替代：这一项判「有没有出处」，G3 判「出处是不是真的」。
    checks["source_ref 页码可回溯且真实"] = bool(realness["ok"])
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "failed": [name for name, ok in checks.items() if not ok],
        "detail": {
            "slide_count": len(slides),
            "layouts": layouts,
            "middle_layouts": sorted(middle_distinct),
            "pages": realness,
        },
    }


def check_anim_2d(spec: dict[str, Any], allowed_pages: set[int]) -> dict[str, Any]:
    """2D 关键帧清单。

    时间轴检查的是 `step_index`（契约里**没有 `t` 字段**）：必须从 0 开始严格递增。
    前端按 `step_index` 排序播放，跳号或重号会让帧序错乱。
    """
    steps = spec.get("steps") or []
    step_indices = [int(step.get("step_index", -1)) for step in steps]
    monotonic = all(
        step_indices[i] < step_indices[i + 1] for i in range(len(step_indices) - 1)
    )

    entity_count_ok = True
    entity_id_ok = True
    coord_ok = True
    source_pages: list[int] = []
    for step in steps:
        entities = step.get("entities") or []
        entity_count_ok = entity_count_ok and (
            ANIM_ENTITY_RANGE[0] <= len(entities) <= ANIM_ENTITY_RANGE[1]
        )
        ids = [str(entity.get("id") or "") for entity in entities]
        entity_id_ok = entity_id_ok and all(ids) and len(set(ids)) == len(ids)
        for entity in entities:
            try:
                x = float(entity.get("x"))
                y = float(entity.get("y"))
            except (TypeError, ValueError):
                coord_ok = False
                continue
            coord_ok = coord_ok and (0.0 <= x <= CANVAS_W and 0.0 <= y <= CANVAS_H)
            if entity.get("source_page") is not None:
                source_pages.append(int(entity["source_page"]))

    # 「随时间推进」：同一 label 至少在 2 帧出现，且位置或标签发生过变化。
    # 这是弱检查（契约的业务约束原话），只作为可读性提示，不计入硬性通过。
    positions: dict[str, set[tuple[float, float]]] = {}
    for step in steps:
        for entity in step.get("entities") or []:
            try:
                key = str(entity.get("label") or "")
                positions.setdefault(key, set()).add(
                    (float(entity.get("x")), float(entity.get("y")))
                )
            except (TypeError, ValueError):
                continue
    moved = any(len(points) > 1 for points in positions.values() if len(points) > 0)

    checks = {
        "关键帧 2~5 帧": _check(ANIM_MIN_STEPS <= len(steps) <= ANIM_MAX_STEPS),
        "step_index 从 0 起严格递增": _check(bool(step_indices) and step_indices[0] == 0 and monotonic),
        "每帧 2~5 个 entity": _check(entity_count_ok),
        "entity id 帧内唯一": _check(entity_id_ok),
        "坐标在 1000×600 内": _check(coord_ok),
        "narration 非空": _check(all(str(step.get("narration") or "").strip() for step in steps)),
    }
    realness = _page_realness(source_pages, allowed_pages)
    if realness["ok"] is not None:
        checks["source_page 真实"] = realness["ok"]
    total_entities = sum(len(step.get("entities") or []) for step in steps)
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "failed": [name for name, ok in checks.items() if not ok],
        "advisory": {
            "同一主体跨帧位移": moved,
            # 契约声明 source_page 可以是 null，所以填不填都不算契约违规；
            # 但 P0-04② 要求「每条事实可回溯到页」，全填 null 等于不可回溯。
            # 因此只作为 advisory 记录，不计入模型质量判定（这是提示词缺陷，模型无关）。
            "source_page 填充率": round(len(source_pages) / total_entities, 3)
            if total_entities
            else None,
        },
        "detail": {
            "step_count": len(steps),
            "step_indices": step_indices,
            "entity_counts": [len(step.get("entities") or []) for step in steps],
            "pages": realness,
        },
    }


def check_scene_3d(spec: dict[str, Any], allowed_pages: set[int]) -> dict[str, Any]:
    """3D 场景清单。

    机位名检查的是**唯一性**而不是「在枚举内」：`CameraPreset.name` 是自由字符串，
    `focus_camera` 通过 `cameraPresetByName(tour, name)` 即 `tour.find(p => p.name === name)`
    定位，重名会让后一个机位永远无法被切到。
    """
    nodes = spec.get("nodes") or []
    tour = spec.get("camera_tour") or []
    node_ids = [str(node.get("id") or "") for node in nodes]
    node_id_set = set(node_ids)

    parent_ids = [node.get("parent_id") for node in nodes]
    parent_ok = all(
        parent is None or str(parent) in node_id_set for parent in parent_ids
    )
    roots = [node for node in nodes if node.get("parent_id") is None]
    # 环检测：沿 parent 链上溯，超过节点总数必然成环
    cycle_free = True
    parent_map = {str(node.get("id")): node.get("parent_id") for node in nodes}
    for start in node_ids:
        seen = {start}
        cursor = parent_map.get(start)
        hops = 0
        while cursor is not None:
            cursor = str(cursor)
            hops += 1
            if cursor in seen or hops > len(nodes):
                cycle_free = False
                break
            seen.add(cursor)
            cursor = parent_map.get(cursor)

    camera_names = [str(preset.get("name") or "").strip() for preset in tour]
    source_pages = [
        int(node["source_page"]) for node in nodes if node.get("source_page") is not None
    ]

    checks = {
        "node 数 3~8": _check(SCENE_MIN_NODES <= len(nodes) <= SCENE_MAX_NODES),
        "机位数 2~4": _check(SCENE_CAMERA_RANGE[0] <= len(tour) <= SCENE_CAMERA_RANGE[1]),
        "node id 唯一": _check(all(node_ids) and len(node_id_set) == len(node_ids)),
        "parent_id 均指向存在节点或 null": _check(parent_ok),
        "parent 链无环": _check(cycle_free),
        "至少 1 个主体（parent_id=null）": _check(len(roots) >= 1),
        "机位名非空且互不重复": _check(
            all(camera_names) and len(set(camera_names)) == len(camera_names)
        ),
        "annotation_markdown 非空": _check(
            all(str(node.get("annotation_markdown") or "").strip() for node in nodes)
        ),
    }
    realness = _page_realness(source_pages, allowed_pages)
    if realness["ok"] is not None:
        checks["source_page 真实"] = realness["ok"]
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "failed": [name for name, ok in checks.items() if not ok],
        "advisory": {
            "source_page 填充率": round(len(source_pages) / len(nodes), 3) if nodes else None,
        },
        "detail": {
            "node_count": len(nodes),
            "camera_count": len(tour),
            "camera_names": camera_names,
            "roots": len(roots),
            "pages": realness,
        },
    }


CHECKERS = {
    "ppt": check_ppt,
    "anim_2d": check_anim_2d,
    "scene_3d": check_scene_3d,
}


# --------------------------------------------------------------------------- #
# 单回合
# --------------------------------------------------------------------------- #
@dataclass
class CaseResult:
    case_id: str
    artifact_type: str
    query: str
    layout_focus: str = ""
    ok: bool = False
    error: str = ""
    # 三层指标所需
    llm_calls: int = 0
    failed_calls: int = 0
    validate_errors: int = 0
    mock_fallback: bool = False
    final_from_llm: bool = False
    contract_error_counts: list[int] = field(default_factory=list)
    # 产物与质量
    returned_type: str = ""
    action: str = ""
    spec: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    # 耗时
    total_ms: float = 0.0
    ttft_ms: float = 0.0
    artifact_ms: float = 0.0
    # 元信息
    grader_status: str = ""
    section_scope: str = ""
    allowed_pages: list[int] = field(default_factory=list)
    effective_models: list[str] = field(default_factory=list)
    prompt_chars: int = 0
    output_chars: int = 0
    completion_tokens: list[int] = field(default_factory=list)
    # 页码可回溯性（G3）：钳制前 / 钳制后各一份，外加被钳制处数
    page_before: dict[str, Any] = field(default_factory=dict)
    page_after: dict[str, Any] = field(default_factory=dict)
    page_clamped: int = 0


async def run_case(
    engine: Any,
    TurnRequest: Any,
    workspace_id: str,
    case: QualityCase,
    profiler: Any,
    trace: Any,
    error_log: list[ContractErrorLog],
    clamp_log: list[ClampRecord],
) -> CaseResult:
    """驱动一个真实回合，把「三层指标 + 质量清单」需要的原始信号全部采下来。"""
    result = CaseResult(
        case_id=case.case_id,
        artifact_type=case.artifact_type,
        query=case.query,
        layout_focus=case.layout_focus,
    )
    profiler.reset()
    calls_before = len(trace.calls)
    error_log.clear()
    clamp_log.clear()

    request = TurnRequest(
        workspace_id=workspace_id,
        query=case.query,
        mode="syllabus_strict",
        artifact_type_override=case.artifact_type,
    )

    allowed_pages: set[int] = set()
    validate_errors = 0
    mock_fallback = False
    final_spec: dict[str, Any] = {}
    started = pl.time.perf_counter()

    try:
        async for name, payload in engine.stream_turn(request):
            now = profiler.now_ms()
            if name == "turn_start":
                result.ttft_ms = 0.0
            elif name == "retrieval":
                for chunk in getattr(payload, "chunks", []) or []:
                    allowed_pages.update(range(chunk.page_start, chunk.page_end + 1))
                result.grader_status = str(getattr(payload, "grader_status", ""))
                result.section_scope = str(getattr(payload, "section_scope", "") or "")
            elif name == "token":
                if not result.ttft_ms:
                    result.ttft_ms = round(now, 1)
            elif name == "error":
                if str(getattr(payload, "stage", "")) == "validate":
                    validate_errors += 1
                    message = str(getattr(payload, "message", ""))
                    retryable = bool(getattr(payload, "retryable", True))
                    if not retryable and "降级" in message:
                        mock_fallback = True
            elif name == "artifact":
                result.action = str(getattr(payload, "action", ""))
                result.returned_type = str(getattr(payload, "artifact_type", ""))
                raw_spec = getattr(payload, "spec", None)
                if hasattr(raw_spec, "model_dump"):
                    final_spec = raw_spec.model_dump(mode="json")
                elif isinstance(raw_spec, dict):
                    final_spec = raw_spec
            elif name == "turn_end":
                result.total_ms = round(now, 1)
    except Exception as exc:  # noqa: BLE001 - 剖析脚本要如实记录任何异常
        result.error = f"{type(exc).__name__}: {exc}"
        result.ok = False
        return result

    elapsed = pl.time.perf_counter() - started
    result.total_ms = result.total_ms or round(pl._ms(elapsed), 1)

    # ---- 三层指标 ----
    turn_calls = trace.calls[calls_before:]
    result.llm_calls = len(turn_calls)
    # 每次失败的尝试恰好产出一条 retryable=True 的 validate 错误；
    # 最后那条 retryable=False 的「降级」不是一次调用，不能计入。
    result.failed_calls = max(0, validate_errors - (1 if mock_fallback else 0))
    result.validate_errors = validate_errors
    result.mock_fallback = mock_fallback
    result.final_from_llm = bool(final_spec) and not mock_fallback
    result.contract_error_counts = [entry.error_count for entry in error_log]
    result.effective_models = sorted({call.model for call in turn_calls})
    result.prompt_chars = turn_calls[0].prompt_chars if turn_calls else 0
    result.output_chars = turn_calls[-1].output_chars if turn_calls else 0
    result.completion_tokens = [
        call.completion_tokens for call in turn_calls if call.completion_tokens
    ]

    artifact_span = next(
        (span for span in profiler.spans if span.label == "stage.artifact_stream"), None
    )
    result.artifact_ms = round(artifact_span.duration, 1) if artifact_span else 0.0

    result.allowed_pages = sorted(allowed_pages)
    result.spec = final_spec
    result.ok = bool(final_spec)

    # ---- 页码可回溯性（G3）：钳制前 / 钳制后 / 钳制次数 ----
    # 「钳制前」取自 `_clamp_spec_pages` 的入参快照；没有快照（mock 降级路径不经过钳制）
    # 时用最终 spec 顶替，并让 page_clamped=0 自己说明「这一回合没发生钳制」。
    before_spec = clamp_log[-1].before if clamp_log else final_spec
    result.page_clamped = sum(record.clamped for record in clamp_log)
    if before_spec:
        result.page_before = page_breakdown(before_spec, allowed_pages)
    if final_spec:
        result.page_after = page_breakdown(final_spec, allowed_pages)

    # ---- 质量清单 ----
    if final_spec:
        checker = CHECKERS.get(result.returned_type or case.artifact_type)
        if checker is None:
            result.quality = {"passed": False, "failed": [f"未知形态 {result.returned_type}"]}
        else:
            result.quality = checker(final_spec, allowed_pages)
    return result


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def aggregate(results: Sequence[CaseResult], artifact_type: str) -> dict[str, Any]:
    """按形态汇总三层指标与质量通过率。"""
    subset = [item for item in results if item.artifact_type == artifact_type]
    if not subset:
        return {}
    # 0 次调用的回合 = 根本没走到 LLM（检索判 NO_MATCH → action=none）。
    # 把它算进分母等于用「检索问题」给「模型质量」判死刑，必须单列。
    evaluable = [item for item in subset if item.llm_calls > 0]
    not_generated = [item for item in subset if item.llm_calls == 0]
    if not evaluable:
        return {
            "artifact_type": artifact_type,
            "turns": 0,
            "not_generated": len(not_generated),
            "not_generated_cases": [item.case_id for item in not_generated],
            "call_valid_rate": None,
            "turn_pass_rate": None,
            "mock_fallback_rate": None,
            "quality_pass_rate": None,
            "total_calls": 0,
            "valid_calls": 0,
            "failed_calls": 0,
            "calls_consistent": True,
            "retried_turns": 0,
            "failed_check_items": {},
            "artifact_ms": pl.stats([]),
            "total_ms": pl.stats([]),
            "effective_models": [],
        }
    total_calls = sum(item.llm_calls for item in evaluable)
    failed_calls = sum(item.failed_calls for item in evaluable)
    valid_calls = total_calls - failed_calls
    turns = len(evaluable)
    from_llm = sum(1 for item in evaluable if item.final_from_llm)
    mocked = sum(1 for item in evaluable if item.mock_fallback)
    quality_passed = sum(1 for item in evaluable if item.quality.get("passed"))

    # 失败条目归类：哪几条清单项最常挂
    failed_counter: dict[str, int] = {}
    for item in evaluable:
        for name in item.quality.get("failed") or []:
            failed_counter[name] = failed_counter.get(name, 0) + 1

    return {
        "artifact_type": artifact_type,
        "turns": turns,
        "total_calls": total_calls,
        "valid_calls": valid_calls,
        "failed_calls": failed_calls,
        # 口径自校验：合法 + 失败 必须等于真实调用总数
        "calls_consistent": valid_calls + failed_calls == total_calls,
        "call_valid_rate": round(valid_calls / total_calls, 4) if total_calls else None,
        "turn_pass_rate": round(from_llm / turns, 4) if turns else None,
        "mock_fallback_rate": round(mocked / turns, 4) if turns else None,
        "quality_pass_rate": round(quality_passed / turns, 4) if turns else None,
        "retried_turns": sum(1 for item in evaluable if item.llm_calls > 1),
        "not_generated": len(not_generated),
        "not_generated_cases": [item.case_id for item in not_generated],
        "failed_check_items": failed_counter,
        "artifact_ms": pl.stats([item.artifact_ms for item in evaluable]),
        "total_ms": pl.stats([item.total_ms for item in evaluable]),
        "effective_models": sorted({model for item in evaluable for model in item.effective_models}),
    }


def print_case_table(results: Sequence[CaseResult]) -> None:
    print("\n" + "=" * 108)
    print("逐回合结果")
    print("=" * 108)
    header = (
        f"{'case':<10}{'形态':<10}{'调用':>5}{'失败':>5}{'重试':>5}"
        f"{'mock':>6}{'质量':>6}{'页/帧/节点':>12}{'产物ms':>9}{'总ms':>8}  挂掉的清单项"
    )
    print(header)
    print("-" * 108)
    for item in results:
        if item.error:
            print(f"{item.case_id:<10}{item.artifact_type:<10}  !! {item.error[:80]}")
            continue
        detail = item.quality.get("detail") or {}
        scale = (
            detail.get("slide_count")
            or detail.get("step_count")
            or detail.get("node_count")
            or 0
        )
        failed = "、".join(item.quality.get("failed") or []) or "-"
        print(
            f"{item.case_id:<10}{item.returned_type or item.artifact_type:<10}"
            f"{item.llm_calls:>5}{item.failed_calls:>5}"
            f"{'是' if item.llm_calls > 1 else '否':>5}"
            f"{'是' if item.mock_fallback else '否':>6}"
            f"{'过' if item.quality.get('passed') else '挂':>6}"
            f"{scale:>12}{item.artifact_ms:>9.0f}{item.total_ms:>8.0f}  {failed}"
        )


def print_metric_table(summaries: dict[str, dict[str, Any]]) -> None:
    print("\n" + "=" * 108)
    print("三层指标（口径：合法调用 + 失败调用 == 真实调用总数）")
    print("=" * 108)
    print(
        f"{'形态':<10}{'回合':>5}{'未生成':>6}{'调用':>5}{'合法':>5}{'失败':>5}{'一致':>6}"
        f"{'单次合法率':>12}{'回合通过率':>12}{'降级mock率':>12}{'质量通过率':>12}"
    )
    print("-" * 108)
    for artifact_type, summary in summaries.items():
        if not summary:
            continue
        print(
            f"{artifact_type:<10}{summary['turns']:>5}{summary.get('not_generated', 0):>6}"
            f"{summary['total_calls']:>5}"
            f"{summary['valid_calls']:>5}{summary['failed_calls']:>5}"
            f"{'✓' if summary['calls_consistent'] else '✗':>6}"
            f"{_pct(summary['call_valid_rate']):>12}{_pct(summary['turn_pass_rate']):>12}"
            f"{_pct(summary['mock_fallback_rate']):>12}"
            f"{_pct(summary['quality_pass_rate']):>12}"
        )
    print("-" * 108)
    for artifact_type, summary in summaries.items():
        if not summary:
            continue
        print(
            f"  {artifact_type}: 产物耗时中位 {summary['artifact_ms']['median']:.0f}ms"
            f"（{summary['artifact_ms']['min']:.0f}~{summary['artifact_ms']['max']:.0f}），"
            f"整轮中位 {summary['total_ms']['median']:.0f}ms，"
            f"生效模型 {summary['effective_models']}，"
            f"发生过重试的回合 {summary['retried_turns']}/{summary['turns']}"
        )
        if summary.get("not_generated"):
            print(
                f"    未生成（检索判 none，不计入模型质量分母）：{summary['not_generated_cases']}"
            )
        if summary["failed_check_items"]:
            print(f"    高频失败清单项：{summary['failed_check_items']}")


def _pct(value: float | None) -> str:
    return "未测" if value is None else f"{value * 100:.0f}%"


def print_page_table(pages: dict[str, Any]) -> None:
    """G3 三行报告。口径固定为「页码落在检索命中区间内的占比」（见文件上方注释）。"""
    print("\n" + "=" * 108)
    print("G3 页码可回溯性（口径固定：页码落在检索命中区间内的占比，不按「可解析 p.N」算）")
    print("=" * 108)

    def _line(label: str, row: dict[str, Any]) -> str:
        total = row.get("total") or 0
        if not total:
            return f"  {label}：无可统计单位"
        return (
            f"  {label}：真实 {row['real']}/{total}（{_pct(row['real_rate'])}）"
            f" · 编造 {row['fabricated']}/{total}（{_pct(row['fabricated_rate'])}）"
            f" · 缺失 {row['missing']}/{total}（{_pct(row['missing_rate'])}）"
        )

    print(_line("钳制前", pages["before"]))
    print(_line("钳制后", pages["after"]))
    print(
        f"  page_clamped：{pages['clamped_total']} 处"
        f"（涉及 {pages['clamped_turns']}/{pages['turns']} 个回合）"
    )
    after = pages["after"]
    print(
        "  咨询（非门禁，专门看 G3 看不见的「全写同一页」）："
        f"每份课件页码去重数均值 {after.get('distinct_pages_avg')}，"
        f"引用同一页码的条目占比 均值 {_pct(after.get('max_same_page_ratio_avg'))}"
        f" / 最高 {_pct(after.get('max_same_page_ratio_max'))}"
        f"（{after.get('decks')} 份课件）"
    )
    print(
        "  说明：钳制只把越界页码剥成「缺失」，不会把编造变成真实 —— "
        "钳制后「编造」恒为 0 是结构性的，不能当成质量改善的证据。"
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run_all(args: argparse.Namespace) -> dict[str, Any]:
    from app.config import get_settings
    from app.db.session import dispose_engine
    from app.schemas.artifacts import parse_artifact_spec
    from app.services.agent_engine import AgentEngine, TurnRequest
    from app.services.llm_client import get_llm_client
    from app.services.retriever import get_retriever

    settings = get_settings()
    if settings.demo_mode:
        raise SystemExit("当前是演示模式（LLM_API_KEY 为空），无法验证真实模型。请检查 backend/.env。")

    profiler = pl.Profiler()
    trace = pl.LlmTrace()
    error_log: list[ContractErrorLog] = []
    clamp_log: list[ClampRecord] = []

    client = get_llm_client()
    pl.instrument_llm(client, profiler, trace)
    force_generator_model(client, args.generator_model)
    instrument_contract_validation(error_log)
    instrument_page_clamp(clamp_log)

    engine = AgentEngine(llm=client, retriever=get_retriever())
    pl.instrument_engine(engine, profiler)

    print("=" * 108)
    print("环境")
    print("=" * 108)
    print(f"  base_url          = {settings.LLM_BASE_URL}")
    print(f"  正文流式模型      = {settings.LLM_MODEL_ROUTER}")
    print(f"  .env 里的 generator= {settings.LLM_MODEL_GENERATOR}（**不改**，本脚本用探针覆盖）")
    print(f"  本次生效 generator = {args.generator_model}")
    print(f"  超时              = {settings.LLM_TIMEOUT_SECONDS}s")

    print("\n准备语料…")
    corpus = await pl.build_corpus(args.pdf, reuse=True)
    print(
        f"  · workspace={corpus.workspace_id[:8]}  {corpus.page_count} 页 / "
        f"{corpus.chunk_count} 切片"
    )

    selected = [case for case in ALL_CASES if case.artifact_type in set(args.only)]
    if args.case_ids:
        wanted = {item.strip() for item in args.case_ids.split(",") if item.strip()}
        selected = [case for case in selected if case.case_id in wanted]
    if not selected:
        raise SystemExit("没有选中任何用例，检查 --only / --case-ids")

    total_turns = len(selected) * args.runs_per_case
    print(f"\n跑 {total_turns} 个真实回合（{len(selected)} 个用例 × {args.runs_per_case} 次）…")

    results: list[CaseResult] = []
    consecutive_mock = 0
    aborted = ""
    executed = 0

    for case in selected:
        for repeat in range(args.runs_per_case):
            if executed >= args.max_turns:
                aborted = f"达到 --max-turns={args.max_turns} 上限"
                break
            label = case.case_id if args.runs_per_case == 1 else f"{case.case_id}#{repeat + 1}"
            print(f"  [{executed + 1}/{total_turns}] {label} …", end="", flush=True)
            item = await run_case(
                engine,
                TurnRequest,
                corpus.workspace_id,
                case,
                profiler,
                trace,
                error_log,
                clamp_log,
            )
            executed += 1
            results.append(item)

            if item.error:
                print(f" 异常：{item.error[:80]}")
                continue
            print(
                f" {item.returned_type} {item.llm_calls} 次调用"
                f"（失败 {item.failed_calls}）"
                f"{' 降级mock' if item.mock_fallback else ''}"
                f" 质量={'过' if item.quality.get('passed') else '挂'}"
                f" 产物 {item.artifact_ms / 1000:.1f}s"
            )

            # ---- 停手条件（任务书要求：不为凑样本继续烧） ----
            consecutive_mock = consecutive_mock + 1 if item.mock_fallback else 0
            if consecutive_mock >= 2:
                aborted = "连续 2 次降级 mock"
                break
            counts = item.contract_error_counts
            if len(counts) >= 2 and all(
                counts[i] < counts[i + 1] for i in range(len(counts) - 1)
            ):
                aborted = f"契约错误数递增（E11 特征：{counts}）"
                break
        if aborted:
            print(f"\n!! 触发停手条件：{aborted}")
            break

    print_case_table(results)
    summaries = {name: aggregate(results, name) for name in ("ppt", "anim_2d", "scene_3d")}
    print_metric_table(summaries)
    page_summary = aggregate_pages(results)
    print_page_table(page_summary)

    # ---- 明确结论 ----
    verdict = build_verdict(summaries, args.generator_model)
    print("\n" + "=" * 108)
    print("结论")
    print("=" * 108)
    print(f"  {verdict['headline']}")
    for line in verdict["reasons"]:
        print(f"  · {line}")

    report = {
        "environment": {
            "base_url": settings.LLM_BASE_URL,
            "router_model": settings.LLM_MODEL_ROUTER,
            "env_generator_model": settings.LLM_MODEL_GENERATOR,
            "effective_generator_model": args.generator_model,
            "timeout_seconds": settings.LLM_TIMEOUT_SECONDS,
        },
        "corpus": {
            "pdf": corpus.pdf_path.name,
            "pages": corpus.page_count,
            "chunks": corpus.chunk_count,
        },
        "executed_turns": executed,
        "planned_turns": total_turns,
        "aborted": aborted,
        "summaries": summaries,
        "page_anchor_g3": page_summary,
        "verdict": verdict,
        "cases": [
            {
                "case_id": item.case_id,
                "artifact_type": item.artifact_type,
                "returned_type": item.returned_type,
                "query": item.query,
                "layout_focus": item.layout_focus,
                "ok": item.ok,
                "error": item.error,
                "llm_calls": item.llm_calls,
                "failed_calls": item.failed_calls,
                "mock_fallback": item.mock_fallback,
                "final_from_llm": item.final_from_llm,
                "contract_error_counts": item.contract_error_counts,
                "effective_models": item.effective_models,
                "grader_status": item.grader_status,
                "section_scope": item.section_scope,
                "allowed_pages": item.allowed_pages,
                "prompt_chars": item.prompt_chars,
                "output_chars": item.output_chars,
                "completion_tokens": item.completion_tokens,
                "total_ms": item.total_ms,
                "ttft_ms": item.ttft_ms,
                "artifact_ms": item.artifact_ms,
                "page_clamped": item.page_clamped,
                "page_before": item.page_before,
                "page_after": item.page_after,
                "quality": item.quality,
                "spec": item.spec,
            }
            for item in results
        ],
    }

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n原始数据已写入 {args.json_out}")

    await dispose_engine()
    return report


def build_verdict(summaries: dict[str, dict[str, Any]], model: str) -> dict[str, Any]:
    """把指标翻译成一句可执行的结论 + 成立条件。

    判定门槛（写死在代码里，避免事后按结果调标准）：
    - 单次合法率 ≥ 0.80：否则每次重试都是一次完整调用，耗时不可控
    - 回合通过率 = 1.00：降级 mock 意味着用户看到模板文案，是产品级缺陷，一次都不该有
    - 质量通过率 ≥ 0.80：结构缺陷会直接让渲染器降级或崩
    """
    present = {
        name: summary
        for name, summary in summaries.items()
        if summary and summary.get("turns")
    }
    if not present:
        return {"verdict": "no_data", "headline": "未测到任何回合，无法下结论", "reasons": []}

    reasons: list[str] = []
    blocking: list[str] = []
    for name, summary in present.items():
        call_rate = summary["call_valid_rate"] or 0.0
        turn_rate = summary["turn_pass_rate"] or 0.0
        mock_rate = summary["mock_fallback_rate"] or 0.0
        quality_rate = summary["quality_pass_rate"] or 0.0
        reasons.append(
            f"{name}：单次合法率 {_pct(call_rate)}、回合通过率 {_pct(turn_rate)}、"
            f"降级 mock 率 {_pct(mock_rate)}、质量通过率 {_pct(quality_rate)}"
            f"（{summary['turns']} 回合 / {summary['total_calls']} 次调用）"
        )
        if mock_rate > 0:
            blocking.append(f"{name} 出现降级 mock（{_pct(mock_rate)}）")
        if call_rate < 0.80:
            blocking.append(f"{name} 单次合法率 {_pct(call_rate)} < 80%")
        if quality_rate < 0.80:
            blocking.append(f"{name} 质量通过率 {_pct(quality_rate)} < 80%")
        if summary["failed_check_items"]:
            reasons.append(f"{name} 高频失败清单项：{summary['failed_check_items']}")

    if not blocking:
        return {
            "verdict": "pass",
            "headline": f"结论：`{model}` 可以作为默认 generator（三形态全部达标）",
            "reasons": reasons,
            "conditions": [],
        }
    if any("降级 mock" in item for item in blocking):
        return {
            "verdict": "fail",
            "headline": f"结论：`{model}` 不能作为默认 generator（出现降级 mock，用户会看到模板文案）",
            "reasons": reasons,
            "blockers": blocking,
            "conditions": [],
        }
    return {
        "verdict": "conditional",
        "headline": f"结论：`{model}` 有条件通过 —— 契约稳定性够，但结构完整性有缺口",
        "reasons": reasons,
        "blockers": blocking,
        "conditions": [
            "把失败的清单项对应的业务约束写进 `_ARTIFACT_CONTRACT`（提示词补强，不改 schema）",
            "重跑本脚本，质量通过率 ≥ 80% 且降级 mock 率 = 0 才可改默认值",
        ],
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="flash 三形态产物质量与契约稳健性验证（只测量，不改业务逻辑）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--generator-model", default=DEFAULT_MODEL, help="本次验证的产物生成模型"
    )
    parser.add_argument(
        "--only",
        default="ppt,anim_2d,scene_3d",
        help="只跑哪些形态，逗号分隔",
    )
    parser.add_argument("--case-ids", default="", help="只跑指定用例 id，逗号分隔")
    parser.add_argument("--runs-per-case", type=int, default=1, help="每个用例重复几次")
    parser.add_argument(
        "--max-turns", type=int, default=20, help="硬上限，防止脚本失控烧 token"
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=DEFAULT_DATA_DIR / "sample_textbook.pdf",
        help="样本教材路径（不存在会自动生成）",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="剖析专用数据目录"
    )
    parser.add_argument(
        "--json-out", type=Path, default=DEFAULT_JSON_OUT, help="原始数据 JSON 输出路径"
    )
    args = parser.parse_args(argv)
    args.only = [item.strip() for item in str(args.only).split(",") if item.strip()]
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    pl.prepare_env(args.data_dir)
    try:
        asyncio.run(run_all(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""pytest 全局夹具：临时 SQLite、HTTP 客户端、三种 AST 样例。

为什么每个测试都用独立临时库：SQLite 单文件 + WAL，共用同一个文件时上一个测试的
残留数据会让「计数类」断言随机失败（比如 6 张表建表检查、版本号自增检查）。
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

# 兜底：即使 pytest 的 pythonpath 配置失效（例如从仓库根直接 `pytest`），也能 import app
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings, reset_settings_cache  # noqa: E402
from app.db.session import dispose_engine, init_db  # noqa: E402
from app.db.vector_store import reset_vector_store  # noqa: E402


@pytest.fixture()
async def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:
    """把 DATABASE_URL / DATA_DIR 指向临时目录，建好表后交给测试。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_API_KEY", "")  # 默认走演示模式，避免任何测试意外外呼

    reset_settings_cache()
    await dispose_engine()
    reset_vector_store()

    await init_db()

    try:
        yield tmp_path
    finally:
        await dispose_engine()
        reset_vector_store()
        reset_settings_cache()


@pytest.fixture()
async def client(db_env: Path) -> AsyncIterator[Any]:
    """httpx AsyncClient 直连 ASGI app（不占端口、不起真实服务器）。

    ASGITransport 不会执行 lifespan，所以建表由 db_env 显式完成 —— 这也是
    为什么 init_db 被设计成可独立调用。
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


# --------------------------------------------------------------------------- #
# 三种 AST 样例（正例基线）。字段顺序刻意与 §5.1 声明顺序一致，
# 这样「patch 后键序不变」的断言才有意义。
# --------------------------------------------------------------------------- #
@pytest.fixture()
def slide_deck_dict() -> dict[str, Any]:
    return {
        "type": "ppt",
        "topic": "Malayan Union 的成立与影响",
        "slides": [
            {
                "node_id": "slide-1",
                "layout": "title_hero",
                "title": "4.1 Malayan Union",
                "subtitle": "Sejarah Form 4 · Bab 4.1",
                "bullets": [],
                "comparison_left": None,
                "comparison_right": None,
                "timeline_items": None,
                "quiz_questions": None,
                "speaker_notes": "先交代时间背景：1946 年 4 月 1 日正式生效。",
                "source_ref": "Form 4 Sejarah, Bab 4.1, p.112",
            },
            {
                "node_id": "slide-2",
                "layout": "bullet_points",
                "title": "成立背景",
                "subtitle": None,
                "bullets": ["战后重建需要统一行政", "英国需要更直接的控制"],
                "comparison_left": None,
                "comparison_right": None,
                "timeline_items": None,
                "quiz_questions": None,
                "speaker_notes": "强调经济与安全两方面的动因。",
                "source_ref": "Form 4 Sejarah, Bab 4.1, p.113",
            },
            {
                "node_id": "slide-3",
                "layout": "comparison",
                "title": "联邦与联合邦的区别",
                "subtitle": None,
                "bullets": [],
                "comparison_left": ["苏丹权力被大幅削减", " citizenship 采属地原则"],
                "comparison_right": ["苏丹权力恢复", "citizenship 收紧"],
                "timeline_items": None,
                "quiz_questions": None,
                "speaker_notes": "左右对照讲，学生最容易在这里混淆。",
                "source_ref": "Form 4 Sejarah, Bab 4.1, p.114",
            },
        ],
    }


@pytest.fixture()
def keyframe_2d_dict() -> dict[str, Any]:
    return {
        "type": "anim_2d",
        "title": "马来亚疆域演变 1945-1948",
        "steps": [
            {
                "step_index": 0,
                "narration": "1945 年战后，行政版图尚未重整。",
                "source_ref": "Form 4 Sejarah, Bab 4.1, p.110",
                "entities": [
                    {
                        "id": "entity-0",
                        "shape": "zone",
                        "label": "马来亚",
                        "x": 420.0,
                        "y": 300.0,
                        "color": "#2563EB",
                        "details": "战后由英军军政管理。",
                        "source_page": 110,
                    },
                    {
                        "id": "entity-1",
                        "shape": "label_box",
                        "label": "1945",
                        "x": 420.0,
                        "y": 120.0,
                        "color": "#64748B",
                        "details": "时间标记。",
                        "source_page": None,
                    },
                ],
            },
            {
                "step_index": 1,
                "narration": "1946 年成立马来亚联邦。",
                "source_ref": "Form 4 Sejarah, Bab 4.1, p.112",
                "entities": [
                    {
                        "id": "entity-0",
                        "shape": "zone",
                        "label": "马来亚联邦",
                        "x": 460.0,
                        "y": 300.0,
                        "color": "#0EA5E9",
                        "details": "苏丹权力被削减。",
                        "source_page": 112,
                    },
                    {
                        "id": "entity-1",
                        "shape": "label_box",
                        "label": "1946",
                        "x": 460.0,
                        "y": 120.0,
                        "color": "#64748B",
                        "details": "时间标记。",
                        "source_page": None,
                    },
                ],
            },
        ],
    }


@pytest.fixture()
def scene_graph_3d_dict() -> dict[str, Any]:
    return {
        "type": "scene_3d",
        "title": "传统马来屋结构",
        "nodes": [
            {
                "id": "node-0",
                "primitive": "box",
                "label": "建筑主体",
                "position": (0.0, 1.5, 0.0),
                "scale": (2.0, 1.0, 2.0),
                "rotation": (0.0, 0.0, 0.0),
                "color": "#2563EB",
                "annotation_markdown": "主体由木构架支撑，分前后两进。",
                "source_page": 121,
                "parent_id": None,
            },
            {
                "id": "node-1",
                "primitive": "terrain_plane",
                "label": "地形基座",
                "position": (0.0, 0.0, 0.0),
                "scale": (5.0, 1.0, 5.0),
                "rotation": (0.0, 0.0, 0.0),
                "color": "#64748B",
                "annotation_markdown": "高脚屋基座以防潮。",
                "source_page": 121,
                "parent_id": "node-0",
            },
            {
                "id": "node-2",
                "primitive": "cone",
                "label": "屋顶",
                "position": (0.0, 3.0, 0.0),
                "scale": (2.2, 1.2, 2.2),
                "rotation": (0.0, 45.0, 0.0),
                "color": "#F59E0B",
                "annotation_markdown": "屋顶形制反映地域气候。",
                "source_page": 122,
                "parent_id": "node-0",
            },
        ],
        "camera_tour": [
            {
                "name": "全景",
                "position": (8.0, 6.0, 8.0),
                "target": (0.0, 1.5, 0.0),
                "explanation": "先看整体比例。",
            },
            {
                "name": "剖面",
                "position": (5.0, 2.0, 0.0),
                "target": (0.0, 1.5, 0.0),
                "explanation": "再看内部结构层次。",
            },
        ],
    }

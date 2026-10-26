"""模型档位（T01 验收 A1–A7）：档位只能来自配置，且真的进了 HTTP payload。

为什么要断言 **payload** 而不只是断言配置项：IMPL-NOTES E11 教训 1 是「配置了不等于
用上了」—— `LLM_MODEL_ROUTER` 曾经配了却一次都没被使用，而所有单测全绿。
断言 `settings.LLM_MODEL_ARTIFACT == "x"` 只能证明「读到了环境变量」，
只有抓到实际发出的 `model` 字段才能证明「用上了」。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings, _parse_model_overrides
from app.services.agent_engine import AgentEngine, TurnRequest
from app.services.llm_client import LLMClient
from app.services.router import route

from test_artifact_parallel import CHUNK_TEXT, FakeLLM, _context


# --------------------------------------------------------------------------- #
# A1 / A2 / A3 / A4：配置层
# --------------------------------------------------------------------------- #
async def test_default_artifact_model_reaches_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A1：默认配置下，`complete_json` 实际发出的 `model` 就是 `artifact_model`。"""
    captured: dict[str, Any] = {}

    async def fake_post(self: LLMClient, payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"choices": [{"message": {"content": '{"ok": 1}'}}]}

    monkeypatch.setattr(LLMClient, "_post_json", fake_post)

    settings = Settings(LLM_API_KEY="k", LLM_MODEL_ARTIFACT="artifact-x")
    await LLMClient(settings).complete_json(system="s", user="u")

    assert captured["model"] == settings.artifact_model == "artifact-x"


async def test_empty_artifact_model_falls_back_to_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A2：`LLM_MODEL_ARTIFACT=` 空 → 跟随 `LLM_MODEL_GENERATOR`（等价于换档前行为）。"""
    captured: dict[str, Any] = {}

    async def fake_post(self: LLMClient, payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"choices": [{"message": {"content": '{"ok": 1}'}}]}

    monkeypatch.setattr(LLMClient, "_post_json", fake_post)

    settings = Settings(
        LLM_API_KEY="k", LLM_MODEL_ARTIFACT="", LLM_MODEL_GENERATOR="heavy-y"
    )
    await LLMClient(settings).complete_json(system="s", user="u")

    assert captured["model"] == "heavy-y"


def test_by_type_override_only_affects_that_type() -> None:
    """A3：分形态覆盖只影响指定形态，其余仍走全局档。"""
    settings = Settings(
        LLM_API_KEY="k",
        LLM_MODEL_ARTIFACT="global-x",
        LLM_MODEL_ARTIFACT_BY_TYPE="anim_2d=heavy-z",
    )

    assert settings.artifact_model_for("anim_2d") == "heavy-z"
    assert settings.artifact_model_for("ppt") == "global-x"
    assert settings.artifact_model_for(None) == "global-x"


def test_by_type_bad_key_fails_fast() -> None:
    """A4：非法键在 `Settings()` 构造时就抛 ValueError —— 不许静默忽略。

    `pydantic.ValidationError` 是 `ValueError` 的子类，所以这里断言 ValueError
    既覆盖「pydantic 包装」也覆盖「直接抛」两种实现。
    """
    with pytest.raises(ValueError):
        Settings(LLM_API_KEY="k", LLM_MODEL_ARTIFACT_BY_TYPE="foo=bar")


def test_parse_model_overrides_accepts_empty_value() -> None:
    """`anim_2d=`（空值）= 该形态跟随全局档，不是错误。"""
    assert _parse_model_overrides("anim_2d=,scene_3d=heavy-z") == {
        "anim_2d": "",
        "scene_3d": "heavy-z",
    }
    assert _parse_model_overrides("") == {}
    assert _parse_model_overrides("  ") == {}


# --------------------------------------------------------------------------- #
# A5 / A6：分形态档位真的被引擎用上
# --------------------------------------------------------------------------- #
async def test_generate_spec_stream_passes_per_type_model() -> None:
    """A5：`_generate_spec_stream` 传的 model 与 `decision.artifact_type` 对应。"""
    llm = FakeLLM(deltas=1)
    settings = Settings(
        LLM_API_KEY="k",
        LLM_MODEL_ARTIFACT="global-x",
        LLM_MODEL_ARTIFACT_BY_TYPE="ppt=ppt-model",
    )
    engine = AgentEngine(settings=settings, llm=llm, retriever=object())  # type: ignore[arg-type]

    decision = route("4.1 Malayan Union", anchor=None, user_override="ppt")
    async for _ in engine._generate_spec_stream(
        TurnRequest(workspace_id="w", query="q"), decision, _context()
    ):
        break

    assert [model for kind, model, _ in llm.calls if kind == "artifact"] == ["ppt-model"]


async def test_generate_ops_stream_passes_per_type_model() -> None:
    """A6：patch 路径同档（按 artifact_type 取，不是写死全局档）。"""
    llm = FakeLLM(deltas=1)
    settings = Settings(
        LLM_API_KEY="k",
        LLM_MODEL_ARTIFACT="global-x",
        LLM_MODEL_ARTIFACT_BY_TYPE="scene_3d=scene-model",
    )
    engine = AgentEngine(settings=settings, llm=llm, retriever=object())  # type: ignore[arg-type]

    request = TurnRequest(
        workspace_id="w",
        query="把这一页讲细一点",
        anchor=_anchor(),
    )
    spec_dict = {"type": "scene_3d", "title": "t", "nodes": [], "camera_tour": []}
    async for _ in engine._generate_ops_stream(
        request, spec_dict, _context(), artifact_type="scene_3d"
    ):
        break

    assert [model for kind, model, _ in llm.calls if kind == "artifact"] == ["scene-model"]


# --------------------------------------------------------------------------- #
# A7：正文档位未被波及（PRD R6）
# --------------------------------------------------------------------------- #
async def test_reply_path_still_uses_router_model() -> None:
    """A7：正文调 `stream_text` 时不传 model（由 `stream_text` 内部取 ROUTER）。

    若有人「顺手统一」把产物档位也传给正文，TTFT 会立刻退化到重档 ——
    这条断言就是那道闸门。
    """
    llm = FakeLLM(deltas=2)
    settings = Settings(
        LLM_API_KEY="k",
        LLM_MODEL_ARTIFACT="artifact-x",
        LLM_MODEL_ROUTER="router-y",
    )
    engine = AgentEngine(settings=settings, llm=llm, retriever=object())  # type: ignore[arg-type]

    decision = route("4.1 Malayan Union", anchor=None, user_override="ppt")
    async for _ in engine._reply_stream(
        TurnRequest(workspace_id="w", query="q"), _retrieval(), decision, _context()
    ):
        break

    assert [model for kind, model, _ in llm.calls if kind == "reply"] == [None]


def test_router_and_artifact_models_are_independent() -> None:
    """三个档位互不牵连：改一个不影响另外两个。"""
    settings = Settings(
        LLM_API_KEY="k",
        LLM_MODEL_ROUTER="r",
        LLM_MODEL_ARTIFACT="a",
        LLM_MODEL_GENERATOR="g",
    )

    assert settings.LLM_MODEL_ROUTER == "r"
    assert settings.artifact_model == "a"
    assert settings.LLM_MODEL_GENERATOR == "g"
    # 产物档位置空只影响产物，不动 GENERATOR 的值
    assert Settings(
        LLM_API_KEY="k", LLM_MODEL_ARTIFACT="", LLM_MODEL_GENERATOR="g"
    ).LLM_MODEL_GENERATOR == "g"


def _retrieval() -> Any:
    from test_artifact_parallel import _retrieval as build

    return build()


def _anchor() -> Any:
    from app.schemas.chat import Anchor

    return Anchor(
        artifact_id="artifact-1",
        node_path="/nodes/0",
        node_label="投票站",
        section_code="4.1",
        page=76,
    )


def test_context_helper_is_stable() -> None:
    """防御：本文件依赖 `_context()` 的 `allowed_pages` 与事实切分，它变了这里先红。"""
    context = _context()
    assert context.allowed_pages == (76, 77)
    # 事实按句切分（句号被 _SENTENCE_SPLIT_RE 吃掉），首句来自 CHUNK_TEXT
    assert context.facts[0] in CHUNK_TEXT
    assert context.fact_pages[0] == 76

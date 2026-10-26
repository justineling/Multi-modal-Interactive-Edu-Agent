#!/usr/bin/env python
"""真实链路耗时剖析（P1「真实模式总耗时优化」的测量工具）。

**这个脚本只做测量，不改任何业务逻辑。**

设计原则
--------
1. **零侵入**：不修改 `backend/app/` 下任何一行代码。全部埋点靠运行期 monkeypatch
   （`_instrument_engine` / `_instrument_llm`），进程退出即失效。
2. **两套独立时钟互相校验**：
   - `事件边界法`：直接驱动 `AgentEngine.stream_turn()`，在每个 SSE 事件产出时刻打
     `perf_counter()`。这正是前端能观测到的分段耗时，作为「用户视角」的口径。
   - `方法跨度法`：包裹 `_retrieve` / `route` / `_reply_stream` / `_create_artifact_stream`
     / `_build_citations` / `_persist_turn` 等内部方法，得到「服务端视角」的口径。
   两者不一致的地方就是异步生成器缓冲/事件循环调度带来的开销，值得单独看。
3. **参数级明细**：包裹 `LLMClient._post_json` 与 `complete_json`，记录每次真实 HTTP
   调用的模型、提示词字符数、`temperature`、`max_tokens`（当前代码未传）、超时值、
   `usage` token 数。`contract_retry` 次数由「阶段内 validate 错误事件数」与
   「`_post_json` 实际调用次数」两个口径同时给出。
4. **传输层探针**：用同一份提示词再做一次 `stream=True` 的裸请求，把「等响应头 /
   首 token 到达（prefill）」与「后续 token 生成（decode）」分开计时。这是唯一能把
   「网络 + 排队 + 预填充」和「模型逐字生成」区分开的办法。

用法
----
```bash
# 完整剖析（3 次基线 + 传输层探针 + effort 对照 + 页数对照）
backend/.venv/Scripts/python.exe scripts/profile_latency.py

# 只跑基线，不跑对照实验（省 token）
backend/.venv/Scripts/python.exe scripts/profile_latency.py --runs 2 --no-probes

# 只做「2 页 vs 5 页」对照（不重复跑完整回合）
backend/.venv/Scripts/python.exe scripts/profile_latency.py --runs 1 --probe-pages 2,5

# 看所有参数
backend/.venv/Scripts/python.exe scripts/profile_latency.py --help
```

代理坑：本机若设了 `http_proxy`，脚本会在启动时自动设置 `NO_PROXY`（只影响本进程，
且本脚本直连外网、不访问 localhost，双保险）。
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import statistics
import sys
import time
import traceback
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
BACKEND_SCRIPTS = BACKEND_ROOT / "scripts"

for _path in (BACKEND_ROOT, BACKEND_SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# 默认查询：与 scripts/real_llm_check.py 保持一致，便于和历史数据对照
DEFAULT_QUERY = "4.1 Malayan Union 对苏丹的影响，梳理成一份课件"
DEFAULT_DATA_DIR = BACKEND_ROOT / "data" / "profile"


# --------------------------------------------------------------------------- #
# 通用计时工具
# --------------------------------------------------------------------------- #
def _ms(seconds: float) -> float:
    return seconds * 1000.0


@dataclass
class Span:
    """一次方法级耗时记录。`start` / `end` 是相对 `Profiler.origin` 的毫秒偏移。"""

    label: str
    start: float
    end: float
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start


class Profiler:
    """收集 Span 的桶。`origin` 用同一时刻的 `perf_counter()`，保证跨 Span 可比。"""

    def __init__(self) -> None:
        self.origin: float = time.perf_counter()
        self.spans: list[Span] = []
        self.notes: list[str] = []

    def reset(self) -> None:
        self.origin = time.perf_counter()
        self.spans = []
        self.notes = []

    def now_ms(self) -> float:
        return _ms(time.perf_counter() - self.origin)

    def add(self, label: str, start_ms: float, end_ms: float, **meta: Any) -> None:
        self.spans.append(Span(label=label, start=start_ms, end=end_ms, meta=dict(meta)))

    def of(self, label: str) -> list[Span]:
        return [span for span in self.spans if span.label == label]

    def first(self, label: str) -> Span | None:
        found = self.of(label)
        return found[0] if found else None


def _timed_async_gen(
    label: str, agen: AsyncIterator[Any], profiler: Profiler
) -> AsyncIterator[Any]:
    """包裹异步生成器：从首次 `__anext__` 开始计时，到耗尽/抛错/被关闭为止。"""
    start = profiler.now_ms()

    async def _runner() -> AsyncIterator[Any]:
        try:
            async for item in agen:
                yield item
        finally:
            profiler.add(label, start, profiler.now_ms())

    return _runner()


def instrument_async_gen(owner: Any, name: str, profiler: Profiler, label: str | None = None) -> None:
    original = getattr(owner, name)
    tag = label or name

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        return _timed_async_gen(tag, original(*args, **kwargs), profiler)

    setattr(owner, name, wrapper)


def instrument_async_fn(owner: Any, name: str, profiler: Profiler, label: str | None = None) -> None:
    original = getattr(owner, name)
    tag = label or name

    @functools.wraps(original)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = profiler.now_ms()
        try:
            return await original(*args, **kwargs)
        finally:
            profiler.add(tag, start, profiler.now_ms())

    setattr(owner, name, wrapper)


def instrument_sync_fn(owner: Any, name: str, profiler: Profiler, label: str | None = None) -> None:
    original = getattr(owner, name)
    tag = label or name

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = profiler.now_ms()
        try:
            return original(*args, **kwargs)
        finally:
            profiler.add(tag, start, profiler.now_ms())

    setattr(owner, name, wrapper)


def stats(values: Sequence[float]) -> dict[str, float]:
    """中位数 / 最小值 / 最大值。样本量为 0 时全部返回 0，避免报告里出现 NaN。"""
    clean = [float(value) for value in values]
    if not clean:
        return {"median": 0.0, "min": 0.0, "max": 0.0, "n": 0}
    return {
        "median": round(statistics.median(clean), 1),
        "min": round(min(clean), 1),
        "max": round(max(clean), 1),
        "n": len(clean),
    }


# --------------------------------------------------------------------------- #
# 环境准备：必须早于任何 `import app.*`
# --------------------------------------------------------------------------- #
def prepare_env(data_dir: Path) -> None:
    """把 DATA_DIR / DATABASE_URL 指到剖析专用目录，避免污染正式的 app.db。

    **必须在 import app.config 之前调用** —— `get_settings()` 是 `lru_cache` 单例，
    首次调用就把 .env 与环境变量解析结果固化了。
    """
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1")
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(data_dir / 'profile.db').as_posix()}"
    # 剖析用的单次调用可能超过 60s（这正是要测的东西之一），临时放宽超时，
    # 否则「超时」会把「慢」这个事实掩盖成一次重试。生产配置不动。
    os.environ["LLM_TIMEOUT_SECONDS"] = "300"


# --------------------------------------------------------------------------- #
# 语料准备
# --------------------------------------------------------------------------- #
@dataclass
class Corpus:
    workspace_id: str
    document_id: str
    pdf_path: Path
    page_count: int
    chunk_count: int
    ingest_ms: float


async def _find_reusable_corpus(pdf_path: Path) -> Corpus | None:
    """在剖析专用库里找一个「已解析完成且还有切片」的工作区，供 --reuse-corpus 复用。

    只找不建：没有就返回 None，由调用方走完整入库流程。
    """
    from app.db import repository
    from app.db.session import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        workspaces = await repository.list_workspaces(session, limit=20)
        candidates: list[Corpus] = []
        with_artifacts: Corpus | None = None
        for workspace in workspaces:
            documents = await repository.list_documents(session, workspace.id)
            for document in documents:
                if document.status != "ready":
                    continue
                chunk_count = await repository.count_chunks(session, document.id)
                if chunk_count <= 0:
                    continue
                corpus = Corpus(
                    workspace_id=workspace.id,
                    document_id=document.id,
                    pdf_path=pdf_path,
                    page_count=document.page_count,
                    chunk_count=chunk_count,
                    ingest_ms=0.0,
                )
                candidates.append(corpus)
                # 优先复用「已经有产物」的工作区，这样 --patch-turns 才有基线可点
                if with_artifacts is None:
                    artifacts = await repository.list_artifacts(session, workspace.id, limit=1)
                    if artifacts:
                        with_artifacts = corpus
                break
    return with_artifacts or (candidates[0] if candidates else None)


async def build_corpus(pdf_path: Path, *, reuse: bool = False) -> Corpus:
    """生成（或复用）样本教材 PDF，落库 + 切片 + 向量化，返回可用工作区。"""
    from app.db import repository
    from app.db.session import get_session_factory, init_db
    from app.services.pdf_parser import inspect_pdf
    from app.services.tasks import get_task_manager
    from make_sample_pdf import build_sample_pdf

    if not pdf_path.exists():
        page_count = build_sample_pdf(pdf_path, chapters=5, pages_per_subsection=2)
        print(f"  · 已生成样本教材 {pdf_path.name}（{page_count} 页）")
    else:
        print(f"  · 复用已有样本教材 {pdf_path.name}")

    await init_db()
    if reuse:
        existing = await _find_reusable_corpus(pdf_path)
        if existing is not None:
            print(
                f"  · 复用已有工作区 {existing.workspace_id[:8]}"
                f"（{existing.page_count} 页 / {existing.chunk_count} 切片，跳过入库）"
            )
            return existing

    started = time.perf_counter()
    factory = get_session_factory()

    async with factory() as session:
        workspace = await repository.create_workspace(session, "耗时剖析")
        await session.flush()
        workspace_id = workspace.id

        content = pdf_path.read_bytes()
        pages = inspect_pdf(content, max_pages=600)
        target = pdf_path.parent / f"{workspace_id}.pdf"
        target.write_bytes(content)
        document = await repository.create_document(
            session,
            workspace_id=workspace_id,
            filename=pdf_path.name,
            stored_path=str(target),
            page_count=pages,
        )
        await session.flush()
        document_id = document.id
        await session.commit()

    manager = get_task_manager()
    manager.start(document_id, workspace_id=workspace_id, stored_path=str(target))
    await manager.wait(document_id, timeout=180.0)

    async with factory() as session:
        document = await repository.require_document(session, document_id)
        status = document.status
        page_count = document.page_count
        chunk_count = await repository.count_chunks(session, document_id)
    if status != "ready":
        raise RuntimeError(f"教材解析失败，状态={status}（详见 app 日志）")

    return Corpus(
        workspace_id=workspace_id,
        document_id=document_id,
        pdf_path=pdf_path,
        page_count=page_count,
        chunk_count=chunk_count,
        ingest_ms=round(_ms(time.perf_counter() - started), 1),
    )


# --------------------------------------------------------------------------- #
# 埋点
# --------------------------------------------------------------------------- #
@dataclass
class LlmCall:
    """一次真实 HTTP 调用（`_post_json`）的完整快照。"""

    model: str
    prompt_chars: int
    system_chars: int
    user_chars: int
    temperature: float | None
    max_tokens: int | None
    response_format: str | None
    elapsed_ms: float
    status: str  # ok / http_error / network_error
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    output_chars: int = 0
    finish_reason: str = ""
    error: str = ""


@dataclass
class LlmTrace:
    """整个剖析过程中所有 LLM 交互的集合。"""

    calls: list[LlmCall] = field(default_factory=list)
    streams: list[dict[str, Any]] = field(default_factory=list)
    artifact_system: str = ""
    artifact_user: str = ""


def instrument_llm(client: Any, profiler: Profiler, trace: LlmTrace) -> None:
    """包裹 LLMClient 的三个关键方法，记录参数级明细。"""
    original_post = client._post_json
    original_complete = client.complete_json
    original_stream = client.stream_text

    @functools.wraps(original_post)
    async def post_json(payload: dict[str, Any]) -> dict[str, Any]:
        messages = payload.get("messages") or []
        system_chars = sum(len(m.get("content") or "") for m in messages[:1])
        user_chars = sum(len(m.get("content") or "") for m in messages[1:])
        response_format = (payload.get("response_format") or {}).get("type")
        started = time.perf_counter()
        status = "ok"
        error = ""
        body: dict[str, Any] = {}
        try:
            body = await original_post(payload)
            return body
        except Exception as exc:  # noqa: BLE001 - 这里要如实记录任何失败形态
            status = "http_error" if "模型服务" in str(exc) else "network_error"
            error = f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            usage = body.get("usage") or {}
            choices = body.get("choices") or [{}]
            message = (choices[0] or {}).get("message") or {}
            trace.calls.append(
                LlmCall(
                    model=str(payload.get("model")),
                    prompt_chars=system_chars + user_chars,
                    system_chars=system_chars,
                    user_chars=user_chars,
                    temperature=payload.get("temperature"),
                    max_tokens=payload.get("max_tokens"),
                    response_format=response_format,
                    elapsed_ms=round(_ms(time.perf_counter() - started), 1),
                    status=status,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    output_chars=len(str(message.get("content") or "")),
                    finish_reason=str((choices[0] or {}).get("finish_reason") or ""),
                    error=error,
                )
            )

    @functools.wraps(original_complete)
    async def complete_json(**kwargs: Any) -> dict[str, Any]:
        # 抓第一份产物提示词，供传输层探针原样重放
        if not trace.artifact_user:
            trace.artifact_system = str(kwargs.get("system") or "")
            trace.artifact_user = str(kwargs.get("user") or "")
        started = profiler.now_ms()
        try:
            return await original_complete(**kwargs)
        finally:
            profiler.add("llm.complete_json", started, profiler.now_ms())

    @functools.wraps(original_stream)
    async def stream_text(**kwargs: Any) -> AsyncIterator[str]:
        model = kwargs.get("model")
        user_chars = len(str(kwargs.get("user") or ""))
        system_chars = len(str(kwargs.get("system") or ""))
        started = time.perf_counter()
        first_at: float | None = None
        output_chars = 0
        chunk_count = 0
        try:
            async for delta in original_stream(**kwargs):
                now = time.perf_counter()
                if first_at is None:
                    first_at = now
                output_chars += len(delta)
                chunk_count += 1
                yield delta
        finally:
            finished = time.perf_counter()
            trace.streams.append(
                {
                    "model": model or "(client default)",
                    "system_chars": system_chars,
                    "user_chars": user_chars,
                    "ttft_ms": round(_ms((first_at - started) if first_at else (finished - started)), 1),
                    "total_ms": round(_ms(finished - started), 1),
                    "output_chars": output_chars,
                    "chunk_count": chunk_count,
                }
            )

    client._post_json = post_json  # type: ignore[method-assign]
    client.complete_json = complete_json  # type: ignore[method-assign]
    client.stream_text = stream_text  # type: ignore[method-assign]


def _force_generator_model(client: Any, model: str) -> None:
    """把 `complete_json` 的模型强制成指定档位（仅本进程，不改 .env）。

    `_generate_spec_stream` / `_generate_ops_stream` 现在**会显式传** `model=`
    （按 `LLM_MODEL_ARTIFACT` / `_BY_TYPE` 取档），但本 wrapper 在调用前无条件
    `kwargs["model"] = model`，所以探针语义不变：本次生效的产物档位永远等于
    `--generator-model`。JSON 里记录的模型来自 `_post_json` 实际发出的 payload，
    因此一定是**真正生效**的那个。
    """
    original = client.complete_json

    @functools.wraps(original)
    async def wrapper(**kwargs: Any) -> Any:
        kwargs["model"] = model
        return await original(**kwargs)

    client.complete_json = wrapper  # type: ignore[method-assign]


def instrument_engine(engine: Any, profiler: Profiler) -> None:
    """包裹 AgentEngine 的各阶段方法，得到服务端视角的分段耗时。

    **并行模式下 `_create_artifact_stream` 根本不会被调用**（产物走分支 B 的
    `_artifact_work`），只埋它会让「4. 产物生成」整行没有样本。所以这里两处都埋：
    `_artifact_work` 覆盖并行/串行两条路径（串行路径也是经它进去的），
    `_create_artifact_stream` / `_generate_spec_stream` 保留以兼容旧报告与串行对照。
    """
    instrument_async_gen(engine, "_reply_stream", profiler, "stage.reply_stream")
    instrument_async_fn(engine, "_artifact_work", profiler, "stage.artifact_work")
    instrument_async_gen(engine, "_create_artifact_stream", profiler, "stage.artifact_stream")
    instrument_async_gen(engine, "_generate_spec_stream", profiler, "stage.generate_spec")
    instrument_async_fn(engine, "_retrieve", profiler, "stage.retrieve")
    instrument_async_fn(engine, "_persist_turn", profiler, "stage.persist")
    instrument_sync_fn(engine, "_build_citations", profiler, "stage.citations")
    instrument_sync_fn(engine, "_drop_invalid_citations", profiler, "stage.citation_filter")
    instrument_sync_fn(engine, "_build_context", profiler, "stage.build_context")

    # `route` 是模块级纯函数，通过替换模块属性来计时（只影响本进程）
    from app.services import agent_engine as engine_module

    original_route = engine_module.route

    @functools.wraps(original_route)
    def route(*args: Any, **kwargs: Any) -> Any:
        start = profiler.now_ms()
        try:
            return original_route(*args, **kwargs)
        finally:
            profiler.add("stage.route", start, profiler.now_ms())

    engine_module.route = route  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 单次回合测量
# --------------------------------------------------------------------------- #
# 事件边界 → 阶段名。顺序即管线顺序（见 agent_engine 模块头注释）。
STAGE_LABELS: dict[str, str] = {
    "retrieval": "1. 检索 + 相关性评估",
    "modality_decision": "2. 形态路由",
    "reply": "3. 正文流式输出",
    "artifact": "4. 产物生成（LLM complete_json）",
    "citations": "5. 引用汇总 + 后校验",
    "persist": "6. 落库 + 收尾",
}


@dataclass
class TurnRun:
    """一次 create_artifact 回合的观测结果。"""

    ok: bool
    total_ms: float = 0.0
    ttft_ms: float = 0.0
    stages: dict[str, float] = field(default_factory=dict)
    event_marks: dict[str, float] = field(default_factory=dict)
    token_chunks: int = 0
    reply_chars: int = 0
    validate_errors: int = 0
    artifact_action: str = ""
    artifact_type: str = ""
    artifact_id: str = ""
    slide_count: int = 0
    spec_chars: int = 0
    grader_status: str = ""
    section_scope: str = ""
    artifact_type_route: str = ""
    route_action: str = ""
    allowed_pages: list[int] = field(default_factory=list)
    source_pages: list[int] = field(default_factory=list)
    pages_are_real: bool | None = None
    spans: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: list[LlmCall] = field(default_factory=list)
    llm_streams: list[dict[str, Any]] = field(default_factory=list)
    artifact_user_prompt: str = ""
    artifact_system_prompt: str = ""
    # 并行化的可读证据：产物 span 的起点（相对 t0）与「正文尾 ∩ 产物」的重叠时长。
    # 没有这两列，「并行到底有没有生效」只能靠总耗时猜。
    artifact_span_start_ms: float | None = None
    artifact_span_ms: float = 0.0
    overlap_ms: float = 0.0
    error: str = ""


async def run_turn(
    engine: Any,
    TurnRequest: Any,
    workspace_id: str,
    query: str,
    profiler: Profiler,
    trace: LlmTrace,
    allowed_pages_override: Sequence[int] | None = None,
    anchor: Any | None = None,
) -> TurnRun:
    """驱动一次完整的 `create_artifact` 回合，按 SSE 事件边界切分阶段。"""
    profiler.reset()
    calls_before = len(trace.calls)
    streams_before = len(trace.streams)
    user_prompt_before = trace.artifact_user

    request = TurnRequest(
        workspace_id=workspace_id, query=query, mode="syllabus_strict", anchor=anchor
    )
    marks: dict[str, float] = {}
    run = TurnRun(ok=False)

    reply_parts: list[str] = []
    token_chunks = 0
    validate_errors = 0
    spec: dict[str, Any] = {}
    allowed_pages: set[int] = set()

    try:
        async for name, payload in engine.stream_turn(request):
            now = profiler.now_ms()
            marks.setdefault(name, now)
            if name == "retrieval":
                for chunk in getattr(payload, "chunks", []) or []:
                    allowed_pages.update(range(chunk.page_start, chunk.page_end + 1))
                run.grader_status = str(getattr(payload, "grader_status", ""))
                run.section_scope = str(getattr(payload, "section_scope", "") or "")
            elif name == "modality_decision":
                run.artifact_type_route = str(getattr(payload, "artifact_type", ""))
                run.route_action = str(getattr(payload, "action", ""))
            elif name == "token":
                token_chunks += 1
                reply_parts.append(str(getattr(payload, "delta", "")))
                marks["token_last"] = now
                marks.setdefault("token_first", now)
            elif name == "error":
                if str(getattr(payload, "stage", "")) == "validate":
                    validate_errors += 1
            elif name == "artifact":
                run.artifact_action = str(getattr(payload, "action", ""))
                run.artifact_type = str(getattr(payload, "artifact_type", ""))
                run.artifact_id = str(getattr(payload, "artifact_id", "") or "")
                # ArtifactEvent.spec 是 pydantic 模型（ArtifactSpec 联合），不是 dict
                raw_spec = getattr(payload, "spec", None)
                if raw_spec is None:
                    spec = {}
                elif hasattr(raw_spec, "model_dump"):
                    spec = raw_spec.model_dump(mode="json")
                elif isinstance(raw_spec, dict):
                    spec = raw_spec
                else:
                    spec = {}
    except Exception as exc:  # noqa: BLE001 - 剖析脚本要如实记录任何异常
        run.error = f"{type(exc).__name__}: {exc}"
        run.spans = [_span_dict(span) for span in profiler.spans]
        return run

    marks.setdefault("token_first", marks.get("modality_decision", 0.0))
    marks.setdefault("token_last", marks.get("modality_decision", 0.0))
    marks.setdefault("artifact", marks.get("token_last", 0.0))
    marks.setdefault("citations", marks.get("artifact", 0.0))
    marks.setdefault("turn_end", profiler.now_ms())

    run.ok = True
    run.total_ms = round(marks["turn_end"], 1)
    run.ttft_ms = round(marks["token_first"], 1)
    run.token_chunks = token_chunks
    run.reply_chars = sum(len(part) for part in reply_parts)
    run.validate_errors = validate_errors
    run.event_marks = {key: round(value, 1) for key, value in marks.items()}

    run.stages = {
        "1. 检索 + 相关性评估": round(marks["retrieval"] - marks.get("turn_start", 0.0), 1),
        "2. 形态路由": round(marks["modality_decision"] - marks["retrieval"], 1),
        "3. 正文流式输出": round(marks["token_last"] - marks["modality_decision"], 1),
        "4. 产物生成（LLM complete_json）": round(marks["artifact"] - marks["token_last"], 1),
        "5. 引用汇总 + 后校验": round(marks["citations"] - marks["artifact"], 1),
        "6. 落库 + 收尾": round(marks["turn_end"] - marks["citations"], 1),
    }

    # ---- 并行化后的阶段归因修正（设计 §3.5）----
    # 事件边界法在并行下必然把「4. 产物生成」算成 ≈ 0（artifact 事件紧跟 token_last），
    # 而产物耗时其实已经藏进「3. 正文」的墙钟时间里了。所以第 4 阶段改走 span，
    # 并把与正文重叠的那一段从第 3 阶段里扣掉 —— 否则各阶段之和会大于总耗时，
    # 报告里的「占比」全部失真。
    artifact_span = profiler.first("stage.artifact_work")
    reply_start = marks["modality_decision"]
    reply_end = marks["token_last"]
    if artifact_span is not None:
        overlap = max(
            0.0,
            min(artifact_span.end, reply_end) - max(artifact_span.start, reply_start),
        )
        run.artifact_span_ms = round(artifact_span.duration, 1)
        run.artifact_span_start_ms = round(artifact_span.start, 1)
        run.overlap_ms = round(overlap, 1)
        run.stages["4. 产物生成（LLM complete_json）"] = run.artifact_span_ms
        run.stages["3. 正文流式输出"] = round(
            max(0.0, run.stages["3. 正文流式输出"] - run.overlap_ms), 1
        )
        # 第 5 阶段的起点是**两条分支都结束**的时刻，不是产物 span 的结束时刻：
        # - `immediate` 下产物先完（span.end < token_last），若从 span.end 起算，
        #   正文剩下的那一段会被算进「引用汇总」，阶段表就假了（自检会抓到，实测偏差 31%）；
        # - `after_first_token` / 串行下产物后完，max 取 span.end，与原行为一致。
        tail_start = max(reply_end, artifact_span.end)
        run.stages["5. 引用汇总 + 后校验"] = round(
            max(0.0, marks["citations"] - tail_start), 1
        )

    # 产物质量：契约已由 parse_artifact_spec 保证，这里只统计页数与页码真实性
    if isinstance(spec, dict):
        slides = spec.get("slides") or []
        run.slide_count = len(slides)
        run.spec_chars = len(json.dumps(spec, ensure_ascii=False))
        source_pages: list[int] = []
        for slide in slides:
            ref = str(slide.get("source_ref") or "")
            for token in ref.replace("p.", " ").replace(",", " ").split():
                if token.isdigit():
                    source_pages.append(int(token))
        run.source_pages = source_pages
        allowed = set(allowed_pages_override or allowed_pages)
        if source_pages and allowed:
            run.pages_are_real = all(page in allowed for page in source_pages)

    run.allowed_pages = sorted(allowed_pages_override or allowed_pages)
    run.spans = [_span_dict(span) for span in profiler.spans]
    run.llm_calls = list(trace.calls[calls_before:])
    run.llm_streams = list(trace.streams[streams_before:])
    if trace.artifact_user and trace.artifact_user != user_prompt_before:
        run.artifact_user_prompt = trace.artifact_user
        run.artifact_system_prompt = trace.artifact_system
    return run


def _span_dict(span: Span) -> dict[str, Any]:
    return {
        "label": span.label,
        "start": round(span.start, 1),
        "end": round(span.end, 1),
        "duration": round(span.duration, 1),
    }


# --------------------------------------------------------------------------- #
# 传输层探针：把「等首字」与「逐字生成」分开
# --------------------------------------------------------------------------- #
async def probe_stream_call(
    *,
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: float,
    label: str,
) -> dict[str, Any]:
    """用 `stream=True` 重放同一份 payload，测 TTFB / 首内容 token / 总耗时。

    DeepSeek 的 `/chat/completions` 非流式请求只能测到「整段返回」这一个时间点，
    无法区分 prefill（吃提示词长度）与 decode（吃输出 token 数）。加 `stream=True`
    后每个 SSE chunk 到达都有时间戳，两者就能分开 —— 这是判断「2 页 vs 5 页是否
    线性」以及「提示词过长拖慢首字」的关键证据。
    """
    import httpx

    body = dict(payload)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}

    started = time.perf_counter()
    headers_at: float | None = None
    first_content_at: float | None = None
    first_reasoning_at: float | None = None
    last_at: float | None = None
    output_chars = 0
    chunk_count = 0
    reasoning_chars = 0
    reasoning_chunks = 0
    usage: dict[str, Any] = {}
    text_parts: list[str] = []
    status_code = 0
    error = ""

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{base_url.rstrip('/')}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            ) as response:
                headers_at = time.perf_counter()
                status_code = response.status_code
                if response.status_code >= 400:
                    raw = await response.aread()
                    error = f"HTTP {response.status_code}: {raw[:300]!r}"
                else:
                    async for line in response.aiter_lines():
                        now = time.perf_counter()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta_obj = choices[0].get("delta") or {}
                        # v4 系列的「思考」内容走独立字段，不计入 content。
                        # 把它单独计量，才能证明「首字慢」到底是排队、预填充还是内部推理。
                        reasoning = (
                            delta_obj.get("reasoning_content")
                            or delta_obj.get("reasoning")
                            or ""
                        )
                        if reasoning:
                            reasoning_chunks += 1
                            reasoning_chars += len(reasoning)
                            if first_reasoning_at is None:
                                first_reasoning_at = now
                        delta = delta_obj.get("content") or ""
                        if delta:
                            chunk_count += 1
                            output_chars += len(delta)
                            text_parts.append(delta)
                            if first_content_at is None:
                                first_content_at = now
                            last_at = now
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    finished = time.perf_counter()
    headers_ms = _ms((headers_at - started)) if headers_at else _ms(finished - started)
    ttft_ms = _ms((first_content_at - started)) if first_content_at else _ms(finished - started)
    decode_ms = _ms((last_at - first_content_at)) if (last_at and first_content_at) else 0.0
    reasoning_ms = (
        _ms((first_content_at - first_reasoning_at))
        if (first_reasoning_at and first_content_at)
        else 0.0
    )
    completion_tokens = usage.get("completion_tokens")
    return {
        "label": label,
        "status_code": status_code,
        "error": error,
        "model": body.get("model"),
        "effort": body.get("effort"),
        "prompt_chars": sum(len(str(m.get("content") or "")) for m in body.get("messages") or []),
        "headers_ms": round(headers_ms, 1),
        "ttft_ms": round(ttft_ms, 1),
        "decode_ms": round(decode_ms, 1),
        "total_ms": round(_ms(finished - started), 1),
        "reasoning_ms": round(reasoning_ms, 1),
        "reasoning_chars": reasoning_chars,
        "reasoning_chunks": reasoning_chunks,
        "output_chars": output_chars,
        "chunk_count": chunk_count,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": completion_tokens,
        "tokens_per_sec": (
            round(completion_tokens / (decode_ms / 1000.0), 2)
            if completion_tokens and decode_ms > 0
            else None
        ),
        "output_text": "".join(text_parts),
    }


# --------------------------------------------------------------------------- #
# 提示词变体（对照实验用）
# --------------------------------------------------------------------------- #
_TWO_PAGE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("共 5~8 页", "共 2 页"),
    (
        "中间各页在 bullet_points / timeline / comparison / cause_effect 里至少用 3 种不同取值；",
        "",
    ),
    ("照此结构扩到 5~8 页", "照此结构补足到 2 页"),
)

_FIVE_PAGE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("共 5~8 页", "共 5 页"),
    ("照此结构扩到 5~8 页", "照此结构扩到 5 页"),
)


def make_page_variant(prompt: str, pages: int) -> str:
    """把产物提示词改成「只输出 N 页」。用于页数-耗时线性度的对照实验。

    只做字符串替换，不做任何业务语义改动；替换标记找不到就直接报错，
    避免「实验悄悄跑了个没改动的提示词」这种最坏情况。
    """
    if pages == 2:
        replacements = _TWO_PAGE_REPLACEMENTS
    elif pages == 5:
        replacements = _FIVE_PAGE_REPLACEMENTS
    else:
        raise ValueError(f"页数变体只支持 2 或 5，收到 {pages}")

    text = prompt
    for old, new in replacements:
        if old not in text:
            raise RuntimeError(f"提示词里找不到替换标记 {old!r}，契约文本可能已变更，请更新脚本")
        text = text.replace(old, new)
    suffix = (
        f"\n\n【本次页数】只输出 {pages} 页，不要更多也不要更少。"
        if pages != 5
        else "\n\n【本次页数】只输出 5 页，不要更多也不要更少。"
    )
    return text + suffix


# --------------------------------------------------------------------------- #
# 报告输出
# --------------------------------------------------------------------------- #
def print_stage_table(runs: Sequence[TurnRun]) -> None:
    print("\n" + "=" * 78)
    print("分段耗时（1/2/5/6 走事件边界，3/4 走 span 并扣除重叠，单位 ms；取中位数）")
    print("=" * 78)
    labels = list(STAGE_LABELS.values())
    totals = [run.total_ms for run in runs]
    total_median = statistics.median(totals) if totals else 0.0
    print(f"{'阶段':<34}{'中位数':>10}{'最小':>10}{'最大':>10}{'占比':>9}")
    print("-" * 78)
    for label in labels:
        values = [run.stages.get(label, 0.0) for run in runs]
        summary = stats(values)
        share = (summary["median"] / total_median * 100.0) if total_median else 0.0
        print(
            f"{label:<34}{summary['median']:>10.1f}{summary['min']:>10.1f}"
            f"{summary['max']:>10.1f}{share:>8.1f}%"
        )
    print("-" * 78)
    summary = stats(totals)
    print(f"{'合计':<34}{summary['median']:>10.1f}{summary['min']:>10.1f}{summary['max']:>10.1f}{100.0:>8.1f}%")
    ttfts = [run.ttft_ms for run in runs]
    ttft = stats(ttfts)
    print(f"{'（其中 TTFT，累计到首个 token）':<34}{ttft['median']:>10.1f}{ttft['min']:>10.1f}{ttft['max']:>10.1f}")
    # 并行是否真的生效，看这两行：产物 span 起点若落在正文区间内、且 overlap > 0，
    # 就说明两条分支确实在同时跑；overlap ≈ 0 则是串行。
    starts = [run.artifact_span_start_ms for run in runs if run.artifact_span_start_ms is not None]
    overlaps = [run.overlap_ms for run in runs]
    if starts:
        start_stats = stats([float(value) for value in starts])
        print(
            f"{'（产物 span 起点，相对 t0）':<34}{start_stats['median']:>10.1f}"
            f"{start_stats['min']:>10.1f}{start_stats['max']:>10.1f}"
        )
    overlap_stats = stats(overlaps)
    print(
        f"{'（overlap = 正文尾 ∩ 产物）':<34}{overlap_stats['median']:>10.1f}"
        f"{overlap_stats['min']:>10.1f}{overlap_stats['max']:>10.1f}"
    )


def assert_stage_sum(runs: Sequence[TurnRun], *, tolerance: float = 0.10) -> None:
    """自检：各阶段耗时之和必须与总耗时一致（±10%），否则非零退出。

    为什么值得直接失败而不是打个警告：并行化之后阶段表**很容易在完全不报错的情况下
    变成假数字**（产物耗时被算成 0、重叠部分被重复计入、span 没埋上）。这类退化只在
    验收时被人读成「并行没生效」或「换档没生效」，比脚本当场失败难查得多。
    """
    bad: list[str] = []
    for index, run in enumerate(runs, start=1):
        if not run.ok or run.total_ms <= 0:
            continue
        stage_sum = sum(run.stages.values())
        error = abs(stage_sum - run.total_ms) / run.total_ms
        if error > tolerance:
            bad.append(
                f"第 {index} 次：Σ阶段={stage_sum:.0f}ms 总耗时={run.total_ms:.0f}ms"
                f" 偏差={error * 100:.1f}%"
            )
    if bad:
        print("\n!! 阶段表自检失败（|Σ阶段 − 总耗时| / 总耗时 > 10%），阶段表不可用于验收：")
        for line in bad:
            print(f"   {line}")
        raise SystemExit(2)
    print("\n阶段表自检通过：各阶段之和与总耗时偏差 ≤ 10%")


def print_llm_table(runs: Sequence[TurnRun]) -> None:
    print("\n" + "=" * 78)
    print("产物生成阶段参数级明细（每次 complete_json 的真实 HTTP 调用）")
    print("=" * 78)
    print(
        f"{'#':<3}{'model':<18}{'prompt字符':>11}{'temperature':>12}"
        f"{'max_tokens':>11}{'耗时ms':>10}{'prompt_tok':>11}{'out_tok':>9}"
    )
    print("-" * 78)
    index = 0
    for run in runs:
        for call in run.llm_calls:
            index += 1
            print(
                f"{index:<3}{call.model:<18}{call.prompt_chars:>11}{str(call.temperature):>12}"
                f"{str(call.max_tokens):>11}{call.elapsed_ms:>10.1f}"
                f"{str(call.prompt_tokens):>11}{str(call.completion_tokens):>9}"
            )
            if call.error:
                print(f"    !! {call.error}")
    print("-" * 78)
    for run in runs:
        print(
            f"  本次回合：contract_retry={run.validate_errors} 次，"
            f"complete_json 实际调用={len(run.llm_calls)} 次，"
            f"产物 {run.slide_count} 页 / {run.spec_chars} 字符，"
            f"页码真实={run.pages_are_real}"
        )
    print("\n正文流式（stream_text）：")
    for run in runs:
        for stream in run.llm_streams:
            print(
                f"  model={stream['model']}  TTFT={stream['ttft_ms']}ms  "
                f"总={stream['total_ms']}ms  输出={stream['output_chars']}字符  "
                f"chunk={stream['chunk_count']}"
            )


def print_span_table(runs: Sequence[TurnRun]) -> None:
    print("\n" + "=" * 78)
    print("服务端方法跨度（与事件边界法互相校验）")
    print("=" * 78)
    label_order = [
        "stage.retrieve",
        "stage.route",
        "stage.build_context",
        "stage.reply_stream",
        "stage.artifact_stream",
        "stage.generate_spec",
        "llm.complete_json",
        "stage.citations",
        "stage.citation_filter",
        "stage.persist",
    ]
    for label in label_order:
        values: list[float] = []
        for run in runs:
            for span in run.spans:
                if span["label"] == label:
                    values.append(float(span["duration"]))
                    break
        if not values:
            continue
        summary = stats(values)
        print(f"{label:<26}{summary['median']:>10.1f}{summary['min']:>10.1f}{summary['max']:>10.1f}")


def print_probes(probes: Sequence[dict[str, Any]]) -> None:
    if not probes:
        return
    print("\n" + "=" * 78)
    print("传输层探针（stream=True 重放同一提示词，分离 prefill 与 decode）")
    print("=" * 78)
    print(
        f"{'实验':<26}{'effort':>8}{'提示词字符':>11}{'TTFBms':>10}{'首字ms':>10}"
        f"{'思考ms':>10}{'生成ms':>10}{'总ms':>9}{'out_tok':>9}{'tok/s':>8}"
    )
    print("-" * 78)
    for probe in probes:
        if probe.get("error"):
            print(f"{probe['label']:<26}  !! {probe['error'][:120]}")
            continue
        print(
            f"{probe['label']:<26}{str(probe.get('effort') or 'default'):>8}"
            f"{probe['prompt_chars']:>11}{probe['headers_ms']:>10.1f}"
            f"{probe['ttft_ms']:>10.1f}{probe.get('reasoning_ms', 0.0):>10.1f}"
            f"{probe['decode_ms']:>10.1f}"
            f"{probe['total_ms']:>9.1f}{str(probe.get('completion_tokens')):>9}"
            f"{str(probe.get('tokens_per_sec')):>8}"
        )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run_all(args: argparse.Namespace) -> dict[str, Any]:
    from app.config import get_settings
    from app.db.session import dispose_engine
    from app.schemas.artifacts import parse_artifact_spec
    from app.services.agent_engine import AgentEngine, TurnRequest, _ARTIFACT_CONTRACT
    from app.services.llm_client import get_llm_client
    from app.services.retriever import get_retriever

    settings = get_settings()
    if settings.demo_mode:
        raise SystemExit("当前是演示模式（LLM_API_KEY 为空），无法剖析真实链路。请检查 backend/.env。")

    profiler = Profiler()
    trace = LlmTrace()
    client = get_llm_client()
    instrument_llm(client, profiler, trace)
    if args.generator_model:
        _force_generator_model(client, args.generator_model)
    if args.fork_mode:
        # 只在本进程覆盖，不动 .env：A/B 要的是「同一份配置下换一个变量」
        settings.ARTIFACT_FORK_MODE = args.fork_mode  # type: ignore[misc]
    engine = AgentEngine(llm=client, retriever=get_retriever())
    instrument_engine(engine, profiler)

    print("=" * 78)
    print("环境")
    print("=" * 78)
    print(f"  base_url      = {settings.LLM_BASE_URL}")
    print(f"  router 模型   = {settings.LLM_MODEL_ROUTER}   （正文流式）")
    # 产物档位现在来自 `LLM_MODEL_ARTIFACT`（空串则跟随 GENERATOR），不再直接读 GENERATOR。
    # 这里必须跟着改，否则报告里写的「generator 模型」与实际发出的 payload 不是一回事。
    artifact_model = args.generator_model or settings.artifact_model
    print(f"  artifact 模型 = {artifact_model}   （产物 JSON）"
          + ("  ← --generator-model 覆盖" if args.generator_model else ""))
    print(f"  GENERATOR     = {settings.LLM_MODEL_GENERATOR}   （artifact 的回退目标）")
    print(f"  并行开关      = {settings.ARTIFACT_PARALLEL_ENABLED}"
          f"   fork 模式 = {settings.ARTIFACT_FORK_MODE}"
          f"   并发上限 = {settings.ARTIFACT_MAX_CONCURRENCY}")
    print(f"  超时          = {settings.LLM_TIMEOUT_SECONDS}s   重试上限 = {settings.LLM_MAX_RETRIES}")
    print(f"  TOP_K         = {settings.RETRIEVAL_TOP_K}   embedding = {settings.EMBEDDING_MODE}")

    print("\n准备语料…")
    corpus = await build_corpus(args.pdf, reuse=args.reuse_corpus)
    print(
        f"  · workspace={corpus.workspace_id[:8]}  {corpus.page_count} 页 / "
        f"{corpus.chunk_count} 切片  入库耗时={corpus.ingest_ms}ms"
    )

    print(f"\n跑 {args.runs} 次 create_artifact 回合（query={args.query!r}）…")
    runs: list[TurnRun] = []
    for index in range(1, args.runs + 1):
        print(f"  [{index}/{args.runs}] 调用中…", end="", flush=True)
        run = await run_turn(
            engine, TurnRequest, corpus.workspace_id, args.query, profiler, trace
        )
        if not run.ok:
            print(f" 失败：{run.error}")
            traceback.print_exc()
            continue
        runs.append(run)
        print(
            f" 总 {run.total_ms / 1000:.1f}s  TTFT {run.ttft_ms / 1000:.1f}s  "
            f"产物 {run.slide_count} 页  retry={run.validate_errors}  "
            f"action={run.artifact_action}"
        )

    # 提示词缓存：让对照实验可以在不重跑基线回合的情况下重复执行（省 token）
    prompt_cache = args.prompt_cache
    if runs and runs[0].artifact_user_prompt:
        prompt_cache.parent.mkdir(parents=True, exist_ok=True)
        prompt_cache.write_text(
            json.dumps(
                {
                    "system": runs[0].artifact_system_prompt,
                    "user": runs[0].artifact_user_prompt,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  · 产物提示词已缓存到 {prompt_cache}")

    # ---- patch 回合（PRD 的 T5 基线）：点着某个节点追问，只改目标子树 ----
    patch_runs: list[TurnRun] = []
    if args.patch_turns > 0:
        from app.schemas.chat import Anchor

        target_artifact = runs[0].artifact_id if (runs and runs[0].artifact_id) else ""
        if not target_artifact:
            # --runs 0 场景：从库里捞最近一份产物，避免为了测 patch 再跑一次全量生成
            from app.db import repository
            from app.db.session import get_session_factory

            async with get_session_factory()() as session:
                existing = await repository.list_artifacts(session, corpus.workspace_id, limit=1)
            if existing:
                target_artifact = existing[0].id
                print(f"\n  复用库中已有产物 {target_artifact[:8]} 作为 patch 基线")

        if not target_artifact:
            print("\n!! 没有可复用的基线产物，跳过 patch 回合测量")
        else:
            first = runs[0] if runs else None
            anchor = Anchor(
                artifact_id=target_artifact,
                node_path="/slides/1",
                node_label="第 2 页",
                page=first.allowed_pages[0] if (first and first.allowed_pages) else 76,
                section_code=(first.section_scope if first else "") or "4.1.2",
            )
            print(f"\n跑 {args.patch_turns} 次 patch 回合（anchor=/slides/1，section={anchor.section_code}）…")
            for index in range(1, args.patch_turns + 1):
                print(f"  [{index}/{args.patch_turns}] 调用中…", end="", flush=True)
                run = await run_turn(
                    engine,
                    TurnRequest,
                    corpus.workspace_id,
                    args.patch_query,
                    profiler,
                    trace,
                    anchor=anchor,
                )
                if not run.ok:
                    print(f" 失败：{run.error}")
                    continue
                patch_runs.append(run)
                print(
                    f" 总 {run.total_ms / 1000:.1f}s  TTFT {run.ttft_ms / 1000:.1f}s  "
                    f"action={run.artifact_action}  retry={run.validate_errors}"
                )
            if patch_runs:
                print("\n" + "=" * 78)
                print("patch 回合（T5 基线）")
                print("=" * 78)
                for index, run in enumerate(patch_runs, start=1):
                    print(f"  第 {index} 次：总 {run.total_ms:.0f}ms  TTFT {run.ttft_ms:.0f}ms")
                    for label, value in run.stages.items():
                        print(f"      {label:<32}{value:>10.1f}ms")
                patch_median = statistics.median([r.total_ms for r in patch_runs])
                create_median = statistics.median([r.total_ms for r in runs]) if runs else 0.0
                if create_median:
                    print(
                        f"  → patch 中位 {patch_median:.0f}ms / create 中位 {create_median:.0f}ms "
                        f"= {patch_median / create_median * 100:.1f}%（P0-10③ 要求 ≤ 50%）"
                    )

    if not runs and not prompt_cache.exists():
        raise SystemExit("没有成功的基线回合，也没有可复用的提示词缓存，无法产出报告。")

    contract_chars = len(_ARTIFACT_CONTRACT["ppt"])
    if runs:
        print_stage_table(runs)
        # 先自检再用表：阶段表失真时直接非零退出，别让假数字流进验收报告
        assert_stage_sum(runs)
        print_llm_table(runs)
        print_span_table(runs)

        # ---- 并行是否真的生效：产物分支与正文是否在时间上重叠 ----
        # 改动前这里叫「假设 A：正文与产物是否串行」，只能**预测**收益上限；
        # 现在并行已落地，所以改成直接报实测重叠（overlap），串行模式下它自然是 0。
        print("\n" + "=" * 78)
        print("并行验证：正文分支与产物分支是否真的同时运行")
        print("=" * 78)
        for index, run in enumerate(runs, start=1):
            reply_span = next((s for s in run.spans if s["label"] == "stage.reply_stream"), None)
            artifact_span = next(
                (
                    s
                    for s in run.spans
                    if s["label"] in ("stage.artifact_work", "stage.artifact_stream")
                ),
                None,
            )
            if reply_span and artifact_span:
                gap = artifact_span["start"] - reply_span["end"]
                print(
                    f"  第 {index} 次：正文 {reply_span['start']:.0f}→{reply_span['end']:.0f}ms "
                    f"({reply_span['duration']:.0f}ms)，产物 {artifact_span['start']:.0f}→"
                    f"{artifact_span['end']:.0f}ms ({artifact_span['duration']:.0f}ms)，"
                    f"起点差 {gap:+.0f}ms，实测重叠 {run.overlap_ms:.0f}ms"
                    f" → {'并行' if run.overlap_ms > 0 else '串行'}"
                )
        overlaps = [run.overlap_ms for run in runs]
        overlap_median = statistics.median(overlaps) if overlaps else 0.0
        reply_durations = [
            float(s["duration"])
            for run in runs
            for s in run.spans
            if s["label"] == "stage.reply_stream"
        ]
        artifact_durations = [run.artifact_span_ms for run in runs if run.artifact_span_ms]
        reply_median = statistics.median(reply_durations) if reply_durations else 0.0
        artifact_median = (
            statistics.median(artifact_durations) if artifact_durations else 0.0
        )
        print(
            f"  → 实测重叠中位 {overlap_median:.0f}ms；理论上限 "
            f"min(正文 {reply_median:.0f}ms, 产物 {artifact_median:.0f}ms) "
            f"= {min(reply_median, artifact_median):.0f}ms"
        )
        if overlap_median <= 0:
            print("    注意：重叠为 0 —— 要么 ARTIFACT_PARALLEL_ENABLED=false，")
            print("    要么产物分支在正文结束后才被调度（此时 T4 不会有改善，先查埋点）")

    # ---- 对照实验 ----
    probes: list[dict[str, Any]] = []
    allowed_pages: list[int] = []
    probe_output_dir = args.data_dir / "probe-outputs"

    if not args.no_probes:
        # 检索是本地确定性计算（哈希向量 + BM25），不消耗 token，
        # 单独跑一次拿到「允许的页码集合」，用于判定产物页码是否真实。
        probe_retrieval = await engine._retrieve(
            TurnRequest(workspace_id=corpus.workspace_id, query=args.query, mode="syllabus_strict")
        )
        allowed_pages = sorted(probe_retrieval.pages)
        print(f"\n  检索允许页码：{allowed_pages}（grader={probe_retrieval.grader_status}）")
        cached = (
            json.loads(prompt_cache.read_text(encoding="utf-8"))
            if prompt_cache.exists()
            else {}
        )
        system_prompt = (runs[0].artifact_system_prompt if runs else "") or cached.get("system", "")
        base_prompt = (runs[0].artifact_user_prompt if runs else "") or cached.get("user", "")
        if not base_prompt:
            print("\n!! 没抓到产物提示词，跳过对照实验")
        else:
            payload = {
                "model": settings.LLM_MODEL_GENERATOR,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": base_prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            }
            base_url = settings.LLM_BASE_URL
            api_key = settings.LLM_API_KEY
            timeout = settings.LLM_TIMEOUT_SECONDS
            default_pages = args.probe_pages[0] if args.probe_pages else 5

            print("\n" + "=" * 78)
            print("对照实验（每次都是一次完整真实调用，消耗 token）")
            print("=" * 78)

            for pages in args.probe_pages:
                variant = payload
                if pages != 5:
                    try:
                        variant = dict(payload)
                        variant["messages"] = [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": make_page_variant(base_prompt, pages)},
                        ]
                    except RuntimeError as exc:
                        print(f"  !! {pages} 页变体构造失败：{exc}")
                        continue
                print(f"  · 页数={pages} 探针…", flush=True)
                result = await probe_stream_call(
                    base_url=base_url,
                    api_key=api_key,
                    payload=variant,
                    timeout=timeout,
                    label=f"{pages} 页 / 默认 effort",
                )
                probes.append(result)
                _dump_probe(result, probe_output_dir, len(probes))
                _report_probe_quality(result, pages, parse_artifact_spec, allowed_pages)

            for effort in args.probe_efforts:
                if effort == "default":
                    continue
                variant = dict(payload)
                variant["effort"] = effort
                print(f"  · effort={effort} 探针…", flush=True)
                result = await probe_stream_call(
                    base_url=base_url,
                    api_key=api_key,
                    payload=variant,
                    timeout=timeout,
                    label=f"{default_pages} 页 / effort={effort}",
                )
                probes.append(result)
                _dump_probe(result, probe_output_dir, len(probes))
                _report_probe_quality(result, default_pages, parse_artifact_spec, allowed_pages)

            for model in args.probe_models:
                # 跳过条件跟着档位语义走：与**当前产物档位**相同就没必要重复探针。
                # 换档前这里比的是 `LLM_MODEL_GENERATOR`，换档后那会让
                # 「探针 deepseek-flash」与「基线已经是 flash」撞车、白白多打一次真实调用。
                if not model or model == settings.artifact_model:
                    continue
                variant = dict(payload)
                variant["model"] = model
                print(f"  · model={model} 探针…", flush=True)
                result = await probe_stream_call(
                    base_url=base_url,
                    api_key=api_key,
                    payload=variant,
                    timeout=timeout,
                    label=f"{default_pages} 页 / {model}",
                )
                probes.append(result)
                _dump_probe(result, probe_output_dir, len(probes))
                _report_probe_quality(result, default_pages, parse_artifact_spec, allowed_pages)

            print_probes(probes)

    # ---- 汇总 ----
    report = {
        "environment": {
            "base_url": settings.LLM_BASE_URL,
            "router_model": settings.LLM_MODEL_ROUTER,
            "generator_model": settings.LLM_MODEL_GENERATOR,
            "timeout_seconds": settings.LLM_TIMEOUT_SECONDS,
            "max_retries": settings.LLM_MAX_RETRIES,
            "retrieval_top_k": settings.RETRIEVAL_TOP_K,
            "embedding_mode": settings.EMBEDDING_MODE,
        },
        "corpus": {
            "pdf": str(corpus.pdf_path.name),
            "pages": corpus.page_count,
            "chunks": corpus.chunk_count,
            "ingest_ms": corpus.ingest_ms,
        },
        "query": args.query,
        "runs": args.runs,
        "stages": {
            label: stats([run.stages.get(label, 0.0) for run in runs])
            for label in STAGE_LABELS.values()
        },
        "total": stats([run.total_ms for run in runs]),
        "ttft": stats([run.ttft_ms for run in runs]),
        "reply_stream": stats(
            [
                float(s["duration"])
                for run in runs
                for s in run.spans
                if s["label"] == "stage.reply_stream"
            ]
        ),
        "artifact_stream": stats(
            [
                float(s["duration"])
                for run in runs
                for s in run.spans
                if s["label"] == "stage.artifact_stream"
            ]
        ),
        "contract_chars": contract_chars,
        "turns": [
            {
                "total_ms": run.total_ms,
                "ttft_ms": run.ttft_ms,
                "stages": run.stages,
                "validate_errors": run.validate_errors,
                "slide_count": run.slide_count,
                "spec_chars": run.spec_chars,
                "pages_are_real": run.pages_are_real,
                "artifact_type": run.artifact_type,
                "grader_status": run.grader_status,
                "route_action": run.route_action,
                "llm_calls": [call.__dict__ for call in run.llm_calls],
                "llm_streams": run.llm_streams,
                "spans": run.spans,
            }
            for run in runs
        ],
        "probes": [
            {key: value for key, value in probe.items() if key != "output_text"}
            for probe in probes
        ],
        "patch_runs": [
            {
                "total_ms": run.total_ms,
                "ttft_ms": run.ttft_ms,
                "stages": run.stages,
                "artifact_action": run.artifact_action,
                "validate_errors": run.validate_errors,
                "llm_calls": [call.__dict__ for call in run.llm_calls],
            }
            for run in patch_runs
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


def _report_probe_quality(
    result: dict[str, Any],
    pages: int,
    parse_artifact_spec: Callable[[Any], Any],
    allowed_pages: Sequence[int] | None = None,
) -> None:
    """检查探针产物是否仍满足契约 —— 「低 effort / 少页数 / 换模型」不能以产物不可用为代价。"""
    text = result.get("output_text") or ""
    if not text:
        print(f"      产物校验：无输出（{result.get('error') or '空'}）")
        return
    from app.services.llm_client import extract_json_object

    try:
        payload = extract_json_object(text)
        candidate = payload.get("spec", payload)
        parse_artifact_spec(candidate)
    except Exception as exc:  # noqa: BLE001
        result["contract_ok"] = False
        result["contract_error"] = str(exc)[:300]
        print(f"      产物校验：❌ {type(exc).__name__}: {str(exc)[:160]}")
        return
    slides = candidate.get("slides") or []
    result["contract_ok"] = True
    result["slide_count"] = len(slides)
    result["output_json_chars"] = len(json.dumps(candidate, ensure_ascii=False))
    layouts = [str(slide.get("layout")) for slide in slides]
    print(
        f"      产物校验：✅ 契约通过，{len(slides)} 页（目标 {pages}），"
        f"布局={layouts}"
    )
    if result.get("reasoning_chars"):
        print(
            f"      思考阶段：{result['reasoning_ms']}ms / {result['reasoning_chars']} 字符"
            f"（{result['reasoning_chunks']} chunk）"
        )
    else:
        print("      思考阶段：未观测到 reasoning_content 字段")

    # 页码真实性：source_ref 里出现的页码必须落在检索切片的页码区间内
    allowed = set(allowed_pages or [])
    cited = _source_ref_pages(slides)
    result["source_ref_pages"] = cited
    if cited and allowed:
        bad = sorted(page for page in cited if page not in allowed)
        result["pages_are_real"] = not bad
        print(
            f"      页码真实性：{'✅ 全部落在检索区间内' if not bad else f'❌ 越界 {bad}'}"
            f"（引用 {sorted(set(cited))}，允许 {min(allowed)}-{max(allowed)}）"
        )
    else:
        print(f"      页码真实性：无法判定（引用页码={sorted(set(cited))}，允许集为空）")
    notes = [str(slide.get("speaker_notes") or "") for slide in slides]
    if notes:
        avg = sum(len(note) for note in notes) / len(notes)
        print(f"      speaker_notes 平均 {avg:.0f} 字符，首条：{notes[0][:60]}")


def _dump_probe(result: dict[str, Any], directory: Path, index: int) -> None:
    """把探针产物的原始输出落盘，便于人工核对内容质量（不参与任何业务逻辑）。"""
    text = result.get("output_text") or ""
    if not text:
        return
    directory.mkdir(parents=True, exist_ok=True)
    slug = str(result.get("label") or f"probe-{index}").replace("/", "_").replace(" ", "")
    target = directory / f"{index:02d}-{slug}.txt"
    target.write_text(text, encoding="utf-8")
    print(f"      原始输出已保存：{target.name}")


def _source_ref_pages(slides: Sequence[Any]) -> list[int]:
    """从 `source_ref`（格式约定 "4.1.3, p.76"）里抠出页码。"""
    import re

    pages: list[int] = []
    for slide in slides:
        ref = str(slide.get("source_ref") or "")
        for match in re.finditer(r"p\.\s*(\d+)(?:\s*-\s*(\d+))?", ref):
            pages.append(int(match.group(1)))
            if match.group(2):
                pages.append(int(match.group(2)))
    return pages


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="真实 LLM 链路耗时剖析（只测量，不改业务逻辑）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--runs", type=int, default=3, help="基线 create_artifact 回合次数（建议 2-3）"
    )
    parser.add_argument("--query", default=DEFAULT_QUERY, help="提问内容")
    parser.add_argument(
        "--probe-pages",
        default="5,2",
        help="页数对照实验，逗号分隔（如 5,2）。5 表示原始契约，2 表示改写成 2 页",
    )
    parser.add_argument(
        "--probe-efforts",
        default="default,low",
        help="effort 对照实验，逗号分隔（default,low,high,max）",
    )
    parser.add_argument(
        "--probe-models",
        default="",
        help="模型对照实验，逗号分隔（如 deepseek-flash）。空 = 不跑",
    )
    parser.add_argument(
        "--generator-model",
        default="",
        help="强制产物生成模型（只在本进程生效，不改 backend/.env）。空 = 用 .env 里的值",
    )
    parser.add_argument(
        "--fork-mode",
        default="",
        choices=["", "after_first_token", "immediate"],
        help=(
            "覆盖 ARTIFACT_FORK_MODE（只在本进程生效）。A/B 用：同一批问句各跑一遍，"
            "比对 TTFT P50/P90 与 T4 P90。空 = 用 .env 里的值"
        ),
    )
    parser.add_argument(
        "--patch-turns", type=int, default=0, help="patch 回合测量次数（0 = 不测，>0 需先有基线产物）"
    )
    parser.add_argument(
        "--patch-query",
        default="这一页的要点拆细一点，并说明对普通人的影响",
        help="patch 回合的追问内容",
    )
    parser.add_argument(
        "--reuse-corpus",
        action="store_true",
        help="复用剖析库里已解析好的工作区，跳过重新入库（配合 --runs 0 使用）",
    )
    parser.add_argument("--no-probes", action="store_true", help="跳过所有对照实验（省 token）")
    parser.add_argument(
        "--prompt-cache",
        type=Path,
        default=DEFAULT_DATA_DIR / "artifact_prompt.json",
        help="产物提示词缓存（基线回合成功后写入，供 --runs 0 的对照实验复用）",
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=DEFAULT_DATA_DIR / "sample_textbook.pdf",
        help="样本教材路径（不存在会自动生成）",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="剖析专用数据目录（不污染正式 data/app.db）",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=PROJECT_ROOT / "docs" / "increments" / "latency-profile.raw.json",
        help="原始数据 JSON 输出路径",
    )
    args = parser.parse_args(argv)
    args.probe_pages = [int(item) for item in str(args.probe_pages).split(",") if item.strip()]
    args.probe_efforts = [item.strip() for item in str(args.probe_efforts).split(",") if item.strip()]
    args.probe_models = [item.strip() for item in str(args.probe_models).split(",") if item.strip()]
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    prepare_env(args.data_dir)
    try:
        asyncio.run(run_all(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

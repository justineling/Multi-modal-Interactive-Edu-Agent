"""OpenAI 兼容 LLM 客户端（httpx 直连，不引 openai SDK）。

为什么不用官方 SDK：只要 `/chat/completions` 一个端点，httpx 30 行搞定；
SDK 会带来版本耦合与额外依赖，而 MVP 的全部收益只是「少写几行」。

职责边界：
- 本模块只负责**传输 + JSON 抽取 + 传输层重试**；
- 「Pydantic 校验失败 → 带错误信息重试」放在 agent_engine（那里才知道目标模型是什么），
  因为重试时要把校验错误回灌给模型，必须能构造模型相关的错误描述。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

import httpx

from app.config import Settings, get_settings
from app.errors import AppError, ErrorCode
from app.logging_conf import log_event

T = TypeVar("T")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
# 传输层重试间隔（§10 第 1 条）：0 / 0.4 / 1.0 秒
RETRY_BACKOFF = (0.0, 0.4, 1.0)


class RateLimitedError(AppError):
    """上游返回 429。**必须单独成类**，不能混在通用 `AppError` 里。

    原因：`agent_engine._generate_spec_stream` 把 `AppError` 一律当「契约校验失败」重试
    （3 次），而 429 重试三次既不会好，还会把配额与延迟一起放大。单独成类之后，
    产物分支可以 `except RateLimitedError: raise` 把它原样上抛，由主路径在**正文结束之后**
    串行重试（那时系统里只剩这一个请求，成功率最高）。

    另外它决定了错误归因：429 的 error 事件必须是 `stage="generate"`，不能是 `"validate"` ——
    否则 `flash_quality_check` 的 `validate_errors` 会虚增，G1 retry 率算错（设计 §3.3.4 MC-4）。
    """

    def __init__(self, message: str | None = None, *, retry_after: float | None = None) -> None:
        super().__init__(ErrorCode.LLM_UPSTREAM_ERROR, message or "模型服务限流（429）")
        # 上游 `Retry-After` 头（秒）。留着供将来做「按服务端建议等待」，
        # 当前串行重试不看它（设计 §4.5 E5：先静默重试一次，不引入新的等待策略）。
        self.retry_after: float | None = retry_after


def extract_json_object(raw: str) -> dict[str, Any]:
    """从模型输出里抠出第一个 JSON 对象。

    模型常见的三种脏输出：包 ```json 围栏、前后带解释性文字、被截断。
    围栏与前后文字可以救，截断救不了（直接抛，让上层重试）。
    """
    text = _FENCE_RE.sub("", raw or "").strip()
    if not text:
        raise ValueError("模型返回空内容")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"期望 JSON 对象，实际是 {type(parsed).__name__}")
    return parsed


def _parse_retry_after(raw: str | None) -> float | None:
    """解析 `Retry-After`（秒数或 HTTP-date）。

    解析失败一律返回 `None` 而不是抛异常：限流响应头格式不规范时，**不该让一个
    诊断字段把整轮对话打断**。当前没有消费者按它等待（见 `RateLimitedError.retry_after`）。
    """
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except (TypeError, ValueError):
        return None


class LLMClient:
    """薄封装。`enabled=False`（无 Key）时所有方法都应视为不可用，调用方走 mock。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return not self._settings.demo_mode

    @property
    def base_url(self) -> str:
        return self._settings.LLM_BASE_URL.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.LLM_API_KEY}",
            "Content-Type": "application/json",
        }

    async def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """带传输层重试的 POST。只对「网络/5xx」重试，4xx 直接失败（重试也不会好）。

        429 是唯一需要**换一种重试策略**的 4xx：它不是「参数错了」，而是「此刻太挤」。
        所以这里不重试、直接抛 `RateLimitedError`，交给产物分支在正文结束后串行重试 ——
        重试的位置比重试的次数更重要。
        """
        last_error: Exception | None = None
        for attempt, delay in enumerate(RETRY_BACKOFF[: self._settings.LLM_MAX_RETRIES]):
            if delay:
                import asyncio

                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(timeout=self._settings.LLM_TIMEOUT_SECONDS) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=self._headers(),
                    )
                if response.status_code == 429:
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    log_event(
                        "llm_call",
                        level=logging.WARNING,
                        attempt=attempt,
                        error="rate_limited",
                        model=payload.get("model"),
                    )
                    raise RateLimitedError(
                        f"模型服务限流（429）：{response.text[:200]}", retry_after=retry_after
                    )
                if response.status_code >= 500:
                    last_error = AppError(
                        ErrorCode.LLM_UPSTREAM_ERROR,
                        f"模型服务 {response.status_code}：{response.text[:200]}",
                    )
                    continue
                if response.status_code >= 400:
                    raise AppError(
                        ErrorCode.LLM_UPSTREAM_ERROR,
                        f"模型服务 {response.status_code}：{response.text[:200]}",
                    )
                return response.json()
            except httpx.TimeoutException as exc:
                last_error = AppError(ErrorCode.LLM_TIMEOUT, "模型响应超时")
                log_event("llm_call", level=logging.WARNING, attempt=attempt, error="timeout")
                last_error.__cause__ = exc
            except AppError:
                # RateLimitedError 也在这里被原样上抛（它是 AppError 的子类），
                # 这正是我们要的：429 绝不进入「网络异常重试」分支。
                raise
            except Exception as exc:  # 网络层异常
                last_error = AppError(ErrorCode.LLM_UPSTREAM_ERROR, f"模型调用失败：{exc}")
                log_event("llm_call", level=logging.WARNING, attempt=attempt, error=str(exc))

        raise last_error or AppError(ErrorCode.LLM_UPSTREAM_ERROR)

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.2,
        response_format: bool = True,
    ) -> dict[str, Any]:
        """请求一个 JSON 对象。JSON 解析失败会内部重试（最多 LLM_MAX_RETRIES 次）。

        默认模型是 `settings.artifact_model`（`LLM_MODEL_ARTIFACT`，空串则跟随
        `LLM_MODEL_GENERATOR`），与 `stream_text` 的默认值**故意不同**：正文要的是首字延迟，
        产物要的是「一次就过」。

        **旧的取舍「结构化 AST 用重档，准确率优先于延迟」已被实测推翻**，不要再照它改回去：
        同一批 11 个回合里，`deepseek-flash` 与 `deepseek-v4-pro` 的契约通过率相同
        （11 次调用 0 次重试、0 次降级 mock），而产物阶段耗时从 55.5s 降到 10.1s
        （`docs/increments/latency-profile.md` / `flash-quality.md`）。
        新的取舍是「**延迟优先 + 用 retry 率做护栏**」：
        - 重试一次就是一次完整调用（约 10s），所以 retry 率是唯一值得守的指标；
        - 若 retry 率破 PRD G1 阈值（阶段一 ≥2 次即否决），用 `LLM_MODEL_ARTIFACT`
          切回重档（或只对某个形态用 `LLM_MODEL_ARTIFACT_BY_TYPE` 切回），
          **不要改代码默认值**。

        显式传 `model=` 时以入参为准（`agent_engine` 用它做分形态覆盖）。
        """
        payload: dict[str, Any] = {
            "model": model or self._settings.artifact_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self._settings.LLM_MAX_RETRIES):
            body = await self._post_json(payload)
            try:
                content = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                last_error = AppError(ErrorCode.LLM_UPSTREAM_ERROR, "模型响应结构异常")
                log_event("llm_call", level=logging.WARNING, attempt=attempt, error=str(exc))
                continue
            try:
                return extract_json_object(str(content))
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                log_event("llm_call", level=logging.WARNING, attempt=attempt, error="bad_json")
                # 把「JSON 不合法」回灌给模型，比自己重试裸提示有效得多
                payload["messages"] = payload["messages"] + [
                    {"role": "assistant", "content": str(content)[:2000]},
                    {"role": "user", "content": f"上面的输出不是合法 JSON（{exc}）。只输出一个 JSON 对象，不要任何解释。"},
                ]
        raise AppError(
            ErrorCode.LLM_UPSTREAM_ERROR,
            f"模型未能返回合法 JSON：{last_error}",
        )

    async def stream_text(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.4,
        on_delta: Callable[[str], None] | None = None,
    ) -> AsyncIterator[str]:
        """流式产出正文增量。TTFT 直接取决于这个接口的首个 chunk 到达时间。

        **默认模型是 `LLM_MODEL_ROUTER`（轻量模型），不是 GENERATOR** —— 这是分层路由的
        落地位置，此前两边都取 GENERATOR，导致「配了 router 却从没用上」：
        - 正文是自然语言讲解，用户盯空屏超过 3 秒就以为卡死（P0-05① 的 TTFT 验收）。
          实测同一段讲解，重模型首 token 要 55 秒，轻量模型 1~2 秒，而学生根本看不出
          措辞上的差异 —— 用重模型换来的那点文采，代价是 55 倍的首字延迟，不划算。
        - 结构化 AST（`complete_json`）的默认档位另有配置（`LLM_MODEL_ARTIFACT`），
          与这里**故意不对称**：正文优化首字延迟，产物优化「一次就过」，
          两者的实测取舍见 `complete_json` 的 docstring。
        两个默认值因此不对称，改动任一默认值前请先想清楚这条取舍。
        """
        payload = {
            "model": model or self._settings.LLM_MODEL_ROUTER,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=self._settings.LLM_TIMEOUT_SECONDS) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=self._headers(),
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise AppError(
                            ErrorCode.LLM_UPSTREAM_ERROR,
                            f"模型服务 {response.status_code}：{body[:200]!r}",
                        )
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0]["delta"].get("content") or ""
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                        if delta:
                            if on_delta is not None:
                                on_delta(delta)
                            yield delta
        except httpx.TimeoutException as exc:
            raise AppError(ErrorCode.LLM_TIMEOUT, "模型响应超时") from exc
        except AppError:
            raise
        except Exception as exc:
            raise AppError(ErrorCode.LLM_UPSTREAM_ERROR, f"模型流式调用失败：{exc}") from exc


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_llm_client() -> None:
    """仅供测试。"""
    global _client
    _client = None


__all__ = ["LLMClient", "RETRY_BACKOFF", "extract_json_object", "get_llm_client", "reset_llm_client"]

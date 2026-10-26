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
        """带传输层重试的 POST。只对「网络/5xx」重试，4xx 直接失败（重试也不会好）。"""
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
        """请求一个 JSON 对象。JSON 解析失败会内部重试（最多 LLM_MAX_RETRIES 次）。"""
        payload: dict[str, Any] = {
            "model": model or self._settings.LLM_MODEL_GENERATOR,
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
        """流式产出正文增量。TTFT 直接取决于这个接口的首个 chunk 到达时间。"""
        payload = {
            "model": model or self._settings.LLM_MODEL_GENERATOR,
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

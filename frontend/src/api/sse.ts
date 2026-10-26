/**
 * POST + SSE 流式解析器。
 *
 * **为什么不用 `EventSource`**：它只支持 GET、不能带自定义 header / 请求体，
 * 而对话接口必须是 `POST /api/chat/stream` + JSON body。因此这里用
 * `fetch` + `ReadableStream` + `TextDecoder` 手写解析器（架构 §5.4）。
 *
 * 解析器要处理的四类现实问题：
 * 1. **跨 chunk 的半截事件**：网络按 16KB 左右切片，一个事件可能被切成两半，
 *    必须缓冲到出现空行（`\n\n`）才派发。
 * 2. **多字节字符被切断**：`TextDecoder.decode(value, {stream: true})` 才能正确续接
 *    中文/emoji，否则会出现乱码（U+FFFD）。
 * 3. **保活注释行**：后端每 15s 发 `: ping`，它不是事件，必须忽略，且**不能**因为
 *    它的空行而误派发一个空事件。
 * 4. **多个 `data:` 行**：SSE 规范要求按 `\n` 拼接后再解析。后端目前发单行紧凑
 *    JSON，但解析器必须支持多行，否则将来换代理/换实现就挂。
 */

import { API_BASE, ApiError, isSseDebugEnabled, toApiError } from "@/api/client";
import { isRecord } from "@/lib/jsonPointer";
import type { ChatRequestBody, ErrorCode } from "@/types/api";
import { normalizeErrorCode } from "@/types/api";
import type {
  ArtifactEventData,
  CitationsEventData,
  ErrorEventData,
  ModalityDecisionEventData,
  RetrievalEventData,
  SseEvent,
  SseEventName,
  TokenEventData,
  TurnEndEventData,
  TurnStartEventData,
} from "@/types/events";
import { isSseEventName } from "@/types/events";

// --------------------------------------------------------------------------- //
// 错误类型
// --------------------------------------------------------------------------- //
/**
 * 协议级异常：响应是 200 但帧内容不符合 §5.4（data 不是 JSON、缺 data 行等）。
 *
 * 与 `ApiError` 分开是因为处置方式不同：`ApiError` 提示用户「重试/换问题」，
 * 协议异常属于前后端版本不一致，应把 `raw` 打到控制台便于定位。
 */
export class SseProtocolError extends Error {
  /** 出问题的原始帧（截断到 500 字符，避免刷屏） */
  readonly raw: string;

  constructor(message: string, raw: string) {
    super(message);
    this.name = "SseProtocolError";
    this.raw = raw.length > 500 ? `${raw.slice(0, 500)}…` : raw;
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

// --------------------------------------------------------------------------- //
// 帧解析
// --------------------------------------------------------------------------- //
/** 一个原始 SSE 帧（尚未 JSON.parse）。 */
export interface RawSseFrame {
  /** `event:` 的值；缺省为 `"message"`（SSE 规范默认值） */
  event: string;
  /** 多个 `data:` 行按 `\n` 拼接后的结果 */
  data: string;
  /** `id:` 的值，用于断线续传（本项目暂未使用，但解析保留） */
  id: string | null;
  /** `retry:` 的值（毫秒） */
  retry: number | null;
}

/**
 * 解析一个「事件块」（已按空行切好）。
 *
 * 返回 `null` 表示这个块不该派发：只有注释（`: ping`）或只有 `id:` 的块。
 */
export function parseSseBlock(block: string, lastEventId: string | null = null): RawSseFrame | null {
  const dataLines: string[] = [];
  let event = "";
  let id: string | null = lastEventId;
  let retry: number | null = null;

  for (const rawLine of block.split("\n")) {
    // 兜底：万一上游用 \r 单独结尾，这里再剥一次
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (line.length === 0) {
      continue;
    }
    if (line.startsWith(":")) {
      // 注释行 / 心跳，整行丢弃（不能因为它的存在就派发空事件）
      continue;
    }
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) {
      value = value.slice(1);
    }

    switch (field) {
      case "event":
        event = value;
        break;
      case "data":
        dataLines.push(value);
        break;
      case "id":
        id = value;
        break;
      case "retry": {
        const parsed = Number.parseInt(value, 10);
        retry = Number.isNaN(parsed) ? null : parsed;
        break;
      }
      default:
        // 未知字段按规范忽略
        break;
    }
  }

  if (dataLines.length === 0) {
    return null;
  }

  return {
    event: event.length > 0 ? event : "message",
    data: dataLines.join("\n"),
    id,
    retry,
  };
}

/**
 * 增量 SSE 解析器：喂 chunk，吐完整帧。
 *
 * 用法：`push(chunk)` 拿到 0..N 个帧；流结束时调用 `flush()` 收尾。
 */
export class SseParser {
  private buffer = "";
  private lastEventId: string | null = null;

  /** 当前尚未成帧的残余（调试用）。 */
  get pending(): string {
    return this.buffer;
  }

  /** 喂一个文本分片，返回本次能成帧的所有事件。 */
  push(chunk: string): RawSseFrame[] {
    this.buffer += chunk;
    return this.drain(false);
  }

  /** 流结束时调用：把末尾没有空行收尾的残帧也解析出来。 */
  flush(): RawSseFrame[] {
    return this.drain(true);
  }

  private drain(isFinal: boolean): RawSseFrame[] {
    // 统一换行符。分片的 \r 与 \n 可能落在两个 chunk 里，
    // 因此每次 push 都对整个 buffer 做一次替换（buffer 只存未完成帧，长度很小）。
    this.buffer = this.buffer.replace(/\r\n/g, "\n");

    const frames: RawSseFrame[] = [];
    let separator = this.buffer.indexOf("\n\n");
    while (separator !== -1) {
      const block = this.buffer.slice(0, separator);
      this.buffer = this.buffer.slice(separator + 2);
      this.collect(block, frames);
      separator = this.buffer.indexOf("\n\n");
    }

    if (isFinal) {
      const rest = this.buffer;
      this.buffer = "";
      if (rest.trim().length > 0) {
        this.collect(rest, frames);
      }
    }

    return frames;
  }

  private collect(block: string, out: RawSseFrame[]): void {
    const frame = parseSseBlock(block, this.lastEventId);
    if (frame === null) {
      return;
    }
    if (frame.id !== null) {
      this.lastEventId = frame.id;
    }
    out.push(frame);
  }
}

// --------------------------------------------------------------------------- //
// 帧 → 类型化事件
// --------------------------------------------------------------------------- //
/**
 * 唯一断言点：`data` 的形状由后端 Pydantic 事件模型（`schemas/chat.py`）保证。
 * 前端不再写一份运行时 schema，否则等于把 §5.4 的字段表维护两遍。
 */
function asEventData<T>(value: unknown): T {
  return value as T;
}

/**
 * 把一个原始帧转成类型化事件；未知事件名返回 `null`（向前兼容）。
 *
 * 帧名合法但 `data` 不是 JSON → 抛 `SseProtocolError`：这是契约被破坏，
 * 静默跳过会让 UI 卡在「正在思考」而没有任何线索。
 */
export function decodeFrame(frame: RawSseFrame): SseEvent | null {
  if (!isSseEventName(frame.event)) {
    return null;
  }
  const name: SseEventName = frame.event;

  let parsed: unknown;
  try {
    parsed = JSON.parse(frame.data);
  } catch {
    throw new SseProtocolError(`事件 ${name} 的 data 不是合法 JSON`, frame.data);
  }
  if (!isRecord(parsed)) {
    throw new SseProtocolError(`事件 ${name} 的 data 不是 JSON 对象`, frame.data);
  }

  switch (name) {
    case "turn_start":
      return { type: "turn_start", data: asEventData<TurnStartEventData>(parsed) };
    case "retrieval":
      return { type: "retrieval", data: asEventData<RetrievalEventData>(parsed) };
    case "token":
      return { type: "token", data: asEventData<TokenEventData>(parsed) };
    case "modality_decision":
      return { type: "modality_decision", data: asEventData<ModalityDecisionEventData>(parsed) };
    case "artifact":
      return { type: "artifact", data: asEventData<ArtifactEventData>(parsed) };
    case "citations":
      return { type: "citations", data: asEventData<CitationsEventData>(parsed) };
    case "error":
      return { type: "error", data: asEventData<ErrorEventData>(parsed) };
    case "turn_end":
      return { type: "turn_end", data: asEventData<TurnEndEventData>(parsed) };
    default:
      return null;
  }
}

// --------------------------------------------------------------------------- //
// 流式请求
// --------------------------------------------------------------------------- //
/** 便于测试注入：与 `fetch` 兼容的最小子集。 */
export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface ChatStreamOptions {
  /** 中断信号：组件卸载或用户点「停止生成」时 abort */
  signal?: AbortSignal;
  /** 覆盖 API 前缀（默认取 `VITE_API_BASE`） */
  apiBase?: string;
  /** 注入 fetch 实现（单测用） */
  fetchImpl?: FetchLike;
  /** 原始帧回调（调试 / 打点），在解码前触发 */
  onRawFrame?: (frame: RawSseFrame) => void;
}

/** 把 HTTP 错误响应体解析成 `ApiError`（后端失败态也是统一包装）。 */
async function buildHttpError(response: Response): Promise<ApiError> {
  let text = "";
  try {
    text = await response.text();
  } catch {
    text = "";
  }

  let code: ErrorCode = "INTERNAL_ERROR";
  let message = `HTTP ${response.status} ${response.statusText}`;
  let requestId = "";

  if (text.trim().length > 0) {
    try {
      const parsed: unknown = JSON.parse(text);
      if (isRecord(parsed)) {
        if (typeof parsed["code"] === "string") {
          code = normalizeErrorCode(parsed["code"]);
        }
        if (typeof parsed["message"] === "string" && parsed["message"].length > 0) {
          message = parsed["message"];
        }
        if (typeof parsed["request_id"] === "string") {
          requestId = parsed["request_id"];
        }
      }
    } catch {
      // 错误体不是 JSON（例如 nginx 的 HTML 502 页），保留 HTTP 文案
    }
  }

  return new ApiError(code, message, response.status, requestId);
}

function resolveStreamUrl(apiBase: string | undefined): string {
  const base = (apiBase ?? API_BASE).replace(/\/+$/, "");
  return `${base}/chat/stream`;
}

/**
 * 异步生成器：逐个 yield 类型化事件。
 *
 * 收流即停：`turn_end` 是后端保证的最后一个事件，收到它立即结束迭代并取消 reader，
 * 避免代理未及时关闭连接时 UI 一直挂着「生成中」。
 *
 * 主动 abort 不算异常（`return` 静默结束），让调用方无需区分「取消」与「失败」。
 */
export async function* iterateChatStream(
  body: ChatRequestBody,
  options: ChatStreamOptions = {},
): AsyncGenerator<SseEvent, void, void> {
  const fetchImpl: FetchLike = options.fetchImpl ?? ((input, init) => fetch(input, init));
  const debug = isSseDebugEnabled();

  let response: Response;
  try {
    response = await fetchImpl(resolveStreamUrl(options.apiBase), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(body),
      signal: options.signal ?? null,
    });
  } catch (error) {
    if (isAbortError(error)) {
      return;
    }
    throw toApiError(error);
  }

  if (!response.ok) {
    throw await buildHttpError(response);
  }

  const stream = response.body;
  if (stream === null) {
    throw new SseProtocolError("响应没有可读流（response.body 为 null）", "");
  }

  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8");
  const parser = new SseParser();
  let finished = false;

  try {
    while (!finished) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      if (value === undefined) {
        continue;
      }
      const text = decoder.decode(value, { stream: true });
      for (const frame of parser.push(text)) {
        if (debug) {
          console.debug("[sse]", frame.event, frame.data);
        }
        options.onRawFrame?.(frame);
        const event = decodeFrame(frame);
        if (event === null) {
          continue;
        }
        yield event;
        if (event.type === "turn_end") {
          finished = true;
          break;
        }
      }
    }

    if (!finished) {
      const tail = decoder.decode();
      const tailFrames =
        tail.length > 0 ? [...parser.push(tail), ...parser.flush()] : parser.flush();
      for (const frame of tailFrames) {
        if (debug) {
          console.debug("[sse][tail]", frame.event, frame.data);
        }
        options.onRawFrame?.(frame);
        const event = decodeFrame(frame);
        if (event === null) {
          continue;
        }
        yield event;
        if (event.type === "turn_end") {
          finished = true;
          break;
        }
      }
    }
  } catch (error) {
    if (isAbortError(error)) {
      return;
    }
    throw error;
  } finally {
    try {
      await reader.cancel();
    } catch {
      // reader 已关闭 / 已取消，忽略
    }
  }
}

/**
 * 回调式包装（`useChatStream` 更常用这种写法）。
 *
 * 内部仍是 `iterateChatStream`，两者行为完全一致，只是控制流风格不同。
 */
export async function streamChat(
  body: ChatRequestBody,
  onEvent: (event: SseEvent) => void,
  options: ChatStreamOptions = {},
): Promise<void> {
  for await (const event of iterateChatStream(body, options)) {
    onEvent(event);
  }
}

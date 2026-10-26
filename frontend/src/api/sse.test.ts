/**
 * `api/sse.ts` 解析器自验。
 *
 * 覆盖四个现实故障点（见 sse.ts 文件头）：
 * 1. 跨 chunk 的半截事件（逐字符切分，最苛刻的切法）
 * 2. 保活注释行 `: ping` 不得被当成事件
 * 3. 多个 `data:` 行按 `\n` 拼接
 * 4. 未知事件名向前兼容、坏 JSON 抛协议异常
 *
 * 运行：`npm run verify:sse`
 */

import { describe, expect, it } from "vitest";

import {
  SseParser,
  SseProtocolError,
  decodeFrame,
  iterateChatStream,
  parseSseBlock,
} from "@/api/sse";
import { ApiError } from "@/api/client";
import type { ChatRequestBody } from "@/types/api";
import type { SseEvent } from "@/types/events";

// --------------------------------------------------------------------------- //
// 构造假流
// --------------------------------------------------------------------------- //
interface FrameSpec {
  event: string;
  data: Record<string, unknown>;
}

function frameToText(frame: FrameSpec): string {
  return `event: ${frame.event}\ndata: ${JSON.stringify(frame.data)}\n\n`;
}

const TURN_START: FrameSpec = {
  event: "turn_start",
  data: {
    turn_id: "turn-1",
    workspace_id: "ws-1",
    mode: "syllabus_strict",
    demo_mode: true,
    has_anchor: false,
    artifact_type_hint: null,
  },
};

const RETRIEVAL: FrameSpec = {
  event: "retrieval",
  data: {
    grader_status: "SYLLABUS_MATCH",
    section_scope: "4.1",
    top_score: 0.82,
    chunks: [
      {
        chunk_id: "c1",
        section_code: "4.1",
        section_title: "马来亚联邦",
        page_start: 94,
        page_end: 96,
        score: 0.82,
        snippet: "……",
      },
    ],
  },
};

const MODALITY: FrameSpec = {
  event: "modality_decision",
  data: {
    artifact_type: "ppt",
    action: "create_artifact",
    reason: "query 命中「梳理」→ PPT",
    user_override: false,
  },
};

const TOKEN_1: FrameSpec = { event: "token", data: { delta: "马来亚联邦" } };
const TOKEN_2: FrameSpec = { event: "token", data: { delta: "成立于 1948 年[p.94]" } };

const CITATIONS: FrameSpec = {
  event: "citations",
  data: { citations: [{ label: "Bab 4.1", section_code: "4.1", page_start: 94, page_end: 96 }] },
};

const TURN_END: FrameSpec = {
  event: "turn_end",
  data: { turn_id: "turn-1", duration_ms: 1234, ttft_ms: 180, version: 1, demo_mode: true },
};

const ALL_FRAMES: FrameSpec[] = [
  TURN_START,
  RETRIEVAL,
  MODALITY,
  TOKEN_1,
  TOKEN_2,
  CITATIONS,
  TURN_END,
];

/** 完整流：帧之间插入 `: ping` 注释行（后端每 15s 一次保活） */
function buildStreamText(): string {
  return ALL_FRAMES.map((frame) => `: ping\n\n${frameToText(frame)}`).join("");
}

function parseAll(chunks: string[]): SseEvent[] {
  const parser = new SseParser();
  const events: SseEvent[] = [];
  const push = (text: string): void => {
    for (const frame of parser.push(text)) {
      const event = decodeFrame(frame);
      if (event !== null) {
        events.push(event);
      }
    }
  };
  for (const chunk of chunks) {
    push(chunk);
  }
  for (const frame of parser.flush()) {
    const event = decodeFrame(frame);
    if (event !== null) {
      events.push(event);
    }
  }
  return events;
}

function splitBySize(text: string, size: number): string[] {
  const chunks: string[] = [];
  for (let index = 0; index < text.length; index += size) {
    chunks.push(text.slice(index, index + size));
  }
  return chunks;
}

// --------------------------------------------------------------------------- //
// 解析器
// --------------------------------------------------------------------------- //
describe("SseParser", () => {
  it("整段一次性喂入能解析出全部事件", () => {
    const events = parseAll([buildStreamText()]);
    expect(events.map((event) => event.type)).toEqual([
      "turn_start",
      "retrieval",
      "modality_decision",
      "token",
      "token",
      "citations",
      "turn_end",
    ]);
  });

  it("逐字符切分（最苛刻的跨 chunk 场景）仍得到相同事件序列", () => {
    const text = buildStreamText();
    const expected = parseAll([text]);
    for (const size of [1, 2, 3, 5, 7, 11, 64]) {
      const actual = parseAll(splitBySize(text, size));
      expect(actual.length, `chunkSize=${size}`).toBe(expected.length);
      expect(actual.map((event) => event.type), `chunkSize=${size}`).toEqual(
        expected.map((event) => event.type),
      );
    }
  });

  it("只有 ping 注释行时不产生任何事件", () => {
    expect(parseAll([": ping\n\n", ": ping\n\n"])).toHaveLength(0);
  });

  it("忽略注释行且不因它的空行误派发空事件", () => {
    const parser = new SseParser();
    const frames = parser.push(": ping\n\n");
    expect(frames).toHaveLength(0);
    expect(parser.pending).toBe("");
  });

  it("多个 data: 行按 \\n 拼接后解析", () => {
    const parser = new SseParser();
    const frames = parser.push('event: token\ndata: {"delta":\ndata: "多行"}\n\n');
    expect(frames).toHaveLength(1);
    const event = frames[0] === undefined ? null : decodeFrame(frames[0]);
    expect(event).not.toBeNull();
    // 两个 data 行以 \n 拼接成 {"delta":\n"多行"}，换行落在 JSON 的空白位，值仍是「多行」
    expect(event !== null && event.type === "token" ? event.data.delta : "").toBe("多行");
  });

  it("\\r\\n 换行的流也能解析（代理改写场景）", () => {
    const text = `event: token\r\ndata: {"delta":"crlf"}\r\n\r\n`;
    const events = parseAll([text]);
    expect(events).toHaveLength(1);
    expect(events[0]?.type).toBe("token");
  });

  it("flush() 能收尾没有尾随空行的残帧", () => {
    const parser = new SseParser();
    expect(parser.push('event: token\ndata: {"delta":"尾部"}')).toHaveLength(0);
    const frames = parser.flush();
    expect(frames).toHaveLength(1);
    const event = frames[0] === undefined ? null : decodeFrame(frames[0]);
    expect(event?.type).toBe("token");
  });

  it("未知事件名向前兼容（返回 null 而不抛错）", () => {
    const block = 'event: future_event\ndata: {"x":1}';
    const frame = parseSseBlock(block);
    expect(frame).not.toBeNull();
    expect(frame === null ? null : decodeFrame(frame)).toBeNull();
  });

  it("data 不是合法 JSON 时抛 SseProtocolError", () => {
    const frame = parseSseBlock('event: token\ndata: {oops}');
    expect(frame).not.toBeNull();
    expect(() => (frame === null ? null : decodeFrame(frame))).toThrow(SseProtocolError);
  });

  it("保留 id / retry 字段（断线续传预留）", () => {
    const frame = parseSseBlock('id: 42\nretry: 3000\nevent: token\ndata: {"delta":"x"}');
    expect(frame?.id).toBe("42");
    expect(frame?.retry).toBe(3000);
  });
});

// --------------------------------------------------------------------------- //
// 流式请求
// --------------------------------------------------------------------------- //
function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

const REQUEST: ChatRequestBody = {
  workspace_id: "ws-1",
  query: "4.1 讲了什么",
  mode: "syllabus_strict",
};

async function collect(chunks: string[]): Promise<SseEvent[]> {
  const events: SseEvent[] = [];
  for await (const event of iterateChatStream(REQUEST, {
    fetchImpl: () => Promise.resolve(streamResponse(chunks)),
  })) {
    events.push(event);
  }
  return events;
}

// --------------------------------------------------------------------------- //
// 真实后端抓包回归
// --------------------------------------------------------------------------- //
/**
 * 2026-10-26 从本地后端 `POST /api/chat/stream` 原样抓取的帧（token 正文截断）。
 *
 * 作用：防止「解析器按自己想象的格式写对了，但真实服务端格式不同」这类事故。
 * 若后端改了事件字段名，这条用例会先挂，而不是等到联调时白屏。
 */
const REAL_CAPTURE = [
  "event: turn_start",
  'data: {"artifact_type_hint":null,"demo_mode":true,"has_anchor":false,"mode":"syllabus_strict","turn_id":"10a8c636-5c78-428e-ad8d-dcfaf81e199c","workspace_id":"e24bf248-f46c-42fb-9cff-dc8c701fa08b"}',
  "",
  "event: retrieval",
  'data: {"chunks":[],"grader_status":"NO_MATCH","section_scope":"4.1","top_score":0.0}',
  "",
  "event: modality_decision",
  'data: {"action":"none","artifact_type":"none","reason":"问句命中「梳理」「要点」，属于离散有序、需要逐条阅读的信息 → PPT 课件","user_override":false}',
  "",
  "event: token",
  'data: {"delta":"该问题课本未覆盖"}',
  "",
  "event: citations",
  'data: {"citations":[]}',
  "",
  "event: turn_end",
  'data: {"demo_mode":true,"duration_ms":812,"ttft_ms":96,"turn_id":"10a8c636-5c78-428e-ad8d-dcfaf81e199c","version":null}',
  "",
  "",
].join("\n");

describe("真实后端抓包", () => {
  it("原样帧能解析出 6 个事件且字段名一致", () => {
    const events = parseAll([REAL_CAPTURE]);
    expect(events.map((event) => event.type)).toEqual([
      "turn_start",
      "retrieval",
      "modality_decision",
      "token",
      "citations",
      "turn_end",
    ]);

    const start = events[0];
    expect(start?.type === "turn_start" ? start.data.demo_mode : null).toBe(true);
    expect(start?.type === "turn_start" ? start.data.artifact_type_hint : "x").toBeNull();

    const retrieval = events[1];
    expect(retrieval?.type === "retrieval" ? retrieval.data.grader_status : null).toBe("NO_MATCH");

    const end = events[5];
    expect(end?.type === "turn_end" ? end.data.version : 0).toBeNull();
    expect(end?.type === "turn_end" ? end.data.ttft_ms : 0).toBe(96);
  });

  it("逐字符切分真实抓包同样正确", () => {
    const events = parseAll(splitBySize(REAL_CAPTURE, 1));
    expect(events).toHaveLength(6);
  });
});

describe("iterateChatStream", () => {
  it("分片到达的假流能产出完整事件序列", async () => {
    const text = buildStreamText();
    const events = await collect(splitBySize(text, 9));
    expect(events).toHaveLength(7);
    expect(events[0]?.type).toBe("turn_start");
    expect(events[6]?.type).toBe("turn_end");

    const firstToken = events[3];
    expect(firstToken?.type === "token" ? firstToken.data.delta : "").toBe("马来亚联邦");
  });

  it("收到 turn_end 即结束迭代（不依赖服务端关连接）", async () => {
    const encoder = new TextEncoder();
    // 故意在 turn_end 之后不再 close，模拟代理迟迟不关连接
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(buildStreamText()));
      },
    });
    const events: SseEvent[] = [];
    for await (const event of iterateChatStream(REQUEST, {
      fetchImpl: () => Promise.resolve(new Response(stream, { status: 200 })),
    })) {
      events.push(event);
    }
    expect(events.at(-1)?.type).toBe("turn_end");
  });

  it("HTTP 400 的错误码被正确解析", async () => {
    const body = JSON.stringify({
      code: "REQUEST_INVALID",
      data: null,
      message: "query 不能为空",
      request_id: "req-1",
    });
    const iterator = iterateChatStream(REQUEST, {
      fetchImpl: () =>
        Promise.resolve(
          new Response(body, { status: 400, headers: { "content-type": "application/json" } }),
        ),
    });
    try {
      await iterator.next();
      expect.unreachable("应当抛出 ApiError");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      if (error instanceof ApiError) {
        expect(error.code).toBe("REQUEST_INVALID");
        expect(error.httpStatus).toBe(400);
        expect(error.requestId).toBe("req-1");
        expect(error.displayMessage).toBe("query 不能为空");
      }
    }
  });

  it("主动 abort 时静默结束，不抛异常", async () => {
    const controller = new AbortController();
    const events: SseEvent[] = [];
    const iterator = iterateChatStream(REQUEST, {
      signal: controller.signal,
      fetchImpl: () => {
        controller.abort();
        return Promise.reject(new DOMException("aborted", "AbortError"));
      },
    });
    for await (const event of iterator) {
      events.push(event);
    }
    expect(events).toHaveLength(0);
  });
});

/**
 * 文本里的「页码引用」解析与「出处字符串」解析。
 *
 * 两件事都在这里做，因为它们是同一个问题的两面：
 * - 渲染侧：把 `[p.112]`、`（p.76-77）` 这类行内标记变成可点击 chip（P0-04②「引用可回溯」）。
 * - 交互侧：把 `source_ref`（如 `Form 4 Sejarah, Bab 4.1, p.94`）解析成
 *   `{section_code, page}`，让「点画布 → 锚点」能带上 `section_code`。
 *   这一步不做，后端就无法把检索锁在该小节，引用页码会跑到同章其它小节。
 */

/** 行内页码标记：`[p.112]` `[p.94-96]` `（p.76）` `(p.76-77)` `p.112` */
const INLINE_PAGE_REF_RE =
  /[[（(]\s*p{1,2}\.\s*(\d{1,4})(?:\s*[-–—~]\s*(\d{1,4}))?\s*[\]）)]/gi;

export interface PageRefToken {
  kind: "pageRef";
  /** 原始文本，用于无 section 时兜底显示 */
  raw: string;
  pageStart: number;
  pageEnd: number;
}

export interface TextToken {
  kind: "text";
  text: string;
}

export type InlineToken = TextToken | PageRefToken;

/**
 * 把一段纯文本切成「普通文本 / 页码引用」两类片段。
 *
 * 只处理纯文本节点：Markdown 结构（列表、表格）由 react-markdown 负责，
 * 这里不解析 Markdown 语法，避免两套解析器打架。
 */
export function splitPageRefs(text: string): InlineToken[] {
  const tokens: InlineToken[] = [];
  let cursor = 0;
  INLINE_PAGE_REF_RE.lastIndex = 0;

  let match = INLINE_PAGE_REF_RE.exec(text);
  while (match !== null) {
    const start = match.index;
    if (start > cursor) {
      tokens.push({ kind: "text", text: text.slice(cursor, start) });
    }
    const startPage = Number.parseInt(match[1] ?? "0", 10);
    const endRaw = match[2];
    const endPage = endRaw === undefined ? startPage : Number.parseInt(endRaw, 10);
    tokens.push({
      kind: "pageRef",
      raw: match[0],
      pageStart: Math.min(startPage, endPage),
      pageEnd: Math.max(startPage, endPage),
    });
    cursor = start + match[0].length;
    match = INLINE_PAGE_REF_RE.exec(text);
  }

  if (cursor < text.length) {
    tokens.push({ kind: "text", text: text.slice(cursor) });
  }
  return tokens;
}

/** 文本中是否含有页码引用（用于决定是否值得走分词渲染）。 */
export function hasPageRef(text: string): boolean {
  INLINE_PAGE_REF_RE.lastIndex = 0;
  return INLINE_PAGE_REF_RE.test(text);
}

export interface SourceRefInfo {
  /** 章节号，如 `4.1` / `1.1.3`；无法识别时为 null */
  section_code: string | null;
  /** 起始页码；无法识别时为 null */
  page: number | null;
  /** 是否为超纲来源（`Extended Knowledge`） */
  isExtended: boolean;
}

const SECTION_RE = /(?:bab|chapter|section|topik|单元|章|节)\s*([0-9]+(?:\.[0-9]+)*)/i;
const BARE_SECTION_RE = /\b(\d+\.\d+(?:\.\d+)*)\b/;
const PAGE_RE = /p{1,2}\.\s*(\d{1,4})/i;

/**
 * 解析 `source_ref`。
 *
 * 兼容后端 mock 与真实模型可能产出的几种写法：
 * - `Form 4 Sejarah, Bab 4.1, p.94`
 * - `Bab 1.1.3 · p.5`
 * - `1.1 Pengertian Sejarah（p.5）`
 * - `Extended Knowledge`（超纲）
 */
export function parseSourceRef(sourceRef: string): SourceRefInfo {
  const text = sourceRef.trim();
  const isExtended = /extended\s*knowledge/i.test(text);

  const sectionMatch = SECTION_RE.exec(text) ?? BARE_SECTION_RE.exec(text);
  const section_code = sectionMatch === null ? null : (sectionMatch[1] ?? null);

  const pageMatch = PAGE_RE.exec(text);
  const page = pageMatch === null ? null : Number.parseInt(pageMatch[1] ?? "", 10);

  return {
    section_code,
    page: page !== null && Number.isFinite(page) ? page : null,
    isExtended,
  };
}

/** 展示用页码范围：`p.94` / `p.94-96`。 */
export function formatPageRange(pageStart: number, pageEnd: number): string {
  return pageStart === pageEnd ? `p.${pageStart}` : `p.${pageStart}-${pageEnd}`;
}

/**
 * 「深度拓展」段落识别。
 *
 * 后端 `hybrid_extended` 模式下用 `> **深度拓展（超出课本范围）**` 包裹拓展内容，
 * 前端必须把它渲染成视觉上明显不同的区块（PRD 要求「防止考试答偏」）。
 */
export function isExtendedHeading(text: string): boolean {
  return /深度拓展|超出课本范围|extended\s*context/i.test(text);
}

/**
 * 规范化 JSON 指纹（sha256）。
 *
 * 用途：证明「patch 之后只有目标子树变化，其余节点字节级不变」（P0-10②）。
 *
 * ⚠️ **只在同一语言侧自比较**（前端 before/after），**绝不要拿去和后端指纹比对**：
 * Python `json.dumps(1.0)` 得到 `"1.0"`，JS `JSON.stringify(1.0)` 得到 `"1"`，
 * 跨语言必然不等。架构 §5.2 已明确这一点 —— 两侧各自算、各自比。
 *
 * 规范化规则（与后端 `canonical_json` 语义对齐，仅用于本侧一致性）：
 * - 对象键排序（消除 dict 插入顺序差异）
 * - 紧凑分隔符 `,` `:`（消除空格差异）
 * - 非 ASCII 原样输出（`JSON.stringify` 天然如此，不转 `\uXXXX`）
 * - `undefined` 值跳过（与 `JSON.stringify` 一致），非有限数字按 `null`
 */

import {
  isAncestorOrSelf,
  isRecord,
  isUnknownArray,
  resolvePointer,
} from "@/lib/jsonPointer";

/** 确定性序列化：同一内容任意键序 → 同一字符串。 */
export function canonicalize(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "number") {
    // NaN / Infinity 在 JSON 里没有表示，统一按 null 处理（与 JSON.stringify 一致）
    return Number.isFinite(value) ? JSON.stringify(value) : "null";
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (typeof value === "string") {
    return JSON.stringify(value);
  }
  if (value === undefined) {
    return "null";
  }
  if (isUnknownArray(value)) {
    return `[${value.map((item) => canonicalize(item)).join(",")}]`;
  }
  if (isRecord(value)) {
    const parts: string[] = [];
    for (const key of Object.keys(value).sort()) {
      const item = value[key];
      if (item === undefined) {
        continue;
      }
      parts.push(`${JSON.stringify(key)}:${canonicalize(item)}`);
    }
    return `{${parts.join(",")}}`;
  }
  // 函数 / Symbol / BigInt 不该出现在 AST 里，出现即视为内容异常
  return "null";
}

const textEncoder = new TextEncoder();

function toHex(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let hex = "";
  for (const byte of bytes) {
    hex += byte.toString(16).padStart(2, "0");
  }
  return hex;
}

/** sha256(text) → 小写 hex。依赖 Web Crypto（浏览器与 Node 18+ 均可用）。 */
export async function sha256Hex(text: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (subtle === undefined) {
    // 非 https/localhost 环境下 crypto.subtle 不可用，明确报错而不是静默降级
    throw new Error("当前环境缺少 Web Crypto（crypto.subtle），无法计算指纹");
  }
  const digest = await subtle.digest("SHA-256", textEncoder.encode(text));
  return toHex(digest);
}

/** 规范化 JSON 的 sha256 指纹。 */
export async function fingerprint(value: unknown): Promise<string> {
  return sha256Hex(canonicalize(value));
}

/**
 * 全路径指纹表：`path → sha256(canonical(subtree))`，根路径为 `""`。
 *
 * 复杂度 O(节点数 × 子树大小)，对 AST 这种「深 ≤ 6、总大小 ≤ 几十 KB」的结构完全够用；
 * 换来的是实现极简、无需维护第二套「增量哈希」定义（两套定义必然对不齐）。
 */
export async function fingerprintIndex(root: unknown): Promise<Map<string, string>> {
  const index = new Map<string, string>();

  async function walk(value: unknown, path: string): Promise<void> {
    index.set(path, await fingerprint(value));
    if (isUnknownArray(value)) {
      for (const [position, item] of value.entries()) {
        await walk(item, `${path}/${position}`);
      }
      return;
    }
    if (isRecord(value)) {
      for (const key of Object.keys(value)) {
        await walk(value[key], `${path}/${key}`);
      }
    }
  }

  await walk(root, "");
  return index;
}

// --------------------------------------------------------------------------- //
// 差异描述
// --------------------------------------------------------------------------- //
function childPaths(value: unknown, path: string): string[] {
  if (isUnknownArray(value)) {
    return value.map((_item, index) => `${path}/${index}`);
  }
  if (isRecord(value)) {
    return Object.keys(value).map((key) => `${path}/${key}`);
  }
  return [];
}

/**
 * 把一个「变了」的路径收敛成**人类可读的最小描述**。
 *
 * 规则：若该节点的**所有**直接子节点都变了，就报该节点本身（整页重做 → 报 `/slides/1`）；
 * 否则继续向下收敛到真正变了的子节点（只改标题 → 报 `/slides/1/title`）。
 * 这样 UI 拿到的路径既能直接用于高亮，也不会退化成「每个叶子字段一条」的噪音。
 */
function describeChange(
  before: unknown,
  after: unknown,
  changed: ReadonlySet<string>,
  path: string,
  out: string[],
): void {
  const children = new Set<string>([
    ...childPaths(resolvePointer(before, path), path),
    ...childPaths(resolvePointer(after, path), path),
  ]);

  if (children.size === 0) {
    out.push(path);
    return;
  }

  const allChildrenChanged = Array.from(children).every((child) => changed.has(child));
  if (allChildrenChanged) {
    out.push(path);
    return;
  }

  for (const child of Array.from(children).sort()) {
    if (changed.has(child)) {
      describeChange(before, after, changed, child, out);
    }
  }
}

function changedPathSet(
  beforeIndex: ReadonlyMap<string, string>,
  afterIndex: ReadonlyMap<string, string>,
): Set<string> {
  const changed = new Set<string>();
  for (const [path, hash] of beforeIndex) {
    if (afterIndex.get(path) !== hash) {
      changed.add(path);
    }
  }
  for (const path of afterIndex.keys()) {
    if (!beforeIndex.has(path)) {
      changed.add(path);
    }
  }
  return changed;
}

/**
 * 两棵树的差异路径（最小描述，已剔除「仅因子孙变化而变」的祖先）。
 *
 * 返回值可直接用于：① 只重渲染被改动的子树；② 与后端 `patch_applied` 日志的
 * `changed_paths` 交叉核对。
 */
export async function changedPointerPaths(before: unknown, after: unknown): Promise<string[]> {
  const beforeIndex = await fingerprintIndex(before);
  const afterIndex = await fingerprintIndex(after);
  const changed = changedPathSet(beforeIndex, afterIndex);

  // 从根的直接子节点起步：根必然「变了」，报它没有信息量
  const roots = new Set<string>([
    ...childPaths(before, ""),
    ...childPaths(after, ""),
  ]);

  const out: string[] = [];
  for (const root of Array.from(roots).sort()) {
    if (changed.has(root)) {
      describeChange(before, after, changed, root, out);
    }
  }
  return out.sort();
}

/** 便于排查的差异明细（added / removed / changed），不参与断言逻辑。 */
export interface FingerprintDiff {
  added: string[];
  removed: string[];
  changed: string[];
}

export async function diffFingerprints(before: unknown, after: unknown): Promise<FingerprintDiff> {
  const beforeIndex = await fingerprintIndex(before);
  const afterIndex = await fingerprintIndex(after);
  const added: string[] = [];
  const removed: string[] = [];
  const changed: string[] = [];

  for (const [path, hash] of afterIndex) {
    const previous = beforeIndex.get(path);
    if (previous === undefined) {
      added.push(path);
    } else if (previous !== hash) {
      changed.push(path);
    }
  }
  for (const path of beforeIndex.keys()) {
    if (!afterIndex.has(path)) {
      removed.push(path);
    }
  }

  return { added: added.sort(), removed: removed.sort(), changed: changed.sort() };
}

/**
 * 断言「除 `allowedPaths` 及其祖先/后代外，其余节点指纹完全一致」。
 *
 * 与后端 `tests/test_patch_engine.py` 的断言逻辑同构：返回不一致的路径，
 * 空数组即通过。`useArtifactPatch`（B5）在每次 patch 后调用它做前端自检。
 */
export async function findUnexpectedChanges(
  before: unknown,
  after: unknown,
  allowedPaths: readonly string[],
): Promise<string[]> {
  const beforeIndex = await fingerprintIndex(before);
  const afterIndex = await fingerprintIndex(after);
  const changed = changedPathSet(beforeIndex, afterIndex);

  const unexpected: string[] = [];
  for (const path of changed) {
    if (path === "") {
      // 根必然变化（子孙变了它也变），不参与判定
      continue;
    }
    if (isAllowed(path, allowedPaths)) {
      continue;
    }
    unexpected.push(path);
  }

  // 去掉「已有意外祖先」的项，再从最上层收敛描述
  const minimal = unexpected.filter(
    (path) => !unexpected.some((other) => other !== path && isAncestorOrSelf(other, path)),
  );

  const out: string[] = [];
  for (const path of minimal.sort()) {
    describeChange(before, after, changed, path, out);
  }
  return out.sort();
}

/**
 * 路径是否落在「允许改动」的范围内。
 *
 * 两个方向都算允许：
 * - `allowed` 是 `path` 的祖先 → 被锚定子树内部的变化，正常；
 * - `path` 是 `allowed` 的祖先 → 容器节点因子孙变化而变，属必然结果。
 * 除此之外的任何差异都是「改了不该改的地方」，必须报出来。
 */
function isAllowed(path: string, allowedPaths: readonly string[]): boolean {
  return allowedPaths.some(
    (allowed) => isAncestorOrSelf(allowed, path) || isAncestorOrSelf(path, allowed),
  );
}

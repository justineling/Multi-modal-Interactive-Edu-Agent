/**
 * JSON Pointer 工具（RFC 6901 子集）+ 通用结构类型守卫。
 *
 * 用途有两个：
 * 1. **前端侧定点写入**：后端只下发 `PatchOp`，前端在本地 AST 上按 `/slides/2`
 *    这样的路径深拷贝后写入，从而保证未锚定节点引用不变（P0-10②）。
 * 2. **指纹比对时的路径运算**：判断「某路径是否属于被改动子树」。
 *
 * 两个容易写错、后端已专门写负例测试的点：
 * - `isAncestorOrSelf` **必须按 `/` 边界判断**。裸 `startsWith` 会把 `/slides/1`
 *   当成 `/slides/10` 的祖先，导致「改第 10 页却把第 1 页排除在指纹校验之外」。
 * - 数组下标必须严格校验为「非负整数」，`/slides/01` 或 `/slides/-1` 一律拒绝。
 */

/** 结构类型守卫放在本模块，供 `fingerprint` / `client` / `sse` 复用（避免四处重复定义）。 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isUnknownArray(value: unknown): value is unknown[] {
  return Array.isArray(value);
}

/** JSON Pointer 相关的用法错误（路径非法、中间节点不是容器等）。 */
export class JsonPointerError extends Error {
  readonly pointer: string;

  constructor(pointer: string, message: string) {
    super(message.length > 0 ? message : `非法 JSON Pointer：${pointer}`);
    this.name = "JsonPointerError";
    this.pointer = pointer;
  }
}

type Container = Record<string, unknown> | unknown[];

/** RFC 6901 转义：`~1` → `/`，`~0` → `~`（顺序不能反）。 */
function unescapeToken(token: string): string {
  return token.replace(/~1/g, "/").replace(/~0/g, "~");
}

/** 反向转义：`~` → `~0`，`/` → `~1`（同样顺序不能反）。 */
function escapeToken(token: string): string {
  return token.replace(/~/g, "~0").replace(/\//g, "~1");
}

/**
 * 解析指针为段数组。`""`（空串）表示根，返回 `[]`。
 * 严格模式：非空指针必须以 `/` 开头，否则抛错（避免把相对路径误当绝对路径）。
 */
export function parsePointer(pointer: string): string[] {
  if (pointer.length === 0) {
    return [];
  }
  if (!pointer.startsWith("/")) {
    throw new JsonPointerError(pointer, `JSON Pointer 必须以 / 开头：${pointer}`);
  }
  return pointer
    .split("/")
    .slice(1)
    .map((token) => unescapeToken(token));
}

/** 段数组 → 指针字符串。 */
export function toPointer(segments: readonly string[]): string {
  if (segments.length === 0) {
    return "";
  }
  return `/${segments.map((segment) => escapeToken(segment)).join("/")}`;
}

/** 去掉末尾多余的 `/`，便于比较（`/slides/2/` → `/slides/2`）。 */
export function normalizePointer(pointer: string): string {
  if (pointer.length <= 1) {
    return pointer === "/" ? "" : pointer;
  }
  return pointer.endsWith("/") ? pointer.slice(0, -1) : pointer;
}

/** 数组下标解析：只接受规范的非负整数字符串（拒绝 `01`、`-1`、`1.0`）。 */
function toArrayIndex(key: string, pointer: string): number {
  if (!/^(0|[1-9]\d*)$/.test(key)) {
    throw new JsonPointerError(pointer, `数组下标非法：${key}`);
  }
  return Number.parseInt(key, 10);
}

function asContainer(value: unknown, pointer: string, segment: string): Container {
  if (isUnknownArray(value)) {
    return value;
  }
  if (isRecord(value)) {
    return value;
  }
  throw new JsonPointerError(pointer, `路径段 ${segment} 指向的不是对象/数组`);
}

/** 读取子节点；键不存在或数组下标非法时返回 `undefined`（读取语义宽容）。 */
function childAt(container: Container, key: string): unknown {
  if (isUnknownArray(container)) {
    if (!/^(0|[1-9]\d*)$/.test(key)) {
      return undefined;
    }
    const index = Number.parseInt(key, 10);
    return index < container.length ? container[index] : undefined;
  }
  return Object.prototype.hasOwnProperty.call(container, key) ? container[key] : undefined;
}

/** 写入子节点（数组按位覆盖，对象按键覆盖）。 */
function writeChild(container: Container, key: string, value: unknown, pointer: string): void {
  if (isUnknownArray(container)) {
    const index = toArrayIndex(key, pointer);
    if (index > container.length) {
      throw new JsonPointerError(pointer, `数组下标越界：${index} > 长度 ${container.length}`);
    }
    container[index] = value;
    return;
  }
  container[key] = value;
}

function lastSegment(segments: readonly string[], pointer: string): string {
  const value = segments[segments.length - 1];
  if (value === undefined) {
    throw new JsonPointerError(pointer, "空指针没有末段");
  }
  return value;
}

/** 沿段数组走到「父容器」，返回该容器。 */
function resolveContainer(root: unknown, segments: readonly string[], pointer: string): Container {
  let cursor: unknown = root;
  for (const segment of segments) {
    cursor = childAt(asContainer(cursor, pointer, segment), segment);
    if (cursor === undefined) {
      throw new JsonPointerError(pointer, `路径不存在：段 ${segment} 处为 undefined`);
    }
  }
  return asContainer(cursor, pointer, segments[segments.length - 1] ?? "");
}

/** 读取指针指向的值；不存在返回 `undefined`。 */
export function resolvePointer(root: unknown, pointer: string): unknown {
  const segments = parsePointer(pointer);
  let cursor: unknown = root;
  for (const segment of segments) {
    if (!isRecord(cursor) && !isUnknownArray(cursor)) {
      return undefined;
    }
    cursor = childAt(cursor, segment);
    if (cursor === undefined) {
      return undefined;
    }
  }
  return cursor;
}

/** 指针是否存在（区分「值为 undefined」与「路径不存在」时用 `resolvePointer`）。 */
export function hasPointer(root: unknown, pointer: string): boolean {
  const segments = parsePointer(pointer);
  let cursor: unknown = root;
  for (const segment of segments) {
    if (!isRecord(cursor) && !isUnknownArray(cursor)) {
      return false;
    }
    if (childAt(cursor, segment) === undefined) {
      return false;
    }
    cursor = childAt(cursor, segment);
  }
  return true;
}

/**
 * 定点写入：返回**新对象**，原 `root` 不被修改（深拷贝语义）。
 *
 * 为什么用 `structuredClone` 而不是逐层浅拷贝：AST 里全是纯 JSON 值
 * （string/number/boolean/null/数组/对象），structuredClone 语义与 JSON 往返一致，
 * 且不会像 `JSON.parse(JSON.stringify())` 那样丢 `undefined` 键导致键序变化。
 */
export function setAtPath<T>(root: T, pointer: string, value: unknown): T {
  const segments = parsePointer(pointer);
  if (segments.length === 0) {
    // 整树替换（`replace_subtree` 指向根时）。调用方保证 value 与 T 同形。
    return value as T;
  }
  const cloned: T = structuredClone(root);
  let container: Container = asContainer(cloned, pointer, "");
  for (const [index, segment] of segments.entries()) {
    if (index === segments.length - 1) {
      writeChild(container, segment, value, pointer);
      return cloned;
    }
    container = asContainer(childAt(container, segment), pointer, segment);
  }
  return cloned;
}

/** 定点插入：数组按 `splice(index, 0, value)`，对象按键写入。 */
export function insertAtPath<T>(root: T, pointer: string, value: unknown): T {
  const segments = parsePointer(pointer);
  if (segments.length === 0) {
    throw new JsonPointerError(pointer, "不能向根路径插入");
  }
  const cloned: T = structuredClone(root);
  const parent = resolveContainer(cloned, segments.slice(0, -1), pointer);
  const key = lastSegment(segments, pointer);
  if (isUnknownArray(parent)) {
    const index = toArrayIndex(key, pointer);
    if (index > parent.length) {
      throw new JsonPointerError(pointer, `插入位置越界：${index} > 长度 ${parent.length}`);
    }
    parent.splice(index, 0, value);
    return cloned;
  }
  parent[key] = value;
  return cloned;
}

/** 定点删除：数组按 `splice(index, 1)`，对象按键删除。 */
export function removeAtPath<T>(root: T, pointer: string): T {
  const segments = parsePointer(pointer);
  if (segments.length === 0) {
    throw new JsonPointerError(pointer, "不能删除根节点");
  }
  const cloned: T = structuredClone(root);
  const parent = resolveContainer(cloned, segments.slice(0, -1), pointer);
  const key = lastSegment(segments, pointer);
  if (isUnknownArray(parent)) {
    const index = toArrayIndex(key, pointer);
    if (index >= parent.length) {
      throw new JsonPointerError(pointer, `删除位置越界：${index} >= 长度 ${parent.length}`);
    }
    parent.splice(index, 1);
    return cloned;
  }
  delete parent[key];
  return cloned;
}

/** 父路径：`/slides/2/title` → `/slides/2`；`/slides` → `""`（根）。 */
export function getParentPath(pointer: string): string {
  const segments = parsePointer(pointer);
  if (segments.length <= 1) {
    return "";
  }
  return toPointer(segments.slice(0, -1));
}

/** 末段：`/slides/2` → `2`；根返回 `""`。 */
export function getLastSegment(pointer: string): string {
  const segments = parsePointer(pointer);
  return segments[segments.length - 1] ?? "";
}

/** 路径深度：`/slides/2` → 2；根 → 0。 */
export function pointerDepth(pointer: string): number {
  return parsePointer(pointer).length;
}

/**
 * `ancestor` 是否是 `descendant` 的祖先或自身。
 *
 * **按 `/` 边界判断**：`/slides/1` 不是 `/slides/10` 的祖先。
 * 根路径 `""` 是所有路径的祖先。
 */
export function isAncestorOrSelf(ancestor: string, descendant: string): boolean {
  const a = normalizePointer(ancestor);
  const b = normalizePointer(descendant);
  if (a.length === 0) {
    return true;
  }
  if (a === b) {
    return true;
  }
  return b.startsWith(`${a}/`);
}

/**
 * 剔除「已被祖先覆盖」的路径，得到最小改动集。
 *
 * 例：`["/slides/2", "/slides/2/title"]` → `["/slides/2"]`。
 * patch 后做指纹校验时用这个集合，避免把整条链上的祖先都算成「被改动」。
 */
export function pruneNestedPaths(paths: readonly string[]): string[] {
  const normalized = Array.from(new Set(paths.map((path) => normalizePointer(path))));
  return normalized
    .filter((path) => !normalized.some((other) => other !== path && isAncestorOrSelf(other, path)))
    .sort();
}

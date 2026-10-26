"""Patch 执行器：深拷贝 + JSON Pointer 定点写入 + Pydantic 强校验 + 指纹自证。

**这是全项目最不能走捷径的一段代码。** 三条硬约束（§5.2）：

1. `working = copy.deepcopy(spec_dict)`，之后只在 `working` 上定点写入。
   禁止 `Model(**new_dict)` 重建、禁止 `model_copy(update=...)` 整体 dump ——
   重建会把键序规范化，P0-10② 的「其余节点字节级不变」立刻失守。
2. 禁止「删掉再插回」同位置元素：insert 之后列表下标会平移，同位置插回等于换了个对象。
3. 失败整批回滚：`working` 只是副本，任何一步抛错就丢弃它，调用方的原始 dict 毫发无损。

指纹用 `canonical_json` + sha256，纯函数、无随机、无时间，满足 P0-12②。
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from app.errors import AppError, ContractValidationError, ErrorCode
from app.schemas.artifacts import (
    SPEC_MODEL_BY_TYPE,
    AstModel,
    CameraPreset,
    Entity2D,
    KeyframeStep,
    Node3D,
    SlideNode,
)
from app.schemas.common import canonical_json
from app.schemas.patch import PatchOp, PatchRequest

_ROOT_LISTS: tuple[str, ...] = ("slides", "nodes", "steps", "camera_tour")

# 每种根列表对应的节点模型。执行器用它做「字段白名单」与「新节点强校验」。
_ROOT_NODE_MODELS: dict[str, type[AstModel]] = {
    "slides": SlideNode,
    "nodes": Node3D,
    "steps": KeyframeStep,
    "camera_tour": CameraPreset,
}

# 身份字段：允许出现在 payload 里（模型要求必填），但不允许改值。
# 改了等于换了另一个节点，会让前端锚点与历史版本对不上。
_IDENTITY_FIELDS: dict[str, str] = {
    "slides": "node_id",
    "nodes": "id",
    "steps": "step_index",
    "camera_tour": "name",
    "entities": "id",
}

TargetKind = Literal["root_list", "element", "entities_list", "entity"]


class PatchInvariantError(AssertionError):
    """「非锚定节点指纹不变」被破坏。属于程序性错误，不是用户输入问题，所以不进 AppError。"""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths: tuple[str, ...] = tuple(paths)
        super().__init__(f"非锚定节点发生了内容变化：{list(self.paths)}")


# --------------------------------------------------------------------------- #
# 指纹
# --------------------------------------------------------------------------- #
def fingerprint(obj: Any) -> str:
    """规范化 JSON 的 sha256。

    纯函数：同样的内容必然同样的指纹，不受 dict 插入顺序、进程重启、PYTHONHASHSEED 影响。
    这是 P0-10② 唯一可信的证明手段 —— 靠「肉眼比对 JSON」不可自动化。
    """
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def path_fingerprints(obj: Any, prefix: str = "") -> dict[str, str]:
    """生成 `path → fingerprint` 的全路径指纹表。

    容器节点也入表（根为 ""），这样「整棵子树被替换」与「叶子字段被改」都能被识别。
    dict 的键先排序：遍历顺序必须稳定，否则同一份数据两次生成的表键序不同。
    """
    table: dict[str, str] = {prefix: fingerprint(obj)}
    if isinstance(obj, dict):
        for key in sorted(obj.keys(), key=str):
            table.update(path_fingerprints(obj[key], f"{prefix}/{key}"))
    elif isinstance(obj, (list, tuple)):
        for index, item in enumerate(obj):
            table.update(path_fingerprints(item, f"{prefix}/{index}"))
    return table


def _is_related(left: str, right: str) -> bool:
    """两个路径是否存在祖先/后代/相等关系。

    注意不能用裸 `startswith`：`/slides/1` 是 `/slides/10` 的字符串前缀但不是它的祖先。
    必须补上分隔符 `/` 才能正确判定。
    """
    if left == right:
        return True
    if left == "" or right == "":
        # 空路径是根，与任何路径都相关
        return True
    return left.startswith(right + "/") or right.startswith(left + "/")


def changed_paths_between(before: Any, after: Any) -> tuple[str, ...]:
    """两棵树之间指纹不一致的路径（含容器）。用于日志与调试，不参与业务判断。"""
    before_map = path_fingerprints(before)
    after_map = path_fingerprints(after)
    return tuple(
        sorted(
            path
            for path in set(before_map) | set(after_map)
            if before_map.get(path) != after_map.get(path)
        )
    )


def assert_only_subtree_changed(
    before: Any, after: Any, changed_paths: Iterable[str]
) -> None:
    """断言「除 changed_paths 及其祖先/后代外，其余路径指纹完全一致」。

    实现方式与前端 `useArtifactPatch.ts` 的自检逻辑同构（§5.2）：
    各自生成全路径指纹表 → 剔除受影响路径 → 两表必须相等。
    任何一处内容变化（哪怕只是多一个空格）都会让指纹不同，因此这是字节级断言。
    """
    normalized = tuple(path.rstrip("/") for path in changed_paths)
    before_map = path_fingerprints(before)
    after_map = path_fingerprints(after)

    def untouched(path: str) -> bool:
        return not any(_is_related(path, changed) for changed in normalized)

    differing = [
        path
        for path in sorted(set(before_map) | set(after_map))
        if untouched(path) and before_map.get(path) != after_map.get(path)
    ]
    if differing:
        raise PatchInvariantError(differing)


# --------------------------------------------------------------------------- #
# 路径解析
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PatchTarget:
    """解析结果。`container[key]` 就是需要定点写入的位置。"""

    path: str
    root: str
    kind: TargetKind
    container: Any
    key: Any
    node_model: type[AstModel] | None
    exists: bool

    @property
    def identity_field(self) -> str:
        return _IDENTITY_FIELDS["entities" if self.kind == "entity" else self.root]


def _invalid(node_path: str, reason: str) -> AppError:
    return AppError(ErrorCode.PATCH_PATH_INVALID, f"锚点路径 {node_path!r} 非法：{reason}")


def _segments(node_path: str) -> tuple[str, ...]:
    return tuple(part for part in node_path.split("/") if part)


def resolve_target(spec: dict[str, Any], node_path: str, *, allow_append: bool = False) -> PatchTarget:
    """把 JSON Pointer 风格路径解析成 (容器, 键)。

    allow_append=True 时允许下标 == 列表长度（insert 到末尾）。
    所有越界/结构不符的情况都翻译成 PATCH_PATH_INVALID，让前端能给出「锚点失效，请重新点选」。
    """
    segments = _segments(node_path)
    if not segments or segments[0] not in _ROOT_LISTS:
        raise _invalid(node_path, "路径必须以 /slides、/nodes、/steps 或 /camera_tour 开头")

    root = segments[0]
    root_list = spec.get(root)
    if not isinstance(root_list, list):
        raise _invalid(node_path, f"当前 AST 没有 /{root} 列表")

    if len(segments) == 1:
        return PatchTarget(node_path, root, "root_list", spec, root, None, True)

    if not segments[1].isdigit():
        raise _invalid(node_path, "列表下标必须是整数")
    index = int(segments[1])
    if index > len(root_list):
        raise _invalid(node_path, f"下标 {index} 越界（/{root} 长度 {len(root_list)}）")
    if index == len(root_list) and not allow_append:
        raise _invalid(node_path, f"下标 {index} 越界（/{root} 长度 {len(root_list)}）")

    if len(segments) == 2:
        return PatchTarget(
            node_path, root, "element", root_list, index, _ROOT_NODE_MODELS[root], index < len(root_list)
        )

    if root != "steps" or segments[2] != "entities":
        raise _invalid(node_path, "只支持 /steps/{i}/entities/{j} 形式的两级路径")
    if index >= len(root_list):
        raise _invalid(node_path, f"第 {index} 个关键帧不存在，无法定位其 entities")
    step = root_list[index]
    if not isinstance(step, dict):
        raise _invalid(node_path, f"第 {index} 个关键帧不是对象")
    entities = step.get("entities")
    if not isinstance(entities, list):
        raise _invalid(node_path, f"第 {index} 个关键帧没有 entities 列表")

    if len(segments) == 3:
        return PatchTarget(node_path, root, "entities_list", step, "entities", None, True)

    if len(segments) != 4 or not segments[3].isdigit():
        raise _invalid(node_path, "entities 下标必须是整数")
    entity_index = int(segments[3])
    if entity_index > len(entities):
        raise _invalid(node_path, f"entities 下标 {entity_index} 越界（长度 {len(entities)}）")
    if entity_index == len(entities) and not allow_append:
        raise _invalid(node_path, f"entities 下标 {entity_index} 越界（长度 {len(entities)}）")
    return PatchTarget(
        node_path, root, "entity", entities, entity_index, Entity2D, entity_index < len(entities)
    )


def _parent_list_path(target: PatchTarget) -> str:
    """被操作列表自身的路径。

    insert/delete 会让后续元素下标平移，所以「变化范围」只能声明到列表这一层：
    若声明到元素层，平移后的兄弟节点会被误判为「非锚定节点被改了」。
    """
    segments = _segments(target.path)
    if target.kind in ("root_list", "element"):
        return f"/{segments[0]}"
    return f"/{segments[0]}/{segments[1]}/entities"


def _summarize(exc: ValidationError, limit: int = 3) -> str:
    """把 Pydantic 错误压成一句人话，回灌给 LLM 重试时它才看得懂。"""
    parts: list[str] = []
    for error in exc.errors()[:limit]:
        location = "/".join(str(part) for part in error.get("loc", ())) or "<root>"
        parts.append(f"{location}: {error.get('msg', '')}")
    return "; ".join(parts)


def _validated_node_dict(model_cls: type[AstModel], payload: dict[str, Any], op_name: str) -> dict[str, Any]:
    """校验新节点并转成可 JSON 序列化的 dict。

    用 model_dump(mode="json") 而不是 mode="python"：tuple 必须落成 list，
    否则 json.dumps 时 tuple 会变数组、但内存里仍是 tuple，指纹会在存库前后不一致。
    """
    try:
        validated = model_cls.model_validate(payload)
    except ValidationError as exc:
        raise ContractValidationError(
            f"{op_name} 的新节点未通过 {model_cls.__name__} 校验：{_summarize(exc)}"
        ) from exc
    return validated.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# 五种 op 的执行
# --------------------------------------------------------------------------- #
def _apply_update_node(working: dict[str, Any], op: PatchOp, changed: set[str]) -> None:
    target = resolve_target(working, op.node_path)
    if target.kind not in ("element", "entity"):
        raise _invalid(op.node_path, "update_node 必须指向单个节点（如 /slides/2）")
    if not target.exists:
        raise _invalid(op.node_path, "目标节点不存在")
    node = target.container[target.key]
    if not isinstance(node, dict):
        raise _invalid(op.node_path, "目标节点不是对象")
    model_cls = target.node_model
    if model_cls is None:
        raise _invalid(op.node_path, "无法确定目标节点的类型")

    allowed_fields = set(model_cls.model_fields)
    identity = target.identity_field
    for field_name, value in (op.payload or {}).items():
        if field_name == "type":
            raise ContractValidationError("payload 不允许修改 type（判别字段）")
        if field_name not in allowed_fields:
            raise ContractValidationError(
                f"字段 {field_name!r} 不属于 {model_cls.__name__}；允许字段：{sorted(allowed_fields)}"
            )
        if field_name == identity and value != node.get(identity):
            raise ContractValidationError(f"不允许修改节点身份字段 {identity!r}")
        # 定点写入：已存在的键保持原位置（键序不变），新键追加到末尾
        node[field_name] = copy.deepcopy(value)
        changed.add(f"{target.path}/{field_name}")


def _apply_insert_node(working: dict[str, Any], op: PatchOp, changed: set[str]) -> None:
    target = resolve_target(working, op.node_path, allow_append=True)
    if target.kind not in ("element", "entity"):
        raise _invalid(op.node_path, "insert_node 必须指向父列表 + 插入位（如 /slides/3）")
    model_cls = target.node_model
    if model_cls is None:
        raise _invalid(op.node_path, "无法确定待插入节点的类型")

    node_dict = _validated_node_dict(model_cls, dict(op.payload or {}), "insert_node")
    identity = target.identity_field
    siblings = target.container
    existing = {item.get(identity) for item in siblings if isinstance(item, dict)}
    if node_dict.get(identity) in existing:
        raise ContractValidationError(
            f"新节点 {identity}={node_dict.get(identity)!r} 与现存节点冲突，请换一个 {identity}"
        )

    siblings.insert(int(target.key), node_dict)
    changed.add(_parent_list_path(target))


def _apply_delete_node(working: dict[str, Any], op: PatchOp, changed: set[str]) -> None:
    target = resolve_target(working, op.node_path)
    if target.kind not in ("element", "entity"):
        raise _invalid(op.node_path, "delete_node 必须指向单个节点")
    if not target.exists:
        raise _invalid(op.node_path, "目标节点不存在")
    if len(target.container) <= 1:
        # 删空会让 AST 违反 min_length=1，前端渲染器也没有空态兜底，就地拦住
        raise AppError(ErrorCode.PATCH_PATH_INVALID, "删除后列表不能为空，请保留至少一个节点")
    del target.container[target.key]
    changed.add(_parent_list_path(target))


def _apply_replace_subtree(working: dict[str, Any], op: PatchOp, changed: set[str]) -> None:
    target = resolve_target(working, op.node_path)
    if target.kind not in ("element", "entity"):
        # 列表级整表替换无法用 dict payload 表达；真需要时用 insert+delete 组合，
        # 这样 changed_paths 的语义仍然诚实（列表整体变化）
        raise _invalid(op.node_path, "replace_subtree 需指向单个节点；整表替换请用 insert_node/delete_node")
    if not target.exists:
        raise _invalid(op.node_path, "目标节点不存在")
    model_cls = target.node_model
    if model_cls is None:
        raise _invalid(op.node_path, "无法确定目标节点的类型")

    target.container[target.key] = _validated_node_dict(
        model_cls, dict(op.payload or {}), "replace_subtree"
    )
    changed.add(target.path)


def _apply_focus_camera(working: dict[str, Any], op: PatchOp) -> str:
    """focus_camera 只移动镜头，**不修改任何内容**。

    这是刻意的：视角是渲染态而非内容态。若把它写进 spec，同一份内容会因「看过哪个角度」
    产生不同指纹，版本比对就失去意义。
    """
    segments = _segments(op.node_path)
    tour = working.get("camera_tour")
    if not isinstance(tour, list) or not tour:
        raise _invalid(op.node_path, "focus_camera 仅适用于带 camera_tour 的 3D 场景")
    if len(segments) < 2:
        raise _invalid(op.node_path, "focus_camera 必须指向具体机位，如 /camera_tour/1")
    if not segments[1].isdigit():
        raise _invalid(op.node_path, "机位下标必须是整数")
    index = int(segments[1])
    if index >= len(tour):
        raise _invalid(op.node_path, f"机位下标 {index} 越界（camera_tour 长度 {len(tour)}）")

    preset_name = str((op.payload or {}).get("preset_name", "")).strip()
    names = {item.get("name") for item in tour if isinstance(item, dict)}
    if preset_name not in names:
        raise ContractValidationError(
            f"机位 {preset_name!r} 不存在；可用机位：{sorted(str(name) for name in names)}"
        )
    return preset_name


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PatchOutcome:
    """执行结果。

    spec 是**定点写入后的原始 dict**（保持键序），model 只用于读取与序列化校验，
    落库必须用 spec —— 用 model 重新 dump 会规范化键序，破坏字节级不变。
    """

    spec: dict[str, Any]
    model: AstModel
    changed_paths: tuple[str, ...]
    focus_preset: str | None = None
    base_version: int | None = None
    new_version: int | None = None


def ensure_base_version(current_version: int, base_version: int) -> None:
    """乐观锁检查（P0-10 / §5.2）。不匹配即 409，前端提示刷新。"""
    if current_version != base_version:
        raise AppError(
            ErrorCode.PATCH_BASE_VERSION_CONFLICT,
            f"画布当前已是 v{current_version}，请求基于 v{base_version}，请刷新后重试",
        )


def apply_patch(
    spec_dict: dict[str, Any],
    ops: Sequence[PatchOp],
    *,
    base_version: int | None = None,
) -> PatchOutcome:
    """执行一批 PatchOp。

    步骤严格按 §5.2：deepcopy → 逐个定点写入 → Pydantic 强校验 → 返回。
    校验失败时 `working` 被丢弃（整批回滚），调用方传入的 spec_dict 从未被触碰。
    """
    if not isinstance(spec_dict, dict):
        raise ContractValidationError("spec 必须是 JSON 对象（dict）")

    spec_type = spec_dict.get("type")
    model_cls = SPEC_MODEL_BY_TYPE.get(spec_type) if isinstance(spec_type, str) else None
    if model_cls is None:
        raise ContractValidationError(f"未知的 artifact 类型：{spec_type!r}")

    # 基线校验：外部传入的 spec 可能来自库里的旧数据或 LLM 的半成品，
    # 基线不合法时算出来的指纹毫无意义，先拒掉。
    try:
        model_cls.model_validate(spec_dict)
    except ValidationError as exc:
        raise ContractValidationError(f"原始 AST 未通过契约校验：{_summarize(exc)}") from exc

    working: dict[str, Any] = copy.deepcopy(spec_dict)
    changed: set[str] = set()
    focus_preset: str | None = None

    for op in ops:
        if op.op == "update_node":
            _apply_update_node(working, op, changed)
        elif op.op == "insert_node":
            _apply_insert_node(working, op, changed)
        elif op.op == "delete_node":
            _apply_delete_node(working, op, changed)
        elif op.op == "replace_subtree":
            _apply_replace_subtree(working, op, changed)
        elif op.op == "focus_camera":
            focus_preset = _apply_focus_camera(working, op)
        else:  # pragma: no cover - Literal 已排除，留作防御
            raise ContractValidationError(f"不支持的 op：{op.op!r}")

    try:
        validated = model_cls.model_validate(working)
    except ValidationError as exc:
        raise ContractValidationError(f"patch 后 AST 未通过契约校验：{_summarize(exc)}") from exc

    return PatchOutcome(
        spec=working,
        model=validated,
        changed_paths=tuple(sorted(changed)),
        focus_preset=focus_preset,
        base_version=base_version,
        new_version=(base_version + 1) if base_version is not None else None,
    )


def apply_patch_request(
    spec_dict: dict[str, Any],
    request: PatchRequest,
    current_version: int,
) -> PatchOutcome:
    """带乐观锁的完整入口：先比版本，再执行。API 层直接调这个。"""
    ensure_base_version(current_version, request.base_version)
    return apply_patch(spec_dict, request.ops, base_version=request.base_version)


__all__ = [
    "PatchInvariantError",
    "PatchOutcome",
    "PatchTarget",
    "apply_patch",
    "apply_patch_request",
    "assert_only_subtree_changed",
    "changed_paths_between",
    "ensure_base_version",
    "fingerprint",
    "path_fingerprints",
    "resolve_target",
]

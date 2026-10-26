"""增量补丁契约（§5.2 / SPEC §1.3 Command Pattern）。

为什么 payload 是 dict 而不是整棵 AST：整树下发等于全量重生成，会直接违反
「patch 后其余节点字节级不变」（P0-10②）。这里用 `dict | None` 把「只允许局部字段」
写进类型里，配合 patch_engine 的字段白名单形成双重约束。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 允许被 patch 的路径形状。限定四种根列表，是为了让执行器不必处理任意 JSON Pointer，
# 也顺手挡掉 LLM 幻想出来的 /slides/2/bullets/0 这种越界路径。
NODE_PATH_RE = r"^/(slides|nodes|steps|camera_tour)(/\d+)?(/(entities)(/\d+)?)?$"

OpName = Literal["update_node", "insert_node", "delete_node", "replace_subtree", "focus_camera"]

# 单批最多 20 个 op：既防止 LLM 一次性下发「整份重写」，也让前端能逐条应用与高亮
MAX_OPS_PER_REQUEST = 20


class PatchOp(BaseModel):
    """单条增量指令。"""

    model_config = ConfigDict(extra="forbid")

    op: OpName
    node_path: str = Field(pattern=NODE_PATH_RE)
    payload: dict[str, Any] | None = None
    reason: str = ""

    @model_validator(mode="after")
    def _op_payload_consistent(self) -> "PatchOp":
        """op 与 payload 的一致性校验（§5.2 表格最后一列）。

        这条校验放在契约层而不是执行器里，是因为它不依赖当前 AST 状态：
        越早失败，agent_engine 的重试就越便宜（不用先读库、先深拷贝）。
        """
        if self.op == "delete_node":
            if self.payload is not None:
                raise ValueError("delete_node 不允许携带 payload")
            return self

        if self.op == "focus_camera":
            if not self.node_path.startswith("/camera_tour/"):
                raise ValueError("focus_camera 的 node_path 必须指向 /camera_tour/{i}")
            if not isinstance(self.payload, dict):
                raise ValueError("focus_camera 必须携带 payload={'preset_name': '...'}")
            preset_name = self.payload.get("preset_name")
            if not isinstance(preset_name, str) or not preset_name.strip():
                raise ValueError("focus_camera 的 payload 必须包含非空 preset_name")
            extra_keys = set(self.payload) - {"preset_name"}
            if extra_keys:
                raise ValueError(f"focus_camera 的 payload 只允许 preset_name，多余键：{sorted(extra_keys)}")
            return self

        # update_node / insert_node / replace_subtree
        if not self.payload:
            raise ValueError(f"{self.op} 必须携带非空 payload")
        if "type" in self.payload:
            # type 是判别字段，改它等于换了一种 AST，必须走 replace_subtree 之外的显式流程
            raise ValueError("payload 不允许出现 type 字段")
        return self

    @property
    def segments(self) -> tuple[str, ...]:
        """把 JSON Pointer 风格路径切成段。执行器与测试共用，避免各自 split 出偏差。"""
        return tuple(part for part in self.node_path.split("/") if part)


class PatchRequest(BaseModel):
    """一批 patch。base_version 是乐观锁：不匹配即 409，防止两个标签页互相覆盖。"""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1)
    base_version: int = Field(ge=1)
    ops: list[PatchOp] = Field(min_length=1, max_length=MAX_OPS_PER_REQUEST)


__all__ = [
    "MAX_OPS_PER_REQUEST",
    "NODE_PATH_RE",
    "OpName",
    "PatchOp",
    "PatchRequest",
]

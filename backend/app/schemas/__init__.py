"""契约层（Pydantic 模型）。

本包只描述「线上传输的形状」，不含任何业务逻辑与 IO：
- artifacts.py  三种 AST（PPT / 2D / 3D）与统一 Agent 响应
- patch.py      PatchOp / PatchRequest（JSON Pointer 风格的增量指令）
- chat.py       ChatRequest 与 8 类 SSE 事件
"""

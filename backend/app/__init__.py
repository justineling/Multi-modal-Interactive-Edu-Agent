"""多模态沉浸式智能助教 · 后端应用包。

分层约定（严格单向依赖，禁止反向 import 造成环）：

    api/        ← 只依赖 services/ 与 schemas/，负责 HTTP/SSE 协议转换
    services/   ← 业务编排，可依赖 db/ 与 schemas/
    db/         ← 持久化与向量存储，只依赖 schemas/common.py
    schemas/    ← 纯契约，禁止 import 任何 app 内部模块（除 common.py）
"""

__all__ = ["__version__"]

# 与 frontend 的顶栏版本号、/api/health 共用；改动需同步 docs/ARCHITECTURE.md
__version__ = "0.1.0"

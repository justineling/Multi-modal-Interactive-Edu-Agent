# SPEC.md — 原始工程规范（需求方提供，不可篡改）

> 本文档由需求方提供，是项目的**原始依据**。`docs/PRD.md` 与 `docs/ARCHITECTURE.md` 均以此为约束展开。
> 若实现与本文档冲突，以本文档为准。

---

## 0. 项目定位

**多模态沉浸式智能助教 / 自适应生成式学习工作区**（Multi-modal Interactive Edu-Agent / Adaptive Spatial Learning Workspace）。

本质不是聊天框，而是「对话驱动的富媒体生成式工作区」：

1. **多层级知识引擎**：以用户私有资料（如 KSSM 课本 PDF）为锚点构建知识库，同时具备通用知识库扩展能力，支持从基础应试到大学级深度的无缝延展。
2. **多形态具象化渲染**：Agent 根据知识点属性自动选择表现形式（概念演变 → PPT/思维导图；空间结构/文物 → 3D；机制与动态过程 → 2D/3D 动画交互）。
3. **上下文连续迭代**：生成出的媒体产物是「活的 Canvas/节点」，用户可在后续对话中针对某个 slide、某个 3D 部件或某段动画直接追问或要求重新生成。

---

## 1. 四大核心工程设计模式（必须落地）

### 1.1 受控 DSL 驱动渲染模式（Schema-Driven Strategy Pattern）

- **严禁 LLM 直接输出 HTML/Three.js 源码**。
- 后端使用 Pydantic 强类型定义三种标准 JSON AST 契约：`SlideDeckSpec`（PPT）、`Keyframe2DSpec`（2D 动态推演）、`SceneGraph3DSpec`（3D 空间结构）。
- **执行链路**：LLM 只负责填充 JSON 数据；前端通过**策略模式（Strategy Pattern）**注册三个确定性渲染引擎组件，读取 JSON 后毫秒级渲染成交互画面。
- 目标：零编译等待、100% 不崩溃、天然支持前端点击交互。

### 1.2 双轨知识路由与自纠错检索模式（Corrective RAG + Router Pattern）

- **目录树精确锚定（Hierarchical TOC Lookup）**：解析 PDF 时提取章节号（如 `4.1`）存入元数据。学生输入"学习 4.1"时，优先执行 SQL 元数据过滤 `WHERE section_code = '4.1'` 拉取整节骨架，再辅以向量 + BM25 混合检索。
- **相关性评估器（Retrieval Grader）**：判断检索片段是否足以回答追问，输出三种状态之一：
  - `SYLLABUS_MATCH`（课本内）：严格依据课本生成，强制携带页码引用（如 `[Form 4, Bab 4.1, p.94]`）。
  - `PARTIAL_OR_BEYOND`（超纲深挖）：触发**双层回复结构** —— 第一层"课本标准答案（应试基准）"，第二层"深度拓展（Extended Context）"调用模型高阶内建知识或联网搜索，明确区分边界，防止考试答偏。

### 1.3 画布增量补丁模式（Command / JSON-Patch Pattern）

- 右侧画布每个产物有唯一 `artifact_id` 和当前 JSON 状态树。
- 学生追问时，Agent 接收当前状态摘要，**仅输出增量操作指令**（如 `INSERT_SLIDE`、`UPDATE_NODE`、`FOCUS_3D_CAMERA`），前端通过状态机局部热更新并触发镜头平滑过渡。
- 不得全量重生成。

### 1.4 节点锚点双向绑定模式（Bidirectional Context Binding）

- PPT 每页、2D 动画每个时间步、3D 场景每个子网格（Mesh）都携带唯一 `node_id`。
- 学生点击某个 3D 部件或某页 PPT 的"追问此处"，前端自动将该 `node_id` 及其绑定的课本原文切片注入左侧输入框上下文。

---

## 2. 唯一锁定技术栈（Vibe Coding 黄金组合）

不提供模棱两可的备选。

| 模块层级 | 唯一锁定技术选型 | 选型理由 |
| --- | --- | --- |
| **前端核心框架** | React 19 + TypeScript + Vite + TailwindCSS | AI 训练语料最丰富，强类型约束防止 AI 乱写属性，热更新极快 |
| **工作区与状态管理** | Zustand + Allotment（分栏容器） | 左侧 Chat 流 + 右侧多标签画布，Zustand 极简无冗余模板代码 |
| **PPT 渲染引擎** | 自定义 JSON Slide 渲染器 + Framer Motion | 基于 Tailwind 预置 6 种教学卡片布局（时间轴、对比表、因果链、核心概念、图文分栏、随堂测验），支持一键导出 PDF |
| **2D 动画引擎** | React-Konva（Canvas 2D）+ GSAP | 适合历史疆域/航线推演、物理受力、生物流程等动态步进演示，由关键帧 JSON 驱动，支持拖拽时间轴 |
| **3D 场景引擎** | React Three Fiber (R3F) + Drei | 将 Three.js 封装为声明式 React 组件，通过 JSON 组装预置几何体、外部 GLTF 模型、3D 悬浮文字标签（Html Annotation）与轨道相机控制器 |
| **后端与 Agent 编排** | Python 3.12+ + FastAPI + Pydantic v2 | 原生支持异步 SSE 流式响应，Pydantic 的 `response_model` 强制 LLM 输出 100% 合法 JSON AST |
| **数据库与向量引擎** | PostgreSQL + pgvector（或 Supabase） | **拒绝繁重的 Milvus/Qdrant 独立集群**。单实例 Postgres 同时搞定业务表 + 向量检索 + 全文检索 |
| **文档结构化解析** | PyMuPDF4LLM + 章节正则解析器 | 将 PDF 极速转换为保留标题层级与表格的 Markdown，自带页码元数据提取 |

> **交付总监补充（工程现实约束）**：为保证项目在无外部依赖的机器上开箱即跑，数据库采用 **dual-mode**：
> - 默认 `sqlite+aiosqlite` + 内存向量索引（零外部依赖）
> - 配置 `DATABASE_URL=postgresql+asyncpg://...` 时切换 PostgreSQL + pgvector
> 两模式共用同一套 SQLAlchemy 模型与检索接口，**业务代码不得出现模式分支**。

---

## 3. 项目目录结构规范

```text
edu-spatial-workspace/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口 + CORS + 路由挂载
│   │   ├── config.py               # 环境变量与模型配置
│   │   ├── db/
│   │   │   ├── session.py          # SQLAlchemy 异步连接池
│   │   │   └── models.py           # User, Workspace, Document, DocChunk, Artifact, Message
│   │   ├── schemas/
│   │   │   ├── chat.py             # 对话请求与 SSE 事件契约
│   │   │   └── artifacts.py        # 核心！PPT / 2D Animation / 3D Scene 的 Pydantic JSON AST
│   │   ├── services/
│   │   │   ├── pdf_parser.py       # PyMuPDF4LLM 解析 + 章节号（如 4.1）正则提取
│   │   │   ├── retriever.py        # 元数据过滤 + pgvector 向量与全文混合检索
│   │   │   └── agent_engine.py     # CRAG 路由决策 + 结构化 Artifact 生成与 Patch 更新
│   │   └── api/
│   │       ├── workspaces.py       # 对话区 CRUD 与多文件上传接口
│   │       └── chat_stream.py      # SSE 流式对话与画布指令推送
│   └── requirements.txt
└── frontend/
    ├── src/
    │   ├── types/
    │   │   └── artifact.ts         # 与后端 Pydantic 严格对应的 TypeScript 接口
    │   ├── store/
    │   │   └── useWorkspaceStore.ts # Zustand：当前对话、文件列表、激活 Artifact、选中节点
    │   ├── components/
    │   │   ├── layout/
    │   │   │   └── SplitWorkspace.tsx  # 左 Chat 右 Canvas 双栏布局
    │   │   ├── chat/
    │   │   │   ├── ChatPanel.tsx       # 引用页码高亮、超纲标签、节点追问锚点
    │   │   │   └── FileDrawer.tsx      # 文件管理与章节目录预览
    │   │   └── canvas/
    │   │       ├── ArtifactHost.tsx    # 策略模式分发器
    │   │       ├── SlideRenderer.tsx   # PPT 渲染器（6 种教学布局 + 单页追问）
    │   │       ├── Anim2DRenderer.tsx  # React-Konva + GSAP 关键帧动态推演器
    │   │       └── Scene3DRenderer.tsx # R3F 空间模型与 3D 标注点渲染器
```

---

## 4. 数据库核心表设计

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- 1. 对话工作区
CREATE TABLE workspaces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. 工作区上传的文件
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    filename VARCHAR(255) NOT NULL,
    toc_tree JSONB DEFAULT '[]',
    status VARCHAR(50) DEFAULT 'processing'
);

-- 3. 结构化文档切片
CREATE TABLE document_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    section_code VARCHAR(50),
    page_number INT,
    content TEXT NOT NULL,
    embedding vector(1536)
);
CREATE INDEX idx_chunks_workspace_section ON document_chunks(workspace_id, section_code);

-- 4. 多模态生成产物
CREATE TABLE artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    artifact_type VARCHAR(50) NOT NULL, -- 'ppt' | 'anim_2d' | 'scene_3d'
    version INT DEFAULT 1,
    spec_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

> SQLite 模式下：`UUID` → `TEXT`，`JSONB` → `JSON`，`vector` → `TEXT`（存 JSON 数组），由架构师设计兼容层。

---

## 5. 核心多模态 JSON AST 契约（前后端灵魂桥梁）

```python
from pydantic import BaseModel, Field
from typing import List, Literal, Optional, Union

# ========== 1. PPT 幻灯片 AST ==========
class SlideNode(BaseModel):
    node_id: str = Field(description="唯一节点ID，如 slide-1，用于学生针对单页追问")
    layout: Literal["title_hero", "bullet_points", "timeline", "comparison", "cause_effect", "quiz"]
    title: str
    subtitle: Optional[str] = None
    bullets: List[str] = []
    comparison_left: Optional[List[str]] = None
    comparison_right: Optional[List[str]] = None
    speaker_notes: str = Field(description="AI 助教对该页知识点的口语化深入讲解")
    source_ref: str = Field(description="课本出处，如 'Form 4 Sejarah, Bab 4.1, p.94' 或 'Extended Knowledge'")

class SlideDeckSpec(BaseModel):
    type: Literal["ppt"] = "ppt"
    topic: str
    slides: List[SlideNode]

# ========== 2. 2D 交互动画 AST ==========
class Entity2D(BaseModel):
    id: str
    shape: Literal["circle", "rect", "arrow", "label_box", "zone"]
    label: str
    x: float  # 归一化坐标 0 ~ 1000
    y: float  # 归一化坐标 0 ~ 600
    color: str
    details: str = Field(description="点击该元素时弹出的详细知识点解释")

class KeyframeStep(BaseModel):
    step_index: int
    narration: str = Field(description="当前动画步骤的旁白解说")
    entities: List[Entity2D] = Field(description="当前步骤各元素的位置与状态，前端自动补间过渡")

class Keyframe2DSpec(BaseModel):
    type: Literal["anim_2d"] = "anim_2d"
    title: str
    steps: List[KeyframeStep]

# ========== 3. 3D 空间场景 AST ==========
class Node3D(BaseModel):
    id: str
    primitive: Literal["box", "sphere", "cylinder", "cone", "terrain_plane", "arrow_link"]
    label: str
    position: tuple[float, float, float]
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    color: str
    annotation_markdown: str = Field(description="3D 空间悬浮标签内容，点击可聚焦镜头并追问")

class CameraPreset(BaseModel):
    name: str
    position: tuple[float, float, float]
    target: tuple[float, float, float]
    explanation: str

class SceneGraph3DSpec(BaseModel):
    type: Literal["scene_3d"] = "scene_3d"
    title: str
    nodes: List[Node3D]
    camera_tour: List[CameraPreset] = Field(description="预设导览机位列表，学生可点击下一步切换视角")

# ========== 4. 统一 Agent 响应封装 ==========
class AgentTurnResponse(BaseModel):
    reply_markdown: str
    knowledge_scope: Literal["syllabus_strict", "hybrid_extended"]
    citations: List[str]
    modality_decision: Literal["none", "create_artifact", "patch_artifact"]
    artifact_payload: Optional[Union[SlideDeckSpec, Keyframe2DSpec, SceneGraph3DSpec]] = None
```

---

## 6. 成本模型（用于架构决策，非实现要求）

- 模型分层路由：轻量任务（路由/评估/答疑）用 flash 级小模型；复杂 JSON AST 生成用 pro 级大模型。
- Prompt Prefix Caching：将章节原文 + JSON Schema 固定在 System Prompt 头部，多轮追问命中缓存。
- 单学生学完一节的 API 成本约 $0.015 ~ $0.030。
- MVP 阶段基础设施月费约 $17（4C8G 单机 Docker Compose）。

> 实现要求：`config.py` 必须支持 `LLM_MODEL_ROUTER`（轻量）与 `LLM_MODEL_GENERATOR`（重）两个独立模型名配置。

---

## 7. 关键落地红线（避坑要点）

1. **禁止服务端实时渲染重量级视频**：不在服务端跑 Manim/Blender。所有产物必须是前端可即时渲染的 JSON DSL。
2. **媒体产物状态持久化**：每个 Artifact 有 `artifact_id` + 版本号；追问时读当前 Artifact 做增量 Diff，不重跑全流程。
3. **禁止 LLM 裸写复杂渲染代码**：十次有八次会出现镜头穿模、材质丢失或语法崩溃。

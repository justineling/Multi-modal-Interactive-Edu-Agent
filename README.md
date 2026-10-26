# 多模态沉浸式智能助教（Edu-Spatial Workspace）

> 对话驱动的富媒体生成式学习工作区。上传教材 PDF，对话中由 Agent 自动生成 **PPT / 2D 推演动画 / 3D 空间场景**，并可针对画布上任意一页、一个元素、一个部件继续追问，系统做**增量补丁**而非整篇重来。

---

## 接手开发？先读交接文档

**→ [`HANDOVER.md`](./HANDOVER.md)**

里面有：回家三步走（怎么把代码拿到另一台机器）、代码地图（改什么去哪）、待办清单（按优先级）、六个必踩的坑、关键决策记录、以及「下次开工第一件事」。

### 当前状态

| 指标 | 当前值 |
|---|---|
| P0 需求 | **12 / 12 交付** |
| 后端测试 | **244 passed / 0 failed** |
| 前端测试 | **153 passed / 7 文件** |
| 端到端冒烟 | **38 项全通过** |
| 真实 LLM（DeepSeek） | TTFT **2.9s** · 5~6 页 / 5~6 种布局 · 页码全部真实 |
| 桌面安装 | 支持（PWA） |
| 已知限制 | 真实模式总耗时约 70s、PG 模式未实测、扫描版 PDF 不支持、无登录体系 |

---

## 文档

**完整文档站入口：`docs/index.html`**（浏览器直接打开，无需启动服务）

共 20 篇，分 6 类：

| 分类 | 篇数 | 适合谁 |
|---|---|---|
| `01-快速开始` | 2 | 所有人：项目定位、环境搭建、五分钟跑起来 |
| `02-用户手册` | 4 | 使用者：上传教材、对话学习、画布交互、常见问题 |
| `03-架构设计` | 5 | 维护者：系统总览、数据模型、DSL 契约、检索与路由、增量补丁与 SSE |
| `04-开发指南` | 5 | **开发者**：一次提问的完整旅程、前后端代码地图、动手改功能、测试与调试 |
| `05-运维部署` | 2 | 运维：部署方案与成本、故障排查 |
| `06-项目档案` | 2 | 所有人：需求与验收标准、16 个踩坑记录 |

> 如果你是第一次接触这个项目，读这三篇就够：
> `01-快速开始/1` → `01-快速开始/2` → `04-开发指南/1`。

Markdown 源文档保留在 `docs/` 根目录（`PRD.md` / `ARCHITECTURE.md` / `SPEC.md` / `IMPL-NOTES.md` / `QA-REPORT-backend.md`），HTML 文档站是它们面向不同读者的重组版本。

---

## 一、核心设计：DSL 契约驱动渲染

本项目**不允许 LLM 直接生成 HTML / Three.js 源码**。LLM 只负责填充 JSON AST，前端用确定性渲染器渲染。这是整个系统稳定性的根基。

```
用户提问
   │
   ├─ 混合检索（章节号精确命中 + BM25 + 向量）→ 相关性评估
   │     ├─ SYLLABUS_MATCH   → 严格依据课本，带页码引用
   │     ├─ PARTIAL_OR_BEYOND → 双层回答（课本基准 + 深度拓展）
   │     └─ NO_MATCH          → 明确「课本未覆盖」，禁止编造页码
   │
   ├─ 形态路由 → ppt | anim_2d | scene_3d
   │
   └─ 生成 JSON AST
         ├─ SlideDeckSpec   → SlideRenderer（6 种教学布局）
         ├─ Keyframe2DSpec  → Anim2DRenderer（Konva + GSAP 关键帧）
         └─ SceneGraph3DSpec → Scene3DRenderer（R3F + Drei）
```

**四种工程设计模式**（详见 `docs/ARCHITECTURE.md`）：

1. **受控 DSL 驱动渲染**（策略模式）— LLM 填 JSON，前端策略分发渲染
2. **双轨知识路由 + 自纠错检索**（CRAG）— 课本内严格依据 vs 超纲拓展，边界清晰
3. **画布增量补丁**（JSON-Patch）— 追问只改目标子树，其余节点字节级不变
4. **节点锚点双向绑定** — 点击画布元素 → 自动携带 `node_path` + `section_code` + 页码回填输入框

---

## 二、技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 19 · TypeScript · Vite 7 · TailwindCSS 3.4 · Zustand · Allotment |
| PPT 渲染 | 自定义 JSON 渲染器（6 布局）+ Framer Motion |
| 2D 动画 | React-Konva 19 · GSAP |
| 3D 场景 | React Three Fiber 9 · Drei 10 · three 0.186 |
| 后端 | Python 3.13 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 异步 · SSE |
| 数据库 | 默认 SQLite（零依赖）· 可切 PostgreSQL + pgvector |
| 文档解析 | PyMuPDF（逐页页码映射）+ 章节号正则 |

> **版本锁定是硬约束**：`@react-three/fiber` 必须 v9（v8 与 React 19 不兼容）、`react-konva` 必须 19、`tailwindcss` 必须 v3.4（v4 是 CSS-first 配置，写法完全不同）、`katex` 必须 0.16 线（`rehype-katex@7` 依赖 `^0.16`，装 0.19 会出现两份 katex 导致公式字体错位）。

---

## 三、快速开始

### 最快方式：一键启动

| 平台 | 操作 |
|---|---|
| **Windows** | 双击根目录的 **`1. 启动网页端(Windows).bat`** |
| **macOS / Linux** | `chmod +x "2. 启动网页端(Mac-Linux).sh" && ./"2. 启动网页端(Mac-Linux).sh"` |

脚本会自动完成：检查 Python/Node → 首次运行建虚拟环境并装依赖 → 创建 `.env` → 生成样本教材 → 在**两个独立窗口**启动前后端 → 打开浏览器。

- 后端窗口标题：`智能助教 · 后端 :8000`
- 前端窗口标题：`智能助教 · 前端 :5173`
- **关闭这两个窗口即停止服务**

> **编码说明**：Windows 的 `.bat` 只有几行纯 ASCII，只负责调用 `scripts/start-web.ps1`；
> 所有中文提示与逻辑都在那个 `.ps1` 里（UTF-8 with BOM）。这样在**英文版 Windows**
> （代码页 437/1252）上也能正常解析和显示，不再依赖 GBK 代码页。
>
> **装成桌面应用**：用 Chrome/Edge 打开 `http://localhost:5173`，点地址栏右侧的「安装」图标，
> 之后就能像本地程序一样从开始菜单/桌面启动（独立窗口、无地址栏）。
> 注意必须是 `localhost`（PWA 要求安全上下文），用 `127.0.0.1` 不会出现安装入口。

> 首次运行需要下载依赖（约 3~5 分钟），之后再启动只需几秒。

---

### 手动启动（需要单独看日志或调端口时）

### 1. 后端

```bash
cd backend

# 创建虚拟环境（Python 3.12+）
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 复制环境变量模板
cp .env.example .env

# 启动（默认 8000 端口）
uvicorn app.main:app --reload --port 8000
```

访问 `http://127.0.0.1:8000/api/health` 应返回 `{"code":"OK", ...}`，`/docs` 有完整 OpenAPI。

### 2. 前端

```bash
cd frontend
npm install
npm run dev
```

访问 `http://localhost:5173`（**注意：vite 默认绑 `::1`，用 `localhost` 而非 `127.0.0.1`**）。

### 3. 生成样本教材（用于演示与测试）

```bash
cd backend
python scripts/make_sample_pdf.py
# 生成 data/sample_textbook.pdf（113 页，5 章 / 15 节 / 45 小节，3 级层级）
```

---

## 四、环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/app.db` | 换成 `postgresql+asyncpg://...` 即切换 PG + pgvector |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容端点（DeepSeek / 通义 / 本地 vLLM 都行） |
| `LLM_API_KEY` | 空 | **留空则进入演示模式**（内置确定性生成器），全链路仍可跑通 |
| `LLM_MODEL_ROUTER` | `gpt-4o-mini` | 轻量任务（路由/评估/答疑） |
| `LLM_MODEL_GENERATOR` | `gpt-4o` | 复杂 JSON AST 生成 |
| `GRADER_CONTENT_HIT_MIN_RATIO` | `0.45` | 实词命中率门槛，低于它判「课本未覆盖」。**换教材要重标** |
| `MAX_UPLOAD_MB` | 50 | 上传体积上限 |
| `MAX_PDF_PAGES` | 600 | 单份 PDF 页数上限 |
| `ARTIFACT_VERSION_KEEP` | 20 | Artifact 历史版本保留窗口 |

**演示模式说明**：`LLM_API_KEY` 为空时自动走内置 mock 生成器（纯函数、确定性、同输入同输出），UI 顶部会常驻黄色「演示模式」标识。这是为了让项目在没有任何 API Key 的机器上也能完整跑通验收链路。

---

## 五、测试

```bash
# 后端：244 个用例
cd backend && ./.venv/Scripts/python.exe -m pytest tests -q

# 前端：153 个用例
cd frontend && npm test

# 前端类型检查 + 构建
cd frontend && npm run build

# 端到端冒烟（需先启动服务在 8123 端口）
cd backend && ./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8123
python scripts/e2e_smoke.py
```

---

## 六、目录结构

```
.
├── HANDOVER.md                     # ★ 交接文档：回家三步走、代码地图、待办、踩坑
├── README.md                       # 你正在看的
├── 1. 启动网页端(Windows).bat       # 双击启动（纯 ASCII，调 scripts/start-web.ps1）
├── 2. 启动网页端(Mac-Linux).sh      # macOS/Linux 启动
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口
│   │   ├── config.py               # 配置（dual-mode DB、模型分层路由、各种阈值）
│   │   ├── errors.py               # 错误码枚举（前后端镜像）
│   │   ├── db/
│   │   │   ├── models.py           # 7 张表
│   │   │   ├── vector_store.py     # EmbeddingStore 协议 + Memory/PgVector 实现
│   │   │   └── repository.py       # 数据访问（版本 append-only）
│   │   ├── schemas/
│   │   │   ├── artifacts.py        # 三种 JSON AST 契约（核心）
│   │   │   ├── patch.py            # PatchOp / node_path 校验
│   │   │   ├── chat.py             # 8 类 SSE 事件契约
│   │   │   └── api.py              # DTO
│   │   ├── services/
│   │   │   ├── pdf_parser.py       # 逐页解析 + 章节号正则 + page_fallback
│   │   │   ├── chunker.py          # 按章节切片（section_code 恒非空）
│   │   │   ├── embedding.py        # 本地确定性 embedding（blake2b）
│   │   │   ├── retriever.py        # 混合检索 + 相关性评估
│   │   │   ├── text_signals.py     # 实词提取与命中率（覆盖判定）
│   │   │   ├── router.py           # 形态路由（可解释）
│   │   │   ├── mock_generator.py   # 确定性 mock（纯函数）
│   │   │   ├── llm_client.py       # OpenAI 兼容 + JSON 修复重试
│   │   │   ├── agent_engine.py     # CRAG 主流程 + 三种 AST 提示词契约
│   │   │   └── patch_engine.py     # deepcopy + 定点写入
│   │   └── api/                    # workspaces / chat_stream / artifacts
│   ├── tests/                      # 244 个用例（含 QA 验收测试）
│   └── scripts/make_sample_pdf.py
├── frontend/
│   ├── public/                     # PWA 静态资源
│   │   ├── manifest.webmanifest    # 安装清单（standalone / 深色主题）
│   │   ├── sw.js                   # Service Worker（排除所有 /api/ 缓存）
│   │   ├── offline.html            # 离线提示页
│   │   └── icons/                  # 192 / 512 / maskable-512 / SVG 源
│   └── src/
│       ├── types/                  # 与 Pydantic 严格对应的 TS 接口
│       ├── api/                    # client + 手写 SSE 解析器（fetch + ReadableStream）
│       ├── store/                  # Zustand
│       ├── components/
│       │   ├── layout/             # 双栏工作区
│       │   ├── chat/               # 对话、锚点、引用、目录树
│       │   └── canvas/             # ArtifactHost + 三种渲染器 + 6 布局
│       ├── hooks/                  # useChatStream / useArtifactPatch
│       └── lib/                    # jsonPointer / fingerprint / anim2d / scene3d / theme
├── docs/
│   ├── index.html                  # ★ 文档站入口（20 篇 HTML）
│   ├── 01-快速开始/ … 06-项目档案/  # 分类文档
│   ├── assets/style.css            # 文档站共享样式
│   ├── PRD.md                      # 12 条 P0 与可测试验收标准
│   ├── ARCHITECTURE.md             # 系统设计与任务分解
│   ├── SPEC.md                     # 需求方原始规范
│   ├── IMPL-NOTES.md               # 实施期裁定与架构勘误（**读这个能避开所有坑**）
│   └── QA-REPORT-backend.md        # 后端 P0 逐条验收结论
└── scripts/
    ├── start-web.ps1               # Windows 启动主逻辑（UTF-8 BOM，中文提示在这）
    ├── e2e_smoke.py                # 端到端冒烟（38 项，验流程正确性）
    └── real_llm_check.py           # 真实 LLM 链路验证（验内容质量，需 Key）
```

---

## 七、开发必读：`docs/IMPL-NOTES.md`

这份文档记录了实施期所有踩过的坑与裁定结论。**改动代码前建议先读**，尤其是这几条：

| 编号 | 结论 | 不遵守的后果 |
|---|---|---|
| **F1** | `update_node` 的 payload 是**局部字段**，必须字段级 merge | 整体替换会丢掉 `node_id`/`layout`/`title`，渲染器当场崩 |
| **E7** | 覆盖判定用「实词命中率 ≥ 0.45」，**不是分数阈值** | 分数没有排序语义，调阈值会同时误判和漏判 |
| **E8** | 断言「内容被改写」要区分「值不等」与「字节表示不等」 | 用保留键序的字节比对断言语义等值 → 假阳性 |
| **E10** | 用「删掉它」保证不残留，不如让「它根本不被创建」 | Windows 上句柄释放依赖 GC，`unlink` 会随机失败 |
| **§四.4** | `insert`/`delete` 的 changed_paths 声明到**父列表层** | 声明到元素层会让「非锚定不变」断言永远失效 |
| **§四.5** | `focus_camera` **不写入 spec** | 视角进 spec 会让同一内容因「看过哪个角度」产生不同指纹 |

---

## 八、已知限制

| 项 | 说明 |
|---|---|
| ~~真实 LLM 路径未验证~~ | **已补验（2026-10-26，DeepSeek）**。首次验证即暴露 2 个 P0（提示词缺 JSON Schema 导致降级 mock、`stream_text` 用错模型导致 TTFT 55 秒），均已修复。现实测 TTFT **2.9s**、`contract_retry` **0 次**、页码全部真实。回归脚本：`scripts/real_llm_check.py`。 |
| **真实模式总耗时约 70 秒** | TTFT 已达标（2.9s），但 5 页 PPT 要等 generator 出完才下发。优化方向：分批流式下发（先出前 2 页）或换更快档位。需先定「课件生成等待上限」。 |
| **PostgreSQL 模式未验证** | 全部测试跑在 SQLite。`PgVectorStore` 与 `vector(1536)` 镜像列无自动化覆盖。 |
| **grader 阈值在固定样本上标定** | 依据「18 条无关最高 0.40 / 16 条相关最低 0.50」。换教材（英文/马来文为主、或非历史学科）需重新标定，方法见 `docs/03-架构设计/4. 检索与路由.html`。 |
| **扫描版 PDF 不支持** | 只支持带文本层的 PDF。扫描件会返回明确错误提示，OCR 属 P1。 |
| **P0-06① 仅单元级证据** | 端到端验证会把「路由准确率」与「检索覆盖度」混在一起，故在路由 seam 上验（10/10）。 |
| **P0-07/08/09 前端交互未自动化** | 6 种布局渲染、拖时间轴 60 次不掉帧、3D 轨道旋转，目前只有人工验证。补 Playwright 可变成可回归的。 |
| **无登录体系** | 单用户设计，公网部署前必须加一层认证，否则任何人都能用你的 API Key 烧钱。 |

---

## 九、下一步可做（P1 需求池）

- PPT 导出 PDF / 图片
- 多文件跨文档对比（两本教材同主题对照）
- 学习进度追踪（章节已学/未学、掌握度模型）
- 错题本（随堂测验结果沉淀与重练）
- 扫描版 PDF 的 OCR 支持

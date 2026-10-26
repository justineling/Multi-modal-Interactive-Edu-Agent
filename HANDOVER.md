# 交接文档 · HANDOVER

> **给下班后的自己看。**目标：5 分钟内恢复完整上下文，直接开始干活。
> 最后更新：2026-10-26 · HEAD `3a7aa4b`

---

## 一、一句话现状

**P0 的 12 条需求全部交付，真实 DeepSeek 链路已验证通过，可以直接给人演示。** 剩下的是 P1 功能和几个已知优化项。

| 指标 | 数值 |
|---|---|
| 后端测试 | **244 passed / 0 failed** |
| 前端测试 | **153 passed / 7 文件** |
| 端到端冒烟 | **38 项全通过** |
| 真实 LLM TTFT | **2.9 秒**（验收标准 3 秒） |
| 入库文件 | 165 个 |
| 提交数 | 30 个原子 commit |

---

## 二、回家三步走

### 第 1 步：把代码拿到家

**项目目前没有远程仓库，只有本地 git。** 三种方式选一种：

| 方式 | 怎么做 | 适合 |
|---|---|---|
| **U 盘 / 移动硬盘拷整个文件夹** | 直接复制 `D:\Project\AI\多模态沉浸式智能助教` 整个目录 | **最省事**，连 `.venv`、`node_modules`、`.env`、上传的教材都带走了，到家直接双击启动 |
| **git bundle** | `git bundle create edu.bundle --all` → 拷这个单文件 → 到家 `git clone edu.bundle` | 只带代码历史，体积小，但要重新装依赖 |
| **推到 GitHub 私有库** | 新建私有仓库 → `git remote add origin ...` → `git push -u origin main` | 长期方案，推荐**

> ⚠️ **如果推公开仓库，先确认 `backend/.env` 没有被提交。** 它已在 `.gitignore` 第 25 行被忽略，里面有你的 DeepSeek API Key。推送前跑一次 `git ls-files | grep -i env` 确认只看到 `.env.example`。

### 第 2 步：跑起来

```bash
# Windows：双击
1. 启动网页端(Windows).bat

# macOS / Linux
chmod +x "2. 启动网页端(Mac-Linux).sh"
./"2. 启动网页端(Mac-Linux).sh"
```

脚本会自动装依赖、建配置、生成样本教材、开两个窗口、打开浏览器。

**如果是用 U 盘拷的整个文件夹**（含 `.venv` 和 `node_modules`）：到家后 `.venv` 里的 Python 路径可能失效（虚拟环境记录的是绝对路径）。如果启动报错，删掉 `backend/.venv` 重新跑脚本，它会重建。

### 第 3 步：配 API Key

**如果用 U 盘拷了整个文件夹**，`backend/.env` 已经在里面，Key 还在，不用动。

**如果是 git clone 的**，`.env` 不在版本库里，需要手动建：

```bash
cd backend
cp .env.example .env
```

然后编辑 `backend/.env`，填入：

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-你的key
LLM_MODEL_ROUTER=deepseek-flash
LLM_MODEL_GENERATOR=deepseek-v4-pro
```

**Key 从哪来**：DeepSeek 开放平台 https://platform.deepseek.com/api_keys

**不想配 Key 也能跑**：留空就是演示模式，用内置确定性生成器，全链路能跑通，只是产物内容是模板填充的（界面顶部会有黄色「演示模式」标识）。

---

## 三、这个项目在做什么

**对话驱动的富媒体生成式学习工作区。** 上传教材 PDF → 对话中自动生成 PPT / 2D 推演动画 / 3D 空间场景 → 点画布上任意一页、一个元素、一个部件追问，系统只改那一处。

**和普通 AI 聊天的三个本质区别：**

1. **严格锚定教材**：每条事实陈述带页码引用，检索不到就说「课本未覆盖」，**禁止编造页码**
2. **形态由系统判断**：离散知识点→PPT，随时间演变→2D 动画，需要空间想象→3D
3. **产物是活的**：点某一页追问，只改那一页，其余页面字节级不变

**核心设计原则**：**禁止 LLM 直接生成 HTML/Three.js 源码**。LLM 只填 JSON（三种 AST 契约），前端用确定性渲染器渲染。这样零编译等待、100% 不崩、天然可交互。

---

## 四、代码地图：想改什么去哪

### 后端 `backend/app/`

| 想改什么 | 去哪个文件 |
|---|---|
| 检索条数、各种阈值 | `config.py`（或 `backend/.env` 覆盖） |
| 「课本未覆盖」的判定严格度 | `config.py` 的 `GRADER_CONTENT_HIT_MIN_RATIO`（默认 0.45） |
| 章节号识别规则 | `services/pdf_parser.py` 的正则 |
| 「为什么生成 PPT」那句话 | `services/router.py` |
| 生成 PPT/2D/3D 的提示词 | `services/agent_engine.py` 的 `_ARTIFACT_CONTRACT` |
| 深度拓展的包装格式 | `services/agent_engine.py` 的 `_compose_reply` |
| 引用页码排序 | `services/agent_engine.py` 的 `_build_citations` |
| 加一个 API 接口 | `api/` 下对应文件 + 前端 `api/client.ts` |
| 错误码 | `errors.py` + 前端 `api/client.ts`（**两处必须同步**） |

### 前端 `frontend/src/`

| 想改什么 | 去哪个文件 |
|---|---|
| 配色、圆角、字号 | `lib/theme.ts` |
| PPT 某布局的外观 | `components/canvas/layouts/*.tsx` |
| 加一种 PPT 布局 | 新建 `layouts/XxxLayout.tsx` + 在 `slideLayouts.tsx` 注册 + 后端加 Literal |
| 消息里 `[p.94]` 的高亮 | `components/chat/pageRefs.ts` |
| 超纲拓展的视觉区分 | `components/chat/MarkdownView.tsx` |
| 锚点 chip 样式 | `components/chat/AnchorChip.tsx` |
| 2D 元素形状 | `components/canvas/Anim2DRenderer.tsx` |
| 3D 相机手感 | `components/canvas/Scene3DRenderer.tsx` |

### 文档

```
docs/index.html          ← 文档站入口（浏览器直接打开，21 个页面）
docs/IMPL-NOTES.md       ← ★ 踩坑记录与设计裁定，改代码前必读
docs/PRD.md              ← 12 条 P0 需求与验收标准
docs/ARCHITECTURE.md     ← 系统架构
docs/QA-REPORT-backend.md ← 后端验收报告
```

---

## 五、常用命令

```bash
# 启动（推荐用脚本，见第二节）

# 后端测试（约 30 秒）
cd backend && .venv/Scripts/python.exe -m pytest tests -q

# 只跑某个文件
cd backend && .venv/Scripts/python.exe -m pytest tests/test_retriever.py -q

# 前端测试（约 3 秒）
cd frontend && npm test

# 前端类型检查（不构建，快）
cd frontend && npm run typecheck

# 前端构建
cd frontend && npm run build

# 端到端冒烟（需先启动后端在 8123 端口）
cd backend && .venv/Scripts/python.exe -m uvicorn app.main:app --port 8123
# 另一个终端：
cd backend && .venv/Scripts/python.exe ../scripts/e2e_smoke.py

# 真实 LLM 链路验证（需配 Key，会消耗 token）
cd backend && .venv/Scripts/python.exe ../scripts/real_llm_check.py

# 重新生成样本教材
cd backend && .venv/Scripts/python.exe scripts/make_sample_pdf.py
```

---

## 六、待办清单（按优先级）

### P1：值得优先做

| # | 任务 | 说明 | 预估 |
|---|---|---|---|
| 1 | **PPT 导出 PDF** | 学生想打印/交作业。前端渲染器已就绪，加 `html2canvas` + `jsPDF` 或 `print` 样式即可 | 半天 |
| 2 | **扫描版 PDF 的 OCR** | 当前明确拒绝，这是最大的使用门槛。可选 PaddleOCR / Tesseract | 2-3 天 |
| 3 | **真实模式总耗时优化** | 现在 5 页 PPT 要等约 70 秒（TTFT 已达标 2.9s）。方向：分批流式下发（先出前 2 页）或 generator 换更快档位 | 1-2 天 |
| 4 | **跨文档对比** | 两本教材同主题对照 | 3 天 |
| 5 | **学习进度追踪 / 错题本** | 需要先定义掌握度模型 | 3-5 天 |

### P2：远期

- 语音输入提问
- 多人协作（同工作区共同编辑画布）
- 教师端班级看板
- 移动端适配优化

### 技术债

| 项 | 说明 |
|---|---|
| **PostgreSQL 模式未验证** | 全部测试跑在 SQLite。代码支持，但 `PgVectorStore` 没实测 |
| **grader 阈值需换教材重标** | 0.45 是基于一份 KSSM 历史教材标的。换英文/马来文教材或换学科必须重标，方法见 `docs/03-架构设计/4. 检索与路由.html` 最后一节 |
| **P0-07/08/09 的前端交互未自动化** | 6 种布局渲染、拖时间轴 60 次不掉帧、3D 轨道旋转，目前只有人工验证。补 Playwright 可变成可回归的 |
| **无登录体系** | 单用户设计，公网部署前必须加一层认证 |

---

## 七、六个必须知道的坑

这些都是踩过的，写在这里省你重新踩一遍。完整版见 `docs/IMPL-NOTES.md`。

### 1. `update_node` 的 payload 是局部字段，必须字段级 merge

```ts
// ❌ 整体替换 → layout 变 undefined → 渲染器当场崩
setAtPath(spec, "/slides/2", payload)

// ✅ 字段级 merge
setAtPath(spec, "/slides/2", { ...current, ...payload })
```

后端只发被修改的字段（实测常是 `{bullets, speaker_notes}`），其余 9 个字段不带。

### 2. 判断"课本有没有覆盖"不能看分数

分数没有排序语义。「怎么用 Rust 写 WebAssembly」的 `bm25_norm` 是 0.998、总分 0.4016，**比真正需要拓展的「Malayan Union 对宪制发展的长期影响」（0.3605）还高**。必须用「实词命中率」这个独立信号，阈值 0.45。

**换教材必须重标这个阈值**，否则误判率会飙升。

### 3. 契约演进只能"加"，不能"改"

历史版本的 spec 是 append-only 的，改了已有字段的语义，所有旧数据都会失效且无法批量迁移。新增字段必须是可选 + 有默认值。

### 4. 写版本快照必须 `copy.deepcopy`

SQLAlchemy 的 `JSON` 类型**不追踪嵌套原地修改**。如果快照和 `artifact.spec_json` 共享同一个 dict 对象，后续修改会连带改脏历史行。

### 5. `vite preview` 和 `vite dev` 的代理是两套配置

PWA 安装验证必须在 `vite preview`（生产构建）下做。如果代理只配了 `server` 没配 `preview`，会出现**「开发时一切正常，装成桌面应用后所有接口 404」**——极难归因。

### 6. 批处理文件里不要放中文

`.bat` 存 UTF-8 → 中文 Windows 上按 GBK 解析、整行截断、命令错乱；存 GBK → 英文 Windows 上乱码。
**当前方案**：`.bat` 纯 ASCII 只做引导，中文和逻辑全在 `scripts/start-web.ps1`（UTF-8 with BOM）。

---

## 八、关键决策记录（为什么这么设计）

| 决策 | 理由 |
|---|---|
| 禁止 LLM 写渲染代码 | 十次有八次出问题（穿模、材质丢失、语法崩溃），且服务端渲染要 30-60 秒 |
| 默认 SQLite 而非 PG | 零依赖开箱即跑；`DATABASE_URL` 一行切换生产配置 |
| 内存向量检索而非 Milvus | 千级 chunk 量级下暴力检索毫秒级，不用维护独立集群 |
| 默认 `syllabus_strict` 模式 | 核心用户是备考学生，"考试不答偏"优先于"讲得丰富" |
| 用 PWA 而非 Tauri | PWA 零构建链、半小时配置；Tauri 要打包 Python（80-150MB）+ 维护跨平台链。**在"用户要桌面感还是真离线"未验证前，PWA 成本最低** |
| 演示模式（无 Key 也能跑） | 让项目在任何机器上都能完整跑通验收链路，不被 API Key 卡住 |

---

## 九、遇到问题怎么查

**顺序很重要，先定位是前端还是后端：**

1. `curl http://127.0.0.1:8000/api/health` —— 后端活着吗
2. `curl -N -X POST http://127.0.0.1:8000/api/chat/stream -H "Content-Type: application/json" -d '{"workspace_id":"xxx","query":"测试","mode":"syllabus_strict"}'` —— 看原始 SSE 流
3. 浏览器 F12 → Console 有没有红色报错
4. 跑一遍 `pytest` —— 基线是不是绿的（如果基线红了，先修测试，说明基础假设被破坏了）
5. 跑 `scripts/e2e_smoke.py` —— 38 项里哪项 FAIL 就定位到具体环节

**完整排查手册**：`docs/05-运维部署/2. 故障排查.html`

---

## 十、下次开工的第一件事

```bash
# 1. 确认代码状态
git log --oneline -3
git status

# 2. 跑一遍测试确认基线是绿的
cd backend && .venv/Scripts/python.exe -m pytest tests -q

# 3. 启动看看
双击 "1. 启动网页端(Windows).bat"
```

三条都正常 → 直接挑一件 P1 任务开工。
任何一条不对 → 先看第七节的坑，再查第九节的排查顺序。

**改代码前先读 `docs/IMPL-NOTES.md`。** 那里面记的是"为什么这么写"，比"写了什么"更能防止你把已经修好的 bug 改回去。

---

## 附：这次做完的东西清单

```
✅ 后端完整实现（FastAPI + 7 张表 + 三种 AST 契约 + CRAG 检索 + 增量补丁 + SSE）
✅ 前端完整实现（双栏工作区 + 6 种 PPT 布局 + 2D 动画 + 3D 场景 + 锚点追问）
✅ 244 后端测试 + 153 前端测试 + 38 项端到端冒烟
✅ 真实 DeepSeek 链路验证（TTFT 2.9s、5-6 页 5-6 布局、页码真实、patch 仅改目标子树）
✅ 一键启动脚本（Windows .bat + PowerShell / macOS-Linux .sh）
✅ PWA 桌面安装（manifest + 图标 + Service Worker + 离线页）
✅ 20 篇 HTML 文档站 + 5 篇 Markdown 源文档
```

**已知限制**（诚实清单）：
- 真实模式总耗时约 70 秒（TTFT 已达标，整份产物要等）
- PostgreSQL 模式未实测
- 扫描版 PDF 不支持
- 无登录体系，公网部署前必须加
- 6 种 PPT 布局的渲染质量只做了人工验证，没有自动化回归

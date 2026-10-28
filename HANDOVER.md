# 交接文档 · HANDOVER

> **给下班后的自己看。**目标：5 分钟内恢复完整上下文，直接开始干活。  
> 最后更新：2026-10-26 · HEAD `5cc1d6a`

---

## 一、一句话现状

**P0 的 12 条需求全部交付，真实 DeepSeek 链路已验证；P1-03（真实模式耗时优化）也已完成，整轮 66.3s → 18.13s。** 可以直接给人演示。

| 指标          | 数值                                                |
| ----------- | ------------------------------------------------- |
| 后端测试        | **356 passed / 0 failed / 0 skipped**             |
| 前端测试        | **153 passed / 7 文件**                             |
| 端到端冒烟       | **31 项全通过**（注：原记录「38 项」为误记）                       |
| 真实 LLM TTFT | **4.14s**（P50，n=15；门禁 ≤ 5.08s）                    |
| 真实 LLM 整轮   | **18.13s**（P50，n=15；目标 18s，**超 0.7%**；P90 24.54s） |
| 演示模式 TTFT   | 7ms                                               |
| 提交数         | 35 个原子 commit                                     |

> 若这些数字对不上，先跑一遍测试确认基线 —— **基线不绿时不要动代码**，那说明基础假设被破坏了。

---

## 二、回家三步走

### 第 1 步：把代码拿到家

| 方式             | 怎么做                                                                  | 适合                                               |
| -------------- | -------------------------------------------------------------------- | ------------------------------------------------ |
| **拷整个文件夹**     | 直接复制整个项目目录                                                           | **最省事**，连 `.venv`、`node_modules`、`.env`、上传的教材都带走 |
| **git bundle** | `git bundle create edu.bundle --all` → 拷单文件 → `git clone edu.bundle` | 只带代码历史，体积小，但要重装依赖                                |
| **推到私有仓库**     | `git remote add origin ...` → `git push -u origin main`              | 长期方案                                             |

> ⚠️ **推公开仓库前先确认 `backend/.env` 没有被提交**（已在 `.gitignore` 忽略，里面有你的 API Key）。推送前跑一次 `git ls-files | grep -i env`，只应看到 `.env.example`。

### 第 2 步：跑起来

```bash
# Windows：双击
1. 启动网页端(Windows).bat

# macOS / Linux
chmod +x "2. 启动网页端(Mac-Linux).sh" && ./"2. 启动网页端(Mac-Linux).sh"
```

脚本会自动装依赖、建配置、生成样本教材、开两个窗口、打开浏览器。

**如果是 U 盘拷的整个文件夹**：`.venv` 记录的是绝对路径，换机器后可能失效。报错就删掉 `backend/.venv` 重跑脚本，它会重建。

### 第 3 步：配 API Key

```bash
cd backend && cp .env.example .env
```

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-你的key
LLM_MODEL_ROUTER=deepseek-flash        # 轻量档：正文流式
LLM_MODEL_ARTIFACT=deepseek-flash      # 产物档位（P1-03 新增，留空跟随 GENERATOR）
LLM_MODEL_GENERATOR=deepseek-v4-pro    # 重档：留作回退目标
```

Key 从 <https://platform.deepseek.com/api_keys> 取。

**不想配 Key 也能跑**：留空即演示模式，用内置确定性生成器，全链路可跑通，界面顶部有黄色「演示模式」标识。

---

## 三、这个项目在做什么

**对话驱动的富媒体生成式学习工作区。** 上传教材 PDF → 对话中自动生成 PPT / 2D 推演动画 / 3D 空间场景 → 点画布上任意一页、一个元素、一个部件追问，系统只改那一处。

**和普通 AI 聊天的三个本质区别：**

1. **严格锚定教材**：每条事实陈述带页码引用，检索不到就说「课本未覆盖」，**禁止编造页码**
2. **形态由系统判断**：离散知识点→PPT，随时间演变→2D 动画，需要空间想象→3D
3. **产物是活的**：点某一页追问，只改那一页，其余页面字节级不变

**核心设计原则**：**禁止 LLM 直接生成 HTML/Three.js 源码**。LLM 只填 JSON（三种 AST 契约），前端用确定性渲染器渲染。零编译等待、100% 不崩、天然可交互。

---

## 四、代码地图：想改什么去哪

### 后端 `backend/app/`

| 想改什么                   | 去哪个文件                                                                                              |
| ---------------------- | -------------------------------------------------------------------------------------------------- |
| 检索条数、各种阈值              | `config.py`（或 `backend/.env` 覆盖）                                                                   |
| 「课本未覆盖」的判定严格度          | `config.py` 的 `GRADER_CONTENT_HIT_MIN_RATIO`（默认 0.45）                                              |
| 章节号识别规则                | `services/pdf_parser.py` 的正则                                                                       |
| 「为什么生成 PPT」那句话         | `services/router.py`                                                                               |
| 生成 PPT/2D/3D 的提示词与契约   | `services/agent_engine.py` 的 `_ARTIFACT_CONTRACT`、`_artifact_prompt`                               |
| **页码注入与钳制**            | `agent_engine.py` 的 `_coerce_page` / `_fix_source_page` / `_clamp_spec_pages` / `_clamp_patch_ops` |
| **并行 fork/join 与产物分支** | `agent_engine.py` 的 `ArtifactWork` / `_artifact_work` / `_join_artifact_work` / `_commit_artifact` |
| 深度拓展的包装格式              | `services/agent_engine.py` 的 `_compose_reply`                                                      |
| 引用页码排序                 | `services/agent_engine.py` 的 `_build_citations`                                                    |
| 加一个 API 接口             | `api/` 下对应文件 + 前端 `api/client.ts`                                                                  |
| 错误码                    | `errors.py` + 前端 `api/client.ts`（**两处必须同步**）                                                       |

### 前端 `frontend/src/`

| 想改什么             | 去哪个文件                                                              |
| ---------------- | ------------------------------------------------------------------ |
| 配色、圆角、字号         | `lib/theme.ts`                                                     |
| PPT 某布局的外观       | `components/canvas/layouts/*.tsx`                                  |
| 加一种 PPT 布局       | 新建 `layouts/XxxLayout.tsx` + 在 `slideLayouts.tsx` 注册 + 后端加 Literal |
| 消息里 `[p.94]` 的高亮 | `components/chat/pageRefs.ts`                                      |
| 超纲拓展的视觉区分        | `components/chat/MarkdownView.tsx`                                 |
| 锚点 chip 样式       | `components/chat/AnchorChip.tsx`                                   |
| 2D 元素形状          | `components/canvas/Anim2DRenderer.tsx`                             |
| 3D 相机手感          | `components/canvas/Scene3DRenderer.tsx`                            |

### 文档

```
README.md                     ← 项目入口（是什么 / 怎么跑 / 现状）
docs/index.html               ← 文档站入口（21 篇 HTML，浏览器直接打开）
docs/IMPL-NOTES.md            ← ★ 踩坑记录与设计裁定，改代码前必读
docs/increments/              ← P1-03 增量文档（PRD / 设计 / 实测 / QA 报告 + 原始数据）
docs/PRD.md                   ← 12 条 P0 需求与验收标准
docs/ARCHITECTURE.md          ← 系统架构
```

---

## 五、最近一次改动：P1-03 真实模式耗时优化

**问题**：整轮 66.3 秒，右侧画布空白近 70 秒。

**实测结论**（`docs/increments/latency-profile.md`）：66.3s 里 **83.7%（55.5s）是一次 `complete_json`**，其中 36–54 秒是模型「思考」（`reasoning_content`）；检索+路由+引用+落库**合计不到 14 毫秒**。

**做了什么**：

| 改动                                        | 效果                             |
| ----------------------------------------- | ------------------------------ |
| 产物生成档位独立可配（`LLM_MODEL_ARTIFACT`，默认 flash） | 55.5s → 10.1s，契约通过率不变          |
| 正文与产物并行（fork 在 TTFT 定格之后，产物分支不落库不发事件）     | 再省约 10.8s                      |
| **顺带修复 P0-05④ 页码锚定缺陷**                    | 事实带来源页注入 + 契约示例去硬编码 + 生成后确定性钳制 |

**结果**：66.3s → **18.13s**（−73%）。目标 18s，**超 0.7%**；P90 24.54s。

**被数据否掉的四个方案**（别再试一遍）：

| 方案            | 实测结论                                                        |
| ------------- | ----------------------------------------------------------- |
| 缩短提示词 / 契约    | **收益 ≈ 0**。TTFB 仅 243–535ms，`prompt_tokens` 1005，慢的是推理不是预填充 |
| `effort=low`  | **反而慢 17%**（67.0s vs 57.4s），推理量没降反升                         |
| 分批下发（先出前 2 页） | **换档后边际收益趋零**。页数与耗时是次线性（2 页 36.5s vs 5 页 57.4s），瓶颈是思考不是输出长度 |
| 减页数提速         | **反而更慢**。页数与 token 不单调：5 页 3114 token、6 页 1843、**7 页 1321** |

**还没解决的**：见第七节「必踩的坑」第 7–9 条。

---

## 六、待办清单（按优先级）

### P1

| # | 任务                | 说明                                                                                                                                                    |
| - | ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 | **压产物长尾**         | 唯一能同时改善 T4 的 P50 与 P90 的杠杆（产物 span P90 18.7s）。候选 A：约束讲解字数（`speaker_notes` 2–3 句→1–2 句），预计省 2.9–3.5s，**需产品确认「讲解变短可接受」**。方案与失效模式见 `fork-mode-ab.md` 第八节 |
| 2 | **逐页页码对应**        | 现在所有 slide 倾向共用第一条事实的页码（`max_same_page_ratio` 91.4%）。加提示词规则实测无效已回退，真修要让 slide 与 fact 建立结构对应                                                           |
| 3 | **修建工作区竞态**       | 返回 200 后事务未提交，13/300 偶发 404。把 commit 移到 `yield` 之前或用显式事务边界                                                                                            |
| 4 | **补页数后校验**        | `slides` 是 `min_length=1`，页数约束只在提示词里。加 `max_tokens` 前必须先补                                                                                             |
| 5 | **扫描版 PDF 的 OCR** | 当前明确拒绝，这是最大的使用门槛                                                                                                                                      |
| 6 | **PPT 导出 PDF**    | 学生想打印/交作业                                                                                                                                             |
| 7 | **补测 patch 换档耗时** | T5 门禁「绝对值 ≤15s」仍是推断（约 7–9s），需 1–2 个真实回合实测                                                                                                             |

### P2

- 跨文档对比（两本教材同主题对照）· 学习进度追踪 / 错题本
- 语音输入 · 多人协作 · 教师端班级看板 · 移动端适配

### 技术债

| 项                        | 说明                                                                            |
| ------------------------ | ----------------------------------------------------------------------------- |
| **PostgreSQL 模式未验证**     | 全部测试跑在 SQLite，`PgVectorStore` 没实测                                             |
| **grader 阈值需换教材重标**      | 0.45 基于一份 KSSM 历史教材标定。换英文/马来文教材或换学科必须重标，方法见 `docs/03-架构设计/4. 检索与路由.html` 最后一节 |
| **P0-07/08/09 前端交互未自动化** | 6 种布局渲染、拖时间轴、3D 轨道旋转只有人工验证。补 Playwright 可变成可回归的                               |
| **无登录体系**                | 单用户设计，公网部署前必须加一层认证                                                            |
| **从未部署过**                | 无目标环境，TTFT 绝对达标只能记「未验证」                                                       |
| **HTML 文档站滞后**           | 新增的配置项未同步到 `docs/01-*`、`docs/05-*`，运维以 `.env.example` 与 `IMPL-NOTES E12` 为准   |

---

## 七、必踩的坑

完整版见 `docs/IMPL-NOTES.md`。这里是最高频的几条：

### 1. `update_node` 的 payload 是局部字段，必须字段级 merge

```ts
// ❌ 整体替换 → layout 变 undefined → 渲染器当场崩
setAtPath(spec, "/slides/2", payload)

// ✅ 字段级 merge
setAtPath(spec, "/slides/2", { ...current, ...payload })
```

后端只发被修改的字段（实测常是 `{bullets, speaker_notes}`），其余 9 个字段不带。

### 2. 判断「课本有没有覆盖」不能看分数

分数没有排序语义。「怎么用 Rust 写 WebAssembly」的 `bm25_norm` 是 0.998、总分 0.4016，**比真正需要拓展的问题还高**。必须用「实词命中率」这个独立信号，阈值 0.45。**换教材必须重标。**

### 3. 契约演进只能「加」，不能「改」

历史版本的 spec 是 append-only 的，改了已有字段的语义，所有旧数据都会失效且无法批量迁移。新增字段必须是可选 + 有默认值。

### 4. 写版本快照必须 `copy.deepcopy`

SQLAlchemy 的 `JSON` 类型**不追踪嵌套原地修改**。快照和 `artifact.spec_json` 共享同一个 dict 对象时，后续修改会连带改脏历史行。

### 5. `vite preview` 和 `vite dev` 的代理是两套配置

PWA 安装验证必须在 `vite preview`（生产构建）下做。只配了 `server` 没配 `preview`，会出现「开发时一切正常，装成桌面应用后所有接口 404」。

### 6. 批处理文件里不要放中文

`.bat` 存 UTF-8 → 中文 Windows 按 GBK 解析、整行截断；存 GBK → 英文 Windows 乱码。**当前方案**：`.bat` 纯 ASCII 只做引导，中文和逻辑全在 `scripts/start-web.ps1`（UTF-8 with BOM）。

### 7. 并行化后：`artifact` 事件必须在落库之后

前端收到 `artifact` 事件后会拉一次权威快照覆盖本地。若事件早于 commit，刚贴上去的内容会被旧快照擦掉 —— 表现为「页面闪一下又消失」。

当前保证：**产物分支不持有 session、不调用任何写路径**（是「不写」而非「不能写」，`_session_factory` 语言层面可达），由 commit 计数测试兜底。

### 8. `CancelledError` 是 `BaseException`

`finally` 里只能 `suppress(asyncio.CancelledError)`。谁把它改成 `except Exception`，「子任务被取消」就静默失效。

### 9. 页码钳制要处理非 int 形态

`source_page` 是 `int | None`，但 pydantic lax 模式接受 `"999"` / `999.0` / `true`。只判 `isinstance(page, int)` 会整条跳过钳制，越界页码原样落库。**`bool` 必须放在 `int` 之前判** —— `isinstance(True, int)` 为真。

### 10. 测量脚本依赖的「内部约定」也是契约

`scripts/profile_latency.py` 与 `flash_quality_check.py` 靠运行期反射取引擎内部状态（方法名、`stage` 取值、error 与失败调用的 1:1 配对）。**破坏这些约定不会报错**，只会让指标变成 0% 或空列表。见 `IMPL-NOTES` §3.3.4 的 MC-1 ~ MC-5。

---

## 八、关键决策记录（为什么这么设计）

| 决策                          | 理由                                                                                |
| --------------------------- | --------------------------------------------------------------------------------- |
| 禁止 LLM 写渲染代码                | 十次有八次出问题（穿模、材质丢失、语法崩溃），且服务端渲染要 30-60 秒                                            |
| 默认 SQLite 而非 PG             | 零依赖开箱即跑；`DATABASE_URL` 一行切换生产配置                                                   |
| 内存向量检索而非 Milvus             | 千级 chunk 量级下暴力检索毫秒级，不用维护独立集群                                                      |
| 默认 `syllabus_strict` 模式     | 核心用户是备考学生，「考试不答偏」优先于「讲得丰富」                                                        |
| 用 PWA 而非 Tauri              | PWA 零构建链；Tauri 要打包 Python（80-150MB）+ 维护跨平台链                                       |
| 演示模式（无 Key 也能跑）             | 让项目在任何机器上都能完整跑通验收链路，不被 API Key 卡住                                                 |
| **产物用轻量档**（P1-03 推翻原裁定）     | 原裁定「spec 用重档保准确率」没有实测支撑。实测 11 回合契约通过率相同、耗时 55.5s→10.1s。**护栏改成「retry 率」而不是「模型档位」** |
| **fork 在首个 token 之后**（而非立即） | 产物请求在 TTFT 定格前根本没被构造，TTFT **可证不变**。代价 T4 +3.9s，换来 P0 红线指标的确定性                     |
| **页码钳制而不是后校验拒绝**            | 拒绝会触发重试，一次重试 = 一次完整调用（约 10s），两次吃掉全部收益；且 E11 证明回灌错误会让模型越改越乱                        |

---

## 九、遇到问题怎么查

**顺序很重要，先定位是前端还是后端：**

1. `curl --noproxy '*' http://127.0.0.1:8000/api/health` —— 后端活着吗（本机设了 `http_proxy`，**必须加 `--noproxy`**）
2. `curl -N --noproxy '*' -X POST http://127.0.0.1:8000/api/chat/stream -H "Content-Type: application/json" -d '{"workspace_id":"xxx","query":"测试","mode":"syllabus_strict"}'` —— 看原始 SSE 流
3. 浏览器 F12 → Console 有没有红色报错
4. 跑一遍 `pytest` —— 基线是不是绿的（基线红了先修测试）
5. 跑 `scripts/e2e_smoke.py` —— 31 项里哪项 FAIL 就定位到具体环节

**完整排查手册**：`docs/05-运维部署/2. 故障排查.html`

**性能问题先看这里**：`docs/increments/latency-profile.md`（耗时分解方法）与 `scripts/profile_latency.py`（可复跑的剖析脚本）。

---

## 十、下次开工的第一件事

```bash
# 1. 确认代码状态
git log --oneline -3
git status

# 2. 跑一遍测试确认基线是绿的
cd backend && ./.venv/Scripts/python.exe -m pytest tests -q    # 期望 356 passed
cd ../frontend && npm test                                      # 期望 153 passed

# 3. 启动看看
双击 "1. 启动网页端(Windows).bat"
```

三条都正常 → 直接挑一件 P1 任务开工。  
任何一条不对 → 先看第七节的坑，再查第九节的排查顺序。

**改代码前先读 `docs/IMPL-NOTES.md`。** 那里面记的是「为什么这么写」，比「写了什么」更能防止你把已经修好的 bug 改回去。

---

## 附：已完成清单

```
✅ 后端完整实现（FastAPI + 7 张表 + 三种 AST 契约 + CRAG 检索 + 增量补丁 + SSE）
✅ 前端完整实现（双栏工作区 + 6 种 PPT 布局 + 2D 动画 + 3D 场景 + 锚点追问）
✅ 356 后端测试 + 153 前端测试 + 31 项端到端冒烟
✅ 真实 DeepSeek 链路验证（TTFT 4.14s、页码可回溯、patch 仅改目标子树）
✅ 一键启动脚本（Windows .bat + PowerShell / macOS-Linux .sh）
✅ PWA 桌面安装（manifest + 图标 + Service Worker + 离线页）
✅ 21 篇 HTML 文档站 + 5 篇 Markdown 源文档
✅ P1-03 耗时优化：66.3s → 18.13s（产物换轻量档 + 正文/产物并行）
✅ P0-05④ 页码锚定修复（不再编造区间外页码；逐页对应仍未达成）
```

**已知限制**（诚实清单）：

- 完整产物 18.13s，未达 18s 目标（超 0.7%），P90 24.54s
- 逐页页码对应未达成（`max_same_page_ratio` 91.4%）
- 建工作区有 4.3% 竞态（改动前就有）
- 页数无 schema 后校验
- PostgreSQL 模式未实测
- 扫描版 PDF 不支持
- 无登录体系，公网部署前必须加
- 从未部署过，TTFT 绝对达标未验证

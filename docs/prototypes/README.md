# rhine_ui_rebuild · 单页原型（非生产）

> **非生产原型，不可用于真人测试。**
> 用户 2026-11-07 拍板：Q-1 = 「外壳 + 装裱层」中间态；Q-2 = 「先看单页原型，生产代码一行不动」。
> 本目录只用于观感评审，回答一个问题：**「档案外壳 + 彩色课件内页」这个双语感成不成立**。
> 项目处于测试期，A 类 6 项真人测试尚未跑；本原型**不进 `frontend/src`**、不改任何生产代码、不代表功能已完成。

---

## 0. 一句话：本原型证明的是「布局反转」，不是换皮

现有 `SplitWorkspace.tsx` 是 `TopBar` + `Allotment` **左聊天 42% / 右画布 58%**，聊天主导、画布是预览。
本原型把它反转为 **`索引 220px / 卷宗页(画布) 1fr / 调阅流水 320px`** 三栏档案终端：
**画布升为主内容栏，对话与输入退到右侧 320px 检视栏**——学生真正要读的是那一页产物，对话只是产生它的调阅流水。

代码里真实成立，不是把双栏刷成米色：

| 约束 | 落地位置 |
|---|---|
| `.rl-shell` 只含 3 个直接子元素 `<header>` / `<main>` / `<footer>` | `index.html` 顶层 `.rl-shell`；模态框与遮罩是**独立视图层**，放在 `.rl-shell` 之外 |
| `auto 1fr auto` 三层骨架 | `rhine-lab.css` 的 `.rl-shell { grid-template-rows: auto 1fr auto }`，`prototype.css` 不覆盖 |
| 三栏 `220 / 1fr / 320px` | `rhine-lab.css` 的 `.rl-split--three` |
| 两条对齐线（gutter 59px / viewport - gutter） | `--rl-gutter: 59px`；品牌块、kicker、面包屑、页签、页眉页脚、底部刻度全部左对齐左线，编号/页码/版本右对齐右线；**无第三个对齐位置**，无 `margin: 0 auto` 居中 |
| 分栏靠 72px 留白，不画竖线 | `prototype.css` 的 `.proto-main { column-gap: var(--rl-space-8) }`；三栏之间**无任何 `border-left/right`**；竖线仅两处例外：`.rl-tree ul` 缩进脊线、`.rl-ticks` 的 1px 刻度竖线 |
| 1px `--rl-line` 页框、直角、无投影、不铺灰底 | `prototype.css` 的 `.proto-frame`（覆写 `.rl-panel` 的浅底/柔和边线为 `1px solid var(--rl-line)` + `background: transparent`） |

**装裱层（PRD §4）**：页框属 A 层；`FILE X-003 / 01/05 / [p.94] / DOC-001` 全部落在**页框之外**的页眉页脚；
框内只放中性占位（一行大写拉丁 + 一行中文），**不渲染课件、不写任何 PALETTE 蓝/青/红绿 hex 到 A 层**。

交付措辞（PRD §4.7，可直接引用）：

> 档案外壳负责第几份、第几页、出自哪里，彩色课件页负责这一页教什么；如同纸质卷宗里夹着一页彩色插图，装帧语言与原件内容各司其职，不互相染色。

---

## 1. 逐文件登记（含审计豁免，逐文件显式列出，不悄悄排除）

审计根目录 = **`docs/prototypes/rhine-archive/`**。命令见 §3。

### 1.1 进审计（5 个文件，全部在 `rhine-archive/` 内）

| # | 文件 | 角色 | 是否进审计 | 说明 |
|---|---|---|---|---|
| 1 | `rhine-archive/index.html` | A 层主原型：品牌块 / kicker / 三栏 / 页框 / 页眉页脚 / 七类编号 / 唯一墨块 / ticks / live 点 / 模态框 | **是** | 相对路径引用同目录 CSS/JS，`<script defer>`（非 module），`file://` 双击可开 |
| 2 | `rhine-archive/rhine-lab.css` | skill 自带设计系统样式表（上一实例复制，**本实例未改动一字**） | **是** | 提供 token、直角、`:focus-visible`、`prefers-reduced-motion`、三栏骨架 |
| 3 | `rhine-archive/rhine-lab.js` | skill 自带交互层（上一实例复制，**本实例未改动一字**） | **是** | `data-*` 自动初始化：主题 / 页签 / 模态 / 刻度 / 检索 / 行选中，零依赖 |
| 4 | `rhine-archive/prototype.css` | 装裱层补充样式 | **是** | 只复用 `--rl-*` token，零新增色值 / 零 `box-shadow` / 零 `border-radius` / 零新动效 |
| 5 | `rhine-archive/prototype.js` | 原型本地交互层 | **是** | 默认亮色基准、目录树选中、刻度轨联动页序、Composer 本地回执；**不发任何网络请求** |

> 两个 `<link rel="stylesheet">`（`rhine-lab.css` / `prototype.css`）都在扫描目录内，因此**不会**触发脚本的
> 「样式表在扫描目录之外，未审计」警告；本次审计 0 warning，不是靠放宽判据蒙混。

### 1.2 不进审计（豁免，逐文件显式登记理由）

| # | 文件 | 角色 | 是否进审计 | 豁免理由（统一表述） |
|---|---|---|---|---|
| 6 | `b-layer-reference.html` | B 层彩色对照：8 色 `PALETTE` 色卡 + 抽象几何色块 + 装裱并置示意 + 6 种布局抽象骨架 | **否 —— 显式豁免** | 位于 `docs/prototypes/` 根，**在审计根目录 `rhine-archive/` 之外**。它必须原样展示 `PALETTE` 的 `#2563EB / #0EA5E9` 蓝青与 `#EF4444 / #10B981` 语义红绿：这些色值受 `backend/app/schemas/artifacts.py::PALETTE` 与 LLM JSON AST 契约约束（与 `frontend/src/lib/theme.ts::PALETTE` 逐字一致，Konva / Three 不认 CSS 变量）。与 `SKILL.md` 铁律 1「不用蓝 / 青 / 语义红绿」的冲突属**预期且经 Q-1 拍板接受**，**不是待修违规**。已按 PRD §4.8 第 3 条做 A/B 单文件拆分，A 层单独可跑 `--strict` 且 0 / 0 |
| 7 | `README.md` | 本登记文件 | **否 —— 非前端文件** | `.md` 不在审计脚本 `SUPPORTED` 扩展名集合内，且位于审计根目录之外；不参与扫描 |

**不得悄悄排除的声明**：

- 若把审计根目录改成 `docs/prototypes/`（父目录），脚本会**明确报出** B 层的蓝/青 error —— 这正是预期，不是「假装通过」。
  我们**没有**把 B 层悄悄移出扫描范围后宣称全绿：本文件 §1.2 逐文件点名、写明冲突来源与拍板依据，且 §3 只声明 `rhine-archive/` 的审计结论。
- 若把 B 层文件搬进 `rhine-archive/`，`--strict` 会立刻失败（铁律 1）。这是设计上的**有意边界**，不是遗漏。
- B 层对照页只有**纯 CSS 抽象几何色块**，页面三处醒目标注「示意 · 非真实产物」；
  **无官方 CDN 素材、无品牌标识、无角色立绘、无 AI 生图、无图标库、无 emoji**，符合 `legal.md` 与 PRD §4.8 第 4 条。
- A 层 `index.html` 内有**显著链接**指向 B 层（`.proto-blink` 区块的 `.rl-action`），供用户并排判断双语感。

---

## 2. 7 类编号落位（各 ≥1 次，全部 `font-variant-numeric: tabular-nums`）

`prototype.css` 在 `.rl` 根上统一设 `tabular-nums`（继承覆盖全部后代），另对 `.proto-code` / `.proto-turn` 显式重申。

| 编号 | 格式 | 绑定的真实对象 | 落位 |
|---|---|---|---|
| `DOC-001` | `DOC-###` | `document_id`（上传的教材 PDF） | kicker 第 2 槽、面包屑、目录树根、`.rl-meta` `DOC ID`、页眉右、检视栏 `CITATION / DOC`、文件抽屉、模态框 |
| `FILE X-003` | `FILE X-###` | artifact id（一份课件产物） | kicker 第 2 槽、面包屑、页眉左、检视栏 kicker 右、页脚、调阅流水 `T-001` 行 |
| `01/05` | `##/##` | `SlideRenderer` 当前页 / 总页 | 面包屑末项、页框页脚 `.rl-readout`、刻度轨标签、锚点片、版本列表、页脚、模态框、`data-page-readout` 联动 |
| `T-003` | `T-###` | `turn_id` / 流水序号 | 检视栏 `T-001 / T-002 / T-003` 行首（`.proto-turn`） |
| `A-03` | `A-##` | `AnchorChip` / patch 目标 | `.rl-tag` 锚点片 `A-03 · 01/05`、`T-003` 行、Composer 占位示例、模态框 `ANCHOR` |
| `REV-07` | `REV-##` | `VersionMenu` / `MAX_PATCHABLE_VERSIONS` | 版本列表 `REV-07 / REV-06`、模态框 kicker 与 `.rl-meta` |
| `[p.94]` | `[p.##]` | 重绑定后的真实切片页码 | 页框页脚 `.rl-meta` `SOURCE`、检视栏 `CITATION`、`T-002` 行、Composer 占位示例 |

**零编造遥测**：kicker 四槽为真实状态 —— 工作区（`workspace_id`）、`DOC-001`（`document_id`）、
`SSE 检索 / STREAMING`（文字枚举 `IDLE / RETRIEVING / NO MATCH / ANCHORED / STREAMING`，取自 `types/events.ts`）、
`DEMO MODE / MOCK OUTPUT`（`GET /api/health.demo_mode`）。
**没有** `ACCESS LEVEL 04`、`LOAD 42%`、`CHANNEL 07`、`SYS/NOMINAL` 这类本产品不存在的伪档案词。

---

## 3. 审计结果（自验）

```
node C:\Users\Justine\.workbuddy-ai\skills\rhine-lab-ui\scripts\audit-rhine-ui.mjs docs/prototypes/rhine-archive --strict
```

结果：**5 files scanned · 25 passed · 0 warnings · 0 errors · Exit 0**（`Clean. Ready to deliver.`）。

- 审计目录内 5 个文件全部被扫到，含 skill 自带的 `rhine-lab.css` / `rhine-lab.js`；
  **skill 自带 CSS 未被扫出任何 warning，未改 skill 源文件、未放宽任何判据**。
- A 层 `index.html` 内**无任何** `#2563EB` 等 PALETTE 蓝/青/红绿 hex；`prototype.css` / `prototype.js` 也零硬编码色值
  （`index.html` 的 `<style>` 为零，全部样式走外链 CSS，避免把颜色写进 HTML 绕过审计）。
- 逐项 error 检查全过：无蓝青、全直角、无 `#fff` 底 / `#000` 字、无非浮层 `box-shadow`、无 `font-weight: 500`、
  `prefers-reduced-motion`、`:focus-visible`、`lang="zh-CN"` + viewport + `header/nav/main/aside/footer` landmark、
  ARIA 引用 ID 全部存在、JS `querySelector` 钩子全部能在 DOM 找到、无官方 CDN / 品牌标识 / 图标库 / emoji。
- `.rl-ticks` 热区 = `height: var(--rl-touch)` = **44px**（`rhine-lab.css`），视觉只有 1px 竖线。
- 正文 15px；≤12px 拉丁标签由 skill 的 `.rl-label` / `.rl-kicker` 等统一 `letter-spacing ≥1px` + 大写。
- 动效只由 skill 的动效族提供：入场 300ms / 出场 200ms（出场快于入场）、只位移 + 透明度、
  无缩放/弹跳/旋转、无 `transition: all`、`prefers-reduced-motion` 下停摆。

### 人工核对（脚本是静态的，不执行页面）

| 项 | 结论 |
|---|---|
| 双击 `rhine-archive/index.html` 能否打开 | 相对路径 `./rhine-lab.css` / `./prototype.css` / `./rhine-lab.js` / `./prototype.js` 全部同目录，`<script defer>` 非 module，`file://` 可用 |
| 7 类编号齐全 | 见 §2，各 ≥1 次 |
| 三栏骨架 / 3 个直接子元素 | 见 §0 |
| `.rl-btn--solid` 只有 1 个 | 只有 Composer「发起调阅」；模态框内是 outline `.rl-btn`；导出 PDF / PWA 安装 / 上传 / 版本修订全为 outline |
| 暖杏金一屏 ≤2 处 | ① kicker 演示模式黄标的 1px 琥珀细线（`.proto-demo`）② 页脚 `.rl-status--live` 呼吸点。当前页 tick 用墨色不占额；主题切换用墨色细线而非 `.rl-tag`（避免第 3 处琥珀） |
| 亮/暗切换 | `data-rl-theme` + `localStorage('rl-theme')`；默认亮色暖灰纸 `#eae5e1`（`prototype.js` 在用户无显式选择时钉住亮色，避免 skill 首访跟随 OS 深色）；暗色由按钮主动切换，不用 `prefers-color-scheme` 作唯一依据 |
| 键盘 tab 可达 | 目录树 `<button>`、页签 `role="tab"`（含方向键漫游）、刻度轨 `<button>`、主题按钮、模态框开关与关闭、Composer `<textarea>` 全部原生可聚焦 + `:focus-visible` 焦点环 |
| 交互 | 目录树选中（单选 `aria-selected`）、页签切换（单根 `.rl-tab-indicator`）、主题切换、刻度轨点切页（两条轨联动 `01/05`）、文件抽屉检索、消息行选中、模态框开合（含 Esc / 遮罩 / 焦点陷阱） |
| Composer 不发请求 | 只有 `click` 写本地回执文字；无 `<form>`、无 `type="submit"`、无 fetch/XHR/EventSource/WebSocket |

**已知陷阱规避**：`hidden` 依赖 `rhine-lab.css` 的 `.rl [hidden]{display:none!important}`；
`<label class="rl-field">` 包 `<input>` 时未重复绑事件（检索由 skill 的 `data-filter` 处理，只监听 `input`）；
消息行内**无复选框**，不存在行点击与复选框冲突；未使用纯 `<table>` 承载 `aria-selected`（用 `role="listbox"` / `role="treeitem"`）。

---

## 4. 三项降级 / 合规事实（PRD §4.7 / AC-V11~V13）

1. **字体已降级**：`--rl-font: MiSans → PingFang SC → Microsoft YaHei`。项目**未打包 MiSans 字体文件**，
   A 层与 B 层页面均有可见标注「字体已降级：MiSans 未打包，当前使用系统回退链」；**不声称 MiSans 已生效**，
   不把 Inter / 思源黑体冒称等价。
2. **三行品牌块是排版不是标识**：`RHINE LAB / Synthesize Information Analysis / ARCHIVE OS` 纯文字，
   无手绘 / 近似官方标识，无官方 CDN 素材、角色立绘、AI 生图；措辞用「莱茵生命风格 / 受 RhineLab 启发」，
   **不写**「官方 UI」「同款」。
3. **B 层彩色受后端 AST 契约约束且经 Q-1 拍板保留**：`PALETTE` 与后端逐字绑定，是 AST 契约色不是主题色；
   不动 `theme.ts::PALETTE`、不动后端 schema / validator、不动 LLM 提示词颜色枚举、不动三个确定性渲染器、
   不动 6 种布局语义色与画面结构、不动导出 PDF 打印结果、不动历史版本颜色（PRD §4.6 七条）。

---

## 5. 打开方式

- **A 层（档案终端）**：双击 `rhine-archive/index.html`，或 `python -m http.server` 后访问。
- **B 层（彩色对照）**：双击 `b-layer-reference.html`；A 层页框下方有显著链接直达，B 层底部有返回链接。
  两页并排比对，判断「装帧语言 + 原件内容」是否协调 —— **这正是本原型要回答的观感问题**。

### 5.1 观感上的诚实提示（勿据此原型高估统一度）

B 层对照是**抽象色块示意**，内页底色取 `PALETTE[7] #E2E8F0`（浅灰蓝）。真实
`SlideRenderer` 的画布底色是 `#F8FAFC`、布局卡片是 `#FFFFFF`（亮色）/ `#0B1220`（暗色），
即一页接近纯白、高饱和的课件夹在暖灰纸 `#eae5e1` 里，**冷暖与明度接缝比本对照页更明显**。
按 PRD §4.7「卷宗夹彩色插图」的依据，这个接缝属预期、可解释，但**不是零违和**：细线页框在
高饱和内页旁边存在感偏弱，容易被内页压过去。建议评审时重点看两点：
① 1px `--rl-line` 页框在高饱和内页旁的「收边」够不够；
② Q-5 —— 本原型按拍板默认**亮色暖灰纸**，而生产 `theme.ts` 当前以**深色**为主，
深色外壳 + 白色课件页的反差更大，全量推进前需再拍一次默认值。

---

## 6. 目录边界

只在 `docs/prototypes/` 下新增：`rhine-archive/index.html`、`rhine-archive/prototype.css`、
`rhine-archive/prototype.js`、`b-layer-reference.html`、`README.md`。
上一实例已复制的 `rhine-archive/rhine-lab.css` / `rhine-archive/rhine-lab.js` **未改动一字**。

**未触碰**：`frontend/src/**`、`backend/**`、`docs/increments/**`、根目录任何 `*.md` / `*.bat` / `*.ps1`、
`package.json`。**未 `git add`、未 commit、未引入 npm 依赖。**

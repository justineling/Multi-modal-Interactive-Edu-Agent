# IMPL-NOTES.md — 实施期裁定与架构勘误

> 本文档记录**实现阶段**相对 `docs/00-源文档/ARCHITECTURE.md` 的偏离、勘误与交付总监裁定。
> 冲突时的优先级：`SPEC.md` > 本文档 > `ARCHITECTURE.md`（后者是设计意图，前者是落地事实）。

---

## 一、权威端点契约（后端已实现 21 个，前端按此对接）

统一响应包装 `{code, data, message, request_id}`，`code === "OK"` 即成功。
**上传接口返回 HTTP 200（非 202）**，一律按 `code` 判成败。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查，含 `demo_mode` |
| POST / GET | `/api/workspaces` | 创建 / 列表 |
| GET / PATCH / DELETE | `/api/workspaces/{id}` | 详情 / 改名 / 删除 |
| POST / GET | `/api/workspaces/{id}/documents` | 上传（multipart）/ 列表 |
| GET / DELETE | `/api/documents/{id}` | 详情（含 `toc_tree`）/ 删除 |
| GET | `/api/documents/{id}/status` | 解析状态与进度 |
| POST | `/api/documents/{id}/cancel` | 取消解析 |
| GET | `/api/documents/{id}/chunks` | 切片列表，支持 `section_code` 过滤 |
| GET | `/api/workspaces/{id}/messages` | 会话历史（升序，用于刷新恢复） |
| POST | `/api/chat/stream` | SSE 流式对话 |
| POST | `/api/chat` | 非流式对话 |
| GET | `/api/workspaces/{id}/artifacts` | Artifact 列表 |
| GET / DELETE | `/api/artifacts/{id}` | 详情（`?version=` 取历史）/ 删除 |
| GET | `/api/artifacts/{id}/versions` | 版本列表 |
| POST | `/api/artifacts/{id}/patch` | 增量补丁 |

---

## 二、契约修正（相对初期下发版本）

1. **`DocumentOut.status` 是五态，首帧为 `pending`**
   `pending → parsing → ready | failed | cancelled`。
   早期契约误写为 `processing`，**前端不得出现 `"processing"` 字面量**。

2. **`citations` 是结构化对象数组**，不是字符串数组：
   `{label: string, section_code: string, page_start: number, page_end: number}`
   架构 §5.4 已如此定义，实施与文档一致；初期口头描述曾误作字符串列表。

3. **`AgentTurnResponse.artifact_payload` 在 patch 场景为 `null`**
   `create_artifact` 返回完整 spec；`patch_artifact` 返回 `null`，补丁结果从 SSE `artifact` 事件或 `GET /api/artifacts/{id}` 取。

4. **两个 DTO 由实现侧定义**（架构文档未覆盖）：
   - `ChunkOut = {id, document_id, section_code, section_title, level, chunk_index, page_start, page_end, content, char_len}`
   - `ArtifactVersionOut = {id, artifact_id, version, spec, patch_ops, origin_turn_id, patchable, created_at}`
     `patchable=false` 表示超出 `ARTIFACT_VERSION_KEEP=20` 的旧版本，UI 应禁用「以此版本继续追问」。

5. **新增错误码 `REQUEST_INVALID`（HTTP 400）**
   架构 §9.1 缺少「客户端参数错误」码，`RequestValidationError` 只能误报为 502/500。前端 `ErrorCode` 联合类型需镜像此值。

---

## 三、架构文档勘误（设计缺陷，实现已修正）

### E1. BM25 归一化公式错误 —— 会导致 P0-05④ 形同虚设（**重要**）

- **原设计**（§7.2）：`bm25_norm = bm25 / max(bm25)`
- **缺陷**：这是**相对**归一化，候选集中最高分永远为 1.0，任何无关问题都能白拿 0.35 的 BM25 基础分，`NO_MATCH` **永远不可能触发**。
  实测「量子纠缠的退相干时间是多少」得 0.380 → 被判 `PARTIAL_OR_BEYOND`，违反 P0-05④「课本未覆盖必须明确提示」。
- **修正**：`bm25_norm = bm25 / max(ideal_score, actual_max)`，其中 `ideal_score = Σ idf`（查询词全部命中、文档长度为均值时的理论上限）。
  修正后同一问题降至 0.216 → 正确判为 `NO_MATCH`。

### E2. Grader 需增加「显式章节命中」规则

- **缺陷**：学生说「学习 4.1」时，问句与课本正文几乎没有词面重叠，纯按分数判定会落入 `NO_MATCH`，恰好打死 SPEC §1.2 的招牌场景（目录树精确锚定）。
- **修正**：显式章节号命中 + 该节 ≥1 切片 + top1 落在该节 → 判 `SYLLABUS_MATCH`。依据是「用户点名了课本中确实存在的一节」这一元数据证据，引用页码仍取自真实切片。

### E3. 带锚点追问时检索必须锁定该小节

`strict_section=True`，按 `anchor.section_code` 限定检索范围。不锁会把同章其它小节拉进来，用户点了 p.76 却看到 p.70 的引用。

### E4. `pymupdf4llm` 默认关闭

`parse_pdf(use_markdown=False)`。原因：v2 版面模型在 CPU 上对 113 页文档耗时不可控，威胁 P0-02 的 60 秒预算；且 PDF 本身没有 Markdown 的 `#` 标题，章节号只能靠正则识别，主路径用 `fitz` 逐页 `get_text()` 做页码映射更精确。
`use_markdown=True` 开关保留，失败自动回退（有测试覆盖）。

### E5. 表数量勘误

架构 §4.1 标题写「6 张表」，表格实际列了 7 行。**以表格为准，实现 7 张**：
`workspaces` / `documents` / `document_chunks` / `chunk_vectors` / `artifacts` / `artifact_versions` / `messages`。

### E6. `replace_subtree` 能力边界

`payload` 类型为 `dict`，无法承载整个列表，故仅支持单节点路径（element / entity）。`/slides` 这类列表路径返回 `PATCH_PATH_INVALID`，需改用 `insert_node` + `delete_node` 组合。

### E7. 检索覆盖判定改为「实词命中率」硬门槛（**修正 §E1 的不足，重要**）

§E1 把 BM25 归一化改成 `bm25 / max(ideal, actual_max)` 是对的方向，但**不足以解决问题**。QA 验收测试实测：14 条完全无关的问题里仍有 8 条得 0.30–0.40 分被判 `PARTIAL_OR_BEYOND`。

**根因**：`ideal = Σ idf(只累加 df>0 的查询词)`。当查询有 21 个词却只命中 3 个单字时，分母被压得极小，`bm25_norm` 反而逼近 1.0。实测「怎么用 Rust 写一个 WebAssembly 模块」`bm25_norm=0.998` → 总分 0.4016，**比真正需要拓展的「Malayan Union 对马来西亚宪制发展的长期影响」（0.3605）还高**。命中词全是单字（`三/分/习`、`色`、`一/写/用`），哈希向量再白送 0.02–0.07。

**结论：分数本身没有覆盖判定的排序语义，不能靠调阈值解决。**

**方案**：新增 `backend/app/services/text_signals.py`，把「覆盖判定」与「分数」解耦：

- `content_words()`：中文取 **bigram**（不用单字，单字会把「三/分/色」这种偶然字面重合算成命中），bigram 内任一字为虚词/疑问词/指示词即丢弃（`什么/怎么/哪里/的/是/这/我/第…`），双字套话整段剔除（`如何/一个/应该…`）；英文取 ≥2 字符且非功能词；数字只认章节号 `4.1`/`4.1.2` 与 ≥3 位年份。
- **判据**：实词在 **Top-3 切片并集**中的命中率 `< GRADER_CONTENT_HIT_MIN_RATIO(0.45)` → 直接 `NO_MATCH`，与 `top_score` 完全解耦。
- **门槛取 0.45 的依据**：18 条无关问题最高 0.40（「矩阵特征值的几何意义」命中「特征」「意义」两个词面巧合），16 条相关问题最低 0.50，门槛落在正中，两侧各留 0.05。
- 另加 `has_explicit_section_reference()`：**裸数字不再算「点名章节」**。原实现 `extract_section_code("帮我写 3 首…")` 返回 `"3"`，只要第 3 章切片排第一就以「用户点名了课本的一节」为由判 `SYLLABUS_MATCH`（实测 0.5235 假阳性）。同时补上汉字章号识别（`第五章`），否则实词门槛会把「第五章讲了什么」误判成 `NO_MATCH`——**新增门槛带来的漏判必须同时补掉**。

**实测结果**：误判率 **0/18**，漏判率 **0/16**。

**已知局限**（写进 config 注释）：0.40 与 0.50 之间的空档只有 0.10，这是纯词法方法的下限。要更稳只能上真分词或语义模型，超出「零外部依赖」约束。

### E8. 一次假阳性：「历史版本被覆盖」并不成立（**非缺陷，仅根因分析**）

> **结论先行：P0-11③ 从未被违反。** QA 用 `git worktree` 在修复前代码（`c0c3c17`）上复现确认，库内 `artifact_versions` 四行内容逐字节正确，v1 从未被改写。以下仅记录这次假阳性的成因，以及由此引出的协议加固。

**现象**：QA 断言「创建 v1 → patch 到 v4 → 读回 v1」内容被改写，报 P0-11③ 不通过。

**真实成因：两条出口的序列化方式不同。**

| 出口 | 序列化 | 首层键序 |
|---|---|---|
| SSE `artifact` 事件（`schemas/chat.py::encode_sse`） | `canonical_json`（`sort_keys=True`） | `slides, topic, type` |
| `GET /api/artifacts/{id}`（`schemas/api.py::artifact_out`） | 直接返回库内 dict 插入顺序 | `type, topic, slides` |

测试的 `original` 取自 SSE、`v1` 取自 GET，用保留键序的方式比对 → **纯键序差异被报成内容改写**。内容完全相同。

**处理决定（交付总监裁定）**：

1. **保留 `canonical_tree` 统一出口序列化，但定位为「协议加固」，不是缺陷修复。**
   理由：同一份内容在任何出口应当是同一串字节。成本几乎为零（一个函数），能消除一整类「看起来不一样但其实一样」的困惑，也让客户端的等值比较与调试字节比对可靠。**它不修复任何 P0，也不应记入缺陷清单。**
2. **同时保留 `repository` 三处 `spec_json` / `patch_ops` 的 `copy.deepcopy`，定位为「防御性加固」。**
   当前无可复现失败用例，但 `repository.apply_artifact_patch` 原先把同一个 dict 对象同时赋给 `artifact.spec_json` 和新版本行，SQLAlchemy 的 `JSON` 类型不追踪嵌套原地修改——将来任何读路径的原地改写都会带脏历史行。回归测试用**对象身份断言**锁死（`version_row.spec_json is not artifact.spec_json`），换浅拷贝立刻红。

> **教训（写给 QA 与后续维护者）**：断言「内容被改写」时，**必须区分「值不等」与「字节表示不等」**。用保留键序的字节比对去断言语义等值，会产生假阳性；反之用 `sort_keys` 比对去断言「字节级不变」，会漏掉真实的键序变化。两种断言各有其适用场景，选错方向就会误报或漏报。
>
> 同时这条也验证了流程的价值：**QA 报的现象可信，但根因必须独立查证**。若按表象直接去改「历史版本表」，会改错地方并引入真 bug。

### E9. 另外 3 个 P0 红线缺陷（QA robustness 测试抓到的）

1. **`NO_MATCH` 回合仍下发 `citations`**（P0-05④）：`_build_citations` 无条件用候选切片构造引用，导致正文说「课本未覆盖」而右侧列出 p.1-4。已改为 `grader_status == NO_MATCH` 直接返回 `[]`。
2. **并发 patch 抛未处理 `IntegrityError`**（P0-10）：乐观锁是「读→比→写」，两请求同时通过比较，第二个撞 `(artifact_id, version)` 唯一约束 → 500。已捕获并翻译为 409 `PATCH_BASE_VERSION_CONFLICT`。
3. **改名 txt 上传时 `PermissionError` 顶替错误码**（P0-02②）：`except AppError` 分支里的 `target.unlink()` 在 Windows 抛 `WinError 32`，把「400 明确提示」变成「未处理异常」。已加 `%PDF-` 魔数前置校验（落盘前就返回 400 `UNSUPPORTED_FILE_TYPE`）+ `_remove_quietly` 兜底清理。

### E10. 损坏 PDF 的孤儿文件：从「删得掉」改为「根本不会被创建」（P2，已结构性消除）

**现象**：有 `%PDF-` 魔数头但内容损坏的文件上传后，落盘文件删不掉（Windows `WinError 32`），成为无主孤儿，长期运行会累积磁盘泄漏。

**初步猜想（错）**：以为是「打开失败的 doc 没被显式 close」。实际上 `pymupdf.open()` 失败时**根本没有 doc 对象**（`doc is None`），`finally: doc.close()` 无从下手。

**真实机制**（三组对照实验确认）：

| 实验 | 结果 |
|---|---|
| A. `open(路径)` 失败 → 异常对象已出作用域 → unlink | **成功** |
| B. `open(路径)` 失败 → 异常对象仍存活（上传错误分支的现状）→ unlink | **失败，WinError 32** |
| C. 同上但加 `raise ... from None` 去掉异常链 | **仍失败**（`except` 块内 `sys.exc_info()` 仍持有那个帧） |
| D. `open(stream=bytes, filetype="pdf")` | **成功** |

即 PyMuPDF 在失败路径上留下一个 OS 句柄，**句柄生命周期绑在异常 traceback 的帧上**。上传接口的错误分支里 `AppError.__cause__` 必然存活，所以「先落盘 → 体检失败 → unlink」这条路上 unlink 必失败。异常对象被 GC 回收后立刻能删——这也解释了「有时能删有时不能」的观感（不是随机，取决于异常对象何时被回收）。

**修法：不试图让删除成功，而是让文件根本不被创建。**

1. `_open_document` / `inspect_pdf` 支持 `bytes` 入参，走 `pymupdf.open(stream=..., filetype="pdf")`——PyMuPDF 不接触文件路径，没有句柄可残留。
2. `upload_document` 改为**先对内存字节做体检，通过后才落盘**。「体检失败」与「磁盘上出现文件」在时序上互斥，**孤儿文件从结构上不可能产生**，不再依赖「删得掉」这个假设。
3. 落盘本身失败（磁盘满 / 无权限）单独兜底：清理半截文件 + `INTERNAL_ERROR`（服务端环境问题，不该报成客户端错误）。
4. `parse_pdf` 的路径入参分支**一行未改**，后台解析行为零影响。

**工程原则**：用「删掉它」来保证不残留，依赖的是「删除一定成功」这个脆弱假设；让「它根本不会被创建」才是结构性保证。**凡是有清理动作的地方，都该先问一句：能不能让它不需要被清理？**

---

## 四、工程约束补充（实现期发现）

1. **`sqlalchemy[asyncio]`**：SQLAlchemy 2.1 起 greenlet 不再随主包安装，只写 `sqlalchemy` 会在 `import sqlalchemy.ext.asyncio` 时 ImportError。
2. **SQLite 相对路径锚定到 `backend/`**：否则 `uvicorn --app-dir backend`（CWD=项目根）与 `cd backend && uvicorn`（CWD=backend）会落到两个不同的 `data/app.db`，表现为「上传过的 PDF 重启后消失」。
3. **`.gitignore` 需含 `*.db-wal` / `*.db-shm` / `*.db-journal`**：WAL 伴生文件否则会被误提交。
4. **`insert_node` / `delete_node` 的 `changed_paths` 声明到父列表层**（如 `/slides`），不到元素层。因为下标平移会让后续兄弟节点路径内容全变，声明到元素层会使「非锚定不变」断言直接失效。
5. **`focus_camera` 不写入 spec**：视角是渲染态不是内容态，写入会让同一内容因「看过哪个角度」产生不同指纹，版本比对失效。改为 `PatchOutcome.focus_preset` 旁路传递。
6. **路径祖先判断必须用 `/` 边界**：裸 `startswith` 会把 `/slides/1` 当成 `/slides/10` 的祖先，产生假阳性。后端已加负例测试，前端 `jsonPointer.ts` 需同样处理。
7. **指纹只做同语言侧自比较**：Python `json.dumps(1.0)` 是 `"1.0"`，JS `JSON.stringify(1.0)` 是 `"1"`，跨语言比对必然不等。前后端一致性靠字段级对应表 + Pydantic 校验保证。

---

## 五、前端契约实测发现（前后端联调确认，必须遵守）

### F1. `update_node` 的 payload 是**局部字段 dict**，必须字段级 merge（**写错必崩**）

后端 `update_node` 只携带被修改的字段，例如锚点追问 `/slides/1` 时 payload 仅有：

```json
{"bullets": [...], "speaker_notes": "..."}
```

**其余 9 个字段不带**。前端若用 `setAtPath(spec, "/slides/1", payload)` 整体替换，会把 `node_id` / `layout` / `title` 全部丢掉，渲染器立刻崩溃。

正确语义：

| op | 应用方式 |
|---|---|
| `update_node` | **字段级 merge**：`{...current, ...payload}` |
| `insert_node` | 整体插入新节点 |
| `replace_subtree` | 整体替换 |
| `delete_node` | 整体移除 |
| `focus_camera` | **不写入 spec**，走旁路设置相机预设 |

### F2. 锚点追问的 `citations` 会包含该节**及其子节**

锚点 `section_code=1.1` 时，`citations` 实测返回 `[{1.1.1, p.6-7}, {1.1, p.5-5}, {1.1.3, p.10-11}, {1.1.2, p.8-9}]`。

这与 §6.2「章节号前缀匹配」语义一致（子节属于该节范围），**不是缺陷**。但由此产生两条要求：

1. 测试断言不能写「带锚点追问只应出现一条引用」。
2. **`citations` 与 `retrieval.chunks` 的排序语义不同**：
   - `retrieval.chunks` 按**相关性**（分数降序），这是检索结果，顺序有意义；
   - `citations` 按**书页顺序**（`page_start` 升序），这是出处清单，让前端给清单排序属职责错位。
   后端已在 `agent_engine._build_citations` 中按 `(page_start, page_end, section_code)` 升序输出（commit `9f8c440`），落库的 `MessageOut.citations` 同序。前端若再做一次排序是幂等的，无害。

### F3. `source_ref` 是字符串，锚点构造需解析兜底

`SlideNode` 只有 `source_ref` 字符串（如 `Form 4 Sejarah, Bab 4.1, p.94`），**没有** `source_page` 数值字段；`Entity2D` / `Node3D` 才有 `source_page`。

从画布构造锚点时：

- 优先用 `source_page`（2D / 3D 节点）
- 退化用正则从 `source_ref` 提取 `p.(\d+)(?:-(\d+))?` 与 `\d+\.\d+(?:\.\d+)?`
- **解析失败时的兜底**：`section_code` 取最近一次 `retrieval` 事件的 `section_scope`，`page` 取该事件首个 chunk 的 `page_start`；仍无则 `page=1`
- **`section_code` 绝不可为 `null`** —— 后端 `strict_section` 检索依赖它锁定小节，缺了就会把同章其它小节拉进来（E3）

---

### E11. 真实 LLM 路径首次验证暴露的两个 P0（**都是「设计写了、实现没接上」**）

在配好 DeepSeek 真实 Key 后首次跑通全链路，暴露出两个 P0。这两个问题的共同特征是：**设计文档里写了，配置项也配了，但代码里没接上**。

#### 问题 1：提示词里从未给出 JSON Schema

**现象**：日志连续 3 次 `contract_retry`，校验错误数 **15 → 21 → 26**（回灌错误后越改越乱），每次都是一次完整的 32~46 秒调用，最后降级到 mock。用户看到的产物是 mock 模板文案（「这一节我们讲…先交代背景」），不是模型生成。

**根因**：`_artifact_prompt` 只写了一句「请输出符合该形态 JSON Schema 的完整对象」，**但从未把 Schema 本身给模型**。模型只能猜：

```json
// 模型实际输出
{"type":"ppt","title":"...","section":"...","slides":[{"heading":"...","bullets":[...]}]}

// 契约要求
{"type":"ppt","topic":"...","slides":[{"node_id":"slide-1","layout":"...","title":"...",
 "subtitle":null,"bullets":[...],"comparison_left":null,"comparison_right":null,
 "timeline_items":null,"quiz_questions":null,"speaker_notes":"...","source_ref":"..."}]}
```

差异：顶层 `title` 应为 `topic`、多出 `section`（`extra="forbid"` 直接拒）、`heading` 应为 `title`、缺 `node_id`/`layout`/`speaker_notes`/`source_ref`。**9 个校验错误**。

**修复**：新增 `_ARTIFACT_CONTRACT`（按 ppt / anim_2d / scene_3d 三套），每套含完整字段名 + 类型 + 枚举值 + 业务约束 + **一个最小合法示例**。两个关键细节：

- **示例必须自己过 `parse_artifact_spec()` 校验** —— 否则等于教模型写错格式。
- **调色板从 `PALETTE` 动态生成**（`_PALETTE_TEXT`），不要手抄颜色值，否则将来改调色板会失同步。

System prompt 里加硬规则：字段名逐字一致、不得自创（不许 `heading` 代 `title`、`section` 代 `topic`、`content` 代 `speaker_notes`）、声明为「数组或 null」的字段用不到时写 `null` 而非省略。

**效果**：`contract_retry` 3 次 → **0 次**（一次通过）。

#### 问题 2：`stream_text` 用错模型，分层路由从未生效

**现象**：TTFT **55,366ms**（验收标准 3,000ms）。

**根因**：`config.py` 里配了 `LLM_MODEL_ROUTER=deepseek-flash` 与 `LLM_MODEL_GENERATOR=deepseek-v4-pro`，README 和架构文档都强调「分层路由能省 70% 成本」。但 `llm_client.py` 里 **`stream_text` 和 `complete_json` 的 model 默认值都是 GENERATOR** —— `ROUTER` 一次都没被使用过。于是正文流式生成也走了 v4-pro 大模型，首 token 等了 55 秒。

**修复**：`stream_text` 默认改用 `LLM_MODEL_ROUTER`，`complete_json` 保持 GENERATOR。取舍理由写进 docstring：正文是自然语言、TTFT 是验收指标（重模型 55s vs 轻量 1~2s，学生看不出措辞差异）；spec 是强结构、字段错一个整份作废，准确率优先于延迟。

**效果**：TTFT 55,366ms → **3,232ms**。

#### 三条教训

1. **「配置了」不等于「用上了」。** 凡是「分层 / 可配置 / 可选」的设计，都必须验证每个分支真的被走到。验收要覆盖「配置生效」，而不只是「代码存在」。
2. **mock 测试全绿 ≠ 功能可用。** mock 按契约正确构造，永远不会触发「模型猜错字段名」这类问题。240 个测试全绿的情况下，接上真实模型后功能等于不可用。真实模型路径必须单独验，且要验内容质量（页码是否真实、布局是否多样、有没有模板句残留）。
3. **契约驱动的系统里，提示词本身就是契约的一部分。** Schema 不给模型，等于契约只对了一半 —— 后端有强校验，但模型不知道要满足什么。

---

### E12. 产物换轻量档 + 正文/产物并行（P1-03 实施记录）

> 设计依据：`docs/increments/P1-03-latency-DESIGN.md`（v1.0）· PRD v1.1 ·
> 实测：`docs/increments/latency-profile.md`、`flash-quality.md`

#### ① 换档裁定：产物走 `LLM_MODEL_ARTIFACT`（默认 `deepseek-flash`）

新增配置项而不是改 `LLM_MODEL_GENERATOR` 的默认值，理由有两条：
`GENERATOR` 在 README / `.env.example` / 架构文档里的语义就是「复杂 AST 用大模型」，
改它的默认值会让三处文档同时变成假话；而且保留一个重档配置，回退时只改一行环境变量。

实测（同一批 11 个回合、同一份教材）：产物阶段 **55.5s → 10.1s**，
契约通过率不变（11 次调用 0 次重试、0 次降级 mock），2D 3/3、3D 3/3 零契约失败。
回退路径三条：全局（`LLM_MODEL_ARTIFACT=` 置空 → 跟随 `GENERATOR`）、
分形态（`LLM_MODEL_ARTIFACT_BY_TYPE=anim_2d=…`，键拼错启动即抛 `ValueError`）、
并行（`ARTIFACT_PARALLEL_ENABLED=false`）。

#### ② 明确推翻 E11 问题 2 的取舍（原文保留，只加这条声明）

E11 问题 2 的结论是「`complete_json` 用重档，准确率优先于延迟」。**该取舍已被实测推翻**，
不要再照它改回去：重档换来的准确率增益在 11 个回合上为 0，代价是 5.5 倍产物耗时。
新取舍是「延迟优先 + 用 retry 率做护栏」：retry 率破 PRD G1 阈值（阶段一 ≥2 次即否决）时，
用 `LLM_MODEL_ARTIFACT` 切回重档，**不改代码默认值**。

#### ③ 比率口径：并行口径与串行口径必须并列报

产物与正文并行之后，「产物耗时占整轮的比例」有两个都对的算法：
分子取产物 span、分母取整轮（并行口径），或分母取「正文+产物」（串行口径）。
两者会给出不同数字，**必须并列报，并注明用的是哪个**；差异 > 50% 时写「已归因（并行导致）」，
**不写「已豁免」** —— 豁免意味着以后不用解释，而这里每次都要解释。

#### ④ HTML 站滞后留痕

HTML 站（`docs/01-*`、`docs/05-*`）**未同步**本次新增变量，运维以 `.env.example` 与本节为准。
待办：把 `LLM_MODEL_ARTIFACT` / `LLM_MODEL_ARTIFACT_BY_TYPE` / `ARTIFACT_FORK_MODE` /
`ARTIFACT_PARALLEL_ENABLED` / `ARTIFACT_MAX_CONCURRENCY` / `ARTIFACT_RATE_LIMIT_RETRY`
补进 HTML 站。

#### ⑤ 实现约束：`CancelledError` 是 `BaseException`

`stream_turn` 的 `finally` 里取消产物分支时，**只能** `contextlib.suppress(asyncio.CancelledError)`。
若有人改成 `except Exception`，`CancelledError` 不会被捕获（它不是 `Exception`），
「子任务被取消」会静默失效 —— 断连后继续烧 token，而且没有任何报错。
`await task` 之前必须先 `add_done_callback(_reap_task)`，否则退出时会打
`Task exception was never retrieved`。这两条是并行方案成立的前提，不是实现细节。

#### ⑥ 页码钳制：换档带来的连带修复，不是独立缺陷

**根因**（三个叠在一起，缺一不可）：
1. `_artifact_prompt` 从不把页码告诉模型（正文切片里一个页码都没有）；
2. 契约示例硬编码了 `"source_ref":"4.1.1, p.76"`，模型原样照抄 —— 在真实页码不是 76 的小节上
   就是**静默编造**（实测 4.3.1 真实页是 83~91，flash 全写 p.76）；
3. `facts` 与 `pages` 之间没有对应关系，模型即使想标也标不出。

**钳制 ≠ 后校验**：`_drop_invalid_citations` 管的是**正文**里的 `[p.N]`（不篡改已流出的正文，
只剔除引用）；`_clamp_spec_pages` 管的是**产物 AST**里的页码（在落库前剥离，不重试）。
两者共用 `result.pages` 作为「哪页合法」，口径必须一致。

**patch 也钳制，无例外**：`update_node` 的 payload 是局部字段 dict（F1），完全可能只带
`{"source_ref":"4.3.1, p.76"}`。create 钳制而 patch 不钳制，用户点一次节点追问，
刚剥掉的假页码就回来了 —— 用户可见的自相矛盾。钳制必须在 `yield "ops"` 之前完成，
否则落库/下发的 ops 与前端本地应用的不是同一份内容，前端指纹自检会报「未声明的改动」。

**钳制会掩盖提示词缺陷，所以 G3 必须同时报两个数**：
「钳制前编造率」与「钳制后达标率」。只报钳制后，等于用钳制把缺陷洗成达标。
口径固定为「页码落在检索命中区间内的 slide 占比」，**不得**改成「可解析 `p.N` 占比」——
按后者 flash 是 45%、v4-pro 是 0%，会得出「flash 更好」的错误结论，
而 flash 的高可解析率正是照抄示例里那个错页码换来的。

#### ⑦ 已知局限（本次不修，写清原因，避免成为悬案）

1. **逐页页码对应未达成**。G3 的「落在区间内」口径看不见「所有页都写同一个页码」：
   实测 5 份课件里 4 份全部 slide 共用一条事实的页码（`max_same_page_ratio` 91.4%）。
   这在语义上仍然是编造，只是编在了区间内。**已试过并回退**：在提示词里加硬规则
   「每页必须引用它所依据的那条事实的页码」，实测无改善（91.4% → 93.3%），
   已删除该规则（不留无效规则占 token）。真修需要让 slide 与 fact 建立**结构对应**
   （例如按页分配事实），不是继续在提示词里叮嘱。监控指标：
   `flash_quality_check.py` 的 `distinct_pages_per_deck` / `max_same_page_ratio`（咨询性，非门禁）。
2. **`source_ref` 的章节号可能跨出检索 scope**（实测 3/30：scope=4.2.1 却写 `4.3`）。
   **不改**：那 3 张是 1948–1957 时间线，跨到 4.3 很可能是内容本身的真实边界，
   强行钳到 4.2.1 反而会把对的改错。风险是 F3 用 `source_ref` 构造追问锚点，
   点这些节点会跳到相邻小节 —— 这是内容边界而非错误，但会让追问落在相邻节上。
3. **页数没有任何后校验 —— 残页课件会被当成合法产物落库**（**结构性缺口，当前未触发**）。

   `parse_artifact_spec` 只校验**结构**，不校验**页数**：
   `SlideDeckSpec.slides = Field(min_length=1)`（`schemas/artifacts.py:154`），
   所以哪怕只剩 1 页也能通过校验；`AnimSpec.steps` / `SceneSpec.nodes` 同样是 `min_length=1`。
   「5~8 页」「2~5 帧」「3~8 个节点」这些约束**只存在于两处**：
   `_ARTIFACT_CONTRACT` 的提示词文本，以及 `scripts/flash_quality_check.py` 的质量清单常量
   —— 后者是**测量脚本**，不在产品路径上。

   后果：**任何导致输出不完整的情况**（输出被截断、模型自己少写几页）都会产出一份
   「通过校验、正常落库、前端正常渲染」的残页课件，系统不会报警、日志里也没有异常。
   用户看到的是一份缺页课件，而系统认为它合法。

   **当前不存在触发路径**：产物生成没有设 `max_tokens`（实测 payload 里 `max_tokens: null`），
   24 次真实回合页数都在 5~7 之间，没有出现过残页。
   **但「当前无此路径」不等于「不存在这个洞」** —— 一旦有人为了压延迟加上 `max_tokens`
   （见 `docs/increments/fork-mode-ab.md` 第八节候选 C），这个洞立刻变成可达路径，
   而且**表现为静默坏结果**。届时的前置条件：先补页数后校验（`min_length=5` 或
   「不足 5 页即重试/降级」），再加限流。
4. **SSE 与 REST 的「逐字节一致」只在页码字段上成立**（QA 实测，P2，用户不可见）。

   `source_page` 那类页码字段已经由钳制规范化成 `int`，两个出口同型。但
   **float 字段没有做归一化**：模型用整数写 `position` / `scale` / `rotation` / `target` /
   2D 的 `x` `y` 时（很常见），

   ```
   模型输出      position = [0, 1, 0]
   DB（原始 dict）position = [0, 1, 0]
   SSE（pydantic）position = [0.0, 1.0, 0.0]      ← 值相同，文本表示不同
   ```

   **判 P2 不是 P1**：前端是 JS，`JSON.stringify([0,1,0])` 与 `JSON.stringify([0.0,1.0,0.0])`
   完全相同，**用户不可见**；`ArtifactEvent` 也不带 fingerprint，所以不触发指纹自检。
   它违反的只是设计 §4.9.4 的字面表述。

   **处置：收窄文档表述，不做归一化。** 把「逐字节一致」限定到页码字段（设计 §4.9.4 与
   `schemas/api.py::artifact_out` 的 docstring 已同步改准）。**不采用**「落库前先
   `model_dump()`」的彻底修法：那会按字段声明顺序重排键，与 P0-10②「保持键序、非锚定节点
   字节级不变」直接冲突 —— 不值得为一条用户看不见的差异去动键序。

#### ⑧ 三条教训（本次实施暴露，按隐蔽程度排序）

这三条的共同点是**失效时没有任何信号**：不报错、不告警、测试照绿。
E11 那三条教训（配置了≠用上了、mock 全绿≠可用、提示词即契约）失效时会表现为
**功能不可用**，人或日志能发现；这三条表现为「一切正常」，所以更危险。

**1. 一条永远不失败的测试，比没有测试更危险 —— 它给出「已覆盖」的错觉。**

凡 `xfail(strict=True)` 这类「等修好才转正」的锁，**必须用 `--runxfail` 跑一遍**，
确认它的失败原因正是要锁的那个缺陷，而不是断言值算错。修好之后**必须摘掉标记**，
否则它永远不参与判定。

实例（QA 第一轮）：`assert touched == 1` 算错了 —— 同一批 entity 被复制进 2 个 step，
越界元素实际按 2 处计数。于是这条锁**无论修没修都失败**（修前 `0 == 1`、修后 `2 == 1`），
`xfail(strict=True)` 永远 XFAIL：看上去一切正常，实际它锁不住任何东西。下次有人把
`_coerce_page` 改回只认 `int`，它仍然静默 XFAIL。修正为 `assert touched == 2` 并摘除
标记之后，它才是一条真正的回归锁。

**2. 测量脚本依赖的「约定」也是契约 —— 改了它不会红，只会给出假绿。**

`scripts/flash_quality_check.py` 的 G1 / G2 / G3 建立在 5 条约定上（设计 §3.3.4 MC-1–MC-5）：
降级文案必须含「降级」、`stage="validate"` 与失败尝试严格 1:1、
`parse_artifact_spec` 与 `_clamp_spec_pages` 必须是模块级可替换属性、
三个 async generator 方法名不变。任何一条被「顺手优化」掉，结果是
**G1 retry 率算成 0%、G2 熔断永不触发、G3 的「钳制前」样本采不到（编造率恒为 0%）**
—— 报告会更漂亮，结论是假的。这五条已写成离线断言（T02 验收 ⑥），
目的就是让「改坏」变成红灯，而不是变成假绿。

**3.「当前没有触发路径」不等于「不存在这个洞」。**

页数没有后校验（见 ⑦.3）：`SlideDeckSpec.slides` 是 `min_length=1`，所以只剩 1 页也能
通过校验、正常落库、前端正常渲染，全程没有报警。现在确实触发不了（没设 `max_tokens`，
24 个真实回合页数都在 5~7）。但只要有人为了压延迟加上 `max_tokens`，这个洞立刻变成
可达路径，**而且表现为静默坏结果**。写「当前无触发路径」时必须同时写清
**「什么改动会让它变成可达」**，否则这句话会被后来人读成「不存在问题」。

4. **数量校验的上界越界走「重试」而非「裁剪」，富内容小节会无谓降级**（2026-10-28 记录）。

   `_SPEC_CARDINALITY` 的下界 5 是 PRD 硬需求（P0-07② 要求 ≥5 页），但**上界 8 是软约束**——
   PRD 没规定上限。模型给出 9+ 页，说明内容更丰富，不是错误。

   当前行为：9 页 → `ValueError` → 重试 3 次（大概率还是 9+）→ 降级 mock（6 页模板）。
   **用户拿到的从「模型真实生成的 9 页」变成「模板拼的 6 页」**，这是净损失。

   更合理的做法是**裁到 8 页**（保留第 1 页标题 + 中间若干页 + 最后一页 quiz），而不是重试。
   但裁剪策略需要产品定义（裁哪几页、是否保留 quiz），不该由工程师顺手决定，故本次未做。

   **触发条件**：24 个真实回合页数都在 5~7，目前没触发。一旦遇到内容极丰富的小节
   （如「整个 Bab 4 的演变过程」），就会走到这条路径。

---

#### ⑧ 口径裁定：`NO_MATCH` 回合不得产出 Artifact

**争议来源**：QA 报告（`docs/increments/qa-report-p1-03.md` §4 D1）指出，任务书里写的是「检索为空 / `NO_MATCH` 回合**仍能出产物**」，而设计 §6.2 写的是「`NO_MATCH` 时不得有 `artifact` 事件」，两者直接冲突，QA 请主理人拍板。

**裁定（交付总监，2026-10-28）：以设计为准，`NO_MATCH` 不出产物。**

理由：P0-05④ 的核心是「没有课本依据就不凭空造内容」。课本里没有这个知识点却生成一份课件，**那就是凭空造内容**——只是没带页码而已。而且它比编造页码更隐蔽：学生看到画布上有一份课件，会默认「课本里有这个内容」，但正文明明写着「课本未覆盖」，自相矛盾。

QA 按设计断言是对的。任务书那句话属笔误，不作为需求。

实现位置：`_decide_action` 对 `NO_MATCH` 返回 `"none"`，`stream_turn` 因此不进入产物分支。已有测试覆盖（D1：`names[-1]=turn_end`、无 `artifact`、`complete_json` 调用 0 次、库中 0 条产物）。

---

## 六、验证基线（供 QA 与后续回归对照）

| 项 | 实测值 | 来源 |
|---|---|---|
| 后端单测 | 237 passed / 0 failed | `pytest backend/tests` |
| 后端单测（P1-03 后） | **304 passed / 0 failed** | 新增 `test_page_anchor` 29 · `test_artifact_parallel` 21 · `test_artifact_model` 10 |
| 端到端冒烟 | **31 项**全通过 | `python scripts/e2e_smoke.py`（需服务运行在 :8123）。<br>**原记录「38 项」为误记**，实测 `check(...)` 调用 31 处（QA 2026-10-26 核对）；数字错了没人发现，会让后来人以为有 7 项没跑 |
| 113 页 PDF 解析 | 0.41s（预算 60s） | e2e |
| 目录树 | 3 级 / 65 节点 | e2e |
| 切片 `section_code` 空值 | 0 / 65 | e2e |
| TTFT（mock） | 8ms（预算 3000ms） | e2e |
| 带锚点 patch 影响范围 | 仅 `/slides/2`，其余页字节级不变 | e2e |
| 连续 patch 版本自增 | 1→2→3→4 | pytest |
| **真实 LLM 模式** | **TTFT 3.2s / 5 页 5 布局 / contract_retry 0 次** | `scripts/real_llm_check.py`（DeepSeek） |

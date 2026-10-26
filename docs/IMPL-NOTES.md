# IMPL-NOTES.md — 实施期裁定与架构勘误

> 本文档记录**实现阶段**相对 `docs/ARCHITECTURE.md` 的偏离、勘误与交付总监裁定。
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

## 五、验证基线（供 QA 与后续回归对照）

| 项 | 实测值 | 来源 |
|---|---|---|
| 后端单测 | 178 passed | `pytest backend/tests` |
| 端到端冒烟 | 38 项全通过 | `python scripts/e2e_smoke.py`（需服务运行在 :8123） |
| 113 页 PDF 解析 | 0.41s（预算 60s） | e2e |
| 目录树 | 3 级 / 65 节点 | e2e |
| 切片 `section_code` 空值 | 0 / 65 | e2e |
| TTFT（mock） | 8ms（预算 3000ms） | e2e |
| 带锚点 patch 影响范围 | 仅 `/slides/2`，其余页字节级不变 | e2e |
| 连续 patch 版本自增 | 1→2→3→4 | pytest |

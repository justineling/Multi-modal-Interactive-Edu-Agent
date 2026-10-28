# P1-03 独立验证报告（QA）

| 项 | 内容 |
|---|---|
| 报告 | `docs/increments/qa-report-p1-03.md` |
| 作者 | 严过关（QA Engineer） |
| 验证对象 | 工作区未提交改动（`git status`：`config.py` / `agent_engine.py` / `llm_client.py` / `mock_generator.py` / `.env.example` / `README.md` / `IMPL-NOTES.md` + 3 个新测试文件 + `scripts/` 两个脚本） |
| 验收基准 | `P1-03-latency-DESIGN.md` v1.0 · `P1-03-latency-PRD.md` v1.1 · `IMPL-NOTES.md` E12 与 §五 |
| 新增测试 | `backend/tests/test_qa_p1_03_invariants.py`（31 条，第二轮后全部通过、无 xfail） |
| 环境 | 离线：假 `LLMClient` + 临时 SQLite；**未跑真实 LLM**（遵守边界，工程师正在占用真实 Key 与 `backend/data/profile/`） |
| 前端 | `git status` 确认 **零改动**（两轮均确认，无任何 `frontend/` 路径） |
| 轮次 | **第二轮**（第一轮 → 工程师修复 P1 → 本轮验证修复 + 修正我自己的测试 Bug） |

> 结论先行：**核心设计与实现是对的**，四条承重不变量全部成立，页码钳制的核心用例（合法 `p.77` 不被误剥）通过。
> 第一轮报的 **P1（非 int 类型越界页码绕过钳制）已被工程师修复，本轮独立复验通过**，并额外确认了「规范化写回」这个次生问题也已解决。
> 本轮我自己犯了一个测试 Bug（`touched` 期望值算错，导致 `xfail` 永远锁不住），已修正并做了变异验证。
> 剩余问题全部是 **P2（文档 / 不变量措辞 / 同类但不影响用户的字节差异）**，无阻塞。
> 路由判定：**NoOne**（无阻塞缺陷；P2 作为已知项留档）。

---

## 1. 全量回归结果

### 第一轮（工程师修复前）

| # | 项 | 命令 | 结果 |
|---|---|---|---|
| A1 | 后端 pytest | `cd backend && ./.venv/Scripts/python.exe -m pytest tests -q` | **322 passed, 3 xfailed, 0 failed**（35.5s）｜自报 304 passed ✅ 一致 |
| A2 | 前端 `npm test` | `cd frontend && npm test` | **153 passed / 7 files, 0 failed** |
| A3 | 前端 `typecheck` | `npm run typecheck` | **0 错误**（两个 tsconfig 均通过） |
| A4 | 前端 `build` | `npm run build` | **成功**（`✓ built in 13.18s`） |
| A5 | 端到端冒烟 | `scripts/e2e_smoke.py` | **31 项 PASS / 0 FAIL**｜⚠️ 自报「38 项」与实际不符，见问题 5 |

### 第二轮（工程师修复 P1 后，本轮复验）

| # | 项 | 结果 | 与第一轮对比 |
|---|---|---|---|
| B1 | 后端 pytest | **349 passed, 0 failed, 0 xfailed**（36.4s） | 工程师补 13 条 + 我修正后 31 条，全部转正 |
| B2 | 前端 `npm test` | **153 passed / 7 files, 0 failed** | 不变（前端零改动，已 `git status` 复核） |
| B3 | 前端 `typecheck` / `build` | 未重跑 | 前端字节级零改动，重跑无信息增益；第一轮结论继续有效 |
| B4 | 端到端冒烟 | **31 项 PASS / 0 FAIL**，结论「全部通过」 | 不变 |
| B5 | 新增测试文件 | **31 passed**（原 21 条 + 10 条类型边界用例，`xfail` 全部摘除） | — |

**冒烟执行方式（重要，供复现）**：`backend/.env` 里配了真实 `LLM_API_KEY`（`/api/health` 返回 `demo_mode:false`）。直接起服务跑冒烟会打真实链路，违反本次边界。我按以下方式隔离执行：

```bash
cd backend
export LLM_API_KEY=""                                  # 强制 demo 模式
export DATABASE_URL="sqlite+aiosqlite:///./data/qa_p103.db"
export DATA_DIR="./data/qa_p103"
NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8123
# 另开一个临时 CWD，用 scripts/make_sample_pdf.py 现生成 113 页样本 PDF
cd D:/tmp/qa_p103 && python <repo>/scripts/e2e_smoke.py
```

隔离后 `/api/health` 返回 `demo_mode:true`，全程未触达任何上游 LLM。**没有写入 `backend/data/app.db`、没有碰 `backend/data/profile/`。**

---

## 2. 四条承重不变量

> 每条都用与工程师**不同的观测点**。另外做了**变异验证**（第 5 节），证明这些断言不是空转。

### ① fork 点在 TTFT 定格之后 —— **成立**

| 观测点 | 断言 | 结果 |
|---|---|---|
| 引擎边界（非假对象内部） | 用 spy 记录 `_artifact_work` 的**进入时刻**；与 `stream_turn` **首个 `token` 事件被消费的时刻**比较，要求 `entered[0] > first_token_at` | ✅ 通过 |
| 定量 | 产物耗时 5ms vs 400ms，`turn_end.ttft_ms` 差值必须 ≤ 40ms（只吸收调度抖动） | ✅ 通过（TTFT 未被产物耗时污染） |
| 反证 | 切 `ARTIFACT_FORK_MODE=immediate`，同一观测点断言方向**反转**（`entered[0] < first_token_at`） | ✅ 通过（说明测试有区分力，两种模式确实不同） |

**踩到的坑（记下来，避免下一个人重复）**：第一版反证用「产物调用时刻 vs 正文调用时刻」比较，在 `immediate` 下**失败**了 —— 因为 `asyncio.create_task` 只是登记，协程体要等事件循环第一次让出才跑，所以两种模式下产物调用都晚于正文调用。**那个观测点没有区分力**。正确的观测点是「vs 首个 delta / 首个 token 事件」，这也正是设计 §4.3 的写法。

### ② 分支 B 物理上无法落库 —— **行为上成立，但设计措辞过强（见问题 6）**

| 观测点 | 断言 | 结果 |
|---|---|---|
| commit 计数 | 包一层 sessionmaker，记录每次 `commit()` 的时刻；在每个 `token` 事件处采样 commit 计数，要求序列恒为 `[0,0,0,…]`；正文结束后 commit 次数 ≥ 2（否则断言空转） | ✅ 通过 |
| 结构 | `dataclasses.fields(ArtifactWork)` 的字段白名单**精确相等**（新增字段必须重新审视「会不会给分支 B 写库能力」） | ✅ 通过 |

**必须指出的一点**：设计 §4.4 / §5.2 写「分支 B **没有 session**，物理上无法落库」——这句在**字面上不成立**。`_work_patch`（`agent_engine.py:920`）本来就在用 `async with self._session_factory() as session` 读基线，所以 `self._session_factory` 在分支 B 内是可达的；理论上 `_work_create` 也能写。真实保证是「**分支 B 不写库**」（行为），不是「不能写库」（结构）。我用 commit 计数验证了行为，这条是可靠的；建议把设计措辞改成「分支 B 不持有写事务，唯一落库点在 `_commit_artifact`，由 commit 计数测试锁死」。

### ③ `artifact` 事件必定在落库之后 —— **成立**

| 观测点 | 断言 | 结果 |
|---|---|---|
| create 路径 | 收到 `artifact` 事件的**那一刻**另开一个 session `require_artifact()`，断言 `row.version == event.version` 且 `canonical_json(row.spec_json) == canonical_json(event.spec)` | ✅ 通过 |
| patch 路径 | 同上，并断言库里已是被 patch 后的内容（`slides[2].title` 已改）、`version == 2` | ✅ 通过 |

这条直接保护前端 `runArtifactPatch({source:"event"})` 的语义（事件早于落库会让刚应用的补丁被旧快照擦掉）。

### ④ 取消不留孤儿 —— **成立**

真 `AgentEngine` + 假 LLM（工程师那条用的是**假引擎**，对并行改造零覆盖，这是设计 §6.3 M2 要求补的洞）：

| 断言 | 结果 |
|---|---|
| 断连前：`artifact_started == 1` 且 `artifact_completed == 0`（保证取消发生在产物任务 pending 时） | ✅ |
| `aclose()` 后 `asyncio.all_tasks()` 相对快照无新增未完成任务 | ✅ 零残留 |
| 断连后 `artifact_completed` 仍为 0（取消真的生效，不是「跑完了但没发」） | ✅ |
| 断连后 `GET /api/workspaces/{id}/artifacts` 条数不增 | ✅ 无孤儿产物 |

---

## 3. 页码钳制攻击（逐用例）

全部经**真实引擎链路**（假 LLM → `_generate_spec_stream` → 钳制 → 落库），同时校验 **SSE 事件里的 spec** 与 **数据库里的 spec** 两份。

| # | 用例 | 期望 | 实际 | 结论 |
|---|---|---|---|---|
| C1 | 允许集合 `{76,77}`（切片 76-77），`source_ref="4.3.1, p.77"` | **原样保留** | 两份都是 `"4.3.1, p.77"`；`complete_json` 调用 1 次（零重试） | ✅ **核心用例通过** |
| C2a | `p.76-77`（两端合法） | 保留 | `"4.3.1, p.76-77"` | ✅ |
| C2b | `p.76-1000`（一端越界） | 整条剥 | `"4.3.1"` | ✅ |
| C2c | `p.1000-1001`（两端越界） | 整条剥 | `"4.3.1"` | ✅ |
| C3a | 剥离后为空（`source_ref="p.999"`，fallback=章节号） | → `"4.3.1"`，**不得空串** | `"4.3.1"` | ✅ |
| C3b | 连章节号也没有（`fallback_ref=""`） | → `"Extended Knowledge"` | `"Extended Knowledge"` | ✅ |
| C4a | 2D `source_page` = `[76, 999, None]` | → `[76, None, None]` | 一致（2 处钳制） | ✅ |
| C4b | 3D `source_page` = `[77, 500]` | → `[77, None]` | 一致（1 处钳制） | ✅ |
| C4c | PPT `timeline_items[].source_page` 越界 | → `None` | 一致 | ✅ |
| C5 | **patch 路径**：`update_node` payload 只带 `{"source_ref":"4.3.1, p.999"}` | 下发的 `ops` 与库里的 spec **都已剥离**；未受影响的页保持原值 | `ops[0].payload.source_ref == "4.3.1"`；`slides[2] == "4.3.1"`；`slides[0] == "4.3.1, p.76"`（未受影响的页原值保留） | ✅ |
| C6 | 幂等：`clamp(clamp(x))` | 第二次 0 处改动，`canonical_json` 字节相等 | 通过 | ✅ |
| C7 | **非 int 类型的越界 `source_page`**（`"999"` / `999.0` / `true`） | → `None` | 第一轮 **未钳制、原样落库**；第二轮修复后 → `None`（计数按发生处 = 2） | ❌→✅ **P1，已修复并复验** |
| C8 | **区间内非 int**（`"76"` / `76.0` / `" 77 "` / `"76.0"`） | **写回 `int`**，SSE 与 DB 字节一致 | 两份出口都是 `int`，`canonical_json` 相等 | ✅（第二轮新增用例） |
| C9 | 脏值（`85.5` / `"abc"` / `[]` / `{}` / `False`） | → `None`，不因「类型怪」跳过 | 全部 → `None` | ✅（第二轮新增用例） |
| C10 | 规范化后幂等 | 再钳一次 0 处改动 | 通过 | ✅（第二轮新增用例） |

### C1 为什么是核心用例

`p.77` 落在切片 `76-77` 内但**不是起始页**。若实现用 `context.pages`（只有 `page_start=76`）当允许集合，`p.77` 会被误剥。实测代码用的是 `context.allowed_pages ← sorted(result.pages)`（`agent_engine.py:750`），`RetrievalResult.pages` 把 `page_start..page_end` **整段展开**（`retriever.py:85-89`），并且 `_drop_invalid_citations` 用的是同一个集合（`agent_engine.py:1498`）——**口径一致，没有分裂**。这是本次设计里最容易踩且最难查的一处，实现是对的。

### C7 的变异验证（证明 C1~C6 不是空转）

我另外做了一次**变异注入**（不改源码，用 monkeypatch 在运行期把 `_build_context` 改成「允许集合退化为切片起始页」这个经典错误），再跑 C1 的同一段断言逻辑：

```
[OK] 变异1 被捕获：p.77 被误剥成 '4.3.1' → QA 核心用例会红
[OK] 不变量3 观测点有效：commit 先于 event
[OK] 变异3 被捕获：正文期间 commit 序列 [0, 0, 1] → QA 用例会红
```

第二轮又加了一次变异（把 `_coerce_page` 退化成「只认 int」的旧实现）：**4 failed / 8 passed**，旧实现被捕获。

四个变异全部被捕获 ⇒ 这些断言**有区分力**，不是「怎么改都绿」。（变异脚本跑完即弃，未入库。）

---

## 4. 破坏性尝试

| # | 尝试 | 期望 | 实际 | 结论 |
|---|---|---|---|---|
| D1 | 检索为空 / `NO_MATCH` 回合 | 不崩、事件序列完整收尾、**不发产物**（P0-05④ + 设计 §6.2） | `names[0]=turn_start`、`names[-1]=turn_end`、有 `citations`、**无 `artifact`**、`complete_json` 调用 0 次、库中 0 条产物 | ✅ |
| D2 | 客户端在「正文流到一半」断开 | 无残留任务、无孤儿产物 | 见不变量 ④ | ✅ |
| D3 | 同一 workspace **并发两次提问** | 产物不串、版本不冲突 | 两个不同 `artifact_id`、都是 `version=1`、库里 2 条、各自 `type=ppt` | ✅ |
| D4 | 串行回退 `ARTIFACT_PARALLEL_ENABLED=false` | 与并行**逐字节一致** | 事件名序列完全相同；spec 的 `canonical_json` 完全相同（demo 模式，排除 LLM 抖动） | ✅ |
| D5 | mock 模式确定性（P0-12②） | 同输入两次，剔除 `duration_ms`/`ttft_ms`/`turn_id`/`artifact_id` 后完全相同 | `canonical_json` 相等 | ✅ |

> 关于 D1 的措辞：任务书里写的是「检索为空 / NO_MATCH 回合**仍能出产物**」。这与设计 §6.2「`NO_MATCH` 时不得有 `artifact` 事件」以及 `_decide_action`（`NO_MATCH → "none"`）**直接冲突**。我按设计文档断言的（**不出产物**），因为那是 P0-05④「没有课本依据就不凭空造内容」的要求。若产品确实希望 NO_MATCH 也出产物，那是需求变更，不在本次范围内。**请主理人确认口径。**

---

## 5. 问题清单

### 问题 1 · **P1（已修复）** · 非 int 类型的越界 `source_page` 绕过页码钳制

**第一轮定位**：`agent_engine.py:242-248` 的 `_clamp_page_number` 只判 `isinstance(page, int)` 且显式跳过 `bool`；`parse_artifact_spec` 的返回值被丢弃（`:1225`），钳制作用在原始 dict 上。pydantic lax 模式放行 `"999"` / `999.0` / `true`，于是这些值到达钳制层后被整条跳过 → **越界页码原样落库**。

**工程师修复**（已确认落地）：`_clamp_page_number` → `_coerce_page`（`:242-277`，规范化 `bool`/`int`/`float`/`str`）+ `_fix_source_page`（`:280-305`，原地改写并计数）。关键实现细节我逐条核对过：

| 核对点 | 结果 |
|---|---|
| `bool` 必须在 `int` 之前挡掉（`isinstance(True, int)` 为真，否则 `True` 会被当成第 1 页放行） | ✅ `:261-264` 顺序正确 |
| `float` 只在 `is_integer()` 时转换（`999.5` 是脏数据不是页码） | ✅ `:265-266` |
| `str` 先按整数解析，失败再按浮点解析（pydantic lax 也认 `"999.0"`） | ✅ `:267-276` |
| 越界与形态非法**都**置 `None` 并计数（不因「类型怪」跳过） | ✅ `:299-301` |
| 合法但类型不规范 → **写回 `int`**，且**不计数** | ✅ `:302-304` |
| patch 路径共用同一函数（无例外） | ✅ `:383` |

**第二轮独立复验**（不看工程师的测试，自己跑）：

```
允许集合 {76,77}
  '76'      -> 76        type=int       计数=0   ← 写回 int，不计数
  76.0      -> 76        type=int       计数=0
  ' 77 '    -> 77        type=int       计数=0
  '76.0'    -> 76        type=int       计数=0
  999       -> None      type=NoneType  计数=1
  '999'     -> None      type=NoneType  计数=1   ← 第一轮的洞，已堵
  999.0     -> None      type=NoneType  计数=1   ← 第一轮的洞，已堵
  True      -> None      type=NoneType  计数=1   ← 第一轮的洞，已堵
  85.5 / 'abc' / [] / {} / False -> None  计数=1
patch payload: {'source_page': 76, 'source_ref': '4.3.1'}  计数=1
```

**变异验证**（确认新断言不是空转）：把 `_coerce_page` 退化成「只认 int」的旧实现后重跑我的类型边界用例 → **4 failed / 8 passed**，旧实现被捕获。

**结论**：P1 已修复，**复验通过**。原 3 条 `xfail(strict=True)` 已摘除，转为常规回归锁（`test_out_of_range_source_page_of_non_int_type_must_be_clamped`，参数化 3 种形态）。

---

### 问题 1-bis · **QA 自身** · 第一轮的 `xfail` 锁永远锁不住（已修正）

工程师指出、我复现确认：第一轮那条 `xfail` 用例里 `assert touched == 1` **算错了**。我的 `_anim2d_spec`（`:121-151`）把同一批 entities 复制进了 **2 个 step**，所以 1 个越界 entity 会以「2 处发生」被计数。

后果比「断言写错」严重：`xfail(strict=True)` 的语义是「缺陷在 → XFAIL；缺陷修好 → XPASS 报错」。而这条**无论修没修都失败**（修前 `0 == 1`、修后 `2 == 1`），于是**永远 XFAIL**，看上去一切正常，实际它锁不住任何东西 —— 下次有人把 `_coerce_page` 改回只认 `int`，它仍然静默 XFAIL。

**已修正**：`assert touched == 2`（并按发生处解释口径），同时**摘除 `xfail` 标记**，让它变回一条正常断言。docstring 里对已删除的 `_clamp_page_number` 的引用也一并改掉。

> 教训记在这里：`xfail(strict=True)` 这类「等修好才转正」的锁，**必须用 `--runxfail` 跑一遍**确认它的失败原因正是你要锁的那个缺陷。否则它只是一个伪装成测试的注释。

---

### 问题 2 · **P2（部分修复）** · 落库的是未规范化的原始 dict，部分字段 SSE 与 DB 字节不一致

| 项 | 内容 |
|---|---|
| 文件:行号 | `agent_engine.py:1225-1233`（`parse_artifact_spec` 返回值被丢弃）+ `:1121`（`spec_json=spec` 存原始 dict） |
| 责任方 | Engineer（可选修） |
| 状态 | **`source_page` 已修**（`_fix_source_page` 写回 `int`，我的 C8 用例端到端证明两份出口字节一致）。**其余数值字段仍未收敛** |

**剩余范围（第二轮新发现）**：模型用整数写浮点字段（LLM 很常见的输出形态，`position: [0,1,0]` 而不是 `[0.0,1.0,0.0]`）时，`parse_artifact_spec` 会把它们强制成 float，但落库的仍是原始 int：

```
模型输出：position=[0,1,0] scale=[2,1,2] rotation=[0,0,0]
  DB  position = [0, 1, 0]        ← 原始 dict
  SSE position = [0.0, 1.0, 0.0]  ← pydantic 序列化
  SSE 与 DB 字节一致: False
  source_page 一致（已修的那部分）: 76 == 76
```

受影响字段：`position` / `scale` / `rotation` / `target` / 2D 的 `x` `y`（凡 `float` 类型都可能）。

**为什么判 P2 而不是 P1**：前端是 JavaScript，`JSON.stringify([0,1,0])` 与 `JSON.stringify([0.0,1.0,0.0])` **完全相同**（JS 数字不区分 int/float），所以**用户不可见**，也不会触发前端的指纹自检（`ArtifactEvent` 里不带 fingerprint 字段）。它违反的是设计 §4.9.4 / P0-11 的**字面表述**「同一回合的 SSE 事件与 `GET /api/artifacts/{id}` 是同一串字节」。

**建议**：二选一，不要悬着。
- **A（改文档，推荐）**：把 §4.9.4 的表述收窄为「**页码相关字段**（`source_ref` / `source_page`）字节一致；数值字段允许 `0` 与 `0.0` 的差异，因为 JS 侧不可区分」。成本零，且不触碰键序。
- **B（改代码）**：落库前用 `parse_artifact_spec(...).model_dump(mode="json")` 归一化。**注意**：`model_dump` 会按字段声明顺序重排键，与 P0-10② 「保持键序、非锚定节点字节级不变」的取向冲突，需架构确认后才做。

---

### 问题 3 · **P2** · `.env.example` / README 缺 `ARTIFACT_RATE_LIMIT_RETRY`

| 项 | 内容 |
|---|---|
| 文件:行号 | `backend/.env.example`（产物执行策略段）、`README.md:175-179` |
| 责任方 | Engineer（文档同步） |
| 说明 | 设计 §2.3 只强制要求 3 个新变量；但它是**运维可调的真实旋钮**（`0` = 不重试），且 IMPL-NOTES E12 ④ 的待办清单里明确列了它。运维在 `.env.example` 找不到这个变量 = E11 教训 1 的轻量版 |

### 问题 4 · **P2** · `_join_artifact_work` 里两处 429 分支是死代码

| 项 | 内容 |
|---|---|
| 文件:行号 | `agent_engine.py:1057-1058`（`except RateLimitedError: work = ArtifactWork(rate_limited=True)`）与 `:1087-1091`（循环后的 `if work.rate_limited:` 降级） |
| 责任方 | Engineer（可读性 / 误导） |
| 说明 | `_artifact_work` 在 `:891-900` 已经把 `RateLimitedError` 全部消化（`defer_on_rate_limit=True` 返回 `rate_limited=True`，`=False` 返回 `_rate_limit_fallback(...)`），**永不外抛**，所以 `:1057` 的 except 不可达；而 `defer_on_rate_limit=False` 的返回值里 `rate_limited` 恒为 `False`，所以 `:1087` 也不可达。两段都无害，但会让后来者以为「429 还有一条从分支 B 冒出来的路径」，从而在错误的地方排查。建议加注释说明是防御性分支，或删掉 |

### 问题 5 · **P2** · 文档里的冒烟项数与实际不符

| 项 | 内容 |
|---|---|
| 文件:行号 | `docs/00-源文档/IMPL-NOTES.md` §六 验证基线表：「端到端冒烟 \| 38 项全通过」 |
| 责任方 | Engineer（文档准确性） |
| 说明 | `scripts/e2e_smoke.py` 内共 **31** 处 `check(` 调用，实测输出 **31 条 `[PASS]`、0 条 `[FAIL]`**。「38 项」在脚本里找不到对应。这不影响功能，但会让下一个验收人以为「少了 7 项没跑」 |

### 问题 6 · **P2** · 设计文档「分支 B 物理上无法落库」的措辞过强

| 项 | 内容 |
|---|---|
| 文件:行号 | `P1-03-latency-DESIGN.md` §4.4 第 1 条、§5.2 标题 |
| 责任方 | **Architect**（文档修正，非代码） |
| 说明 | `_work_patch`（`agent_engine.py:920`）本来就用 `self._session_factory` 读基线，所以「分支 B 没有 session」字面不成立。真实保证是「**不写**」而非「**不能写**」。建议改为「分支 B 不持有写事务；唯一落库点是 `_commit_artifact`，由 commit 计数测试（不变量 ②）锁死」。措辞差别不小：将来有人照着「物理上无法」去写分支 B，就不会觉得自己需要小心 |

### 问题 7 · **P2** · 新增的测量约定（MC-5）未进设计文档、无显式断言

| 项 | 内容 |
|---|---|
| 文件:行号 | `scripts/flash_quality_check.py:231-247`（`instrument_page_clamp` 按名包裹 `agent_engine._clamp_spec_pages`）；设计 §3.3.4 的 MC-1~MC-4 未包含它 |
| 责任方 | Architect（补 MC-5 到 §3.3.4）+ Engineer（补一条 `assert`） |
| 说明 | 这是与 MC-2 **完全同类**的失效模式：脚本按名 `getattr` 包住 `_clamp_spec_pages`，一旦有人把它改成 `AgentEngine` 的方法或改名，`clamp_log` 会**静默变空** → G3 的「钳制前编造率」与「钳制后达标率」变成同一份数据 → **看起来达标，实际没测**。目前只被 `test_page_anchor.py:30` 的 `import` 间接守护（改名会 ImportError），但「变成方法」不会报错。建议在 §3.3.4 加 MC-5，并在测试里补一条 `hasattr(engine_module, "_clamp_spec_pages") and callable(...)` |

### 问题 8 · **P3** · 钳制的章节号兜底可能张冠李戴

| 项 | 内容 |
|---|---|
| 文件:行号 | `agent_engine.py:1232`（`fallback_ref=context.section_code`） |
| 责任方 | 无需修（设计已接受该近似） |
| 说明 | `context.section_code` 取的是 `chunks[0].section_code`，而被钳制的 slide 可能属于另一个章节，于是兜底会给出「别人的章节号」。设计 §4.9.6 已把提示词层根治列为待办，本条只是记录，不建议本次改 |

### 附：设计验收标准自身的措辞矛盾（不构成代码问题）

`DESIGN.md` §8 T01 验收 ② 写「`grep -r "deepseek-flash" backend/app/` **零命中**」，但 §2.2 又要求 `LLM_MODEL_ARTIFACT` 默认值就是 `deepseek-flash`，两者不可能同时成立。实际状态是**正确的**：`backend/app/services/agent_engine.py` 里零命中（符合 §3.1 的纪律），字面量只出现在 `config.py:88` 的默认值与注释、`llm_client.py:182` 的 docstring 里。建议把验收 ② 改成「`backend/app/services/agent_engine.py` 零命中」。

---

## 6. 无法验证 / 未覆盖的部分（如实说明）

| 项 | 原因 |
|---|---|
| T1 / T3 / T4 的**真实链路**指标（TTFT P50/P90、整轮耗时、patch 比率） | 按边界不跑真实 LLM。工程师已产出 `docs/increments/fork-mode-ab.md` 与 `ab-*.json`，**本报告不对其数据做背书**（我未复算） |
| `scripts/profile_latency.py` 的阶段表自检（|Σ阶段−总耗时|/总耗时 ≤ 10% 且非零退出） | 只做了**静态核查**：`assert_stage_sum(..., tolerance=0.10)`（`:935`）、`:1210` 调用点、`:954-958` 打印并失败。未实际运行（会占用 `backend/data/profile/`，与工程师冲突） |
| `scripts/flash_quality_check.py` 的 G1/G2/G3 指标 | 同上，未运行。只核对了 MC-1~MC-4 的代码特征与测试守护情况 |
| 覆盖率百分比 | 仓库未配置覆盖率工具，**不编造数字**。按关键路径评估：四条不变量、页码钳制 10 组、5 组破坏性尝试均已覆盖 |
| 真实模型输出非 int `source_page` 的**发生概率** | 离线无法测量。但第一轮已按「修它只要几行、不修的后果是钳制对一整类输入失效」判断值得修，工程师已修，本轮只验证行为不验证概率 |

---

## 7. 智能路由判定

> **状态说明（2026-10-26 收尾复核）**：本节原「建议的收尾动作」表已按工作区**实际状态**更正。原表 4 行待办中，**问题 7 / 问题 2（文档部分）/ 问题 6 / 问题 3 / 问题 5 / 问题 4 共 6 项均已完成**，现移入下方「已修 + 证据」表并附复核命令；真正未修的只剩 **3 项**（问题 2 的 float 代码部分、问题 8、附录 T01 验收 ②），全部 P2/P3，不阻塞交付。
> **为什么要更正**：把已修问题列成未修，会让下一个人重复排查同一件事，或者反过来让他以为问题还在而不敢用这个版本。**报告的准确性和结论的准确性同等重要。**

> ### 路由：**NoOne**（无阻塞缺陷，全通过）

**依据**：

1. 第一轮报的 **P1 已被工程师修复**，本轮用**独立于工程师测试**的方式复验通过（11 种取值形态 + 变异验证）。
2. 我自己第一轮那条 `xfail` 锁的断言值算错（`touched` 应为 2），已修正并摘除 `xfail`，重新做了变异验证确认它有区分力。**这是我的测试 Bug，不是源码问题。**
3. 全量回归 **349 passed / 0 failed / 0 xfailed**（**第二轮时点**；收尾复核的最终树为 **356**，差额已查清为「后补 7 条加强用例」，见 §8.2），前端 153 passed，冒烟 31 项全通过。
4. 剩余问题全部 **P2/P3**，均不阻塞：文档同步（问题 3、5）、可读性（问题 4）、不变量措辞（问题 6、7）、以及一条**用户不可见**的字节差异（问题 2，JS 侧 `0` 与 `0.0` 不可区分）。**收尾复核后其中 6 项已修**，实际未修 3 项，见 7.1 / 7.2。

#### 7.1 已修（附复核证据）

| 项 | 结论 | 证据 / 复核方式（2026-10-26 实测） |
|---|---|---|
| **问题 7**（MC-5 未进设计文档、无断言） | ✅ **已修** | ① `grep -c "MC-5" docs/increments/P1-03-latency-DESIGN.md` → **3**；② `backend/tests/test_artifact_parallel.py:506-515` 有**双向**断言：`hasattr(engine_module, "_clamp_spec_pages")` + `callable(...)` + **`not hasattr(AgentEngine, "_clamp_spec_pages")`** —— 「改名」与「变成方法」两种失效模式都被锁死，G3 不会再静默变空 |
| **问题 2**（§4.9.4「字节一致」措辞） | ✅ **已修（文档收窄）** | `DESIGN.md:588` 已改为「在**页码字段上**逐字节相同」，并显式写明 `position`/`scale`/`rotation`/`target`/2D `x y` 等 float 字段「**值相同、文本表示可能不同**」，同时补了「改钳制规则属内容变更，不得回写历史版本」的死约束；`backend/app/schemas/api.py:207-213` docstring 同步。**采用 A 方案（零成本），未动键序** |
| **问题 6**（「分支 B 物理上无法落库」措辞过强） | ✅ **已修** | `grep -rn "物理上无法落库" docs/increments/P1-03-latency-DESIGN.md backend/app/services/agent_engine.py` → 设计文档**零命中**；源码仅剩 `agent_engine.py:434` 一处「早期文档写成『物理上无法落库』是夸大」的**更正说明**。措辞已从「不能写」（结构）纠正为「不写」（行为，由 commit 计数测试锁死） |
| **问题 3**（`.env.example` / README 缺 `ARTIFACT_RATE_LIMIT_RETRY`） | ✅ **已修** | `grep -c "ARTIFACT_RATE_LIMIT_RETRY"` → `backend/.env.example`: **1**、`README.md`: **1** |
| **问题 5**（冒烟项数 38 与实际不符） | ✅ **已修** | `docs/00-源文档/IMPL-NOTES.md:406` 已改为「**31 项**全通过」，并注明「**原记录『38 项』为误记**，实测 `check(...)` 调用 31 处」。本次实测 `grep -c "check("` = 32（含 1 处 `def check(` 定义）⇒ **31 个真实调用**，与文档一致 |
| **问题 4**（`_join_artifact_work` 里 429 分支是死代码） | ✅ **已修（删真死的、留活着的）** | ① 真死的 `except RateLimitedError` **已删**，位置改为说明性 docstring（`agent_engine.py:1111-1115`）：「别再加一个『看着像在处理 429』的分支：看着有效的死代码比没有更危险」；② **`if work.rate_limited:`（`:1152`）保留** —— 它**不是**死代码：`ARTIFACT_RATE_LIMIT_RETRY=0` 时 `for` 循环一次都不跑（`:1146`），`work` 仍是 defer 返回的 `rate_limited=True`，**此处是唯一降级路径**，删掉会导致「本轮既无产物也无 error 事件」= 画布白屏 |

#### 7.2 真正未修（3 项，均不阻塞）

| 优先级 | 项 | 现状 | 归属 / 处置 |
|---|---|---|---|
| P2 | **问题 2 的 float 字段部分** | `position` / `scale` / `rotation` / `target` / 2D `x y` 的 `int`↔`float` 文本差异**仍在**（DB 存原始 dict `[0,1,0]`，SSE 走 pydantic 序列化 `[0.0,1.0,0.0]`）。**已决定不修代码**，改走文档收窄（7.1 已完成） | 无需再动。理由：JS 侧 `JSON.stringify` 对二者输出相同 ⇒ 用户不可见，也不触发指纹自检；而 B 方案（`model_dump()` 归一化）会按字段声明顺序重排键，与 P0-10②「保持键序、非锚定节点字节级不变」冲突 |
| P3 | **问题 8**（钳制的章节号兜底可能张冠李戴） | `agent_engine.py:1299`、`:1489` 仍为 `fallback_ref=context.section_code`（取 `chunks[0].section_code`），被钳制的 slide 可能属于另一章节 ⇒ 兜底给出「别人的章节号」。**§4.9.6「残留洞口（诚实清单）」目前未收录此条** | 无需修（设计已接受该近似）。**建议**：把此条补进 §4.9.6 清单，避免「诚实清单」名不副实 —— Architect |
| P3 | **附录：T01 验收 ② 字面不可满足** | `DESIGN.md:799` 仍写 `grep -r "deepseek-flash" backend/app/` **零命中**，但 `backend/app/config.py:88` 的默认值就是 `"deepseek-flash"`（另 `llm_client.py:182` docstring 提及）⇒ 该验收条件**按字面永远不可能通过** | **建议**改为 `grep -r "deepseek-flash" backend/app/services/agent_engine.py` 零命中（这才是 §3.1 纪律的真实范围，实测确实零命中）—— Architect |

**本次改动的核心结论（两轮一致）**：并行化的三条结构性保证（fork 点、分支 B 不落库、事件晚于落库）与取消安全性**全部成立**，串行回退逐字节一致，确定性未破。页码钳制的核心用例（合法 `p.77` 不被误剥）**通过** —— 这是我最担心的一处，实现选对了允许集合；类型边界这个洞已被堵上并加锁。

---

## 8. 最终认证（2026-10-26，对**最终代码树**）

本节不是新一轮测试，而是对交付树的**一次性认证**：工程师在本报告成稿后又改了 4 处（`IMPL-NOTES.md` E12 加一条教训、`P1-03-latency-DESIGN.md`、`schemas/api.py` **仅 docstring**、并删除临时脚本 `backend/_mut_check.py`）。这些改动**理论上**零行为影响 —— 但「理论上」不算验证，故在最终树上重跑全部五项。

### 8.1 五项认证结果

| # | 项 | 命令 | 结果 | 基线 | 判定 |
|---|---|---|---|---|---|
| 1 | 后端全量 | `cd backend && ./.venv/Scripts/python.exe -m pytest tests -q` | **356 passed / 0 failed / 0 skipped / 0 xfailed**（46.5s） | 报告第二轮 349 | ✅ 一致（差额已解释，见 8.2） |
| 2 | 前端单测 | `cd frontend && npm test` | **153 passed / 7 files / 0 failed**（2.75s） | 153 | ✅ 完全一致 |
| 3 | 前端类型检查 | `cd frontend && npm run typecheck` | **0 错误**（`tsconfig.json` + `tsconfig.node.json` 均通过，exit 0） | 0 | ✅ 一致 |
| 4 | 端到端冒烟 | `scripts/e2e_smoke.py`（**强制 demo 模式**） | **31 PASS / 0 FAIL**，exit 0，结论「全部通过」 | 31 | ✅ 完全一致 |
| 5 | 前端零改动 | `git status --short frontend/` | **输出为空** | 空 | ✅ 确认 |

**冒烟隔离方式（可复现，未触达真实 LLM）**：

```bash
cd backend
LLM_API_KEY="" DATA_DIR="D:/tmp/qa_p103_cert/data" \
  DATABASE_URL="sqlite+aiosqlite:///D:/tmp/qa_p103_cert/app.db" \
  NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8123
# /api/health 实测返回 demo_mode:true（已核，见下），全程零上游调用
# 另开 CWD D:/tmp/qa_p103_cert（内含 data/sample_textbook.pdf，113 页）跑 scripts/e2e_smoke.py
```

`/api/health` 实测：`{"demo_mode":true,"embedding_mode":"local","vector_store":"MemoryVectorStore",...}`。**未写入 `backend/data/app.db`、未碰 `backend/data/profile/`**（`git status --short backend/data/` 为空）。代理坑已按环境说明处理（服务端带 `NO_PROXY`，`curl` 带 `--noproxy '*'`）。

### 8.2 后端 356 vs 349 的差额：**已查清，不是测试被删或被跳过**

这是本次唯一一个「数字不符」的项，按纪律逐条查到底：

1. **先排除最坏情况**：`pytest -q -rs` 输出**无任何 skip / xfail 行**；`grep -rn "skip\|xfail" tests/*.py` **零命中**（全部测试文件）。⇒ **不存在「被静默跳过」**。
2. **再排除「被删」**：把本报告附录列出的 **22 个唯一用例名**逐一对照 `--collect-only` 输出，**22 个全部仍在**（实测唯一函数名 25 个 = 原有 22 + 后补 3）。⇒ **零删除**。
3. **差额定位**：`--collect-only` 按文件统计，总和 **356**；其中 `test_qa_p1_03_invariants.py` 收集 **38 条**，而报告成稿时是 **31 条**。**349 + 7 = 356**，差额完全由该文件的 7 条后补用例解释。
4. **这 7 条是什么**：`test_bool_is_rejected_even_when_page_one_is_allowed`(+1)、`test_source_page_type_matches_model_dump`(+3)、`test_patch_ops_payload_source_page_is_not_a_bypass`(+1)、`test_dirty_source_page_values_become_none` 由 5 扩到 7(+2)。四个名字与 `_mut_check.py` 的四个 `-k` 表达式**一一对应** —— 即上一实例在写完报告后，为了让四个变异靶点「有真实承重」而补的。**是加强，不是削弱。**
5. 文件 mtime 佐证：`test_qa_p1_03_invariants.py` = 20:26，晚于本报告 = 20:24。

> ⚠️ **口径提示**：交接时给的基线是 **354**，实测 **356**。354 应为 20:19–20:26 之间的某次中间态运行（后补用例尚未写全）。**当前树的正确数字是 356**，且构成可逐文件复核（见下）。后续如再出现数字变动，请以「零 skip + 用例名集合比对」两步法复查，不要只看总数。

```
按文件收集数（合计 356）：
test_contracts 53 | test_page_anchor 42 | test_qa_p1_03_invariants 38 | test_acceptance_p0_robustness 33
test_patch_engine 30 | test_router 25 | test_artifact_parallel 22 | test_mock_determinism 19
test_retriever 18 | test_pdf_parser 14 | test_api_smoke 13 | test_acceptance_p0_retrieval 11
test_artifact_model 10 | test_acceptance_p0_artifact 9 | test_vector_store 7
test_regression_grader_and_versions 5 | test_regression_citation_range 4 | test_regression_p2 3
```

### 8.3 `backend/_mut_check.py` 去留

| 项 | 结论 |
|---|---|
| 是否被引用 | `grep -rn "_mut_check" backend/` → **零命中**。无任何测试或源码 `import` 它 |
| 现状 | **已由工程师删除**（本次会话开始时仍存在，mtime 20:27；收尾时 `ls` 已不存在，`git status` 亦不再列出） |
| 风险评估（若将来重跑） | 该脚本会**就地改写** `app/services/agent_engine.py` 制造变异，靠 `try/finally` 还原。**风险点**：① 依赖 `finally`，进程被强杀（Ctrl-C / SIGKILL）时会**把变异留在源码里**；② 路径是相对的（`app/services/agent_engine.py`），只能在 `backend/` 下运行，别处运行会直接 `FileNotFoundError`；③ 未进 `.gitignore`（`.gitignore` 只忽略 `*.py[cod]`），留在树里容易被误提交 |
| 结论 | **可安全删除，且已删除。** 其价值（四个变异靶点的承重性）已固化为 `test_qa_p1_03_invariants.py` 里那 7 条用例，脚本本身不必入库 |

### 8.4 认证中**新发现**的一个既有缺陷（不属本次增量，不阻塞）

跑冒烟时遇到一次非预期失败（第二次复用同一数据目录时，`POST .../documents` 返回 `WORKSPACE_NOT_FOUND`）。**没有放过**，追到了根因：

| 项 | 内容 |
|---|---|
| 现象 | `POST /api/workspaces` 返回 200 且带回新 `id`，**紧接着**的请求却收到 `404 WORKSPACE_NOT_FOUND` |
| 复现率 | 建区后**立即**读取：**13/300 ≈ 4.3%**（另一次 8/100 = 8%）。**加 50ms 延迟后：0/100** |
| 根因 | `get_session`（`app/db/session.py:127-140`）把 `await session.commit()` 放在 **yield 之后**（依赖收尾），而 `create_workspace`（`app/api/workspaces.py`）只 `await session.flush()`。FastAPI **0.162.0**（≥0.106 起）的依赖收尾在**响应发出之后**执行 ⇒ **客户端可能拿到 200 时数据还没 commit**，紧接的下一请求读不到 |
| 归属 | **既有缺陷，非本次改动引入**：`git status` 确认 `app/db/session.py` 与 `app/api/workspaces.py` **本次零改动** |
| 影响 | ① 真实用户几乎撞不到（人手操作间隔远大于 50ms）；② **但 `scripts/e2e_smoke.py` 正是「建区→立即上传」的客户端，因此冒烟本身带 ~4% 的偶发失败率** —— 这就是它偶尔「第 3 步上传」崩掉的原因，**不是被测功能的回归** |
| 建议 | 若要修：把 `create_workspace` 的 `flush()` 改成显式 `await session.commit()`（或在 `get_session` 里改为「返回前 commit」）。**本次不修**（超范围，且会动请求级事务语义，需架构确认） |
| 对本次认证的影响 | **无**。干净数据目录下冒烟稳定 **31/31 通过**（8.1 第 4 项即在该状态下取得）。仅提示：**冒烟重跑若偶发失败，先重跑一次再判断**，并检查失败点是否落在「建区后第一个请求」 |

### 8.5 最终判定

> ### 认证结论：**通过（PASS）** ｜ 路由：**NoOne**

- 五项认证数字**全部达标**，唯一的数字差额（356 vs 349/354）已逐层查清为「**后补了 7 条加强用例**」，**不存在测试被删除或被跳过**。
- 工程师在本报告成稿后的 4 处改动（含删 `_mut_check.py`）**行为影响为零**，已在最终树上验证：后端 356 全绿、前端 153 全绿、类型检查 0 错误、冒烟 31/31。
- §7 的 6 项「已修」均已复核到**具体文件行号 + 可重跑命令**，不再有待办与事实不符的情况。
- 剩余 3 项未修（问题 2 float 代码部分、问题 8、附录 T01 验收 ②）均为 **P2/P3 文档级**，已决定不修或已给出精确改法，**不阻塞交付**。
- 新增 1 条**既有**缺陷记录（8.4 的 commit 时序竞态），已明确标注「非本次引入、不阻塞」，并说明它只影响**验收脚本的偶发性**，不影响产品功能。

---

## 附：新增测试文件清单

`backend/tests/test_qa_p1_03_invariants.py`（**收集 38 条，全部通过，无 xfail**）

> **条数更正（2026-10-26 收尾）**：本附录原写「31 条」，那是本报告成稿时的数字。成稿之后又补了 **7 条**（`test_bool_is_rejected_even_when_page_one_is_allowed` ×1、`test_source_page_type_matches_model_dump` ×3、`test_patch_ops_payload_source_page_is_not_a_bypass` ×1、`test_dirty_source_page_values_become_none` 由 5 条扩到 7 条 ×2），现实际收集 **38 条**。这 7 条正是变异验证的四个靶点（与被删除的 `_mut_check.py` 的 `-k` 表达式一一对应），补它们是为了让「变异必须被捕获」有真实承重。下表为成稿时的 31 条清单，7 条后补项未列入。

| 分组 | 用例 |
|---|---|
| 不变量 ①（3） | `test_artifact_work_entered_after_first_token_is_visible` · `test_ttft_ms_does_not_depend_on_artifact_latency` · `test_immediate_mode_inverts_the_ordering` |
| 不变量 ②（2） | `test_no_commit_while_reply_is_streaming` · `test_artifact_work_has_no_session_field` |
| 不变量 ③（2） | `test_create_event_is_readable_from_db_at_emit_time` · `test_patch_event_is_readable_from_db_at_emit_time` |
| 不变量 ④（1） | `test_client_disconnect_leaves_no_task_and_no_orphan_artifact` |
| 页码钳制（6） | `test_clamp_keeps_legal_end_page_inside_chunk_range`（核心）· `test_clamp_range_two_endpoints` · `test_clamp_never_produces_empty_string` · `test_clamp_source_page_for_2d_and_3d` · `test_clamp_is_idempotent` · `test_patch_payload_page_is_clamped_end_to_end` |
| 类型边界（13，第二轮） | `test_out_of_range_source_page_of_non_int_type_must_be_clamped`（×3）· `test_in_range_non_int_source_page_is_normalized_to_int`（×4）· `test_dirty_source_page_values_become_none`（×5）· `test_normalized_result_is_idempotent`（×1） |
| 破坏性（4） | `test_no_match_turn_does_not_crash_and_emits_no_artifact` · `test_two_concurrent_turns_in_same_workspace_do_not_mix` · `test_serial_fallback_is_byte_identical` · `test_mock_mode_is_deterministic_across_two_runs`（+ 断连复用不变量 ④） |

> 分组小计：3 + 2 + 2 + 1 + 6 + 13 + 4 = **31**，与成稿时的「31 条」一致。（原表三处小计写错：钳制标 7 实为 6、类型边界标 10 实为 13、破坏性标 5 实为 4，合计恰好又凑回 30 —— 这就是「31 条」与表内合计对不上的根源，2026-10-26 一并更正。）

> 第二轮相对第一轮：摘除 3 条 `xfail`（P1 已修），新增 10 条类型边界用例（含「写回 int / SSE-DB 一致 / 脏值 / 幂等」四个方向）。
> 收尾相对第二轮：再补 7 条变异靶点用例，唯一函数数由 22 增至 **25**，收集条数由 31 增至 **38**（`--collect-only` 实测）。


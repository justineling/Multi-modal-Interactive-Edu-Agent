# 后端 P0 验收测试报告

- 项目：多模态沉浸式智能助教（马来西亚 KSSM 历史课）
- 测试负责人：严过关（QA）
- 被测代码：`backend/app/`（`main` 分支 `c0c3c17` + bugfix-engineer 未提交修复）
- 报告日期：2026-10-26
- 运行命令：`cd backend && ./.venv/Scripts/python.exe -m pytest tests -q`

---

## 0. 一句话结论

**后端 P0-01 ~ P0-12 的「可自动化验收部分」全部通过（366 passed / 0 failed / 0 skipped）。**
本轮共发现 **5 个真实缺陷**（1 个 P0 阻塞、3 个 P1 严重、1 个 P2 一般），均已由 bugfix-engineer 修复并有回归用例锁定；
另有 **3 个 P2 一般问题**（B7~B9）也已修复并补上断言；
**1 个我自己的测试缺陷**（曾误报 P0-11③）已修正并撤回该缺陷结论。

> 注意：本报告**不含**前端交互与性能类验收（P0-07③ / P0-08①④ / P0-09①③④ / P0-10③），
> 那部分属前端工程师的职责，见 `docs/QA-REPORT-frontend.md`（如有）。

---

## 1. 测试统计

| 指标 | 数值 |
|---|---|
| 全量用例总数 | **366**（366 passed，后端测试集含 bugfix-engineer / 前端契约 / 另一批 QA 用例） |
| 失败 | **0** |
| 跳过 / 预期失败 | **0** |
| 本报告负责的用例 | **53**（3 个文件 + 1 个共享工具模块） |
| 其中上轮已写、本轮修正 | 17（`test_acceptance_p0_artifact.py` 8 + `test_acceptance_p0_retrieval.py` 9） |
| 本轮新写 | **36**（`test_acceptance_p0_robustness.py` 33 + 留出集 2 + 版本保留窗口 1） |
| 执行耗时 | 约 38s（含 113 页 PDF 生成与解析） |
| 覆盖率（估） | 后端 API 端点 100% 被至少一条用例打到；`services/` 关键分支（patch 5 种 op、grader 三态、取消/失败/超时）均有覆盖 |

新增文件：

| 文件 | 用例数 | 覆盖内容 |
|---|---|---|
| `backend/tests/acceptance_support.py` | —（工具） | SSE 解析、字节级比对、HTML/JS 递归扫描、页码痕迹扫描、异常 PDF 构造。**关键断言全部独立实现，不调用被测代码的同名工具** |
| `backend/tests/test_acceptance_p0_artifact.py` | 9 | P0-07② / P0-10② / P0-11①②③（含保留窗口）/ P0-12② / P0-06② |
| `backend/tests/test_acceptance_p0_retrieval.py` | 11 | P0-05②③④ / P0-04① / P0-01①③ / 留出集泛化 |
| `backend/tests/test_acceptance_p0_robustness.py` | 33 | P0-02②③ / P0-01① / P0-03③ / P0-05④ / P0-07② / P0-10 / §5.4 规则② / 输入边界 |

---

## 2. P0 逐条验收结论表

图例：**通过** = 有自动化用例且当前为绿；**部分通过** = 后端侧通过、前端侧未验或只验了一部分；
**未覆盖** = 属前端/性能，后端无法自动验收；**失败** = 有红用例。

| P0 | 验收点 | 结论 | 证据（测试用例） |
|---|---|---|---|
| **P0-01①** | 2 个 workspace 各传 PDF，切换后目录树/历史 Artifact 互不串台 | **通过** | `test_workspace_isolation_and_delete`（同一 PDF 两份，`chunk_id` 零交集）<br>`test_workspaces_do_not_leak_documents_or_artifacts`（文档列表、产物列表零交集） |
| **P0-01②** | 刷新后当前 workspace 与最近会话仍恢复（DB 持久化） | **部分通过** | `test_messages_persisted_for_session_restore`（user+assistant 两条消息、citations 落库）<br>`repository.list_workspaces` 按 `last_active_at desc` 排序<br>※「刷新后 UI 恢复」是前端行为，后端只保证数据在 |
| **P0-01③** | 删除 workspace 后其切片与 Artifact 不可再被检索 | **通过** | `test_workspace_isolation_and_delete`（删后切片表 0 行、检索 NO_MATCH、另一个 workspace 不受影响）<br>`test_delete_workspace_cascades`（文档 404） |
| **P0-02①** | ≥100 页 PDF 60 秒内 ready | **通过** | 113 页样本实测 **0.38s** 到 ready，切 65 个 chunk；所有用例的轮询上限即 60s（超时判失败，不判通过） |
| **P0-02②** | 非 PDF / 加密 / 0 页 → 明确错误码，不崩溃 | **通过**（本轮修复） | `test_bad_upload_returns_explicit_error_code[5]`：`PDF_ENCRYPTED` / `PDF_EMPTY`×2 / 超页数 / 超体积，全部 400 且 `data=null`、中文提示非空<br>`test_corrupt_pdf_returns_clear_error_not_500`（改名 txt）<br>`test_broken_pdf_with_valid_header_does_not_crash`（头合法正文损坏） |
| **P0-02③** | 解析期间有进度反馈且可取消 | **通过** | `test_cancel_during_parse_ends_in_cancelled_not_parsing`（取消→`cancelled`+`PARSE_TASK_CANCELLED`+0 切片）<br>`test_cancel_is_idempotent_after_terminal`<br>进度：`_flush_progress` 每 0.3s 落库，取消任务 `progress<100` |
| **P0-03①** | ≥100 页教材目录树至少 3 层 | **通过** | `test_document_toc_has_three_levels`（`max_level==3`，5 个顶层章） |
| **P0-03②** | 目录树节点数 ≥ 真实章节数 90% | **通过** | `test_parse_detects_three_levels`（节点数断言 ≥ 90%） |
| **P0-03③** | 点任一节点可定位到对应页码范围 | **通过** | `test_toc_nodes_are_locatable_and_match_chunk_pages`（每个节点 `1 ≤ start ≤ end ≤ page_count`，且该节点下所有切片页码落在区间内） |
| **P0-04①** | 切片 `section_code` 命中率 100% | **通过** | `test_no_chunk_in_library_has_empty_section_code`（含「无章节号 PDF 走 page_fallback」的降级文档，全库 0 条空值） |
| **P0-04②** | `page_start/page_end` 可回溯真实页码 | **通过** | `test_chunk_pages_match_section_ranges`、`test_toc_nodes_are_locatable_and_match_chunk_pages` |
| **P0-04③** | 「Malayan Union 苏丹」Top-5 全来自同一 chapter | **通过** | `test_top5_all_from_same_chapter`、`test_top_pages_are_traceable` |
| **P0-05①** | TTFT ≤ 3 秒 | **通过** | `test_chat_stream_event_order_and_artifact` 断言 `turn_end.ttft_ms < 3000`（实测个位数 ms） |
| **P0-05②** | `syllabus_strict` 每条事实陈述带 `[p.页码]` | **通过** | `test_syllabus_strict_fact_lines_all_carry_page_number`（逐条正则校验，且引用页码必须落在检索切片的真实页码范围内 —— 不许凭空造） |
| **P0-05③** | `hybrid_extended` 开头声明「含超纲内容」+ 视觉可区分 | **通过** | `test_hybrid_extended_reply_declares_and_marks_extended_part`（首行以「含超纲内容」开头、拓展段在 `> **深度拓展` 引用块内、拓展段自身不带页码） |
| **P0-05④** | 无命中降级「课本未覆盖」，禁止编造页码 | **通过**（本轮修复 2 个缺陷） | `test_unrelated_queries_must_be_no_match`（14 条无关问句全部 NO_MATCH）<br>`test_heldout_unrelated_queries_are_no_match`（**12 条未参与调参的留出集**，防过拟合）<br>`test_heldout_covered_queries_are_not_no_match`（反向：4 条课本确有的问句不得被判未覆盖）<br>`test_no_match_turn_never_emits_page_marker_anywhere`（正文+citations+artifact 全域无 `p.N`，`citations==[]`）<br>`test_compose_reply_no_match_never_prints_pages`<br>`test_chat_no_match_does_not_fabricate_pages` |
| **P0-06①** | 10 条预置问句路由准确率 ≥80% | **通过**（单元级） | `test_preset_queries_route_correctly`（**10/10**）、`test_preset_accuracy_at_least_80_percent`<br>※ 未做端到端逐条验证，原因见 §5-R3 |
| **P0-06②** | 用户显式说「用 3D」100% 遵循 | **通过** | `test_explicit_artifact_type_override_is_respected[3]`（`scene_3d`/`anim_2d`/`ppt` 三种 override 端到端走完 SSE，`modality_decision.user_override=true` 且产物类型一致）<br>`test_explicit_override_detected`、`test_explicit_request_param_wins_over_keywords` |
| **P0-06③** | UI 展示「为什么选这个形态」一句话理由 | **通过** | `test_reason_is_human_readable`；端到端断言 `modality_decision.reason` 非空且含中文说明 |
| **P0-07①** | 6 种布局各至少渲染一次 | **部分通过** | 后端：`test_ppt_covers_all_six_layouts`、`test_ppt_spec_is_valid_and_markup_free`（6 种布局全覆盖）<br>※ 实际渲染属前端 |
| **P0-07②** | 单次生成 ≥5 页 + 过 Pydantic 校验 + 不含 HTML/JS | **通过** | `test_ppt_spec_is_valid_and_markup_free`（≥5 页、6 布局、`parse_artifact_spec` 通过、递归扫描 15 种注入标记零命中）<br>`test_all_artifact_types_are_markup_free[3]`（2D/3D 也扫，不只 PPT）<br>※ 生成路径另有数量后校验（ppt 5~8 页 / anim_2d 2~5 帧 / scene_3d 3~8 节点，`IMPL-NOTES E12 ⑦.3`），越界走「重试 3 次 → 降级 mock」，见工程师的 `test_spec_cardinality.py` |
| **P0-07③** | 渲染器对缺字段/超长文本优雅降级 | **未覆盖** | 前端职责（后端契约层已用 `extra=forbid` + 长度约束挡住非法输入） |
| **P0-08①** | 动画播放/暂停/拖拽/逐帧 | **未覆盖** | 前端职责 |
| **P0-08②** | ≥3 类语义（疆域/航线/受力） | **部分通过** | 后端：`test_anim2d_has_steps_and_entities`（≥2 关键帧、每帧 ≥2 元素、坐标/调色板合法）；语义分类由 mock 生成器按 query 关键词决定 |
| **P0-08③** | 每个关键帧可标注课本页码 | **通过** | `test_anim2d_has_steps_and_entities`（`source_ref`/`source_page` 非空）；`test_all_artifact_types_are_markup_free` 校验 spec 完整 |
| **P0-08④** | 拖 60 次不掉帧/不泄漏 | **未覆盖** | 前端性能，需真实浏览器 |
| **P0-09①** | 轨道旋转/缩放/复位 | **未覆盖** | 前端职责 |
| **P0-09②** | ≥3 个可点击标注点 | **通过** | `test_scene3d_has_three_annotation_points_and_camera_tour`（≥3 个 `annotation_markdown` 非空节点、≥2 个机位） |
| **P0-09③** | 首帧 ≤5s（≤200 节点） | **未覆盖** | 前端性能 |
| **P0-09④** | 低性能设备降级 | **未覆盖** | 前端职责 |
| **P0-10①** | 点击后输入框出现锚点 chip（含节点名+页码） | **部分通过** | 后端：`Anchor` 契约强校验（`node_path` 正则、`page`/`section_code` 可选）；`test_anchor_turn_locks_to_subtree_and_sorts_citations` 验证锚点回传后的检索锁定<br>※ chip 渲染属前端 |
| **P0-10②** | patch 后仅锚定子树变化，其余**字节级**不变 | **通过** | `test_patch_changes_only_anchored_slide_bytes`（**独立实现**：把锚定页挖空后对整棵树做保序序列化比对 + 逐页比对 + 锚定页确实变了 + 身份字段一个不丢）<br>`test_focus_camera_patch_does_not_touch_content`（视角指令一字不改 spec）<br>※ 刻意不复用 `patch_engine.assert_only_subtree_changed`，避免「自己验自己」 |
| **P0-10③** | patch 响应 ≤ 同规模全量生成的 50% | **未覆盖** | 性能类。mock 下两者都在毫秒级，测出来没有信息量；真实 LLM 路径无 Key 无法验证。**结构性证据**：`artifact(action=patch)` 事件不带 spec、只带 ops，从协议上排除了「全量重生成」 |
| **P0-10④** | 取消锚点后退回全局提问 | **通过** | `test_chat_stream_event_order_and_artifact`（无 anchor 时 `turn_start.has_anchor=false`、`action=create_artifact`） |
| **P0-11①** | 连续 patch 3 次 version 1→2→3→4 | **通过** | `test_versions_append_only_and_old_version_not_overwritable`（断言 `versions == [2,3,4]`、版本列表 `[1,2,3,4]`）<br>`test_three_patches_bump_version_to_four` |
| **P0-11②** | 可切换历史版本只读预览 | **通过** | 同上（`GET /api/artifacts/{id}?version=1` 返回 v1 快照）；`artifact_version_out` 带 `patchable` 标记 |
| **P0-11③** | 历史版本不可被覆盖/删除 | **通过**（本轮确认为「从未失败」，见 §3-B5） | `test_versions_append_only_and_old_version_not_overwritable`（v1 逐字节等于创建时快照；被拒绝的 patch 不产生版本）<br>`test_old_versions_beyond_keep_window_stay_readable_but_not_patchable`（推到 v22：22 条版本一条不少、老版本仍可读、只是 `patchable=false` —— 「不可 patch」≠「被删除」）<br>`test_history_version_is_byte_stable_after_patches`（工程师独立回归） |
| **P0-12①** | 清空 Key 后全链路仍可完成 | **通过** | 全部 236 条用例都在 `LLM_API_KEY=""` 下运行（`conftest.db_env` 强制清空），覆盖上传→解析→检索→SSE→产物→patch→版本 |
| **P0-12②** | mock 产物确定性 | **通过** | `test_mock_spec_fingerprint_is_stable_across_processes`（**跨进程**：3 个不同 `PYTHONHASHSEED` 的独立子进程算 AST sha256，逐一相等；**并带金丝雀自检** —— 若 seed 未生效则测试自身报错，杜绝「假通过」）<br>`test_same_input_yields_identical_fingerprint`、`test_mock_generator_has_no_nondeterminism`（AST 级检查禁用 `hash()`/`random`/`time`） |
| **P0-12③** | UI 顶部「演示模式」标识，不可静默伪装 | **部分通过** | 后端：`/api/health` 与 `turn_start`/`turn_end` 均带 `demo_mode:true`；`test_chat_stream_event_order_and_artifact` 断言<br>※ 黄标渲染属前端 |

**统计：40 个细分验收点 —— 通过 28 / 部分通过 5 / 未覆盖 7 / 失败 0。**
（「部分通过」= 后端侧已验、前端侧未验；「未覆盖」= 纯前端交互或性能，后端无法自动验收。）

---

## 3. Bug 清单

### P0 阻塞

| # | 缺陷 | 对应 PRD | 状态 |
|---|---|---|---|
| **B1** | **无关问题被判「课本有覆盖」**：14 条无关问句里 8 条判 `PARTIAL_OR_BEYOND`，用户问「怎么做红烧肉」会收到「【课本依据】+ [p.105]」的历史课产物。根因：融合分数是相对归一化，`GRADER_PARTIAL_THRESHOLD` 分不开无关问题与真中间态 | P0-05④ | **已派工程师修复**（`retriever` 引入实词命中率 `content_hit_ratio` + `text_signals.py` + 新阈值 `GRADER_CONTENT_HIT_MIN_RATIO`）。修复后：14/14 无关→NO_MATCH，12/12 留出集→NO_MATCH，4/4 课本确有问题→非 NO_MATCH |
| **B2** | **NO_MATCH 回合仍下发带页码的 citations**：正文说「课本未覆盖」，`citations` 事件却给出 8 条 `Warisan Sejarah（p.1-4）`，前端 `CitationList.tsx` 直接渲染成引用 chip，与正文自相矛盾。根因：`_build_citations` 只看 `result.chunks`，没看 `grader_status`；`_drop_invalid_citations` 只剔「检索集里不存在的页码」，而这些页码都真实存在 | P0-05④ | **已派工程师修复**（`NO_MATCH` 时 `_build_citations` 直接返回 `[]`）。修复前复现：`test_no_match_turn_never_emits_page_marker_anywhere` 红 |

### P1 严重

| # | 缺陷 | 对应 PRD | 状态 |
|---|---|---|---|
| **B3** | **上传改名 txt 导致 500 + 未处理异常 + 孤儿文件**。三处叠加：① `_open_document` 归为 `PDF_PARSE_FAILED`（§9.1 → 500），客户端输入错误报成服务端故障；② 错误分支 `target.unlink()` 在 Windows 抛 `PermissionError [WinError 32]`（PyMuPDF 仍持句柄），异常穿透 ASGI 层，连统一错误包装都丢了；③ 落盘文件残留，每次失败泄漏一份磁盘 | P0-02② | **已派工程师修复**（`%PDF-` 魔数前置校验 → 400 `UNSUPPORTED_FILE_TYPE`；`_remove_quietly` best-effort 清理，失败只记日志）。修复后 400 + 无残留 |
| **B4** | **并发 patch 同一 artifact 时抛 `IntegrityError`（500）而非 409**。两个标签页同时基于 v1 提交，都能通过 `base_version` 比较，第二个写版本行撞 `uq_ver` 唯一约束。数据没坏（约束兜住了），但「明确提示刷新」这条产品要求没做到 | P0-10 / PRD Q3 | **已派工程师修复**（`patch_artifact` 捕获 `IntegrityError` → `rollback` → 409 `PATCH_BASE_VERSION_CONFLICT`）。修复前复现：`test_concurrent_patch_with_same_base_version` 红 |

### QA 自身的测试缺陷（不是源码缺陷，记录以示透明）

| # | 问题 | 处理 |
|---|---|---|
| **B5** | 上轮报的「P0-11③ 历史版本被后续 patch 改写」是**假阳性**。真因：我的断言把「SSE 下发的 spec（`canonical_json`，键序排序）」与「`GET ?version=1` 返回的 spec（库内插入序）」做了**保序**字节比对，内容相同、键序不同 → 误判。用 `git worktree` 检出修复前代码实测：`sort_keys` 归一后 `v1 == SSE` 为 `True`，v1 的 `speaker_notes` 仍是原文 | **已修正测试**：字节级断言两端统一从 `GET` 取（同一出口），另加一条 `sort_keys` 比对专门验证「SSE 与 GET 只存在键序差异、无内容差异」。修正后在**修复前代码上 8/8 全绿**，证明源码从未有此缺陷。已同步给 bugfix-engineer 撤回该缺陷<br>**裁定结果**（交付总监，见 `IMPL-NOTES §E8`）：`canonical_tree` 统一出口序列化保留，定位为**协议加固**而非缺陷修复；`repository` 三处 `deepcopy` 保留，定位为**防御性加固**（工程师用对象身份断言 `version_row.spec_json is not artifact.spec_json` 锁死）。两者都不计入缺陷清单 |

### P2 一般（本轮已全部修复）

| # | 问题 | 影响 | 状态与证据 |
|---|---|---|---|
| **B6** | **「头合法、正文损坏」的 PDF 会留下无主孤儿文件**。原实现「先落盘 → 体检 → 失败则 `unlink`」，而 `pymupdf.open` 失败时留下的 OS 句柄**绑在异常 traceback 的帧上**，只要错误分支里的 `AppError` 还活着就释放不了 → `unlink` 必抛 `WinError 32`，文件成孤儿。（我最初判断为「取决于 GC 时机、不稳定」；工程师做了四组对照实验后给出确定机制，我的判断被修正 —— 记录在此以免后人重犯） | 磁盘缓慢泄漏 | **已修（`871453e`，结构性消除）**：`upload_document` 改为**先对内存字节体检、通过后才落盘**（`inspect_pdf(bytes)` 走 PyMuPDF stream 模式），「体检失败」与「磁盘上出现文件」时序互斥，孤儿文件**不可能产生**。用例：`test_broken_pdf_never_touches_disk`（确定性强断言 `assert not leftovers`）—— 谁把 `inspect_pdf` 改回传路径，这条立刻红 |
| **B7** | **`error` 事件的 `stage` 一律兜底成 `"generate"`**，patch 阶段的错误也被标成 generate | 前端按 stage 提示「哪一步失败」会指错环节 | **已修**：锚点指向已删除产物时现在报 `stage="patch"`。用例：`test_error_event_is_followed_by_turn_end` 断言 `error["stage"] == "patch"` |
| **B8** | **`NO_MATCH` 时 `retrieval.chunks` 仍下发 8 个候选片段（含 `page_start/page_end`）** | 任何「显示相关片段」的 UI 都会把页码暴露在「课本未覆盖」旁边 | **已修**：`NO_MATCH` 时只清 `chunks`，保留 `grader_status`/`section_scope`/`top_score` 供诊断。用例：`test_no_match_turn_never_emits_page_marker_anywhere` 断言 `retrieval["chunks"] == []` |
| **B9** | **兜底 `turn_end` 的 `duration_ms`/`ttft_ms` 恒为 0** | 失败回合的耗时统计全部失真，无法回答「超时集中在哪个阶段」 | **已修**：`_fallback_end` 改为填真实耗时（实测 error 回合 `duration_ms=68ms / ttft_ms=65ms`）。用例：`test_error_event_is_followed_by_turn_end` 断言 `duration_ms > 0` 且 `0 < ttft_ms <= duration_ms` |

---

## 4. 智能路由判定

### 判定：**NoOne**

理由：

1. 全量 **366 passed / 0 failed**，没有任何红用例指向源码缺陷。
2. 本轮发现的 8 个问题（B1~B4、B6~B9）已全部由 bugfix-engineer 修复，且都有回归用例锁定。
3. B5 是我自己的测试缺陷，已自行修正，不计入源码缺陷。
4. 没有需要路由给 Engineer 的未决项；也没有需要我自己继续修的红用例。

### 回归复跑方式（工程师改完后）

```bash
cd backend && ./.venv/Scripts/python.exe -m pytest tests -q
```

---

## 5. 遗留风险与未覆盖项

| # | 风险 | 说明与缓解 |
|---|---|---|
| **R1** | ~~真实 LLM 路径完全没有验证~~ → **已补验，且已反过来挖出 2 个 P0** | 2026-10-26 用 DeepSeek 真实 Key 补验，首次跑通即暴露 2 个 P0：① 提示词从未给出 JSON Schema，模型自创 `heading`/`section` 字段，连续 3 次校验失败（错误数 15→21→26 递增）后降级 mock；② `stream_text` 与 `complete_json` 默认都用重档模型，`LLM_MODEL_ROUTER` 从未生效，TTFT 55 秒。均已修复。<br>2026-10-28 完成 **P1-03**（产物换轻量档 `LLM_MODEL_ARTIFACT` + 正文/产物并行），整轮 66.3s → **18.13s**，并顺带修掉换档才暴露的 P0-05④ 页码静默编造（契约示例硬编码 `p.76`，模型照抄）。<br>回归脚本：`scripts/real_llm_check.py`、`scripts/profile_latency.py`、`scripts/flash_quality_check.py`。<br>**本报告其余结论仍在演示模式下验证**；真实模式的行为差异与新增约束见 `docs/IMPL-NOTES.md` E11 / E12。 |
| **R2** | **PostgreSQL + pgvector 模式未验证** | 全部用例跑在 SQLite。`PgVectorStore` 与 `vector(1536)` 镜像列没有自动化覆盖；`UniqueConstraint`/`CASCADE` 行为在 PG 下语义一致但未实测。缓解：`test_vector_store.py` 有 DSN 解析与实现选择的单测。 |
| **R3** | **P0-06① 只有单元级证据** | 端到端逐条验证会把「路由准确率」与「检索覆盖度」混在一起：问句「桥梁受力各方向的力如何分布」在历史教材里必然 NO_MATCH，`modality_decision.artifact_type` 会被置为 `none`，于是路由判对也算错。故 P0-06① 在路由 seam 上验（10/10），端到端只验 P0-06②（显式 override，3/3）。若验收方要求端到端口径，需要先约定「NO_MATCH 回合不计入路由准确率」。 |
| **R4** | **grader 阈值是在固定样本上标的** | 新阈值（实词命中率 ≥0.45、窗口 3）依据是「18 条无关最高 0.40 / 16 条相关最低 0.50」。我已加 12 条留出集 + 4 条反向用例防过拟合，但样本仍是同一本教材。换教材（英文/马来文为主、或非历史学科）时需重新标定。 |
| **R5** | **取消/断连的时序仍有窗口** | 取消测试用「慢速假解析器」把窗口放大到 5s 才稳定；真实 113 页解析约 0.4~0.5s，用户点取消时大概率已经 ready（此时接口幂等返回 ready，属正确行为）。断连测试直接驱动 `_event_stream` 生成器（`ASGITransport` 无法模拟读一半断开）。**未被覆盖**：真实 uvicorn 下的 TCP 断连、SSE 保活 `: ping` 的实际下发（15s 间隔，测试不会等那么久）。 |
| **R6** | **并发只验了「同 base_version 双请求」** | 未验：并发上传同一 PDF 到同一 workspace、并发删除 workspace 与正在跑的解析任务、并发 patch 不同 artifact。`tasks.start` 有「重复解析先取消旧任务」的逻辑但无并发用例。 |
| **R7** | **版本保留窗口只在默认值 20 上验过一次** | `test_old_versions_beyond_keep_window_stay_readable_but_not_patchable` 已覆盖「推到 v22 后 v1 仍可读、`patchable=false`」。未覆盖：把 `ARTIFACT_VERSION_KEEP` 改成 1 或极大值时的边界行为（属调参场景，风险低）。 |
| **R8** | **超长/异常文本的渲染降级未验** | P0-07③ 的「超长文本截断 + 提示」在后端只做了 `_clip`（正文 96 字、拓展 320 字），spec 层没有字段长度上限校验，超长内容能否落库/渲染依赖前端。 |

---

## 6. 测试资产与「为什么这么测」

三条自我约束（写在 `acceptance_support.py` 文件头，这里重申）：

1. **关键断言独立实现**：SSE 解析、字节级比对、HTML/JS 递归扫描、页码痕迹扫描都在测试侧重写一遍，
   不 `import` 被测代码的同名工具。用 `patch_engine.assert_only_subtree_changed` 去验证
   patch 引擎，等于自己验自己。
2. **走用户实际走的路**：数据全部经 HTTP 接口上传真实 PDF（`scripts/make_sample_pdf.py` 生成），
   不走任何内部捷径。少数例外（`compose_reply`、`_event_stream`）在文件头注明了理由。
3. **失败的测试是资产**：本轮 8 个缺陷都是「测试先红、源码后修」，修复后同一批用例转绿即完成回归。
   没有保留任何 `xfail` —— 曾短暂用 xfail 标注的 B6（孤儿文件）在工程师做完结构性修复后
   已收紧为确定性断言。

### 几条「防作弊」设计（值得后续沿用）

| 设计 | 防的是什么 |
|---|---|
| P0-12② 的**金丝雀自检**（先证明三个子进程 `hash()` 结果不同，再比 AST 指纹） | 防「`PYTHONHASHSEED` 没生效 → 测试永远绿」的假通过 |
| P0-05④ 的**留出集 + 反向用例**成对出现 | 防「把阈值调到刚好通过那 14 条」的过拟合；也防「全判 NO_MATCH」这种作弊式修复 |
| P0-10② 的**挖空+保序序列化**比对 | 防「用 `sort_keys=True` 把键序变化掩盖掉」—— 前端 diff 会误判整页重绘 |
| P0-02③ 的**慢速假解析器** | 防「和 CPU 抢时间」的随机测试（真解析 0.38s，取消窗口太小） |
| 字节级断言的**两端同出口** | 本轮 B5 的教训：跨出口（SSE vs REST）比对会把协议层规范化误判成内容变化 |

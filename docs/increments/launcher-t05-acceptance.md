# T05 真机验收清单（Windows Job Object / 关窗口即停）

> 状态：**本会话未完成任何「真实启动」实测**。
> 原因：本会话的工具安全策略禁止在命令行文本里动态编译 .NET 代码，且进程创建路径
> （`Start-Process` / `.NET Process.Start` / `Win32_Process Create`）被拦截，因此
> §7.2 / §7.3 / §7.4 要求的「起服务 → 强杀启动器 → 验孙进程全清」无法在工具沙箱
> 内稳定复现。下面每一项都必须在**一台干净的、未挂调试器 / 未托管 Job 的
> Windows 真机**上手工执行；执行结果请回填，不要以 DryRun 输出代替实测。

## 0. 前置：确认 DryRun 已通过

在项目根目录执行（PS 5.1）：

```
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\start-web.ps1" -DryRun -NoPause -NoBrowser
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\start-web.ps1" -DryRun -ForceJobFallback -NoPause -NoBrowser
```

期望：第一条 `Job capability` 行给出**真实** `AddType=OK|FAIL`、
`Marshal.SizeOf(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)=144`（x64）、
`Legacy trio: enabled=yes`（`-NoPause` 下必须启用）；第二条 `Legacy trio: enabled=yes`。
两条都不得在 `backend\data\launcher\` 下新增任何文件，执行前后 `chcp` 值不变。

若 `AddType=FAIL`，说明本机策略拦截了运行时编译，**该机器必然走降级路径**，
请直接跳到本清单第 4 节，并在第 1/2/3 节标注「不适用（本机能力不可用）」。

## 1. 正常路径：关窗口即停（§7.2 / §7.3，最重要）

1. 干净会话里启动（不要在 IDE / 调试器 / 托管 Job 里跑，宿主额外持有 job 句柄会假阴性）：

   ```
   cd /d "<项目根目录>"
   "1. 启动网页端(Windows).bat" -NoBrowser
   ```

   期望：只出现 1 个可见控制台窗口；完成页出现 `启动完成。直接关闭本窗口即会自动停止全部服务。`
   （英文降级环境为 `Started. Closing this window stops ALL services automatically.`），
   以及一行 `Job.RaceNotice` 低调提示；**不出现**「请再运行停止脚本兜底清理」。
   两个端口就绪（`http://127.0.0.1:8000/api/health` 返回 `code=OK`，
   `http://127.0.0.1:5173` 与 `http://[::1]:5173` 均可连）。

2. 启动前记录完整进程树（**含孙进程**，这是本项的核心）：

   ```
   powershell -NoProfile -Command "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | Export-Csv -NoTypeInformation before.csv"
   ```

   期望 `before.csv` 里能看到 `python.exe -m uvicorn app.main:app --reload` 的
   **reloader 父 + server 子两层**，以及 `cmd.exe /c npm run dev` -> `node.exe` ->
   `esbuild.exe` 整条链；且这些进程 `MainWindowHandle=0`、`MainWindowTitle=""`。

3. 记下启动器自身 PID `$launcherPid`（任务管理器里那个唯一的控制台窗口对应的
   `powershell.exe`；或用 `Get-CimInstance Win32_Process | ? { $_.CommandLine -like '*start-web.ps1*' }`），
   然后**不要按回车**，精确强杀单 PID：

   ```
   powershell -NoProfile -Command "Stop-Process -Id <launcherPid> -Force"
   ```

   **禁止**用 `taskkill /PID <launcherPid> /T /F`：它会连坐杀掉服务，产生假阳性，
   验不出 Job 的效果。**禁止**用 `CloseMainWindow()`：隐藏服务没有窗口。

4. 等待 <= 5 秒后检查：

   ```
   powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object LocalPort,State,OwningProcess"
   powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 5173 -ErrorAction SilentlyContinue | Select-Object LocalPort,State,OwningProcess"
   powershell -NoProfile -Command "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | Export-Csv -NoTypeInformation after.csv"
   ```

   > ⚠️ **判据修正（QA 真机实测 2026-11-08，见 `launcher-t05-verify-report.md` §1.2）**：
   > 原文写的 `Get-NetTCPConnection -LocalPort 8000,5173 ... | Where-Object State -eq Listen`
   > 在本机 **PowerShell 5.1 上恒返回空**——服务确实在监听、`health=200` 成功时仍无输出。
   > 照抄会得出「端口从未监听」的**假绿结论**。修正为**单端口分别调用**，且必须再用
   > 真实 TCP 建连（覆盖 `127.0.0.1:8000` / `127.0.0.1:5173` / `[::1]:5173`，vite 只绑 IPv6）
   > 与 `http://127.0.0.1:8000/api/health`、`http://localhost:5173` 的 HTTP 请求交叉印证。
   > **端口判据不能只看一个命令的输出**，尤其不能把「什么都没查到」当成「已停止」。

   期望：单端口查询无 `Listen` 项、三个地址均无法建连、health 与前端均不可连；
   `before.csv` 里所有服务进程（**逐条对比孙进程**）在 `after.csv` 中全部消失；
   `backend\data\launcher\pids.json` **不应被新建**（三件套已退场）。

5. 再跑一轮用 `GenerateConsoleCtrlEvent(CTRL_CLOSE_EVENT, 0)` 模拟「点 X」，
   对比与 `Stop-Process -Force` 的差异（§3.9 列为需实测项）。

### 为什么必须验孙进程，而不是只验父进程

这正是 Job Object 相对 `pids.json` 的全部价值，也是 T05 最容易静默失败的地方：

- Windows **没有**「父进程退出则子进程终止」的语义。父退出后子进程被 reparent 到
  PID 0 并继续占端口。只验顶层后端/前端 PID，会得到一个「看起来停了、其实还在跑」的假绿。
- 本项目保留了 `uvicorn --reload`（决议 6），后端是 **reloader 父 + server 子两层**；
  前端是 `cmd -> npm -> node -> esbuild` 链。真正监听 8000/5173 的往往是孙进程，
  不是启动器直接持有的那两个 PID。
- `Start-Process` 无法以 `CREATE_SUSPENDED` 启动，`CreateProcess -> Assign` 之间存在
  毫秒级竞态窗口：**在该窗口内派生的孙进程当时不在 job 内，可能逃逸**。
  T05 用「每次 Start-Process 后立即 Assign」+ `Get-CimInstance Win32_Process`
  递归补挂 **2 轮 × 200ms** 缓解，但残留风险如实保留在 `Job.RaceNotice` 里，
  **不得写成已消除**。只有逐条核对孙进程，才能发现这个逃逸是否真的发生。
- 因此 §7.3 明确要求：正常路径下如果 `pids.json` 反而出现，说明误走了降级分支，
  也是通过孙进程清单才能反查出来的。

## 2. `-NoPause`：服务常驻，pids.json 与停止脚本必须可用（§3.4 / §3.5）

```
"1. 启动网页端(Windows).bat" -NoPause -NoBrowser
```

期望：完成页出现 `-NoPause：服务常驻后台...`（英文为纯 ASCII 的
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-web.ps1` 可执行命令），
**绝不出现**「关窗口即停」；启动器退出后服务仍在 8000/5173 监听；
`backend\data\launcher\pids.json` 存在且为 v2 格式
（`pids` / `updated_at` / `target_url` 三个旧字段 + 可选 `processes[pid,process_name,start_time]`）；
然后单独运行 `"3. 停止网页端(Windows).bat" -NoPause`，期望按 P1-3 四态判定杀树并删除
`pids.json`，且 `pause` 不挂起自动化。
`KillFlag=skipped(-NoPause)`、`KillOnClose=no` 应出现在 DryRun 输出里。

## 3. 降级路径四项可用（§7.4，用 `-ForceJobFallback` 稳定复现）

```
"1. 启动网页端(Windows).bat" -NoBrowser -ForceJobFallback
```

期望（四项逐条验）：

1. 完成页**任何一处都没有**「关窗口即停」；明确出现
   `本次未启用自动清理（原因：...），请在本窗口按回车停止服务后再关闭。`
   （英文 `Auto-cleanup NOT enabled this run (...). Press ENTER here to stop services before closing.`），
   以及「请按回车键停止服务并关闭」和停止脚本兜底提示。`FallbackReason` 必须可诊断，
   不能只写「失败」。
2. `backend\data\launcher\pids.json` 存在且含本轮真实 PID / `updated_at` / `target_url` /
   `processes` 明细。
3. 在完成页按回车 -> 8000/5173 全部释放、孙进程全清、`pids.json` 被删除；服务起得来、可用。
4. 另起一轮，不按回车直接关窗口，再独立运行 `"3. 停止网页端(Windows).bat"`，
   期望杀树并删除 `pids.json`。

环境级备用（可选）：在受限会话里让能力探测失败，复现 `Fallback=Add-Type blocked ...`，
确认屏幕同样不出现「关窗口即停」。

## 4. 降级路径「服务仍可用」与降级安全不变量

人为制造 S6 部分失败（例如让第二个 Assign 返回失败）不易复现，可选做。
核心判据：**降级不能把已启动的服务杀掉**。任何失败路径都必须先
`SetInformationJobObject(..., LimitFlags=0)` 清掉 KILL flag，再 `CloseHandle`；
若直接关句柄，`KILL_ON_JOB_CLOSE` 会立刻杀掉 job 内进程，服务就丢了。
代码审查可确认，运行时结论需真机验证并在此处回填。

## 5. 本会话已完成 / 未完成的对照

| 项 | 状态 |
|---|---|
| 三个 `.ps1` 的 `Parser::ParseFile` -> `ParseErrors=0` | 本会话已验 |
| `check-launcher-encoding.ps1` 全 PASS（含 MSG-PAIR 新增 4 键） | 本会话已验 |
| 两个 `.bat` 100% ASCII、三个 `.ps1` UTF-8 BOM + CRLF 逐字节复核 | 本会话已验 |
| `-DryRun -NoPause -NoBrowser` 全部 §7.1 行 + 真实 AddType 结果 + Marshal.SizeOf=144 + chcp 不变 + 无新文件 | 本会话已验 |
| `-DryRun -ForceJobFallback -NoPause -NoBrowser` -> `Legacy trio: enabled=yes` + 无副作用 | 本会话已验 |
| 后端 pytest 404 passed / 1 deselected | 本会话已验 |
| `git status --short` 无 `D`；单 commit 只含 T05 文件 | 本会话已验 |
| 真实启动 / 单窗口 / 隐藏进程 / 双栈就绪 | **未实测**（本清单第 1 节） |
| 强杀 launcher 单 PID 后 8000/5173 释放 + reloader+server+node+esbuild 孙进程全清 | **未实测**（本清单第 1 节） |
| `CTRL_CLOSE_EVENT` 点 X 与 `Stop-Process -Force` 的差异 | **未实测**（本清单第 1 节第 5 步） |
| 降级路径四项可用（pids.json / 退出事件 / 按回车杀树 / 停止脚本） | **未实测**（本清单第 3 节） |
| `-NoPause` 服务常驻 + 停止脚本可用 | **未实测**（本清单第 2 节） |
| 嵌套 Job / `SILENT_BREAKAWAY_OK`、job 句柄关闭时机、孙进程逃逸 | **未实测**（设计 §3.9） |

> 严禁把上表「未实测」改写成「已验证」。T05 的失败模式是「用户以为关窗口就停了，
> 其实服务还在跑」，与已修掉的 C8 属同一类静默错误；只有真机逐条核对孙进程才能收口。

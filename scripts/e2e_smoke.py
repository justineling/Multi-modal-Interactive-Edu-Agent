"""端到端冒烟验证（由交付总监独立执行，不复用工程师脚本）。

链路：health -> 建 workspace -> 上传 113 页样本 PDF -> 轮询解析 -> SSE 对话
     -> 校验 8 类事件顺序 -> 带锚点追问 -> 校验 patch 仅改目标子树 -> 版本与消息持久化。
"""

import json
import time

import httpx

BASE = "http://127.0.0.1:8123"
PDF = "data/sample_textbook.pdf"
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' :: ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """独立实现 SSE 解析（不调用被测代码），事件块以空行分隔。"""
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        ev, data_lines = None, []
        for line in block.split("\n"):
            if line.startswith(": "):  # ping 注释行
                continue
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if ev and data_lines:
            events.append((ev, json.loads("\n".join(data_lines))))
    return events


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=120.0)

    print("\n[1] 健康检查")
    r = c.get("/api/health")
    check("health 200", r.status_code == 200, r.text[:120])
    check("demo_mode=true（无 Key 时降级）", r.json()["data"]["demo_mode"] is True)

    print("\n[2] 创建工作区")
    r = c.post("/api/workspaces", json={"title": "E2E 验证工作区"})
    ws = r.json()["data"]
    ws_id = ws["id"]
    check("workspace 创建", r.status_code == 200 and bool(ws_id), ws_id[:8])

    print("\n[3] 上传 113 页 PDF")
    t0 = time.perf_counter()
    with open(PDF, "rb") as f:
        r = c.post(
            f"/api/workspaces/{ws_id}/documents",
            files={"file": ("sample_textbook.pdf", f, "application/pdf")},
        )
    doc = r.json()["data"]
    doc_id = doc["id"]
    check("上传返回 200", r.status_code == 200)
    check("初始状态为 pending", doc["status"] == "pending", doc["status"])
    check("页数识别", doc["page_count"] == 113, str(doc["page_count"]))

    print("\n[4] 轮询解析状态")
    status, progress_seen = None, set()
    for _ in range(120):
        s = c.get(f"/api/documents/{doc_id}/status").json()["data"]
        status = s["status"]
        progress_seen.add(s["progress"])
        if status in ("ready", "failed", "cancelled"):
            break
        time.sleep(0.3)
    elapsed = time.perf_counter() - t0
    check("解析到 ready", status == "ready", f"status={status}, {elapsed:.2f}s")
    check("解析耗时 < 60s（P0-02①）", elapsed < 60, f"{elapsed:.2f}s")

    print("\n[5] 目录树层级（P0-03① 要求 ≥3 级）")
    d = c.get(f"/api/documents/{doc_id}").json()["data"]
    toc = d["toc_tree"]

    def depth(nodes: list) -> int:
        if not nodes:
            return 0
        return 1 + max(depth(n.get("children") or []) for n in nodes)

    def count(nodes: list) -> int:
        return sum(1 + count(n.get("children") or []) for n in nodes)

    dep, cnt = depth(toc), count(toc)
    check("目录树层级 >= 3", dep >= 3, f"depth={dep}")
    check("目录节点数 >= 60", cnt >= 60, f"nodes={cnt}")

    print("\n[6] 切片 section_code 无空值（P0-04① 硬指标）")
    chunks = c.get(f"/api/documents/{doc_id}/chunks", params={"limit": 500}).json()["data"]
    empty = [x for x in chunks if not x["section_code"]]
    check("section_code 命中率 100%", len(empty) == 0, f"{len(chunks)} 片, 空={len(empty)}")
    bad_page = [x for x in chunks if x["page_start"] < 1 or x["page_end"] < x["page_start"]]
    check("页码区间合法", len(bad_page) == 0, f"非法={len(bad_page)}")

    print("\n[7] SSE 全量生成（P0-05 / P0-06）")
    t0 = time.perf_counter()
    with c.stream(
        "POST",
        "/api/chat/stream",
        json={"workspace_id": ws_id, "query": "4.1 Malayan Union 对苏丹的影响", "mode": "syllabus_strict"},
    ) as resp:
        raw = "".join(resp.iter_text())
    wall = time.perf_counter() - t0
    events = parse_sse(raw)
    names = [e for e, _ in events]
    print(f"  事件序列: {names}")
    check("8 类事件齐备", set(names) >= {
        "turn_start", "retrieval", "modality_decision", "token", "artifact", "citations", "turn_end"
    }, str(sorted(set(names))))
    order = ["turn_start", "retrieval", "modality_decision", "artifact", "citations", "turn_end"]
    idx = [names.index(n) for n in order if n in names]
    check("事件顺序正确", idx == sorted(idx), str(idx))

    payload = dict(events)
    check("data 为单行紧凑 JSON", all("\n" not in blk.split("data:", 1)[1] for blk in raw.split("\n\n") if "data:" in blk))
    check("grader_status 有值", payload["retrieval"]["grader_status"] in
          ("SYLLABUS_MATCH", "PARTIAL_OR_BEYOND", "NO_MATCH"), payload["retrieval"]["grader_status"])
    check("路由给了人话理由", len(payload["modality_decision"]["reason"]) > 8,
          payload["modality_decision"]["reason"][:40])
    art = payload["artifact"]
    check("artifact 为 create", art["action"] == "create", art["artifact_type"])
    check("PPT 页数 >= 5（P0-07②）", len(art["spec"]["slides"]) >= 5, str(len(art["spec"]["slides"])))
    check("TTFT < 3s（P0-05①）", payload["turn_end"]["ttft_ms"] < 3000,
          f"{payload['turn_end']['ttft_ms']}ms / 墙钟 {wall*1000:.0f}ms")
    # 架构 §5.4 定义 citations 为对象数组 {label, section_code, page_start, page_end}
    cits = payload["citations"]["citations"]
    check(
        "引用为结构化对象且带页码区间",
        bool(cits) and all(
            isinstance(x, dict)
            and x.get("label")
            and isinstance(x.get("page_start"), int)
            and isinstance(x.get("page_end"), int)
            and x["page_start"] >= 1
            for x in cits
        ),
        str(cits[:2]),
    )
    artifact_id = art["artifact_id"]

    print("\n[8] 带锚点追问 -> 局部 patch（P0-10）")
    v1 = c.get(f"/api/artifacts/{artifact_id}").json()["data"]
    before = {i: json.dumps(s, sort_keys=True, ensure_ascii=False) for i, s in enumerate(v1["spec"]["slides"])}
    with c.stream(
        "POST",
        "/api/chat/stream",
        json={
            "workspace_id": ws_id,
            "query": "这一页讲得不清楚，帮我拆解一下公民权条件",
            "mode": "syllabus_strict",
            "anchor": {
                "artifact_id": artifact_id,
                "node_path": "/slides/2",
                "node_label": "第 3 页",
                "page": 76,
                "section_code": "4.1.3",
            },
        },
    ) as resp:
        raw2 = "".join(resp.iter_text())
    ev2 = dict(parse_sse(raw2))
    check("动作为 patch", ev2["artifact"]["action"] == "patch", str(ev2["artifact"].get("ops"))[:80])
    check("version 递增到 2", ev2["artifact"]["version"] == 2, str(ev2["artifact"]["version"]))
    ops = ev2["artifact"]["ops"]
    check("ops 只指向 /slides/2", all(o["node_path"] == "/slides/2" for o in ops),
          str([o["node_path"] for o in ops]))

    v2 = c.get(f"/api/artifacts/{artifact_id}").json()["data"]
    after = {i: json.dumps(s, sort_keys=True, ensure_ascii=False) for i, s in enumerate(v2["spec"]["slides"])}
    changed = [i for i in before if before[i] != after[i]]
    check("仅目标子树变化（其余字节级不变）", changed == [2], f"变化页={changed}")

    print("\n[9] 版本与消息持久化（P0-11 / P0-01②）")
    versions = c.get(f"/api/artifacts/{artifact_id}/versions").json()["data"]
    check("版本列表含 v1/v2", [v["version"] for v in versions] == [1, 2],
          str([v["version"] for v in versions]))
    check("v1 patchable 标记存在", all("patchable" in v for v in versions))
    msgs = c.get(f"/api/workspaces/{ws_id}/messages").json()["data"]
    check("消息已落库 4 条", len(msgs) == 4, f"{len(msgs)} 条")
    check("锚点随消息持久化", any(m.get("anchor") for m in msgs))

    print("\n[10] 边界与错误码")
    r = c.post(f"/api/artifacts/{artifact_id}/patch", json={
        "artifact_id": artifact_id, "base_version": 99,
        "ops": [{"op": "update_node", "node_path": "/slides/0", "payload": {"title": "x"}}],
    })
    check("base_version 冲突 -> 409", r.status_code == 409, str(r.status_code))
    r = c.post(f"/api/artifacts/{artifact_id}/patch", json={
        "artifact_id": artifact_id, "base_version": 2,
        "ops": [{"op": "update_node", "node_path": "/slides/99", "payload": {"title": "x"}}],
    })
    check("非法 node_path -> 400/422", r.status_code in (400, 422), str(r.status_code))

    print("\n" + "=" * 60)
    if FAIL:
        print(f"结果: {len(FAIL)} 项失败 -> {FAIL}")
    else:
        print("结果: 全部通过")


if __name__ == "__main__":
    main()

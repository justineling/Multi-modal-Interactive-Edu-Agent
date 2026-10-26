"""真实 LLM 链路验证（DeepSeek）。

与 e2e_smoke.py 的区别：那个验的是"流程对不对"，这个验的是"真实模型生成的内容质量"。
重点观察：
  1. demo_mode 是否为 false
  2. 真实 TTFT / 总耗时（对比 mock 的 7ms）
  3. 生成的 PPT 内容是否真的理解了课本（而不是模板填充）
  4. 严格模式的页码引用是否真实存在
  5. 带锚点 patch 是否只改目标子树
"""

import json
import time

import httpx

BASE = "http://127.0.0.1:8123"
PDF = "data/sample_textbook.pdf"


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        ev, data_lines = None, []
        for line in block.split("\n"):
            if line.startswith(": "):
                continue
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if ev and data_lines:
            events.append((ev, json.loads("\n".join(data_lines))))
    return events


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=600.0)

    h = c.get("/api/health").json()["data"]
    print(f"[0] demo_mode={h['demo_mode']}  embedding={h['embedding_mode']}")
    if h["demo_mode"]:
        print("!! 仍是演示模式，检查 .env 是否被读到")
        return

    r = c.post("/api/workspaces", json={"title": "真实 LLM 验证"})
    ws = r.json()["data"]["id"]
    print(f"[1] workspace={ws[:8]}")

    t0 = time.perf_counter()
    with open(PDF, "rb") as f:
        doc = c.post(
            f"/api/workspaces/{ws}/documents",
            files={"file": ("sample_textbook.pdf", f, "application/pdf")},
        ).json()["data"]
    doc_id = doc["id"]

    for _ in range(120):
        s = c.get(f"/api/documents/{doc_id}/status").json()["data"]
        if s["status"] in ("ready", "failed", "cancelled"):
            break
        time.sleep(0.3)
    print(f"[2] 解析 {s['status']}  {time.perf_counter() - t0:.2f}s  {doc['page_count']} 页")

    print("\n[3] 真实对话（严格模式）…")
    query = "4.1 Malayan Union 对苏丹的影响"
    t0 = time.perf_counter()
    with c.stream(
        "POST",
        "/api/chat/stream",
        json={"workspace_id": ws, "query": query, "mode": "syllabus_strict"},
    ) as resp:
        raw = "".join(resp.iter_text())
    wall = time.perf_counter() - t0

    events = parse_sse(raw)
    names = [e for e, _ in events]
    payload = {}
    for k, v in events:
        payload.setdefault(k, v)

    token_count = sum(1 for n in names if n == "token")
    print(f"    事件数={len(events)}  token 块={token_count}  墙钟={wall:.1f}s")
    print(f"    事件序列={names[:4]}…{names[-3:]}")

    end = payload.get("turn_end", {})
    print(f"    TTFT={end.get('ttft_ms')}ms  duration={end.get('duration_ms')}ms  demo={end.get('demo_mode')}")

    ret = payload.get("retrieval", {})
    print(f"    grader={ret.get('grader_status')}  scope={ret.get('section_scope')}  top={ret.get('top_score')}")

    dec = payload.get("modality_decision", {})
    print(f"    路由={dec.get('artifact_type')}  理由={dec.get('reason')}")

    art = payload.get("artifact", {})
    spec = art.get("spec") or {}
    slides = spec.get("slides") or []
    print(f"\n[4] 产物：{art.get('artifact_type')}  {len(slides)} 页  version={art.get('version')}")

    for i, s in enumerate(slides):
        layout = s.get("layout")
        title = s.get("title", "")
        bullets = s.get("bullets") or []
        notes = (s.get("speaker_notes") or "")[:60]
        print(f"    P{i + 1} [{layout:14}] {title}")
        for b in bullets[:3]:
            print(f"         · {b[:70]}")
        if notes:
            print(f"         讲解: {notes}…")
        print(f"         出处: {s.get('source_ref')}")

    cits = payload.get("citations", {}).get("citations") or []
    print(f"\n[5] 引用 {len(cits)} 条:")
    for x in cits:
        print(f"    {x['label']}")

    # 正文里是否真有页码标记
    body = "".join(v.get("delta", "") for k, v in events if k == "token")
    import re

    pages = re.findall(r"p\.(\d+)", body)
    print(f"\n[6] 正文 {len(body)} 字，页码标记 {len(pages)} 处: {pages[:8]}")

    print("\n[7] 带锚点追问（真实 patch）…")
    t0 = time.perf_counter()
    with c.stream(
        "POST",
        "/api/chat/stream",
        json={
            "workspace_id": ws,
            "query": "这一页的公民权条件拆细一点，并说明对非马来人的影响",
            "mode": "syllabus_strict",
            "anchor": {
                "artifact_id": art["artifact_id"],
                "node_path": "/slides/2",
                "node_label": "第 3 页",
                "page": 76,
                "section_code": ret.get("section_scope") or "4.1",
            },
        },
    ) as resp:
        raw2 = "".join(resp.iter_text())
    wall2 = time.perf_counter() - t0
    ev2 = {}
    for k, v in parse_sse(raw2):
        ev2.setdefault(k, v)

    a2 = ev2.get("artifact", {})
    print(f"    action={a2.get('action')}  version={a2.get('version')}  base={a2.get('base_version')}")
    for op in a2.get("ops") or []:
        print(f"    op={op['op']}  path={op['node_path']}  字段={list((op.get('payload') or {}).keys())}")
    print(f"    耗时={wall2:.1f}s  TTFT={ev2.get('turn_end', {}).get('ttft_ms')}ms")

    # 验证只有目标页变化
    v1 = c.get(f"/api/artifacts/{art['artifact_id']}?version=1").json()["data"]["spec"]["slides"]
    v2 = c.get(f"/api/artifacts/{art['artifact_id']}").json()["data"]["spec"]["slides"]
    changed = [i for i in range(min(len(v1), len(v2)))
               if json.dumps(v1[i], sort_keys=True) != json.dumps(v2[i], sort_keys=True)]
    print(f"\n[8] 变化的页: {changed}（期望 [2]）")
    if changed == [2]:
        print("    ✅ 只有目标子树变化")
    else:
        print("    ⚠️  影响面超出预期")


if __name__ == "__main__":
    main()

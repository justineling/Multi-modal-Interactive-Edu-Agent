import { useEffect } from "react";

import { getDocument, getDocumentStatus } from "@/api/client";
import { useWorkspaceStore } from "@/store/useWorkspaceStore";

/** 轮询间隔。后端解析 113 页约 0.06~0.5s，1.5s 足够，且不会把 SQLite 打满。 */
export const DOCUMENT_POLL_INTERVAL_MS = 1500;

function isTerminal(status: string): boolean {
  return status === "ready" || status === "failed" || status === "cancelled";
}

/**
 * 文档解析进度轮询。
 *
 * 只轮询处于 `pending` / `parsing` 的文档（五态里的两个非终态），终态立刻停止。
 * 状态变 `ready` 时**额外拉一次完整文档**——轮询接口刻意不带 `toc_tree`（省流量），
 * 而目录树正是 ready 之后唯一要看的东西，不补这一枪会导致目录永远是空的。
 *
 * 依赖项用「待轮询 id 拼成的字符串」而不是 `documents` 数组：
 * 否则每次 `progress` 变化都会重建 interval，等于自己给自己制造抖动。
 */
export function useDocumentPolling(intervalMs: number = DOCUMENT_POLL_INTERVAL_MS): void {
  const pendingKey = useWorkspaceStore((state) =>
    state.documents
      .filter((document) => !isTerminal(document.status))
      .map((document) => document.id)
      .join(","),
  );

  useEffect(() => {
    if (pendingKey.length === 0) {
      return;
    }
    const ids = pendingKey.split(",");
    const controller = new AbortController();
    let stopped = false;

    const tick = async (): Promise<void> => {
      for (const id of ids) {
        if (stopped) {
          return;
        }
        const before = useWorkspaceStore.getState().documents.find((item) => item.id === id);
        if (before === undefined || isTerminal(before.status)) {
          continue;
        }
        try {
          const status = await getDocumentStatus(id, controller.signal);
          if (stopped) {
            return;
          }
          if (status.status === "ready") {
            const full = await getDocument(id, controller.signal);
            if (stopped) {
              return;
            }
            useWorkspaceStore.getState().upsertDocument(full);
            continue;
          }
          if (status.status === before.status && status.progress === before.progress) {
            continue;
          }
          useWorkspaceStore.getState().upsertDocument({
            ...before,
            status: status.status,
            progress: status.progress,
            error_code: status.error_code,
            error_message: status.error_message,
          });
        } catch {
          // 单次轮询失败（网络抖动 / 文档被删）不打断整体轮询，下一拍继续
        }
      }
    };

    void tick();
    const timer = window.setInterval(() => {
      void tick();
    }, intervalMs);

    return () => {
      stopped = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [pendingKey, intervalMs]);
}

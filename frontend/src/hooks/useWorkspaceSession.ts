import { useCallback, useEffect, useState } from "react";

import {
  createWorkspace,
  getHealth,
  listArtifacts,
  listDocuments,
  listMessages,
  listWorkspaces,
  toApiError,
} from "@/api/client";
import { useWorkspaceStore } from "@/store/useWorkspaceStore";
import type { WorkspaceOut } from "@/types/api";

export interface SessionState {
  bootstrapping: boolean;
  bootError: string | null;
  /** 手动重试引导（后端刚启动 / 网络恢复时用） */
  retry: () => void;
}

/**
 * 首次进入工作区时的引导。
 *
 * 做四件事，缺一件都会让首屏体验断掉：
 * 1. 拉 workspace 列表；**列表为空则自动建一个**（零配置开箱即用，P0-12 演示要求）。
 * 2. 取 `GET /api/health` 的 `demo_mode` 作为顶栏黄标初始值。
 * 3. 当前 workspace 变化时，并行拉 documents / messages / artifacts（P0-01② 刷新恢复）。
 * 4. 全部请求挂 `AbortSignal`，切换 workspace 或卸载时立刻取消。
 *
 * ⚠️ StrictMode 下 effect 会跑两遍：自动建 workspace 如果跑两次就会建出两个。
 * 因此「列表为空则建一个」这一步用**模块级 Promise 去重**（同一时刻只允许一个在飞），
 * 第二个挂载复用第一个的结果。
 */
let bootstrapPromise: Promise<WorkspaceOut[]> | null = null;

async function bootstrapWorkspaces(): Promise<WorkspaceOut[]> {
  if (bootstrapPromise !== null) {
    return bootstrapPromise;
  }
  bootstrapPromise = (async () => {
    const list = await listWorkspaces();
    if (list.length > 0) {
      return list;
    }
    const created = await createWorkspace({ title: "我的学习工作区" });
    return [created];
  })();
  try {
    return await bootstrapPromise;
  } finally {
    bootstrapPromise = null;
  }
}

/**
 * 健康检查只跑一次（模块级标记，**不是** useRef）。
 *
 * 这里踩过一个坑：StrictMode 下 effect 是「同一实例内 mount → cleanup → mount」，
 * ref 会跨两次执行保留，所以用 `useRef` 做「只跑一次」的守卫会让第一次请求
 * 被自己的 cleanup abort 掉、第二次又被 ref 挡住 —— 结果是 `demo_mode` 永远是 false，
 * 顶栏黄标不出现。改成模块级标记（只在成功后才置位）即可：dev 下最多多发一次幂等 GET。
 */
let healthFetched = false;

export function useWorkspaceSession(): SessionState {
  const currentWorkspaceId = useWorkspaceStore((state) => state.currentWorkspaceId);
  const [bootstrapping, setBootstrapping] = useState<boolean>(true);
  const [bootError, setBootError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState<number>(0);

  const retry = useCallback((): void => {
    setBootError(null);
    setBootstrapping(true);
    setRetryToken((value) => value + 1);
  }, []);

  // ---- 1. workspace 列表 + 健康检查（只做一次，重试时重跑） ----
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    const run = async (): Promise<void> => {
      try {
        const list = await bootstrapWorkspaces();
        if (cancelled) {
          return;
        }
        useWorkspaceStore.getState().setWorkspaces(list);
        setBootError(null);
      } catch (error) {
        if (!cancelled) {
          setBootError(toApiError(error).displayMessage);
        }
      } finally {
        if (!cancelled) {
          setBootstrapping(false);
        }
      }
    };

    void run();

    if (!healthFetched) {
      getHealth(controller.signal)
        .then((health) => {
          healthFetched = true;
          if (!cancelled) {
            useWorkspaceStore.getState().setDemoMode(health.demo_mode);
          }
        })
        .catch(() => {
          // 健康检查失败不阻塞主流程：SSE 的 turn_start 还会再给一次 demo_mode
          healthFetched = false;
        });
    }

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [retryToken]);

  // ---- 2. 当前 workspace 的资源（刷新恢复） ----
  useEffect(() => {
    if (currentWorkspaceId === null) {
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    const store = useWorkspaceStore.getState();

    store.setLoading("documents", true);
    store.setLoading("messages", true);
    store.setLoading("artifacts", true);

    const load = async (): Promise<void> => {
      const [documents, messages, artifacts] = await Promise.all([
        listDocuments(currentWorkspaceId, controller.signal).catch(() => null),
        listMessages(currentWorkspaceId, {}, controller.signal).catch(() => null),
        listArtifacts(currentWorkspaceId, controller.signal).catch(() => null),
      ]);
      if (cancelled) {
        return;
      }
      const target = useWorkspaceStore.getState();
      if (documents !== null) {
        target.setDocuments(documents);
      }
      if (messages !== null) {
        target.setMessages(messages);
      }
      if (artifacts !== null) {
        target.setArtifacts(artifacts);
      }
    };

    void load().finally(() => {
      if (cancelled) {
        return;
      }
      const target = useWorkspaceStore.getState();
      target.setLoading("documents", false);
      target.setLoading("messages", false);
      target.setLoading("artifacts", false);
    });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [currentWorkspaceId]);

  return { bootstrapping, bootError, retry };
}

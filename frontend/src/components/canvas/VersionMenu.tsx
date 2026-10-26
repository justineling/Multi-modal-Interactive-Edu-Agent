import { useCallback, useEffect, useState } from "react";
import type { ReactElement } from "react";

import { listArtifactVersions, toApiError } from "@/api/client";
import { cx } from "@/components/common/cx";
import type { ArtifactVersionOut } from "@/types/api";

interface VersionMenuProps {
  artifactId: string;
  /** 最新版本号（artifact 当前 version） */
  latestVersion: number;
  /** 当前正在查看的版本；null 表示跟随最新 */
  viewingVersion: number | null;
  /** 选择版本：传 null 回到最新 */
  onSelect: (version: number | null) => void;
  /** 关闭菜单 */
  onClose: () => void;
}

function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * 版本菜单（P0-11③：历史版本只读、可回看）。
 *
 * `patchable=false` 的版本（超出 `ARTIFACT_VERSION_KEEP=20` 窗口）**禁用**而不是隐藏：
 * 让学生看到「有过这些改动」本身就是教学价值，只是不能再从那么老的版本继续改。
 */
export function VersionMenu({
  artifactId,
  latestVersion,
  viewingVersion,
  onSelect,
  onClose,
}: VersionMenuProps): ReactElement {
  const [versions, setVersions] = useState<ArtifactVersionOut[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    listArtifactVersions(artifactId, controller.signal)
      .then((list) => {
        if (!cancelled) {
          // 后端按升序返回，这里倒序：最新在最上面更符合直觉
          setVersions([...list].reverse());
        }
      })
      .catch((loadError: unknown) => {
        if (!cancelled) {
          setError(toApiError(loadError).displayMessage);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [artifactId]);

  const pick = useCallback(
    (version: number | null): void => {
      onSelect(version);
      onClose();
    },
    [onSelect, onClose],
  );

  return (
    <div className="absolute right-0 top-full z-30 mt-1 w-72 rounded-md border border-line bg-surface-overlay p-1.5 shadow-floating">
      <div className="flex items-center justify-between px-1 py-0.5">
        <span className="text-xxs font-medium text-content-secondary">版本历史</span>
        <button
          type="button"
          onClick={onClose}
          className="text-xxs text-content-muted hover:text-content-primary"
        >
          关闭
        </button>
      </div>

      {error === null ? null : (
        <p className="px-1 py-1 text-xxs text-palette-red">{error}</p>
      )}

      <div className="max-h-64 overflow-y-auto">
        <button
          type="button"
          onClick={() => pick(null)}
          className={cx(
            "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left transition-colors",
            viewingVersion === null ? "bg-accent/15 text-accent" : "hover:bg-surface-raised",
          )}
        >
          <span className="text-xs">跟随最新</span>
          <span className="ml-auto font-mono text-xxs text-content-muted">v{latestVersion}</span>
        </button>

        {loading ? (
          <p className="px-2 py-1 text-xxs text-content-muted">加载中…</p>
        ) : (
          versions.map((version) => {
            const isCurrent = version.version === latestVersion;
            const disabled = !version.patchable;
            return (
              <button
                key={version.id}
                type="button"
                disabled={disabled}
                onClick={() => pick(version.version)}
                title={
                  disabled
                    ? "该版本已超出可继续修改的窗口，仅供查看"
                    : `查看 v${version.version}（${version.patch_ops === null ? "全量生成" : `${version.patch_ops.length} 个补丁`}）`
                }
                className={cx(
                  "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left transition-colors",
                  viewingVersion === version.version ? "bg-accent/15" : "hover:bg-surface-raised",
                  disabled ? "cursor-not-allowed opacity-45" : "",
                )}
              >
                <span className="font-mono text-xs text-content-secondary">v{version.version}</span>
                <span className="text-xxs text-content-muted">
                  {version.patch_ops === null ? "全量生成" : `${version.patch_ops.length} 个补丁`}
                </span>
                {isCurrent ? (
                  <span className="rounded-xs border border-palette-green/50 px-1 text-xxs text-palette-green">
                    当前
                  </span>
                ) : null}
                <span className="ml-auto font-mono text-xxs text-content-muted">
                  {formatTime(version.created_at)}
                </span>
              </button>
            );
          })
        )}
      </div>

      <p className="mt-1 border-t border-line px-1 pt-1 text-xxs leading-relaxed text-content-muted">
        历史版本只读，不能继续追问；切换回「跟随最新」即可继续修改。
      </p>
    </div>
  );
}

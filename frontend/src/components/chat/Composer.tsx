import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent, KeyboardEvent, ReactElement } from "react";

import { AnchorChip } from "@/components/chat/AnchorChip";
import { cx } from "@/components/common/cx";
import { LAYOUT } from "@/lib/theme";
import type { ArtifactType } from "@/types/artifact";
import type { Anchor } from "@/types/patch";

interface ComposerProps {
  anchor: Anchor | null;
  onClearAnchor: () => void;
  onSend: (query: string, override: ArtifactType | null) => void;
  isStreaming: boolean;
  onStop: () => void;
  disabled: boolean;
  /**
   * 外部写入草稿（点目录树节点 / 点画布空态示例问句时）。
   * 用 `token` 而不是纯文本触发，避免用户正在打字时被相同文本重置。
   */
  draft?: DraftRequest;
}

export interface DraftRequest {
  text: string;
  token: number;
}

interface OverrideOption {
  value: ArtifactType | null;
  label: string;
  hint: string;
}

/** 形态选择（P0-06②：用户显式指定时 100% 遵循） */
const OVERRIDE_OPTIONS: readonly OverrideOption[] = [
  { value: null, label: "自动", hint: "由路由根据知识点属性选择形态" },
  { value: "ppt", label: "PPT", hint: "强制生成幻灯片课件" },
  { value: "anim_2d", label: "2D", hint: "强制生成 2D 关键帧推演" },
  { value: "scene_3d", label: "3D", hint: "强制生成 3D 空间场景" },
];

const MAX_QUERY_LENGTH = 2000;

/**
 * 输入区。
 *
 * 两个容易被忽略但很致命的细节：
 * 1. **中文输入法**：`keydown` 时若 `isComposing` 为真，说明用户还在选字，
 *    此时回车是「确认候选词」而不是「发送」。不判断就会把半截拼音发出去。
 * 2. **锚点提示**：有锚点时 placeholder 必须变（「针对「第 3 页」追问…」），
 *    并明确告知「只更新该节点」，否则用户不知道自己在局部模式里。
 */
export function Composer({
  anchor,
  onClearAnchor,
  onSend,
  isStreaming,
  onStop,
  disabled,
  draft,
}: ComposerProps): ReactElement {
  const [value, setValue] = useState<string>("");
  const [override, setOverride] = useState<ArtifactType | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const draftTokenRef = useRef<number>(-1);

  // 外部草稿：令牌变化才覆盖（同一段文本点两次也要能重新填入，故不能只比文本）
  useEffect(() => {
    if (draft === undefined || draft.token === draftTokenRef.current) {
      return;
    }
    draftTokenRef.current = draft.token;
    setValue(draft.text.slice(0, MAX_QUERY_LENGTH));
    textareaRef.current?.focus();
  }, [draft]);

  // 高度自适应：最多 8 行，超出滚动
  useEffect(() => {
    const node = textareaRef.current;
    if (node === null) {
      return;
    }
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 168)}px`;
  }, [value]);

  const submit = useCallback((): void => {
    const query = value.trim();
    if (query.length === 0 || disabled || isStreaming) {
      return;
    }
    onSend(query, override);
    setValue("");
  }, [value, disabled, isStreaming, onSend, override]);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>): void => {
      if (event.key !== "Enter" || event.shiftKey) {
        return;
      }
      // 输入法选字中：回车用于确认候选词，不触发发送
      if (event.nativeEvent.isComposing) {
        return;
      }
      event.preventDefault();
      submit();
    },
    [submit],
  );

  const handleChange = useCallback((event: ChangeEvent<HTMLTextAreaElement>): void => {
    setValue(event.target.value.slice(0, MAX_QUERY_LENGTH));
  }, []);

  const placeholder =
    anchor === null
      ? "问我任何课本里的知识点，例如「学习 4.1」「用 3D 讲讲这座建筑的构造」"
      : `针对「${anchor.node_label.length > 0 ? anchor.node_label : anchor.node_path}」追问…`;

  return (
    <div className="border-t border-line bg-surface-base px-3 py-2.5" style={{ minHeight: LAYOUT.composerMinHeight }}>
      {anchor === null ? null : (
        <div className="mb-2">
          <AnchorChip anchor={anchor} onClear={onClearAnchor} />
          <p className="mt-1 text-xxs text-content-muted">回答将只更新该节点，画布其余部分保持不变</p>
        </div>
      )}

      <div
        className={cx(
          "rounded-md border bg-surface-raised transition-colors",
          anchor === null ? "border-line" : "border-accent/50",
        )}
      >
        <textarea
          ref={textareaRef}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          rows={2}
          disabled={disabled}
          className="w-full resize-none bg-transparent px-3 py-2 text-sm leading-relaxed text-content-primary placeholder:text-content-muted focus:outline-none disabled:cursor-not-allowed"
        />

        <div className="flex items-center gap-1.5 border-t border-line/70 px-2 py-1.5">
          <div className="flex items-center gap-1" role="group" aria-label="形态选择">
            {OVERRIDE_OPTIONS.map((option) => (
              <button
                key={option.label}
                type="button"
                title={option.hint}
                onClick={() => setOverride(option.value)}
                className={cx(
                  "rounded-xs px-1.5 py-0.5 text-xxs transition-colors",
                  override === option.value
                    ? "bg-accent/20 text-accent"
                    : "text-content-muted hover:bg-surface-overlay hover:text-content-secondary",
                )}
              >
                {option.label}
              </button>
            ))}
          </div>

          <span className="ml-1 truncate text-xxs text-content-muted">
            {isStreaming ? "生成中…（可随时停止）" : "Enter 发送 · Shift+Enter 换行"}
          </span>

          <div className="ml-auto flex items-center gap-1.5">
            {value.length > 0 ? (
              <span className="font-mono text-xxs text-content-muted">
                {value.length}/{MAX_QUERY_LENGTH}
              </span>
            ) : null}
            {isStreaming ? (
              <button
                type="button"
                onClick={onStop}
                className="rounded-sm border border-palette-red/50 px-2 py-1 text-xxs text-palette-red transition-colors hover:bg-palette-red/10"
              >
                停止
              </button>
            ) : (
              <button
                type="button"
                onClick={submit}
                disabled={disabled || value.trim().length === 0}
                className="rounded-sm bg-accent px-2.5 py-1 text-xxs font-medium text-accent-foreground transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
              >
                发送
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

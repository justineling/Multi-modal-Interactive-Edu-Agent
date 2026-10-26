import { Component } from "react";
import type { ErrorInfo, ReactElement, ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
  /** 出错时的兜底 UI；不传则用默认样式 */
  fallback?: (error: Error, reset: () => void) => ReactNode;
  /** 出错时上报（默认打 console.error，便于演示现场定位） */
  onError?: (error: Error, info: ErrorInfo) => void;
  /** 变更该值可自动重置边界（例如切换到另一张幻灯片时） */
  resetKey?: string;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * 渲染器兜底。
 *
 * 为什么必须有：三种 AST 由 LLM 填充，即使 Pydantic 校验通过，也可能出现
 * 「字段合法但组合异常」（例如 quiz 的 answer_index 恰好等于 options 长度-1 且
 * 前端某处数组越界）。React 的默认行为是整棵树卸载 → **白屏**，
 * 而产品要求是「绝不白屏，降级为可读的错误提示 + 重试」。
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    if (this.props.onError !== undefined) {
      this.props.onError(error, info);
      return;
    }
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  override componentDidUpdate(previous: ErrorBoundaryProps): void {
    // 切换到另一张幻灯片时自动复位，避免上一页的错误一直挂住
    if (this.state.error !== null && previous.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  private readonly reset = (): void => {
    this.setState({ error: null });
  };

  override render(): ReactNode {
    const { error } = this.state;
    if (error === null) {
      return this.props.children;
    }
    if (this.props.fallback !== undefined) {
      return this.props.fallback(error, this.reset);
    }
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-3 p-6 text-center">
        <p className="text-sm font-medium text-palette-red">这一页渲染失败</p>
        <p className="max-w-md font-mono text-xs leading-relaxed text-content-muted">
          {error.message}
        </p>
        <button
          type="button"
          onClick={this.reset}
          className="rounded-sm border border-line-strong px-3 py-1 text-xs text-content-secondary hover:bg-surface-raised"
        >
          重试渲染
        </button>
      </div>
    );
  }
}

export function renderFallback(error: Error, reset: () => void): ReactElement {
  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-3 p-6 text-center">
      <p className="text-sm font-medium text-palette-red">渲染失败</p>
      <p className="max-w-md font-mono text-xs text-content-muted">{error.message}</p>
      <button
        type="button"
        onClick={reset}
        className="rounded-sm border border-line-strong px-3 py-1 text-xs text-content-secondary hover:bg-surface-raised"
      >
        重试
      </button>
    </div>
  );
}

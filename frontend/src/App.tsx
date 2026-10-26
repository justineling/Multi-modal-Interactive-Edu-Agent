import type { ReactElement } from "react";

import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { SplitWorkspace } from "@/components/layout/SplitWorkspace";

/**
 * 应用入口。
 *
 * 最外层套一个 ErrorBoundary：任何未捕获的渲染异常都降级为「可读错误 + 重试」，
 * 而不是把整个页面卸载成白屏（演示场景尤其致命）。
 */
export default function App(): ReactElement {
  return (
    <ErrorBoundary>
      <SplitWorkspace />
    </ErrorBoundary>
  );
}

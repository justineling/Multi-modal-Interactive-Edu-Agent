import { Allotment } from "allotment";
import type { ReactElement } from "react";

import { LAYOUT } from "@/lib/theme";

/**
 * 顶层布局（**B1 占位版**）。
 *
 * 本轮只验证三件事：
 * 1. Allotment 双栏能渲染、能拖动、min size 生效；
 * 2. 深色主题变量与 Tailwind 自定义色板生效；
 * 3. 后续 B3 的挂载点位置明确（左 ChatPanel / 右 ArtifactHost）。
 *
 * 真正的 `components/layout/SplitWorkspace.tsx`、`TopBar.tsx` 等留到 B3，
 * 因此这里刻意不抽组件、不引 store，避免下一轮出现「占位版与正式版并存」的歧义。
 */
export default function App(): ReactElement {
  return (
    <div className="flex h-full w-full flex-col overflow-hidden bg-surface-base text-content-primary">
      {/* 顶栏占位：B3 的 TopBar 会接管 workspace 切换、新建与演示模式黄标 */}
      <header
        className="flex shrink-0 items-center gap-3 border-b border-line bg-surface-raised px-4"
        style={{ height: LAYOUT.topBarHeight }}
      >
        <span className="text-sm font-semibold tracking-wide">多模态沉浸式智能助教</span>
        <span className="rounded-full border border-palette-amber/40 bg-palette-amber/10 px-2 py-0.5 text-xxs text-palette-amber">
          演示模式
        </span>
        <span className="ml-auto font-mono text-xxs text-content-muted">
          B1 骨架 · 占位双栏 · 可拖动
        </span>
      </header>

      <main className="min-h-0 flex-1">
        <Allotment
          defaultSizes={[LAYOUT.chatDefaultPercent, 100 - LAYOUT.chatDefaultPercent]}
          vertical={false}
        >
          {/* 左：对话区（B3 挂载 ChatPanel / FileDrawer / Composer） */}
          <Allotment.Pane minSize={LAYOUT.chatMinWidth} preferredSize="42%">
            <section className="flex h-full flex-col border-r border-line bg-surface-base">
              <div className="flex items-center gap-2 border-b border-line px-4 py-3">
                <h2 className="text-sm font-medium text-content-primary">对话区</h2>
                <span className="font-mono text-xxs text-content-muted">
                  ChatPanel · 待 B3
                </span>
              </div>
              <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
                <p className="text-sm text-content-secondary">
                  这里将承载教材目录、消息流与输入区
                </p>
                <p className="max-w-[26rem] text-xs leading-relaxed text-content-muted">
                  上传 PDF 后在此提问；点击右侧画布元素会出现「锚点 chip」，
                  追问时只改动被锚定的那一块内容。
                </p>
              </div>
              <div className="border-t border-line px-4 py-3">
                <div className="rounded-md border border-dashed border-line-strong px-3 py-2 text-xs text-content-muted">
                  输入区占位（Composer 待 B3）
                </div>
              </div>
            </section>
          </Allotment.Pane>

          {/* 右：画布区（B4/B5 挂载 ArtifactHost → Slide/2D/3D 渲染器） */}
          <Allotment.Pane minSize={LAYOUT.canvasMinWidth}>
            <section className="flex h-full flex-col bg-surface-sunken">
              <div className="flex items-center gap-2 border-b border-line px-4 py-3">
                <h2 className="text-sm font-medium text-content-primary">画布区</h2>
                <span className="font-mono text-xxs text-content-muted">
                  ArtifactHost · 待 B4
                </span>
              </div>
              <div className="canvas-baseline flex flex-1 items-center justify-center">
                <div className="flex flex-col items-center gap-3 px-8 text-center">
                  <div className="flex gap-2">
                    {(["PPT", "2D", "3D"] as const).map((label) => (
                      <span
                        key={label}
                        className="rounded-sm border border-line bg-surface-raised px-3 py-1 font-mono text-xxs text-content-secondary"
                      >
                        {label}
                      </span>
                    ))}
                  </div>
                  <p className="text-sm text-content-secondary">
                    三种形态的确定性渲染器将在此挂载
                  </p>
                  <p className="max-w-[28rem] text-xs leading-relaxed text-content-muted">
                    后端只输出受控 JSON AST（SlideDeckSpec / Keyframe2DSpec / SceneGraph3DSpec），
                    前端按 <code className="font-mono">spec.type</code> 策略分发，不执行任何模型生成的代码。
                  </p>
                </div>
              </div>
            </section>
          </Allotment.Pane>
        </Allotment>
      </main>
    </div>
  );
}

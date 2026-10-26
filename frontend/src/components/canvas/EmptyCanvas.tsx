import type { ReactElement } from "react";

interface EmptyCanvasProps {
  onSamplePrompt: (text: string) => void;
}

const EXAMPLES: ReadonlyArray<{ text: string; hint: string }> = [
  { text: "学习 1.1，帮我梳理这一节的要点", hint: "→ 生成 PPT 课件" },
  { text: "用 2D 动画演示这个过程", hint: "→ 生成关键帧推演" },
  { text: "用 3D 展示这个空间结构", hint: "→ 生成 3D 场景" },
];

const MODALITIES: ReadonlyArray<{ label: string; desc: string; color: string }> = [
  { label: "PPT 课件", desc: "概念演变、要点梳理、对比与测验", color: "text-palette-blue" },
  { label: "2D 推演", desc: "机制流程、疆域航线、受力过程", color: "text-palette-sky" },
  { label: "3D 场景", desc: "空间结构、文物建筑、立体构造", color: "text-palette-indigo" },
];

/**
 * 画布空态。
 *
 * 空态不是「空白」，而是**第一次教学机会**：直接给出可点击的示例问句，
 * 学生不需要先学会「怎么问才能生成画布」。
 */
export function EmptyCanvas({ onSamplePrompt }: EmptyCanvasProps): ReactElement {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-5 px-8 py-6 text-center">
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-content-secondary">上传教材后，我可以生成三种形态的画布</p>
        <p className="text-xxs text-content-muted">点击下面的问句，会直接填进左侧输入框</p>
      </div>

      <div className="grid w-full max-w-lg grid-cols-3 gap-2">
        {MODALITIES.map((item) => (
          <div
            key={item.label}
            className="rounded-sm border border-line bg-surface-raised px-2 py-2 text-left"
          >
            <p className={`text-xs font-medium ${item.color}`}>{item.label}</p>
            <p className="mt-0.5 text-xxs leading-relaxed text-content-muted">{item.desc}</p>
          </div>
        ))}
      </div>

      <div className="flex w-full max-w-md flex-col gap-1.5">
        {EXAMPLES.map((example) => (
          <button
            key={example.text}
            type="button"
            onClick={() => onSamplePrompt(example.text)}
            className="flex items-center gap-2 rounded-sm border border-line bg-surface-base px-3 py-2 text-left transition-colors hover:border-accent/50 hover:bg-surface-raised"
          >
            <span className="flex-1 text-xs text-content-secondary">{example.text}</span>
            <span className="shrink-0 font-mono text-xxs text-content-muted">{example.hint}</span>
          </button>
        ))}
      </div>

      <p className="max-w-sm text-xxs leading-relaxed text-content-muted">
        画布内容由受控 JSON 描述、前端确定性渲染 —— 模型不会直接生成代码，因此不会白屏。
      </p>
    </div>
  );
}

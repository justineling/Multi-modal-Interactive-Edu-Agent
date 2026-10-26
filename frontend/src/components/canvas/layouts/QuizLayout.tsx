import { useState } from "react";
import type { ReactElement } from "react";

import { MissingField, SlideCanvas, SlideHeading, truncateList } from "@/components/canvas/layouts/shared";
import type { SlideLayoutProps } from "@/components/canvas/layouts/shared";
import type { QuizQuestion } from "@/types/artifact";

const MAX_QUESTIONS = 2;
const OPTION_LABELS = ["A", "B", "C", "D", "E", "F"] as const;

function optionLabel(index: number): string {
  return OPTION_LABELS[index] ?? String(index + 1);
}

interface QuestionCardProps {
  question: QuizQuestion;
  index: number;
  total: number;
}

/**
 * 单题卡片：点选项后显形对错与解析。
 *
 * 这是 6 种布局里**唯一需要交互**的一个，因此必须能承受「学生乱点」：
 * - 选项越界（`answer_index >= options.length`）时降级为「无正确答案」，不崩；
 * - 选中后再点其它选项不改变判定（只揭示一次），避免学生靠穷举找答案。
 */
function QuestionCard({ question, index, total }: QuestionCardProps): ReactElement {
  const [picked, setPicked] = useState<number | null>(null);
  const hasValidAnswer = question.answer_index >= 0 && question.answer_index < question.options.length;
  const revealed = picked !== null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span
          style={{
            padding: "2px 10px",
            borderRadius: 999,
            backgroundColor: "#6366F1",
            color: "#FFFFFF",
            fontSize: 14,
            fontWeight: 600,
          }}
        >
          随堂测验 {index + 1}/{total}
        </span>
        <p style={{ margin: 0, fontSize: 24, lineHeight: 1.4, color: "#0F172A", fontWeight: 600 }}>
          {question.question}
        </p>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {question.options.map((option, optionIndex) => {
          const isAnswer = hasValidAnswer && optionIndex === question.answer_index;
          const isPicked = picked === optionIndex;
          const wrongPick = revealed && isPicked && !isAnswer;

          let border = "1px solid #E2E8F0";
          let background = "#FFFFFF";
          if (revealed && isAnswer) {
            border = "1px solid #10B981";
            background = "#ECFDF5";
          } else if (wrongPick) {
            border = "1px solid #EF4444";
            background = "#FEF2F2";
          }

          return (
            <button
              key={`${optionIndex}-${option}`}
              type="button"
              onClick={() => {
                if (!revealed) {
                  setPicked(optionIndex);
                }
              }}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "10px 14px",
                borderRadius: 10,
                border,
                background,
                textAlign: "left",
                cursor: revealed ? "default" : "pointer",
                transition: "background-color 150ms, border-color 150ms",
              }}
            >
              <span
                style={{
                  flexShrink: 0,
                  width: 26,
                  height: 26,
                  borderRadius: 8,
                  backgroundColor: revealed && isAnswer ? "#10B981" : "#E2E8F0",
                  color: revealed && isAnswer ? "#FFFFFF" : "#475569",
                  fontSize: 14,
                  fontWeight: 600,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                {optionLabel(optionIndex)}
              </span>
              <span style={{ fontSize: 20, lineHeight: 1.4, color: "#1E293B" }}>{option}</span>
              {revealed && isAnswer ? (
                <span style={{ marginLeft: "auto", fontSize: 16, color: "#10B981" }}>正确</span>
              ) : null}
              {wrongPick ? (
                <span style={{ marginLeft: "auto", fontSize: 16, color: "#EF4444" }}>已选</span>
              ) : null}
            </button>
          );
        })}
      </div>

      {revealed ? (
        <div
          style={{
            padding: "10px 14px",
            borderRadius: 10,
            backgroundColor: "#F8FAFC",
            borderLeft: "3px solid #6366F1",
          }}
        >
          <p style={{ margin: 0, fontSize: 17, lineHeight: 1.55, color: "#334155" }}>
            {hasValidAnswer ? `正确答案：${optionLabel(question.answer_index)}. ` : "本题缺少有效答案。"}
            {question.explanation.length > 0 ? question.explanation : "（暂无解析）"}
          </p>
        </div>
      ) : (
        <p style={{ margin: 0, fontSize: 15, color: "#94A3B8" }}>点击选项查看答案与解析</p>
      )}
    </div>
  );
}

/**
 * `quiz` —— 随堂测验。
 *
 * 数据来自后端扩展字段 `quiz_questions: [{question, options, answer_index, explanation}]`。
 * 版面按「一页最多 2 题」控制：再多就变成试卷而不是课件页了。
 */
export function QuizLayout({ slide }: SlideLayoutProps): ReactElement {
  const { visible, hidden } = truncateList(slide.quiz_questions, MAX_QUESTIONS);

  if (visible.length === 0) {
    return (
      <SlideCanvas>
        <SlideHeading title={slide.title} subtitle={slide.subtitle} />
        <MissingField label="测验题目（quiz_questions）" />
      </SlideCanvas>
    );
  }

  return (
    <SlideCanvas>
      <SlideHeading title={slide.title} subtitle={slide.subtitle} />
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 22,
          flex: 1,
          minHeight: 0,
          justifyContent: "center",
        }}
      >
        {visible.map((question, index) => (
          <QuestionCard
            key={`${index}-${question.question}`}
            question={question}
            index={index}
            total={visible.length + hidden}
          />
        ))}
      </div>
      {hidden > 0 ? (
        <p style={{ marginTop: 12, fontSize: 16, color: "#94A3B8" }}>…等 {hidden} 题未显示</p>
      ) : null}
    </SlideCanvas>
  );
}

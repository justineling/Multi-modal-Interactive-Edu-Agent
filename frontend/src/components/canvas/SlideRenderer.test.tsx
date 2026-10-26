/**
 * 渲染层冒烟测试。
 *
 * 用 `renderToStaticMarkup` 而不是 jsdom + testing-library：
 * 6 种布局都是**纯函数式版面**（唯一的交互态是 quiz 的「点选项」，SSR 也能渲染出初始态），
 * 因此不需要 DOM 环境就能验证「不抛错 + 关键内容真的渲染出来了」，
 * 省掉 jsdom 这一整条依赖链。
 *
 * 断言的重点不是像素，而是**降级行为**：缺字段、超长列表、未知布局都不能白屏。
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { LAYOUT_LABELS, LAYOUT_REGISTRY, getLayoutComponent } from "@/components/canvas/slideLayouts";
import type { SlideLayoutProps } from "@/components/canvas/slideLayouts";
import {
  isExtendedHeading,
  parseSourceRef,
  splitPageRefs,
} from "@/components/chat/pageRefs";
import { MarkdownView } from "@/components/chat/MarkdownView";
import { SLIDE_LAYOUTS } from "@/types/artifact";
import type { SlideLayout, SlideNode } from "@/types/artifact";

function makeSlide(overrides: Partial<SlideNode> = {}): SlideNode {
  return {
    node_id: "slide-1",
    layout: "bullet_points",
    title: "马来亚联邦的成立",
    subtitle: "Bab 4.1 · p.94",
    bullets: ["1946 年提出马来亚联邦计划", "1948 年正式成立"],
    comparison_left: null,
    comparison_right: null,
    timeline_items: null,
    quiz_questions: null,
    speaker_notes: "这一页先讲背景，再讲结果。",
    source_ref: "Form 4 Sejarah, Bab 4.1, p.94",
    ...overrides,
  };
}

function render(layout: SlideLayout, overrides: Partial<SlideNode> = {}): string {
  const Component = LAYOUT_REGISTRY[layout];
  const props: SlideLayoutProps = {
    slide: makeSlide({ layout, ...overrides }),
    pageIndex: 0,
    totalPages: 3,
  };
  return renderToStaticMarkup(<Component {...props} />);
}

describe("LAYOUT_REGISTRY", () => {
  it("6 种布局全部注册，没有遗漏", () => {
    expect(Object.keys(LAYOUT_REGISTRY).sort()).toEqual([...SLIDE_LAYOUTS].sort());
  });

  it("每种布局都有中文名", () => {
    for (const layout of SLIDE_LAYOUTS) {
      expect(LAYOUT_LABELS[layout].length).toBeGreaterThan(0);
    }
  });

  it("未知布局降级到要点布局而不是抛错", () => {
    // 运行时可能收到历史版本里已废弃的 layout 名，必须能兜住
    const unknown = "hero_v2" as SlideLayout;
    expect(getLayoutComponent(unknown)).toBe(LAYOUT_REGISTRY.bullet_points);
  });
});

describe("6 种布局渲染", () => {
  it("title_hero：主题 + 副标题 + 页码", () => {
    const html = render("title_hero");
    expect(html).toContain("马来亚联邦的成立");
    expect(html).toContain("Bab 4.1 · p.94");
    expect(html).toContain("1 / 3");
  });

  it("bullet_points：逐条渲染要点", () => {
    const html = render("bullet_points");
    expect(html).toContain("1946 年提出马来亚联邦计划");
    expect(html).toContain("1948 年正式成立");
  });

  it("bullet_points：超过 7 条截断并提示剩余条数", () => {
    const bullets = Array.from({ length: 10 }, (_unused, index) => `要点 ${index + 1}`);
    const html = render("bullet_points", { bullets });
    expect(html).toContain("要点 7");
    expect(html).not.toContain("要点 8");
    expect(html).toContain("…等 3 条");
  });

  it("bullet_points：没有要点时给降级提示而不是空白", () => {
    const html = render("bullet_points", { bullets: [] });
    expect(html).toContain("本页缺少");
  });

  it("comparison：左右两栏按行对齐渲染", () => {
    const html = render("comparison", {
      comparison_left: ["中央集权", "由英王任命"],
      comparison_right: ["联邦制", "由本地选举"],
    });
    expect(html).toContain("对比 A");
    expect(html).toContain("对比 B");
    expect(html).toContain("中央集权");
    expect(html).toContain("由本地选举");
  });

  it("comparison：缺字段时降级提示", () => {
    const html = render("comparison", { comparison_left: null, comparison_right: null });
    expect(html).toContain("本页缺少");
  });

  it("timeline：≤5 个节点用横向时间轴，含年份与页码", () => {
    const html = render("timeline", {
      timeline_items: [
        { year: "1945", label: "二战结束", detail: "", source_page: 92 },
        { year: "1946", label: "联邦计划提出", detail: "", source_page: 93 },
      ],
    });
    expect(html).toContain("1945");
    expect(html).toContain("联邦计划提出");
    expect(html).toContain("p.93");
  });

  it("timeline：>5 个节点改纵向排布且不抛错", () => {
    const timeline_items = Array.from({ length: 8 }, (_unused, index) => ({
      year: `${1900 + index}`,
      label: `事件 ${index + 1}`,
      detail: "",
      source_page: null,
    }));
    const html = render("timeline", { timeline_items });
    expect(html).toContain("事件 7");
    expect(html).toContain("…等 1 个节点未显示");
  });

  it("timeline：缺字段时降级提示", () => {
    expect(render("timeline", { timeline_items: null })).toContain("本页缺少");
  });

  it("cause_effect：渲染箭头链条", () => {
    const html = render("cause_effect", {
      bullets: ["关税提高", "进口减少 → 本地工业兴起"],
    });
    expect(html).toContain("关税提高");
    expect(html).toContain("→");
    expect(html).toContain("本地工业兴起");
  });

  it("quiz：题干 + 选项 + 未作答提示", () => {
    const html = render("quiz", {
      quiz_questions: [
        {
          question: "马来亚联邦成立于哪一年？",
          options: ["1946", "1948", "1957"],
          answer_index: 1,
          explanation: "1948 年 2 月 1 日正式成立。",
        },
      ],
    });
    expect(html).toContain("马来亚联邦成立于哪一年？");
    expect(html).toContain("1948");
    expect(html).toContain("点击选项查看答案与解析");
    // 未点击前不应泄露答案
    expect(html).not.toContain("正确答案");
  });

  it("quiz：answer_index 越界时降级，不抛错", () => {
    const html = render("quiz", {
      quiz_questions: [
        { question: "越界题", options: ["A", "B"], answer_index: 9, explanation: "" },
      ],
    });
    expect(html).toContain("越界题");
  });
});

describe("pageRefs 解析", () => {
  it("识别 [p.112] 与 （p.76-77）", () => {
    const tokens = splitPageRefs("见 [p.112] 与（p.76-77）两处");
    const refs = tokens.filter((token) => token.kind === "pageRef");
    expect(refs).toHaveLength(2);
    expect(refs[0]).toMatchObject({ pageStart: 112, pageEnd: 112 });
    expect(refs[1]).toMatchObject({ pageStart: 76, pageEnd: 77 });
  });

  it("没有页码标记时原样返回单段文本", () => {
    const tokens = splitPageRefs("没有任何页码");
    expect(tokens).toHaveLength(1);
    expect(tokens[0]?.kind).toBe("text");
  });

  it("从 source_ref 解析章节号与页码", () => {
    expect(parseSourceRef("Form 4 Sejarah, Bab 4.1, p.94")).toMatchObject({
      section_code: "4.1",
      page: 94,
      isExtended: false,
    });
    expect(parseSourceRef("Bab 1.1.3 · p.5")).toMatchObject({ section_code: "1.1.3", page: 5 });
    expect(parseSourceRef("Extended Knowledge")).toMatchObject({
      section_code: null,
      page: null,
      isExtended: true,
    });
  });

  it("识别超纲区块标题", () => {
    expect(isExtendedHeading("深度拓展（超出课本范围）")).toBe(true);
    expect(isExtendedHeading("课本标准答案")).toBe(false);
  });
});

describe("MarkdownView", () => {
  it("渲染 LaTeX、表格与页码 chip", () => {
    const markdown = [
      "公式 $E=mc^2$ 很重要。",
      "",
      "| 年份 | 事件 |",
      "| --- | --- |",
      "| 1948 | 成立 |",
      "",
      "详见 [p.94]。",
    ].join("\n");
    const html = renderToStaticMarkup(<MarkdownView markdown={markdown} />);
    // KaTeX 会输出 katex 容器；表格走 GFM；[p.94] 变成可点击 chip
    expect(html).toContain("katex");
    expect(html).toContain("<table");
    expect(html).toContain("p.94");
  });

  it("超纲引用块渲染为「超纲拓展」徽章", () => {
    const html = renderToStaticMarkup(
      <MarkdownView markdown={"> **深度拓展（超出课本范围）**\n> 这部分不是考试基准。"} />,
    );
    expect(html).toContain("超纲拓展");
    expect(html).toContain("非考试基准");
  });

  it("不执行内联 HTML（防注入）", () => {
    const html = renderToStaticMarkup(<MarkdownView markdown={'<img src=x onerror="alert(1)" />'} />);
    // react-markdown 默认不解析原始 HTML：尖括号被转义成文本，不会产生真实的 img 节点
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });
});

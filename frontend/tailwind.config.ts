import type { Config } from "tailwindcss";

import { FONT_FAMILY, RADIUS, SHADOW } from "./src/lib/theme";

/**
 * Tailwind v3 配置（**不要升级到 v4**：v4 是 CSS-first 配置，与 `tailwind.config.ts`
 * + `postcss.config.js` 的 v3 写法不兼容，混用会直接没有样式）。
 *
 * 颜色分两层：
 * - `palette.*`：§9.5 的 8 色语义调色板，与后端 `PALETTE` 同值，用于数据可视化与状态标识。
 * - `surface/content/line/accent`：走 CSS 变量，由 `src/index.css` 的 `:root`（浅色）
 *   与 `.dark`（深色，默认）两组变量驱动，因此 `darkMode: "class"` 只需切 `<html class>`。
 *
 * 单一权威来源仍是 `src/lib/theme.ts`：这里的 hex 通过 import 取用，不再手写第二份。
 */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: {
          base: "rgb(var(--surface-base) / <alpha-value>)",
          raised: "rgb(var(--surface-raised) / <alpha-value>)",
          sunken: "rgb(var(--surface-sunken) / <alpha-value>)",
          overlay: "rgb(var(--surface-overlay) / <alpha-value>)",
        },
        content: {
          primary: "rgb(var(--content-primary) / <alpha-value>)",
          secondary: "rgb(var(--content-secondary) / <alpha-value>)",
          muted: "rgb(var(--content-muted) / <alpha-value>)",
        },
        line: {
          DEFAULT: "rgb(var(--line-subtle) / <alpha-value>)",
          subtle: "rgb(var(--line-subtle) / <alpha-value>)",
          strong: "rgb(var(--line-strong) / <alpha-value>)",
        },
        accent: {
          DEFAULT: "#2563EB",
          soft: "#1D4ED8",
          foreground: "#F8FAFC",
        },
        palette: {
          blue: "#2563EB",
          sky: "#0EA5E9",
          amber: "#F59E0B",
          red: "#EF4444",
          green: "#10B981",
          indigo: "#6366F1",
          slate: "#64748B",
          mist: "#E2E8F0",
        },
        scope: {
          /** 超纲/拓展区的描边与底色（PRD 要求视觉可区分） */
          extended: "#F59E0B",
          strict: "#10B981",
          none: "#64748B",
        },
      },
      fontFamily: {
        sans: [FONT_FAMILY.sans],
        mono: [FONT_FAMILY.mono],
      },
      fontSize: {
        /* 只补 xxs：比 Tailwind 默认最小档（text-xs=12px）再小一档，
           用于顶栏元信息、行内 code 等「不承载正文语义」的文字。
           其余档位沿用 Tailwind 默认，避免覆盖默认排版节奏。 */
        xxs: ["10px", { lineHeight: "14px" }],
      },
      borderRadius: {
        xs: `${RADIUS.xs}px`,
        sm: `${RADIUS.sm}px`,
        md: `${RADIUS.md}px`,
        lg: `${RADIUS.lg}px`,
        xl: `${RADIUS.xl}px`,
      },
      boxShadow: {
        card: SHADOW.card,
        panel: SHADOW.panel,
        floating: SHADOW.floating,
      },
      spacing: {
        topbar: "52px",
        sash: "10px",
      },
      transitionTimingFunction: {
        enter: "cubic-bezier(0.16, 1, 0.3, 1)",
        exit: "cubic-bezier(0.4, 0, 1, 1)",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "pulse-soft": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.55" },
        },
      },
      animation: {
        "fade-in": "fade-in 240ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "pulse-soft": "pulse-soft 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
} satisfies Config;

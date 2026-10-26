/**
 * PostCSS 配置（Tailwind v3 写法）。
 *
 * 顺序不能换：tailwindcss 先展开 `@tailwind` 指令，autoprefixer 再补厂商前缀。
 * 这里刻意不引入 `postcss-import`：CSS `@import` 交给 Vite 内置解析器处理
 * （`src/index.css` 顶部用 `@import "katex/dist/katex.min.css"` 引第三方样式）。
 */
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};

import { defineConfig } from "vite";

export default defineConfig({
  root: "src",
  // ★ 必须是 "/"，不能是 "/static/dist/"。
  //   实测（2026-09-27，vite 8.3.1）：base="/" 时 HTML 里的绝对路径
  //   （/static/manifest.json、/static/icon-192.png）原样保留；
  //   若写成 /static/dist/ 会被改写成 /static/dist/static/... → 404。
  base: "/",
  build: {
    // ⚠️ outDir 相对的是 root（即 src/），不是项目根。所以这里是 ../../。
    //    方案 §5.3 写的 "../app/static/dist" 是错的，会落到 frontend/app/static/dist。
    outDir: "../../app/static/dist",
    emptyOutDir: true,
    assetsDir: "assets",
    cssCodeSplit: false, // CSS 合并为单文件，与当前"一张样式表"的心智一致
    target: "es2020",
    // vite 8 的打包器是 rolldown，内置 oxc 压缩；写 "esbuild" 需要单独安装 esbuild 包。
    minify: true,
    sourcemap: false,
    chunkSizeWarningLimit: 200,
  },
});

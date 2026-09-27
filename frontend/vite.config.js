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
    // ⚠️ 多入口下必须为 true。cssCodeSplit:false 会把两个页面的样式合并成
    //    同一张表 —— 用户页与后台看板的选择器大量同名（.card/.btn/.metric），
    //    合并后互相污染，先后顺序还不受控。单入口时它才等价于"一张样式表"。
    cssCodeSplit: true,
    target: "es2020",
    // vite 8 的打包器是 rolldown，内置 oxc 压缩；写 "esbuild" 需要单独安装 esbuild 包。
    minify: true,
    sourcemap: false,
    chunkSizeWarningLimit: 200,
    rollupOptions: {
      // 路径相对 root（即 src/）
      input: {
        index: "index.html",   // 用户页（P1 起走 2.0 灰度通道）
        admin: "admin.html",   // 后台看板（P4 加入）
      },
    },
  },
});

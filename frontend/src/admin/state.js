/**
 * 后台看板的模块级状态（2.0 P4）。
 *
 * 原先是散在 admin.html <script> 顶部的 8 个声明（原 467–475 行）。
 * ES module 导出的 `let` 对导入方是只读绑定、不能跨模块赋值，故把
 * **5 个真正会被跨模块写的 let** 收敛进 `state` 对象；
 * 另外 3 个是 const 引用（对象 / DOM 节点），**引用不变、属性可变**，
 * 直接具名导出即可，因此调用处代码一字未改。
 *
 * ⚠️ 这里刻意不叫 `expanded` 的东西进 state —— 字符串 "list-expanded"
 *    与 CSS 类 .list-expanded 会被盲改的 `\bexpanded\b` 正则误伤。
 */
export const expanded = { devices: false, errors: false, recent: false };

export const overlay = document.getElementById("loginOverlay");
export const content = document.getElementById("content");

export const state = {
  adminToken: "",      // 管理员会话令牌（内存；CSRF 走 cookie）
  deviceCache: [],     // /api/admin/devices 的 100 条（筛选/排序本地重渲染用）
  recentCache: [],     // /api/admin/recent 的 50 条
  lastErrors: [],      // /api/admin/errors 的聚合结果（renderErrors 需两个入参）
  lastErrorKinds: [],
};

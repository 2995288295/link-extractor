/**
 * 全局可变状态（2.0 P1 唯一需要改名的地方）。
 *
 * 原先是 9 个散落在 index.html 里的模块级 let（原 425/426/506/507/639-642/1147 行）。
 * ES module 导出的 let 对导入方是只读绑定、不能跨模块赋值，故按 2.0 方案 §5.6
 * 收敛为单一对象，引用处统一改为 state.xxx。
 *
 * 与原实现的一处**良性差异**：原先 `autoExtractTimer` 等 4 个变量声明（639-642 行）
 * 位于使用它们的 clearInput（570 行）之后，靠"定义早于调用"侥幸成立；
 * 收敛后所有字段在模块初始化时一次到位，反而更稳。
 */
export const state = {
  /* 设备签名：后端签发，持久化在 localStorage */
  deviceId: localStorage.getItem("device_id") || "",
  deviceSig: localStorage.getItem("device_sig") || "",

  /* 提取结果与并发闸 */
  latestResults: [], // 最近一次提取的全部结果（供「重试失败项」用）
  extractingNow: false, // 提取中防重复
  lastAutoValue: "", // 上次自动提取的内容，避免重复提取
  autoExtractTimer: null, // 「停止输入 1 秒后自动提取」的定时器

  /* 封面预览弹层 */
  coverPreviewTrigger: null, // 打开弹层前的聚焦元素，关闭时归还焦点
  coverPreviewBodyOverflow: "", // 弹层打开前的 body overflow，关闭时还原

  /* 历史清空的二次确认 */
  clearArmTimer: null,
};

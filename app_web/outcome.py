"""把一次提取归到粗粒度 outcome_class（P5 拆包自 app.py）。

首选 extractor 给的 error_kind；拿不到才回退文案匹配。
**新增抛错点请改 lib/extractor.py 里带 kind 的异常类，不要往这里加关键词。**
"""

from __future__ import annotations

from lib.extractor import ERROR_KIND_TO_OUTCOME



def _classify_outcome(success: bool, error: str, error_kind: str = "") -> str:
    """把一次提取归到粗粒度 outcome_class。

    首选 extractor 给的 **error_kind**（谁抛错谁归因，见 lib/extractor.py 的错误归因一节）；
    只有拿不到 kind 时才回退到文案匹配。之所以要这样：过去全靠对报错文案做关键词 LIKE，
    文案改一个字分类就崩，而且根因不同的失败会共用同一句文案（真风控 / 平台 404 /
    笔记已删除 全都写成「小红书平台暂时限制访问」），成功率与告警口径一起失真。
    """
    if success:
        return "success"
    coarse = ERROR_KIND_TO_OUTCOME.get((error_kind or "").strip())
    if coarse:
        return coarse
    return _classify_outcome_by_text(success, error)



def _classify_outcome_by_text(success: bool, error: str) -> str:
    """兜底路径：对报错文案做关键词匹配。

    ⚠️ 只在没有 error_kind（历史记录、旧缓存、外部调用）时使用。
    新增抛错点时请改用 lib.extractor 里带 kind 的异常类，不要再往这里加关键词。
    """
    if success:
        return "success"
    text = (error or "").lower()
    # 只把平台已明确确认不可用的作品归为“内容失效”。
    # 不要因为提示语里带“请确认链接未失效”就误分为用户错误。
    if any(token in text for token in ("作品已删除", "作品不存在", "status_reviewing", "not_publicly_available", "平台返回 http 404")):
        return "expired_content"
    if any(token in text for token in ("不支持", "xsec_token", "作品 id", "作品id", "无法识别", "直播", "商品")):
        return "invalid_input"
    if any(token in text for token in ("风控", "验证", "captcha", "challenge", "html", "json", "请求", "超时", "网络", "完整文案", "未返回可识别")):
        return "upstream_error"
    return "internal_error"

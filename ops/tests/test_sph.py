"""桩测试：视频号（sph）纯函数回归 —— 不联网、不碰代理池。

覆盖 v1.13.0 新增的本地逻辑：短码识别 / 计数解析 / 标题截取 /
post_id 预提取 / 规范链接 / 归因映射 / 白名单。

跑法（服务器）：ops/tests/run_all.sh
"""
import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
_CANDIDATES = (HERE, HERE.parent, HERE.parent.parent / "lib", HERE.parent / "lib", HERE / "lib")
_TARGET = next((b / "extractor.py" for b in _CANDIDATES if (b / "extractor.py").is_file()), None)
if _TARGET is None:
    raise SystemExit("找不到 extractor.py（找过：%s）" % ", ".join(str(b) for b in _CANDIDATES))
spec = importlib.util.spec_from_file_location("ex", _TARGET)
ex = importlib.util.module_from_spec(spec)
sys.modules["ex"] = ex
spec.loader.exec_module(ex)

results = []


def check(name, got, want):
    ok = got == want
    print(("  ✅ " if ok else "  ❌ ") + name)
    if not ok:
        print("     got :", got)
        print("     want:", want)
    results.append(ok)


# ---- 短码识别
check("短链 /sph/ 提取", ex._sph_short_id("https://weixin.qq.com/sph/ALzwMBWJIl"), "ALzwMBWJIl")
check("finder-preview ?id= 提取", ex._sph_short_id("https://channels.weixin.qq.com/finder-preview/pages/sph?id=ALzwMBWJIl"), "ALzwMBWJIl")
check("非视频号链接返回空", ex._sph_short_id("https://v.douyin.com/abc123/"), "")
check("乱粘路径不误判", ex._sph_short_id("https://weixin.qq.com/sph/"), "")

# ---- 计数解析（likeCountFmt 是格式化字符串）
check("计数 '32'", ex._parse_sph_count("32"), 32)
check("计数 '1.2万'", ex._parse_sph_count("1.2万"), 12000)
check("计数 '781'", ex._parse_sph_count(781), 781)
check("计数空串", ex._parse_sph_count(""), 0)
check("计数乱值", ex._parse_sph_count("abc"), 0)

# ---- 标题截取（保留标点，取 # 之前的正文）
check("标题取正文首行", ex._sph_title_from_caption("这是不是你返校的速度？#跑步#体育生"), "这是不是你返校的速度？")
check("标题全标签时兜底", ex._sph_title_from_caption("#跑步#体育生"), "#跑步#体育生"[:30])
check("标题空文案", ex._sph_title_from_caption(""), "")

# ---- post_id 预提取（缓存键）
check("post_id 短链", ex._extract_post_id_from_url("https://weixin.qq.com/sph/ALzwMBWJIl"), "ALzwMBWJIl")
check("post_id 预览页", ex._extract_post_id_from_url("https://channels.weixin.qq.com/finder-preview/pages/sph?id=ANwXivvZ9x"), "ANwXivvZ9x")
check("post_id 抖音不受影响", ex._extract_post_id_from_url("https://www.douyin.com/video/7412345678901234567"), "7412345678901234567")

# ---- 规范链接
check("规范链接 sph", ex._canonicalize_media_url("sph", "", "ALzwMBWJIl"), "https://weixin.qq.com/sph/ALzwMBWJIl")
check("规范链接抖音不受影响", ex._canonicalize_media_url("douyin", "", "7412345678901234567"), "https://www.douyin.com/video/7412345678901234567")

# ---- 归因体系：新 kind 必须落回既有 outcome（红线 §4-B3：outcome 仍 5 值）
check("sph_link_invalid → invalid_input", ex.ERROR_KIND_TO_OUTCOME.get("sph_link_invalid"), "invalid_input")
check("error_kind_of(SphLinkInvalidError)", ex.error_kind_of(ex.SphLinkInvalidError("x")), "sph_link_invalid")
check("平台中文标签含视频号", ex.PLATFORM_LABELS.get("sph"), "视频号")

# ---- SSRF 白名单（不解析 DNS，只查集合成员）
check("白名单含 weixin.qq.com", "weixin.qq.com" in ex.ALLOWED_DOMAINS, True)
check("白名单原抖音域名仍在", "v.douyin.com" in ex.ALLOWED_DOMAINS, True)

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)

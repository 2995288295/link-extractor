# 更新日志 (Changelog)

本项目的所有重要变更均记录于此。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 修复（v1.17.2 · 2026-10-01）· 一轮系统性复查的四个问题

- **BUG 1｜后台徽标一直没有样式**：admin.css 只 import `pages/admin.css`，
  而 `.result-badge` / `.identity-chip` 定义在用户页的 `components.css` 里——
  自 v1.14.0 加成员模块起，「本月第一」徽标和 v1.17.0 的身份徽标在看板上
  全是无样式纯文本。已在 `pages/admin.css` 用后台自己的变量补定义
  （两套设计体系仍保持分开）。
- **BUG 2｜月度有效条数可能静默少算**：历史留存清理按 id 无脑截断到最新
  1000 条，**不区分月份**——单设备本月超过 1000 条时，本月记录会被删掉，
  KPI 数字偏小且无人知晓。改为「只清理本月之前的数据」（= 最新 1000 条
  ∪ 本月全部），月增长有界。
- **BUG 3｜成员模块可能被挤到看板最顶部**：`defaultLayout` 漏登记 "members"
  （v1.14.0 新增模块时漏的）。老用户浏览器里的旧布局、或点「恢复默认」时，
  applyLayout 只 append 清单内模块，members 从未被移动 → 视觉错位到首位。已补登。
- **BUG 4｜页脚版本号骗人**：硬编码 "v1.14.0" 从 v1.15.0 起再没更新过，
  老大因此误以为前端没更新（实际早已是 v1.17.1）。改为**服务端下发**：
  config 启动时 `git describe` 探测一次（失败退 "dev"），`/api/health` 带
  `version` 字段，用户页/后台页脚动态渲染——版本显示与真实部署永远一致。
- **顺手两项**：`_ensure_members_table()` 加进程级缓存（此前每次
  GET /api/profile 都跑一遍建表写事务）；`renderMembers` 空数据时也刷新
  身份分布（避免残留旧 chips）。

### 修复（v1.17.1 · 2026-10-01）· 引导框不弹 / 点「设置姓名」无响应

- **现象 1（老大反馈：无痕窗口也不弹引导框）**：`initProfile()` 判「信息是否填全」
  只读本地 localStorage（`member_name && member_identity`）——v1.16.1 时代只存了
  姓名、没有身份字段，老用户本地永远有 `member_name` 无 `member_identity`，
  而 v1.17.0 的 initProfile 在这种「半齐全」状态下应弹未弹。
  根因是**判定口径用错了数据源**：齐全与否应以服务端 `members` 表为准。
  改为先 `GET /api/profile` 再判；请求失败退回本地判断并放行弹窗，不卡用户。
- **现象 2（点「设置姓名」没反应）**：`openNameModal()` 把 `await apiFetch(...)`
  放在**弹窗显示之前**，弱网/限速（429）时按钮看起来像死了。
  改为**先显示弹窗再补拉数据**（本地值立即渲染，服务端值到了再回填）。
- 顺带：`openNameModal()` 的姓名也改为服务端口径优先（换设备后本地为空时回填）。

### 新增（v1.17.0 · 2026-10-01）· 成员身份选择

- **引导框新增「你的身份」三选一**（彩色按钮，选中=对应色描边+浅底）：
  签约创作者（品牌蓝）/ 创作大使（紫）/ 学校/区域创作者（绿）。与姓名一起
  保存，未选择时保存会提示「请选择身份」（暂不设置仍是完全跳过）。
- **数据**：`members` 表加 `identity` 列（code 存 signed/ambassador/school，
  老表 `PRAGMA` 探测 + `ALTER` 幂等补列）；`/api/profile` GET 返回 identity 与
  可选身份清单，POST 校验后入库；**已填姓名但未选身份的成员会再次看到引导框**
  （标题自动变「完善你的信息」、姓名回填），保证身份最终人人都有。
- **统计页**：姓名旁显示身份徽标（与引导框同色系），「改名」按钮改「修改信息」。
- **后台**：成员署名排行新增「身份」列 + 卡片顶部「身份分布」（三种身份各多少人 /
  未选择多少人），一眼看清队伍构成。
- 身份用 code 存储：以后改中文标签不必刷历史数据。

### 修复（v1.16.0 · 2026-10-01）· 首屏引导 UI 重构：不再双弹窗叠罗汉

- **现象（老大反馈「界面很奇怪」）**：首屏同时弹出两块——顶部月度有效条数提示条
  （`z-index:1200`）与全屏遮罩的姓名弹窗（`z-index:1000`），层级相反、互相遮挡，
  且姓名弹窗复用了封面大图的深色 78% 遮罩 + 毛玻璃皮肤（`.cover-preview`），
  轻量表单套大图弹窗的皮肤，又暗又重，主区域被压在遮罩下显得空。
- **改法**：两块合并为**一次性引导框**（浅色磨砂遮罩 `rgba(247,248,250,.82)`、
  白色卡片 + 品牌蓝浅底横幅），顶部横幅展示「本月有效条数 N 条」，
  下面接姓名表单；说明文案精简为一页；移动端 <480px 横幅竖排不贴边。
- 月度数字随引导框异步拉取（失败静默，不阻塞填名），完整统计仍在统计页卡片。
- 旧的 `.monthly-tip` 顶条样式与自动收起逻辑整块删除（死代码清理），
  `showMonthlySummary` 保留导出但不再被调用。
- 后端零改动。

### 变更（v1.15.0 · 2026-10-01）· 2.0 前端转默认（灰度发布收尾）

- **`/` 与 `/admin` 默认即 2.0（vite 产物）**，不再服务 v1 化石模板；
  `?v2=0` 仍可显式回退 v1（逃生门，留到 v1 模板确认无人用再删）。
- **旧灰度 cookie 失效**：早期体验成员浏览器里可能还留着 `ui=v1` 旧 cookie，
  默认反转后新访问一律走 v2，不会再出现「更新了却还看旧页」。
- 产物缺失时的自动回落逻辑保留（clone 下来直接能跑）。
- 这是 v1.8.0 起的灰度机制收尾：用户页 / 后台此前各自放量，现已全部默认 v2。

### 新增（v1.14.0 · 2026-10-01）· 成员署名 + 统计/历史页增强

- **成员署名（无密码）**：首次访问弹姓名登记框（可跳过、可改名），姓名存
  `members` 表（device_id ↔ name，延迟建表 `CREATE TABLE IF NOT EXISTS`，
  db.py 不动）。后台新增「成员署名排行」模块：按**姓名**聚合本月有效条数
  （与月度口径同一套去重加权常量）、分平台条数、累计成功、设备数、最近活跃；
  未署名设备聚合为「未署名」，产量不被漏看。同一人换设备重填姓名即归并到同一人。
  ⚠️ 定位是**署名**不是鉴权：无密码意味着名字可冒填，用于区分产量够用，
  防冒用需后续加 PIN。
- **统计页常驻「本月有效条数」卡片**：与首页弹窗同口径（去重加权），附姓名
  显示与「改名」入口，不再只弹一次就消失。
- **历史页筛选**：平台 chips（全部/抖音/小红书/视频号）+ 关键词搜索
  （匹配链接/文案/标题），接口一次返回 200 条（原 50），筛选在浏览器侧完成。
- **读剪贴板提取**：提取页新增按钮，一键读剪贴板并开始提取。
  ⚠️ `clipboard.readText` 仅 HTTPS/localhost 可用，当前明文 HTTP 环境会
  降级提示手动粘贴；上线 HTTPS 后自动生效（顺带解锁 PWA 安装）。
- admin「最近动态」平台筛选补上视频号；用户页历史封面徽标补视频号。
- **数据库零迁移**：members 表延迟建表，history 表结构未动。

### 修复（v1.13.1 · 2026-10-01）· 死链误判：活链被错锁 24h

- **根因**：v1.12.0 的死链判定按「24h 内连败 ≥5 次」计数，而唯一计数入口挂在
  **短链限流失败**（`ShortLinkBlockedError`）路径上——晚间高峰把活链错标 24h，
  且被标后用户重贴被缓存直接秒回，看起来像「链接坏了」。
  复盘实证：`xhslink.cn/o/AmuW5fQBVLB` 被判死链 4 次后**次日成功提取 4 次**；
  `xhslink.cn/o/KUZTY9JAt0` 被判死链后**下一分钟**即成功；此前 33 连败的
  `1DFrhWr6gS4` 实为 9/24 旧版桌面头 bug 受害者，9/27 起一直成功。
- **修法（实证制）**：死链登记只认 `NoteDeletedError`（新异常，继承
  `ContentExpiredError`，kind 仍为 `expired_content`）——仅两处平台实证会抛出：
  笔记页 HTTP 404、页面明确提示「已删除或设为私密」（INITIAL_STATE 容器为空）。
  限流（`ShortLinkBlockedError`/`PlatformLimitedError`）、抖音「审核中」等
  一切可恢复失败一律**不**登记。登记时同时记原始链接与作品 ID 双键，
  换分享形式重贴也能命中；24h 后自动过期，重新提交即真实复核。
- **验证**：限流路径不再有任何死链写入（原 807 行调用点删除）；
  死链门改为「原始链接 ∨ 作品 ID」双键；`ops/tests` 全绿。

### 新增（v1.13.1 · 2026-10-01）· 本月有效条数 + 重复提报警示

- **`GET /api/monthly-summary`**（按设备隔离）：本月成功记录按规范链接去重后
  加权计数——抖音 1 条、小红书 0.5 条、视频号 0.5 条（权重与平台映射集中在
  `_MONTHLY_WEIGHTS` / `_MONTHLY_KEYS`）。返回 `counts`（分平台去重条数）、
  `valid_count`（加权有效条数）、`unique_count`。
- **重复提报标记**：`/api/extract` 每条结果新增 `duplicate_this_month`——
  服务端对该设备本月已成功的规范链接集合（含本批次内刚提报的）比对判定。
  前端结果卡片显示「⚠ 本月已提报」徽标，批结束时 toast 汇总提醒。
- **首页小提示弹窗**：打开页面即弹出「本月有效条数 X 条」
  （分平台明细 + 计法说明），10 秒自动收起，可手动关闭，拉取失败静默。
- **历史保留上限 200 → 1000**：月度统计按历史表计算，高产用户一个月可超
  200 条，不放宽会静默少算（`_HISTORY_RETENTION_PER_DEVICE`）。
- 无需新建数据库：去重与加权直接基于现有 `history` 表
  （`idx_history_device(device_id, created_at)` 索引已覆盖查询路径）。

### 新增（v1.13.0 · 2026-10-01）· 视频号（微信 Channels）兼容

- **新平台「视频号」上线**：识别 `weixin.qq.com/sph/{id}` 分享短链（及
  `finder-preview/pages/sph?id=` 形态），直调 finder-preview 公开接口
  `POST /finder-preview/api/feed/get_feed_info`，返回平台徽标「视频号」
  （`platform_raw=sph`）。
- **字段映射**：文案（description 全量，一次拿全无折叠）、作者昵称、点赞数
  （`likeCountFmt` 支持「1.2万」格式）、发布时间（unix → 本地时间）、封面
  （`cover_url`，已加入 `/api/cover` 白名单 `finder.video.qq.com`）、规范链接
  `https://weixin.qq.com/sph/{id}`；无独立 title 字段 → 取文案首行。
- **关键技术点（实测 9/9 真实链接）**：
  - 接口无 cookie/签名/登录态，但 **TLS/HTTP2 指纹层会拒绝非浏览器客户端**
    （requests/curl → `permission verification failed`）；用 `curl_cffi
    impersonate="chrome"` 复刻 Chrome 指纹后全部通过。排查方法：页面内同请求体
    fetch 成功 + 全程零 cookie → 排除 cookie/头/参数，锁定传输层指纹。
  - 成功响应是 **HTTP 201**（非 200），判定需兼容。
  - 依赖 `curl_cffi>=0.16` 已登记 requirements.txt 并装入 venv。
- **归因与边界（红线不破）**：新 `error_kind=sph_link_invalid`（「转发文字」里
  无链接时引导用户「点分享→复制链接」）→ `outcome_class` 仍映射回
  `invalid_input`，粗粒度 5 值不变；`_ERROR_HINT_BY_KIND` 与「仅支持…」用户文案
  同步带上视频号（全仓 grep 过，仅 lib/extractor.py 一处来源）。
- **平台闸门**：`SPH_GATE_CONCURRENCY=2 / SPH_GATE_INTERVAL=0.2s`（环境变量可调）；
  SSRF 白名单加 `weixin.qq.com`；作品级缓存复用现有 `public_work_cache`
  （post_id = sph 短码）。
- **验收**：9/9 sph 链接成功（单条 API 耗时 164ms，二次请求缓存命中 0ms）；
  抖音/小红书真实链接回归正常；无短码输入正确引导、不发平台请求；
  封面经代理出图 110KB；`ops/tests` 102 项全过（新增 test_sph.py 22 项）。
- ⚠️ 观察项：该接口是公开分享预览页所用，上线初期留意频率风控态度；
  封面 CDN 链接带 token 可能有时效，已由 `/api/cover` 24h 缓存兜住。


### 新增（v1.12.1 · 2026-09-28）· 短链解析结果缓存

- **短链 → 笔记页 URL 的 30min 结果缓存**（`_shortlink_resolved_cached`，TTL
  `SHORTLINK_CACHE_TTL_SECONDS=1800`）：重复提交同一短链时命中缓存，省掉 302
  解析那一跳（约 300ms+）。只缓存「解析出了不同地址」的成功结果，失败不写缓存
  （失败重试语义由死链缓存与兜底链路负责）。进程内记忆，重启即清。
- **v1.12.0 上线后首个半日观察**（9/27 16:39 → 9/28 09:50）：
  - 成功率：上线前（当日 0–16 点）76.9% → 上线后 **96.3%**；昨晚 20–23 点高峰
    **97.6%**（9/26 同期 32.8%）。
  - **P95 耗时 5.4s → 1.5s**（9/26 → 9/27），关闭 `nodeEnabled` 砍白等的收益实锤；
    P50 稳定在 ~950ms。
  - ⚠️ 口径说明：补一枪 / 死链缓存 / 换 IP 兜底昨晚**零触发**（风控计数仅 3 次、
    全部未达阈值）——昨晚平台风控本身较松，成功率提升主要来自平台波动与此前
    分享头修复；新机制处于「装好待命」，待下一次风控收紧时才能验证真实价值。
- 验证：双版本编译、隔离进程断言 3 项（命中省解析 / TTL 过期重解 / 失败不缓存）、
  `ops/tests` 80 项全绿。


### 新增（v1.12.0 · 2026-09-27）· 死链识别 + 短链补一枪（成功率专项，P6 缺席后的替代方案）

**数据依据**（近 7 天 1219 条：成功 52.6% / 失败 47.4%，其中 88% 为 platform_limited）：
- 限流失败后 5 分钟内重新提交的 119 条 **100% 成功** → 大部分失败是瞬时软限流，
  新一轮请求（新租约）即可通过，以前靠用户手动重贴完成；
- 232 个 distinct URL 打出 547 次失败，最惨一条 xhslink **33 连败 0 成功** → 死链重试纯属炮灰。

#### 新增
- **短链补一枪**（`lib/extractor.py::_shortlink_extra_shot`）：第一轮换 IP 重试仍被拦时，
  rotate 挂意图、取新租约再试一轮短链，把「用户手动重贴」变成服务端自动完成。
  保险丝 `AJIASU_EXTRA_SHOT_MAX_PER_DAY=8`（约 480s 额度/天，在池预算 900s 内）；
  仅在 `proxy_ready`（达风控阈值）时触发，不破坏「成簇才换」设计。
- **死链识别 + 硬负缓存**（`_note_dead_link` / `_is_dead_link`）：同一短链 24h 内
  跨 30min 连败 ≥5 次 → 后续提交不打平台直接秒回「该笔记已删除或暂不可见」
  （归 `expired_content`，不进限流统计）。判据刻意保守：30 分钟内的突发连败
  不标（夜间高峰失败率 70%+，防误标）；进程内记忆，重启清零。

#### 变更
- `platform_limited` / `short_link_blocked` 的用户提示由「请稍后重试」改为
  「请等 1 分钟后重新提交」（与重试成功率数据对齐，且给兜底留出完成时间）。
- **性能**：关闭代理池 `nodeEnabled`（P6 出口下沉确认不可行，7891 永无节点，
  此前每次兜底 acquire 先白等 ≤8s 探活）——预期 P95 从 9.9s 显著下降。

#### 验证
- py3.9 / py3.11 双版本编译通过；隔离进程断言 9 项全过（死链判定 4 / 保险丝 1 / 解耦 3）；
  `ops/tests` 80 项回归全绿；带断言补丁脚本 `tmp/patch-v1.12.0.py`（4 处锚点各命中 1 次）。


### 变更（v1.11.0 · 2026-09-27）· 第五期：后端拆分（app.py 1969 行 → app_web/ 包，行为零变化）

**范围**：`app.py`（1969 行 / 84830 B 单文件）→ `app_web/` 15 个模块；`app.py` 保留为 **29 行 shim**。
**部署入口与 HTTP 行为一个字节未变**：systemd 的 `ExecStart=… gunicorn … app:app`、
`WorkingDirectory=/opt/link-extractor`、`python app.py`、32 条 `@app.route`（url_map 33 条规则）、
5 个请求钩子 —— 全部逐项一致。

#### 模块划分（148 个顶层节点 100% 有归属）

| 文件 | 行 | 内容 |
|---|---|---|
| `app.py`（shim） | 29 | 只做 `from app_web import app` + `if __name__` 本地入口 |
| `app_web/__init__.py` | 58 | 建 app 对象 → 装配钩子/路由 → 按序跑一次性初始化 |
| `app_web/config.py` | 136 | 路径 / 口令 / 并发 / 日志装配（唯一的配置入口，不依赖兄弟模块） |
| `app_web/db.py` | 166 | `_DbHandle` 线程内复用 + 建表 + 审计写入 |
| `app_web/outcome.py` | 47 | 失败归因（error_kind → outcome_class） |
| `app_web/ratelimit.py` | 152 | 进程内滑动窗口限速器（含两个阈值环境变量） |
| `app_web/security.py` | 90 | 访问口令 / 管理员会话 / device 签名 |
| `app_web/hooks.py` | 140 | 5 个 Flask 钩子（注册顺序 = 原文件顺序） |
| `app_web/assets.py` | 76 | vite 产物服务（预压缩 + immutable 长缓存） |
| `app_web/pool.py` | 174 | 爱加速代理池 CLI 封装（无 HTTP 层，可脱离 Flask 单测） |
| `app_web/cover.py` | 182 | 封面代理（三道 SSRF 防线） |
| `app_web/routes/*.py` | 15+357+421+234+91 | HTTP 路由层：`__init__` / `public` / `admin` / `pool` / `pages` |

合计 **2368 行 / 73 个函数 / 32 处 `@app.route` / 5 个钩子 / 114 条 import**（import 全部由脚本推导，非手写）。

#### 「只搬不改」的证据（脚本化，七项全绿）

| 项 | 结果 |
|---|---|
| 字符区间切片 | 按 AST `(ext_start, end_lineno)` 取源文本，**不用行号拼接** → 杜绝漏行/错位 |
| 顶层节点归属 | **148 / 148** 有归属，未映射即报错；每个模块声明的 parts 与归属表**严格互反** |
| def/class 逐字节 | **73 个** AST 完全一致（另有 1 个行级覆盖，见下） |
| 赋值/表达式原样 | **44 个**原样出现在生成物中 |
| import 图 | 每条相对 import 的目标要么是「目标模块定义的符号」，要么是「已知子模块」 |
| 自由名可解析 | 114 条 import 由 `symtable` 自由名 + provider 表推导，**0 个未解析** |
| 单例唯一 | 10 个进程级单例（`_rate_buckets`/`_extract_queue`/`_db_local`/`app`/`log`…）**各只定义一次** |

#### 全仓只有 3 处**必须**改（其余源码一个字符未动）

1. **`BASE_DIR`**（`config.py`）：原为 `Path(app.py).parent`；搬深一层后加 `.parent.parent`。
   不改则 `DB_PATH` / `_ASSET_DIR` / dist 产物路径**全部错位**。
2. **`Flask(__name__)` → `Flask("app_web", root_path=str(BASE_DIR), …)`**（`__init__.py`）：
   Flask 用 `root_path` 解析相对 `static_folder` 与 `send_from_directory` 的相对目录。
   不显式传，`/static/*` 与 `send_from_directory("app/templates", …)` 会**全部 404**。
3. **日志 logger 名**（`config.py` 的 `setup_logging`）：拆包后 `__name__` 会变 `app_web.config`，
   让 `journalctl | grep` 的既有排查习惯失效 → 显式写回 `"app"`。只改了一行，运行时语义等价。

#### 三个 Python 特有的坑（P1/P4 的 JS 拆包没有的）

1. **`from . import x` 的 `ast.ImportFrom.module` 是 `None`**，只有 `level` 记相对层数。
   用 `module.startswith(".")` 判断相对导入会**全部漏掉** —— 首轮就踩了，
   导致「装配层漏导入」的检查全部误报。已改为按 `level` 解析。
2. **定义路由的模块必须被装配层导入，否则静默丢路由。** 首轮实测**漏了 `cover.py`** →
   `/api/cover` 整个 404；而语法检查、AST 逐节点比对**全都发现不了**。
   现生成器第 [7] 项机械断言「定义了 `@app.*` 的 **7 个**模块必须都被 import 到」。
3. **`symtable` 对被 import 绑定的名字只置 `is_imported()`，`is_assigned()` 仍是 `False`。**
   只用 `is_assigned()` 判「本模块绑定」→ shim 上手写的 `import os` 被误判为未绑定，
   又自动生成了一遍（且生成的是非法的 `from . import app` —— shim 不在包内）。

#### 3 处「模块级重新绑定」**一处都没改**

`_rate_last_persist`（原 L479）· `_ASSET_CACHE`（原 L633 `.clear()`）· `_cover_cache`（mutation）——
归属表**刻意**让它们与各自的定义留在同一模块，所以 `global _rate_last_persist` 与两处 mutation
的字面代码一个字都不用动。这是归属表设计的核心约束，不是巧合。

#### 四层验证（全绿）

- **结构层**：上表七项
- **隔离副本 25 项**（本机 + 服务器各跑一遍，服务器用生产同款 `venv/bin/python3`）：
  33 条路由的 rule/methods/endpoint 一致 · 视图函数名与钩子顺序一致 ·
  `root_path`/`static_folder`/`static_url_path`/`config` 一致 · 7 张表 + 7 个索引 + 22 个 history 列一致 ·
  **31 条 HTTP 用例响应逐项一致**（含未鉴权 401、登录 200+cookie、`/assets` 路径穿越 404、
  gzip/Vary/Cache-Control） · 限速器连打 25 次 **20×200 + 5×429**，且计数落在**模块级**
  `_rate_buckets` 上（跨模块单例未拆坏） · **导入期副作用顺序**：预置限速窗口 → 导入后必须被
  `_load_rate_buckets()` 载回（证明 bootstrap 按序执行）
- **真 gunicorn A/B 20 项**：以生产完全相同的形态（`-w 1 --worker-class gthread --threads 4`）
  跑新旧两个副本，含 8 线程并发与真 socket 响应头，**差异 0 个**
- **补充冒烟**：`python app.py` 走 shim 的 `__main__` 起 dev server 正常；从**非项目根 cwd**
  启动正常（`sys.path` / `lib` 导入不受影响）；7 个子模块单独 import 无循环依赖
- **生产端验收 35 项**（上线后在真服务上打 `127.0.0.1:5003`，脚本 `/tmp/p5-diag/prod-accept-p5.py`，
  口令只在服务器内读 `/etc/link-extractor.env`）：服务降权 `linkext` + cwd 正确 + `runuser` 可读
  `app_web` 全部 15 个 `.py` · 4 条页面路径页脚均为 `v1.11.0` · **21 条路由无一 404**（P5 最大风险项）·
  `/assets` immutable + gzip · 路径穿越 404 · 鉴权边界 401/200 · 9 条管理员只读接口全 200 ·
  **真提取**（NDJSON `start/item/end` 结构 + 复用历史成功链接 231ms/4ms 成功 + `record_history=false`
  确实未落库）· 限速器**额度精确计数** · 历史库 6 张核心表 + 22 列 · 日志 `[INFO] app: >>>` 前缀保真 · 无 Traceback/500

  > 验收脚本自身踩的坑（已修）：`/api/admin/pool/*` 的 10 条路由函数体里**看不到**任何
  > `_admin_require_rate`，它们统一走 `pool._pool_precheck()`（docstring：「代理池接口统一前置限速」）。
  > 只按「路由函数直接函数体」判是否消耗限速桶 → 少算 1 个额度 → 「额度精确」断言**假失败**。
  > 已改用 **AST 调用图跨模块可达性**推导「哪些路由最终会走到 `_admin_require_rate`」（19 条），
  > 并用它跟脚本期望集做**严格相等**比对。教训：凡是「精确推导额度」的验收，都必须机械核对推导前提。

#### 有意偏离方案 §10.1 的两处（理由已回填方案文档）

1. **不引入 `create_app()` 工厂，保留模块级 `app`。** 32 处 `@app.route` 若搬进工厂作用域，
   endpoint 名会变成 `create_app.<locals>.api_xxx`；而全仓 `url_for` / `render_template` /
   `current_app` 命中数均为 **0** —— 工厂的可测性收益在这里是零，纯粹扩大改动面。
2. **不引入 Blueprint，沿用 `@app.route`。** 同上：`url_for` 命中 0，Blueprint 只有改名成本。

模块切分比 §10.1 的草图细一档：`http_util` 按职责一分为 `hooks` + `assets`；新增 `outcome` / `cover`；
`pool` 拆成「CLI 封装」与「HTTP 层（`routes/pool.py`）」两块。

#### 版本号

页脚 `v1.9.1` / `v1.10.0` → **`v1.11.0`**（用户页与后台各 3 处：`app/templates`、`app/static/dist`、
`frontend/src`）。**纯文本改动，不触发前端重建** —— 页脚是入口 HTML 里的静态节点，不在 bundle 中，
故 dist 资源 hash 不变。

#### 已知的、有意保留的差异

- `_load_device_secret()` 里两处 `logging.getLogger(__name__).warning(...)` **未改**：拆包后会显示
  `app_web.config`。属冷路径（服务器上 `DEVICE_SECRET` 由 systemd 注入，永不触发）。
  主日志对象 `log` 的名字已保住，`grep 'app:'` 的排查习惯不受影响。
- `app.logger`（仅 `api_cover` 1 处使用）的 logger 名由 `app` 变 `app_web`。

#### 回滚

```bash
ssh zine-server 'cd /opt/link-extractor && git checkout v1.10.0 -- app.py app_web app/templates app/static/dist frontend && systemctl restart link-extractor'
```

### 变更（v1.10.0 · 2026-09-27）· 第四期：后台看板 2.0（多入口构建 + 模块化，视觉零变化）

**范围**：`app/templates/admin.html`（1198 行单文件）→ `frontend/src/admin.html` + `admin.js`
+ `frontend/src/admin/` 7 个模块 + `styles/pages/admin.css`；`app.py` 的 `/admin` 加 v2 灰度分流；
`frontend/vite.config.js` 改多入口。**用户页一行未改。**

#### 「只搬不改」的证据（与 P1 同一套方法论）

| 层 | 结果 |
|---|---|
| 函数体 | **57 个**逐字节一致（归一化仅去 `state.` 前缀与空白） |
| 顶层裸语句块 | **17 块**一致（onclick 绑定 / addEventListener / init IIFE / `initLayoutEditor();`） |
| CSS | `pages/admin.css` **20255 B 与原文逐字节一致** |
| 状态声明 | 原文 16 个顶层变量全部有归属（5 个 `let` 收敛进 `state`、3 个 `const` 具名导出、8 个原地保留） |
| 顶层节点 | 90 个，**100% 有归属**（脚本未映射即报错退出） |

#### 模块划分

| 文件 | 内容 |
|---|---|
| `admin/state.js`（手写） | `state` 对象（5 个跨模块写的 `let`）+ `expanded`/`overlay`/`content` 具名导出 |
| `admin/core.js` | 会话 / HTTP / 小工具（8 个函数） |
| `admin/render-metrics.js` | 手写 SVG 趋势图 + 指标渲染（6 个） |
| `admin/render-tables.js` | 设备/错误/动态三张表 + 7 处本地重渲染绑定（5 个函数） |
| `admin/load.js` | `loadCore` / `loadDetails` / `loadAll` / `refreshCore` / `currentView`（5 个） |
| `admin/pool.js` | 代理池运维（21 个 + `poolState` + 事件委托） |
| `admin/layout-editor.js` | 布局编辑器（10 个 + 4 个常量，含顶层 `initLayoutEditor();` 调用） |
| `admin.js` | 入口：样式导入 + range/tab 绑定 + init IIFE + 轮询 + 过渡层 |

模块依赖含一个**函数级循环引用**：`core → load → pool → core`（`login()` 调 `loadAll()`，
`loadAll()` 调 `loadPool()`，`loadPool()` 调 `logout()/showToast()`）。三个模块的顶层语句
都不在模块体求值期使用循环另一侧的绑定，故 ESM 安全 —— 已由行为级验证实测覆盖。

#### 三个 admin 特有的坑（P1 没有的）

1. **`expanded` 会被盲改正则误伤**：字符串 `"list-expanded"` 与 CSS 类 `.list-expanded` 都含
   词边界完整的 `expanded`。→ 本期**只对 5 个 `let` 做改名**，且改名时**跳过字符串字面量区间**；
   `expanded` 是 const 对象引用，直接具名导出，**代码一字不改**。
2. **12 处内联 onclick 依赖全局函数名**（10 个函数）—— ES module 不挂载就 `ReferenceError`。
   → `admin.js` 加 `Object.assign(window, {...})` 过渡层。**2.0 方案 §9.2 的拆分清单漏了这一项**
   （与 P1 漏掉 `unescapeHtml` 同源），已回填到方案文档。
3. **`cssCodeSplit` 必须为 true**：多入口下 `false` 会把两页样式合并成同一张表，而用户页与后台
   的选择器大量同名（`.card`/`.btn`/`.metric`）→ 互相污染且先后顺序不受控。

#### 灰度通道

`/admin?v2=1` / `?v2=0` + cookie **`adminui`**（90 天），产物缺失自动回退老版。
**与用户页的 `ui` cookie 刻意分开** —— 用户页面向外部创作者，后台只有我们自己用，放量节奏不同。
产物：`dist/admin.html` 4.01 KB(gzip) · `admin-D6lOd26g.css` 4.23 KB · `admin-3iqu8Te0.js` 11.15 KB。

#### 验证（四层，全脚本化）

- **结构层**：函数 57/57 · 裸块 17/17 · CSS 20255 B 逐字节 · 变量归属 16/16
- **隔离副本 31 项**：默认 v1 / `?v2=1` / `?v2=0` / cookie 粘性 / **产物缺失回退** /
  两个灰度 cookie 互不干扰 / `/assets` gzip + immutable / 路径穿越 404 / 既有接口不受影响
- **行为级 42 项 ×2**（真 Chrome + mock server）：v1 与 v2 **各 42/42**；含
  「搜索/排序/展开 = **0 新请求**」（v1.7.8 性能成果保持）与布局编辑器全流程
  - ⚠️ 按 §9.3：**有副作用项（兜底开关/轻检/深检/释放/回收/账号增删改）只验「按钮存在 + 绑定未断」，
    从未实际点击** —— 那是唯一能真改生产状态的界面
- **视觉级**：DOM **75 个 id 完全一致** · **34 项 computedStyle ×2 断点完全一致** ·
  盒模型 7 项一致 · 双断点无溢出无裁切无 JS 报错
- **生产端 21 项**（经隧道真浏览器、**不登录**）：v1/v2 各绿；真实提取 2/2 成功；
  告警逻辑单测 11/11、alert timer dry-run 正常

#### 顺带记录的两件事

- `app/templates/admin.html` 第 306 行有个**既存 HTML 瑕疵**：
  `<section ...><div ...></div> aria-labelledby="alertsTitle">` —— 多余的 `aria-labelledby="alertsTitle">`
  成了页面上的可见文本。**v1 本来就有**，本期「只搬不改」故照原样保留，留给专门的一批清理。
- 用户页 JS chunk 由 `index-frI5QCS.js`（20356 B）变为 `index-wlCVAi2j.js`（19731 B）：
  多入口构建把共享的 `modulepreload-polyfill` 抽成独立 chunk。**用户页源码一行未改**
  （`frontend/src` 下 22 个用户页文件 md5 与 v1.9.1 相同），行为级 17/17 不变。
  同理用户页 CSS 由 `style-DI41pM7j.css` 更名 `index-DI41pM7j.css`。

#### 回滚

```bash
ssh zine-server 'cd /opt/link-extractor && git checkout v1.9.1 -- app.py app/templates app/static/dist frontend && systemctl restart link-extractor'
```
轻量回滚：访问 `/admin?v2=0` 切回老版看板（无需重启）。

### 变更（v1.9.1 · 2026-09-27）· 品牌色由黄改回蓝（P2 视觉的回退）

依据老大反馈「颜色用之前的蓝色，不要黄色」。**只改颜色** —— P2 的代码分层、性能收益、
语义色两档、对比度修复、去背景渐变等**全部保留**。

| 令牌 | v1.9.0（黄） | v1.9.1（蓝） | 用途 |
|---|---|---|---|
| `--brand` | `#FFEE00` | `#1769E0` | 主按钮 / 复制按钮填充、tab 下划线、品牌标识 |
| `--brand-ink` | `#14181F` | `#FFFFFF` | 品牌深底上的文字（**5.08:1**） |
| `--brand-hover` | `#F5E400` | `#1256BA` | 悬停 |
| `--brand-soft` | `#FFFBD6` | `#E8F0FE` | 浅底态按钮底 |
| `--brand-soft-hover` | `#FFF7B8` | `#DCE9FC` | 浅底悬停 |
| `--brand-soft-ink` | — | `#124C9D` | **新增**：浅底上的文字（**7.20:1**） |

**一个必须说的结构改动**：P2 的 `--brand-ink` 是黑，**同时**压在深黄底与浅黄底上 —— 黑字压深浅通吃，
一个令牌够用。改蓝后白字压不住浅蓝底（≈1.2:1），所以 `.btn-copy[data-kind="caption"]` 的文字
改用新增的 `--brand-soft-ink`。`components.css` 只动了这一处声明值，**选择器本身无增删**。

**没动**：`--accent:#0F7A85`（趋势线数据色，青 —— 它不是黄色，且与品牌色职责分离）·
`theme-color` meta（`#F7F8FA`）· `manifest.json` 的 `theme_color`（v1/v2 共用）·
`app.py`（**一字未改**，`/` 走 `send_from_directory` 不过 Jinja → **全程零重启**）·
JS（`index-frI5QCS.js` md5 `a21e23c2…` 与 v1.8.0 **逐字节相同**）。

**验证**：样式层 6 组（选择器 135 个**零增零删** / 42 令牌全可解析 / 品牌黄四项彻底退场 /
**22 项对比度全过** / JS chunk md5 未变）；视觉级 33 项 computedStyle 对照 + 双断点无溢出无裁切 +
tab 无 1px 跳变；行为级真 Chrome v1/v2 各 **17/17**；生产端真浏览器 v1/v2 各 **21 项** +
真实提取 **3/3**；新 CSS gzip 3235 B / immutable；旧 CSS 404；路径穿越 404。

**相对 v1 仍保留的 2.0 结构差异**：纯色底（无渐变）· 容器 960px · 卡片圆角 14px ·
tab 下划线 3px · 链接深色 + 下划线 · 语义色两档 · **品牌标识为实心胶囊块**（v1 是蓝色文字）。

### 新增（v1.9.0 · 2026-09-27）· 2.0 第二期：用户页视觉换新

来自 `docs/2026-09-27-2.0改造方案-方案A.md` §4 + §7。**范围只有 `frontend/src/styles/*`**：
把 335 行单文件样式拆成 **4 层**、落地浅色 2.0 色板，界面正式换皮。

**一个可机器验证的硬约束：JS 一个字节都不许动。**
`styles/index.css` 改成只做聚合的**桶文件**（4 条 `@import`），于是 `index.js` 里
`import "./styles/index.css"` **一字未改** —— 构建后 JS chunk 仍是 `index-fFrI5QCS.js`、
md5 `a21e23c2…`，与 v1.8.0 **逐字节相同**。这条哈希就是「本期零交互逻辑改动」的证明，
比任何人眼 review 都硬，也意味着最大风险（改样式改坏交互）在结构上被排除了。

**样式分层**：`tokens.css`(令牌) / `base.css`(重置·焦点环·无障碍) /
`components.css`(组件) / `pages/index.css`(页骨架·响应式)。

**换新内容**
- **品牌色由蓝转黄**：`--primary:#1769e0` 拆成两个角色 —— 动作色 `--brand:#FFEE00`、
  强调文字 `--ink:#14181F`。主按钮/复制按钮 = 黄底黑字（对比度 **14.81:1**）；
  tab 选中 = 深字 + **3px 黄下划线**；页头 `.eyebrow` = 黄底黑字实心胶囊。
  **黄色一律只做填充块**：`` #FFEE00 `` 当文字在浅底上只有 ≈**1.05:1**，等于看不见。
- 去掉 `linear-gradient(180deg,#edf3fb 0,var(--bg) 280px)` —— 浅色下渐变让页面上半部分发灰。
- `.container` 统一 **960px**（原 860 / 大屏 960 双档），顺手删掉 `@media (min-width:901px)`
  —— 在 ≤992px 视口下它本来就是恒真分支。
- 链接字段去蓝：`--ink` + `text-decoration: underline`（`text-underline-offset: 3px`）。
- 语义色统一：徽标 / 统计数字 / 危险按钮改用同一套 `--ok` / `--fail` / `--warn`。
- 间距统一到 4 的倍数（10px → 12px、6px → 8px）。**只统一间距，不动按钮 padding**
  —— 内边距属元件比例，改了会连带改按钮尺寸。
- `.card` 圆角 12 → 14px、阴影收敛为 `--shadow-2`(blur 20px)；
  `.result-item` 反而**降到** `--shadow-1` —— 满屏结果卡片时 20px 大模糊阴影会糊成一片。
- `theme-color` 由 `#3a7afe` 改 `#F7F8FA`（**只改 v2 壳**；`app/static/manifest.json` 里的
  `theme_color` 有意不动 —— 那个文件 v1/v2 共用，等 v1 退役再统一）。
- 页脚版本号 v1.8.0 → v1.9.0（v1 模板照旧只改这一处）。
- 通用封面占位从品牌蓝渐变改为中性石墨（品牌蓝已退场；小红书的平台粉渐变保留）。

**顺手修掉的两个既有缺陷**
1. **徽标/白字压绿的对比度**：方案 §4.1 给的 `--ok:#12A06A` 直接当文字压在 `--ok-soft` 上
   只有 **3.12:1**（12px 不达 WCAG AA）；白字压 `--ok` 只有 3.49:1，比旧版 `--success`
   的 5.37:1 **倒退**。→ 语义色拆「填充 / 文字」两档，新增 `--ok-ink`(5.88:1)、
   `--fail-ink`(5.15:1)、`--warn-ink`(6.02:1)、`--ok-strong`(5.36:1, 白字压绿底用)。
2. **`.loading-spinner` 在浅色按钮上不可见**：它原本是 `rgba(255,255,255,.4)`/`#fff` 的
   白色 spinner，压在 `.btn-ghost`（浅灰底）上一直看不见，只有蓝底主按钮上能看见。
   → 改 `currentColor`，任何按钮上都跟文字色走。

**兼容别名（有意的技术债，P3 清）**
JS 模板里有 10 处内联 `var()`，涉及 3 个变量：`--text2` ×4、`--error` ×4、
`--primary` ×2（趋势图 SVG 的 `fill`/`stroke` —— 内联属性无法用 class 覆盖）。
本期边界是「只改样式层」，所以在 `tokens.css` 末尾加了**显式标注的兼容别名块**兜住它们，
其中 `--primary` 指向**数据色** `--accent:#0F7A85`（不是品牌黄 —— 黄折线在浅底上几乎看不见）。
P3 重写结果卡片渲染时连同这些内联 style 一起删除；
验证脚本会在 JS 里出现第 4 个变量名时**立刻报警**，防止这条债被悄悄遗忘。

**验证（三层 + 生产，全部脚本化）**
- **样式层 6 组**（`tmp/diag-20260928/verify-styles.py`）：
  ① 选择器集合 135 个 —— 只按方案删掉 1 条恒真 media、补了 1 个 hover，其余**零增零删**；
  ② `@keyframes` 名一致；③ 41 个 `var()` 引用全部有定义；
  ④ 色值白名单：旧蓝色系/旧语义色/旧中性灰**全部退场**，36 种十六进制色值无散装硬编码；
  ⑤ **22 项对比度断言**（含方案原稿漏检的两处）；⑥ JS chunk md5 未变。
- **行为级**：真 Chrome + mock server，v1 / v2 各 **17/17**（含流式提取、tab 切换、
  封面弹层 + Esc、复制反馈、全程无 JS 报错）。
- **视觉级**（`verify-visual.js`）：34 项 computedStyle 对照 +
  **双断点（900 / 390px）溢出与文字裁切检查**；tab 切换高度序列 `[40,40,40]`
  **无 1px 跳变** —— 下划线 2px→3px 的经典坑用「描边宽度固定、只换颜色」规避。
- **生产端**（`prod-browser-check.js`，经 SSH 隧道真浏览器）：v1 / v2 各 **21 项**；
  真实提取 **3/3 成功**；新 CSS gzip **3219 B**（解压 11210 B）、`immutable` + `Vary` 正确、
  旧 CSS 404、路径穿越 404、**无双重 gzip**。

**部署**：只动了 `frontend/src/`、`app/static/dist/`、`app/templates/index.html`（页脚版本号），
**未改 `app.py`**。`/` 走 `send_from_directory` 原始直发、**不经 Jinja**，
所以模板改动**不需要重启服务**。
回滚：`tar xzf /tmp/dist-backup-<ts>.tar.gz -C app/static/` +
`cp app/templates/index.html.bak-<ts> app/templates/index.html`；轻量回滚 `/?v2=0`。


### 新增（v1.8.0 · 2026-09-27）· 2.0 第一期：用户页施工脚手架

来自 `docs/2026-09-27-2.0改造方案-方案A.md` §5。**唯一目标：拆模块 + 跑通构建链 + 建灰度通道，
界面看起来必须一模一样。执行策略是「只搬不改」——函数体原样搬运。**

**为什么先做这个而不是直接换皮**：先换地基再装修。出问题时能立刻分清是「搬家漏东西」
还是「装修写错样式」；两者混在一批，排查就得同时怀疑两处。

- 新增 `frontend/`（vite **8.3.1**，锁精确版本而非 `^8.3.1`）+ `app/static/dist/`。
  1181 行单文件拆成 **12 个 ES 模块**：`core/state·api·dom`、`ui/toast·tabs·lightbox`、
  `features/input·extract·results·copy·stats·history`，加 `index.js` 入口。构建约 220 ms。
  **产物入库**（服务器上没有 node，构建只能在本地做）。
- 新增 `/assets/<path>` 路由：自己读盘 + 预压缩 + `immutable` 长缓存。
  **不用 `send_from_directory`** —— 它返回 `direct_passthrough` 流式响应，会被
  `_optimize_response()` 的 gzip 分支跳过，JS/CSS 将以未压缩状态传输。
- 新增灰度通道：`/?v2=1` 走 2.0 产物、`/?v2=0` 回 v1，两者都会写入 cookie `ui`（90 天），
  之后直接访问 `/` 即走对应版本；**产物缺失时自动回退 v1**，没跑过 build 也能正常访问。
- `app.py` +81 行（4 处，带断言补丁，每处断言命中数恰好 1）：`import mimetypes`、
  新增资产区与路由、首页按 `?v2`/cookie 分流、`_optimize_response` 增加
  「已有 `Content-Encoding` 就不再压」守卫（否则已预压缩的资源会被压第二次）。

**「只搬不改」的硬证据**（不是靠手测，是靠逐字节比对）：33 个函数 + 4 个顶层裸语句块
（SW 注册 / 输入框滚动 IIFE / Esc 监听 / window load），归一化（**仅**去掉 `state.` 前缀与空白）
后与原文**零差异**；CSS 11639 B 内容一致；9 个模块级变量与 `state` 对象字段一一对应。

**唯一改名**：9 个模块级 `let` 收敛为 `state` 对象 —— ES module 导出的 `let` 对导入方是只读绑定，
无法跨模块赋值。原实现里 `autoExtractTimer` 等 4 个变量声明（原 639-642 行）位于使用它们的
`clearInput`（原 570 行）之后，靠「定义早于调用」侥幸成立；收敛后所有字段在模块初始化时一次到位。

**验证**：隔离副本 39 项（分流 / cookie / 回退 / gzip / 缓存头 / 路径穿越 / 双重 gzip / 既有接口）
→ 行为级 17 项 ×2（真实 Chrome + mock，v1 与 v2 各跑一遍，均 17/17：全局挂载、流式提取、
tab 切换、封面弹层 + Esc、复制反馈、全程无 JS 报错）→ 80 项告警口径回归全绿 →
生产实测（v1/v2 各 17 项、DOM id 集合完全一致、真实提取 3/3、资源 gzip 后 6877 B / 3014 B、穿越 404）。

**有意不做**：视觉上任何变化，**包括 `theme-color`** —— 属第二期。本期只换地基。

**顺带发现两处既有事实（本批未处理）**：
1. `_optimize_response` 里 `setdefault("Cache-Control", "public, max-age=3600")` 对 `/static/*`
   **从未生效** —— Flask 的 `send_file` 默认已带 `no-cache`，`setdefault` 不覆盖。
   方案 §5.9 的缓存表据此有误。
2. HTML 响应走 `direct_passthrough`，**本就不参与 gzip**（v1 同样如此），并非 2.0 引入。

**踩坑**：macOS 的 `tar` 会把扩展属性打包成 `._*`（AppleDouble）条目，解压到 Linux 会变成
垃圾文件、污染 `/assets/`。打包必须 `COPYFILE_DISABLE=1`；服务器端已核对无残留。

### 性能（v1.7.8 · 2026-09-27）· 独立批：后台看板请求量降到 1/3 以下

来自 `docs/2026-09-27-2.0改造方案-方案A.md` §6。**只改 `app/templates/admin.html`，未动后端**，
因此**不需要重启服务**（管理页由 `send_from_directory` 直发，替换文件即生效 —— 本次已再次实测确认）。

**先说一个自我纠正**：动手前逐条实测了看板各接口的 SQL，结论是「SQL 根本不是瓶颈」——
全库 4415 行、已有 4 个索引，30 天全量刷新 37 ms；被列为 P1 的「趋势图 N+1」实测 30d 才 22 ms，
改一条 `GROUP BY` 能省 19.6 ms。**为省 20 ms 去改 SQL，收益远小于改动风险，故本批不做。**
真正的开销全在「前端调用方式」。

- **A1 每 30 秒无条件全量刷新**：原 `setInterval` 只在「已登录」时判断，**切到后台标签页照打**，
  每次 `loadAll()` = 6 个接口 + `loadPool()`（起一个 Python 子进程）= 7 请求 + 1 子进程，
  每分钟 14 次，占管理限速（30 次/分钟）的 **47%**。现改为：`document.hidden` 后台不刷、
  未登录不刷、`<html>` 不可见不刷，并监听 `visibilitychange` 在回到前台时补刷一次。
- **A2 轮询只刷「会变的部分」**：`loadAll()` 拆成 `loadCore()`（overview / performance / health，
  随时在变）+ `loadDetails()`（devices / errors / recent，变化慢）。轮询只跑前者（3 请求）。
- **A3 把 `loadPool()` 从 `loadAll()` 解绑**：只在「切到代理池 tab」「手动刷新」「池内有写操作」时拉。
  原来每 30 秒 spawn 一个 Python 进程，且 `pool.py` 内部 `with pool_lock()` **最坏占住一个
  gunicorn 线程 30 秒**。
- **A4 六个交互点改为本地重渲染（零请求）**：三个「展开全部」按钮原先**只是把一个布尔值翻面
  也要打 7 个请求 + 1 个子进程**；搜索框每敲一次、切一次排序/平台/状态同样全量刷新。
  现改为复用缓存本地重渲染 —— `renderDevices` / `renderRecent` 内部本就自己读搜索框与下拉框，
  所以**行为与改前完全一致，只是不发请求**（函数体一字未改）。新增 `deviceCache` /
  `recentCache` / `lastErrors` / `lastErrorKinds` 四个缓存变量。
- **附**：管理页页脚版本号 v1.5.0 → v1.7.8。

**对照实测**（jsdom + 假计时器加载真实 HTML，新旧两版跑同一套 23 项断言）：

| 场景 | v1.7.7 | v1.7.8 |
|---|---|---|
| 初始加载 | 8 请求（含 pool 子进程） | **7 请求（无子进程）** |
| 搜索框输入一次 | 7 请求 | **0** |
| 排序 + 平台 + 状态（3 次） | 21 请求 | **0** |
| 三个「展开」按钮 | 21 请求 | **0** |
| 前台轮询 1 次 | 7 请求 | **3 请求** |
| 切后台标签页 90 秒 | 21 请求 | **0** |
| 1 小时常驻（非代理池 tab） | 840 请求 → 14 次/分钟 | **360 请求 → 6 次/分钟** |
| 断言通过数 | 5 / 19 | **23 / 23** |

> 这不只是「更快」：一次「搜索 + 排序 + 展开」原来是 21 个请求，而管理限速是每 IP 30 次/分钟 ——
> **是个现在就会撞到 429 的可用性缺陷**，本批一并修掉。

- **未做（有意）**：§6.9 的 A5（趋势 N+1 改一条 GROUP BY，省 19.6 ms）、A6（日志尾部读取）、
  A7（P95 走 SQL）。收益都在毫秒级，不值得引入改动风险；更要紧的是**把它们和「零重启的纯前端改动」
  分开**，出问题时能立刻判定归属。留作后续独立小批。

### 修复 + 优化（v1.7.7 · 2026-09-27）· 本周批 W1 + 顺手项 L4/L6/L7

来自 `docs/2026-09-27-修复清单.md` 的 W1（需要跑并发回归的那批），外加三个零风险清理项。
背景：D1 决策「5003 继续公开给外部创作者自助使用」之后，**限速器成了唯一的守门人**，
而它本身在并发下是失效的。

- **W1-a 限速器并发下失效（实测放行 40/20）**：原实现是「查 SQLite → 改 list → 写回」，
  三步之间既无事务也无锁。单进程 4 线程（`gunicorn -w 1 --threads 4`）会各自读到同一份旧数据
  再互相覆盖 —— 同机对照实测：**并发 40 次、限额 20/分钟 → 旧版放行 40 次**。
  现改为**进程内内存滑动窗口 + `threading.Lock`**（单进程 4 线程天然共享，加锁即正确），
  每 15 秒把窗口快照落回 `rate_limits` 表、启动时载回（`_load_rate_buckets`），
  保留原表结构且**服务重启不重置计数**（实测重启日志出现「限速窗口已载入：1 个活跃桶」）。
- **W1-b 每请求新开一个 SQLite 连接**：原来 `_get_db()` 每次 `connect` + `PRAGMA journal_mode=WAL`，
  一个 20 条批次约开 22 个连接。现改为**线程内复用长连接**（`threading.local`），
  并加 `PRAGMA synchronous=NORMAL`（WAL 下的官方推荐搭配）。
  ⚠️ 关键陷阱：文件里几乎所有使用点都写成 `finally: conn.close()`，
  因此 `_get_db()` 返回的是 `_DbHandle` 薄代理 —— **`close()` 只回滚未提交事务、不真关闭连接**，
  否则第一次 close 就把长连接关掉。复用只在本进程内成立：**若将来改 `-w >1` 或加 `--preload`，
  必须改回「每请求新建 + 真关闭」**（该注释已写在 `_get_db()` 上）。
- **对照数据（同机、同脚本、新旧各一份隔离副本）**：串行限速 6.037 ms/次 → **0.008 ms/次**；
  40 并发总耗时 1242 ms → **6 ms**；并发放行数 40/40（超额 100%）→ **20/40（正确）**。
- **L4 gzip 未压缩分支漏 `Vary`**：首屏 HTML 走 `send_from_directory`（流式，本就跳过压缩），
  于是「未压缩的 200」不带 `Vary`，缓存可能存下未压缩版本。现按响应类型统一
  `setdefault("Vary", "Accept-Encoding")`。
- **L6 `_check_admin_token` 有不可达 `return`**；**L7 `app.py` 未使用导入 `base64` / `wraps`** —— 一并清理。

**发布与验证**：线上基准 v1.7.6 → 备份 `app.py.bak-20260927-132223` → 带断言补丁脚本（7 处全命中）
→ 双版本 `ast.parse`（3.9 / 3.13；不用 `py_compile`，避免 root 属主 `.pyc`）→ 隔离副本 15 项验证全过
（并发正确性 / `close()` 语义 / 跨进程载入 / HTTP 429 接线 / `Vary`）→ `ops/tests` 80 项全绿
→ 重启 → md5 双向一致 → 生产实测 3/3 小红书链接成功、`/api/history` 第 21 次起 429。

### 修复（v1.7.6 · 2026-09-27）· 立刻批 5 项：报错透传 / 封面代理 / 面板建议 / 权限 / 安全头

由当天全面自查（`docs/2026-09-27-修复清单.md`）整理出的「立刻修」批次，全部低风险。

- **F1 用户可见报错泄漏（确认 Bug）**：`_extract_xhs_initial_state` 里的 `json.loads(state_blob)`
  没有 try/except（同文件 `_extract_xhs_lightweight` 是包了的）。小红书返回裸
  `window.__INITIAL_STATE__=undefined` 时抛 `JSONDecodeError`，它继承 `ValueError`，
  于是走 `extract_link` 的 `error=str(e)` 通路，把
  `Expecting value: line 1 column 1 (char 0)` 原样显示在用户的「错误信息」栏里。
  两处修复：① 该行包 try/except → `PageStructureError("暂时无法获取该笔记内容")`；
  ② 把 `extract_link` 的透传白名单由 `(ValueError, RuntimeError)` 收紧为 **`ExtractError`** ——
  只信我们自己定义的业务异常能带用户文案，其它一律脱敏为「提取失败」。
  （实测：非 ExtractError 的 ValueError 已脱敏；自家 `ContentExpiredError` 仍保留友好文案。）

- **F2 `/api/cover` 曾是「同源 HTML 代理」**：原代码把上游 Content-Type **原样透传**，
  而域名白名单里有 `xiaohongshu.com` 整站 → 实测 `?url=https://www.xiaohongshu.com/`
  返回 `200 text/html`、36KB、`max-age=86400`，本站域名可承载任意白名单域名下的 HTML
  （可挂钓鱼页且被浏览器缓存 24h），并构成同源脚本执行面。
  修复：只放行白名单位图类型（**不用 `image/*` 通配** —— `image/svg+xml` 是可执行脚本的），
  其余 502；响应加 `X-Content-Type-Options: nosniff`。
  同时修掉内存风险：`stream=True` + `_read_limited()` 按 5MB 上限流式读取
  （原先 `r.content` 一次性全读、无上限），封面缓存改**条数 + 总字节（64MB）双上限**淘汰。
  实测：非图片 → 502；真封面图 → 200 `image/jpeg` 282KB + nosniff，未误伤。

- **F3 面板「处理建议」全部失配（v1.7.4/1.7.5 改文案的连带伤）**：
  `admin.html` 的 `errorAction()` 仍匹配 `xsec_token` / `作品 ID` / `风控` / `不支持`
  这些**已从用户文案中删掉**的词，导致输入类错误被一律导到「查看服务日志」，**给运营的指导是错的**。
  修复：`/api/admin/errors` 每条错误带上 `error_kind`（`MAX(CASE WHEN error_kind != '' ...)`），
  前端改为**按 error_kind 判定**（`ACTION_BY_KIND` 覆盖 17 类），文案匹配仅作历史行兜底。
  实测：接口 9 条失败记录全部带 kind，建议映射零回落。

- **F4 `.device_secret` 权限 644 → 600**：这是 device_id 的 HMAC 密钥，保护「历史仅自己可见」。
  同机还跑着多个服务，任一以别的非 root 用户运行的进程读到它，就能伪造任意 device_id 读删他人历史。

- **F5 补齐基础安全响应头**：`X-Content-Type-Options: nosniff`、`X-Frame-Options: SAMEORIGIN`、
  `Referrer-Policy: strict-origin-when-cross-origin`（统一加在 `_optimize_response`）。
  **刻意不设 CSP** —— 页面含内联 `<script>/<style>` 并引用外部资源，配错会直接白屏。

> **关于「5003 直接暴露公网」：这是有意设计，不再作为缺陷项。**
> 本工具就是给外部创作者自助使用的，用户量大且来源分散，按 IP 收窄不现实。
> 因此**不加 `ACCESS_TOKEN`**（会挡住创作者），安全组也不收窄。
> 代价是限速器成为唯一守门人 —— 而它在并发下不准（见修复清单 W1），
> 该项优先级因此上升；`/api/cover` 的资源与内容类型防护（本版 F2）同理更重要。

验证：`ops/tests` 80 项全绿；隔离进程验证 F1/F2/F3/F5 全部通过（含真封面图未误伤）；
生产接口 3/3；`/admin` 模板已重新渲染；重启后无异常日志。
另清理了我此前以 root 跑 `py_compile` 留下的 root 属主 `.pyc`（现改用 `ast.parse` 自检）。

### 优化（v1.7.5 · 2026-09-27）· 用户可见文案：不再向用户汇报「我们自己的运行状况」

v1.7.4 把口语改成了书面，但仍有一类更根本的问题：**文案的主语是「我们」和「平台」，
而不是「用户这条链接」**。

- 「小红书平台暂时限制访问」—— 把「我们被平台限流」这件**内部运维问题**讲给用户：
  对用户既无意义，又像是在说这个工具不行；语法上也像「小红书官网访问不了」。
- 「平台未返回该作品内容」「平台页面已更新，暂时无法解析」—— 技术公告腔，像在念接口返回。
- **最严重的一句**：`链接域名或网络地址不在允许范围内: http://...` —— SSRF 防护术语 +
  原始 URL 直接抛给用户（`RedirectGuardError` 继承 `ValueError`，走 `str(e)` 透传分支）。
- 「链接不完整，可能被聊天工具截断」—— 用「可能」，等于没给结论。
- 术语混用（作品 / 笔记 / 内容）、「App 内」「页面状态」等内部说法。
- 页面底部印着 v1.6.0（实际已 v1.7.4），对外不一致。

改动（`lib/extractor.py` 30 处 + `app/templates/index.html` 1 处）：

- `error` 一律改成**用户视角的链接状态**：`该笔记/该作品暂时无法获取`、
  `该笔记已删除或设为私密`、`链接不完整`、`链接地址不受支持`、`链接无法访问`、
  `未识别到抖音作品`…… 用户文案里不再出现「平台限制访问」「域名 / 重定向」等词。
- `hint` 统一为一个动作，去掉「App 内」等内部说法。
- 抖音部分成功提示：「链接已转换为抖音用户主页；主页不包含单条作品的文案和数据」
  → 「该链接是抖音用户主页，不包含单条作品的文案」。
- 页脚版本号 v1.6.0 → v1.7.5。
- 把这条原则连同**反面例子**写进代码注释，防止以后又把内部术语写进用户文案。

安全性：`_is_risk_control_error()` 对 `PlatformLimitedError` 的判定不受文案影响
（**把异常文案传空字符串，判定仍为 True**，已隔离验证）；`app.py` 归因按 `error_kind` 走。
回归 80 项全绿，生产接口 3/3。

### 优化（v1.7.4 · 2026-09-27）· 用户可见文案统一：去歧义、去技术黑话

近 7 天失败记录里 **509 次是同一句**「小红书平台暂时限制访问，请稍后重试；若持续失败请从
App 重新复制最新分享链接」—— 前半句让用户等、后半句让用户换链接，**两个互相打架的动作**
堆在一句里，用户不知道该选哪个；另有「抖音作品不可用（status_reviewing）：作品不存在或
不可公开访问」把平台内部字段值和模糊结论一起抛给用户。

- **统一原则**：`error` 一句话说清发生了什么（不出现 `filter_reason` / `xsec_token` /
  「页面状态结构」这类代码术语）；`hint` 只给**一个**下一步动作。
- 主要改动：
  - 平台限制文案砍掉互相打架的后半句 → `小红书平台暂时限制访问` / `抖音平台暂时限制访问`
  - 抖音 `filter_reason` 代号翻成人话（`status_reviewing` →「该作品正在平台审核中」），
    代号只进日志供排查
  - 「未找到小红书笔记详情」→「该笔记已被删除或设为私密」
  - 「小红书页面状态结构变化」→「平台页面已更新，暂时无法解析」（是我们的问题，别让用户
    以为是自己填错）
  - 「小红书链接无效或缺少 xsec_token 参数……格式如：/discovery/item/xxx?xsec_token=...」
    →「链接无效或已失效」
  - 兜底 hint 由「请检查链接是否正确、作品是否公开可见、小红书链接是否带 xsec_token」
    （一次列三种可能，等于让用户自己排查）改为**按 `error_kind` 映射出的一个动作**
  - 去掉 error 里冗余的「提取失败: 」前缀（前端本就有「错误信息」标签，前缀只是把有用
    信息挤到行尾）
- **前提：把两处「靠匹配文案」的逻辑改成按错误类型判定**（否则改文案会静默打断逻辑）：
  - `lib/extractor.py` `_is_risk_control_error()` 主判据改为 `isinstance(exc, PlatformLimitedError)`
    —— 此前靠匹配文案里的「触发验证」，**改一个字的用户文案就会让抖音的换 IP 兜底失效**
  - `app.py` `_risk_trend()` 改为 `error_kind IN (...) OR error LIKE ?`（保留文案匹配以兼容历史行）
  - 修掉 `_validate_url_integrity` 上方一句错误注释（「失败负缓存会兜底 10 分钟」——
    该机制**并不存在**，`cache_put` 只在成功路径被调用，重试会实打实再打一遍平台）
- **口径效果**：管理面板近 7 天风控数 510 → **547**。多出的 37 次是抖音「触发验证」这类
  不含「平台暂时限制」字样的风控失败，旧口径**漏统计**了 —— 属修正，不是变差。

验证：`ops/tests` 80 项全绿；隔离验证覆盖输入类文案、**风控判定解耦**（把文案清空后
风控类异常仍判为 True、非风控仍为 False）、hint 全 kind 映射、抖音 reason 翻译；
生产接口 2/2。

### 优化（v1.7.3 · 2026-09-27）· 短链阶段：改分享头优先 + 纳入并发闸门

v1.7.2 修好笔记页之后，短链阶段还剩两处「按老假设写的」代码。

- **短链解析改「分享头优先」**（原为桌面头优先）。
  实测同一批 6 条短链交替请求：桌面头 **6/6** 被打回 `/login`、分享头 **6/6** 解析成功。
  桌面头既然必然失败，先打它就等于每条链接都白送一个「被平台拒绝」的负样本给风控，
  还多花约 300ms。改后与笔记页链路（`_fetch_xhs_note_page`）统一为分享头优先，
  隔离实测短链阶段换头次数 **6/6 → 0**，平均 **0.62s → 0.34s**。

- **短链解析纳入并发热闸门**（新增 `SHORTLINK_GATE`）。
  此前短链阶段**完全不受任何闸门保护**：笔记页有 `Semaphore(1)` + 1s 间隔，
  短链却能被多个 worker 线程同时打出去 —— 正是月底高峰期的瞬时并发突发源
  （本模块「平台请求闸门」注释里点名过这个场景，但闸门当时只加在了抖音和笔记页上）。
  新闸门**只限并发、不加额外间隔**（默认并发 2、间隔 0）：真实用户点短链时
  浏览器会立刻跟随 302 打第二跳，两跳贴在一起才像真人，被风控盯上的是
  「多用户瞬时并发」而非「同一用户的两次跳转」，所以只削峰、不拖慢单条。
  实测 6 线程并发解析短链，最大同时在飞数 **6 → 2**，单条耗时不变。
  可用 `SHORTLINK_GATE_CONCURRENCY` / `SHORTLINK_GATE_INTERVAL` 环境变量覆盖。

- 顺带：meta 兜底的作者字段正则兼容新版 `nickName`（原只认 `nickname`）。

实测：10 条真实链接 10/10 成功，抖音回归 2/2，`ops/tests` 80 项全绿，生产接口 3/3。

### 修复（v1.7.2 · 2026-09-27）· 小红书成功率 15% → 恢复：笔记页改「分享头优先」+ 兼容新版页面结构

- **现象**：9/19 起小红书成功率从 82% 一路跌到 9/24 的 7~15%（抖音同期 92% 正常），
  失败几乎全部报「小红书平台暂时限制访问，请稍后重试」，一度被误判为机房 IP 被平台限流。
- **真正的根因是两条代码缺陷，换出口 IP 一个都解决不了**：
  1. **笔记页请求写死桌面头，且没有分享头回退。** `_resolve_short_link()` 早有
     「桌面头被打回 → 换移动端分享头」的回退（所以日志里短链总能解析成功），
     但 `_extract_xhs_initial_state()` / `_extract_xhs_lightweight()` 请求笔记页时
     只发桌面头。小红书现在对桌面 UA 直接 302 到 `/login`，于是抛
     `XhsAccessDeniedError`，对外报成「平台暂时限制访问」。
     实测（同一笔记页 URL、同一时刻、同一出口 IP，交替各 3 次）：
     **桌面头被打回 3/3，分享头 0/3 且拿到 133KB 完整笔记页**。
  2. **页面结构改版未跟进。** 新版 `window.__INITIAL_STATE__` 把笔记数据放到
     `state["noteData"]["data"]["noteData"]`，而旧位置 `state["note"]["noteDetailMap"]`
     现已恒为空 `{}`，解析器取不到即抛 `ContentExpiredError("未找到小红书笔记详情")`，
     被当成「笔记已删除」。**即使请求头过了也会倒在这一步。**
- **修复**：
  - 新增 `_fetch_xhs_note_page()`：小红书笔记页统一走「分享头优先、桌面头回退」，
    两套头都被打回才判真限流（抛 `XhsAccessDeniedError` 交上层换 IP）。
  - 新增 `_xhs_note_from_state()`：解析兼容**新旧两种页面结构**，并保留
    「容器非空但取不到笔记 = 页面结构变化」与「容器为空 = 笔记被删」的区分。
    （注意新版作者字段是 `user.nickName`，不是 `nickname`。）
  - 归因映射修正：`short_link_blocked` / `platform_limited` 由 `upstream_error`
    改为 `platform_limited` —— 此前管理看板 `outcome_class='platform_limited'` **恒为 0**。
- **实测效果**：修复前同样 10 条真实失败链接 **0/10**，修复后 **10/10**；
  上线后生产接口实测 3/3，抖音回归 2/2 正常，`ops/tests` 80 项全绿。
- **副作用**：正常路径请求数不变（分享头一次过）；仅当分享头被打回才多发一次请求。
  不再需要靠换 IP 绕过，代理池额度消耗预计大幅下降
  （当天 6 笔租约 360s 全是 `xiaohongshu-risk`，都花在「买桌面头能过」上）。

### 修复（v1.7.1 · 2026-09-16）· 「缺 xsec_token」误报：/website-login/error 归为平台风控

- **现象**：`xhslink.cn/o/2RCIArkIEOz`（链接本身带有效 `xsec_token`）连续报
  「小红书链接无效或缺少 xsec_token 参数」，且被归类 `invalid_input`（用户输入问题）。
- **根因**：小红书对服务器出口打回的第二种拦截页此前未被识别——分享头会被
  302 到 `/website-login/error?...&error_code=300011&error_msg=账号异常，请稍后重试`：
  1. `_request_short_link()` 只判 `path == "/login"`，该错误页被当成「短链解析成功」；
  2. 轻量兜底用子串 `"error_code"/"error_msg" in final_url` 命中后抛
     `MissingTokenError`，把平台风控误报成用户缺 token（还污染了 invalid_input 统计）。
- **修复**：新增 `_is_xhs_bounce_page()`（判定 `/login`、`/website-login/error`、
  query 带 `error_code=/error_msg=`；`redirectPath` 的值是 URL 编码的，不会误伤），
  短链解析与两个提取函数的拦截页判定统一走它。命中后归 `short_link_blocked` /
  `platform_limited` 链路：计风控计数、达阈值按需换 IP（v1.7.0 机制）。
  轻量兜底的 "404" 判定同步收紧为 path 匹配，防止 ID/参数里恰好含 "404" 误伤。
- **验证**：ops/tests 80 项全桩测试全绿；线上复测该短链，报错口径变为
  「小红书平台暂时限制访问」（`error_kind=short_link_blocked`，`upstream_error`）。

### 变更（v1.7.0 · 2026-09-16）· ⚠️ 行为变更：换 IP 改为「按需取代理」

- **代理窗口不再按时间预租，改成「达阈值只挂意图、真有请求才取代理」。** 旧实现一达阈值就
  立刻 acquire 一个固定时长的窗口并开始计时，而线上请求稀疏（1–2 分钟一条）→ 窗口大多开在
  没请求的空档里：2026-09-16 实测 **17 个窗口 / 1261.7s 额度（已超当日 1200s）只承载了约 8 个
  请求**，其中一笔 300s 的升级档窗口覆盖 **0 个请求**。现在拆成两段：
  达阈值 → 只挂「换 IP 意图」（**零消耗**）；真有请求要走代理时 → `_ensure_proxy()` 才
  acquire（实测握手约 3s），拿到后握 `AJIASU_FAILOVER_HOLD_SECONDS`（默认 60s）到期释放。
- **升级档不再拉长窗口。** 300s 内风控 3 次仍判定「持续未缓解」，但动作由「把窗口拉到 300s」
  改为**强制换一条线路**（挂意图时带 `rotate`）。`AJIASU_FAILOVER_ESCALATE_HOLD` 已移除。
- **新增配置**：`AJIASU_FAILOVER_INTENT_TTL`（意图有效期，默认 300s；到期没用上即作废，不花钱）、
  `AJIASU_FAILOVER_HOLD_SECONDS`（真正取到代理后握多久，默认 60s）、
  `AJIASU_ACQUIRE_WAIT`（同伴正在取代理时最多等多久，默认 5s）。
- **移除配置**：`AJIASU_FAILOVER_WINDOW`（原短档窗口）与 `AJIASU_FAILOVER_ESCALATE_HOLD`
  —— 「窗口」概念被「握多久」取代。
- **并发安全**：acquire 改为**单飞**（`gthread` 单进程 4 线程共享同一份状态）。只有一个线程
  真正取代理，其余最多等 `AJIASU_ACQUIRE_WAIT` 秒复用同一份；等不到则退回直连，不拖死用户请求。
  爱加速 1080 全机单连接，因此这一步是必需的。意图在 acquire 结束后才清空（清早了会让等在
  门外的同伴误判成「没意图」而退回直连）。
- **可观测性**：新增「爱加速风控计数 [平台]：短档 a/b(cs) 长档 d/e(fs) → 已挂意图/已有代理/未达阈值」
  日志，触发来源可自证；「挂意图」与「真正 acquire」各有一条独立日志，便于逐笔对账。
- **回归测试**：新增 `ops/tests/test_ondemand.py`（30 项，含 4 线程并发单飞、意图过期零消耗、
  acquire 失败不重试等），并同步更新 `test_escalate.py`（21 项）。合计 **80 项全桩测试**，
  服务器 `ops/tests/run_all.sh` 全绿。

### 修复（v1.6.9.1 · 2026-09-16）
- **成功率被「首次失败 + 重试成功」双计拉低，阈值怎么调都会一直告警**：客户端拿到失败会在
  几秒后重试，同一条短链于是在 `history` 里留下「失败 + 成功」两行。实测近 30 分钟
  **30 次成功 / 18 次平台限流 ≈ 62%**，而用户其实每条都拿到了链接 —— 所以告警阈值设 90 还是 80
  都会持续误报。现在把「N 秒内同 URL 又成功」的失败行识别为**已自动补回**，不计入有效样本：
  同一窗口下成功率从 53.8% 修正为 100.0%，告警随之消失。
  - 新增 `ALERT_RECOVERED_GRACE_SECONDS`（默认 180s；设 0 即关闭该口径，退回旧行为）。
  - **只对**限流类失败（`platform_limited` / `short_link_blocked`）豁免；`page_changed`、
    `upstream_data_missing` 等真实故障即使随后重试成功，仍照常计入失败，避免掩盖代码缺陷。
  - 告警卡片新增「已自动补回 N 次」一行，软限流造成的影响仍然可见、不被隐藏。

### 修复（v1.6.9 · 2026-09-16）
- **换 IP 的代理窗口比请求间隔还短，换了等于没换**：兜底窗口原为固定 60s，而实际用户请求
  间隔约 2 分钟 —— 窗口在下一个请求到达前就已到期，请求照旧走直连（实测 14:16–14:24 每轮
  都是「失败 → 开 60s 窗口 → 到期 → 再失败」），既没解决限流，又白烧额度（当天 14:25 已用
  901s / 1200s）。新增**持续限流升级档**：300s 内风控达 3 次即判定「5 分钟未缓解」，强制再换
  一个出口 IP 并把窗口拉到 300s，覆盖住后续请求，把这波限流熬过去。
  - 短档（90s 内 2 次 → 60s 窗口）保持不变，用于零星软限流；两档互斥，一次调用只触发一档。
  - 相关阈值均可用环境变量覆盖：`AJIASU_FAILOVER_ESCALATE_RISK_WINDOW`（默认 300s）、
    `AJIASU_FAILOVER_ESCALATE_THRESHOLD`（默认 3）、`AJIASU_FAILOVER_ESCALATE_HOLD`（默认 300s）。

### 变更（v1.6.9 · 2026-09-16）
- **成功率告警阈值 90% → 80%**：短暂 IP 软限流属常态且会自愈，不再在 90% 这条线上频繁打扰；
  只有确实跌破 80% 才推送。
- **平台限流类告警默认静音**：新增 `ALERT_PLATFORM_ALERTS`（`off` / `auto` 默认 / `on`）。
  `auto` 下「平台暂时限制」不再单独推送 —— 改由 5003 自动换出口 IP 兜底；
  但若某平台的失败**并非以限流为主**（例如选择器失效、接口变更），仍会照常告警，
  避免把真实故障一起静音掉。

### 修复
- **失败归因不再靠猜**：失败原因改为在抛错处按**异常类型**直接落库（新增 `history.error_kind` 列），
  不再对报错文案做关键词匹配。此前「平台限流」「平台 404」「笔记已删除」三种根因共用
  「小红书平台暂时限制访问」同一句文案，导致成功率统计、平台趋势与飞书告警口径一并失真。
- **短链解析补上换 IP 兜底**：`_resolve_short_link()` 此前没有风控计数与换 IP 逻辑，
  被平台打回登录页只换请求头重试、两次都失败就静默返回原 URL。当天 35 次失败全部落在
  xhslink 短链上，正是这段盲区。现在短链阶段同样计入风控计数并参与换出口 IP。
- **短链形态预检**：查询参数被粘进路径（如 `https://xhslink.cn/oxsec_token/xxx`）时本地直接拦截，
  不再白跑 3 次重试 + 1 次兜底共 4 个平台请求。
- 用户输入问题（链接被截断、域名不支持、粘贴变形、缺 xsec_token）此前多数被计成服务失败，
  现在正确归入 `invalid_input`，不再拉低服务成功率。

### 优化
- 管理看板「失败原因 TOP」新增**归因分布**（限流 / 内容已删除 / 页面结构变化 / 用户输入问题 …），
  不再只显示那句万能文案；导出 CSV 增加 `error_kind` 列。
- 5003 告警的「平台暂时限制」计数改用 `error_kind`，并新增「其中短链阶段 N 次」拆解，
  直接指向是否需要换出口 IP。

### 优化
- 5003 在每月 1～5 日及月末 5 天自动采用保守批次并发，降低月底、月初平台风控概率。
- 全局提取线程池默认并发由 3 调整为 2，优先保证高峰期平台请求稳定性；普通时段仍可通过环境变量调整。

### 文档
- 在用户界面和 README 增加使用免责声明，明确提取结果不保证 100% 准确，使用者须逐条核对并自行承担使用后果。

## [v1.6.0] - 2026-08-30

### 新增
- **小红书提取可靠性链路**：页面主状态解析失败或正文为空时，使用独立会话进行有限重试；补充 JSON-LD 与 meta 解析兜底。
- **作品级完整结果缓存**：抖音、小红书的完整正文可按稳定作品 ID 跨分享形式复用；小红书共享缓存不保留分享参数。
- **小红书请求节流与快速预检**：每个服务进程内限制小红书页面请求节奏；识别登录页后立即提示分享凭证失效，避免无意义重试。

### 修复
- **小红书空文案误失败**：平台临时返回不完整页面时，不再首次请求即判定“未返回完整文案”；拿到稳定作品信息但仍无正文时保留可识别转换结果并标记待补。
- **失败原因可操作化**：过期或受限的小红书分享链接明确提示从 App 重新复制最新链接。

### 优化
- 提取链路增加阶段耗时观测：缓存、短链跳转、平台首次请求与解析、重试等待、重试请求/解析和缓存写入均写入服务日志，不改变提取策略。
- 历史记录新增 `outcome_class` 分类，区分成功、用户输入错误、内容失效、上游平台错误和内部错误。
- 管理后台新增服务稳定性、输入有效率、用户错误数和服务失败数，告警不再被无效链接直接拉低。
- 旧历史数据自动按错误文案回溯分类，CSV 导出包含结果分类。
- 增加 Cookie 会话的 CSRF 校验、登录/退出审计、历史告警记录和 CSV 导出。
- 后台显示服务运行时长，趋势图区分总提取与成功数量，并补充加载失败提示。
- 设备接口支持服务端分页参数，错误和平台统计可继续扩展筛选。
- 管理员登录改为 8 小时 HttpOnly 会话 Cookie，登录接口增加每 IP 限速；保留旧请求头兼容。
- 后台设备接口增加服务端分页元数据，失败平台统计补充从原始链接推断。
- 仪表盘增加异常状态标题/Toast 提醒，底部卡片等高，趋势图悬停详情、筛选排序和错误处理建议。
- 后台看板支持最近 24 小时、7 天、30 天筛选。
- 接入性能与缓存指标，并显示最后更新时间。
- 活跃设备按所选窗口统计，同时补充近 7 天与近 30 天口径。
- 管理员口令迁移至权限为 600 的 `/etc/link-extractor.env`，避免直接写入 systemd 单元。

## [v1.5.4] - 2026-08-18

### 修复
- **数据库维护容错**：服务忙碌导致 WAL 检查点被锁定时跳过本次检查点，维护任务继续成功完成并在下次定时执行时重试。

## [v1.5.3] - 2026-08-17

### 优化
- **共享缓存与安全 DNS 缓存**：成功结果按完整输入链接缓存至 SQLite（24 小时，跨 worker 和重启可用）；已验证的公网 DNS 解析短缓存 60 秒并定期重验。
- **有界提取队列**：提取任务复用每进程全局线程池，批次采用滑动提交，避免大量链接一次性抢占线程；可通过 `GLOBAL_EXTRACT_CONCURRENCY` 配置。
- **性能指标与维护**：历史记录新增耗时和缓存命中字段，管理员接口提供性能汇总；每日 systemd 任务清理过期缓存/限流记录并优化 SQLite。

## [v1.5.2] - 2026-08-17

### 新增
- **本机健康兜底**：新增 systemd 定时健康检查。每分钟探测本机健康接口，连续两次失败才自动重启服务，避免短暂波动误重启；检查和重启事件写入系统日志，无需第三方服务或额外费用。

## [v1.5.1] - 2026-08-17

### 优化
- **并发与可观测性**：生产部署默认改为 2 个 Gunicorn worker、每 worker 2 个线程，避免一个长批量提取阻塞其他用户请求；单批提取并发数通过 `EXTRACT_CONCURRENCY` 配置（默认 3，范围 1-5），继续控制外部平台请求节奏。
- **耗时日志**：记录每条提取和每批任务的耗时、成功数与并发数，便于通过服务日志定位平台请求、解析或数据库环节的性能问题。

## [v1.5.0] - 2026-08-17

### 新增
- **后台运营看板（管理员）**：
  - 独立页面 `/admin` + 独立管理员口令 `ADMIN_TOKEN`（环境变量），与普通访问完全隔离；未配置时管理接口返回 503 防裸奔
  - 4 个聚合接口（均含 IP 限速）：`/api/admin/overview`（总提取/成功率/活跃设备/今日/近7天趋势/平台分布）、`/api/admin/devices`（设备排行，ID 脱敏）、`/api/admin/errors`（失败原因 TOP）、`/api/admin/recent`（最近动态，不含任何 URL/文案）
  - 数据脱敏：设备 ID 只显示前后 4 位，最近动态不返回原始链接与文案，保护使用者隐私
  - history 表新增 `created_at`、`status` 索引（全局聚合加速）

## [v1.4.8] - 2026-08-17

### 修复
- **抖音间歇性提取失败（重要）**：抖音升级风控后，首次请求可能返回「有 `_ROUTER_DATA` 标记但 loaderData 是占位/不完整结构」的页面。原逻辑只判断正则是否匹配，占位页会被误判为解析成功，直接报「无法解析视频或图集信息」，且**不触发二次请求兜底**。重构为统一的 `_parse_douyin_video_info()` 校验——只有真正解析出 `videoInfoRes` 才算成功，否则依次走二次 share URL 请求 + 新会话重试，成功率大幅提升

## [v1.4.7] - 2026-08-11

### 优化
- **抖音平台标签改黑色**：黑底白字（原为浅红底红字，与小红色系太接近、缺少标志性；黑底白字呼应抖音品牌主色），小红书保持红色系
- **「更多信息」里的原视频链接折叠**：抖音原始视频地址（带签名参数很长）改为单行省略显示 + 悬停显示完整地址，不再撑满卡片

## [v1.4.6] - 2026-08-11

### 变更
- **口令模块默认隐藏（打开即用）**：前端移除口令输入弹窗、页脚「清除口令/退出」按钮及相关 401 重试逻辑；后端 `ACCESS_TOKEN` 校验机制保留（不设置即不校验）。线上部署不再设置 `ACCESS_TOKEN`，任何人可直接使用
- 若以后需恢复访问限制：设置 `ACCESS_TOKEN` 环境变量重启服务即可（后端校验自动生效，前端逻辑已预留）

## [v1.4.5] - 2026-08-11

### 优化
- **长链接/长文案视觉折叠**：
  - 链接字段（含转换链接/原始链接）改为单行省略显示，鼠标悬停显示完整地址；复制按钮复制的仍是完整链接（CSS 截断不影响 DOM 文本）
  - 文案字段由「最高 160px + 滚动条」改为默认最多显示 3 行，超长时右下角出现「展开全部 ▼」按钮，可展开看全文 / 收起；历史记录卡片同步生效
  - 展开按钮仅在实际超长时显示（按内容高度动态检测），短文案卡片不出现多余按钮

## [v1.4.4] - 2026-08-11

### 修复
- **复制按钮文字单字折行**：「复制链接/复制文案」按钮在较窄容器下文字被压缩成单字换行。`.copy-row .btn` 增加 `white-space: nowrap` 禁止折行，最小宽度 110px → 120px（容纳 emoji + 4 字完整文字）

## [v1.4.3] - 2026-08-11

### 新增
- **deploy.sh 适配 RHEL 系服务器（重要）**：自动识别发行版——Debian/Ubuntu 用 `www-data` 用户，OpenCloudOS/CentOS/Rocky 等 RHEL 系用 `root`（原脚本在 RHEL 系会因无 www-data 用户导致 systemd 启动失败）；防火墙提示按 firewalld/ufw 区分
- **deploy.sh 支持 git 拉取部署**：新增 `GIT_REPO` 环境变量，服务器上一条命令完成 clone + 部署（国内服务器可走 SSH 443 通道），后续 `git pull` 即可更新

### 优化
- **gunicorn `--timeout` 60 → 120**：抖音提取最长链路（请求+风控重试）可能超 60 秒，提高超时避免 worker 被误杀
- **封面代理缓存淘汰**：超限时由「清空全部」改为「淘汰最旧的一半」，保留热数据减少回源
- **限速桶清理触发**：由依赖时间戳巧合（`int(now) % 100`）改为独立写计数，清理时机稳定可靠
- **封面白名单清理**：移除匹配不到真实域名的无效条目 `sns-img`、`cn-hangzhou`
- **代码规范**：app.py 中部 import 统一移到文件顶部

### 文档
- README 限速描述统一为每 IP 20 次/分钟（原功能列表误写 30）
- README 部署示例统一为单 worker + timeout 120（低内存服务器 + SQLite 跨进程一致性）
- README 补充 git 拉取部署方式与 RHEL 系说明

## [v1.4.2] - 2026-07-31

### 优化
- **移除「🔄 重新输入口令」按钮**：口令输入已由 4 个自动触发点覆盖（页面加载验证、apiFetch/流式提取/重试的 401 自动弹窗），按钮属冗余入口，删除后页脚更简洁；「清除口令 / 退出」保留，换口令时退出重输即可

## [v1.4.1] - 2026-07-31

### 修复
- **封面图不显示（重要）**：访问口令启用后，`/api/cover` 被 401 拦截——前端 `<img>` 标签无法携带自定义请求头，导致所有封面加载失败。已将 `/api/cover` 加入口令豁免，并为其单独加 IP 限速（防滥用）
- **封面代理校验误拦**：`_is_cover_url_safe` 复用了提取器的域名白名单（只认页面域名），导致图片 CDN 域名（douyinpic.com 等）被误拒。改为独立的白名单 + 公网 IP 校验

## [v1.4] - 2026-07-31

### 修复
- **rate_limits 表无限增长**：新增过期桶自动清理（写入时触发，删除超过 1 小时的限速记录），防止长期运行数据库膨胀
- **设备限速可绕过**：无有效设备签名的请求不再参与设备维度限速（防伪造设备每次换新 ID 绕过），由 IP 限速兜底
- **前端口令交互完善**：
  - 页面加载即验证口令（无口令立即弹窗，无需等首次操作）
  - 页脚新增「🔄 重新输入口令」按钮（口令过期/输错时手动触发）
  - 取消口令弹窗时给出明确提示「请输入访问口令后才能使用工具」
- **清除重复的 `apiFetch` 定义**（旧版残留覆盖新版导致口令逻辑失效）

### 变更
- 版本号同步：页面页脚与 README 统一为 v1.4

## [v1.3] - 2026-07-31

### 安全加固（自己使用 + 小范围分享场景）
- **轻量访问口令**：环境变量 `ACCESS_TOKEN` 设置后，所有 `/api/*` 接口需携带 `X-Access-Token` 头（`hmac.compare_digest` 防时序攻击），401 返回「访问口令错误或未提供」；未设置则跳过（本地开发）；前端简洁口令弹窗 + 清除口令按钮
- **频率限制持久化**：限速从内存 dict 改为 **SQLite 存储**（重启不丢、多 worker 生效）；`/api/extract`、`/api/history`、`/api/stats` 全部启用；**IP 维度 20 次/分钟 + 设备维度 15 次/分钟** 双维度
- **device_id 防伪造**：环境变量 `DEVICE_SECRET`（未设置自动生成临时密钥并告警）；服务端签发 device_id 时附 HMAC-SHA256 签名，前端保存 device_id+signature，后端校验签名，伪造他人 device_id 无法越权访问历史/统计
- **错误信息脱敏**：未知异常统一返回「提取失败，请稍后重试」，详细异常（路径/库名/堆栈）只进服务端日志；已知业务错误（作品不存在/缺参数）保留有用文案
- **重定向安全加固**：封面代理 `/api/cover` 改为手动跟随重定向（每跳校验域名+公网IP）；抖音/小红书提取链路同样改为逐跳校验

### 修复
- 封面代理白名单从子串匹配改为精确域名匹配（防 `evil-douyinpic.com` 绕过）

## [v1.2] - 2026-07-31

### 性能优化
- **SQLite WAL 模式**：并发读写不再互相阻塞，多线程写库性能显著提升（+ busy_timeout 防锁死）
- **gzip 响应压缩**：API 文本/JSON 响应自动压缩（压缩率实测 ~92%），在线传输体积大幅下降；文件响应与流式响应自动跳过（修复 direct_passthrough 兼容问题）
- **静态资源浏览器缓存**：manifest/图标等加 `Cache-Control: max-age=3600`，重复打开更快（sw.js 除外避免更新失效）

### 修复
- after_request gzip 逻辑对 `send_from_directory` 文件响应抛 `RuntimeError` 的问题（跳过文件/流式响应）

## [v1.1] - 2026-07-31

### 新增
- **移动端优化**：
  - 响应式适配：窄屏（≤600px）下工具栏按钮等宽、触控区 ≥44px、统计卡片单列、封面图缩小、Toast 避开刘海屏
  - PWA 基础版：`manifest.json` + 192/512 图标 + Service Worker（网络优先缓存静态资源），支持手机"添加到主屏幕"像 App 一样启动
  - 移动端交互：输入框聚焦自动滚动到可视区，避免手机键盘遮挡
- **一键部署脚本** `deploy.sh`：服务器自动建 venv、装依赖、配置 systemd 服务、健康检查（适配 Debian/Ubuntu）

### 变更
- 无破坏性变更

## [v1.0] - 2026-07-31

### 新增
- **核心功能**：
  - 粘贴抖音/小红书分享链接 → 1 秒自动提取（支持整段混合文本智能识别）
  - 转换链接生成：抖音 → `douyin.com/video/{id}`；小红书 → `explore/{id}` + 保留 xsec_token
  - 文案提取与清洗（去 `[话题]` 标签、`>` 引用）
  - 逐条独立复制（每条结果含「复制链接」「复制文案」按钮）
- **批量与性能**：
  - 3 并发提取 + 随机抖动（规避平台风控节奏检测）
  - 作品 ID 缓存（同一视频不同链接 24h 秒回）
  - HTTP 连接池复用
  - 流式返回：每条完成即时显示，无需等待全部
- **数据与安全**：
  - 历史记录：SQLite 存储 + 设备 ID 隔离（每人只见自己的数据）
  - 统计看板：总数/成功率/平台分布/近 7 天趋势（SVG 折线图）
  - SSRF 防护（域名白名单 + 内网 IP 拦截）、速率限制（每 IP 30 次/分钟）
  - 封面图后端代理（绕开跨域防盗链 + 24h 缓存）
- **体验**：
  - 失败一键重试
  - 实时链接计数提示
  - 页脚作者信息（黄徽徽）
- **启动方式**：
  - `start.py` 一键启动器（自动建 venv/装依赖/选端口/开浏览器）
  - `启动-链接提取工具.bat` Windows 双击启动

### 修复
- 整段混合文本粘贴只识别第一条的问题（改为按 URL 数识别）
- 抖音偶发"解析视频信息失败"（风控重试机制）
- 前端链接计数与后端不一致（统一正则）
- 清空历史按钮无反应（iframe 沙箱禁用 confirm，改二次点击确认）
- 封面不显示（跨域签名校验，后端代理解决）
- Windows 启动脚本闪退（UTF-8 编码问题，改 Python 启动器）

### 已知问题
- HTTP 环境下 PWA Service Worker 无法注册（浏览器安全限制），需 HTTPS 部署后完整生效
- 服务器数据中心 IP 提取抖音可能偶发失败（平台反爬）

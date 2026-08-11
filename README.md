# 链接提取工具

粘贴抖音/小红书分享链接 → 批量提取**转换链接 + 文案** → 逐条独立复制。轻量 Web 工具，纯 Python + requests，无浏览器依赖，适合低内存服务器。

**作者**：黄徽徽 · 联系方式：H15217830799 · 项目：链接提取工具 v1.4.5
**版权说明**：本工具免费开源，任何人可自由使用/修改/分发，但请保留页面底部及本文档的作者信息。

## 项目概述

**背景**：创作者需要定期把抖音/小红书作品链接提交到内容表单（如飞书多维表格），手动复制粘贴整理 14 个字段繁琐易错。本工具将"链接 → 信息"的提取环节独立成 Web 服务，一次粘贴、自动提取、逐条复制，把单次整理时间从几分钟压缩到几秒。

**核心能力**：
- 粘贴抖音/小红书分享链接（支持整段混合文本）→ **1 秒自动提取**
- 生成**转换链接**（去渠道参数）+ 提取**文案/作者/时间/点赞/封面**
- **逐条独立复制**，适配一条条填表
- 历史记录 + 统计看板，**设备 ID 隔离**（每人只见自己的数据）
- 纯 Python 实现，无浏览器依赖，低内存服务器可跑

**适用人群**：自媒体创作者、内容运营、需要批量整理平台作品链接的个人或小团队。

## 功能

- 🎯 批量提取：粘贴后 **1 秒自动提取**，无需点击；也支持手动"提取全部"（最多 20 条）
- 📋 整段粘贴智能识别：文案与多个链接混在一起也能自动提取全部链接
- 🔗 转换链接优先展示：
  - 抖音 → `https://www.douyin.com/video/{id}`（去掉渠道参数）
  - 小红书 → `https://www.xiaohongshu.com/explore/{id}?xsec_token=...`（保留必要参数）
- 📝 文案展示（小红书会清理 `[话题]` 标签、`>` 引用）
- 📋 **逐条独立复制**：每条结果有「复制链接」「复制文案」两个独立按钮，方便一条一条填写
- 🕐 历史记录：服务端 SQLite 存储 + **设备 ID 隔离**（每人只能看到自己的记录）
- 📊 统计看板：总数/成功率/平台分布/近 7 天趋势（SVG 折线图）
- 📱 移动端：响应式适配 + PWA 添加到主屏（HTTPS 下完整生效）
- 🛡️ 安全：SSRF 防护（域名白名单 + 内网 IP 拦截）、速率限制（每 IP 20 次/分钟）

> 完整变更记录见 [CHANGELOG.md](./CHANGELOG.md)。

## 技术方案

| 项 | 方案 |
|----|------|
| 后端 | Python Flask |
| 提取 | 抖音：移动端分享页 `_ROUTER_DATA` JSON 解析；小红书：`__INITIAL_STATE__` 解析 + meta 轻量兜底 |
| 存储 | SQLite（`data/history.db`，每设备保留最近 200 条） |
| 依赖 | 仅 `flask` + `requests`（+ 生产环境 `gunicorn`） |
| 端口 | 5003（自动检测占用，被占则 +1；可用环境变量 `PORT` 修改） |

> 提取逻辑提炼自开源 SDK `social-media-toolkit`（JNHFlow21）与 `video-tools-local` 项目。抖音采用移动端分享页公开数据，无签名逆向。

## 本机运行

Windows 双击 `启动-链接提取工具.bat` 即可（自动建 venv、装依赖、开浏览器）。也可以手动：

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt

# 2. 一键启动（推荐，自动选端口+开浏览器）
python start.py
# 指定端口
python start.py 8080

# 3. 或直接启动（默认端口 5003）
python app.py
PORT=8080 python app.py

# 4. 浏览器打开
# http://127.0.0.1:5003
```

## 安全配置（环境变量）

| 环境变量 | 必填 | 说明 |
|---------|------|------|
| `ACCESS_TOKEN` | 部署时必须 | 访问口令。设置后所有 API 需携带 `X-Access-Token` 头；未设置则跳过校验（本地开发用）。**给朋友分享前务必设置** |
| `DEVICE_SECRET` | 推荐 | 设备签名密钥。用于防伪造 device_id（历史越权）；未设置自动生成临时密钥（重启后设备失效） |
| `RATE_IP_PER_MINUTE` | 可选 | 每 IP 每分钟请求上限，默认 20 |
| `RATE_DEVICE_PER_MINUTE` | 可选 | 每设备每分钟请求上限，默认 15 |

示例（Linux 部署）：
```bash
ACCESS_TOKEN="你的私人口令" DEVICE_SECRET="一串随机字符" ./venv/bin/gunicorn -w 1 -b 0.0.0.0:5003 app:app --timeout 120
```

> 前端首次访问会弹出口令输入框，输入后保存在浏览器 localStorage；页脚有「清除口令/退出」按钮。

## 部署到服务器（Debian 12 / OpenCloudOS 已验证）

服务器要求：Python 3.10+，无需 Docker，无需 Node.js。

**方式一：git 拉取部署（推荐，后续更新 `git pull` 即可）**
```bash
# 国内服务器访问 GitHub 需先配置 SSH 443 通道（~/.ssh/config）：
#   Host github.com
#     HostName ssh.github.com
#     Port 443
#     User git

ACCESS_TOKEN="你的口令" DEVICE_SECRET="随机串" \
GIT_REPO="git@github.com:2995288295/link-extractor.git" \
sudo bash deploy.sh
```

**方式二：上传代码后部署**
```bash
# 1. 上传项目到服务器（例：/opt/link-extractor/）
# 2. 创建虚拟环境并安装
cd /opt/link-extractor
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. 用 gunicorn 启动（单 worker：SQLite 限速/缓存跨进程共享，低内存服务器推荐）
./venv/bin/gunicorn -w 1 -b 0.0.0.0:5003 app:app --timeout 120

# 4. 配置 systemd 常驻（可选，推荐）
sudo tee /etc/systemd/system/link-extractor.service > /dev/null <<'EOF'
[Unit]
Description=Link Extractor Web Service
After=network.target

[Service]
WorkingDirectory=/opt/link-extractor
ExecStart=/opt/link-extractor/venv/bin/gunicorn -w 1 -b 0.0.0.0:5003 app:app --timeout 120
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now link-extractor
sudo systemctl status link-extractor
```

> 注：`deploy.sh` 会自动识别发行版——Debian/Ubuntu 用 `www-data` 用户，OpenCloudOS/CentOS 等 RHEL 系用 `root`（无 www-data 用户）。

## 防火墙

服务器有公网 IP 时需放行端口（Debian 12 若无 ufw 可跳过）：

```bash
sudo ufw allow 5003/tcp
```

## 访问

- 无域名：`http://服务器公网IP:5003`
- 建议后续配 Nginx 反代 + HTTPS（域名需 ICP 备案）

## 注意事项

1. **反爬风险**：在线部署后抖音/小红书看到的是数据中心 IP，提取成功率可能低于本机；若频繁失败，建议限制使用频率（已内置每 IP 20 次/分钟）
2. **小红书必须带 xsec_token**：请用小红书 App「复制链接」功能获取分享链接（含 xsec_token 参数），否则无法提取
3. **合规**：仅用于个人内容整理，请勿高频批量抓取
4. **抖音稳定性**：依赖 iesdouyin 分享页公开数据，若平台改版需跟进适配

## 项目结构

```
link-extractor/
├── app.py                  # Flask 后端（API + 历史 + 统计 + 设备隔离 + 封面代理）
├── lib/
│   └── extractor.py        # 提取核心（抖音/小红书 + 转换链接 + 缓存 + 连接池 + 安全）
├── app/
│   ├── templates/
│   │   └── index.html      # 前端单页（自动提取/逐条复制/历史/统计折线图）
│   └── static/
├── start.py                # 一键启动器（自动建 venv/装依赖/选端口/开浏览器）
├── 启动-链接提取工具.bat   # Windows 双击启动
├── requirements.txt
└── README.md
```

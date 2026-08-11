#!/usr/bin/env bash
# =====================================================
# 链接提取工具 - 服务器一键部署脚本
# 适用：Debian 12 / Ubuntu / OpenCloudOS / CentOS 等 Linux（Python 3.10+）
# 两种用法：
#   1) 代码已在服务器:  ACCESS_TOKEN="口令" DEVICE_SECRET="随机串" sudo bash deploy.sh
#   2) 从 Git 拉取部署:  ACCESS_TOKEN="口令" DEVICE_SECRET="随机串" \
#        GIT_REPO="git@github.com:2995288295/link-extractor.git" sudo bash deploy.sh
# =====================================================
set -e

APP_DIR="${APP_DIR:-/opt/link-extractor}"
SERVICE_NAME="link-extractor"
PORT="${PORT:-5003}"

echo "=============================================="
echo " 链接提取工具部署"
echo " 目标目录: $APP_DIR"
echo " 端口: $PORT"
if [ -z "${ACCESS_TOKEN:-}" ]; then
    echo " [警告] 未设置 ACCESS_TOKEN，服务将无访问口令（任何人可用）！"
    echo "         建议: ACCESS_TOKEN=你的口令 DEVICE_SECRET=随机串 sudo bash deploy.sh"
fi
echo "=============================================="

# 1. 检查 root 权限
if [ "$(id -u)" -ne 0 ]; then
    echo "[错误] 请用 sudo 运行: sudo bash deploy.sh"
    exit 1
fi

# 2. 检查 Python 与 git
if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未找到 python3，请先安装:"
    echo "  Debian/Ubuntu:   apt install python3 python3-venv python3-pip"
    echo "  RHEL/OpenCloudOS: dnf install python3 python3-pip"
    exit 1
fi
PY_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
echo "[OK] Python $PY_VERSION"

# 3. 识别发行版（决定运行用户与防火墙提示）
. /etc/os-release 2>/dev/null || true
OS_ID="${ID:-unknown}"
case "$OS_ID" in
    debian|ubuntu)
        SERVICE_USER="www-data"
        FW_HINT="sudo ufw allow ${PORT}/tcp"
        ;;
    rhel|centos|opencloudos|rocky|almalinux|anolis|fedora)
        # RHEL 系无 www-data 用户：个人服务器直接用 root 运行（目录权限最省心）；
        # 若已安装 nginx/apache 且想最小权限，可改这里为对应用户并 chown
        SERVICE_USER="root"
        if command -v firewall-cmd >/dev/null 2>&1; then
            FW_HINT="sudo firewall-cmd --permanent --add-port=${PORT}/tcp && sudo firewall-cmd --reload"
        else
            FW_HINT=""
        fi
        ;;
    *)
        SERVICE_USER="root"
        FW_HINT=""
        echo " [提示] 未识别的发行版 ($OS_ID)，按 root 用户部署；如需最小权限请手动调整 systemd User= 与目录属主"
        ;;
esac
echo "[OK] 发行版: ${OS_ID}（服务运行用户: ${SERVICE_USER}）"

# 4. 创建目录
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# 5. 拉取或检查代码
if [ -n "${GIT_REPO:-}" ]; then
    if [ -d "$APP_DIR/.git" ]; then
        echo "[步骤] 检测到已 clone 的仓库，执行 git pull 更新..."
        git pull --ff-only || true
    else
        echo "[1/6] 从 Git 克隆代码: $GIT_REPO"
        if ! git clone "$GIT_REPO" "$APP_DIR" 2>/dev/null && [ ! -f "$APP_DIR/app.py" ]; then
            echo "[错误] git clone 失败。国内服务器访问 GitHub 建议先配置 SSH 443 通道:"
            echo "       ~/.ssh/config 中: Host github.com / HostName ssh.github.com / Port 443 / User git"
            exit 1
        fi
    fi
fi

if [ ! -f "$APP_DIR/app.py" ]; then
    echo "[提示] 未检测到 app.py，请先上传代码或设置 GIT_REPO 环境变量拉取"
    echo "       上传完成后重新运行: sudo bash deploy.sh"
    exit 0
fi

# 6. 创建虚拟环境并安装依赖
echo "[2/6] 创建虚拟环境..."
if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv venv
fi
echo "[3/6] 安装依赖..."
./venv/bin/pip install -r requirements.txt -q --upgrade pip -q
echo "[OK] 依赖安装完成"

# 7. 创建数据目录并授权
echo "[4/6] 初始化数据目录..."
mkdir -p "$APP_DIR/data"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR" 2>/dev/null || true

# 8. 配置 systemd 服务
echo "[5/6] 配置 systemd 服务..."
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Link Extractor Web Service
After=network.target

[Service]
WorkingDirectory=$APP_DIR
Environment=ACCESS_TOKEN=${ACCESS_TOKEN:-}
Environment=DEVICE_SECRET=${DEVICE_SECRET:-}
ExecStart=$APP_DIR/venv/bin/gunicorn -w 1 -b 0.0.0.0:${PORT} app:app --timeout 120
Restart=always
RestartSec=3
User=${SERVICE_USER}
Group=${SERVICE_USER}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME} >/dev/null 2>&1 || true

# 9. 启动服务
echo "[6/6] 启动服务..."
systemctl restart ${SERVICE_NAME}
sleep 2

# 10. 健康检查
echo "----------------------------------------------"
if systemctl is-active --quiet ${SERVICE_NAME}; then
    echo "[OK] 服务已启动!"
    IP=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || echo "服务器IP")
    echo "访问地址: http://${IP}:${PORT}"
    echo "健康检查: curl http://127.0.0.1:${PORT}/api/health"
    echo "查看日志: journalctl -u ${SERVICE_NAME} -f"
else
    echo "[错误] 服务启动失败，查看日志:"
    journalctl -u ${SERVICE_NAME} --no-pager -n 20
    exit 1
fi

# 11. 防火墙放行提示
echo "----------------------------------------------"
if [ -n "$FW_HINT" ]; then
    echo "若使用系统防火墙，请放行端口:"
    echo "  $FW_HINT"
fi
echo "若服务器在云厂商（腾讯云等）安全组中，请在控制台放行 ${PORT}/tcp"
echo "=============================================="
echo "部署完成！"

#!/usr/bin/env bash
# =====================================================
# 链接提取工具 - 服务器一键部署脚本
# 适用：Debian 12 / Ubuntu 等 Linux 服务器（Python 3.10+）
# 用法：ACCESS_TOKEN="你的口令" DEVICE_SECRET="随机串" sudo bash deploy.sh
# =====================================================
set -e

APP_DIR="/opt/link-extractor"
SERVICE_NAME="link-extractor"
PORT=5003

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

# 2. 检查 Python
if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未找到 python3，请先安装: apt install python3 python3-venv python3-pip"
    exit 1
fi
PY_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
echo "[OK] Python $PY_VERSION"

# 3. 创建目录（如果代码还没上传，先建目录）
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# 4. 检查代码是否存在
if [ ! -f "$APP_DIR/app.py" ]; then
    echo "[提示] 未检测到 app.py，请先把项目代码上传到 $APP_DIR"
    echo "       (app.py / lib/ / app/ / requirements.txt / start.py)"
    echo "       上传完成后重新运行: sudo bash deploy.sh"
    exit 0
fi

# 5. 创建虚拟环境并安装依赖
echo "[1/5] 创建虚拟环境..."
if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv venv
fi
echo "[2/5] 安装依赖..."
./venv/bin/pip install -r requirements.txt -q --upgrade pip -q
echo "[OK] 依赖安装完成"

# 6. 创建数据目录并授权
echo "[3/5] 初始化数据目录..."
mkdir -p "$APP_DIR/data"
chown -R www-data:www-data "$APP_DIR" 2>/dev/null || true

# 7. 配置 systemd 服务
echo "[4/5] 配置 systemd 服务..."
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Link Extractor Web Service
After=network.target

[Service]
WorkingDirectory=$APP_DIR
Environment=ACCESS_TOKEN=${ACCESS_TOKEN:-}
Environment=DEVICE_SECRET=${DEVICE_SECRET:-}
ExecStart=$APP_DIR/venv/bin/gunicorn -w 1 -b 0.0.0.0:${PORT} app:app --timeout 60
Restart=always
RestartSec=3
User=www-data
Group=www-data

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME} >/dev/null 2>&1 || true

# 8. 启动服务
echo "[5/5] 启动服务..."
systemctl restart ${SERVICE_NAME}
sleep 2

# 9. 健康检查
echo "----------------------------------------------"
if systemctl is-active --quiet ${SERVICE_NAME}; then
    echo "[OK] 服务已启动!"
    IP=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || echo "服务器IP")
    echo "访问地址: http://${IP}:${PORT}"
    echo "健康检查: curl http://127.0.0.1:${PORT}/api/health"
else
    echo "[错误] 服务启动失败，查看日志:"
    journalctl -u ${SERVICE_NAME} --no-pager -n 20
    exit 1
fi

# 10. 防火墙放行
echo "----------------------------------------------"
echo "若使用 ufw 防火墙，请放行端口:"
echo "  sudo ufw allow ${PORT}/tcp"
echo "=============================================="
echo "部署完成！"

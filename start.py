#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""链接提取工具 - 一键启动器（Python 版）

功能：
- 自动检测/创建虚拟环境，安装依赖
- 自动选择可用端口（默认 5003，被占用自动 +1）
- 启动 Flask 服务，日志实时显示
- 服务就绪后自动打开浏览器
- 按 Ctrl+C 或输入 q 退出并停止服务

用法：
    python start.py            # 默认端口 5003
    python start.py 8080       # 指定端口
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VENV_DIR = BASE_DIR / "venv"
DEFAULT_PORT = 5003


def find_available_port(preferred: int) -> int:
    """返回可用端口：优先 preferred，被占用则 +1 依次探测。"""
    port = preferred
    for _ in range(10):  # 最多探测 10 个
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                port += 1
    raise RuntimeError("没有找到可用端口（5003-5012 均被占用）")


def get_python() -> Path:
    """优先用项目 venv 的 python，否则用系统 python。"""
    venv_py = VENV_DIR / "Scripts" / "python.exe"
    if venv_py.exists():
        return venv_py
    # 当前解释器就是 python
    return Path(sys.executable)


def ensure_venv(python: Path) -> Path:
    """确保 venv 存在，返回 venv 内的 python。"""
    if python == Path(sys.executable) and not VENV_DIR.exists():
        print("[1/3] 首次运行，创建虚拟环境...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
        python = VENV_DIR / "Scripts" / "python.exe"
    if not python.exists():
        raise RuntimeError("Python 环境异常，请检查安装")
    return python


def ensure_deps(python: Path) -> None:
    print("[2/3] 检查依赖...")
    req = BASE_DIR / "requirements.txt"
    if not req.exists():
        return
    # 快速检查核心依赖是否已装
    probe = subprocess.run(
        [str(python), "-c", "import flask, requests"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        print("      正在安装依赖，首次可能需要 1-2 分钟...")
        subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(req), "-q"],
            check=True,
        )
        print("      依赖安装完成")


def main() -> int:
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT

    print("=" * 56)
    print("   链接提取工具 - 一键启动")
    print(f"   访问地址: http://127.0.0.1:{port}")
    print("   本窗口将实时显示运行日志")
    print("   按 Ctrl+C 停止服务")
    print("=" * 56)
    print()

    # 1. 环境准备
    base_python = Path(sys.executable)
    python = ensure_venv(base_python)
    ensure_deps(python)

    # 2. 端口选择
    try:
        final_port = find_available_port(port)
    except RuntimeError as e:
        print(f"[错误] {e}")
        return 1
    if final_port != port:
        print(f"[提示] 端口 {port} 被占用，改用端口 {final_port}")

    # 3. 启动服务（子进程）
    print(f"[3/3] 启动服务（端口 {final_port}）...")
    print()
    env = dict(os.environ)
    env["PORT"] = str(final_port)
    proc = subprocess.Popen(
        [str(python), str(BASE_DIR / "app.py")],
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    # 4. 等待端口就绪
    url = f"http://127.0.0.1:{final_port}"
    ready = False
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            print("[错误] 服务进程提前退出，退出码:", proc.returncode)
            return 1
        try:
            with socket.create_connection(("127.0.0.1", final_port), timeout=1):
                ready = True
                break
        except OSError:
            time.sleep(0.5)
    if not ready:
        print("[错误] 服务启动超时（30 秒）")
        proc.kill()
        return 1

    # 5. 打开浏览器
    print(f"\n[OK] 服务已启动，正在打开浏览器: {url}")
    webbrowser.open(url)
    print()
    print("服务运行中，日志实时输出如下：")
    print("-" * 56)

    # 6. 实时转发子进程日志到本窗口
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)
    except KeyboardInterrupt:
        print("\n\n收到停止信号，正在停止服务...")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("服务已停止，再见！")
    return 0


if __name__ == "__main__":
    sys.exit(main())

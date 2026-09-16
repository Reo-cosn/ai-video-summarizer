#!/usr/bin/env bash
# AI 视频总结 —— macOS / Linux 一键启动（对应 Windows 的 start.bat）
# 用法：./start.sh
cd "$(dirname "$0")" || exit 1

# ---- 定位 Python 3.10+ ----
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$cand"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "❌ 未找到 Python 3.10+，请先安装：https://www.python.org/downloads/"
  exit 1
fi

# ---- 首次运行：创建虚拟环境并安装依赖 ----
if [ ! -d .venv ]; then
  echo "[1/2] 首次运行：创建虚拟环境..."
  if ! "$PY" -m venv .venv; then
    echo "❌ 创建虚拟环境失败"
    exit 1
  fi
  echo "[2/2] 安装依赖（约 2-3 分钟）..."
  if ! .venv/bin/python -m pip install -r requirements.txt --default-timeout 120; then
    echo "❌ 依赖安装失败，可换国内镜像重试："
    echo "   .venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
    exit 1
  fi
fi

# ---- 启动 ----
echo ""
echo "正在启动 AI 视频总结..."
echo "  地址：http://127.0.0.1:7860（浏览器会自动打开）"
echo "  加载：Gradio 导入需 5-10 秒，请稍候..."
echo ""
export PYTHONIOENCODING=utf-8
export GRADIO_ANALYTICS_ENABLED=False

if ! .venv/bin/python app.py 2> launch_error.log; then
  echo ""
  echo "❌ app.py 运行出错，详情如下："
  cat launch_error.log
  exit 1
fi

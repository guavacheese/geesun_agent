#!/usr/bin/env bash
# Geesun Agent API 开发启动脚本
#
# 用法:
#   ./start.sh         （Git Bash 下直接执行）
#   bash start.sh      （任意 bash 环境）
#
# 等价于:
#   uv run uvicorn src.server:app --host 0.0.0.0 --port 8009 2>&1 | tee server.log

set -euo pipefail

exec uv run uvicorn src.server:app --host 0.0.0.0 --port 8009 2>&1 | tee server.log

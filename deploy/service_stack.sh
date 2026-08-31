#!/usr/bin/env bash
#
# service_stack.sh — 查看 geesun_agent Swarm stack 的服务状态。
#
# 用法：
#   ./service_stack.sh            # 列出 stack geesun 的全部服务（含副本数/镜像/端口）
#   STACK_NAME=geesun ./service_stack.sh
#
set -euo pipefail

STACK="${STACK_NAME:-geesun}"

echo "==> docker stack services $STACK"
docker stack services "$STACK"

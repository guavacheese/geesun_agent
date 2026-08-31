#!/usr/bin/env bash
#
# stop_stack.sh — 停止并移除 geesun_agent Swarm stack（不删除数据卷）。
#
# 用法：
#   ./stop_stack.sh            # 移除 stack geesun
#   STACK_NAME=geesun ./stop_stack.sh
#
# 注意：
#   - docker stack rm 只移除服务与网络，命名卷（geesun_agent_pg_data 等，
#     经 stack 前缀后名为 geesun_<volume>）不会被删除，数据保留。
#   - 若要彻底清理卷（⚠️ 数据不可恢复）：docker volume ls | grep geesun_ 后手动 docker volume rm。
#
set -euo pipefail

STACK="${STACK_NAME:-geesun}"

echo "==> docker stack rm $STACK"
docker stack rm "$STACK"

echo ""
echo "✅ 已发起移除。服务会逐步退出（graceful 秒级）。"
echo "   残留命名卷（数据保留）可用：docker volume ls | grep ${STACK}_"

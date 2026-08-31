#!/usr/bin/env bash
#
# start_stack.sh — 以 Docker Swarm stack 方式部署 geesun_agent 主栈（含可选附加 compose）。
#
# 与 flyctrl_deploy 的 start_stack.sh 同源思路，但补齐了 Swarm 必带参数与前置检查：
#   - 前置检查 docker / swarm 状态 / .env 存在
#   - 默认先 build-push.sh 打包并推送镜像到 Harbor（--no-build 可跳过，改配置时只重启）
#   - docker stack deploy 必带 --with-registry-auth（Harbor 私有仓库，否则节点拉不到镜像）
#                              --resolve-image=always（固定 tag 重新发布时强制拉新镜像）
#                              --prune（compose 中删掉的服务会被真正清理）
#   - --with=phoenix,langfuse,mcp,web 按需叠加附加 compose（默认只部署主文件）
#
# 用法：
#   ./start_stack.sh                          # 默认仅主栈（先打包再发布）
#   ./start_stack.sh --no-build               # 跳过打包，仅用已推送镜像重新部署
#   ./start_stack.sh --with=mcp,web           # 并入 MCP / 前端
#   ./start_stack.sh --with=phoenix,langfuse  # 路线 B 自托管可观测栈兜底
#   STACK_NAME=geesun ./start_stack.sh         # 显式指定 stack 名（默认 geesun）
#
set -euo pipefail

STACK="${STACK_NAME:-geesun}"
COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$COMPOSE_DIR/.env"

# ── 前置检查 ───────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || { echo "❌ 未找到 docker，请先安装 Docker Engine"; exit 1; }

SWARM_STATE="$(docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null || echo inactive)"
if [ "$SWARM_STATE" != "active" ]; then
  echo "❌ 当前节点未加入 Docker Swarm（LocalNodeState=$SWARM_STATE）"
  echo "   单节点部署请先执行：docker swarm init"
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ 缺少 $ENV_FILE"
  echo "   请先：cp $COMPOSE_DIR/.env.example $ENV_FILE  并填写全部密钥（REGISTRY_GEESUN / REGISTRY_HUB / Harbor 凭证 / GRAFANA_PASSWORD 等）"
  exit 1
fi

# ── 解析参数 ───────────────────────────────────────────────
EXTRA=()
BUILD=1
for arg in "$@"; do
  case "$arg" in
    --with=*)
      IFS=',' read -ra parts <<< "${arg#*=}"
      for p in "${parts[@]}"; do
        f="$COMPOSE_DIR/docker-compose.$p.yml"
        [ -f "$f" ] || { echo "❌ 未知附加 compose 文件：$f"; exit 1; }
        EXTRA+=(-c "$f")
      done
      ;;
    --no-build) BUILD=0 ;;
    *) echo "❌ 未知参数：$arg（仅支持 --with=<name,...> 与 --no-build）"; exit 1 ;;
  esac
done

# ── 打包 + 推送镜像（除非 --no-build）────────────────────────
if [ "$BUILD" -eq 1 ]; then
  echo "==> [1/2] 构建并推送镜像（build-push.sh）"
  bash "$COMPOSE_DIR/build-push.sh"
else
  echo "==> [1/2] 跳过打包（--no-build），使用已推送镜像"
fi

# ── 发布到 Swarm ────────────────────────────────────────────
echo "==> [2/2] docker stack deploy $STACK"
# 注：--with-registry-auth 依赖本机已 docker login 到 Harbor（凭证存入 docker config）。
#     若节点未登录，私有镜像拉取会失败；请先：docker login ${REGISTRY_HUB%/*}
docker stack deploy \
  -c "$COMPOSE_DIR/docker-compose.yml" \
  "${EXTRA[@]}" \
  --with-registry-auth \
  --resolve-image=always \
  --prune \
  "$STACK"

echo ""
echo "✅ 已发起部署。查看服务状态：./service_stack.sh"
echo "   查看某服务日志：docker service logs -f ${STACK}_geesun-agent"

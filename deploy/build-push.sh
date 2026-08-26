#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────
# 构建 geesun-agent 镜像，并把所有第三方镜像同步进 Harbor（geesun_ai 项目）。
#
# 前置条件（目标机 / 构建机一次性配置）：
#   1) Harbor 为 HTTP（172.16.220.74:8333），需把该地址加入 Docker「不安全仓库」：
#      Linux: 编辑 /etc/docker/daemon.json 增加
#        { "insecure-registries": ["172.16.220.74:8333"] }
#      然后 systemctl restart docker
#      Docker Desktop: Settings → Docker Engine → 加入上述 JSON → Apply & Restart
#   2) 当前机器需能拉取公网镜像（用于把第三方镜像同步进 Harbor）；
#      生产内网机若无法直连公网，请在能出网的机器跑本脚本后，再到内网机 docker compose pull。
#   3) 生产机需预创建 agent 工作目录（防容器重建丢数据）：
#      mkdir -p /opt/geesun/data/{agent,uploads,reports}   # 路径对应 .env 的 AGENT_DATA_ROOT
#
# 用法：
#   HARBOR_USER=xxx HARBOR_PASSWORD=yyy bash deploy/build-push.sh [agent-tag]
#   # 不传 agent-tag 时默认用 .env 里的 GEESUN_AGENT_TAG 或 1.0.0
# ───────────────────────────────────────────────────────────────────────────
set -euo pipefail

REGISTRY="${REGISTRY:-172.16.220.74:8333/geesun_ai}"
HARBOR_HOST="172.16.220.74:8333"
HARBOR_USER="${HARBOR_USER:-}"
HARBOR_PASSWORD="${HARBOR_PASSWORD:-}"

# 仓库根（geesun_agent）与构建上下文（langchain-cubesandbox 的同级目录 /d/workspace）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTEXT="$(cd "$REPO_ROOT/.." && pwd)"   # 必须含 langchain-cubesandbox

AGENT_TAG="${1:-${GEESUN_AGENT_TAG:-1.0.0}}"

echo "==> REGISTRY       = $REGISTRY"
echo "==> CONTEXT        = $CONTEXT"
echo "==> AGENT_TAG      = $AGENT_TAG"

# ── 1. Harbor 登录 ──
if [[ -z "$HARBOR_USER" || -z "$HARBOR_PASSWORD" ]]; then
  echo "==> 未设置 HARBOR_USER/HARBOR_PASSWORD，尝试使用已有 docker 登录态" >&2
else
  echo "==> docker login $HARBOR_HOST"
  echo "$HARBOR_PASSWORD" | docker login "$HARBOR_HOST" --username "$HARBOR_USER" --password-stdin
fi

# ── 2. 构建并推送 geesun-agent ──
# .dockerignore 必须位于构建上下文根（/d/workspace/.dockerignore），否则不生效。
if [[ -f "$REPO_ROOT/.dockerignore" ]]; then
  cp "$REPO_ROOT/.dockerignore" "$CONTEXT/.dockerignore"
  echo "==> 已放置 $CONTEXT/.dockerignore"
fi

AGENT_IMAGE="$REGISTRY/geesun-agent:$AGENT_TAG"
echo "==> 构建 $AGENT_IMAGE (context=$CONTEXT)"
docker build -f "$REPO_ROOT/Dockerfile" -t "$AGENT_IMAGE" "$CONTEXT"
docker push "$AGENT_IMAGE"

# ── 3. 同步第三方镜像进 Harbor ──
# 用法: sync <公网镜像> <harbor内短名:tag>
sync() {
  local src="$1" dst="$REGISTRY/$2"
  echo "==> 同步 $src -> $dst"
  if docker pull "$src" 2>/dev/null; then
    :
  elif docker image inspect "$src" >/dev/null 2>&1; then
    echo "    (公网拉取失败，使用本地已存在的 $src)"
  else
    echo "    [错误] 无法拉取且本地无 $src，跳过" >&2
    return 1
  fi
  docker tag "$src" "$dst"
  docker push "$dst"
}

# 主栈依赖
sync "pgvector/pgvector:0.8.0-pg17"            "pgvector:0.8.0-pg17"
sync "caddy:2.8-alpine"                        "caddy:2.8-alpine"
sync "grafana/loki:3.2.0"                      "loki:3.2.0"
sync "grafana/promtail:3.2.0"                  "promtail:3.2.0"
sync "grafana/grafana:11.3.0"                  "grafana:11.3.0"
# Phoenix
sync "arizephoenix/phoenix:19.1.0"            "phoenix:19.1.0"
sync "postgres:16.14"                          "postgres:16.14"
# Langfuse（pin 3.224.3，与 .env 的 LANGFUSE_TAG 一致；4.0.0 非可拉取镜像 tag）
sync "langfuse/langfuse:3.224.3"              "langfuse:3.224.3"
sync "clickhouse/clickhouse-server:25.12"      "clickhouse-server:25.12"
sync "cgr.dev/chainguard/minio"                "minio:chainguard"
sync "redis:7"                                 "redis:7"
sync "postgres:17"                             "postgres:17"

echo "==> 完成。所有镜像已推送至 $REGISTRY"
echo "    后续在目标机执行："
echo "      mkdir -p /opt/geesun/data/{agent,uploads,reports}"
echo "      cd $REPO_ROOT/deploy"
echo "      cp .env.example .env && vi .env"
echo "      docker compose -f docker-compose.yml -f docker-compose.phoenix.yml -f docker-compose.langfuse.yml pull"
echo "      docker compose -f docker-compose.yml -f docker-compose.phoenix.yml -f docker-compose.langfuse.yml up -d"

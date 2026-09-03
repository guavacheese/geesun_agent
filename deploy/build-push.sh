#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────
# 构建自有应用镜像（推送至 REGISTRY_GEESUN 所指项目，默认 geesun_ai），并把所有第三方
# 镜像同步进 REGISTRY_HUB 所指项目——生产单仓（2026-09-02 起）：REGISTRY_HUB 与
# REGISTRY_GEESUN 同值 geesun_ai，第三方镜像不再进 dockerhub proxy 项目；变量名保留便于将来拆分。
#
# 前置条件（目标机 / 构建机一次性配置）：
#   1) Harbor 为 HTTP（172.16.220.74:8333），需把该地址加入 Docker「不安全仓库」：
#      Linux: 编辑 /etc/docker/daemon.json 增加
#        { "insecure-registries": ["172.16.220.74:8333"] }
#      然后 systemctl restart docker
#      Docker Desktop: Settings → Docker Engine → 加入上述 JSON → Apply & Restart
#   2) 当前机器需能拉取公网镜像（用于把第三方镜像同步进 Harbor，默认 geesun_ai 单仓）；
#      生产内网机若无法直连公网，请在能出网的机器跑本脚本后，再到内网机 docker compose pull。
#   3) 目标 Harbor 需建好 geesun_ai 项目（单仓）；dockerhub proxy 项目可选（仅工作站手动拉官方镜像加速）。
#   4) 生产机需预创建 agent 工作目录（防容器重建丢数据）：
#      mkdir -p /opt/geesun/data/{agent,uploads,reports}   # 路径对应 .env 的 AGENT_DATA_ROOT
#
# 用法：
#   HARBOR_USER=xxx HARBOR_PASSWORD=yyy bash deploy/build-push.sh [agent-tag]
#   # 不传 agent-tag 时默认用 .env 里的 GEESUN_AGENT_TAG 或 1.0.0
#   # 也可用环境变量覆盖仓库：REGISTRY_GEESUN=... REGISTRY_HUB=...
# ───────────────────────────────────────────────────────────────────────────
set -euo pipefail

# 从 deploy/.env 载入部署期变量（REGISTRY_* / *_TAG 等），使构建与推送以 .env 为准。
# 仅当文件存在时载入；构建机若尚未生成 .env，则回落到下方内置默认值，不报错。
_DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$_DEPLOY_DIR/.env" ]]; then
  set -a
  . "$_DEPLOY_DIR/.env"
  set +a
  echo "==> 已从 $_DEPLOY_DIR/.env 载入部署变量"
fi

REGISTRY_GEESUN="${REGISTRY_GEESUN:-172.16.220.74:8333/geesun_ai}"
REGISTRY_HUB="${REGISTRY_HUB:-172.16.220.74:8333/geesun_ai}"   # 单仓默认与 GEESUN 同值；拆分时改回 dockerhub
HARBOR_HOST="172.16.220.74:8333"
HARBOR_USER="${HARBOR_USER:-}"
HARBOR_PASSWORD="${HARBOR_PASSWORD:-}"

# 仓库根（geesun_agent）与构建上下文（langchain-cubesandbox 的同级目录 /d/workspace）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTEXT="$(cd "$REPO_ROOT/.." && pwd)"   # 必须含 langchain-cubesandbox

AGENT_TAG="${1:-${GEESUN_AGENT_TAG:-1.0.0}}"
# 第三方镜像 tag（与 .env.example 对应；可被环境变量覆盖）
ALLOY_TAG="${ALLOY_TAG:-v1.19.2}"
PROMETHEUS_TAG="${PROMETHEUS_TAG:-3.0}"

echo "==> REGISTRY_GEESUN = $REGISTRY_GEESUN"
echo "==> REGISTRY_HUB     = $REGISTRY_HUB"
echo "==> CONTEXT          = $CONTEXT"
echo "==> AGENT_TAG        = $AGENT_TAG"

# ── 1. Harbor 登录 ──
if [[ -z "$HARBOR_USER" || -z "$HARBOR_PASSWORD" ]]; then
  echo "==> 未设置 HARBOR_USER/HARBOR_PASSWORD，尝试使用已有 docker 登录态" >&2
else
  echo "==> docker login $HARBOR_HOST"
  echo "$HARBOR_PASSWORD" | docker login "$HARBOR_HOST" --username "$HARBOR_USER" --password-stdin
fi

# ── 2. 构建并推送 geesun-agent（→ geesun_ai 项目）──
# .dockerignore 必须位于构建上下文根（/d/workspace/.dockerignore），否则不生效。
if [[ -f "$REPO_ROOT/.dockerignore" ]]; then
  cp "$REPO_ROOT/.dockerignore" "$CONTEXT/.dockerignore"
  echo "==> 已放置 $CONTEXT/.dockerignore"
fi

AGENT_IMAGE="$REGISTRY_GEESUN/geesun-agent:$AGENT_TAG"
echo "==> 构建 $AGENT_IMAGE (context=$CONTEXT)"
docker build -f "$REPO_ROOT/Dockerfile" -t "$AGENT_IMAGE" "$CONTEXT"
docker push "$AGENT_IMAGE"

# ── 2.5 构建并推送自有应用：geesun-mcp-server（→ geesun_ai 项目）──
# 命名说明：本函数是「构建自家代码镜像 + push 进 Harbor geesun_ai 项目」（build + push），
# 与下方 sync() 的「同步第三方镜像」（公网 pull → tag → push 进 REGISTRY_HUB 所指项目，单仓=geesun_ai）语义不同；
# 旧名 sync_mcp 沿用 sync_ 前缀易误导，现按行为命名为 build_push_mcp。
# 构建上下文为 geesun_mcp_server 仓库根（与 geesun_agent 同级 /d/workspace/geesun_mcp_server）。
# 该仓库自带 Dockerfile（非 root UID 1001 mcpuser，与 agent 同 UID，便于共享挂载目录属主）。
build_push_mcp() {
  local mcp_repo="$REPO_ROOT/../geesun_mcp_server"
  if [[ ! -d "$mcp_repo" ]]; then
    echo "    [警告] 未找到 $mcp_repo，跳过 mcp 镜像构建（请确认 geesun_mcp_server 与 geesun_agent 同级）" >&2
    return 0
  fi
  local MCP_TAG="${MCP_TAG:-1.0.0}"
  local MCP_IMAGE="$REGISTRY_GEESUN/geesun-mcp-server:$MCP_TAG"
  echo "==> 构建 $MCP_IMAGE (context=$mcp_repo)"
  docker build -f "$mcp_repo/Dockerfile" -t "$MCP_IMAGE" "$mcp_repo"
  docker push "$MCP_IMAGE"
}
# 调用必须在定义之后（Bash 函数先定义后调用；旧版误把调用放在定义前导致 command not found）
build_push_mcp

# ── 2.6 构建并推送自有应用：geesun-agent-web（Next.js 前端，→ geesun_ai 项目）──
# 命名说明：同 2.5——「构建自有代码镜像 + push 进 geesun_ai 项目」，旧名 sync_web 易与
# sync()（同步第三方镜像）混淆，故命名为 build_push_web。
# NEXT_PUBLIC_API_BASE 是 build-time 变量（Next.js 公共变量构建时内联进浏览器产物），
# 默认生产 http://10.10.10.67/（Caddy :80 同域，/api/* 由 Caddy 转发后端）；换环境重构建。
build_push_web() {
  local web_repo="$REPO_ROOT/../geesun_agent_web"
  if [[ ! -d "$web_repo" ]]; then
    echo "    [警告] 未找到 $web_repo，跳过 web 镜像构建（请确认 geesun_agent_web 与 geesun_agent 同级）" >&2
    return 0
  fi
  local WEB_TAG="${WEB_TAG:-1.0.0}"
  local API_BASE="${NEXT_PUBLIC_API_BASE:-http://10.10.10.67}"
  local WEB_IMAGE="$REGISTRY_GEESUN/geesun-agent-web:$WEB_TAG"
  echo "==> 构建 $WEB_IMAGE (context=$web_repo, NEXT_PUBLIC_API_BASE=$API_BASE)"
  docker build \
    --build-arg "NEXT_PUBLIC_API_BASE=$API_BASE" \
    -f "$web_repo/Dockerfile" -t "$WEB_IMAGE" "$web_repo"
  docker push "$WEB_IMAGE"
}
build_push_web

# ── 3. 同步第三方镜像进 Harbor（REGISTRY_HUB 所指项目，生产单仓=geesun_ai）──
# 用法: sync <公网镜像> <harbor内短名:tag>
# 注意：sync() 是「同步第三方镜像」（公网 pull → tag → push 进 REGISTRY_HUB 所指项目），
# 与上面 build_push_mcp / build_push_web（构建自有代码镜像推 geesun_ai 项目）语义不同。
sync() {
  local src="$1" dst="$REGISTRY_HUB/$2"
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

# 主栈依赖（→ REGISTRY_HUB=geesun_ai 单仓）
sync "pgvector/pgvector:0.8.0-pg17"            "pgvector:0.8.0-pg17"
sync "caddy:2.8-alpine"                        "caddy:2.8-alpine"
sync "grafana/loki:3.2.0"                      "loki:3.2.0"
sync "grafana/alloy:${ALLOY_TAG:-v1.19.2}"                  "alloy:${ALLOY_TAG:-v1.19.2}"
sync "grafana/grafana:11.3.0"                  "grafana:11.3.0"
# Alloy（统一采集，替代 Promtail；ALLOY_TAG 与 .env.example 一致）
sync "prometheus:${PROMETHEUS_TAG:-3.0}"        "prometheus:${PROMETHEUS_TAG:-3.0}"
# Phoenix（→ REGISTRY_HUB=geesun_ai 单仓）
sync "arizephoenix/phoenix:19.1.0"            "phoenix:19.1.0"
sync "postgres:16.14"                          "postgres:16.14"
# Langfuse（pin 3.224.3，与 .env 的 LANGFUSE_TAG 一致；4.0.0 非可拉取镜像 tag）
sync "langfuse/langfuse:3.224.3"              "langfuse:3.224.3"
sync "clickhouse/clickhouse-server:25.12"      "clickhouse-server:25.12"
sync "cgr.dev/chainguard/minio"                "minio:chainguard"
sync "redis:7"                                 "redis:7"
sync "postgres:17"                             "postgres:17"

echo "==> 完成。geesun-agent / geesun-mcp-server / geesun-agent-web 已推送至 $REGISTRY_GEESUN；第三方镜像已推送至 $REGISTRY_HUB"
echo "    后续在目标机（镜像已在 Harbor）执行："
echo "      mkdir -p /opt/geesun/data/{agent,uploads,reports}   # 路径对应 .env 的 AGENT_DATA_ROOT"
echo "      cd $REPO_ROOT/deploy"
echo "      cp .env.example .env && vi .env   # 填全部密钥（REGISTRY_GEESUN / REGISTRY_HUB / Harbor 凭证 / 各 *PASSWORD）"
echo "      docker login 172.16.220.74:8333   # 私有仓库拉取镜像必需（--with-registry-auth 依赖本机登录态）"
echo "      ./setup-cube-dns.sh               # 可选但推荐：*.cube.app DNS（dnsmasq 直答，见 README Deployment §0）"
echo "      ./start_stack.sh --no-build --with=mcp,web   # 用已推送镜像发布到 Swarm（--no-build 跳过打包）"
echo "      ./service_stack.sh               # 校验服务状态（docker stack services geesun）"
echo "    （构建机若已在本目录，可直接 ./start_stack.sh --with=mcp,web，它会先 build-push 再发布）"
echo "    停止 / 升级：./stop_stack.sh  或  改完 .env 后 ./start_stack.sh --no-build --with=mcp,web"

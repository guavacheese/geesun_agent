#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────
# 生产机一次性主机准备脚本（在 docker compose up 之前运行一次）。
#
# 作用：预创建 geesun-agent 的数据目录与备份目录，并把属主改成容器运行 UID
#       （Dockerfile 中用 useradd --uid 1001 appuser 创建非 root 用户），
#       避免 Docker 在挂载点不存在时自动以 root 建目录 → 容器（UID 1001）无写权限。
#
# 用法：
#   bash deploy/init-host.sh
#   AGENT_DATA_ROOT=/srv/geesun/data bash deploy/init-host.sh   # 自定义路径
# ───────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 若同级 .env 存在，读取 AGENT_DATA_ROOT / BACKUP_ROOT（与 compose 保持一致）
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a; . "$SCRIPT_DIR/.env"; set +a
fi

DATA_ROOT="${AGENT_DATA_ROOT:-/opt/geesun/data}"
BACKUP_ROOT="${BACKUP_ROOT:-/opt/geesun/backups}"
APP_UID=1001
APP_GID=1001

echo "==> 数据目录: $DATA_ROOT"
mkdir -p "$DATA_ROOT"/agent "$DATA_ROOT"/uploads "$DATA_ROOT"/reports
# skills 三层目录：__system__ 预装 / __agent__ agent 自创 / __user_*__ 用户上传（上传 API
# 按需 makedirs，无需预建）。compose 把 $DATA_ROOT/skills 挂为容器 /data/agent/skills，是
# backend 路由 root_dir，缺失即 path_not_found；必须在此预建并随下方 chown -R 归 1001。
# 2026-10-08 实测：root 手工补建的 skills/（root:root 755）致容器 appuser 写入全部
# EACCES——agent 自创 skill 与新用户 skill 上传同时失效（session a4cc5127 事故）。
mkdir -p "$DATA_ROOT"/skills/__system__ "$DATA_ROOT"/skills/__agent__
echo "==> 备份目录: $BACKUP_ROOT"
mkdir -p "$BACKUP_ROOT"

echo "==> 设置属主为容器 UID/GID $APP_UID:$APP_GID"
chown -R "$APP_UID:$APP_GID" "$DATA_ROOT" "$BACKUP_ROOT"
chmod 0755 "$DATA_ROOT" "$BACKUP_ROOT"
chmod 0755 "$DATA_ROOT"/agent "$DATA_ROOT"/uploads "$DATA_ROOT"/reports "$DATA_ROOT"/skills

# CubeSandbox CA 检查（agent/mcp 容器挂载源，相对 deploy/ 上级 certs/ 目录）
CA_FILE="$SCRIPT_DIR/../certs/cube-root-ca.crt"
if [ -f "$CA_FILE" ]; then
  echo "==> CubeSandbox CA: $CA_FILE 存在（容器将挂载并注入 REQUESTS_CA_BUNDLE/SSL_CERT_FILE）"
else
  echo "    [警告] 未找到 $CA_FILE —— 请把 geesun_agent/certs/ 拷到 deploy 上级目录，"
  echo "           否则 sandbox egress TLS 会因证书不受信失败（无 MITM 环境可忽略）"
fi

echo "完成。可继续："
echo "  sudo bash deploy/setup-cube-dns.sh    # 可选但推荐：容器内解析 *.cube.app（见 README Deployment §0）"
echo "  docker compose -f docker-compose.yml -f docker-compose.mcp.yml -f docker-compose.web.yml pull"
echo "  docker compose -f docker-compose.yml -f docker-compose.mcp.yml -f docker-compose.web.yml up -d"
echo "(若主机无 UID 1001 账户属正常；chown 按数字 UID 生效即可)"

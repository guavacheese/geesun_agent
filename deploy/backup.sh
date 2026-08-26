#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────
# 每日备份：agent_mem / phoenix / langfuse(postgres) 的 pg_dump + langfuse MinIO 桶镜像。
#
# 用法：
#   bash deploy/backup.sh                      # 默认保留 7 天
#   RETENTION_DAYS=14 bash deploy/backup.sh    # 保留 14 天
#   BACKUP_ROOT=/srv/geesun/backups bash backup.sh
#
# cron 示例（每天 03:07）：
#   7 3 * * *  cd /opt/geesun/geesun_agent/deploy && BACKUP_ROOT=/opt/geesun/backups bash backup.sh >> /opt/geesun/backups/cron.log 2>&1
#
# 前置：服务已 docker compose up 运行中；本脚本从同级 .env 读取 DB 密码 / MinIO 凭据。
# ───────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BACKUP_ROOT="${BACKUP_ROOT:-/opt/geesun/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
# 第三方镜像（minio）来自 Harbor dockerhub 项目
REGISTRY_HUB="${REGISTRY_HUB:-172.16.220.74:8333/dockerhub}"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.phoenix.yml -f docker-compose.langfuse.yml)
COMPOSE=(docker compose "${COMPOSE_FILES[@]}")

# 读取 .env（含 DB 密码 / MinIO 凭据）
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a; . "$SCRIPT_DIR/.env"; set +a
fi

DATE="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$DATE"
mkdir -p "$DEST"

echo "[$(date)] 备份开始 -> $DEST"

# --- PostgreSQL 逻辑备份 ---
dump_pg() {
  local svc="$1" user="$2" db="$3" pass="$4" out="$5"
  echo "  -> pg_dump $svc/$db"
  PGPASSWORD="$pass" "${COMPOSE[@]}" exec -T -e PGPASSWORD="$pass" "$svc" \
    pg_dump -U "$user" -d "$db" --no-owner --no-privileges 2>/dev/null | gzip > "$out"
  if [ ! -s "$out" ]; then
    echo "  [WARN] $out 为空，请检查服务名/凭据" >&2
  fi
}

dump_pg agent-postgres "${AGENT_PG_USER:-geesun}"    "${AGENT_PG_DB:-agent_mem}"  "${AGENT_PG_PASSWORD}"    "$DEST/agent_mem.sql.gz"
dump_pg phoenix-db     "${PHOENIX_DB_USER:-phoenix}" "${PHOENIX_DB_NAME:-phoenix}" "${PHOENIX_DB_PASSWORD}" "$DEST/phoenix.sql.gz"
dump_pg postgres       "${POSTGRES_USER:-postgres}"  "${POSTGRES_DB:-postgres}"   "${POSTGRES_PASSWORD}"   "$DEST/langfuse.sql.gz"

# --- MinIO（langfuse 桶）镜像：用 minio 镜像内自带的 mc ---
BUCKET="${LANGFUSE_S3_EVENT_UPLOAD_BUCKET:-langfuse}"
echo "  -> minio mirror (bucket: $BUCKET)"
mkdir -p "$DEST/minio"
if docker run --rm --network appnet \
    -v "$DEST/minio":/backup \
    "$REGISTRY_HUB/minio:chainguard" \
    sh -c "mc alias set m http://minio:9000 '${MINIO_ROOT_USER:-minio}' '${MINIO_ROOT_PASSWORD}' && mc mb -p m/$BUCKET >/dev/null 2>&1; mc mirror m/$BUCKET /backup" 2>/dev/null; then
  echo "  minio 备份完成 -> $DEST/minio"
else
  echo "  [WARN] minio 备份失败，请检查 minio 容器与 MINIO_ROOT_PASSWORD" >&2
fi

# --- 清理旧备份 ---
echo "[$(date)] 清理 $RETENTION_DAYS 天前的备份"
find "$BACKUP_ROOT" -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9]{8}-[0-9]{6}' -mtime "+$RETENTION_DAYS" -exec rm -rf {} +
echo "[$(date)] 完成。最新备份: $DEST"

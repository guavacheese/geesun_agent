#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────
# combined-ca.pem 生成脚本（生产机/开发机一次性执行，无需 root）。
#
# 背景：为什么必须用 combined-ca.pem，不能直接挂 rootCA.pem 单证书？
#   rootCA.pem 只是 CubeSandbox egress MITM 的 mkcert 单证书（约 1.7KB）。
#   容器内 SSL_CERT_FILE / REQUESTS_CA_BUNDLE 指向它后，Python/pip/uv 的
#   默认信任库被【整体替换】为这一个文件 → 系统根全部被排除：
#     ✓ 信任内网 mkcert 签发的 cube-egress 拦截证书（sandbox 出网可走）
#     ✗ 不信任外网 pypi.org（GlobalSign 签发）→ agent 启动 uv sync 报
#       invalid peer certificate: UnknownIssuer → 容器启动即崩
#       （2026-09-01 实测反证：仅挂 rootCA.pem 后 agent 起不来）。
#   因此必须合并：mkcert CA（信任内网）+ 系统根 bundle（信任外网）→
#   combined-ca.pem（约 148 证书 / 227KB），两者缺一不可。
#
# 本脚本：
#   1) 定位输入：仓库/本机已有的 mkcert 单证书（rootCA.pem | cube-root-ca.pem
#      | cube-ca.pem，按存在顺序取第一个；也可 CA_INPUT=/path/to/ca.pem 覆盖）
#   2) 探测系统根 bundle：
#        Rocky/OpenCloudOS:  /etc/pki/tls/certs/ca-bundle.crt
#        Debian/Ubuntu:      /etc/ssl/certs/ca-certificates.crt
#        （可用 CA_SYS_ROOT=/path/to/ca-bundle.crt 强制覆盖）
#   3) python3 合并 + 按证书块 sha256 去重 → combined-ca.pem
#      （默认输出 certs/combined-ca.pem，即 .env 的 CA_MOUNT_SRC 默认指向；
#        也可 CA_OUTPUT=/path/to/combined-ca.pem 覆盖）
#   4) 打印证书数 / 大小 / md5，供与容器内文件比对。
#
# 用法：
#   bash deploy/setup-combined-ca.sh                        # 默认输出 certs/combined-ca.pem
#   bash deploy/setup-combined-ca.sh --force                # 覆盖已存在的输出
#   CA_INPUT=/opt/geesun/certs/rootCA.pem bash deploy/setup-combined-ca.sh
#   CA_OUTPUT=/opt/geesun/certs/combined-ca.pem bash deploy/setup-combined-ca.sh
# ───────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_OUTPUT="${SCRIPT_DIR}/../certs/combined-ca.pem"

CA_INPUT="${CA_INPUT:-}"
CA_OUTPUT="${CA_OUTPUT:-$DEFAULT_OUTPUT}"
FORCE=0
# 解析位置参数：支持 --force 与 KEY=VAL（等价于环境变量前缀写法，更直观）
for arg in "$@"; do
  case "$arg" in
    *=*) export "$arg" ;;
    --force) FORCE=1 ;;
    *) echo "[错误] 未知参数: $arg（仅支持 --force 与 KEY=VAL，如 CA_INPUT=/path/rootCA.pem）" >&2; exit 1 ;;
  esac
done

# 1) 定位输入单证书
if [[ -z "$CA_INPUT" ]]; then
  for cand in "$SCRIPT_DIR/../certs/rootCA.pem" "$SCRIPT_DIR/../certs/cube-root-ca.pem" "$SCRIPT_DIR/../certs/cube-ca.pem"; do
    if [[ -f "$cand" ]]; then CA_INPUT="$cand"; break; fi
  done
fi
if [[ -z "$CA_INPUT" ]] || [[ ! -f "$CA_INPUT" ]]; then
  echo "[错误] 未找到 mkcert 单证书输入。请用 CA_INPUT=/path/to/rootCA.pem 指定。" >&2
  exit 1
fi
echo "==> 1/4 输入单证书: $CA_INPUT（$(grep -c 'BEGIN CERTIFICATE' "$CA_INPUT") 个证书）"

# 2) 探测系统根 bundle
SYS_CA="${CA_SYS_ROOT:-}"
if [[ -z "$SYS_CA" ]]; then
  for cand in /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/certs/ca-certificates.crt; do
    if [[ -f "$cand" ]]; then SYS_CA="$cand"; break; fi
  done
fi
if [[ -z "$SYS_CA" ]]; then
  echo "[错误] 未找到系统根 bundle（/etc/pki/tls/certs/ca-bundle.crt 或 /etc/ssl/certs/ca-certificates.crt）" >&2
  exit 1
fi
echo "==> 2/4 系统根 bundle: $SYS_CA（$(grep -c 'BEGIN CERTIFICATE' "$SYS_CA") 个证书）"

# 3) 合并 + 去重（python3 按证书块 sha256 去重，mkcert CA 若已在系统根中则自动跳过）
if [[ -f "$CA_OUTPUT" ]] && [[ $FORCE -ne 1 ]]; then
  echo "[错误] 输出已存在: $CA_OUTPUT（如需覆盖，加 --force 或先删除旧文件）" >&2
  exit 1
fi
mkdir -p "$(dirname "$CA_OUTPUT")"
echo "==> 3/4 合并去重 → $CA_OUTPUT"
python3 - "$CA_INPUT" "$SYS_CA" "$CA_OUTPUT.tmp" <<'PYEOF'
import hashlib, sys

def split_certs(path):
    """按 BEGIN/END CERTIFICATE 边界切成独立证书块（字节级，不按行）。"""
    with open(path, "rb") as f:
        data = f.read()
    marker = b"-----BEGIN CERTIFICATE-----"
    blocks = []
    for chunk in data.split(marker)[1:]:
        end = chunk.find(b"-----END CERTIFICATE-----")
        if end == -1:
            continue
        blocks.append(marker + chunk[: end + len(b"-----END CERTIFICATE-----")])
    return blocks

seen, merged = set(), []
for p in sys.argv[1:3]:
    for blk in split_certs(p):
        h = hashlib.sha256(blk).hexdigest()
        if h not in seen:
            seen.add(h)
            merged.append(blk)

with open(sys.argv[3], "wb") as f:
    for blk in merged:
        f.write(blk + b"\n\n")
print(f"    合并后证书数: {len(merged)}")
PYEOF
mv "$CA_OUTPUT.tmp" "$CA_OUTPUT"

# 4) 校验输出
echo "==> 4/4 校验输出"
echo "    文件: $CA_OUTPUT"
echo "    证书数: $(grep -c 'BEGIN CERTIFICATE' "$CA_OUTPUT")"
echo "    大小: $(du -h "$CA_OUTPUT" | cut -f1)  md5: $(md5sum "$CA_OUTPUT" | cut -d' ' -f1)"
echo
echo "完成。compose 的 CA_MOUNT_SRC 默认指向 certs/combined-ca.pem；"
echo "生产机请确认 deploy 上级 certs/ 存在该文件（或把 CA_MOUNT_SRC 指向实际路径）。"
echo "验证容器内：docker exec <agent容器> sh -c 'md5sum /etc/ssl/certs/combined-ca.pem' 应与上方 md5 一致。"

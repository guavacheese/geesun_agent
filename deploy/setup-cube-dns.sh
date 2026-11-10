#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────────────
# CubeSandbox DNS + docker0 网段配置（生产机执行，需 root；可重复执行）。
#
# 作用：让 docker 容器内能解析 sandbox 域名（形如 <port>-<sandbox_id>.cube.app → 172.16.66.13），
#       同时保证 docker0 落在网络规划指定的网段上。
#   docker 内嵌 DNS(127.0.0.11) 只解 appnet 服务名；外部域名转发给 daemon.json 的
#   "dns" 上游。本脚本按顺序做七件事：
#     1) 检测/安装 dnsmasq
#     2) 取宿主机上游 DNS
#     3) 写入 /etc/dnsmasq.d/cube.conf：
#          address=/cube.app/172.16.66.13       # *.cube.app 本地直答（address= 模式）
#          server=<resolv.conf 上游>            # 非 cube.app 域名正常外网解析
#          listen-address=172.30.10.1,127.0.0.1 # docker0 网关，供容器经 daemon dns 访问
#     4) 合并写 /etc/docker/daemon.json 的三个键：dns / bip / default-address-pools
#     5) 【仅必要时】重启 dockerd，让 docker0 换到 bip 段，并轮询等地址就绪
#     6) 确认监听的地址在本机存在后，重启 dnsmasq
#     7) 验证容器内解析
#
# ⚠️ 三个键缺一不可（2026-11-10 复盘修正）：
#   - "bip"                    : docker0 自身的网段。只写 dns 不写 bip ⇒ 新机器 docker0
#                                仍回默认 172.17.0.1/16，原始故障原样复发。
#   - "default-address-pools"  : docker 新建 user-defined bridge 时从哪个池切。不设的话
#                                内置池仍从 172.17.0.0/16 起分配，会把那条 /16 直连路由
#                                带回来（最长前缀直连路由优先于默认网关 ⇒ 黑洞复发）。
#                                注意：只对**新建**网络生效，既有网络不受影响。
#   - "dns"                    : 容器外部域名上游，必须等于 dnsmasq 的 listen 地址。
#
# ⚠️ 顺序铁律：改 daemon.json → 重启 dockerd 建好新 docker0 地址 → 才能重启 dnsmasq。
#   反过来（先启 dnsmasq）会因地址尚不存在导致 bind 失败、dnsmasq 直接退出，而
#   dnsmasq.service 没有 Restart= 策略（Type=forking），systemd 不会拉起 ⇒ 全网容器
#   DNS 中断且不自愈。67 上实测报错：
#     dnsmasq: failed to create listening socket for <addr>: Cannot assign requested address
#
# ⚠️ docker0 网段 2026-11-10 由 172.17.0.0/16 改为 172.30.10.0/24（IT 指定 172.30 段）。
#   网关刻意取 172.30.10.1 而不是 172.30.0.1：实测 .0.1 已被 10.10.10.66（开 :3000）与
#   10.10.10.71（开 :5432）两台内网设备 ARP 应答（linux 默认 arp_ignore=0，本机地址在
#   任何网卡上都应答），且这两台**同时也应答 172.17.0.1**；172.30 段 19 个候选网关
#   地址实测仅 .0.1 被占。用 /24 而非 /16，避免 docker0 独占整段重演同一问题。
#   遗留：docker_gwbridge 仍是 172.21.0.0/16，bip 管不到它，重建需 swarm leave。
#
# ⚠️ 必须用 address= 直答模式，不要用 server=/cube.app/<IP> 转发模式：
#   server= 会把查询转发给 <IP>:53 的 DNS 服务；172.16.66.13 上并无 DNS 服务
#   （实测 53 端口不通），转发必然 NXDOMAIN/超时。address= 由 dnsmasq 本地直答，
#   与 dev 机 WSL 的 /etc/dnsmasq.d/cube.conf 写法一致（2026-09-02 实测修正）。
#
# 为什么不用 compose 的 dns: 字段：它会顶掉 127.0.0.11，导致 agent/mcp 无法用
#   appnet 服务名互访；daemon 级 dns 保留内嵌 DNS，仅转发外部域名。
#
# 用法：
#   sudo bash deploy/setup-cube-dns.sh
#   CUBE_SANDBOX_IP=172.16.66.13 bash deploy/setup-cube-dns.sh   # 覆盖 sandbox IP
#   CUBE_DOMAIN=cube.app bash deploy/setup-cube-dns.sh           # 覆盖域名后缀
#   DOCKER_BIP=198.18.0.1/24 DOCKER_POOL_BASE=198.19.0.0/16 \
#     bash deploy/setup-cube-dns.sh                              # 换网段（同步派生 listen）
#   DRY_RUN=1 bash deploy/setup-cube-dns.sh                      # 只打印将要做什么，不落盘
# ───────────────────────────────────────────────────────────────────────────
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "[错误] 需要 root：sudo bash $0" >&2
  exit 1
fi

CUBE_SANDBOX_IP="${CUBE_SANDBOX_IP:-172.16.66.13}"
CUBE_DOMAIN="${CUBE_DOMAIN:-cube.app}"
# docker0 自身网段（含 .1 网关地址与掩码）
DOCKER_BIP="${DOCKER_BIP:-172.30.10.1/24}"
# docker 新建 bridge 网络的地址池。必须与 DOCKER_BIP 不重叠（脚本会自检）
DOCKER_POOL_BASE="${DOCKER_POOL_BASE:-172.30.16.0/20}"
DOCKER_POOL_SIZE="${DOCKER_POOL_SIZE:-24}"
# dnsmasq 监听地址默认从 DOCKER_BIP 派生 —— 保证两者永不脱钩（只改 bip 漏改这里是历史 bug）
DNSMASQ_LISTEN="${DNSMASQ_LISTEN:-${DOCKER_BIP%%/*}}"
DRY_RUN="${DRY_RUN:-0}"

DAEMON_JSON=/etc/docker/daemon.json
CUBE_CONF=/etc/dnsmasq.d/cube.conf
MARK=/tmp/.cube-dns-daemon-changed
PREVIEW=""
# dry-run 的预演副本若因中途失败残留，退出时清理（正常流程会先删，重复 rm -f 无害）
trap 'if [[ -n "$PREVIEW" ]]; then rm -f "$PREVIEW"; fi' EXIT

say() { echo "$@"; }
run() { if [[ "$DRY_RUN" == "1" ]]; then echo "    [dry-run] $*"; else eval "$@"; fi; }

echo "==> 0/7 参数确认"
echo "    sandbox 域名 : *.$CUBE_DOMAIN → $CUBE_SANDBOX_IP"
echo "    docker0 bip  : $DOCKER_BIP"
echo "    地址池       : $DOCKER_POOL_BASE (size $DOCKER_POOL_SIZE)"
echo "    dnsmasq 监听 : $DNSMASQ_LISTEN,127.0.0.1"
if [[ "$DRY_RUN" == "1" ]]; then echo "    *** DRY_RUN=1：不写文件、不重启任何服务 ***"; fi

echo "==> 1/7 检测 dnsmasq"
if ! command -v dnsmasq >/dev/null 2>&1; then
  echo "    dnsmasq 未安装，尝试安装..."
  if command -v dnf >/dev/null 2>&1; then run "dnf install -y dnsmasq"
  elif command -v yum >/dev/null 2>&1; then run "yum install -y dnsmasq"
  elif command -v apt-get >/dev/null 2>&1; then run "apt-get update && apt-get install -y dnsmasq"
  else echo "[错误] 无法自动安装 dnsmasq，请手动安装后重跑" >&2; exit 1; fi
fi
# 只登记开机自启，不在这里 start —— 启动时机在步骤 6（地址就绪之后）
run "systemctl enable dnsmasq >/dev/null 2>&1 || true"
echo "    已登记开机自启（启动时机见步骤 6）"

echo "==> 2/7 取宿主机上游 DNS（保持外网解析）"
UPSTREAMS=()
while read -r _ns; do
  if [[ -n "$_ns" ]]; then UPSTREAMS+=("$_ns"); fi
done < <(grep -E '^\s*nameserver\s+' /etc/resolv.conf 2>/dev/null | awk '{print $2}' | grep -v '^127\.' | grep -v '^::1' || true)
if [[ ${#UPSTREAMS[@]} -eq 0 ]]; then
  echo "    resolv.conf 无可用上游，fallback 8.8.8.8 / 1.1.1.1"
  UPSTREAMS=(8.8.8.8 1.1.1.1)
fi
echo "    上游: ${UPSTREAMS[*]}"

echo "==> 3/7 写入 $CUBE_CONF"
if [[ -f $CUBE_CONF ]]; then
  # 线上该文件常被手工改过（如 address= 指向与默认不同的 sandbox IP），先备份再覆盖
  run "cp -a $CUBE_CONF ${CUBE_CONF}.bak.$(date +%Y%m%d%H%M%S)"
fi
if [[ "$DRY_RUN" == "1" ]]; then
  echo "    [dry-run] 将写入：address=/$CUBE_DOMAIN/$CUBE_SANDBOX_IP; server=<${#UPSTREAMS[@]} 个>; listen-address=$DNSMASQ_LISTEN,127.0.0.1; bind-interfaces"
else
  mkdir -p /etc/dnsmasq.d
  {
    echo "# generated by deploy/setup-cube-dns.sh — CubeSandbox 域名直答（address= 模式）"
    echo "address=/$CUBE_DOMAIN/$CUBE_SANDBOX_IP"
    echo "# 上游 DNS（保持外网解析）"
    for ns in "${UPSTREAMS[@]}"; do echo "server=$ns"; done
    echo "listen-address=$DNSMASQ_LISTEN,127.0.0.1"
    echo "bind-interfaces"
  } > "$CUBE_CONF"
  echo "    address=/$CUBE_DOMAIN/$CUBE_SANDBOX_IP"
  echo "    listen-address=$DNSMASQ_LISTEN,127.0.0.1"
fi

echo "==> 4/7 合并写入 $DAEMON_JSON（dns + bip + default-address-pools）"
if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 需要 python3 来合并 daemon.json，请安装后重跑" >&2
  exit 1
fi

# 网段自检放在落盘之前，且 dry-run 也执行：dockerd --validate 不校验地址池重叠，
# 重叠配置照样返回 configuration OK，只能自己拦。
python3 - "$DOCKER_BIP" "$DOCKER_POOL_BASE" <<'PYCHK'
import ipaddress, sys
bip, pool_base = sys.argv[1], sys.argv[2]
try:
    nb = ipaddress.ip_network(bip, strict=False)
    np = ipaddress.ip_network(pool_base, strict=False)
except ValueError as e:
    sys.exit(f"    [错误] 网段参数非法：{e}")
if nb.overlaps(np):
    sys.exit(f"    [错误] bip={bip}（{nb}）与 default-address-pools base={pool_base}（{np}）"
             f"重叠 —— dockerd 会接受，但运行时会争地址，必须错开")
print(f"    ✓ 网段自检通过：docker0 {nb} / 地址池 {np}（不重叠）")
PYCHK

if [[ "$DRY_RUN" == "1" ]]; then
  # dry-run 也真实预演一遍合并：对**副本**跑同一段 python 逻辑，得出真实的"是否变化"
  # 结论。否则幂等判断只能靠猜，而它决定了要不要重启 dockerd（会停机）。
  PREVIEW="${DAEMON_JSON}.dryrun.preview"
  if [[ -f $DAEMON_JSON ]]; then
    cp -a "$DAEMON_JSON" "$PREVIEW"
  else
    : > "$PREVIEW"
  fi
  echo "    [dry-run] 对副本预演合并（不改动 $DAEMON_JSON）"
  TARGET="$PREVIEW"
else
  if [[ -f $DAEMON_JSON ]]; then
    run "cp -a $DAEMON_JSON ${DAEMON_JSON}.bak.$(date +%Y%m%d%H%M%S)"
    # 只保留最近 3 份备份，避免每次执行都堆积
    ls -1t ${DAEMON_JSON}.bak.* 2>/dev/null | tail -n +4 | xargs -r rm -f || true
  fi
  TARGET="$DAEMON_JSON"
fi
python3 - "$TARGET" "$DNSMASQ_LISTEN" "$DOCKER_BIP" "$DOCKER_POOL_BASE" "$DOCKER_POOL_SIZE" "$MARK" <<'PYEOF'
import json, sys, os

path, dns, bip, pool_base, pool_size, mark = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]), sys.argv[6]

# 网段合法性/重叠已在步骤 4 开头单独校验过，这里只负责合并与落盘
try:
    with open(path, encoding="utf-8") as f:
        old = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    old = None

cfg = {} if old is None else dict(old)
cfg["dns"] = [dns]
cfg["bip"] = bip
cfg["default-address-pools"] = [{"base": pool_base, "size": pool_size}]

for k, v in (("dns", f"[{dns}]"), ("bip", bip),
             ("default-address-pools", f"[{{base:{pool_base}, size:{pool_size}}}]")):
    before = (old or {}).get(k, "<未设置>")
    after = cfg[k]
    flag = "" if before == after else "  ← 变更"
    print(f"    {k:24s} {before!s:34s} → {after}{flag}")

changed = (old != cfg)
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
tmp = path + ".tmp.cubedns"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, path)
with open(mark, "w") as f:
    f.write("1" if changed else "0")
print(f"    已写入（内容{'有' if changed else '无'}变化），原有其它键保留")
PYEOF

if [[ "$DRY_RUN" == "1" ]]; then
  rm -f "$PREVIEW"
  echo "    [dry-run] 预演副本已删除，$DAEMON_JSON 本体未改动"
fi

echo "==> 5/7 对齐 docker0 网段（必要时才重启 dockerd）"
CUR_BIP="$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | head -1 || true)"
DAEMON_CHANGED="$(cat "$MARK" 2>/dev/null || echo 1)"
NEED_RESTART=0
# 注意：这两条必须写成 if，不能写 [[ ... ]] && NEED_RESTART=1 —— 条件为假时该语句
# 返回 1，在 set -e 下会让脚本静默退出。
if [[ "$CUR_BIP" != "$DOCKER_BIP" ]]; then NEED_RESTART=1; fi
if [[ "$DAEMON_CHANGED" == "1" ]]; then NEED_RESTART=1; fi

echo "    当前 docker0: ${CUR_BIP:-<不存在>}；目标: $DOCKER_BIP；daemon.json 变化: $DAEMON_CHANGED"

if [[ "$NEED_RESTART" == "0" ]]; then
  echo "    ✓ 已是目标网段且 daemon.json 无变化 —— 跳过 dockerd 重启（零停机）"
else
  echo "    → 需要重启 dockerd（bip/default-address-pools/dns 都不在 SIGHUP 可重载列表里）"
  if command -v docker >/dev/null 2>&1 && docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null | grep -q active; then
    svc_n="$(docker service ls -q 2>/dev/null | wc -l | tr -d ' ')"
    echo "    ⚠️ 检测到本机是 swarm 节点，重启 dockerd 会让 $svc_n 个服务短暂中断"
    echo "       （live-restore 对 swarm 服务无效，无法规避；服务会自行恢复）"
  fi
  run "systemctl restart docker"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "    [dry-run] 将轮询 docker0 直到出现 $DOCKER_BIP（最多 30s）"
  else
    _ok=0
    for _i in $(seq 1 30); do
      CUR_BIP="$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | head -1 || true)"
      if [[ "$CUR_BIP" == "$DOCKER_BIP" ]]; then _ok=1; break; fi
      sleep 1
    done
    if [[ "$_ok" != "1" ]]; then
      echo "    [错误] 等 30s，docker0 仍是 '${CUR_BIP:-<不存在>}'，期望 $DOCKER_BIP" >&2
      echo "           立即排查：journalctl -u docker -n 50 --no-pager | grep -i -E 'bip|address pool'" >&2
      echo "           回滚：cp -a ${DAEMON_JSON}.bak.<时间戳> $DAEMON_JSON && systemctl restart docker" >&2
      exit 1
    fi
    echo "    ✓ docker0 已就绪：$CUR_BIP"
  fi
fi

echo "==> 6/7 重启 dnsmasq（地址已确认存在才动）"
if [[ "$DRY_RUN" != "1" ]]; then
  # 硬闸门：listen 地址不在本机任何接口上时，重启 dnsmasq 必然 bind 失败并退出，
  # 而 dnsmasq.service 没有 Restart= ⇒ 不会自愈。宁可不重启也不要把 DNS 弄挂。
  if ! ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -qx "$DNSMASQ_LISTEN"; then
    echo "    [错误] $DNSMASQ_LISTEN 不在本机任何接口上，重启 dnsmasq 会 bind 失败。" >&2
    echo "           已保留当前 dnsmasq 运行状态不动。请先解决 docker0 网段问题。" >&2
    exit 1
  fi
fi
run "systemctl restart dnsmasq"
if [[ "$DRY_RUN" != "1" ]]; then
  sleep 1
  systemctl is-active dnsmasq || {
    echo "    [错误] dnsmasq 未处于 active，查 journalctl -u dnsmasq -n 30 --no-pager" >&2
    exit 1
  }
  echo "    ✓ dnsmasq active"
  ss -lntup 2>/dev/null | grep ':53 ' | grep dnsmasq | head -3 || true
fi

echo "==> 7/7 验证"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "    [dry-run] 将用 busybox 验证容器内解析 *.$CUBE_DOMAIN"
else
  echo "    容器内解析（docker0 网关 $DNSMASQ_LISTEN）："
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker run --rm busybox nslookup "test-sandbox.$CUBE_DOMAIN" 2>&1 | head -8 || \
      echo "    （test-sandbox 可能 NXDOMAIN——只要指向 $CUBE_SANDBOX_IP 即链路通）"
  else
    echo "    （docker 不可用，跳过）"
  fi
  echo "    本机解析自检："
  if command -v dig >/dev/null 2>&1; then
    dig +short +time=2 +tries=1 "test-sandbox.$CUBE_DOMAIN" @"$DNSMASQ_LISTEN" 2>/dev/null || true
  else
    echo "    （无 dig，跳过）"
  fi
fi

echo
echo "完成。后续 docker compose up 后可用："
echo "  docker exec <agent容器> getent hosts <sandbox-id>.$CUBE_DOMAIN"
echo "  应返回 $CUBE_SANDBOX_IP"
echo
echo "注意：default-address-pools 只影响**新建**的 bridge 网络；既有网络保持原子网。"
echo "      docker_gwbridge 不受 bip 管辖（仍为 172.21.0.0/16），重建需 swarm leave。"

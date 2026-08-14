from src.core.config import settings
import os
import base64
from pathlib import Path
import logging
import json
import time
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
ca_path = os.getenv("CUBE_CA_PATH", str(BASE_DIR / "certs" / "rootCA.pem"))

# ─── 沙箱实例缓存 ────────────────────────────────────────────────
# 同 thread_id 复用同一 CubeSandbox 实例：避免每次 POST /api/v1/chat 重建对象，
# 旧对象被 GC 后触发 SDK __del__ 误杀沙箱（2026-08-14 实测：活跃沙箱被 DELETE，
# 后续 execute 全 504）。配合 langchain-cubesandbox 的 close 不销毁修复双保险。
_sandbox_cache: dict[str, object] = {}
_sandbox_cache_lock = threading.Lock()


# ─── 环境快照（设计文档 M1：环境预检注入，消除"模型现场探索环境"）───────────

# 默认探测白名单：只探测这些命令是否存在（不执行任意命令，杜绝注入面）
DEFAULT_PROBE_COMMANDS = ["rustc", "cargo", "python3", "node", "go", "gcc", "javac"]


@dataclass
class SandboxEnvSnapshot:
    """一次沙箱环境探测的结构化结果，注入 system 消息供模型直接使用。"""

    ok: bool  # 探测是否成功（失败 = 降级，不阻断任务）
    commands: dict[str, str | None] = field(
        default_factory=dict
    )  # 命令 -> 绝对路径或 None
    disk_avail_mb: int | None = None  # df -h / 可用空间（MB）
    toolchains: list[str] = field(
        default_factory=list
    )  # rustup toolchain list（如存在）
    error: str | None = None  # 探测失败原因（降级时非 None）

    def to_hint(self) -> str:
        """渲染为注入 system 消息的一段文本。"""
        parts = []
        if self.ok:
            cmd_bits = [
                f"{name}: {path or '未安装'}"
                for name, path in sorted(self.commands.items())
            ]
            parts.append("  " + " | ".join(cmd_bits))
            if self.disk_avail_mb is not None:
                warn = ""
                if self.disk_avail_mb < settings.sandbox_disk_warn_mb:
                    warn = "（偏低，编译/大文件类任务可能失败）"
                parts.append(f"  可用磁盘: {self.disk_avail_mb}MB{warn}")
            if self.toolchains:
                parts.append(f"  rustup toolchains: {', '.join(self.toolchains)}")
        else:
            parts.append(
                f"  环境探测失败: {self.error or '未知原因'}，请自行确认可用工具与磁盘空间"
            )
        return "\n".join(parts)


# 快照缓存：{thread_id: (ts, snapshot)}，TTL 由 settings.sandbox_probe_ttl_sec 控制
_env_snapshot_cache: dict[str, tuple[float, SandboxEnvSnapshot]] = {}


def _probe_commands() -> list[str]:
    """读取探测命令白名单（配置可覆盖，JSON 数组）。"""
    raw = (settings.sandbox_probe_commands or "").strip()
    if not raw:
        return list(DEFAULT_PROBE_COMMANDS)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and parsed:
            return [str(c) for c in parsed]
    except json.JSONDecodeError:
        logger.warning("sandbox_probe_commands 解析失败，使用默认白名单: %s", raw)
    return list(DEFAULT_PROBE_COMMANDS)


def probe_sandbox_env(sandbox) -> SandboxEnvSnapshot:
    """对沙箱执行一次环境探测，合并为单次 execute 降低开销。

    探测内容（全部只读，无副作用）：
      1. 白名单命令是否存在：for c in ...; do command -v $c
      2. 根分区可用空间：df -h / | tail -1（parse 第 4 列如 "120M"/"1.2G"）
      3. rustup 默认 toolchain：rustup toolchain list（存在 rustup 才跑）
    任一步骤失败不抛异常，置 ok=False 降级（环境探测失败不应阻断任务）。
    """
    snapshot = SandboxEnvSnapshot(ok=True, commands={})
    try:
        probe_list = _probe_commands()
        cmd = (
            "for c in %s; do p=$(command -v $c 2>/dev/null); "
            'if [ -n "$p" ]; then echo "CMD:$c=$p"; fi; done; '
            "echo '---'; df -h / | tail -1; echo '---'; "
            "if command -v rustup >/dev/null 2>&1; then rustup toolchain list 2>/dev/null; fi"
            % " ".join(probe_list)
        )
        resp = sandbox.execute(cmd, timeout=30)
        if resp.exit_code != 0:
            raise RuntimeError(
                f"probe 命令退出码 {resp.exit_code}: {resp.output[:200]}"
            )

        output = resp.output or ""
        section = "cmds"
        for line in output.splitlines():
            line = line.strip()
            if line == "---":
                section = "df" if section == "cmds" else "rustup"
                continue
            if section == "cmds":
                if line.startswith("CMD:"):
                    name, _, path = line[4:].partition("=")
                    if name:
                        snapshot.commands[name] = path or None
            elif section == "df" and line:
                # df -h 行格式: Filesystem Size Used Avail Use% Mounted on
                # tail -1 取数据行，第 4 列是 Avail（如 "120M"、"1.2G"、"0"）
                fields = line.split()
                if len(fields) >= 4:
                    snapshot.disk_avail_mb = _df_avail_to_mb(fields[3])
            elif section == "rustup" and line:
                snapshot.toolchains.append(line)
        return snapshot
    except Exception as e:  # noqa: BLE001
        logger.warning("probe_sandbox_env 失败（降级）: %s", e)
        return SandboxEnvSnapshot(ok=False, error=str(e)[:200])


def _df_avail_to_mb(avail: str) -> int | None:
    """把 df -h 的 Avail 列（如 120M / 1.2G / 512K）解析为 MB。解析失败返回 None。"""
    avail = avail.strip().upper()
    try:
        if avail.endswith("G"):
            return int(float(avail[:-1]) * 1024)
        if avail.endswith("M"):
            return int(float(avail[:-1]))
        if avail.endswith("K"):
            return max(1, int(float(avail[:-1]) // 1024))
        if avail.endswith("T"):
            return int(float(avail[:-1]) * 1024 * 1024)
        # 无单位：按字节处理（df 无 -h 时）
        return int(float(avail)) // (1024 * 1024)
    except (ValueError, TypeError):
        return None


def get_env_snapshot(
    sandbox, thread_id: str | None = None
) -> SandboxEnvSnapshot | None:
    """带缓存的快照入口：TTL 内同一 thread_id 复用，避免每轮 chat 重跑探测。

    返回 None 表示无沙箱（本地模式）或 thread_id 缺失，调用方跳过注入。
    """
    if sandbox is None:
        return None
    cache_key = thread_id or "default"
    now = time.time()
    cached = _env_snapshot_cache.get(cache_key)
    if cached and (now - cached[0]) < settings.sandbox_probe_ttl_sec:
        return cached[1]
    snapshot = probe_sandbox_env(sandbox)
    _env_snapshot_cache[cache_key] = (now, snapshot)
    return snapshot


def create_sandbox(thread_id: str):
    key = settings.cube_api_key
    if not key or not key.startswith("e2b_"):
        return None  # key 无效，不尝试创建，静默跳过

    # 同 thread_id 复用已缓存的沙箱实例（不重建对象，避免旧实例 GC 误杀沙箱）
    with _sandbox_cache_lock:
        cached = _sandbox_cache.get(thread_id)
        if cached is not None:
            return cached

    try:
        from langchain_cubesandbox import CubeSandbox

        sandbox = CubeSandbox.get_or_create(
            template=settings.cube_template_id,
            thread_id=thread_id,
            api_url=settings.cube_api_url,
            api_key=key,
            ssl_cert=str(ca_path),
            timeout=settings.sandbox_idle_timeout_sec,
        )

        # 沙箱内配置 pip 指向内网 devpi 源（136 出口干净，绕开 AC 认证网关/egress MITM）
        # 按需安装：AI 需要时才 pip install，沙箱创建时不预装
        if hasattr(sandbox, "_sandbox") and sandbox._sandbox is not None:
            try:
                _r = sandbox.execute(
                    "pip config set global.index-url http://192.168.10.136:3141/root/pypi/+simple/ "
                    "&& pip config set global.trusted-host 192.168.10.136"
                )
                # 记录执行结果（不静默吞）：排查 pip config 是否真正生效
                logger.warning("[DIAG] 沙箱 pip config 结果: %s", str(_r)[:200])
            except Exception as e:
                logger.warning("[DIAG] 沙箱 pip config 失败: %s", e)

            # 注入 cube-egress MITM 根 CA（治本：沙箱信任后 https 出站全通，pip/curl 不再报
            # CERTIFICATE_VERIFY_FAILED）。CA 由部署时从 CubeSandbox 控制面同步到 certs/。
            # 背景：沙箱 https 出站被 cube-egress 透明代理用 cube-root-ca.crt 签发证书，
            # 沙箱不信任该 CA → SSL 验证失败（2026-08-13 实测 pip install 全挂）。
            _ca_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "..", "certs", "cube-root-ca.crt",
            )
            if os.path.isfile(_ca_file):
                try:
                    with open(_ca_file, "rb") as _f:
                        _ca_b64 = base64.b64encode(_f.read()).decode()
                    _r = sandbox.execute(
                        f"echo {_ca_b64} | base64 -d > /tmp/cube-root-ca.crt "
                        "&& mkdir -p /usr/local/share/ca-certificates "
                        "&& cp /tmp/cube-root-ca.crt /usr/local/share/ca-certificates/cube-root-ca.crt "
                        "&& update-ca-certificates 2>&1 | tail -2"
                    )
                    logger.warning("[DIAG] 沙箱注入 cube-egress CA 结果: %s", str(_r)[:200])
                except Exception as e:
                    logger.warning("[DIAG] 沙箱注入 cube-egress CA 失败: %s", e)
            else:
                logger.warning("[DIAG] certs/cube-root-ca.crt 不存在，跳过 CA 注入")
            # 首次创建完成后缓存实例，同 thread_id 后续请求复用
            with _sandbox_cache_lock:
                _sandbox_cache[thread_id] = sandbox
            return sandbox
        return None
    except Exception as e:
        logger.warning("sandbox unavailable: %s", e)
        return None

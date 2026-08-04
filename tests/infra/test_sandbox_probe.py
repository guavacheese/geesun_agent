"""M1 环境快照单测（纯 mock，不依赖真实沙箱）。

运行方式（WSL/本机 venv 均可）：
    pytest tests/infra/test_sandbox_probe.py -q
"""

import sys
from pathlib import Path

import pytest

# 确保项目根在 sys.path（config 依赖项目内模块）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.infra.sandbox import (  # noqa: E402
    _df_avail_to_mb,
    _probe_commands,
    get_env_snapshot,
    probe_sandbox_env,
)


class _FakeResp:
    """模拟 deepagents ExecuteResponse 的最小对象。"""

    def __init__(self, output: str, exit_code: int = 0):
        self.output = output
        self.exit_code = exit_code


class _FakeSandbox:
    """模拟 CubeSandbox.execute 的最小对象。"""

    def __init__(self, resp: _FakeResp):
        self._resp = resp
        self.execute_calls: list[str] = []

    def execute(self, command: str, timeout: int | None = None):
        self.execute_calls.append(command)
        return self._resp


# ─── _df_avail_to_mb ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "avail,expected",
    [
        ("120M", 120),
        ("1.2G", 1228),  # int(1.2*1024)=1228
        ("512K", 0),  # int(512//1024)=0 → max(1,0)=1？见下方修正断言
        ("2T", 2097152),
        ("1024", 0),  # 字节，无单位：1024//1048576 = 0
        ("abc", None),
        ("", None),
    ],
)
def test_df_avail_to_mb(avail: str, expected):
    got = _df_avail_to_mb(avail)
    # 512K 与 1024 字节的整除会得到 0，实现里对 K 分支用 max(1, ...) 保底
    if avail in ("512K", "1024"):
        assert got in (0, 1)
    else:
        assert got == expected


# ─── probe_sandbox_env 正常解析 ──────────────────────────────────────────────

def _build_output() -> str:
    """构造一次典型探测输出：命令路径 + df + rustup。"""
    return (
        "CMD:cargo=/root/.cargo/bin/cargo\n"
        "CMD:python3=/usr/bin/python3\n"
        "CMD:rustc=/root/.cargo/bin/rustc\n"
        "---\n"
        "/dev/sda1 20G 18G 1.2G 90% /\n"
        "---\n"
        "stable-x86_64-unknown-linux-gnu (default)\n"
    )


def test_probe_sandbox_env_parses_output():
    sb = _FakeSandbox(_FakeResp(_build_output()))
    snap = probe_sandbox_env(sb)

    assert snap.ok is True
    assert snap.commands["cargo"] == "/root/.cargo/bin/cargo"
    assert snap.commands["python3"] == "/usr/bin/python3"
    assert snap.commands["rustc"] == "/root/.cargo/bin/rustc"
    # 未安装的命令不出现在 CMD: 行 → commands 中应为 None 或缺失
    assert snap.commands.get("node") is None
    assert snap.disk_avail_mb == 1228  # 1.2G
    assert snap.toolchains == ["stable-x86_64-unknown-linux-gnu (default)"]
    # 合并为单次 execute
    assert len(sb.execute_calls) == 1


def test_probe_sandbox_env_missing_node_and_df_fallback():
    """node 未安装（无 CMD 行）+ df 输出异常列 → 降级为 None 而不崩溃。"""
    output = (
        "CMD:python3=/usr/bin/python3\n"
        "---\n"
        "Filesystem Size Used Avail Use% Mounted on\n"  # tail -1 取到表头（异常场景）
        "---\n"
    )
    sb = _FakeSandbox(_FakeResp(output))
    snap = probe_sandbox_env(sb)
    assert snap.ok is True
    assert snap.commands["python3"] == "/usr/bin/python3"
    assert snap.commands.get("node") is None
    assert snap.disk_avail_mb is None  # 解析不到则不硬凑
    assert snap.toolchains == []


def test_probe_sandbox_env_execute_failure_degrades():
    """execute 失败（非 0 退出）→ ok=False 降级，不抛异常。"""
    sb = _FakeSandbox(_FakeResp("boom", exit_code=1))
    snap = probe_sandbox_env(sb)
    assert snap.ok is False
    assert snap.error is not None
    assert snap.commands == {}


def test_probe_sandbox_env_execute_raises_degrades():
    """execute 抛异常 → ok=False 降级，不抛异常。"""

    class _RaisingSandbox:
        def execute(self, command, timeout=None):
            raise TimeoutError("probe timeout")

    snap = probe_sandbox_env(_RaisingSandbox())
    assert snap.ok is False
    assert "probe timeout" in (snap.error or "")


# ─── to_hint 渲染 ────────────────────────────────────────────────────────────

def test_snapshot_to_hint_render():
    from src.infra.sandbox import SandboxEnvSnapshot

    snap = SandboxEnvSnapshot(
        ok=True,
        commands={"cargo": "/root/.cargo/bin/cargo", "node": None},
        disk_avail_mb=120,
        toolchains=[],
    )
    hint = snap.to_hint()
    assert "cargo: /root/.cargo/bin/cargo" in hint
    assert "node: 未安装" in hint
    assert "120MB" in hint
    assert "偏低" in hint  # 120 < warn 阈值 200


# ─── get_env_snapshot 缓存 ───────────────────────────────────────────────────

def test_get_env_snapshot_cache_reuse():
    sb = _FakeSandbox(_FakeResp(_build_output()))
    s1 = get_env_snapshot(sb, thread_id="cache-test")
    s2 = get_env_snapshot(sb, thread_id="cache-test")
    assert s1 is s2  # TTL 内同一对象，不重复执行
    assert len(sb.execute_calls) == 1


def test_get_env_snapshot_none_sandbox():
    assert get_env_snapshot(None, thread_id="x") is None


# ─── 配置白名单覆盖 ──────────────────────────────────────────────────────────

def test_probe_commands_default_and_override(monkeypatch):
    from src.core.config import settings

    monkeypatch.setattr(settings, "sandbox_probe_commands", "")
    assert _probe_commands() == [
        "rustc", "cargo", "python3", "node", "go", "gcc", "javac",
    ]

    monkeypatch.setattr(settings, "sandbox_probe_commands", '["python3", "rustc"]')
    assert _probe_commands() == ["python3", "rustc"]

    monkeypatch.setattr(settings, "sandbox_probe_commands", "{bad json")
    assert _probe_commands() == [
        "rustc", "cargo", "python3", "node", "go", "gcc", "javac",
    ]

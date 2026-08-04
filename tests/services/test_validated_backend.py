"""M2 写入纠错单测：拒绝时返回可执行修正建议（设计文档 M2）。

覆盖 _reject_hint 三分类路径（沙箱路径 / 只读路径 / 未知路径）与
write/awrite 拒绝分支的 error 文案。允许路径的 super().write 路由
属集成测试范畴，此处只测拒绝路径（不触发真实 backend 写入）。

说明：Windows 侧无法安装 deepagents（私有源/网络差异），测试通过
sys.modules 打桩 deepagents 与 langchain.messages 后仍可真实加载
src.services.agent 的 ValidatedCompositeBackend 进行纯逻辑断言。

运行：pytest tests/services/test_validated_backend.py -q
"""

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _install_stubs() -> None:
    """打桩 deepagents / langchain.messages，使本模块可在无 deepagents 环境加载。"""

    @dataclass
    class WriteResult:
        error: str | None = None
        path: str = ""
        files_update: object = None
        created_files: list = field(default_factory=list)
        updated_files: list = field(default_factory=list)

    @dataclass
    class FileDownloadResponse:
        path: str = ""
        content: bytes | None = None
        error: str | None = None

    class CompositeBackend:
        def __init__(self, default=None, routes=None):
            self.default = default
            self.routes = routes or {}

        def write(self, file_path, content):
            raise NotImplementedError

        async def awrite(self, file_path, content):
            raise NotImplementedError

    protocol = types.ModuleType("deepagents.backends.protocol")
    protocol.WriteResult = WriteResult
    protocol.FileDownloadResponse = FileDownloadResponse

    utils = types.ModuleType("deepagents.backends.utils")
    utils.file_data_to_string = lambda x: str(x)

    backends = types.ModuleType("deepagents.backends")
    backends.CompositeBackend = CompositeBackend
    backends.FilesystemBackend = type("FilesystemBackend", (), {})
    backends.LocalShellBackend = type("LocalShellBackend", (), {})
    backends.StateBackend = type("StateBackend", (), {})
    backends.StoreBackend = type("StoreBackend", (), {})
    backends.protocol = protocol
    backends.utils = utils

    deepagents = types.ModuleType("deepagents")
    deepagents.create_deep_agent = lambda **kw: None
    deepagents.backends = backends

    langchain_messages = types.ModuleType("langchain.messages")
    langchain_messages.trim_messages = lambda *a, **k: None

    for name, mod in (
        ("deepagents", deepagents),
        ("deepagents.backends", backends),
        ("deepagents.backends.protocol", protocol),
        ("deepagents.backends.utils", utils),
        ("langchain.messages", langchain_messages),
    ):
        sys.modules.setdefault(name, mod)

    # src.core.model 依赖 langchain_openai，也打桩
    stub_model = types.ModuleType("src.core.model")
    stub_model.create_model = lambda *a, **k: None
    stub_model.switch_model = lambda *a, **k: None
    sys.modules.setdefault("src.core.model", stub_model)


_install_stubs()

from src.services.agent import ValidatedCompositeBackend  # noqa: E402
from deepagents.backends.protocol import WriteResult  # noqa: E402


@pytest.fixture
def backend():
    """用空 default 实例化（拒绝路径不会触碰 default）。"""
    return ValidatedCompositeBackend(
        default=None,
        routes={},
        user_id="user-01",
        session_id="sess-02",
    )


# ─── _reject_hint 三分类 ─────────────────────────────────────────────────────

def test_hint_sandbox_path(backend):
    hint = backend._reject_hint("/tmp/rust_payment/Cargo.toml")
    assert "沙箱内路径" in hint
    assert "upload_to_sandbox" in hint or "copy_script_to_sandbox" in hint
    assert "/reports/user-01/sess-02/" in hint  # 上下文已算好塞回


def test_hint_readonly_virtual_path(backend):
    hint = backend._reject_hint("/uploads/user-01/sess-02/input.xml")
    assert "只读" in hint
    assert "/reports/user-01/sess-02/" in hint


def test_hint_unknown_path(backend):
    hint = backend._reject_hint("/somewhere/else/file.txt")
    assert "只能写入" in hint
    assert "/reports/user-01/sess-02/" in hint


def test_allowed_prefixes_not_rejected():
    allowed = [
        p
        for p in (
            "/reports/user-01/sess-02/out.md",
            "/workspace/memories/prefs.md",
        )
        if not any(p.startswith(x) for x in ValidatedCompositeBackend.ALLOWED_WRITE_PREFIXES)
    ]
    assert allowed == []


# ─── write 拒绝分支 ──────────────────────────────────────────────────────────

def test_write_reject_returns_error_with_hint(backend):
    result = backend.write("/tmp/rust_payment/src/main.rs", "fn main() {}")
    assert isinstance(result, WriteResult)
    assert result.error is not None
    assert "拒绝写入" in result.error
    assert "/reports/user-01/sess-02/" in result.error
    assert result.path == "/tmp/rust_payment/src/main.rs"


def test_awrite_reject_returns_error_with_hint(backend):
    import asyncio

    result = asyncio.run(backend.awrite("/root/foo.rs", "x"))
    assert isinstance(result, WriteResult)
    assert result.error is not None
    assert "/reports/user-01/sess-02/" in result.error


# ─── 默认 session 兜底 ───────────────────────────────────────────────────────

def test_default_report_prefix_without_session():
    b = ValidatedCompositeBackend(default=None, routes={})
    hint = b._reject_hint("/tmp/x.rs")
    assert "/reports/<user_id>/<session_id>/" in hint  # 未传上下文时给模板

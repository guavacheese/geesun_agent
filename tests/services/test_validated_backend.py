"""M2 写入纠错单测：拒绝时返回可执行修正建议（设计文档 M2）。

覆盖 _reject_hint 三分类路径（沙箱路径 / 只读路径 / 未知路径）与
write/awrite 拒绝分支的 error 文案。允许路径的 super().write 路由
属集成测试范畴，此处只测拒绝路径（不触发真实 backend 写入）。

依赖来源：**优先真实 deepagents**（本机与生产镜像均为 0.6.12）；仅当真实包不可用时，
才按 src/services/agent.py 的模块级 import 契约装一套最小桩（见 _REQUIRED_DEEPAGENTS）。

⚠️ 2026-09-11 教训：旧版无条件打桩，且桩只覆盖 protocol 的 2 个结果类型 + backends 的
5 个类 + utils 的 1 个函数，**缺 GlobResult/GrepResult/ReadResult 与
middleware.summarization / middleware.skills 两个子包**；又用 sys.modules.setdefault
抢在真实包之前注册 → 即使真实 deepagents 已安装也被桩顶掉，import agent.py 直接
`ImportError: cannot import name 'GlobResult' from 'deepagents.backends.protocol'`。
根治：真实包可用时**不装桩**；桩体系改为声明式契约 + 装完自校验。

运行：pytest tests/services/test_validated_backend.py -q
"""

import importlib
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ─── 依赖契约：src/services/agent.py 第 9~31 行的模块级 import ───────────────────
# 键 = 模块名，值 = 该模块必须提供的符号。**改 agent.py 的 import 时同步改这里**，
# 否则末尾的契约自校验会立刻报错，而不是退化成难定位的 ImportError。
_REQUIRED_DEEPAGENTS: dict[str, tuple[str, ...]] = {
    "deepagents": ("create_deep_agent",),
    "deepagents.backends": (
        "CompositeBackend",
        "FilesystemBackend",
        "LocalShellBackend",
        "StateBackend",
        "StoreBackend",
    ),
    "deepagents.backends.protocol": (
        "FileDownloadResponse",
        "GlobResult",
        "GrepResult",
        "ReadResult",
        "WriteResult",
    ),
    "deepagents.backends.utils": ("file_data_to_string",),
    "deepagents.middleware.summarization": ("SummarizationMiddleware",),
    "deepagents.middleware.skills": (
        "SkillsMiddleware",
        "SkillsStateUpdate",
        "_alist_skills_with_errors",
        "_list_skills_with_errors",
    ),
}


def _real_deepagents_usable() -> bool:
    """真实 deepagents 是否满足上表契约（任一模块缺失/符号不全即视为不可用）。"""
    for mod_name, attrs in _REQUIRED_DEEPAGENTS.items():
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001 —— 缺包、版本不符统一归为"不可用"
            return False
        if any(not hasattr(mod, attr) for attr in attrs):
            return False
    return True


def _build_stub_modules() -> dict[str, types.ModuleType]:
    """构造覆盖整张契约的最小桩（**仅真实包不可用时**调用）。"""

    @dataclass
    class _Result:
        """结果占位类型：字段对齐真实值（拒绝分支只消费 error / path）。"""

        error: str | None = None
        path: str | None = None
        files_update: dict | None = None

    class CompositeBackend:
        def __init__(self, default=None, routes=None, **kwargs):
            self.default = default
            self.routes = routes or {}

        def write(self, file_path, content):
            raise NotImplementedError

        async def awrite(self, file_path, content):
            raise NotImplementedError

    def _not_implemented(*_args, **_kwargs):
        raise NotImplementedError("deepagents 桩：该调用不在本单测范围内")

    protocol = types.ModuleType("deepagents.backends.protocol")
    for _name in (
        "WriteResult",
        "FileDownloadResponse",
        "GlobResult",
        "GrepResult",
        "ReadResult",
    ):
        setattr(protocol, _name, _Result)

    utils = types.ModuleType("deepagents.backends.utils")
    utils.file_data_to_string = lambda x: str(x)

    backends = types.ModuleType("deepagents.backends")
    backends.CompositeBackend = CompositeBackend
    for _name in ("FilesystemBackend", "LocalShellBackend", "StateBackend", "StoreBackend"):
        setattr(backends, _name, type(_name, (), {}))
    backends.protocol = protocol
    backends.utils = utils

    summarization = types.ModuleType("deepagents.middleware.summarization")

    class SummarizationMiddleware:
        """桩类，仅供 import。"""

    summarization.SummarizationMiddleware = SummarizationMiddleware

    skills = types.ModuleType("deepagents.middleware.skills")

    class SkillsMiddleware:
        """桩类，agent.py 的 _FreshSkillsMiddleware 会继承它。"""

    skills.SkillsMiddleware = SkillsMiddleware
    skills.SkillsStateUpdate = dict
    skills._alist_skills_with_errors = _not_implemented
    skills._list_skills_with_errors = _not_implemented

    middleware = types.ModuleType("deepagents.middleware")
    middleware.summarization = summarization
    middleware.skills = skills

    top = types.ModuleType("deepagents")
    top.create_deep_agent = _not_implemented
    top.backends = backends
    top.middleware = middleware

    return {
        "deepagents": top,
        "deepagents.backends": backends,
        "deepagents.backends.protocol": protocol,
        "deepagents.backends.utils": utils,
        "deepagents.middleware": middleware,
        "deepagents.middleware.summarization": summarization,
        "deepagents.middleware.skills": skills,
    }


def _ensure_dependencies() -> str:
    """就绪 deepagents 依赖，返回实际来源（"real" / "stub"）。"""
    if _real_deepagents_usable():
        return "real"
    # 真实包不可用（或缺符号）：强制覆盖注册，保证"纯桩"而非半真半假
    sys.modules.update(_build_stub_modules())
    for mod_name, attrs in _REQUIRED_DEEPAGENTS.items():
        missing = [a for a in attrs if not hasattr(sys.modules[mod_name], a)]
        assert not missing, f"deepagents 桩契约不完整: {mod_name} 缺 {missing}"
    return "stub"


# 注：不再打桩 langchain.messages 与 src.core.model——前者真实存在；后者在 agent.py 里是
# **函数级** import，模块级加载用不到，而往 sys.modules 里塞假模块会污染同一 pytest
# 进程中的其他测试文件（静默换掉它们的真实依赖）。
DEEPAGENTS_SOURCE = _ensure_dependencies()

from deepagents.backends.protocol import WriteResult  # noqa: E402
from src.services.agent import ValidatedCompositeBackend  # noqa: E402


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
    # ⚠️ 必须是**真正被拒**的沙箱路径：/home/、/tmp/ 已放进 ALLOWED_WRITE_PREFIXES
    # （直写沙箱通道），只有 /root/ /mnt/ /code/ /var/ 仍被拒（SANDBOX_PATH_PREFIXES）。
    # 旧版测试用 /tmp/rust_payment/Cargo.toml，在白名单变更后已属"允许写入"，断言随之失效。
    hint = backend._reject_hint("/root/rust_payment/Cargo.toml")
    assert "沙箱内系统级/挂载路径" in hint
    # 提示同步指向「直写沙箱 /home/user/」，不再引导 upload_to_sandbox
    assert "write_file" in hint and "/home/" in hint
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
            "/conversation_history/history.md",
            "/skills/__agent__/my-skill/SKILL.md",
            "/home/user/script.py",  # 直写沙箱通道（2026-08 起放行）
            "/tmp/build/out.log",  # 同上
        )
        if not any(p.startswith(x) for x in ValidatedCompositeBackend.ALLOWED_WRITE_PREFIXES)
    ]
    assert allowed == []


# ─── write 拒绝分支 ──────────────────────────────────────────────────────────

def test_write_reject_returns_error_with_hint(backend):
    # 同 test_hint_sandbox_path：用 /root/ 而非 /tmp/ 才会命中拒绝分支；
    # /tmp/ 属白名单 → 会走到 super().write()（本测试 default=None，必然 AttributeError）。
    target = "/root/rust_payment/src/main.rs"
    result = backend.write(target, "fn main() {}")
    assert isinstance(result, WriteResult)
    assert result.error is not None
    assert "拒绝写入" in result.error
    assert "/reports/user-01/sess-02/" in result.error
    assert result.path == target


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


# ─── 依赖契约自校验（防再次漂移）─────────────────────────────────────────────

def test_deepagents_import_contract_satisfied():
    """agent.py 模块级 import 的每个符号都必须存在——真包与桩两种模式均适用。"""
    for mod_name, attrs in _REQUIRED_DEEPAGENTS.items():
        mod = importlib.import_module(mod_name)
        missing = [a for a in attrs if not hasattr(mod, a)]
        assert not missing, f"{mod_name} 缺 {missing}"


def test_report_dependency_source():
    """留痕：本次断言跑在真实包还是桩上（打印出来，便于区分环境差异）。"""
    print(f"\n[test_validated_backend] deepagents 来源 = {DEEPAGENTS_SOURCE}")
    assert DEEPAGENTS_SOURCE in ("real", "stub")

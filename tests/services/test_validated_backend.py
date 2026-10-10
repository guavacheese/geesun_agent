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

import ast
import importlib
import json
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
        "ExecuteResponse",
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

    @dataclass
    class _ExecuteResponse:
        """execute 结果占位：字段对齐真实值（C 闸门读 output/exit_code，写 truncated）。"""

        output: str
        exit_code: int | None = None
        truncated: bool = False

    protocol.ExecuteResponse = _ExecuteResponse

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

from deepagents.backends.protocol import ExecuteResponse, WriteResult  # noqa: E402
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


# ─── C｜execute 输出体积闸门（2026-10-10）─────────────────────────────────────
# 事故：会话 4863afff 单次 execute 返回 45.8MB（OCR 脚本把含 base64 图片的结果
# 整坨 print），offload 通道被白名单拒绝 → 45.8MB 留在 state → 下一轮撑爆窗口。

_LIMIT = ValidatedCompositeBackend.MAX_EXECUTE_OUTPUT_CHARS


def _exec_resp(output: str, *, exit_code: int = 0, truncated: bool = False) -> ExecuteResponse:
    return ExecuteResponse(output=output, exit_code=exit_code, truncated=truncated)


def test_clamp_passthrough_when_under_limit():
    """未超限必须**原样返回同一对象**——闸门不得触碰正常路径。"""
    r = _exec_resp("hello\n")
    assert ValidatedCompositeBackend._clamp_execute_output(r) is r
    assert r.truncated is False


def test_clamp_passthrough_exactly_at_limit():
    """恰好等于上限不截断（边界取闭区间，避免无谓的通知噪音）。"""
    r = _exec_resp("x" * _LIMIT)
    assert ValidatedCompositeBackend._clamp_execute_output(r) is r


def test_clamp_limit_non_positive_disables():
    """limit <= 0 = 不限制（排障开关），不得截断。"""
    r = _exec_resp("y" * (_LIMIT + 1))
    assert ValidatedCompositeBackend._clamp_execute_output(r, limit=0) is r


def test_clamp_truncates_head_and_tail_keeps_middle_dropped():
    """超限 → 头尾保留、中间丢弃、总长恰为上限、truncated 置真、exit_code 不变。"""
    body = "H" * _LIMIT + "M" * 5_000_000 + "T" * _LIMIT
    total = len(body)
    out = ValidatedCompositeBackend._clamp_execute_output(
        _exec_resp(body, exit_code=7)
    )
    assert out.truncated is True
    assert out.exit_code == 7, "闸门不得改变命令成败事实"
    assert len(out.output) == _LIMIT, "截断后总长必须恰好等于上限（含通知）"
    assert out.output.startswith("H" * 100), "头部必须保留"
    assert out.output.endswith("T" * 100), "尾部必须保留（结论/traceback 在此）"
    assert "M" not in out.output, "中间部分必须被丢弃"
    assert "输出超限已截断" in out.output
    assert str(total) in out.output and str(_LIMIT) in out.output, (
        "通知必须给出真实体积，否则用户/模型无法判断被截了多少"
    )
    assert "/tmp/out.txt" in out.output and "read_file" in out.output, (
        "通知必须给出可执行的取回指引，而不是只报错"
    )


def test_clamp_notice_survives_absurdly_small_limit():
    """上限小于通知长度时：牺牲正文，但『已截断』这条信息必须保住。"""
    out = ValidatedCompositeBackend._clamp_execute_output(
        _exec_resp("z" * 5000), limit=30
    )
    assert "已截断" in out.output
    assert "z" not in out.output
    assert out.truncated is True


# ─── 接线契约：闸门必须挂在工具层执行入口，且不得误伤内部文件系统管道 ──────────

def _agent_ast():
    return ast.parse(
        (PROJECT_ROOT / "src" / "services" / "agent.py").read_text(encoding="utf-8")
    )


def _func_ast(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls(node, name: str) -> bool:
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr == name:
            return True
        if isinstance(f, ast.Name) and f.id == name:
            return True
    return False


def test_clamp_wired_into_both_execute_paths():
    """execute 与 aexecute 都必须过闸门——模型实际走 aexecute（coroutine 分支），
    只补同步版等于没修。"""
    tree = _agent_ast()
    for fn in ("execute", "aexecute"):
        node = _func_ast(tree, fn)
        assert node is not None, f"缺少 {fn}"
        assert _calls(node, "_clamp_execute_output"), (
            f"{fn} 未调用 _clamp_execute_output —— 该通道的输出重新变成无界"
        )


def test_clamp_not_applied_to_internal_filesystem_pipeline():
    """read/ls/grep/glob 的输出是结构化格式，被截断会导致解析失败。

    deepagents 的 BaseSandbox 内部复用 `self.execute()`（backends/sandbox.py:629/
    668/698/813/857/956/987）并解析其 JSON，因此闸门必须只挂在 CompositeBackend
    这一层（工具层唯一入口）。**反向断言**：这些方法自身不得引入截断逻辑。
    """
    tree = _agent_ast()
    guarded = ("read", "aread", "grep", "agrep", "ls", "als", "glob", "aglob")
    checked = 0
    for fn in guarded:
        node = _func_ast(tree, fn)
        if node is None:  # 未覆写即继承父类 → 必然不经闸门，安全
            continue
        checked += 1
        assert not _calls(node, "_clamp_execute_output"), (
            f"{fn} 引入了输出截断 —— 会破坏其结构化输出的解析（read_file/grep/ls 全线报错）"
        )
    # 至少要有覆写被检查到，否则本断言形同虚设（防止将来方法名变更后静默失效）
    assert checked >= 1, "未覆写任何内部文件系统方法，本断言失去意义，请同步更新方法名"


def test_cap_scoped_to_tool_channel_not_filesystem_pipeline():
    """**取证级对照**：同一坨超大输出，走两条路结果必须不同。

    - 工具通道 `backend.execute(cmd)`（模型自己发的 shell 命令）→ 必须被截断；
    - 文件系统通道 `backend.read(path)`（read_file 工具）→ **必须原样返回**。

    后者之所以安全，是因为 deepagents 的 `BaseSandbox.read` 内部复用**底层**
    sandbox 的 `execute`（backends/sandbox.py:668），**不经过本类**。这正是闸门
    必须放在 `CompositeBackend` 这一层、而不能下沉到底层 `sandbox.execute()` 的
    原因：后者被 read/ls/grep/glob 共用且按 JSON 解析（_parse_read_output
    `json.loads` —— 见 backends/sandbox.py:456），截断会往 JSON 尾部追加提示文本
    → `json.JSONDecodeError` → read_file/grep/ls 全线报错。

    本测试是那个设计决策的守门人：谁把闸门挪到底层，这里立刻变红。
    """
    oversized = json.dumps({"content": "A" * (_LIMIT + 5000), "encoding": "utf-8"})

    # 桩模式下没有 BaseSandbox 的真实语义，本测试只在真包上跑（其余测试两种模式都跑）
    _sandbox_mod = pytest.importorskip(
        "deepagents.backends.sandbox",
        reason="本测试需要真实 deepagents 的 BaseSandbox 复用关系",
    )
    BaseSandbox = _sandbox_mod.BaseSandbox

    class _FakeSandbox(BaseSandbox):
        """最小可用的沙箱替身：固定回同一坨超大输出，并记录被调用情况。"""

        def __init__(self) -> None:
            self.commands: list[str] = []

        @property
        def id(self) -> str:
            return "fake-sandbox"

        def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
            self.commands.append(command)
            return ExecuteResponse(output=oversized, exit_code=0)

        def upload_files(self, files):
            return []

        def download_files(self, paths):
            return []

    fake = _FakeSandbox()
    backend = ValidatedCompositeBackend(
        default=fake, routes={}, user_id="user-01", session_id="sess-02"
    )

    # ① 文件系统通道：原样通过（证明未被截断 → JSON 解析成功、内容完整）
    read_result = backend.read("/tmp/big.txt")
    assert read_result.error is None, (
        f"read 被截断会直接破坏 JSON 解析：{read_result.error}"
    )
    assert len(read_result.file_data["content"]) == _LIMIT + 5000, (
        "read 通道被截断了 —— 闸门被错误地下沉到了底层 sandbox.execute"
    )

    # ② 工具通道：同一个 sandbox、同一坨输出，必须被截断
    exec_result = backend.execute("echo hi")
    assert exec_result.truncated is True
    assert len(exec_result.output) == _LIMIT
    assert "输出超限已截断" in exec_result.output

    # ③ 两条通道都确实打到了底层 sandbox（否则①的结论没有意义）
    assert len(fake.commands) == 2, fake.commands


def test_negative_control_bottom_layer_cap_would_break_read():
    """负向对照：**故意**把闸门下沉到底层 sandbox.execute，read 立刻被打挂。

    这不是假想风险，是本设计选择的直接依据——用真实 `_parse_read_output` 跑一遍
    即可复现：截断把提示文本追加进 JSON 尾部 → `json.JSONDecodeError`
    （backends/sandbox.py:456）→ `ReadResult.error = "unexpected server response"`。

    去掉上面那条 `test_cap_scoped_to_tool_channel_not_filesystem_pipeline` 时，
    本测试就是它存在的理由；两条一起看：闸门放对了层，read 才不会碎。
    """
    oversized = json.dumps({"content": "A" * (_LIMIT + 5000), "encoding": "utf-8"})

    _sandbox_mod = pytest.importorskip("deepagents.backends.sandbox")

    class _BottomCappedSandbox(_sandbox_mod.BaseSandbox):
        """反例替身：把截断逻辑塞进底层 execute（即"放错层"的形态）。"""

        @property
        def id(self) -> str:
            return "bottom-capped"

        def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
            return ValidatedCompositeBackend._clamp_execute_output(
                ExecuteResponse(output=oversized, exit_code=0)
            )

        def upload_files(self, files):
            return []

        def download_files(self, paths):
            return []

    backend = ValidatedCompositeBackend(
        default=_BottomCappedSandbox(),
        routes={},
        user_id="user-01",
        session_id="sess-02",
    )
    read_result = backend.read("/tmp/big.txt")
    assert read_result.error is not None, (
        "负向对照失效：底层截断竟未打挂 read —— 说明本对照已不能证明"
        "「闸门必须放在工具层」这一论断，请复查"
    )
    assert "unexpected server response" in read_result.error


def test_clamp_active_on_async_path():
    """模型实际走的是 aexecute（工具层优先 coroutine 分支）——必须行为一致。

    只补同步版等于没修：这条用真实的 async 链路
    `ValidatedCompositeBackend.aexecute → CompositeBackend.aexecute →
    BaseSandbox.aexecute(to_thread) → 底层 execute` 跑一遍。
    """
    import asyncio

    _sandbox_mod = pytest.importorskip("deepagents.backends.sandbox")

    class _BigSandbox(_sandbox_mod.BaseSandbox):
        @property
        def id(self) -> str:
            return "big-output"

        def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
            return ExecuteResponse(output="A" * (_LIMIT * 3), exit_code=3)

        def upload_files(self, files):
            return []

        def download_files(self, paths):
            return []

    backend = ValidatedCompositeBackend(
        default=_BigSandbox(), routes={}, user_id="user-01", session_id="sess-02"
    )
    result = asyncio.run(backend.aexecute("echo hi"))
    assert result.truncated is True
    assert len(result.output) == _LIMIT
    assert result.exit_code == 3
    assert "输出超限已截断" in result.output


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

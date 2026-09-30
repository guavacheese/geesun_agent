"""Spike: execute 虚拟路径护栏（A 前置拒绝 + B 收尾救济）回归位（2026-09-29 新增）。

背景（生产会话 GY35377:60c64c4f 实证，报告见
`geesun_agent_web/.workbuddy/reports/pptx-claim-vs-download-60c64c4f-2026-09-29.html`）：
    模型用 execute 在沙箱内生成 pptx，再在沙箱内 `mkdir -p /reports/... && cp ...`
    （exit 0）就宣称"已保存、可直接下载"；而**沙箱从未挂载宿主报告卷**，
    产物只活在 MicroVM 里，5min TTL 一过灰飞烟灭，宿主目录全程零产出。
    注意环境在这里是**帮凶**：`cp: ... No such file or directory` 把模型引导成
    "目录没建好"，于是 mkdir 后重试"成功"——提示词拦不住这条被诱导的路径。

本 spike 直接 import **真实实现**（不是复制一份），覆盖：
    A  正则命中矩阵（路径字面量命中 / 同前缀目录名不误伤）
    A  ValidatedCompositeBackend.execute/aexecute：命中拒绝且**不触达** default、
       未命中透传、`timeout` 关键字签名保留（工具层靠它判定后端是否支持超时）
    A  拒绝姿势契约：ValueError（而非 ExecuteResponse(exit_code=1)，后者会被工具层
       当成功）
    A  拒绝文案契约：必须给出正确通道（write_file / download_from_sandbox）
  B  轮次信号生命周期：note/peek/consume/clear + 窗口上限 + 跨会话隔离
  B  A→B 串联：backend 命中即登记信号；信号驱动收尾救济把沙箱产物拉回宿主
  B  调用点接入契约（AST 静态断言）：chat.py 开轮 clear / 收尾 peek+consume 真实存在，
     顺序为 clear < peek < consume；note_* 只由 backend 调用
     （补测动机：1.0.22 首轮构建抓到 clear_* 全文零调用点而模块行为测试全绿
      —— 模块级正确 ≠ 被正确接入，见该节注释）

运行环境：**生产同款镜像**（chat.py 依赖 fastapi 等；deepagents 需与生产同版本）：
  docker run --rm --entrypoint /bin/sh 172.16.220.74:8333/geesun_ai/geesun-agent:1.0.21 \
    -c 'cd /app && /app/.venv/bin/python /tmp/spike_test.py'
（实际由 `geesun_agent_web/.workbuddy/spikes/run_src_tests.py` 驱动，会自动还原容器内源码）
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import sys
import tempfile
from typing import Any

sys.path.insert(0, "/app")

from deepagents.backends import LocalShellBackend  # noqa: E402
from deepagents.backends.protocol import (  # noqa: E402
    ExecuteResponse,
    execute_accepts_timeout,
)
from deepagents.middleware.filesystem import supports_execution  # noqa: E402

from src.api.endpoints.chat import (  # noqa: E402
    _salvage_sandbox_artifacts,
    _tool_result_is_error,
)
from src.core import execution_guard  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.services.agent import ValidatedCompositeBackend  # noqa: E402

PASS = 0
FAIL = 0
USER = "GY35377"
SID = "60c64c4f"
REPORT_PREFIX = f"/reports/{USER}/{SID}/"

# 生产原始命令（会话 60c64c4f key=46/48）
PROD_CP_CMD = (
    f"cd /home/user && cp 项目团队任命书.pptx {REPORT_PREFIX}项目团队任命书.pptx"
)
PROD_MKDIR_CMD = f"mkdir -p {REPORT_PREFIX} && cp /home/user/项目团队任命书.pptx {REPORT_PREFIX}项目团队任命书.pptx"


def check(label: str, actual: Any, expected: Any) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}\n      actual   = {actual!r}\n      expected = {expected!r}")


def check_true(label: str, cond: bool, detail: str = "") -> None:
    check(label if not detail else f"{label} [{detail}]", bool(cond), True)


# ─────────────────────────── 替身 ───────────────────────────


class SpyDefault:
    """只记录调用的 default backend：用于断言"命中时 default 一次都没被调用"。

    ⚠️ 故意**不**实现 SandboxBackendProtocol 全量成员：命中路径在
    `super().execute()` 的 isinstance 检查**之前**就抛 ValueError，所以替身不需要
    通过 protocol 判定；未命中的透传用真 LocalShellBackend 另测（见 _make_shell）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | None]] = []

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        self.calls.append(("sync", command, timeout))
        return ExecuteResponse(output="spy-ok", exit_code=0)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        self.calls.append(("async", command, timeout))
        return ExecuteResponse(output="spy-ok", exit_code=0)


class FakeResp:
    """对齐 langchain_cubesandbox 的 ExecuteResponse / FileDownloadResponse。"""

    def __init__(self, output: str = "", exit_code: int = 0, content: bytes | None = None):
        self.output = output
        self.exit_code = exit_code
        self.content = content


class FakeSandbox:
    """B 部分的最小沙箱替身：execute 只用于 find 枚举，download_files 取内容。"""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.commands: list[str] = []
        self.download_calls: list[list[str]] = []

    def execute(self, command: str, timeout: int | None = None) -> FakeResp:
        self.commands.append(command)
        lines = [f"{p}\t{len(c)}" for p, c in sorted(self.files.items())]
        return FakeResp(output="\n".join(lines) + ("\n" if lines else ""))

    def download_files(self, paths: list[str]) -> list[FakeResp]:
        self.download_calls.append(list(paths))
        return [FakeResp(content=self.files.get(p)) for p in paths]


class _TempReportRoot:
    """把 settings.report_root 指到临时目录（被测函数直接读 settings），退出即还原。"""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="execguard_")
        self.saved = settings.report_root

    def __enter__(self) -> str:
        settings.report_root = self.tmp
        return self.tmp

    def __exit__(self, *exc: Any) -> None:
        settings.report_root = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)


def _make_backend(default: Any) -> ValidatedCompositeBackend:
    """构造被测 backend（入参同 src.services.agent.build_backend）。"""
    return ValidatedCompositeBackend(
        default=default, routes={}, user_id=USER, session_id=SID
    )


def _make_shell() -> LocalShellBackend:
    """真 shell 后端：用于"未命中命令必须正常执行"（含 timeout 透传）的行为断言。

    注意 `LocalShellBackend` 没有公开的 root_dir 属性，故临时目录单独登记，由
    `main()` 统一清理（不要写 `shell.root_dir`——实测该属性不存在）。
    """
    d = tempfile.mkdtemp(prefix="execguard_shell_")
    _SHELL_TMPDIRS.append(d)
    return LocalShellBackend(root_dir=d, virtual_mode=True, env={**os.environ})


_SHELL_TMPDIRS: list[str] = []


def _expect_value_error(fn, *args, **kwargs) -> str | None:
    """调用 fn，命中护栏时返回拒绝文案，否则返回 None（并打印实际返回）。"""
    try:
        fn(*args, **kwargs)
    except ValueError as e:
        return str(e)
    return None


# ─────────────────────── A：正则命中矩阵 ───────────────────────


def test_a_regex_matrix() -> None:
    """A 的匹配面：路径**字面量**一律拦；同前缀目录名不误伤。"""
    print("\n[A] 虚拟路径正则命中矩阵")
    re_ = ValidatedCompositeBackend.EXECUTE_VIRTUAL_PATH_RE

    hits = {
        "生产原始 cp（含中文文件名）": PROD_CP_CMD,
        "生产 mkdir+cp 形态": PROD_MKDIR_CMD,
        "cd 到该目录": "cd /reports && ls",
        "目录即路径末（行尾）": "mkdir -p /reports",
        "python -c 内 open()": "python3 -c \"open('/reports/u/s/a.pptx','wb').write(b'x')\"",
        "读 /uploads（同样是虚拟路径）": f"ls /uploads/{USER}/{SID}/",
        "heredoc 多行": "cat > /reports/u/s/a.md <<'EOF'\nhi\nEOF",
        "重定向无空格": "echo hi >/reports/u/s/a.txt",
        "单引号包裹": "cp x '/reports/u/s/a.pptx'",
        "双引号包裹": 'cp x "/reports/u/s/a.pptx"',
    }
    for label, cmd in hits.items():
        check_true(f"A·命中 {label}", re_.search(cmd) is not None)

    misses = {
        "同前缀目录名 /reportsX": "echo hi > /reportsX/a.txt",
        "/tmp/reports.txt（后跟点）": "cat /tmp/reports.txt",
        "正常脚本执行": "cd /home/user && python3 generate_ppt.py",
        "沙箱内正常落盘": "cp /home/user/a.pptx /home/user/b.pptx",
        "无路径的普通命令": "pip list",
        "词里含 reports": "echo my_reports_are_ready",
    }
    for label, cmd in misses.items():
        check_true(f"A·不误伤 {label}", re_.search(cmd) is None)


# ─────────────────── A：backend 覆写行为 ───────────────────


def test_a_reject_and_passthrough() -> None:
    """A 的行为：命中 → ValueError 且 default 未被调用；未命中 → 透传真执行。"""
    print("\n[A] execute 覆写：拒绝与透传")

    # ── 命中（sync）──
    spy = SpyDefault()
    backend = _make_backend(spy)
    hint = _expect_value_error(backend.execute, PROD_MKDIR_CMD)
    check_true("A·sync 命中抛 ValueError", hint is not None)
    check("A·拒绝时 default **一次都没被调用**（命令确实没执行）", spy.calls, [])

    # ── 命中（async）──
    spy2 = SpyDefault()
    backend2 = _make_backend(spy2)
    hint2 = _expect_value_error(
        lambda: asyncio.run(backend2.aexecute(PROD_CP_CMD))
    )
    check_true("A·async 命中抛 ValueError", hint2 is not None)
    check("A·async 拒绝时 default 未被调用", spy2.calls, [])

    # ── 未命中：透传真 shell（sync/async 一致）──
    shell = _make_shell()
    backend3 = _make_backend(shell)
    r = backend3.execute("echo geesun-guard-ok")
    check("A·未命中 sync 透传 exit_code", r.exit_code, 0)
    check_true("A·未命中 sync 真执行（输出含标记）", "geesun-guard-ok" in (r.output or ""), repr(r.output))
    r2 = asyncio.run(backend3.aexecute("echo geesun-guard-ok-async"))
    check("A·未命中 async 透传 exit_code", r2.exit_code, 0)
    check_true(
        "A·未命中 async 真执行", "geesun-guard-ok-async" in (r2.output or ""), repr(r2.output)
    )

    # ── 未命中且带 timeout：证明签名兼容（工具层会带 timeout 调用）──
    shell2 = _make_shell()
    backend4 = _make_backend(shell2)
    r3 = backend4.execute("echo with-timeout", timeout=30)
    check("A·带 timeout 的未命中命令正常执行", r3.exit_code, 0)
    r4 = asyncio.run(backend4.aexecute("echo with-timeout-async", timeout=30))
    check("A·async 带 timeout 正常执行", r4.exit_code, 0)
def test_a_signature_and_identity_contract() -> None:
    """A 的结构契约：timeout 签名保留 + backend 仍被判定"支持执行"。"""
    print("\n[A] 签名/身份契约（回归位）")
    sig = inspect.signature(ValidatedCompositeBackend.execute)
    check_true("A·execute 签名含 timeout 关键字（工具层据此判定支持超时）",
               "timeout" in sig.parameters)
    check_true(
        "A·execute_accepts_timeout(后端类) is True（丢了 timeout 会让模型带 timeout 的调用被工具层直接拒）",
        execute_accepts_timeout(ValidatedCompositeBackend),
    )
    sig_a = inspect.signature(ValidatedCompositeBackend.aexecute)
    check_true("A·aexecute 签名含 timeout 关键字", "timeout" in sig_a.parameters)

    backend = _make_backend(_make_shell())
    check_true(
        "A·supports_execution(backend) is True（覆写没破坏工具层的运行时准入检查）",
        supports_execution(backend),
    )

    # 真 shell default 也必须是"可作为 default 的执行后端"，否则上面这条会假绿
    shell = _make_shell()
    check_true(
        "A·对照：LocalShellBackend 本身通过 supports_execution（替身无法通过，故未命中透传必须用真 shell）",
        supports_execution(_make_backend(shell)),
    )


def test_a_hint_contract() -> None:
    """A 的文案契约：必须把"环境错误"改写成"规则违反 + 正确通道 + 禁宣称"。"""
    print("\n[A] 拒绝文案契约")
    hint = execution_guard.virtual_path_write_hint(PROD_CP_CMD, REPORT_PREFIX)
    for phrase in (
        "禁止",              # 明确拒绝
        "并不存在",           # 打破"目录没建好"的误导
        "write_file",        # 通道 1
        "download_from_sandbox",  # 通道 2
        "禁止对用户声称",      # 反幻觉：没回执不许说可下载
        REPORT_PREFIX,       # 会话真实交付目录（backend 注入）
    ):
        check_true(f"A·文案含 {phrase!r}", phrase in hint)
    check_true("A·文案不提 exit code（避免又给一个被误读的失败信号）", "exit code" not in hint)
    check_true(
        "A·文案是给模型的指令（含'请改用正确通道'）",
        "请改用正确通道" in hint,
        hint[:60],
    )


# ─────────────────── B：轮次信号生命周期 ───────────────────


def test_b_signal_lifecycle() -> None:
    """B 的信号：note → peek → consume（pop 语义）→ 再取为空；clear 兜底。"""
    print("\n[B] 轮次信号生命周期")
    execution_guard.clear_execute_path_violation(USER, SID)
    check("B·初始为空", execution_guard.peek_execute_path_violation(USER, SID), None)

    execution_guard.note_execute_path_violation(USER, SID, PROD_MKDIR_CMD)
    peek = execution_guard.peek_execute_path_violation(USER, SID)
    check_true("B·note 后 peek 命中", peek is not None)
    check_true(
        "B·信号含命令摘要（排障用）",
        peek is not None and "项目团队任命书" in peek.get("command_head", ""),
    )
    check("B·peek 不消费（仍可 peek）",
          execution_guard.peek_execute_path_violation(USER, SID) is not None, True)

    got = execution_guard.consume_execute_path_violation(USER, SID)
    check_true("B·consume 取到信号", got is not None)
    check("B·consume 是 pop 语义（再取为空）",
          execution_guard.consume_execute_path_violation(USER, SID), None)
    check("B·消费后 peek 也为空",
          execution_guard.peek_execute_path_violation(USER, SID), None)

    # clear = 开轮清残留（防上一轮断连遗留被下一轮误当自己的信号）
    execution_guard.note_execute_path_violation(USER, SID, "cp x /reports/u/s/a")
    execution_guard.clear_execute_path_violation(USER, SID)
    check("B·clear 清掉残留", execution_guard.peek_execute_path_violation(USER, SID), None)

    # 跨会话隔离
    execution_guard.note_execute_path_violation(USER, SID, "cp x /reports/u/s/a")
    check("B·跨会话隔离（别的会话不受影响）",
          execution_guard.peek_execute_path_violation(USER, "other-session"), None)
    check("B·跨用户隔离",
          execution_guard.peek_execute_path_violation("other-user", SID), None)
    execution_guard.clear_execute_path_violation(USER, SID)

    # 摘要截断
    execution_guard.note_execute_path_violation(USER, SID, "x" * 900)
    long_peek = execution_guard.peek_execute_path_violation(USER, SID)
    check("B·command_head 截断到 300 字",
          len((long_peek or {}).get("command_head", "")), 300)
    execution_guard.clear_execute_path_violation(USER, SID)


def test_b_window_cap() -> None:
    """B 的表必须有窗口上限：无界 dict 会在长跑进程里慢性泄漏。"""
    print("\n[B] 信号表窗口上限（防泄漏）")
    table = execution_guard._VIOLATIONS
    saved = dict(table)
    try:
        table.clear()
        for i in range(execution_guard._VIOLATION_WINDOW + 20):
            execution_guard.note_execute_path_violation("u%d" % i, "s", "cmd")
        check(
            "B·写入超过窗口后长度被截到上限",
            len(table),
            execution_guard._VIOLATION_WINDOW,
        )
        check_true(
            "B·保留的是**最新**条目（LRU 淘汰最旧）",
            execution_guard.peek_execute_path_violation("u%d" % (execution_guard._VIOLATION_WINDOW + 19), "s") is not None,
        )
        check(
            "B·最旧条目已被淘汰",
            execution_guard.peek_execute_path_violation("u0", "s"),
            None,
        )
    finally:
        table.clear()
        table.update(saved)


def test_b_backend_note_on_reject() -> None:
    """A→B 串联：backend 命中拒绝时**同时**登记信号（B 的触发源）。"""
    print("\n[B] A→B 串联：命中即登记信号")
    execution_guard.clear_execute_path_violation(USER, SID)
    backend = _make_backend(SpyDefault())

    _expect_value_error(backend.execute, PROD_MKDIR_CMD)
    peek = execution_guard.peek_execute_path_violation(USER, SID)
    check_true("B·sync 命中后信号已登记", peek is not None)

    execution_guard.clear_execute_path_violation(USER, SID)
    _expect_value_error(lambda: asyncio.run(backend.aexecute(PROD_CP_CMD)))
    peek2 = execution_guard.peek_execute_path_violation(USER, SID)
    check_true("B·async 命中后信号已登记", peek2 is not None)

    # 未命中的命令不产生信号（否则每轮都会多扫一次沙箱）。
    # ⚠️ 必须用真 shell 后端：SpyDefault 过不了 supports_execution，未命中会走
    #    super().execute() 的协议检查并抛 NotImplementedError（替身只能测"命中"路径）。
    execution_guard.clear_execute_path_violation(USER, SID)
    shell_backend = _make_backend(_make_shell())
    shell_backend.execute("echo harmless")
    check("B·未命中命令不登记信号",
          execution_guard.peek_execute_path_violation(USER, SID), None)
    execution_guard.clear_execute_path_violation(USER, SID)


def test_b_salvage_driven_by_signal() -> None:
    """B 的救济本体：信号驱动的沙箱差集回收（复用已上线的 _salvage_sandbox_artifacts）。

    场景 = 生产事故的"可救"版本：模型把产物写在沙箱 /home/user/（未走 download），
    且**另有一个上一轮遗留文件**（必须不被误拉）。
    """
    print("\n[B] 信号驱动收尾救济：沙箱产物拉回宿主")
    stale = "/home/user/上一轮遗留.md"
    fresh = "/home/user/项目团队任命书.pptx"
    content = b"PK\x03\x04fake-pptx-bytes-after-guard"
    sandbox = FakeSandbox({stale: b"old", fresh: content})
    before = {stale: 3}  # 上一轮就在 → 差集应排除

    with _TempReportRoot() as root:
        execution_guard.note_execute_path_violation(USER, SID, PROD_MKDIR_CMD)
        signal = execution_guard.consume_execute_path_violation(USER, SID)
        check_true("B·收尾消费到信号（触发强制兜底）", signal is not None)

        salvaged = asyncio.run(
            _salvage_sandbox_artifacts(sandbox, USER, SID, before)
        )
        names = sorted(s["file_name"] for s in salvaged)
        check("B·只回收本轮新增（上一轮遗留不误拉）", names, ["项目团队任命书.pptx"])
        check(
            "B·虚拟路径格式正确",
            [s["file_path"] for s in salvaged],
            [f"{REPORT_PREFIX}项目团队任命书.pptx"],
        )
        check("B·大小取真实字节数", [s["file_size"] for s in salvaged], [len(content)])
        disk = os.path.join(root, USER, SID, "项目团队任命书.pptx")
        check_true(f"B·文件已落宿主持久卷 {disk}", os.path.isfile(disk))
        check_true("B·内容一致（二进制安全）", open(disk, "rb").read() == content)
        check_true(
            "B·存量遗留文件没有被写进宿主",
            not os.path.exists(os.path.join(root, USER, SID, "上一轮遗留.md")),
        )

    check("B·信号已被消费（不会泄漏到下一轮）",
          execution_guard.peek_execute_path_violation(USER, SID), None)


# ─────────── A：拒绝结果的失败判定（护栏文案不得被当成功）───────────


def test_a_tool_result_error_judgement() -> None:
    """护栏拒绝必须是 is_error=True，否则拒绝文案里的 /reports/... 会被解析成幽灵卡片。

    （2026-09-29 活体 e2e 实测：旧判定只认 "command failed with exit code" /
    "Execution error:"，而护栏抛 ValueError → `Error: Invalid parameter. {文案}`，
    两头不沾 → 判成功 → 产出 file_size=0 的卡片。）
    """
    print("\n[A] 工具结果失败判定（status 优先）")
    hint = execution_guard.virtual_path_write_hint("cp x /reports/u/s/a", REPORT_PREFIX)

    # ── 护栏拒绝现场 ──
    reject_content = "Error: Invalid parameter. " + hint
    check_true(
        "A·护栏拒绝（status=error）判为失败",
        _tool_result_is_error("execute", reject_content, "error"),
    )
    check_true(
        "A·诚实记录：同样的文案**若 status 缺失**，关键词兜底判不出来"
        "（这正是必须让 status 优先的原因；将来若强化关键词兜底，此断言会翻转，属预期变更）",
        _tool_result_is_error("execute", reject_content, None) is False,
    )
    check_true(
        "A·空内容 + status=error 也判失败",
        _tool_result_is_error("execute", "", "error"),
    )

    # ── execute 关键词兜底（老版本/无 status 时的回归位）──
    check_true(
        "A·execute 真失败（exit code 1）判失败",
        _tool_result_is_error("execute", "boom\n[Command failed with exit code 1]", "success"),
    )
    check_true(
        "A·execute 成功判成功",
        _tool_result_is_error("execute", "ok\n[Command succeeded with exit code 0]", "success") is False,
    )

    # ── read_file 的历史误判回归位（2026-08-19）──
    check_true(
        "A·read_file 成功正文含 'No such file' 仍判成功（内容型工具不能靠关键词）",
        _tool_result_is_error(
            "read_file", "  25→示例：cp: No such file or directory", "success"
        ) is False,
    )
    check_true(
        "A·read_file 失败以 'Error: ' 开头判失败",
        _tool_result_is_error("read_file", "Error: file not found", "success"),
    )

    # ── MCP 结构化 JSON（历史误判回归位："error": null）──
    check_true(
        "A·MCP JSON success=true 且 error=null 判成功",
        _tool_result_is_error("mcp_tool", '{"success": true, "error": null}', "success") is False,
    )
    check_true(
        "A·MCP JSON success=false 判失败",
        _tool_result_is_error("mcp_tool", '{"success": false, "error": "x"}', "success"),
    )


# ─────────── B：调用点接入契约（AST 静态断言，2026-09-29 补）───────────
#
# 为什么必须单独测"调用点"：本套件原先只测 execution_guard 的**模块行为**
# （clear/peek/consume 各自调了都对），但没有任何一条断言它们**被 chat.py 调用**。
# 结果 1.0.22 首轮构建就抓到这个缺口：clear_execute_path_violation 只有定义、
# 全文零调用点 —— 开轮清残留这一环完全没落地，而模块行为测试全绿。
# 教训与 2026-09-17 那次同源：**模块级正确 ≠ 被正确接入**。
# 这类缺口用一个 AST 断言就能永久挡住，成本极低，故固化在此。


def _chat_source_path() -> str:
    """取 chat.py 的真实源码路径（不依赖 cwd，镜像内/本机都能定位）。"""
    import src.api.endpoints.chat as _chat  # noqa: PLC0415
    return inspect.getsourcefile(_chat) or ""


def _guard_call_sites(path: str) -> dict[str, list[int]]:
    """AST 解析 chat.py，返回 {execution_guard 方法名: [行号...]}。

    只认**真实调用节点**（ast.Call），注释与字符串天然不会被匹配到 ——
    这正是"反向标记命中 ≠ 有旧代码（可能是注释）"那个坑的正解。
    """
    import ast  # noqa: PLC0415

    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # 形如 execution_guard.<name>(...)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "execution_guard"
        ):
            found.setdefault(func.attr, []).append(node.lineno)
    return found


def test_b_call_site_wiring() -> None:
    print("\n── B：调用点接入契约（chat.py AST 静态断言）──")

    path = _chat_source_path()
    check_true("B·能定位 chat.py 源码路径", bool(path) and os.path.exists(path), path)
    sites = _guard_call_sites(path)

    # 先自证解析到的是真的 chat.py（而不是空文件/桩文件）
    import ast  # noqa: PLC0415

    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    check_true(
        "B·AST 解析到真实 chat.py（含 _tool_result_is_error 定义）",
        any(
            isinstance(n, ast.FunctionDef) and n.name == "_tool_result_is_error"
            for n in ast.walk(ast.parse(src, filename=path))
        ),
    )

    # ① 开轮清残留必须被调用（本次缺口本体）
    check_true(
        "B·chat.py 开轮调用 clear_execute_path_violation（防跨轮串味）",
        len(sites.get("clear_execute_path_violation", [])) >= 1,
        "命中行=%s" % sites.get("clear_execute_path_violation", []),
    )
    # ② 收尾消费点必须在
    peek_lines = sites.get("peek_execute_path_violation", [])
    consume_lines = sites.get("consume_execute_path_violation", [])
    check_true("B·chat.py 收尾 peek 存在", len(peek_lines) >= 1, "命中行=%s" % peek_lines)
    check_true(
        "B·chat.py 收尾 consume 存在（唯一消费者）", len(consume_lines) >= 1,
        "命中行=%s" % consume_lines,
    )
    # ③ 顺序契约：开轮清必须**早于**收尾 peek/consume
    #    否则"清除"发生在信号已被消费之后，等于没清。
    clear_lines = sites.get("clear_execute_path_violation", [])
    if clear_lines and peek_lines and consume_lines:
        check_true(
            "B·顺序契约：clear(开轮) < peek(收尾) < consume(收尾)",
            min(clear_lines) < min(peek_lines) <= max(consume_lines),
            "clear=%d peek=%d consume=%d" % (min(clear_lines), min(peek_lines), min(consume_lines)),
        )
    else:
        check_true("B·顺序契约：clear < peek < consume", False, "前置调用点缺失，无法判序")

    # ④ 反向：agent.py 的 backend 覆写必须是**同步+异步成对**（漏一个等于半开护栏）
    src_agent = "/app/src/services/agent.py"
    if not os.path.exists(src_agent):
        # 本机跑（非镜像）时按仓库相对路径兜底
        src_agent = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "src", "services", "agent.py")
    if os.path.exists(src_agent):
        with open(src_agent, "r", encoding="utf-8") as fh:
            agent_tree = ast.parse(fh.read(), filename=src_agent)
        methods = {
            n.name
            for n in ast.walk(agent_tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        check_true("B·agent.py 覆写 execute（同步）", "execute" in methods)
        check_true("B·agent.py 覆写 aexecute（异步）", "aexecute" in methods)
    else:
        check_true("B·能定位 agent.py 以核对 backend 覆写", False, src_agent)

    # ⑤ 合同：note_* 只由 backend 调用（不在 API 层登记，避免职责漂移）
    check_true(
        "B·note_execute_path_violation 不在 chat.py 出现（登记职责属 backend）",
        "note_execute_path_violation" not in sites,
        "chat.py 命中=%s" % sites.get("note_execute_path_violation", []),
    )


# ─────────────────────────── main ───────────────────────────


def main() -> int:
    print("=" * 78)
    print("Spike: execute 虚拟路径护栏（A 拒绝 + B 救济）")
    print("=" * 78)
    test_a_regex_matrix()
    test_a_reject_and_passthrough()
    test_a_signature_and_identity_contract()
    test_a_hint_contract()
    test_a_tool_result_error_judgement()
    test_b_signal_lifecycle()
    test_b_window_cap()
    test_b_backend_note_on_reject()
    test_b_salvage_driven_by_signal()
    test_b_call_site_wiring()
    for _d in _SHELL_TMPDIRS:
        shutil.rmtree(_d, ignore_errors=True)
    print()
    print("=" * 78)
    print("PASS=%d  FAIL=%d" % (PASS, FAIL))
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

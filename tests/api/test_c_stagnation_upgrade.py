# -*- coding: utf-8 -*-
"""C 停滞检测升级（2026-11-18，会话 GY22328:59bbcbe3 事故四项修复）的接线与行为测试。

事故复盘（生产 docker logs + store 双重取证）：
    Excel 伺服提取任务，成品 v1.xlsx（137 行、断言 PASS）一直写在沙箱
    /home/user/ 下、从未 download_from_sandbox —— C 检测器只认 /reports/
    新文件 ⇒ 第二次攒满 15 次阈值后 ToolLoopAbortError 硬终止：
      · 10:40:12 B② 收尾兜底把用户**上传的** input.xlsx 当"新产物"拉回发卡片
        （基线取在 run 前、上传发生在 run 内 ⇒ 上传件必然落进前后差集）；
      · 假交付物让 M3 完成门按"产物齐放行"跳过 ⇒ run 标 completed/error=null；
      · 红条只是瞬态 SSE error，store 里对话戛然而止于最后一条工具结果。

四项修复（本文件锁接线 + 行为）：
    B1 证据签名：沙箱**结论类**文件新增/变化 = 客观进展，停滞计数清零
       （.py 脚本不在 _SALVAGE_EXTS 内，"反复重写脚本"的空转仍被 C 抓住）；
    B2 软停：第二次攒满不再直接 ToolLoopAbortError，先注入强制收尾轮
       （StagnationWrapupError），收尾轮仍停滞才真终止；
    A2 兜底排除上传件：_salvage_sandbox_artifacts 增加 exclude 参数，
       调用点传入本轮 upload_to_sandbox 收集的沙箱路径；
    A3 兜底机会改每轮：while 顶部重置 _salvage_done，硬终止轮有第二次兜底；
    A4 终止落库：终止说明持久化为 AI 消息（_write_session_notice，必须在
       _persist_session 之后）+ run 终态标 failed + error 字段落库；
    A1 收敛提示补"沙箱已有成品立即 download 交付"条款。

运行：
    C:/Users/GY24428/.workbuddy/binaries/python/envs/cubesandbox-verify/Scripts/python.exe \\
      -m pytest tests/api/test_c_stagnation_upgrade.py -q
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CHAT_PY = PROJECT_ROOT / "src" / "api" / "endpoints" / "chat.py"

USER = "GY22328"
SID = "59bbcbe3"


def _tree() -> ast.Module:
    return ast.parse(CHAT_PY.read_text(encoding="utf-8"), filename=str(CHAT_PY))


def _src() -> str:
    return CHAT_PY.read_text(encoding="utf-8")


def _func_def(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _section(src: str, start_marker: str, end_marker: str) -> str:
    """截取两个稳定注释锚点之间的源码段（锚点不存在时测试直接失败）。"""
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


# ═══════════════════ B1：证据签名接线 ═══════════════════


def test_b1_evidence_scan_wired_into_c_branch():
    """C 计数段必须接上证据签名扫描：检查点条件 + 结论类枚举 + 进展清零。"""
    src = _src()
    c_section = _section(
        src,
        "# ─── C：交付物停滞计数（2026-10-17，2026-10-20 补进展豁免）───",
        "# ─── 检测工具返回的文件信息，提取生成/下载的文件 ───",
    )
    # ① 扫描只在检查点（阈值-3）触发，且被 bg 豁免与 sandbox 空值门控
    assert "no_progress_stagnation_calls - 3" in c_section, (
        "B1 扫描没有挂在检查点（阈值-3）——每次工具结果都扫会拖慢整轮流"
    )
    assert "not _bg_progress_ok" in c_section, "B1 扫描必须被后台进展豁免门控"
    # ② 枚举走 to_thread（同步网络调用不得裸调冻住事件循环）
    assert "asyncio.to_thread(\n                                        _sandbox_artifact_listing" in c_section or (
        "_sandbox_artifact_listing, sandbox" in c_section
    ), "B1 必须用 asyncio.to_thread 包住 _sandbox_artifact_listing"
    # ③ 证据变化 → 计数清零（进展判据本体）
    assert "_stagnation_calls = 0" in c_section, "B1 证据变化分支必须清零 _stagnation_calls"
    # ④ 排除本轮上传件（防一次 upload 把计数洗白）
    assert "p not in _uploaded_sandbox_paths" in c_section, (
        "B1 签名必须排除 _uploaded_sandbox_paths"
    )
    # ⑤ 首次扫描只建基线不清零（None 哨兵）
    assert "_sandbox_sig_last is None" in c_section, "B1 必须区分'建基线'与'比对'"


def test_b1_listing_excludes_scripts_by_construction():
    """证据签名复用的 _sandbox_artifact_listing 只列结论类扩展名——脚本不算进展。

    这条是 B1 不重开 83345f0c 漏洞（反复重写脚本空转）的结构性保证：
    .py / .log 不在 _SALVAGE_EXTS 里，find 的 -name 子句天然不含它们。
    """
    src = _src()
    i = src.index("_SALVAGE_EXTS = (")
    j = src.index(")", i)
    exts_blob = src[i:j]
    assert '".py"' not in exts_blob and "'py'" not in exts_blob, (
        "_SALVAGE_EXTS 不应包含 .py —— 脚本重写必须继续被判为停滞"
    )
    for must in ("xlsx", "docx", "pptx", "md", "csv"):
        assert '"%s"' % must in exts_blob, "结论类扩展名缺失: %s" % must


# ═══════════════════ B2：软停接线 ═══════════════════


def test_b2_wrapup_phase_before_hard_abort():
    """C 二次阈值：先判 _stagnation_wrapup_used 走软停，真终止在其后。"""
    src = _src()
    c_section = _section(
        src,
        "# ─── C：交付物停滞计数（2026-10-17，2026-10-20 补进展豁免）───",
        "# ─── 检测工具返回的文件信息，提取生成/下载的文件 ───",
    )
    wrapup_pos = c_section.find("if not _stagnation_wrapup_used:")
    assert wrapup_pos >= 0, "C 二次阈值分支缺少软停判据 if not _stagnation_wrapup_used"
    abort_pos = c_section.find("raise ToolLoopAbortError(", wrapup_pos)
    assert abort_pos > wrapup_pos, "真终止（ToolLoopAbortError）必须位于软停判据之后"
    wrapup_abort = c_section[wrapup_pos:abort_pos]
    assert "StagnationWrapupError(" in wrapup_abort, (
        "软停分支必须抛 StagnationWrapupError（走收敛注入通道）"
    )
    assert "break" in wrapup_abort, "软停分支必须 break 延迟到 super-step 提交后"
    assert "_stagnation_wrapup_used = True" in wrapup_abort, (
        "软停分支必须置位一次性标记，收尾轮再停滞才真终止"
    )


def test_b2_wrapup_exception_is_no_progress_subclass():
    """StagnationWrapupError 必须继承 NoProgressAbortError——复用收敛注入通道。"""
    tree = _tree()
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "StagnationWrapupError":
            target = node
            break
    assert target is not None, "缺少 StagnationWrapupError 类定义"
    bases = {getattr(b, "id", getattr(b, "attr", None)) for b in target.bases}
    assert "NoProgressAbortError" in bases, (
        "StagnationWrapupError 必须继承 NoProgressAbortError，"
        "否则 except NoProgressAbortError 分支接不住它"
    )


def test_b2_wrapup_prompt_targets_delivery_and_summary():
    """收尾文案必须落到三件事：停止新工作 / 先 download 已有成品 / 文字总结。"""
    src = _src()
    assert "最后一轮机会" in src, "收尾文案缺少'最后一轮机会'措辞"
    assert "停止一切探查/自检/重新生成类工具调用" in src
    assert "download_from_sandbox 拉回" in src, "收尾文案必须指明先拉回成品交付"
    assert "未完成的部分及原因" in src, "收尾文案必须要求总结未完成部分（对齐 deepseek wrapup）"


# ═══════════════════ A1：收敛提示补条款 ═══════════════════


def test_a1_convergence_prompt_mentions_sandbox_delivery():
    """收敛提示必须补第 2 条：沙箱已有成品立即 download_from_sandbox 交付。"""
    src = _src()
    assert "2) 若沙箱内（如 /home/user/ 或其他工作目录）已有成品交付文件" in src, (
        "收敛提示缺少 A1 条款（GY22328:59bbcbe3 实测：只引导查 /reports/ 时"
        "模型 ls 完继续埋头自检）"
    )


# ═══════════════════ A2：兜底排除上传件 ═══════════════════


def _extract_sandbox_files(snapshot: dict) -> dict[str, bytes]:
    """FakeSandbox 需要的路径→内容表。"""
    return snapshot


class FakeSandbox:
    """最小沙箱替身（同 test_salvage_artifacts.py 的结构）。"""

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.download_calls: list[list[str]] = []

    class _Resp:
        def __init__(self, content=None, output=""):
            self.content = content
            self.output = output
            self.exit_code = 0

    def execute(self, command: str, timeout: int | None = None) -> "FakeSandbox._Resp":
        lines = ["%s\t%d" % (p, len(c)) for p, c in sorted(self.files.items())]
        return self._Resp(output="\n".join(lines) + ("\n" if lines else ""))

    def download_files(self, paths: list[str]) -> list["FakeSandbox._Resp"]:
        self.download_calls.append(list(paths))
        return [self._Resp(content=self.files.get(p)) for p in paths]


@pytest.fixture()
def report_root(tmp_path, monkeypatch):
    from src.core.config import settings

    monkeypatch.setattr(settings, "report_root", str(tmp_path))
    return str(tmp_path)


def test_a2_salvage_excludes_uploaded_input(report_root):
    """A2：上传件 input.xlsx 必须被 exclude 挡住，真成品照常拉回。

    事故原形：基线取在 run 前、上传发生在 run 内 ⇒ input.xlsx 落进前后差集，
    被当"本轮新产物"拉回宿主 + 发卡片（用户 10:43:45 点开的就是这张假卡片），
    还骗过 M3 完成门。
    """
    from src.api.endpoints.chat import _salvage_sandbox_artifacts

    files = {
        "/home/user/input.xlsx": b"U" * 765_691,  # 用户上传的原件
        "/home/user/伺服电机电缆选型_v1.xlsx": b"D" * 15_881,  # 真成品
    }
    salvaged = asyncio.run(
        _salvage_sandbox_artifacts(
            FakeSandbox(files), USER, SID, before={},
            exclude={"/home/user/input.xlsx"},
        )
    )
    got_paths = {s["file_path"] for s in salvaged}
    assert got_paths == {"/reports/%s/%s/伺服电机电缆选型_v1.xlsx" % (USER, SID)}, (
        "上传件被误捞（exclude 未生效）或真成品没被拉回: %s" % got_paths
    )


def test_a2_exclude_none_keeps_legacy_behavior(report_root):
    """exclude 缺省 None 时保持旧行为（既有调用点不传 exclude 也不回归）。"""
    from src.api.endpoints.chat import _salvage_sandbox_artifacts

    files = {"/home/user/报告.md": b"R" * 100}
    salvaged = asyncio.run(
        _salvage_sandbox_artifacts(FakeSandbox(files), USER, SID, before={})
    )
    assert len(salvaged) == 1


def test_a2_call_site_passes_uploaded_paths():
    """调用点必须把 _uploaded_sandbox_paths 传给兜底函数（接线）。"""
    src = _src()
    assert "exclude=_uploaded_sandbox_paths" in src, (
        "_salvage_sandbox_artifacts 调用点未传 exclude —— A2 未接线"
    )
    # 上传路径采集：两种上传工具都要覆盖
    for tool in ("upload_to_sandbox", "decrypt_and_upload_to_sandbox"):
        assert '"%s"' % tool in src, "上传路径采集缺少工具名: %s" % tool
    assert 'sandbox_path' in src, "上传路径采集未解析 sandbox_path 字段"


# ═══════════════════ A3：兜底机会改每轮 ═══════════════════


def test_a3_salvage_gate_resets_every_round():
    """while 顶部必须每轮重置 _salvage_done（硬终止轮要有第二次兜底机会）。"""
    src = _src()
    m = src.index("while True:")
    drain_pos = src.index("await _drain_astream(_graph_input)", m)
    body = src[m:drain_pos]
    assert "_salvage_done = False" in body, (
        "while True: 体内、_drain_astream 之前必须重置 _salvage_done（A3）"
    )


# ═══════════════════ A4：终止落库 ═══════════════════


def test_a4_finish_run_supports_error_field():
    """_finish_run 必须支持 error 参数并传给 run_registry.mark。"""
    tree = _tree()
    finish = _func_def(tree, "_finish_run")
    assert finish is not None
    args = [a.arg for a in finish.args.args] + [
        a.arg for a in finish.args.kwonlyargs
    ]
    assert "error" in args, "_finish_run 缺少 error 参数"
    marks = [
        n for n in ast.walk(finish)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "mark"
        and any(
            getattr(kw.value, "id", None) == "error" for kw in n.keywords
        )
    ]
    assert marks, "_finish_run 里的 run_registry.mark 调用未透传 error=..."


def test_a4_termination_note_persisted_after_persist_before_finish():
    """终止说明持久化的位置铁律：_persist_session 之后、_finish_run 之前。"""
    src = _src()
    i = src.index("await _persist_session(\n                agent, thread_id")
    note_pos = src.find("await _write_session_notice(", i)
    finish_pos = src.find("await _finish_run(\n                run_id,", i)
    assert note_pos > i, "终止说明写入必须在 _persist_session 之后（水位链安全）"
    assert 0 < finish_pos and note_pos < finish_pos, (
        "终止说明写入必须在 _finish_run（[DONE]）之前——前端收到 DONE 时消息必须已落盘"
    )


def test_a4_terminal_status_failed_on_hard_abort():
    """硬终止轮 run 终态必须标 failed（原实现无条件 completed/error=null）。"""
    src = _src()
    assert "STATUS_FAILED if _terminal_error" in src, (
        "_finish_run 调用处必须按 _terminal_error 分流 STATUS_FAILED"
    )


def test_a4_drain_sets_terminal_error_on_abort():
    """ToolLoopAbortError 处理器必须落 _terminal_error（否则 A4 全链失效）。"""
    src = _src()
    handler = _section(
        src,
        "except ToolLoopAbortError as e:",
        "except NoProgressAbortError as e:",
    )
    assert "_terminal_error = str(e)[:500]" in handler


def test_a4_notice_writer_appends_at_watermark_tail():
    """_write_session_notice 行为：水位尾部追加、旧格式拒绝、失败返回 False。"""
    from src.api.endpoints.chat import _write_session_notice

    class FakeStore:
        def __init__(self, *, legacy_rows=0, fail=False):
            self.legacy_rows = legacy_rows
            self.fail = fail
            self.written: list[tuple[str, dict]] = []

        async def aget_message_watermark(self, prefix):
            if self.fail:
                raise RuntimeError("db down")
            return (3, "last-id", self.legacy_rows)

        async def awrite_messages(self, namespace, items):
            if self.fail:
                raise RuntimeError("db down")
            for key, entry in items:
                self.written.append((key, entry))

    store = FakeStore()
    ok = asyncio.run(
        _write_session_notice(store, user_id=USER, session_id=SID, content="终止说明")
    )
    assert ok is True
    assert len(store.written) == 1
    key, entry = store.written[0]
    # 水位 prev_count=3 ⇒ 写在 key=4（尾部追加，不覆盖既有条目）
    assert key == "00000004", "公告必须写在水位 prev_count+1 处: key=%s" % key
    assert entry["role"] == "ai"
    assert "终止说明" in entry["content"]
    assert entry["id"], "合成条目必须有 id（水位 by_id 链依赖 id 存在）"

    legacy = FakeStore(legacy_rows=7)
    assert asyncio.run(
        _write_session_notice(legacy, user_id=USER, session_id=SID, content="x")
    ) is False, "存量旧格式必须拒绝写入（部署顺序防线）"

    broken = FakeStore(fail=True)
    assert asyncio.run(
        _write_session_notice(broken, user_id=USER, session_id=SID, content="x")
    ) is False, "水位读取/写入失败必须返回 False（不抛异常阻断收尾）"


# ═══════════════════ 状态声明完备性 ═══════════════════


def test_new_state_declared_and_nonlocal_wired():
    """新增状态变量必须有声明 + nonlocal（缺 nonlocal = 闭包赋值 UnboundLocalError）。"""
    src = _src()
    for var in (
        "_stagnation_wrapup_used",
        "_sandbox_sig_last",
        "_uploaded_sandbox_paths",
        "_terminal_error",
    ):
        assert "%s =" % var.split("[")[0] in src or "%s:" % var.split("[")[0] in src, (
            "缺少状态声明: %s" % var
        )
    drain = _func_def(_tree(), "_drain_astream")
    assert drain is not None
    nonlocal_names: set[str] = set()
    for node in ast.walk(drain):
        if isinstance(node, ast.Nonlocal):
            nonlocal_names |= set(node.names)
    for var in ("_stagnation_wrapup_used", "_sandbox_sig_last", "_terminal_error"):
        assert var in nonlocal_names, (
            "_drain_astream 的 nonlocal 缺少 %s —— 闭包内赋值会 UnboundLocalError" % var
        )

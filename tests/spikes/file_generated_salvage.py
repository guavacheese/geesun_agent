"""Spike: 交付物_幻觉卡片 的 A/B/C 三处修复回归位（2026-09-29 新增）。

背景（生产会话 GY35377:60c64c4f 实证，报告见
`geesun_agent_web/.workbuddy/reports/pptx-claim-vs-download-60c64c4f-2026-09-29.html`）：
    模型用 execute 在**沙箱内**生成 pptx，再在沙箱内 `mkdir -p /reports/... && cp ...`
    （exit 0）就宣称"已保存、可直接下载"；宿主报告目录一个文件都没有，用户点下载必然 404。
    同一会话还暴露第二个病：正文里出现过的 /reports/... 路径会被"老数据补全"
    **无条件**补成 file_size=0 的卡片 → 前端 HEAD 404 → 红框"文件不可用"，
    与同屏"本轮未产出交付物"横幅自相矛盾。

本 spike 直接 import **真实函数**（不是复制一份实现），覆盖：
    A  交付物写入通道提示词的存在性（tripwire：被删即 FAIL）
    B① 报错结果里的路径 → 清洗 → 沙箱拉回（生产原始报错文本做夹具）
    B② 沙箱前后差集枚举 + 拉回落盘（含"不误拉上一轮遗留"）
    C① 正文补全只认磁盘真实存在的文件（幻觉路径不再实体化）
    C② 拦截文案明确否定"已保存/可直接下载"

运行环境：**生产同款镜像**（chat.py/sessions.py 依赖 fastapi 等）：
  docker run --rm -v 'D:/workspace/geesun_agent/src:/app/src:ro' \
    -v 'D:/workspace/geesun_agent/tests:/app/tests:ro' --entrypoint sh \
    172.16.220.74:8333/geesun_ai/geesun-agent:1.0.20 \
    -c 'cd /app && /app/.venv/bin/python tests/spikes/file_generated_salvage.py'
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from typing import Any

sys.path.insert(0, "/app")

from src.api.endpoints.chat import (  # noqa: E402
    _REPORTS_PATH_RE,
    _SALVAGE_DIRS,
    _SALVAGE_EXTS,
    _SALVAGE_MAX_FILES,
    _pull_report_from_sandbox,
    _salvage_sandbox_artifacts,
    _sandbox_artifact_listing,
    _sanitize_generated_path,
    _PARSE_ERROR_TOOL_RESULTS,
)
from src.api.endpoints.sessions import _scan_content_files  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.infra.reports import find_session_file, session_file_roots  # noqa: E402

PASS = 0
FAIL = 0
USER = "u1"
SID = "s1"

# 生产原始报错文本（会话 60c64c4f key=46，cp 因沙箱内无 /reports 目录而 exit 1）
PROD_CP_ERROR = (
    "cp: cannot create regular file "
    "'/reports/GY35377/60c64c4f/项目团队任命书.pptx': No such file or directory\n"
    "[Command failed with exit code 1]\n"
    "<stderr></stderr>"
)
PROD_PPTX = "项目团队任命书.pptx"


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


# ─────────────────────────── 沙箱替身 ───────────────────────────


class FakeResp:
    """对齐 langchain_cubesandbox 的 ExecuteResponse / FileDownloadResponse。"""

    def __init__(self, output: str = "", exit_code: int = 0, content: bytes | None = None):
        self.output = output
        self.exit_code = exit_code
        self.content = content


class FakeSandbox:
    """最小沙箱替身：只实现本仓实际用到的 execute / download_files。

    `files` 模拟沙箱文件系统 {绝对路径: 内容}。execute 的**唯一**用途是枚举
    （find），故这里把 files 的键按行吐回去（`路径\\t大小`），与真实 find -printf
    的输出格式一致。扩展名白名单由 find 命令自身承担（真实沙箱里也是），
    本替身不重复实现 —— 白名单契约另有"命令行断言"覆盖。
    """

    def __init__(self, files: dict[str, bytes] | None = None, exec_error: bool = False):
        self.files = dict(files or {})
        self.exec_error = exec_error
        self.commands: list[str] = []
        self.download_calls: list[list[str]] = []

    def execute(self, command: str, timeout: int | None = None) -> FakeResp:
        self.commands.append(command)
        if self.exec_error:
            return FakeResp(output="find: 沙箱已回收", exit_code=1)
        lines = [f"{p}\t{len(c)}" for p, c in sorted(self.files.items())]
        return FakeResp(output="\n".join(lines) + ("\n" if lines else ""))

    def download_files(self, paths: list[str]) -> list[FakeResp]:
        self.download_calls.append(list(paths))
        return [FakeResp(content=self.files.get(p)) for p in paths]


class _TempReportRoot:
    """把 settings.report_root 指到临时目录（被测函数直接读 settings），退出即还原。"""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="salvage_")
        self.saved = settings.report_root

    def __enter__(self) -> str:
        settings.report_root = self.tmp
        return self.tmp

    def __exit__(self, *exc: Any) -> None:
        settings.report_root = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)


# ─────────────────────────── A：提示词 tripwire ───────────────────────────


def test_a_prompt_contract() -> None:
    """A 的契约：path_hint 必须写明"shell 不能写 /reports、shell 产物必须 download_from_sandbox"。

    这是 tripwire（源码断言）而非行为断言 —— path_hint 在 chat() 里内联拼接，
    行为级验证要跑完整 endpoint（代价不成比例）。删掉这几句即 FAIL。
    """
    print("\n[A] 交付物写入通道提示词")
    src = open("/app/src/api/endpoints/chat.py", encoding="utf-8").read()
    for phrase in (
        "交付物写入通道",
        "禁止用 execute/shell 往 /reports/...",
        "没有挂载",
        "download_from_sandbox 拉回",
        "禁止声称",
    ):
        check_true(f"A·提示词含 {phrase!r}", phrase in src)
    check_true(
        "A·提示词仍在 path_hint 内（未被挪到别处）",
        "交付物写入通道" in src.split("path_hint = (", 1)[1].split("user_message =", 1)[0],
    )


# ─────────────────────────── B①：报错路径 → 拉回 ───────────────────────────


def test_b1_extract_and_sanitize() -> None:
    """B① 前置：生产报错文本里的路径能被干净地抠出来（带 `':` 尾巴也要剥掉）。"""
    print("\n[B①] 报错结果里的路径提取与清洗")
    m = _REPORTS_PATH_RE.search(PROD_CP_ERROR)
    check_true("B1·正则能在 cp 报错里命中路径", m is not None)
    raw = m.group(0)
    # 正则不把 `'` 当分隔符 → 会多吃一个 `':`，这是预期内的，由清洗负责剥掉
    check_true(f"B1·原始提取带脏尾巴（{raw!r}）", raw.endswith("':") or raw.endswith("'"))
    cleaned = _sanitize_generated_path(raw)
    check("B1·清洗后 == 生产真实路径", cleaned, f"/reports/GY35377/60c64c4f/{PROD_PPTX}")

    check("B1·尾随单引号（2026-08-24 老场景）", _sanitize_generated_path("x/diff.json'"), "x/diff.json")
    check("B1·尾随空白", _sanitize_generated_path("  /reports/a/b/c.md \n"), "/reports/a/b/c.md")
    check("B1·空串原样返回", _sanitize_generated_path(""), "")
    check("B1·路径中间的冒号不动", _sanitize_generated_path("/reports/u/s/a:b.md"), "/reports/u/s/a:b.md")


def test_b1_pull_back_from_sandbox() -> None:
    """B① 主链：报错里声称的 /reports/... 不存在，但文件在沙箱 /home/user/ 下 → 应拉回。"""
    print("\n[B①] 报错路径 → 沙箱拉回 → 落盘宿主")
    content = b"PK\x03\x04fake-pptx-bytes"
    sandbox = FakeSandbox({f"/home/user/{PROD_PPTX}": content})
    with _TempReportRoot() as root:
        size = asyncio.run(
            _pull_report_from_sandbox(
                sandbox, f"/reports/GY35377/60c64c4f/{PROD_PPTX}", "GY35377", "60c64c4f", PROD_PPTX
            )
        )
        check("B1·返回落盘字节数", size, len(content))
        disk = os.path.join(root, "GY35377", "60c64c4f", PROD_PPTX)
        check_true(f"B1·文件已落宿主持久卷 {disk}", os.path.isfile(disk))
        check_true("B1·内容一致（二进制安全）", open(disk, "rb").read() == content)
    check_true(
        "B1·候选按序命中即短路（第一条报错路径落空后试 /home/user/reports/...，"
        "命中第三条 /home/user/<basename> 前共试 3 条）",
        1 <= len(sandbox.download_calls) <= 4,
        f"calls={len(sandbox.download_calls)}",
    )


def test_b1_guard_flag_on() -> None:
    """B① 的开关必须处于打开态，否则报错结果根本进不了解析分支。"""
    print("\n[B①] 解析开关与尺寸来源契约")
    check("B1·_PARSE_ERROR_TOOL_RESULTS", _PARSE_ERROR_TOOL_RESULTS, True)
    src = open("/app/src/api/endpoints/chat.py", encoding="utf-8").read()
    check_true("B1·解析门已放开报错结果", "if not is_error or _PARSE_ERROR_TOOL_RESULTS:" in src)
    check_true(
        "B1·emit 护栏在位（报错来源只有拉回成功才发卡片）",
        "if is_error and file_size == 0:" in src and "报错结果中的路径未拉回成功" in src,
    )
    check_true(
        "B1·报错结果不采信返回值里的 size（否则护栏被绕过）",
        "if file_size == 0 and not is_error:" in src,
    )


# ─────────────────────────── B②：沙箱差集兜底 ───────────────────────────


def test_b2_listing_command_contract() -> None:
    """B② 的扩展名白名单/排除项由 find 命令承担，这里断言命令本身。"""
    print("\n[B②] 枚举命令契约")
    sandbox = FakeSandbox({"/home/user/a.md": b"x"})
    _sandbox_artifact_listing(sandbox)
    cmd = sandbox.commands[-1]
    for d in _SALVAGE_DIRS:
        check_true(f"B2·命令覆盖目录 {d}", d in cmd)
    # ⚠️ 回归位（2026-09-29 真沙箱实测）：必须先把不存在的目录滤掉再 find。
    # 新沙箱没有 /reports（要模型 mkdir 才有），GNU find 遇到不存在的起始路径
    # exit=1 → 整条命令被判失败 → 兜底永久静默失效。
    check_true("B2·先探测目录存在性（[ -d ] 过滤）", '[ -d "$d" ]' in cmd)
    check_true("B2·用变量 D 收集存在的目录", "D=''" in cmd and "for d in " in cmd)
    check_true("B2·find 只吃 D（存在目录集合）", "find $D " in cmd)
    check_true("B2·无存在目录时整条命令退出码为 0（if 无 else）", cmd.rstrip().endswith("fi"))
    check_true("B2·旧的裸目录写法已消除（否则 /reports 缺失即全挂）",
               "find %s " % " ".join(_SALVAGE_DIRS) not in cmd)
    for ext in ("pptx", "xlsx", "pdf", "md", "csv", "docx"):
        check_true(f"B2·白名单含 .{ext}", f"-name '*.{ext}'" in cmd)
    for noise in ("__pycache__", "site-packages", "node_modules", ".git"):
        check_true(f"B2·排除 {noise}", noise in cmd)
    check_true("B2·用 -printf 取 路径+大小", "-printf '%p\\t%s\\n'" in cmd, cmd[:60])
    check_true("B2·限定 maxdepth（防扫全盘）", "-maxdepth 4" in cmd)
    # 脚本/日志不是交付物，绝不能进白名单
    for non in ("py", "sh", "log", "lock"):
        check_true(f"B2·白名单**不含** .{non}", f"-name '*.{non}'" not in cmd)


def test_b2_listing_parse() -> None:
    print("\n[B②] 枚举结果解析")
    sandbox = FakeSandbox({
        "/home/user/报告.md": b"0123456789",
        "/tmp/表 格.xlsx": b"abc",
        "/reports/u/s/sub/deep.pdf": b"x" * 300,
    })
    got = _sandbox_artifact_listing(sandbox)
    check("B2·三个文件都解析出来", sorted(got), sorted(sandbox.files))
    check("B2·大小正确", got["/home/user/报告.md"], 10)
    check("B2·含空格/中文路径 OK", got["/tmp/表 格.xlsx"], 3)

    check("B2·空沙箱 → {}（不是 None）", _sandbox_artifact_listing(FakeSandbox({})), {})
    check("B2·sandbox=None → {}", _sandbox_artifact_listing(None), {})
    check("B2·命令失败 → {}（放弃兜底，不抛）",
          _sandbox_artifact_listing(FakeSandbox({"/x.md": b"y"}, exec_error=True)), {})
    check("B2·非 / 开头的脏行被丢弃",
          _sandbox_artifact_listing(FakeSandbox({"relative.md": b"z"})), {})


def test_b2_salvage_diff_and_write() -> None:
    """B② 主链：只回收"本轮新增/改写"，上一轮遗留不重复搬。"""
    print("\n[B②] 差集回收 + 落盘")
    old = b"old-turn-artifact"
    overwritten = b"same-path-different-size-XXXX"
    fresh = b"brand-new-deliverable"
    sandbox = FakeSandbox({
        "/home/user/上轮遗留.pdf": old,        # 基线里同路径同大小 → 不该回收
        "/home/user/覆盖写.xlsx": overwritten,  # 基线里同路径但大小变了 → 该回收
        "/home/user/本轮新增.pptx": fresh,      # 基线里没有 → 该回收
    })
    baseline = {
        "/home/user/上轮遗留.pdf": len(old),
        "/home/user/覆盖写.xlsx": 3,           # 旧大小不同
    }
    with _TempReportRoot() as root:
        got = asyncio.run(_salvage_sandbox_artifacts(sandbox, USER, SID, baseline))
        # 注意 sorted 是码点序：本(U+672C) < 覆(U+8986)
        names = sorted(g["file_name"] for g in got)
        check("B2·只回收新增+改写两项", names, ["本轮新增.pptx", "覆盖写.xlsx"])
        check("B2·虚拟路径格式", sorted(g["file_path"] for g in got),
              [f"/reports/{USER}/{SID}/本轮新增.pptx", f"/reports/{USER}/{SID}/覆盖写.xlsx"])
        check("B2·size 来自真实字节数", {g["file_name"]: g["file_size"] for g in got},
              {"覆盖写.xlsx": len(overwritten), "本轮新增.pptx": len(fresh)})
        base = os.path.join(root, USER, SID)
        check_true("B2·文件已落宿主", os.path.isfile(os.path.join(base, "本轮新增.pptx")))
        check_true("B2·上轮遗留**没有**被搬进来",
                   not os.path.exists(os.path.join(base, "上轮遗留.pdf")))
        check("B2·file_type 推断", {g["file_name"]: g["file_type"] for g in got},
              {"覆盖写.xlsx": "spreadsheet", "本轮新增.pptx": "other"})


def test_b2_salvage_guards() -> None:
    print("\n[B②] 兜底护栏")
    content = {"/home/user/a.pptx": b"ppt"}
    with _TempReportRoot():
        # 基线不可用（None）→ 一次沙箱都不该碰
        sb = FakeSandbox(content)
        check("B2·基线 None → 返回空", asyncio.run(_salvage_sandbox_artifacts(sb, USER, SID, None)), [])
        check("B2·基线 None → 未触发任何沙箱调用", (sb.commands, sb.download_calls), ([], []))
        check("B2·sandbox None → []", asyncio.run(_salvage_sandbox_artifacts(None, USER, SID, {})), [])

        # 读取不到内容（沙箱回收）→ 跳过，不落 0 字节垃圾
        sb2 = FakeSandbox({})
        check("B2·沙箱空 → []", asyncio.run(_salvage_sandbox_artifacts(sb2, USER, SID, {})), [])

        # 数量上限
        many = {f"/home/user/f{i}.md": b"x" * (i + 1) for i in range(_SALVAGE_MAX_FILES + 3)}
        sb3 = FakeSandbox(many)
        got = asyncio.run(_salvage_sandbox_artifacts(sb3, USER, SID, {}))
        check(f"B2·最多回收 {_SALVAGE_MAX_FILES} 个", len(got), _SALVAGE_MAX_FILES)
        check("B2·大文件优先",
              [g["file_size"] for g in got],
              sorted((len(v) for v in many.values()), reverse=True)[:_SALVAGE_MAX_FILES])

        # 路径穿越：basename 是 .. 时归一化会跳出会话目录，必须拦掉
        sb4 = FakeSandbox({"/home/user/..": b"evil"})
        got4 = asyncio.run(_salvage_sandbox_artifacts(sb4, USER, SID, {}))
        check("B2·basename == .. 被越界拦截", got4, [])


# ─────────────────────────── C①：正文补全只认磁盘 ───────────────────────────


def test_c1_scan_content_files() -> None:
    print("\n[C①] 正文扫描只补磁盘真实存在的文件")
    tmp = tempfile.mkdtemp(prefix="scan_")
    try:
        base = os.path.join(tmp, USER, SID)
        os.makedirs(os.path.join(base, "sub"), exist_ok=True)
        real = os.path.join(base, "真实报告.md")
        open(real, "w", encoding="utf-8").write("hello")
        open(os.path.join(base, "sub", "嵌套.json"), "w", encoding="utf-8").write("{}")

        content = "\n".join([
            f"报告已保存到 /reports/{USER}/{SID}/真实报告.md",
            f"另外生成了 /reports/{USER}/{SID}/幻觉文件.pptx（其实没落盘）",
            f"嵌套产物在 /reports/{USER}/{SID}/sub/嵌套.json",
            f"目录引用 /reports/{USER}/{SID}/",
            f"别家用户的路径 /reports/other/{SID}/别人的.md",
            "无关文本 /etc/passwd",
            f"重复引用 /reports/{USER}/{SID}/真实报告.md",
        ])
        files = _scan_content_files(content, USER, SID, roots=[tmp])
        # file_name 取 basename（与 chat.py 实时 emit 的 `file_path_virtual.split('/')[-1]`
        # 保持一致；嵌套子目录信息保留在 file_path 里，前端 extractFilename 会带上子路径
        # 组成下载 URL）。契约固化在此，改动两边必须一起动。
        # 注意 sorted 是码点序：嵌(U+5D4C) < 真(U+771F)
        check("C1·只补磁盘存在的两项", sorted(f["file_name"] for f in files),
              ["嵌套.json", "真实报告.md"])
        check_true("C1·嵌套文件的 file_path 保留子目录",
                   any(f["file_path"] == f"/reports/{USER}/{SID}/sub/嵌套.json" for f in files))
        check_true("C1·幻觉文件不在结果里",
                   all("幻觉" not in f["file_path"] for f in files))
        check("C1·file_size 取真实大小（不再是恒 0）",
              {f["file_name"]: f["file_size"] for f in files},
              {"真实报告.md": 5, "嵌套.json": 2})
        check("C1·同一路径去重", len([f for f in files if f["file_name"] == "真实报告.md"]), 1)
        check_true("C1·返回路径保持虚拟形式（/reports/.. 而非磁盘路径）",
                   all(f["file_path"].startswith(f"/reports/{USER}/{SID}/") for f in files))

        # 历史上"整轮零产出却出现下载卡"的场景：正文提到但一个都不存在 → 空列表
        check("C1·全部幻觉 → 空列表（不产出任何卡片）",
              _scan_content_files(
                  f"已保存到 /reports/{USER}/{SID}/a.pptx，可直接下载",
                  USER, SID, roots=[tmp]), [])
        check("C1·空正文 → 空列表", _scan_content_files("", USER, SID, roots=[tmp]), [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_c2_blocked_wording() -> None:
    """C②：拦截文案必须明确否定正文的下载承诺，且随核查范围变化。"""
    print("\n[C②] 拦截文案否定下载承诺")
    src = open("/app/src/api/endpoints/chat.py", encoding="utf-8").read()
    check_true("C2·文案含否定句", "该说法不成立" in src and "请勿据此操作" in src)
    check_true("C2·否定范围随沙箱兜底是否跑过变化", "_verified_scope" in src)
    check_true("C2·沙箱也核查过时才有资格说'沙箱'", "宿主报告目录与沙箱均已核查" in src)


# ─────────────────────────── 共用：路径解析 ───────────────────────────


def test_find_session_file() -> None:
    print("\n[共用] find_session_file / session_file_roots")
    tmp = tempfile.mkdtemp(prefix="find_")
    try:
        base = os.path.join(tmp, USER, SID)
        os.makedirs(os.path.join(base, "sub"), exist_ok=True)
        open(os.path.join(base, "a.md"), "w").write("x")
        open(os.path.join(base, "sub", "b.json"), "w").write("{}")

        check("F1·命中顶层文件", find_session_file(USER, SID, "a.md", roots=[tmp]),
              os.path.join(base, "a.md"))
        check("F1·命中嵌套文件", find_session_file(USER, SID, "sub/b.json", roots=[tmp]),
              os.path.join(base, "sub", "b.json"))
        check("F1·不存在 → None", find_session_file(USER, SID, "nope.md", roots=[tmp]), None)
        check("F1·拒绝 ..", find_session_file(USER, SID, "../a.md", roots=[tmp]), None)
        check("F1·拒绝嵌套 ..", find_session_file(USER, SID, "sub/../../a.md", roots=[tmp]), None)
        check("F1·拒绝绝对路径", find_session_file(USER, SID, "/etc/passwd", roots=[tmp]), None)
        check("F1·空路径 → None", find_session_file(USER, SID, "", roots=[tmp]), None)
        check("F1·目录不算命中", find_session_file(USER, SID, "sub", roots=[tmp]), None)
        # 多根：第二个根里才有的文件也要找到
        tmp2 = tempfile.mkdtemp(prefix="find2_")
        try:
            os.makedirs(os.path.join(tmp2, USER, SID))
            open(os.path.join(tmp2, USER, SID, "c.pdf"), "wb").write(b"%PDF")
            check("F1·多根按序查找", find_session_file(USER, SID, "c.pdf", roots=[tmp, tmp2]),
                  os.path.join(tmp2, USER, SID, "c.pdf"))
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        roots = session_file_roots()
        check_true("F1·session_file_roots 含 report_root", settings.report_root in roots)
        check_true("F1·session_file_roots 含 upload_root", settings.upload_root in roots)
        check_true("F1·session_file_roots 无重复",
                   len(roots) == len({os.path.normpath(r) for r in roots}))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_a_prompt_contract()
    test_b1_extract_and_sanitize()
    test_b1_pull_back_from_sandbox()
    test_b1_guard_flag_on()
    test_b2_listing_command_contract()
    test_b2_listing_parse()
    test_b2_salvage_diff_and_write()
    test_b2_salvage_guards()
    test_c1_scan_content_files()
    test_c2_blocked_wording()
    test_find_session_file()
    print(f"\nmini: {PASS} passed / {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

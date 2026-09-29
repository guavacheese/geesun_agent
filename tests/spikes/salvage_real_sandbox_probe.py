"""Probe: B② 沙箱差集兜底在**真实 CubeSandbox** 上能不能跑通（2026-09-29）。

为什么必须用真沙箱验：B② 的"哪些文件算交付物"完全交给沙箱侧 `find -printf '%p\\t%s'`
来做（含扩展名白名单与排除项）。若该沙箱镜像的 find 不支持 -printf（busybox find 等），
命令会非 0 退出 → `_sandbox_artifact_listing` 返回 {} → 兜底**静默失效**，
单测（假沙箱）看不出来。这条探针专治这个盲区。

覆盖：
  1. 真沙箱上 `find -printf` 可用（exit=0 且能列出 /tmp 自身）
  2. 真沙箱上 `_sandbox_artifact_listing` 能枚举出新建的交付物
  3. 真沙箱上 `_salvage_sandbox_artifacts` 能把差集产物拉回宿主临时报告目录
  4. 脚本/日志（.py/.log）不被当成交付物

运行环境（容器内，需要 cube_api_key 等 env）：
  docker exec <agent容器> /app/.venv/bin/python /tmp/probe.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, "/app")

from src.api.endpoints.chat import (  # noqa: E402
    _SALVAGE_MAX_FILES,
    _salvage_sandbox_artifacts,
    _sandbox_artifact_listing,
)
from src.core.config import settings  # noqa: E402
from src.infra.sandbox import create_sandbox  # noqa: E402

PASS = 0
FAIL = 0
USER, SID = "probeuser", "probesid"
# 制造一批沙箱内产物：3 个"交付物"（含子目录、含中文）+ 2 个"非交付物"
DELIVERABLES = {
    "/home/user/任命书.pptx": b"PK\x03\x04" + b"p" * 4096,
    "/home/user/汇总表.xlsx": b"xl" + b"x" * 1024,
    "/tmp/notes/sub/说明.md": b"# hi\n" * 100,
}
NOISE = {
    "/home/user/generate_ppt.py": b"print('should not be salvaged')\n",
    "/tmp/run.log": b"log line\n" * 500,
}


def check(label: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}\n      actual   = {actual!r}\n      expected = {expected!r}")


def check_true(label: str, cond: bool, detail: str = "") -> None:
    check(label if not detail else f"{label} [{detail}]", bool(cond), True)


def main() -> int:
    thread_id = "probe-salvage-%d" % int(time.time())
    print("创建真实沙箱: thread_id=%s" % thread_id)
    sb = create_sandbox(thread_id)
    if sb is None:
        print("!! 沙箱创建失败（cube_api_key 无效？）—— 本探针无法继续")
        return 2
    print("沙箱 id = %s" % getattr(sb, "sandbox_id", "?"))

    try:
        # ─── 1. find -printf 可用性 ───
        print("\n[1] 真沙箱 find -printf 可用性")
        resp = sb.execute("find /tmp -maxdepth 1 -printf '%p\\t%s\\n'")
        check("1·exit_code", getattr(resp, "exit_code", None), 0)
        out = getattr(resp, "output", "") or ""
        check_true("1·-printf 能输出制表符分隔的 路径+大小", "/tmp\t" in out, out[:120].replace("\n", "|"))
        check_true("1·含 find 的 stderr 提示（不支持时会在这里露出来）",
                   "unrecognized" not in out and "invalid" not in out.lower(),
                   out[:120].replace("\n", "|"))

        # ─── 2. 基线 + 新建产物 + 差集枚举 ───
        print("\n[2] 差集枚举（真实沙箱）")
        baseline = _sandbox_artifact_listing(sb)
        print("    基线候选数 = %d" % len(baseline))

        for p, c in {**DELIVERABLES, **NOISE}.items():
            d = os.path.dirname(p)
            b64 = __import__("base64").b64encode(c).decode()
            r = sb.execute("mkdir -p %s && echo %s | base64 -d > %s && ls -l %s" % (d, b64, p, p))
            if getattr(r, "exit_code", None) != 0:
                print("!! 造文件失败 %s: %s" % (p, getattr(r, "output", "")[:200]))
                return 2

        after = _sandbox_artifact_listing(sb)
        for p in DELIVERABLES:
            check_true("2·枚举到交付物 %s" % p, p in after, "size=%s" % after.get(p))
        for p in NOISE:
            check_true("2·非交付物被白名单挡掉 %s" % p, p not in after)

        diff = [p for p, s in after.items() if baseline.get(p) != s]
        check("2·差集恰好是新造的 3 个交付物", sorted(diff), sorted(DELIVERABLES))

        # ─── 3. 拉回宿主 ───
        print("\n[3] 拉回宿主临时报告目录（真实沙箱）")
        tmp = tempfile.mkdtemp(prefix="salvage_real_")
        saved_root = settings.report_root
        settings.report_root = tmp
        try:
            if len(diff) > _SALVAGE_MAX_FILES:
                print("    ⚠ 差集 %d 个 > 上限 %d，只验前 %d 个"
                      % (len(diff), _SALVAGE_MAX_FILES, _SALVAGE_MAX_FILES))
            import asyncio
            got = asyncio.run(_salvage_sandbox_artifacts(sb, USER, SID, baseline))
            got_map = {g["file_name"]: g for g in got}
            check("3·回收条数", len(got), min(len(DELIVERABLES), _SALVAGE_MAX_FILES))
            for p, c in DELIVERABLES.items():
                name = p.rsplit("/", 1)[-1]
                g = got_map.get(name)
                check_true("3·回收 %s" % name, g is not None)
                if g:
                    check("3·%s 大小与沙箱一致" % name, g["file_size"], len(c))
                    disk = os.path.join(tmp, USER, SID, name)
                    check_true("3·%s 已落宿主磁盘" % name, os.path.isfile(disk))
                    if os.path.isfile(disk):
                        check("3·%s 字节一致" % name, open(disk, "rb").read() == c, True)
            for p in NOISE:
                name = p.rsplit("/", 1)[-1]
                check_true("3·非交付物 %s 未被搬运" % name,
                           not os.path.exists(os.path.join(tmp, USER, SID, name)))
            # 回归：基线不可用必须放弃兜底
            check("3·基线 None → 不回收", asyncio.run(_salvage_sandbox_artifacts(sb, USER, SID, None)), [])
            # 覆盖写场景：同路径改大小后应被认作新产物
            sb.execute("echo changed >> /home/user/任命书.pptx")
            after2 = _sandbox_artifact_listing(sb)
            diff2 = [p for p, s in after2.items() if baseline.get(p) != s]
            check_true("3·覆盖写（同路径不同大小）被算作新产物",
                       "/home/user/任命书.pptx" in diff2, "size=%s" % after2.get("/home/user/任命书.pptx"))
        finally:
            settings.report_root = saved_root
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        try:
            sb.destroy()
            print("\n[4] 探针沙箱已销毁")
        except Exception as e:  # noqa: BLE001
            print("\n[4] 沙箱销毁失败（将在 TTL 后自行回收）: %s" % e)

    print(f"\nmini: {PASS} passed / {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

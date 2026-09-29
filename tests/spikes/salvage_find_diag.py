"""Diag: B② 枚举命令为什么在真实沙箱上返回空（2026-09-29）。

现象：`find /tmp -maxdepth 1 -printf ...` 单目录可用，但
`find /home/user /tmp /reports -maxdepth 4 -type f \\( -name ... \\) ...` 枚举不到
刚创建的文件。怀疑：**候选目录里有不存在的**（/reports 要模型 mkdir 才有）→
GNU find 对不存在的起始路径返回 **exit 1** → `_sandbox_artifact_listing` 依
`exit_code != 0` 判定失败并返回 {}，兜底静默失效。

本诊断逐条拆开取证：每个目录单独 find、合并 find、目录是否存在、
以及退出码/输出原样打印。
"""

from __future__ import annotations

import base64
import sys
import time

sys.path.insert(0, "/app")

from src.api.endpoints.chat import _SALVAGE_DIRS, _SALVAGE_EXTS, _sandbox_artifact_listing  # noqa: E402
from src.infra.sandbox import create_sandbox  # noqa: E402


def show(label: str, resp) -> None:
    out = (getattr(resp, "output", "") or "").replace("\n", "⏎")
    print("  · %-58s exit=%s out=%s"
          % (label, getattr(resp, "exit_code", None), out[:160]))


def main() -> int:
    sb = create_sandbox("probe-diag-%d" % int(time.time()))
    if sb is None:
        print("!! 沙箱创建失败")
        return 2
    try:
        print("沙箱 id = %s" % getattr(sb, "sandbox_id", "?"))
        name_clause = " -o ".join("-name '*.%s'" % e for e in _SALVAGE_EXTS)
        print("\n候选目录 = %s" % (_SALVAGE_DIRS,))

        print("\n[A] 目录是否存在")
        show("ls -d /home/user /tmp /reports", sb.execute("ls -d /home/user /tmp /reports"))
        show("test -d 逐个", sb.execute(
            "for d in /home/user /tmp /reports; do if [ -d $d ]; then echo YES:$d; else echo NO:$d; fi; done"))

        print("\n[B] 单目录 find（各目录独立）")
        for d in _SALVAGE_DIRS:
            show("find %s -maxdepth 4 -type f -printf" % d,
                 sb.execute("find %s -maxdepth 4 -type f -printf '%%p\\t%%s\\n'" % d))

        print("\n[C] 合并 find（被测代码用的形态）")
        full = (
            "find %s -maxdepth 4 -type f \\( %s \\) "
            "-not -path '*/__pycache__/*' -not -path '*/site-packages/*' "
            "-not -path '*/node_modules/*' -not -path '*/.cache/*' -not -path '*/.git/*' "
            "-printf '%%p\\t%%s\\n' 2>/dev/null"
            % (" ".join(_SALVAGE_DIRS), name_clause)
        )
        show("带 2>/dev/null 的完整命令", sb.execute(full))
        show("不带 2>/dev/null（看 stderr 真身）", sb.execute(full.replace(" 2>/dev/null", "")))

        print("\n[D] 造一个文件后再看")
        payload = base64.b64encode(b"PK\x03\x04diag").decode()
        show("写入 /home/user/diag.pptx",
             sb.execute("mkdir -p /home/user && echo %s | base64 -d > /home/user/diag.pptx && ls -l /home/user/diag.pptx" % payload))
        show("单目录 find /home/user", sb.execute("find /home/user -maxdepth 4 -type f -printf '%%p\\t%%s\\n'"))
        show("合并 find（复现被测形态）", sb.execute(full))
        print("  · _sandbox_artifact_listing() = %r" % (_sandbox_artifact_listing(sb),))

        print("\n[E] 只给存在的目录会怎样")
        existing = " ".join(
            d for d in _SALVAGE_DIRS
            if getattr(sb.execute("test -d %s" % d), "exit_code", 1) == 0
        )
        print("  · 存在的候选目录 = %r" % existing)
        if existing:
            show("合并 find（仅存在目录）", sb.execute(full.replace(" ".join(_SALVAGE_DIRS), existing)))
    finally:
        try:
            sb.destroy()
        except Exception as e:  # noqa: BLE001
            print("沙箱销毁失败: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())

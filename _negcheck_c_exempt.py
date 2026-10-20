"""负向验证：C 进展豁免的 5 条接线断言必须有区分力（补丁前 FAIL / 补丁后 PASS）。

用**同一份判据**（直接 import tests/api/test_chat_wiring.py 里的测试函数），
只换被解析的文件：
    · 补丁前 = `git show HEAD:src/api/endpoints/chat.py`（当前改动尚未提交）
    · 补丁后 = 工作区的 src/api/endpoints/chat.py

判据恒真（补丁前也 PASS）就说明这 5 条测试只是装饰，必须重写。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import tests.api.test_chat_wiring as W  # noqa: E402

CASES = [
    "test_stagnation_exemption_judge_is_wired_before_counting",
    "test_stagnation_exemption_is_bounded_by_config",
    "test_convergence_prompt_branches_on_background_job",
    "test_exemption_quota_not_reset_on_convergence",
]

PRE = REPO / "_precheck_chat.py"


def run_cases(label: str, path: pathlib.Path) -> dict[str, bool]:
    W.CHAT_PY = path
    out: dict[str, bool] = {}
    for name in CASES:
        try:
            getattr(W, name)()
            out[name] = True
        except AssertionError as exc:
            out[name] = False
            print("    %-58s FAIL: %s" % (name, str(exc).splitlines()[0][:90]))
    print("=== %s ===" % label)
    for name, ok in out.items():
        print("  %-58s %s" % (name, "PASS" if ok else "FAIL"))
    return out


def main() -> int:
    pre_src = subprocess.run(
        ["git", "show", "HEAD:src/api/endpoints/chat.py"],
        cwd=str(REPO), capture_output=True, text=True, check=True,
    ).stdout
    assert "_progress_exempt_count" not in pre_src, "HEAD 已含豁免代码，负向验证前提失效"
    PRE.write_text(pre_src, encoding="utf-8")
    try:
        pre = run_cases("补丁前（HEAD）", PRE)
        post = run_cases("补丁后（工作区）", REPO / "src" / "api" / "endpoints" / "chat.py")
    finally:
        PRE.unlink(missing_ok=True)

    print()
    bad = []
    for name in CASES:
        if pre[name] is not False:
            bad.append("%s：补丁前居然 PASS ⇒ 判据无区分力" % name)
        if post[name] is not True:
            bad.append("%s：补丁后仍 FAIL ⇒ 修复没接上" % name)
    if bad:
        for b in bad:
            print("✗", b)
        return 1
    print("结论：%d/%d 条判据补丁前 FAIL、补丁后 PASS ⇒ 有区分力，非恒真" % (len(CASES), len(CASES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

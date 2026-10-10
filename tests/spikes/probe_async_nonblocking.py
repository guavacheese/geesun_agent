# -*- coding: utf-8 -*-
"""行为级判据：沙箱 async 接口是否真的不阻塞事件循环（P0-1 的验收）。

为什么需要它：改 `aexecute` 为 `asyncio.to_thread` 之后，**静态断言（AST 里有 to_thread）
只能证明"写法对了"，证明不了"事件循环真的没被占"**。本探针直接问事件循环自己：
在 `await sb.aexecute("sleep 6")` 进行期间，另开一个协程按 0.1s 心跳 —— 数它转了多少圈。

  · 旧实现（同步直调）：事件循环被占死 ⇒ 心跳几乎不转（ticks ≈ 0~1）
  · 新实现（to_thread）：心跳照常 ⇒ ticks ≈ 60

所以**同一份探针在旧镜像上必然 FAIL、在新镜像上必须 PASS** —— 这既是验收判据，
也是判据自身的证伪（证明它不是恒真的空测）。

判据：
  [1] aexecute 期间心跳 tick ≥ 期望的一半，且结果正确
  [2] 两个 aexecute 并发时总耗时 ≈ 单次（真并行，而非串行排队）
  [3] aupload_files 功能正确（上传后可 read 回来）
  [4] adownload_files 功能正确

用法（67 上一次容器，见 run_probe_67.py）:
  python run_probe_67.py probe_async_nonblocking.py 1.0.23   # 期望 FAIL（证伪）
  python run_probe_67.py probe_async_nonblocking.py 1.0.24   # 期望 PASS（验收）
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, "/app")

_PASS = 0
_FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("  PASS %s%s" % (name, ("  | " + str(detail)) if detail else ""))
    else:
        _FAIL += 1
        print("  FAIL %s%s" % (name, ("  | " + str(detail)) if detail else ""))


key = os.environ.get("CUBE_API_KEY") or os.environ.get("cube_api_key") or ""
api_url = os.environ.get("CUBE_API_URL") or os.environ.get("cube_api_url") or ""
template = os.environ.get("CUBE_TEMPLATE_ID") or os.environ.get("cube_template_id") or ""

from langchain_cubesandbox import CubeSandbox  # noqa: E402

sb = CubeSandbox.get_or_create(
    template=template, thread_id="ZZZ_ASYNC_PROBE_1024",
    api_url=api_url, api_key=key,
    ssl_cert=os.environ.get("CUBE_CA_PATH") or None, timeout=900,
)
print("沙箱: id=%s | api=%s" % (getattr(sb, "sandbox_id", "?"), api_url))

SLEEP = 6
TICK = 0.1


async def main() -> None:
    # ── [1] aexecute 期间事件循环是否仍在转 ──────────────────────────
    task = asyncio.create_task(sb.aexecute("sleep %d; echo DONE_ASYNC" % SLEEP, timeout=180))
    ticks = 0
    t0 = time.time()
    while not task.done():
        await asyncio.sleep(TICK)
        ticks += 1
    dur = time.time() - t0
    try:
        r = task.result()
        out, code, err = (r.output or ""), r.exit_code, None
    except Exception as exc:  # noqa: BLE001
        out, code, err = "", None, "%s: %s" % (type(exc).__name__, str(exc)[:140])

    expect = SLEEP / TICK
    print("     aexecute(sleep %d) 耗时 %.2fs | 心跳 ticks=%d（期望 ≈%.0f）" % (SLEEP, dur, ticks, expect))
    check("[1] aexecute 期间事件循环仍在转（ticks ≥ 期望一半）",
          ticks >= expect * 0.5, "ticks=%d expect≈%.0f" % (ticks, expect))
    check("[1b] aexecute 结果正确（无异常 + exit=0 + 输出命中）",
          err is None and code == 0 and "DONE_ASYNC" in out,
          "err=%s exit=%s out=%r" % (err, code, out[:60]))

    # ── [2] 两个 aexecute 并发：真并行则总耗时 ≈ 单次 ────────────────
    t0 = time.time()
    r1, r2 = await asyncio.gather(
        sb.aexecute("sleep %d; echo A" % SLEEP, timeout=180),
        sb.aexecute("sleep %d; echo B" % SLEEP, timeout=180),
    )
    par = time.time() - t0
    print("     两路并发 aexecute 总耗时 %.2fs（单次 %.2fs）" % (par, dur))
    check("[2] 两路 aexecute 真并行（总耗时 < 单次的 1.6 倍）",
          par < dur * 1.6 and (r1.exit_code == 0) and (r2.exit_code == 0),
          "par=%.2fs single=%.2fs exit=(%s,%s)" % (par, dur, r1.exit_code, r2.exit_code))

    # ── [3][4] upload / download 走 async 路径的功能正确性 ───────────
    payload = ("async-probe-payload\n" * 64).encode()
    ups = await sb.aupload_files([("/home/user/_async_probe.txt", payload)])
    up_ok = bool(ups) and all(getattr(u, "error", None) in (None, "") for u in ups)
    check("[3] aupload_files 成功", up_ok, str(ups)[:160])

    downs = await sb.adownload_files(["/home/user/_async_probe.txt"])
    got = b""
    d_ok = False
    if downs:
        d = downs[0]
        got = getattr(d, "content", b"") or b""
        d_ok = (getattr(d, "error", None) in (None, "")) and got == payload
    check("[4] adownload_files 内容与上传一致", d_ok,
          "len=%d expect=%d err=%s" % (len(got), len(payload), getattr(downs[0], "error", None) if downs else "no-result"))


asyncio.run(main())

try:
    sb.close()
except Exception:  # noqa: BLE001
    pass

print("-" * 74)
print("%d PASS / %d FAIL" % (_PASS, _FAIL))
print("###RESULT### %s" % ("ALL PASS" if _FAIL == 0 else "HAS FAILURE"))
sys.exit(0 if _FAIL == 0 else 1)

# -*- coding: utf-8 -*-
"""线程安全验证：e2b sync client 能否承受「多线程同时 execute」。

为什么必须验：修复方案是把 `langchain_cubesandbox` 的三个假异步方法
（aexecute / aupload_files / adownload_files）改成 `await asyncio.to_thread(...)`。
改完之后，同一个 CubeSandbox 对象的 `execute` 会被 **uvicorn 的线程池** 并发调用
（此前是同步直调，天然串行）。若 e2b sync client 不线程安全，会引入新的沙箱异常
甚至误杀（与 2026-08-14 的 `__del__` 误杀同源风险）。

本脚本**精确模拟修复后的调用形态**：多线程直接并发调 `sb.execute(...)`，
不看 `aexecute`（它现在还是同步直调）。所以它给出的结论，直接对应修复后是否要加锁。

判据：
  [1] 8 路并发 execute 全部成功（无异常、exit_code==0）
  [2] **无串台**：每路输出里必须含自己的唯一标记，且不能含别人的标记
  [3] 并发确实并行（总耗时 < 串行耗时的 60%）
  [4] 并发 execute + upload_files 混合无异常
  [5] 结束后沙箱仍然存活（execute 仍可用）—— 防"并发把沙箱搞坏/误杀"

用法（本机构建机上，一次性容器，隔离生产）:
  docker run --rm -v "<repo>/tests:/app/tests:ro" \
    -e CUBE_API_URL=... -e CUBE_API_KEY=... -e CUBE_TEMPLATE_ID=... \
    --entrypoint sh <img> -c "cd /app && /app/.venv/bin/python tests/spikes/probe_threadsafe_1023.py"
"""
from __future__ import annotations

import concurrent.futures as cf
import json
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


# ── [0] 配置就位（直接读 env：容器内没有 .env，且 Settings 有必填字段）──
key = os.environ.get("CUBE_API_KEY") or os.environ.get("cube_api_key") or ""
api_url = os.environ.get("CUBE_API_URL") or os.environ.get("cube_api_url") or ""
template = os.environ.get("CUBE_TEMPLATE_ID") or os.environ.get("cube_template_id") or ""
check("[0] cube_api_key 就位", key.startswith("e2b_"), "len=%d" % len(key))
print("     template = %s | api_url = %s" % (template, api_url))
if not key.startswith("e2b_"):
    print("###RESULT### HAS FAILURE | FAIL=1 -> 配置缺失")
    sys.exit(1)

from langchain_cubesandbox import CubeSandbox  # noqa: E402

THREAD = "ZZZ_TS_PROBE_1023"
t0 = time.time()
sb = CubeSandbox.get_or_create(
    template=template,
    thread_id=THREAD,
    api_url=api_url,
    api_key=key,
    ssl_cert=os.environ.get("CUBE_CA_PATH") or None,
    timeout=900,
)
print("     沙箱就绪：%.1fs | id=%s" % (time.time() - t0, getattr(sb, "sandbox_id", "?")))

# ── 基线：单线程 execute（同时拿到串行耗时基准）──────────────────────
t0 = time.time()
base = sb.execute("echo BASE_OK", timeout=120)
base_dur = time.time() - t0
check("[0b] 基线 execute 成功", base.exit_code == 0 and "BASE_OK" in (base.output or ""),
      "exit=%s dur=%.2fs out=%r" % (base.exit_code, base_dur, (base.output or "")[:50]))

# ── [1][2] 8 路并发 execute：唯一标记 + 跨路串台检测 ──────────────────
N = 8
SLEEP = 3.0          # 让并发窗口足够宽，串行 8 路 ≈ 24s，真并行 ≈ 3-4s


def job(i: int) -> dict:
    tag = "TSPROBE%02d" % i
    t = time.time()
    try:
        r = sb.execute(
            "sleep %s; echo %s_START; sleep 0.2; echo %s_END" % (SLEEP, tag, tag),
            timeout=180,
        )
        out = r.output or ""
        return {
            "i": i, "tag": tag, "exit": r.exit_code, "dur": round(time.time() - t, 2),
            "own_start": ("%s_START" % tag) in out,
            "own_end": ("%s_END" % tag) in out,
            "foreign": [j for j in range(N) if j != i and ("TSPROBE%02d" % j) in out],
            "err": None,
            "out": out.strip()[:80],
        }
    except Exception as exc:  # noqa: BLE001
        return {"i": i, "tag": tag, "exit": None, "dur": round(time.time() - t, 2),
                "own_start": False, "own_end": False, "foreign": [],
                "err": "%s: %s" % (type(exc).__name__, str(exc)[:120]), "out": ""}


t0 = time.time()
with cf.ThreadPoolExecutor(max_workers=N) as ex:
    res = list(ex.map(job, range(N)))
par_dur = time.time() - t0
print("     %d 路并发 execute 总耗时 %.2fs（串行基准 ≈ %.1fs）" % (N, par_dur, base_dur + N * (SLEEP + 0.2)))
for r in res:
    print("       #%d exit=%s dur=%s own=%s/%s foreign=%s %s"
          % (r["i"], r["exit"], r["dur"], r["own_start"], r["own_end"], r["foreign"],
             ("ERR=" + r["err"]) if r["err"] else ""))

errs = [r for r in res if r["err"]]
check("[1] 8 路并发 execute 无异常", not errs, errs[:2] if errs else "0 异常")
check("[1b] 8 路全部 exit_code==0",
      all(r["exit"] == 0 for r in res), [r["exit"] for r in res])
check("[2] 每路都拿到自己的标记（无丢失）",
      all(r["own_start"] and r["own_end"] for r in res),
      [(r["i"], r["own_start"], r["own_end"]) for r in res if not (r["own_start"] and r["own_end"])])
check("[2b] 无跨路串台（输出里不含他人标记）",
      all(not r["foreign"] for r in res),
      [(r["i"], r["foreign"]) for r in res if r["foreign"]])
serial_est = base_dur + N * (SLEEP + 0.2)
check("[3] 确实并行（总耗时 < 串行估算的 60%）", par_dur < serial_est * 0.6,
      "par=%.2fs serial_est=%.2fs 比值=%.2f" % (par_dur, serial_est, par_dur / serial_est))

# ── [4] 并发 execute + upload_files 混合 ─────────────────────────────
def mixed(i: int) -> dict:
    tag = "MIX%02d" % i
    t = time.time()
    try:
        if i % 2 == 0:
            payload = ("payload-%s\n" % tag) * 200
            ups = sb.upload_files([("/home/user/%s.txt" % tag, payload.encode())])
            ok = bool(ups) and all(getattr(u, "error", None) in (None, "") for u in ups)
            return {"i": i, "kind": "upload", "ok": ok, "dur": round(time.time() - t, 2), "err": None}
        r = sb.execute("cat /home/user/%s.txt 2>/dev/null | head -1" % ("MIX%02d" % (i - 1)),
                       timeout=120)
        out = (r.output or "").strip()
        return {"i": i, "kind": "exec", "ok": (r.exit_code == 0),
                "dur": round(time.time() - t, 2), "err": None, "out": out[:40]}
    except Exception as exc:  # noqa: BLE001
        return {"i": i, "kind": "?", "ok": False, "dur": round(time.time() - t, 2),
                "err": "%s: %s" % (type(exc).__name__, str(exc)[:120])}


with cf.ThreadPoolExecutor(max_workers=8) as ex:
    mix = list(ex.map(mixed, range(8)))
for m in mix:
    print("       %s #%d ok=%s dur=%s %s" % (m["kind"], m["i"], m["ok"], m["dur"],
                                              ("ERR=" + m["err"]) if m["err"] else ""))
mix_errs = [m for m in mix if m["err"]]
check("[4] 并发 execute+upload 混合无异常", not mix_errs, mix_errs[:2] if mix_errs else "0 异常")
check("[4b] 混合操作全部成功", all(m["ok"] for m in mix),
      [(m["i"], m["kind"], m["ok"]) for m in mix if not m["ok"]])

# ── [5] 并发风暴后沙箱仍可用（防"并发把沙箱搞坏/误杀"）──────────────
t0 = time.time()
try:
    after = sb.execute("echo AFTER_STORM_OK", timeout=120)
    check("[5] 并发风暴后沙箱仍可 execute", after.exit_code == 0 and "AFTER_STORM_OK" in (after.output or ""),
          "exit=%s dur=%.2fs out=%r" % (after.exit_code, time.time() - t0, (after.output or "")[:60]))
except Exception as exc:  # noqa: BLE001
    check("[5] 并发风暴后沙箱仍可 execute", False, "%s: %s" % (type(exc).__name__, str(exc)[:120]))

# ── 清理：销毁测试沙箱，避免残留 ─────────────────────────────────────
try:
    sb.kill()
    print("     测试沙箱已 kill")
except Exception as exc:  # noqa: BLE001
    print("     kill 失败（交给 TTL 回收）：%s" % str(exc)[:100])
    try:
        sb.close()
    except Exception:  # noqa: BLE001
        pass

print()
print(json.dumps({"parallel_seconds": round(par_dur, 2), "baseline_seconds": round(base_dur, 2)},
                 ensure_ascii=False))
print("-" * 74)
print("%d PASS / %d FAIL" % (_PASS, _FAIL))
print("###RESULT### %s" % ("ALL PASS" if _FAIL == 0 else "HAS FAILURE"))
sys.exit(0 if _FAIL == 0 else 1)

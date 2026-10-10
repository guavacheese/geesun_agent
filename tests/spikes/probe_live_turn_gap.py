# -*- coding: utf-8 -*-
"""量化「每轮 chat 起始处的同步停顿」：同一会话连打 2 轮，逐轮测 healthz 最大空档。

为什么要单独测：
  probe_live_1024 已经证明 P0-1 修好了（长命令期间 healthz 161/161 全成功），
  但同时暴露了**另一个独立的停顿源**：每轮 chat 起始处事件循环静默 ~2.9s。
  本探针回答两个决策所需的问题：
    · 它是「每轮都要付」还是「只在冷启/首轮付」？
    · 它有多长、稳不稳定（多轮取分布）？

判据：
  [A] 每轮的 healthz 采样数 ≥ 8（保证确实在持续探测）
  [B] 每轮 healthz 成功率 100%（事件循环没死，只是短暂停顿）
  [C] 逐轮记录「最大空档」用于分布分析（不做 PASS/FAIL 阈值判定——那属于决策，不是测量）
"""
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/app")

U = "ZZZ_E2E_GAP"
BASE = "http://127.0.0.1:8009/api/v1"
HEALTH = "http://127.0.0.1:8009/healthz"
ROUNDS = 3

fails = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name, ("  | " + str(detail)) if detail else ""))
    if not ok:
        fails.append(name)


def api(method, path, payload=None, timeout=60, token=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def probe_health(timeout=12.0):
    t0 = time.time()
    try:
        req = urllib.request.Request(HEALTH)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return time.time() - t0, r.status
    except Exception:  # noqa: BLE001
        return time.time() - t0, None


from src.core.auth import create_jwt_token  # noqa: E402

tok = create_jwt_token({"user_id": U, "display_name": U, "role": "admin"})
st, body = api("POST", "/sessions", {"title": "e2e-每轮停顿量化-临时"}, token=tok)
check("建临时会话", st in (200, 201), st)
sid = json.loads(body)["session_id"]
print("会话 = %s\n" % sid)

print("轮次 | chat 耗时 | 探测数 | 成功 | 最大空档 | 空档起点(相对秒) | 采样最大延迟")
print("-" * 100)

for i in range(1, ROUNDS + 1):
    if i == 1:
        msg = "用一句话回答：1+1 等于几？不要调用任何工具。"
    else:
        msg = "再回答一个：2+2 等于几？同样只用一句话，不要调用工具。"

    turn = {"done": False}
    times = []
    fails_local = []

    def run_turn():
        try:
            payload = json.dumps({"session_id": sid, "message": msg}).encode("utf-8")
            req = urllib.request.Request(BASE + "/chat", data=payload, method="POST")
            req.add_header("Authorization", "Bearer " + tok)
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=300) as r:
                for raw in r:
                    pass
        except Exception as exc:  # noqa: BLE001
            fails_local.append("%s: %s" % (type(exc).__name__, str(exc)[:120]))
        finally:
            turn["done"] = True

    th = threading.Thread(target=run_turn, daemon=True)
    t0 = time.time()
    th.start()
    while True:
        d, code = probe_health()
        times.append((round(time.time() - t0, 3), round(d, 3), code))
        if turn["done"]:
            break
        time.sleep(0.2)
    th.join(timeout=20)
    elapsed = time.time() - t0

    ok_n = sum(1 for _t, _d, c in times if c == 200)
    # 最大空档 = 相邻**成功**采样之间最大的时间间隔（服务端真正停止应答的时长）
    succ = [t for t, _d, c in times if c == 200]
    max_gap, gap_at = 0.0, None
    for a, b in zip(succ, succ[1:]):
        if b - a > max_gap:
            max_gap, gap_at = b - a, a
    max_d = max(d for _t, d, _c in times)

    print("%4d | %8.1fs | %6d | %4d | %7.3fs | %15s | %12.3fs"
          % (i, elapsed, len(times), ok_n, max_gap,
             ("%.2f" % gap_at) if gap_at is not None else "-", max_d))
    if fails_local:
        print("       chat 异常: %s" % fails_local)
    check("第 %d 轮 healthz 采样数 ≥ 8" % i, len(times) >= 8, len(times))
    check("第 %d 轮 healthz 成功率 100%%" % i, ok_n == len(times), "%d/%d" % (ok_n, len(times)))

st3, _ = api("DELETE", "/sessions/%s" % sid, timeout=60, token=tok)
check("临时会话已删除", st3 in (200, 202, 204), st3)
for d in ("/data/reports/%s/%s" % (U, sid), "/data/uploads/%s/%s" % (U, sid)):
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        print("已清理目录 %s" % d)

print()
print("###RESULT### %s | FAIL=%d%s" % ("ALL PASS" if not fails else "HAS FAILURE",
                                       len(fails), (" -> " + "; ".join(fails)) if fails else ""))
sys.exit(0 if not fails else 1)

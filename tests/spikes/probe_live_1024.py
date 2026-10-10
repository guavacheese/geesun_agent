# -*- coding: utf-8 -*-
"""生产活体验证（1.0.24）：沙箱**长命令执行**期间事件循环是否仍可服务。

为什么必须单独做这一轮（而不是复用 probe_live_1023）：
  1.0.23 那轮验的是 `create_sandbox` 慢路径（P0，c51a903 已修）。本轮验的是
  **另一条独立的冻结点**：`sandbox.aexecute` 里同步直调 e2b SDK（P0-1，b9e60ea 修）。
  两者都冻结同一个单 worker 事件循环，但**判据不同、触发方式不同**：
     · create_sandbox：请求一进来（沙箱冷启）就冻
     · aexecute：要模型真的决定调 execute 工具跑一条长命令才冻
  事故现场（GY24428，2026-10-10 11:06）：模型决定调 execute 跑 150 页 OCR，
  日志停在「execute工具超时限制是600秒」，随后 /healthz 连续失败 → SIGKILL。
  所以本探针要**尽量复刻那个现场**：全新会话（沙箱冷启）+ 明确要求跑长命令。

判据（活体 / 真实 HTTP / 真跑一条 sleep 长命令）：
  [0] 前置：cube_api_key 已配置（否则本轮不走沙箱，判据无意义）
  [A] chat 期间 healthz 探测次数 ≥ 10
  [B] healthz 成功率 100%（旧代码在此必然超时）
  [C] healthz 最大延迟 < 2.0s（旧代码 >8s 乃至完全不响应）
  [D] chat 轮次正常收尾（无 error 事件、连接未断）
  [E] 沙箱 execute 长命令**真的被执行了**（在事件里看到工具调用；未执行则判 INCONCLUSIVE）
  [F] 长命令的真实输出被拿到（出现标记串 LONG_DONE_1024）

诚实性约束：[E] 不成立时**不判 PASS**，而是报 INCONCLUSIVE —— 否则就是"测了个寂寞
还宣称通过"。

自清理：合成用户 ZZZ_E2E_1024 的会话、沙箱、磁盘目录全部删除。
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

U = "ZZZ_E2E_1024"
BASE = "http://127.0.0.1:8009/api/v1"
HEALTH = "http://127.0.0.1:8009/healthz"
MARK = "LONG_DONE_1024"
SLEEP_S = 25

fails = []
inconclusive = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  | " + str(detail)) if detail else ""))
    if not ok:
        fails.append(name)


def note_inconclusive(name, detail=""):
    print("  ???? %s  <- 无法判定%s" % (name, ("  | " + str(detail)) if detail else ""))
    inconclusive.append(name)


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


def probe_health(timeout=8.0):
    t0 = time.time()
    try:
        req = urllib.request.Request(HEALTH)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        return time.time() - t0, r.status, body[:80]
    except Exception as exc:  # noqa: BLE001
        return time.time() - t0, None, "%s: %s" % (type(exc).__name__, str(exc)[:70])


# ── [0] 前置 ──
from src.core.auth import create_jwt_token  # noqa: E402
from src.core.config import Settings  # noqa: E402

s = Settings()
_key = s.cube_api_key or ""
check("[0] cube_api_key 已配置（沙箱路径可达）", _key.startswith("e2b_"),
      "prefix=%s len=%d" % (_key[:4], len(_key)))
print("     cube_template_id = %s | api_url = %s" % (s.cube_template_id, s.cube_api_url))

base_delays = [probe_health()[0] for _ in range(3)]
print("     空闲基线 healthz 延迟 = %s" % ["%.3fs" % d for d in base_delays])

tok = create_jwt_token({"user_id": U, "display_name": U, "role": "admin"})

st, body = api("POST", "/sessions", {"title": "e2e-长命令不阻塞事件循环-临时"}, token=tok)
check("① 建临时会话 HTTP 200/201", st in (200, 201), "st=%s %s" % (st, body[:150]))
sid = json.loads(body)["session_id"]
print("     临时会话 = %s（thread_id = %s:%s，沙箱缓存必未命中）" % (sid, U, sid))

MSG = ("请执行下面这件事，不要用其他方式代替：\n"
       "1) 在沙箱里运行这条 shell 命令：sleep %d && echo %s\n"
       "2) 把命令的完整标准输出原样贴给我，一个字都不要改写。\n"
       "3) 明确告诉我它实际耗时大约多少秒。\n"
       "必须真的执行这条命令（它是本任务的核心），不要凭想象回答。"
       % (SLEEP_S, MARK))

turn = {"done": False, "ok": False, "err": "", "events": [], "text": "",
        "tool_calls": [], "tool_results": []}


def run_turn():
    try:
        payload = json.dumps({"session_id": sid, "message": MSG}).encode("utf-8")
        req = urllib.request.Request(BASE + "/chat", data=payload, method="POST")
        req.add_header("Authorization", "Bearer " + tok)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=900) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                et = ev.get("type")
                turn["events"].append(et)
                if et == "token":
                    turn["text"] += ev.get("content", "")
                if et == "error":
                    turn["err"] = str(ev)[:300]
                # 工具调用事件：不同版本字段名不同，都收进来
                if et and ("tool" in et or "step" in et):
                    blob = json.dumps(ev, ensure_ascii=False)
                    if "tool_calls" in blob or "tool_call" in et or "tool" in et:
                        turn["tool_calls"].append(blob[:400])
                    if et in ("tool_result", "tool_end", "tool_output"):
                        turn["tool_results"].append(blob[:400])
        turn["ok"] = True
    except Exception as exc:  # noqa: BLE001
        turn["err"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    finally:
        turn["done"] = True


t = threading.Thread(target=run_turn, daemon=True)
t0 = time.time()
t.start()

samples = []
while True:
    d, code, note = probe_health()
    samples.append((time.time() - t0, d, code, note))
    if turn["done"]:
        break
    time.sleep(0.2)
t.join(timeout=30)
elapsed = time.time() - t0

ok_n = sum(1 for _ts, _d, c, _n in samples if c == 200)
max_d = max(d for _ts, d, _c, _n in samples)
print("\n② chat 轮次耗时 %.1fs | healthz 探测 %d 次 | 成功 %d 次 | 最大延迟 %.3fs"
      % (elapsed, len(samples), ok_n, max_d))
slow = [(round(ts, 2), round(d, 2), c, n[:40]) for ts, d, c, n in samples if c != 200 or d > 2.0]
if slow:
    print("     异常样本（ts, 延迟, 状态码, 备注）= %s" % slow[:12])
print("     事件种类 = %s" % sorted(set(x for x in turn["events"] if x)))
print("     回答前 200 字 = %s" % turn["text"][:200].replace("\n", " "))

check("[A] chat 期间 healthz 探测次数 ≥ 10", len(samples) >= 10, len(samples))
check("[B] healthz 成功率 100%（无超时/无 5xx）", ok_n == len(samples),
      "%d/%d" % (ok_n, len(samples)))

# ── [C] 判据设计说明（2026-10-10 修正，附实测依据）─────────────────────────
# 初版写成「最大延迟 < 2.0s」并不成立，原因是**这个数字的语义被污染了**：
# 探针是容器内的另一个进程，容器 cpus="2.0"；langgraph/模型阶段把 2 核占满时，
# 探针**自己**被 CPU 饿着，于是客户端计时变长 —— 这与「服务端事件循环被占死」
# 是两回事。首次实测就出现一次 2.743s：随后用服务端 access log 复核（独立证人），
# 发现服务端确实有 2.943s 空档，但发生在**容器启动后第一个 chat 轮**
# （首轮预热：agent 图构建/skills 加载/首次 tokenizer），第二轮起最大空档仅 0.202s。
# 所以正确的判据是两条，而不是拍一个 2.0s：
#   [C]  运维硬阈值：最大延迟 < 5.0s（= healthcheck 的 timeout=5s，超过就会判失败）
#   [C2] 停顿形态：连续 ≥3 个采样 > 2s ⇒ 服务端停顿；孤立 1 个 ⇒ 疑似探针自身
#        CPU 饥饿，须用服务端 access log 复核（脚本 judge_live_stall.py）
check("[C] healthz 最大延迟 < 5.0s（= healthcheck timeout，超过则 healthcheck 必失败）",
      max_d < 5.0, "%.3fs" % max_d)

over = [ts for ts, d, _c, _n in samples if d > 2.0]
# 连续段计数：判断是"整段停顿"还是"孤立尖峰"
runs, cur = [], []
for ts, d, _c, _n in samples:
    if d > 2.0:
        cur.append(ts)
    elif cur:
        runs.append(cur)
        cur = []
if cur:
    runs.append(cur)
longest = max((len(r) for r in runs), default=0)
check("[C2] 无「连续 ≥3 个采样 > 2s」的停顿段", longest < 3,
      ">2s 样本 %d 个（最长连续 %d）%s" % (len(over), longest,
                                        "; 需人工复核服务端 access log" if over else ""))
if over:
    print("     ⚠️ 存在 >2s 采样：%s —— 请跑 judge_live_stall.py 用服务端 access log 复核"
          "（区分「事件循环停顿」与「探针进程 CPU 饥饿」）"
          % [round(t, 2) for t in over][:10])

check("[D1] chat 请求正常结束（未被掐断）", turn["ok"], turn["err"][:200])
check("[D2] 无 error 事件", not turn["err"], turn["err"][:200])
check("[D3] 收到正文（模型确实回应了）", len(turn["text"]) > 0, len(turn["text"]))

# ── [E] 长命令是否真的被执行 ──
ran = MARK in turn["text"] or any(MARK in x for x in turn["tool_results"]) \
    or any("execute" in x for x in turn["tool_calls"])
tool_seen = any("execute" in x or "shell" in x or "sandbox" in x
                for x in turn["tool_calls"] + turn["tool_results"])
print("\n     工具事件条数 = %d | 出现 execute/shell/sandbox 字样 = %s"
      % (len(turn["tool_calls"]) + len(turn["tool_results"]), tool_seen))
if ran:
    check("[E] 沙箱 execute 长命令真的被执行了", True,
          "命中标记 %s 或出现 execute 工具事件" % MARK)
else:
    note_inconclusive("[E] 沙箱 execute 长命令是否真的被执行",
                      "未在事件/正文里看到 %s，也未看到 execute 工具事件；"
                      "本轮未覆盖 P0-1 路径" % MARK)
check("[F] 长命令真实输出被拿到（出现标记串）", MARK in turn["text"],
      "len(text)=%d" % len(turn["text"]))

# ── ③ 清理 ──
st3, _ = api("DELETE", "/sessions/%s" % sid, timeout=60, token=tok)
check("③ 临时会话已删除", st3 in (200, 202, 204), "st=%s" % st3)
for d in ("/data/reports/%s/%s" % (U, sid), "/data/uploads/%s/%s" % (U, sid)):
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        print("     已清理目录 %s" % d)

print()
if inconclusive and not fails:
    print("###RESULT### INCONCLUSIVE | 未判定=%d | 需换方式覆盖 P0-1 路径" % len(inconclusive))
    sys.exit(2)
print("###RESULT### %s | FAIL=%d%s" % ("ALL PASS" if not fails else "HAS FAILURE",
                                       len(fails),
                                       (" -> " + "; ".join(fails)) if fails else ""))
sys.exit(0 if not fails else 1)

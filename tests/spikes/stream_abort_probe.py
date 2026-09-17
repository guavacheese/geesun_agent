# -*- coding: utf-8 -*-
"""Spike v2: 客户端断开流式连接后，vLLM 是否真的停止生成（省不省 GPU）。

背景：退化思考循环（会话 e9c77ce9，reasoning 93634 字符烧光 65536 输出预算，
content 为空）的 L1 兜底方案是「应用侧检测到思考复读 → 掐断流」。
该方案价值分两档，本探针判定属于哪一档：
  - 好档：断连触发 vLLM 中止生成 → 显存/算力槽位立刻释放（省 GPU）
  - 差档：断连只是客户端不再读，vLLM 后台仍把整段跑完（只省用户等待，不省 GPU）

v1 的教训：只用 `num_requests_running` 归零 + `finished_reason="abort"` 计数做判据，
出现「running 归零但 abort 计数不动」的歧义 —— 该计数在 vLLM 里并不覆盖客户端断连路径。
v2 换用两个不受计数口径影响的硬判据：
  ① `vllm:generation_tokens_total` 增量是否在断连后**冻结**（全局计数器，断连后不再涨 = 生成真的停了）
  ② `vllm:kv_cache_usage_perc` 是否回落（KV 释放）

对照组（口径校准）：跑一个 max_tokens=500 且不掐断的请求，验证 Δgeneration_tokens ≈ 实际生成量，
说明该计数器确实能反映「这次请求生成了多少 token」，主实验的判读才站得住。

运行（本机可直连生产 vLLM，无需进容器）：
  set -a; . /d/workspace/geesun_agent/.env; set +a
  /c/Users/GY24428/.workbuddy/binaries/python/envs/default/Scripts/python.exe \
      tests/spikes/stream_abort_probe.py

副作用：对照组会完整生成约 500 token；主实验只跑 ~3 秒即断。不写库、不改服务端配置。
"""
from __future__ import annotations

import os
import time

import httpx


def _env(name: str, default: str = "") -> str:
    """读环境变量并剥掉换行/CR/引号 —— .env 常为 CRLF，不剥会让 URL 非法。"""
    raw = os.environ.get(name) or default
    return raw.replace("\r", "").replace("\n", "").strip().strip('"').strip("'")


BASE = _env("BASE_URL", "http://172.16.66.13:8003/v1").rstrip("/")
KEY = _env("OPENAI_API_KEY")
MODEL = _env("MODEL_NAME", "Qwen3.6-35B-A3B")
CHAT = BASE + "/chat/completions" if BASE.endswith("/v1") else BASE + "/v1/chat/completions"
METRICS = BASE[: -len("/v1")] + "/metrics" if BASE.endswith("/v1") else BASE + "/metrics"

QUESTION = (
    "请写一份 9000 字的中国制造业智能制造转型深度报告，分十个章节，"
    "每章都要有数据支撑与案例分析，并在章末给出可执行建议。"
)
HOLD_SECONDS = float(os.environ.get("HOLD_SECONDS") or "3")    # 主实验：读多久后断连
WATCH_SECONDS = int(os.environ.get("WATCH_SECONDS") or "20")   # 断连后观察多久
CALIB_MAX_TOKENS = int(os.environ.get("CALIB_MAX_TOKENS") or "500")

KEYS = (
    ('vllm:num_requests_running{engine="0"', "running"),
    ('vllm:generation_tokens_total{engine="0"', "gen_tokens"),
    ('vllm:kv_cache_usage_perc{engine="0"', "kv_perc"),
    ('vllm:request_success_total{engine="0",finished_reason="abort"', "abort_cnt"),
    ('vllm:e2e_request_latency_seconds_count{engine="0"', "e2e_cnt"),
)


def _val(text: str, name: str) -> float:
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(name):
            try:
                return float(line.rsplit(" ", 1)[1])
            except ValueError:
                return -1.0
    return -1.0


def snap(label: str, quiet: bool = False) -> dict:
    """单次抓取 /metrics，解析本次实验关心的量。"""
    try:
        text = httpx.get(METRICS, timeout=10, trust_env=False).text
    except Exception as e:  # noqa: BLE001
        print("  [%s] !! metrics 抓取失败: %s %s" % (label, type(e).__name__, e))
        return {k: -1.0 for _, k in KEYS}
    out = {k: _val(text, prefix) for prefix, k in KEYS}
    if not quiet:
        print("  [%s] running=%s kv=%s gen_tokens=%.0f abort_cnt=%.0f e2e_cnt=%.0f"
              % (label.ljust(12), out["running"], out["kv_perc"],
                 out["gen_tokens"], out["abort_cnt"], out["e2e_cnt"]))
    return out


def stream(question: str, max_tokens: int, hold_seconds: float | None):
    """流式请求。hold_seconds 为 None 表示读完整条流；否则读满该秒数后 break（模拟掐断）。"""
    n_events = 0
    n_chars = 0
    finish = None
    t0 = time.time()
    close_reason = "stream_end"
    with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0), trust_env=False) as client:
        with client.stream(
            "POST", CHAT,
            headers={"Authorization": "Bearer " + KEY},
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": question}],
                "max_tokens": max_tokens,
                "stream": True,
            },
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                print("  !! HTTP", resp.status_code, resp.text[:300])
                return None
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    finish = finish or "DONE"
                    break
                n_events += 1
                n_chars += len(body)
                if hold_seconds is not None and (time.time() - t0) >= hold_seconds:
                    close_reason = "client_cut@%.1fs" % (time.time() - t0)
                    break
    return {"events": n_events, "chars": n_chars, "elapsed": time.time() - t0,
            "finish": finish, "close_reason": close_reason}


print("endpoint =", CHAT)
print("metrics  =", METRICS)
print("model    =", MODEL)

# ---------------------------------------------------------------- 对照：口径校准
print("\n" + "=" * 76)
print("### 对照组：计数口径校准（max_tokens=%d，不掐断，等自然结束）" % CALIB_MAX_TOKENS)
print("=" * 76)
c0 = snap("calib-T0")
r = stream("请用三句话说明什么是智能制造。", CALIB_MAX_TOKENS, None)
if r:
    print("  收到事件=%d 字符=%d 耗时=%.1fs finish=%s"
          % (r["events"], r["chars"], r["elapsed"], r["finish"]))
c1 = snap("calib-T1")
d_calib = c1["gen_tokens"] - c0["gen_tokens"]
print("  ⇒ 本次请求 Δgeneration_tokens = %.0f（max_tokens 上限 %d）" % (d_calib, CALIB_MAX_TOKENS))
calib_ok = 0 < d_calib <= CALIB_MAX_TOKENS
print("  [%s] 口径校准：Δ 落在 (0, max_tokens] 内 ⇒ 该计数器能反映单次请求生成量"
      % ("PASS" if calib_ok else "FAIL"))

# ---------------------------------------------------------------- 主实验：掐断
print("\n" + "=" * 76)
print("### 主实验：流式读满 %.1fs 后主动断连（模拟应用侧掐断），观察 %d 秒"
      % (HOLD_SECONDS, WATCH_SECONDS))
print("=" * 76)
m0 = snap("main-T0")
r = stream(QUESTION, 30000, HOLD_SECONDS)
if not r:
    raise SystemExit("主实验请求失败")
print("  收到事件=%d 字符=%d 断连原因=%s" % (r["events"], r["chars"], r["close_reason"]))
t_cut = time.time()
m_cut = snap("cut 瞬间")

print("\n  --- 断连后逐 2 秒观察（gen_tokens 是否继续增长 = 后台是否还在生成）---")
series = []
for i in range(WATCH_SECONDS // 2):
    time.sleep(2)
    s = snap("+%ds" % (2 * (i + 1)))
    series.append(s)

print("\n" + "=" * 76)
print("判读（主实验）")
print("=" * 76)
gen_at_cut = m_cut["gen_tokens"] - m0["gen_tokens"]
gen_after = series[-1]["gen_tokens"] - m_cut["gen_tokens"]
print("  断连前本次已生成 token      = %.0f" % gen_at_cut)
print("  断连后新增 token（%ds 内）  = %.0f" % (WATCH_SECONDS, gen_after))
frozen = gen_after <= 0
running_down = series[0]["running"] == 0.0
kv_down = series[0]["kv_perc"] <= 0.001
abort_delta = series[-1]["abort_cnt"] - m0["abort_cnt"]
e2e_delta = series[-1]["e2e_cnt"] - m0["e2e_cnt"]

print("  [%s] A 断连后 generation_tokens 冻结（Δ=%.0f，后台不再生成）" % ("PASS" if frozen else "FAIL", gen_after))
print("  [%s] B num_requests_running 归零" % ("PASS" if running_down else "FAIL"))
print("  [%s] C kv_cache_usage_perc 回落（现值 %s）" % ("PASS" if kv_down else "FAIL", series[0]["kv_perc"]))
print("  [info] abort 计数增量 = %.0f；e2e 计数增量 = %.0f（口径附注，不作判据）" % (abort_delta, e2e_delta))

print()
if frozen and running_down:
    print("  ⇒ 结论：断连 = 真中止生成，KV 与算力槽位立刻释放。")
    print("     L1「流式掐断」既省用户等待，也真省 GPU（不再白烧剩余输出预算）。")
    if abort_delta == 0:
        print("     附注：vLLM 0.19 未把客户端断连计入 finished_reason=\"abort\" —— 监控上别用该项统计取消率，")
        print("           否则会得出「取消率恒为 0」的错误结论（本次实测已证伪）。")
else:
    print("  ⇒ 结论：断连后仍在生成 —— 掐断只省用户等待，不省 GPU。")
    print("     L1 仍可上（治「空白气泡 + 用户干等数分钟」），但不能拿「省算力」当立项理由。")

# -*- coding: utf-8 -*-
"""探测 vLLM 对「抗重复采样参数」的实际支持情况 —— 决定破解退化循环能落哪几个旋钮。

背景（生产实锤）：会话 e9c77ce9 的思考里，唯一 24-gram 仅占 2.3%（=97.7% 的位置是
重复内容），3260 行里只有 67 个不同的行，最高频行被复读 442 次。
应用侧当前 `temperature=0` 且**完全没有** penalty —— 贪婪解码下重复是吸收态，无法逃逸。

要回答的问题：我们这台 vLLM 0.19.0 到底接受哪些旋钮？
  A) OpenAI 标准 presence_penalty / frequency_penalty
  B) vLLM 扩展 repetition_penalty
  C) 是否支持 min_p / top_k 等其他 vLLM 专属参数
判据：HTTP 200 = 参数被接受；400 = 被拒（错误原文会说明原因）。

运行（生产同款镜像内，env 已注入）：
  python run_in_image.py <tag> "cd /app && /app/.venv/bin/python tests/spikes/penalty_probe.py"
"""
from __future__ import annotations

import json
import os

import httpx

BASE = (os.environ.get("BASE_URL") or "").rstrip("/")
KEY = os.environ.get("OPENAI_API_KEY") or ""
MODEL = os.environ.get("MODEL_NAME") or ""
URL = BASE + "/chat/completions" if BASE.endswith("/v1") else BASE + "/v1/chat/completions"

print("endpoint =", URL)
print("model    =", MODEL)

# 刻意构造一个「容易原地打转」的核对类任务：给了 12 行清单，要求逐行核对。
# 退化循环的典型温床就是"逐项核对 + 自我怀疑"。
LINES = "\n".join(
    "%d. D%05d 卷绕机上料位安全信号（空盘） 数量 1 单位 个" % (i, i)
    for i in range(1, 13)
)
QUESTION = (
    "下面是一张 BOM 表的前 12 行，请逐行核对每一行是否满足「单位 = 个」，"
    "逐行给出结论，并在最后汇总。\n" + LINES
)


def call(label: str, extra: dict) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": QUESTION}],
        "max_tokens": 3000,
        "temperature": 0,
    }
    payload.update(extra)
    print("\n" + "=" * 74)
    print("### %s" % label)
    print("    extra = %s" % json.dumps(extra, ensure_ascii=False))
    print("=" * 74)
    try:
        r = httpx.post(URL, json=payload, timeout=300,
                       headers={"Authorization": "Bearer " + KEY})
    except Exception as e:  # noqa: BLE001
        print("  请求异常:", type(e).__name__, e)
        return {}
    print("  HTTP", r.status_code)
    if r.status_code != 200:
        print("  ✗ 被拒，错误原文:", r.text[:400])
        return {"ok": False, "status": r.status_code}
    d = r.json()
    ch = d["choices"][0]
    msg = ch.get("message") or {}
    rc = msg.get("reasoning_content") or msg.get("reasoning") or ""
    ct = msg.get("content") or ""
    u = d.get("usage") or {}
    print("  ✓ 接受 | reasoning=%d 字符 | content=%d 字符 | finish=%r | completion_tokens=%s"
          % (len(rc), len(ct), ch.get("finish_reason"), u.get("completion_tokens")))
    return {"ok": True, "status": 200, "r": len(rc), "c": len(ct),
            "finish": ch.get("finish_reason")}


print("\n" + "#" * 74)
print("# 参数接受性探测")
print("#" * 74)
res = {}
res["baseline(temperature=0,无penalty)"] = call("① 基线：temperature=0，无 penalty", {})
res["presence+frequency"] = call(
    "② OpenAI 标准：presence_penalty=1.5 + frequency_penalty=1.5",
    {"presence_penalty": 1.5, "frequency_penalty": 1.5})
res["repetition_penalty"] = call(
    "③ vLLM 扩展：repetition_penalty=1.15",
    {"repetition_penalty": 1.15})
res["min_p_top_k"] = call(
    "④ vLLM 专属：min_p=0.05 + top_k=20",
    {"min_p": 0.05, "top_k": 20})
res["temp0.3"] = call("⑤ 只把温度抬到 0.3（无 penalty）", {"temperature": 0.3})

print("\n" + "#" * 74)
print("# 判读汇总")
print("#" * 74)
for k, v in res.items():
    if not v:
        print("  %-34s → 请求失败" % k)
    elif not v.get("ok"):
        print("  %-34s → ❌ 被拒 HTTP %s" % (k, v.get("status")))
    else:
        print("  %-34s → ✅ 接受  reasoning=%-6d content=%-5d finish=%s"
              % (k, v.get("r", -1), v.get("c", -1), v.get("finish")))

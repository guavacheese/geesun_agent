# -*- coding: utf-8 -*-
"""Spike: 实测 vLLM 是否支持 thinking_token_budget（限制思考 token 上限）。

背景：会话 e9c77ce9 第二轮，模型把 65536 输出预算全烧在 reasoning 里，
content 为空、finish_reason='length'，用户看到空白气泡。
vLLM 文档称 Qwen3/DeepSeek/Nemotron3 支持 thinking_token_budget——
达到预算时强制产出 reasoning_end_str，从而把输出空间让给正文。

判据：
  PASS-A 不报 400（参数被接受）
  PASS-B reasoning 被显著截短（远小于 max_tokens）
  PASS-C content 非空（正文真的产出了）← 这才是我们要的结果
对照：不带该参数时 reasoning 会自然展开（可能长得多）。

运行（生产同款镜像内，<tag> 换成当前生产 TAG）：
  python run_in_image.py <tag> "cd /app && /app/.venv/bin/python tests/spikes/thinking_budget_probe.py"

依赖环境变量 BASE_URL / OPENAI_API_KEY / MODEL_NAME（容器内已由 compose 注入）。
"""
from __future__ import annotations

import json
import os
import sys

import httpx

BASE = (os.environ.get("BASE_URL") or "").rstrip("/")
KEY = os.environ.get("OPENAI_API_KEY") or ""
MODEL = os.environ.get("MODEL_NAME") or ""
if BASE.endswith("/v1"):
    URL = BASE + "/chat/completions"
else:
    URL = BASE + "/v1/chat/completions"

print("endpoint =", URL)
print("model    =", MODEL)
print()

# 故意给一个「容易想很久」的开放题，方便观察预算是否生效
QUESTION = "一个笼子里有鸡和兔共 35 只，脚共 94 只。鸡兔各几只？请直接给答案。"


def call(label: str, extra: dict) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": QUESTION}],
        "max_tokens": 4096,
        "temperature": 0.6,
    }
    payload.update(extra)
    print("=" * 70)
    print("### %s   extra=%s" % (label, json.dumps(extra, ensure_ascii=False)))
    print("=" * 70)
    try:
        r = httpx.post(URL, json=payload, timeout=300,
                       headers={"Authorization": "Bearer " + KEY})
    except Exception as e:  # noqa: BLE001
        print("请求异常:", type(e).__name__, e)
        return {}
    print("HTTP", r.status_code)
    try:
        d = r.json()
    except Exception:  # noqa: BLE001
        print(r.text[:400])
        return {}
    if "choices" not in d:
        print("非预期响应:", json.dumps(d, ensure_ascii=False)[:500])
        return d
    ch = d["choices"][0]
    msg = ch.get("message") or {}
    rc = msg.get("reasoning_content") or msg.get("reasoning") or ""
    ct = msg.get("content") or ""
    print("  reasoning 长度(字符) = %d" % len(rc))
    print("  content   长度(字符) = %d" % len(ct))
    print("  finish_reason        = %r" % ch.get("finish_reason"))
    u = d.get("usage") or {}
    print("  usage                = completion=%s prompt=%s" % (u.get("completion_tokens"), u.get("prompt_tokens")))
    print("  reasoning 前 120     = %r" % rc[:120])
    print("  reasoning 后 120     = %r" % rc[-120:])
    print("  content 全文         = %r" % ct[:300])
    return {"status": r.status_code, "reasoning_len": len(rc),
            "content_len": len(ct), "finish": ch.get("finish_reason"),
            "completion_tokens": u.get("completion_tokens")}


base_res = call("① 对照：不带 thinking_token_budget", {})
budget_res = call("② 带 thinking_token_budget=200", {"thinking_token_budget": 200})

print("\n" + "=" * 70)
print("判读")
print("=" * 70)
ok_a = budget_res.get("status") == 200
print("  [%s] A 参数被接受（无 400）" % ("PASS" if ok_a else "FAIL"))
if ok_a:
    b = budget_res.get("reasoning_len", 0)
    c = budget_res.get("content_len", 0)
    print("  [%s] B reasoning 被截短（%d 字符，对照 %d）"
          % ("PASS" if b and b < max(base_res.get("reasoning_len", 0), 1) else "CHECK",
             b, base_res.get("reasoning_len", 0)))
    print("  [%s] C content 非空（%d 字符）→ 正文真的产出了" % ("PASS" if c > 0 else "FAIL", c))
else:
    print("  ⇒ 该 vLLM 构建不支持 thinking_token_budget，需走应用侧兜底方案")

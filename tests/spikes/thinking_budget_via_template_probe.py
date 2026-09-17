# -*- coding: utf-8 -*-
"""Spike: 能否**不改服务端**就用 chat_template_kwargs 限制思考预算？

背景（两条线索的交汇）
  1) 会话 e9c77ce9 退化思考循环烧光 65536 输出预算 → content 空。治本方向是
     vLLM 的 thinking_token_budget，但顶层传参返回 400：
       "thinking_token_budget is set but reasoning_config is not configured.
        Please set --reasoning-config to use thinking_token_budget."
     即必须重启生产 vLLM（影响全体用户），代价高。
  2) 意外发现：chat_template_kwargs={"enable_thinking": false} **生效**（reasoning=0、
     直接给正文）—— 说明 **chat template 变量通道是通的**，不需要 --reasoning-config。
     ⇒ 于是产生本探针要回答的问题：Qwen3.6 的 chat template 是否本身支持某个
       「思考预算」变量（如 thinking_budget / thinking_token_budget）？
       若支持，就能纯请求侧限思考，完全绕开重启。

判据（同一道长思考题，只改 chat_template_kwargs）：
  对照 R0  不传 → reasoning 长（吃满 max_tokens 或自然收敛）
  实验 R1  {"thinking_token_budget": 200} → 若 reasoning 骤降且 content 非空 = 生效
  实验 R2  {"thinking_budget": 200}       → 同上
  实验 R3  {"enable_thinking": false}     → 已知生效，作「通道可用」的正对照
  关键：R1/R2 必须**同时满足** reasoning 显著变短 **且** content 非空。
        只变短不给正文 = 模板只是截断，不是我们要的「强制收尾转正文」。

运行（本机可直连生产 vLLM）：
  set -a; . /d/workspace/geesun_agent/.env; set +a
  /c/Users/GY24428/.workbuddy/binaries/python/envs/default/Scripts/python.exe \
      tests/spikes/thinking_budget_via_template_probe.py
"""
from __future__ import annotations

import json
import os
import time

import httpx


def _env(name: str, default: str = "") -> str:
    raw = os.environ.get(name) or default
    return raw.replace("\r", "").replace("\n", "").strip().strip('"').strip("'")


BASE = _env("BASE_URL", "http://172.16.66.13:8003/v1").rstrip("/")
KEY = _env("OPENAI_API_KEY")
MODEL = _env("MODEL_NAME", "Qwen3.6-35B-A3B")
URL = BASE + "/chat/completions" if BASE.endswith("/v1") else BASE + "/v1/chat/completions"

MAX_TOKENS = int(os.environ.get("MAX_TOKENS") or "2000")

# 长思考题：开放式长文写作，模型会花大量 token 做规划与自我检查（实测 3s 生成 629 token 仍未收尾）
QUESTION = (
    "请写一份 9000 字的中国制造业智能制造转型深度报告，分十个章节，"
    "每章都要有数据支撑与案例分析，并在章末给出可执行建议。"
)


def call(label: str, extra_body: dict) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": QUESTION}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.6,
    }
    payload.update(extra_body)
    extra = extra_body.get("chat_template_kwargs")
    print("\n" + "=" * 76)
    print("### %s   chat_template_kwargs=%s"
          % (label, json.dumps(extra, ensure_ascii=False) if extra else "（不传）"))
    print("=" * 76)
    t0 = time.time()
    try:
        r = httpx.post(URL, json=payload, timeout=600, trust_env=False,
                       headers={"Authorization": "Bearer " + KEY})
    except Exception as e:  # noqa: BLE001
        print("  请求异常:", type(e).__name__, e)
        return {}
    dt = time.time() - t0
    if r.status_code != 200:
        print("  HTTP", r.status_code, r.text[:300])
        return {"status": r.status_code}
    d = r.json()
    ch = d["choices"][0]
    msg = ch.get("message") or {}
    rc = msg.get("reasoning_content") or msg.get("reasoning") or ""
    ct = msg.get("content") or ""
    u = d.get("usage") or {}
    print("  reasoning 长度 = %5d 字符   content 长度 = %5d 字符   耗时 %.1fs"
          % (len(rc), len(ct), dt))
    print("  finish_reason  = %r     completion_tokens = %s"
          % (ch.get("finish_reason"), u.get("completion_tokens")))
    print("  reasoning 尾部 = %r" % rc[-100:])
    print("  content 开头   = %r" % ct[:160])
    return {"status": 200, "reason_len": len(rc), "content_len": len(ct),
            "finish": ch.get("finish_reason"), "completion_tokens": u.get("completion_tokens")}


r0 = call("对照 R0 基线", {})
r1 = call("实验 R1", {"chat_template_kwargs": {"thinking_token_budget": 200}})
r2 = call("实验 R2", {"chat_template_kwargs": {"thinking_budget": 200}})
r3 = call("正对照 R3", {"chat_template_kwargs": {"enable_thinking": False}})

print("\n" + "=" * 76)
print("判读")
print("=" * 76)
print("  R0 基线      reasoning=%s content=%s"
      % (r0.get("reason_len"), r0.get("content_len")))
for tag, r in (("R1 thinking_token_budget", r1), ("R2 thinking_budget", r2)):
    if r.get("status") != 200:
        print("  %-26s 被拒（HTTP %s）" % (tag, r.get("status")))
        continue
    shorter = r["reason_len"] < max(r0.get("reason_len", 0) * 0.6, 1)
    has_content = r["content_len"] > 0
    print("  %-26s reasoning=%d content=%d  → %s"
          % (tag, r["reason_len"], r["content_len"],
             "生效（预算+转正文）" if (shorter and has_content)
             else "未生效（长度未降）" if not shorter
             else "仅截断未转正文"))
print("  R3 enable_thinking=false   reasoning=%s content=%s （通道正对照）"
      % (r3.get("reason_len"), r3.get("content_len")))

print("\n  结论判读：")
if any(r.get("status") == 200 and r["reason_len"] < max(r0.get("reason_len", 0) * 0.6, 1)
       and r["content_len"] > 0 for r in (r1, r2)):
    print("  ⇒ Qwen3.6 chat template **原生支持思考预算变量**，可纯请求侧限思考，")
    print("     无需重启生产 vLLM（无需 --reasoning-config）。")
else:
    print("  ⇒ 模板不认思考预算变量（两者都不缩短 reasoning）。")
    print("     想用 thinking_token_budget 就必须服务端加 --reasoning-config 并重启 vLLM；")
    print("     否则只能走应用侧方案（L1 流式掐断 / L2 采样参数）。")
    if r3.get("reason_len") == 0:
        print("     注：enable_thinking=false 生效 ⇒ 模板变量通道本身是通的，")
        print("         说明缺的只是「预算」变量本身，不是通道问题。")

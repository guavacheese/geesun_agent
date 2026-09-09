# -*- coding: utf-8 -*-
"""
Spike: chat.py _drain_astream content 归一化验证（vLLM content-parts 数组）

背景（2026-09-09 实测）：
  vLLM 0.19 启用 --reasoning-parser qwen3 + enable_thinking 后，流式 delta.content
  可能不是 str 而是 OpenAI content-parts 数组：
      [{"type": "text", "text": "..."}, ...]
  chat.py 原逻辑 `_think_buffer += content`（str += list）抛
  TypeError: can only concatenate str (not "list") to str，
  导致 upload_to_sandbox 后的正文输出 chunk 直接崩掉整轮 agent 流（M3 零产出拦截）。

语义约定（与原 chat.py Qwen 增量流式一致）：
  - not _think_done 阶段：所有 content 先进 think buffer；未到 </think> 时整段
    当思考增量 reasoning 滚出（Qwen 思考在正文前）
  - 命中 </think>：`</think>` 前为思考、之后 remaining 为正文走 token
  - think 已闭合（done）后：content 直接走 token（写答案阶段）
  本 spike 的 drain_chunk 复刻上述语义，仅在入口插入修复后的 normalize 归一化，
  验证归一化不改变各路径行为且文本拼接正确。

运行：python tests/spikes/qwen_content_parts.py（纯标准库，无第三方依赖）
"""


def normalize(raw_content):
    """与 chat.py 修复后逻辑一致：str / content-parts 数组 → str；
    None（tool_calls chunk）与未知类型 → ""（保持 falsy，防 str(None) 污染）"""
    if raw_content is None:
        return ""
    if isinstance(raw_content, list):
        text_parts = []
        for p in raw_content:
            if isinstance(p, str):
                text_parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text":
                text_parts.append(p.get("text", ""))
        return "".join(text_parts)
    if isinstance(raw_content, str):
        return raw_content
    return ""


def drain_chunk(buf, sent_len, done, content):
    """简化版单 chunk 处理（复刻 chat.py:813-896 语义，入口带 normalize 修复点）。
    返回 (buf, sent_len, done, reasoning_out, token_out)"""
    reasoning_out, token_out = "", ""
    content = normalize(content)  # ← 修复点
    if not done and content:
        buf += content
        think_end = buf.find("</think>")
        if think_end >= 0:
            done = True
            pending = buf[sent_len:think_end]
            sent_len = think_end
            remaining = buf[think_end + 8:]
            if remaining.startswith("\n"):
                remaining = remaining[1:]
            if pending.strip():
                reasoning_out = pending
            if remaining:
                token_out = remaining
        else:
            hold = 0
            for k in range(7, 1, -1):  # </think>[:7..2]
                if content.endswith("</think>"[:k]):
                    hold = k
                    break
            send = content[:-hold] if hold else content
            sent_len = len(buf) - hold
            if send:
                reasoning_out = send
    elif content:
        # think 已闭合：写答案阶段，正文走 token
        token_out = content
    return buf, sent_len, done, reasoning_out, token_out


PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"[FAIL] {name} {detail}"
    PASS += 1
    print(f"  PASS {name}")


print("=== S1: 纯 str 无 </think>（思考增量，原路径）===")
buf, sent, done, r, t = drain_chunk("", 0, False, "今天是2026年9月9日")
check("S1 str 当思考增量滚出", r == "今天是2026年9月9日" and t == "" and not done, f"r={r!r}")

print("=== S2: list[str]（vLLM 罕见形态）===")
buf, sent, done, r, t = drain_chunk("", 0, False, ["北京", "天气", "20°C"])
check("S2 list[str] 拼接后滚出", r == "北京天气20°C" and t == "" and not done, f"r={r!r}")

print("=== S3: content-parts 数组（本次崩溃现场形态）===")
buf, sent, done, r, t = drain_chunk("", 0, False, [{"type": "text", "text": "### 立项审批表"}])
check("S3 text part 提取", r == "### 立项审批表" and t == "" and not done, f"r={r!r}")

print("=== S4: 混合 str + dict parts + 非 text part 忽略 ===")
mixed = [
    "文件：",
    {"type": "text", "text": "GPC24051081"},
    {"type": "image_url", "image_url": {"url": "data:..."}},  # 非 text → 忽略
    {"type": "text", "text": ".pdf"},
]
buf, sent, done, r, t = drain_chunk("", 0, False, mixed)
check("S4 非 text part 忽略且保序", r == "文件：GPC24051081.pdf" and not done, f"r={r!r}")

print("=== S5: None / 非 str 非 list（防御兜底，保持 falsy）===")
buf, sent, done, r, t = drain_chunk("", 0, False, None)
check("S5 None → 空输出", r == "" and t == "" and buf == "", f"buf={buf!r}")
buf, sent, done, r, t = drain_chunk("", 0, False, 12345)
check("S5 未知类型 → 空（不猜不污染）", r == "" and t == "" and buf == "", f"buf={buf!r}")

print("=== S6: 归一化后仍走 </think> 增量流式（跨 chunk 残片）===")
buf, sent, done, r, t = drain_chunk("", 0, False, [{"type": "text", "text": "让我想想怎么查</t"}])
check("S6-A 残片截留（reasoning 已滚、</t 前缀 hold）",
      r == "让我想想怎么查" and t == "" and sent == 7, f"r={r!r} sent={sent}")
buf, sent, done, r, t = drain_chunk(buf, sent, done, [{"type": "text", "text": "hink>找到了，审批表在此。"}])
# A 段思考已滚出（sent=7）→ B 段 pending=buf[7:7]="" 防重复；正文从 </think> 后剥离
check("S6-B 补全 </think>：思考不重复（pending 空）", done and r == "", f"r={r!r} done={done}")
check("S6-B </think> 后 remaining 走 token", t == "找到了，审批表在此。", f"t={t!r}")
# think 闭合后：后续 parts 正文走 token（覆盖 chat.py:893 分支）
buf, sent, done, r, t = drain_chunk(buf, sent, done, [{"type": "text", "text": "补充说明。"}])
check("S6-C 闭合后 parts 正文走 token", r == "" and t == "补充说明。", f"t={t!r}")

print("=== S7: 思考中多 chunk 逐段滚出 ===")
buf, sent, done, r, t = drain_chunk("", 0, False, [{"type": "text", "text": "第一步先看目录"}])
check("S7 首段 parts 滚出", r == "第一步先看目录" and not done)
buf, sent, done, r, t = drain_chunk(buf, sent, done, ["第二步对比两份文件"])
check("S7 次段 str 继续滚出", r == "第二步对比两份文件" and not done)

print("=== S8: text part 带换行/空 part（拼接保真）===")
buf, sent, done, r, t = drain_chunk("", 0, False, [{"type": "text", "text": "\n"}, {"type": "text", "text": ""}, {"type": "text", "text": "done"}])
check("S8 空 part 不影响拼接", r == "\ndone" and not done, f"r={r!r}")

print(f"\n全部通过: {PASS} 断言")

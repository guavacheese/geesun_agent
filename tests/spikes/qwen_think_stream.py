"""spike：Qwen </think> 增量流式切分逻辑验证（与 chat.py _drain_astream 内算法同构）。

验证点（2026-09-09 修复）：
1. 思考期间每个 chunk 都立即 yield reasoning（不再攒到 </think> 一次性发）
2. </think> 到达后补发尾部增量，无重复、无遗漏
3. </think> 跨 chunk 截断时正确切分
4. 多 AI 消息（新 langgraph step）重置 _think_sent_len 后仍正确
"""
import json


class FakeStream:
    """模拟 chat.py Qwen </think> 分支：输入 chunk 序列，输出 SSE 事件序列。"""

    def __init__(self):
        self._think_buffer = ""
        self._think_sent_len = 0
        self._think_done = False
        self.thinking_emitted = False
        self.generating_emitted = False
        self.events: list[dict] = []

    def _emit(self, type_, content=None, status=None):
        if status:
            self.events.append({"type": "agent_status", "status": status})
        if content is not None:
            self.events.append({"type": type_, "content": content})

    def feed(self, content: str):
        """等价于 chat.py:829-876 的 elif not _think_done and content 分支。"""
        if self._think_done or not content:
            return
        self._think_buffer += content
        think_end = self._think_buffer.find("</think>")
        if think_end >= 0:
            self._think_done = True
            pending = self._think_buffer[self._think_sent_len:think_end]
            self._think_sent_len = think_end
            remaining = self._think_buffer[think_end + 8:]
            if remaining.startswith("\n"):
                remaining = remaining[1:]
            if pending.strip():
                if not self.thinking_emitted:
                    self.thinking_emitted = True
                    self._emit("reasoning", status="thinking")
                self._emit("reasoning", content=pending)
            if remaining:
                if not self.generating_emitted:
                    self.generating_emitted = True
                    self._emit("token", status="generating")
                self._emit("token", content=remaining)
        else:
            # 未到 </think>：整 chunk 是思考增量 → 立即流式发。
            # 若 chunk 尾部恰是 </think> 标签前缀（跨 chunk 截断），截留待下 chunk 判定
            hold = 0
            for _k in range(7, 1, -1):
                if content.endswith("</think>"[:_k]):
                    hold = _k
                    break
            _send = content[:-hold] if hold else content
            self._think_sent_len = len(self._think_buffer) - hold
            if _send:
                if not self.thinking_emitted:
                    self.thinking_emitted = True
                    self._emit("reasoning", status="thinking")
                self._emit("reasoning", content=_send)

    def reset_step(self):
        """新 AI 消息（langgraph step）→ 重置（chat.py:801-804 区域）。"""
        self._think_done = False
        self._think_buffer = ""
        self._think_sent_len = 0
        self.thinking_emitted = False
        self.generating_emitted = False


def reasoning_text(events: list[dict]) -> str:
    return "".join(e.get("content", "") for e in events if e["type"] == "reasoning")


def token_text(events: list[dict]) -> str:
    return "".join(e.get("content", "") for e in events if e["type"] == "token")


PASS = 0
TOTAL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, TOTAL
    TOTAL += 1
    mark = "✅" if cond else "❌"
    if cond:
        PASS += 1
    print(f"{mark} {name}" + (f" — {detail}" if detail and not cond else ""))


# ─── 场景 1：多 chunk 思考增量 → 每个 chunk 都流式 yield ───
s = FakeStream()
chunks = [
    "The user is asking a simple math question: 1+1=? ",
    "This is a trivial question that doesn't require any tools ",
    "or complex reasoning.\n",
    "</think>\n\n1+1=2",
]
for c in chunks:
    s.feed(c)
events = s.events
rs = reasoning_text(events)
ts = token_text(events)
# 断言：思考文本被完整保留且每个 chunk 出现（增量流式）
check("场景1: thinking 期间每 chunk 都流式 yield reasoning", len([e for e in events if e["type"] == "reasoning"]) >= 3,
      f"reasoning 事件数={len([e for e in events if e['type']=='reasoning'])}")
check("场景1: 思考文本完整无遗漏", "The user is asking a simple math question: 1+1=? This is a trivial question that doesn't require any tools or complex reasoning." in rs,
      f"rs={rs!r}")
check("场景1: answer token 完整", ts.lstrip("\n") == "1+1=2", f"ts={ts!r}")
check("场景1: thinking 状态先于 generating", events[0]["type"] == "agent_status" and events[0]["status"] == "thinking")
check("场景1: reasoning 不含 </think> 标签", "</think>" not in rs)

# ─── 场景 2：单 chunk 一次含完整 think + </think> + answer（无中间流式）───
s = FakeStream()
s.feed("Short think.</think>\n\n42")
events = s.events
check("场景2: 单 chunk 完整 → reasoning 一次性发出", reasoning_text(events) == "Short think.", f"rs={reasoning_text(events)!r}")
check("场景2: answer token=42", token_text(events).lstrip("\n") == "42", f"ts={token_text(events)!r}")

# ─── 场景 3：</think> 跨 chunk 截断（split "</t|hink>")───
s = FakeStream()
s.feed("Thinking across boundary... </t")
s.feed("hink>\n\nAnswer here")
events = s.events
rs = reasoning_text(events)
check("场景3: 跨 chunk 截断 → thinking 文本完整且无标签残片", rs == "Thinking across boundary... ",
      f"rs={rs!r}")
check("场景3: 跨 chunk 截断 → answer 完整", token_text(events).lstrip("\n") == "Answer here", f"ts={token_text(events)!r}")
# 关键：第一 chunk 的 "…</t" 截留不发，不得出现在 reasoning 里
r_chunks = [e["content"] for e in events if e["type"] == "reasoning"]
check("场景3: 标签残片 </t 未作为思考内容发出", all("</t" not in c for c in r_chunks), f"r_chunks={r_chunks!r}")

# ─── 场景 4：多 AI 消息（新 step 重置）───
s = FakeStream()
s.feed("Think A part1 ")
s.feed("Think A part2</think>\n\nAnswer A")
s.reset_step()
s.feed("Think B</think>\n\nAnswer B")
events = s.events
check("场景4: 重置后第二条 AI 消息思考/答案正确", reasoning_text(events).endswith("Think B") and token_text(events).endswith("Answer B"),
      f"rs_tail={reasoning_text(events)[-20:]!r} ts_tail={token_text(events)[-20:]!r}")

# ─── 场景 5：无 </think> 模型（全 content 是思考增量）→ 全部作为 reasoning 流出不卡死 ───
s = FakeStream()
s.feed("No think tag model line1 ")
s.feed("line2")
events = s.events
check("场景5: 无 </think> → 文本仍全部作为 reasoning 流出（不静默吞）",
      reasoning_text(events) == "No think tag model line1 line2", f"rs={reasoning_text(events)!r}")
check("场景5: 无 </think> → 不产生 token 事件", all(e["type"] != "token" for e in events))

print(f"\n结果: {PASS}/{TOTAL} PASS")
if PASS != TOTAL:
    raise SystemExit(1)

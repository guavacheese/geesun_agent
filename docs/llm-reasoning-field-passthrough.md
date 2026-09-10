# LLM 推理字段透传：流式与非流式的行为差异

> 沉淀日期：2026-09-10
> 触发问题：直接访问 vLLM 有 thinking 内容和单独字段，但通过 deepagents 的 `ainvoke`
> 拿不到 thinking——是"不剥 reasoning"还是"字段被丢"？
> 实证脚本：`spike_ainvoke_reasoning.py` / `spike_stream_vs_nonstream_reasoning.py` /
> `spike_reasoning_ainvoke_gap.py`（本机实测，直连 172.16.66.13:8003）

---

## 0. 结论（先看这个）

| 问题 | 答案 |
|---|---|
| vLLM 的字段名是什么？ | **`reasoning`**（流式 `delta.reasoning` / 非流式 `message.reasoning`）——**不是** `reasoning_content` |
| 原生 langchain-openai 会保留吗？ | **不会**。两条路径都丢（模块 docstring 明示第三方扩展字段 "not extracted or preserved"） |
| 项目 `ReasoningChatOpenAI` 能拿到吗？ | **流式能（603 字符）、非流式不能（0 字符）** |
| 是"不剥"还是"被丢"？ | **被丢**。不是主动剥离，是 langchain 的转换函数**不认识**该字段名，静默丢弃 |
| 为什么流式能拿到？ | 项目子类覆盖了**流式专属钩子** `_convert_chunk_to_generation_chunk` 把字段捞回来 |
| 为什么非流式拿不到？ | 非流式走 `_create_chat_result`，**项目没有覆盖它** → 无人捞 → 丢 |
| 怎么修？ | 覆盖 `_create_chat_result`，从原始响应里取 `reasoning` 写回 `additional_kwargs` |

---

## 1. 实证矩阵（决定性证据）

同一 prompt（`1+1=?`），同一模型（Qwen3.6-35B-A3B），三个实现对照：

| 变体 | `astream`（流式） | `ainvoke`（非流式） |
|---|---|---|
| A 原生 `ChatOpenAI` | 无 ✗ (0 字符) | 无 ✗ (0 字符) |
| **B 项目现状**（`ReasoningChatOpenAI`，仅流式钩子） | **有 ✓ (603 字符)** | **无 ✗ (0 字符)** |
| C 修复版（补 `_create_chat_result`） | 有 ✓ (603 字符) | **有 ✓ (603 字符)** |

**B 行即用户观察到的现象**：`astream` 有 thinking、`ainvoke` 没有。

### wire 层字段名普查（直连 vLLM）

```
非流式 message 的全部 key:
['annotations', 'audio', 'content', 'function_call', 'reasoning', 'refusal', 'role', 'tool_calls']
                                                                  ^^^^^^^^^
流式 delta 出现过的全部 key:
['content', 'reasoning', 'role']
              ^^^^^^^^^
```

**关键**：字段名是 `reasoning`（OpenAI 新规范风格），而 langchain-openai 1.2.1
只认 `reasoning_content`（DeepSeek 风格）等已知字段。

---

## 2. 根因定位（精确到行）

### 流式路径（**项目已覆盖**）

```
ChatOpenAI._stream / _astream
  └─ _convert_chunk_to_generation_chunk(chunk, ...)     ← 项目 ReasoningChatOpenAI 覆盖了这里
       └─ 从 chunk["choices"][0]["delta"] 取 reasoning / reasoning_content
          → 写入 msg.additional_kwargs["reasoning_content"]
```

项目实现见 `src/core/model.py:275-317`（`ReasoningChatOpenAI`）。

### 非流式路径（**项目未覆盖 ← 缺口**）

```
ChatOpenAI._generate / _agenerate
  └─ _create_chat_result(response, generation_info)
       └─ 第 1760 行: message = _convert_dict_to_message(res["message"])
            ↑
            `_convert_dict_to_message` 是**模块级函数**（base.py:198），
            只按官方 OpenAI 规范取值 → `reasoning` 是第三方扩展字段 → 静默丢弃
```

`_create_chat_result` 位于 `langchain_openai/chat_models/base.py:1714`，
末尾仅对 `openai.BaseModel` 类型的响应显式补 `parsed` / `refusal`：

```python
if isinstance(response, openai.BaseModel) and getattr(response, "choices", None):
    message = response.choices[0].message
    if hasattr(message, "parsed"):
        generations[0].message.additional_kwargs["parsed"] = message.parsed
    if hasattr(message, "refusal"):
        generations[0].message.additional_kwargs["refusal"] = message.refusal
    # ← 没有 reasoning！这就是它被丢的地方
```

**这解释了为什么 `refusal` 能存活而 `reasoning` 不能**——前者有显式处理，后者没有。

---

## 3. 修复方案（已验证有效）

在 `ReasoningChatOpenAI` 中补 `_create_chat_result` 覆盖：

```python
def _create_chat_result(self, response, generation_info=None):
    result = super()._create_chat_result(response, generation_info)
    # 从原始响应里把 provider 扩展推理字段捞回来
    resp_dict = (
        response
        if isinstance(response, dict)
        else response.model_dump(
            exclude={"choices": {"__all__": {"message": {"parsed"}}}}
        )
    )
    choices = resp_dict.get("choices") or []
    for gen, res in zip(result.generations, choices):
        msg = gen.message
        if not isinstance(msg, AIMessage):
            continue
        rc = self._pick(res.get("message") or {})   # 与流式钩子共用取字段逻辑
        if rc:
            msg.additional_kwargs["reasoning_content"] = rc
    return result
```

其中 `_pick` 与流式钩子共用同一份"多字段名兼容"逻辑
（`reasoning_content` → `reasoning` → `reasoning_details`，取第一个非空），
保证两条路径行为一致。

**建议**：把 `_pick` 抽成 `ReasoningChatOpenAI` 的静态方法，流式与非流式钩子共用，
避免两处字段名列表漂移。

---

## 4. 影响范围与优先级

### 当前实际影响：**有限但需确认**

项目的对话主链路走 `chat.py` 的 **`agent.astream`**（流式），因此**生产对话不受影响**
——思考能正常流式显示（这也与用户此前截图一致）。

**需要排查的是非流式调用点**：
- `deepagents` / `langgraph` 内部是否有 middleware 或工具走 `ainvoke`
- 任何"摘要/标题生成/结构化抽取"类调用（这类常图省事用 `ainvoke`）
- 若存在，这些路径的 thinking 会**静默丢失**（不报错，难察觉）

### 修复优先级

**中**。理由：
- 主链路（流式对话）不受影响
- 但非流式路径丢字段是**静默失败**——不报错、无日志，只在需要 thinking 时才暴露
- 修复成本低（约 20 行，已实证有效）

---

## 5. 通用教训

1. **"直连 provider 有字段" ≠ "经过框架后还有字段"**。中间框架按自己的规范消费响应，
   第三方扩展字段默认不保留。排查任何"字段消失"问题，都要**逐层打印实际结构**，
   而不是推测。

2. **流式与非流式是两条独立的转换路径**，覆盖钩子必须**成对**：
   - 流式 → `_convert_chunk_to_generation_chunk`
   - 非流式 → `_create_chat_result`
   只覆盖一条是常见疏漏（本次即此）。

3. **字段名要按 provider 兼容取值**，不要硬编码单个名字：
   `reasoning_content`（DeepSeek）/ `reasoning`（vLLM Qwen3）/ `reasoning_details`（其他）。

4. **静默丢字段最难排查**。在子类里加一条 debug 日志（当原始响应含 `reasoning` 但
   转换后 `additional_kwargs` 无 `reasoning_content` 时告警），可提前暴露此类问题。

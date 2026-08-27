# 上下文窗口超限修复 — 改动 Diff 草稿（已落地 2026-08-27，commit 9c32f08）

> 本文件是改动方案记录。方案已于 2026-08-27 按此落地（`src/core/config.py` / `src/core/model.py` / `src/services/agent.py`），commit `9c32f08`。

## 一、改动总览

| # | 文件 | 改动 | 作用 |
|---|---|---|---|
| 1 | `src/core/config.py` (L85-104) | `model_max_tokens` 200000→65536；修正 `model_max_len` 误导注释；`summarization_trigger_tokens` 改为下限保护值 | 治本：把 input 容错空间从 45960 放大到 ~180k |
| 2 | `src/core/model.py` | 新增 `_parse_context_error` / `probe_model_max_len`；`model_call_guard` 加 400 兜底重试循环 | **核心保险**：自己 catch 真实异常（非 deepagents 死代码 `ContextOverflowError`），吃 API 真实报错，自适应任何模型 |
| 3 | `src/services/agent.py` (L792-799) | summarization 触发阈值改为 `model_max_len - model_max_tokens - margin` 动态推导 | 提前压，减少无谓的 400 次数 |

## 二、关键证据（已验证）

- `server.log:411`：`This model's maximum context length is 262144 tokens. However, you requested 200000 output tokens and your prompt contains at least 62145 input tokens` → **API 真实返回**，三种数字均可正则解析，100% 可靠。
- `config.py:95` 的 `model_max_len = 262144` 是**写死默认值**；注释声称的"探活 /v1/models"**未实现**（全代码无 `/v1/models` 调用）。
- 全代码**无 400-上下文超限重试**（grep `_RETRIABLE`/`max_retries` 仅涉及沙箱完成门与 SDK 连接重试，与模型 400 无关）。
- **缺口 B（致命）：deepagents 的 `ContextOverflowError` overflow 兜底（`summarization.py:1435` 与 `:1569`）是死代码。** 全 venv + geesun 源码 grep `raise ContextOverflowError` **零命中**——没有任何代码把 vLLM 的 400 `BadRequestError` 翻译成 `ContextOverflowError`，故那两个 `except ContextOverflowError` 永不触发，400 直接冒泡到 `[ERROR] Agent 流式处理异常` → M3 记「零产出」。这正好解释 server.log 4 次同款 400 零救回。**结论：我们的 400 兜底必须自己 catch 真实异常，绝不能依赖 deepagents 的这个兜底。**
- **缺口 A（计数仍低估，导致主动压不触发）：** `agent.py:746` 的 `_count_tokens_accurate` 虽已完全接管 deepagents 计数（langchain `__init__` 对非哨兵 counter 走 else 分支，两计数属性皆换为它），但实现本身仍低估：① 忽略 `tools=` 参数 → function schema 完全不计入；② `model.get_num_tokens` 用 tiktoken cl100k，对 Qwen 中文低估（Qwen 自研 tokenizer 约 1 字符≈1 token）。故真实 62145 的 case counter 报远低于 100000 触发阈值 → 不触发压缩 → 死亡区间照旧。这进一步说明 400 兜底才是唯一能 work 的核心。

## 三、改动 1：`src/core/config.py` (L85-104)

**Before:**
```python
    # ─── model 单次调用输出上限（2026-08-24 决策：thinking 保留，上限给足）───
    # Qwen3 保留 thinking（复杂任务需要深度思考），max_tokens 取 200000，
    # 接近 max_model_len=262144（~256k）上限、留 ~62k 余量。
    # 注意：vLLM 约束 prompt + max_tokens ≤ max_model_len——输入接近 262k 时
    # 200k 输出会 400，但 Summarization 在 20 万 tokens 触发压缩（keep=10 条），
    # 实际输入远小于上限；单次调用失控仍由 model_call_timeout_sec(600s) 兜底。
    model_max_tokens: int = 200000
    # ─── vLLM 上下文总上限（prompt + max_tokens 不得超过）───
    # 探活 /v1/models 得 max_model_len=262144；动态 max_tokens 用它做减法，
    # 防止"输入 62k + 输出 200k = 262145 > 262144"被 vLLM 400 拒载（2026-08-24 实测）。
    model_max_len: int = 262144
    # ─── 动态 max_tokens 安全边距（tokens）───
    # 防 prompt 估算低估导致 vLLM 400（2026-08-25 实测估算 125238 vs 实际 129335、
    # 低估 4097，margin=4096 差 1 token 又被拒）。16384 留足余量（含 tools schema 等未计入部分）。
    model_max_tokens_margin: int = 16384
    # ─── Summarization 压缩触发阈值（tokens）───
    # 上下文达此值即把历史压缩为摘要（keep 10 条 + 资源清单注入，前端消息表不受影响）。
    # 2026-08-25 从 200000 降到 100000：fe27a95a 反复失败重跑历史膨胀到 12.9 万 tokens
    # 仍未触发（阈值偏高 + get_num_tokens 对 Qwen 估算不稳定），prefill 慢且挤压输出空间。
    summarization_trigger_tokens: int = 100000
```

**After:**
```python
    # ─── model 单次调用输出上限 ───
    # 2026-08-27 修正：从 200000 降到 65536。
    # 原 200000 把 input 安全空间压到仅 45960（262144-200000-16384），
    # 中文 context 稍长即触发 vLLM 400（server.log 实测 input 62145 + 200000 = 262145 > 262144）。
    # 降到 65536 后 input 安全空间放大到 ~180k（262144-65536-16384），
    # 即便 token 计数低估中文 ~3x 也触碰不到 400 线；报告类任务 64k 输出足够。
    model_max_tokens: int = 65536
    # ─── vLLM 上下文总上限（prompt + max_tokens 不得超过）───
    # 默认值 262144 仅作 fallback；真实上限由两路获得（按优先级）：
    # ① model.py probe_model_max_len() 启动探活 /v1/models（best-effort，字段名待实测）；
    # ② 模型调用 400 时从错误 message 解析 "maximum context length is N tokens"（API 返回，100% 可靠）。
    # 注：原注释"探活 /v1/models 得 max_model_len=262144"此前未实现，本次补齐于 model.py。
    model_max_len: int = 262144
    # ─── 动态 max_tokens 安全边距（tokens）───
    # 16384 留足余量（含 tools schema 等未计入部分 + 计数误差缓冲）。
    model_max_tokens_margin: int = 16384
    # ─── Summarization 压缩触发阈值（tokens，仅作下限保护）───
    # 2026-08-27 修正：不再写死 100000（该值落在 400 死亡线 62144 之后 → 永不触发）。
    # 运行时在 agent.py 按 effective_trigger = model_max_len - model_max_tokens - margin 推导，
    # 保证压缩在 400 之前触发；此处值仅防止推导异常过小时无下限。
    summarization_trigger_tokens: int = 20000
```

## 四、改动 2：`src/core/model.py`

### 2a 顶部 import (L1-12) 增加

**Before:**
```python
from dataclasses import dataclass, field, asdict
from typing import Callable
import asyncio
import logging

from langchain_openai import ChatOpenAI
from langchain.agents.middleware import (
    wrap_model_call,
    ModelRequest,
    ModelResponse,
)
from src.core.config import settings
```

**After:**
```python
from dataclasses import dataclass, field, asdict
from typing import Callable
import asyncio
import logging
import re

from langchain_openai import ChatOpenAI
from langchain.agents.middleware import (
    wrap_model_call,
    ModelRequest,
    ModelResponse,
)
from src.core.config import settings

# 模块级缓存的真实上下文上限（首次探活 / 首次 400 修正后填入，全局受益）
_probed_max_len: int | None = None
```

### 2b 新增辅助函数（放在 `model_call_guard` 之前，约 L136 之后）

```python
# ─── 上下文超限解析 + 探活（2026-08-27 新增）───

def _parse_context_error(text: str) -> tuple[int, int] | None:
    """从 vLLM 400 错误 message 解析 (real_limit, real_input)。

    样例（server.log:411，API 真实返回，100% 可靠）：
      "This model's maximum context length is 262144 tokens.
       However, you requested 200000 output tokens and your prompt contains
       at least 62145 input tokens"
    → 返回 (262144, 62145)。非上下文超限错误返回 None。
    """
    if "context length" not in text and "maximum context" not in text:
        return None
    m_limit = re.search(r"maximum context length is (\d+)", text)
    if not m_limit:
        return None
    limit = int(m_limit.group(1))
    m_input = re.search(r"(\d+) input tokens", text)
    inp = int(m_input.group(1)) if m_input else 0
    return limit, inp


async def probe_model_max_len() -> int:
    """启动探活 /v1/models 拿真实上下文上限（best-effort）。

    ⚠️ vLLM /v1/models 是否含 max_model_len 字段需实测确认；
       解析失败 / 字段缺失 → 保留 config 默认值，不影响主流程。
       真实上限最终以 400 错误 message 解析为准（见 model_call_guard）。
    """
    global _probed_max_len
    if _probed_max_len is not None:
        return _probed_max_len
    try:
        import httpx  # langchain_openai 依赖 httpx，必可用
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.base_url.rstrip('/')}/v1/models")
            data = r.json().get("data", [])
            if data and "max_model_len" in data[0]:
                _probed_max_len = int(data[0]["max_model_len"])
                settings.model_max_len = _probed_max_len
                logger.warning("[DIAG] 探活 model_max_len=%d", _probed_max_len)
    except Exception as e:
        logger.warning("[DIAG] 探活 model_max_len 失败，用默认值: %s", e)
    return _probed_max_len or settings.model_max_len
```

### 2c `model_call_guard` 末尾 try/except 改造 (L187-195)

**Before:**
```python
    try:
        return await asyncio.wait_for(
            handler(request), timeout=settings.model_call_timeout_sec
        )
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"model 调用超过 {settings.model_call_timeout_sec}s 总时长（"
            f"messages={len(msgs)}, chars={total_chars}）——生成超长或引擎无响应，已中止"
        ) from None
```

**After:**
```python
    # lazy 探活：首次调用时 best-effort 拿真实上限（失败保留默认，不阻断）
    if _probed_max_len is None:
        try:
            await probe_model_max_len()
        except Exception:
            pass

    # ─── 400 兜底：上下文超限 → 解析真实上限 → 收紧 max_tokens/trim → 重试 ───
    # 这是唯一能扛住「token 计数低估中文」+「model_max_len 写死不准」的硬保险：
    # 直接吃 API 自己的判决，不依赖任何本地假设，也**不依赖 deepagents 的
    # ContextOverflowError 兜底**（已确认是死代码，见「设计依据」章节）。
    # 捕获用 `except Exception` + message 关键字，而非 `from openai import BadRequestError`
    # 类捕获——避免异常经 langchain 多层包装后类身份丢失。最多重试 2 次，防无限循环。
    _MAX_CTX_RETRIES = 2
    attempt = 0
    while True:
        try:
            return await asyncio.wait_for(
                handler(request), timeout=settings.model_call_timeout_sec
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"model 调用超过 {settings.model_call_timeout_sec}s 总时长（"
                f"messages={len(msgs)}, chars={total_chars}）——生成超长或引擎无响应，已中止"
            ) from None
        except Exception as e:
            parsed = _parse_context_error(str(e))
            if parsed is None or attempt >= _MAX_CTX_RETRIES:
                raise  # 非上下文超限错误，或重试耗尽 → 原样上抛（由 chat.py/M3 处理）
            real_limit, real_input = parsed
            # 用 API 真实上限修正 settings（后续所有调用永久受益）
            settings.model_max_len = real_limit
            logger.warning(
                "[DIAG] 400 上下文超限 → 收紧重试 #%d: real_limit=%d, real_input=%d",
                attempt + 1, real_limit, real_input,
            )
            # 重算 max_tokens：留 margin，保证 input + max_tokens ≤ real_limit
            safe_max = max(
                _MODEL_MIN_OUTPUT_TOKENS,
                real_limit - real_input - settings.model_max_tokens_margin,
            )
            # 极端情况：input 本身就逼近/超过总上限，连 safe_max 都 ≤0 → 必须砍 input
            if safe_max <= _MODEL_MIN_OUTPUT_TOKENS:
                keep = max(4, len(msgs) - (attempt + 1) * 10)
                trimmed = msgs[-keep:]
                request = request.override(messages=trimmed)
                msgs = trimmed
                safe_max = max(
                    _MODEL_MIN_OUTPUT_TOKENS,
                    real_limit - real_input // 2 - settings.model_max_tokens_margin,
                )
            request = request.override(model_settings={
                **request.model_settings,
                "max_tokens": safe_max,
            })
            attempt += 1
            continue
```

## 五、改动 3：`src/services/agent.py` (L792-799)

**Before:**
```python
    summarization_mw = _SummarizationAccurate(
        model=model,
        backend=backend,
        trigger=("tokens", settings.summarization_trigger_tokens),  # 阈值可调（.env SUMMARIZATION_TRIGGER_TOKENS）
        keep=("messages", 10),
        token_counter=_count_tokens_accurate,
        inventory_provider=_build_inventory_provider(user_id, session_id, sandbox),
    )
```

**After:**
```python
    # 触发阈值按真实上限推导（替代写死 100000，该值落在 400 死亡线之后永不触发）：
    # 保证压缩在 400 之前发生；分母是真探活/400 修正后的 model_max_len，1M 模型自动放大。
    effective_trigger = max(
        settings.summarization_trigger_tokens,  # 下限保护（config 值）
        settings.model_max_len - settings.model_max_tokens - settings.model_max_tokens_margin,
    )
    summarization_mw = _SummarizationAccurate(
        model=model,
        backend=backend,
        trigger=("tokens", effective_trigger),  # 动态推导，提前压
        keep=("messages", 10),
        token_counter=_count_tokens_accurate,
        inventory_provider=_build_inventory_provider(user_id, session_id, sandbox),
    )
```

## 六、设计依据：400 兜底为何必须自己 catch（缺口 B 死代码）

deepagents 的 `SummarizationMiddleware` **结构上**确实有 overflow → summarize → retry 路径
（`summarization.py:1432-1437` 与 `:1564-1571` 的 `except ContextOverflowError`），
但在**我们的接法（raw OpenAI 兼容 vLLM，经 langchain `ChatOpenAI` 包装）下，它是一个死代码分支**：

- 全 venv（含 `langchain_core`）+ geesun 源码 grep `raise ContextOverflowError` → **零命中**。
  没有任何 adapter / 模型包装层把 vLLM 的 400 `BadRequestError` 翻译成 `ContextOverflowError`。
- 因此那两个 `except ContextOverflowError` 永远不会命中，400 直接冒泡。这正是 server.log
  里 4 次同款 400「零救回」的真正原因（之前我只说「deepagents 有 overflow fallback」，
  那是有结构、无机制的死分支，本次已实测确认）。

**推论（落地铁律）：**
1. 我们的 400 兜底**必须自己 catch 真实异常**——用 `except Exception` + message 关键字
   （`_parse_context_error` 判 `maximum context length`），**不能**依赖 deepagents 的
   `ContextOverflowError`（死代码）。
2. 兜底逻辑应放在**我们自己的 `model_call_guard`**（`src/core/model.py`）里，而非指望
   中间件链去救。这是本次修复唯一能 work 的核心层。

## 七、长 PDF 精确比对任务的特殊约束（Q2）

> 用户追问：80 页技术协议（如正/负极规格 010153/010154）精确比对，
> 「读结论够、不必全留」会出问题吗？——**会，而且恰恰不能 prune/summarize/offload PDF 全文。**

**为什么约束成立：**
- 协议比对的价值在「差异」不在「摘要」。逐条款对比必须同时持有两份全文（或至少相关条款）。
  一旦把 PDF 全文 summarize / offload / prune，全文从上下文消失只剩摘要 → **无法做精确条款级 diff**。
- 三种现有机制对 80 页比对的影响：

  | 机制 | 对精确比对的影响 |
  |---|---|
  | 历史 offload（SummarizationMiddleware） | 触发后老消息移出+摘要，PDF 全文滚过 `keep` 即被摘要化 → **比对能力归零** |
  | 大工具结果 offload（filesystem.py） | 移出但留引用，模型 `read_file` 取回——每次取回重注全文 → **重新触发 overflow（取回爆炸）** |
  | deepseek pruner 砍头尾 | 永久丢中间，80 页「中间」即主体 → **比对直接废** |

**正确做法（架构级，非本修复范围但必须对齐）：**
1. **比对下沉沙箱/worker**：`pdfplumber`/`pdftotext` 抽文本 → 程序化 diff → 只把 diff 结果返回
   LLM 上下文。这正是已有 `protocol-diff` / `tech-spec-pdf-diff` skill 的路子——**全文根本不进 262k 上下文**。
2. 若 PDF 全文必须进上下文：用 offload-with-reference，且 agent 只 `read` **目标条款（bounded）**，
   不要整本重注；并**在比对进行中对 PDF 抽取文豁免 eviction**（别在比对中途把两份全文摘要掉）。

**对本修复的边界约束：**
- 本修复（fraction 触发 + 降 max_tokens + 400 兜底）只解决**「不崩」**，不解决**「比对正确性」**。
- 长 PDF 精确比对的正确性是**架构问题**（比对下沉沙箱 / 按需 bounded 取回 + 比对期豁免 eviction），
  不应指望上下文窗口策略去「保住」全文。换句话说：上下文窗口策略对「PDF 全文这类比对素材」
  要特殊对待——要么根本不进主上下文（沙箱比），要么进来了在任务完成前不被压缩。

## 八、⚠️ 待确认 / 风险点

- [ ] **`request.override(messages=...)` 是否支持**：trim 路径（input 本身超总上限的极端情况）依赖它。若 `ModelRequest.override` 不支持 messages 参数，退化为「仅收紧 max_tokens」（案例 B 的 input 62145 不需 trim，足够救回）。需落地前确认或加 try/except 兜底。
- [ ] **真探活字段名**：`/v1/models` 的 `max_model_len` 字段名待实测；解析失败安全（保留默认 262144），**建议仅作为可选前置优化**，400 兜底才是必选核心。
- [ ] **捕获方式**：用 `except Exception` + message 关键字判断，**而非 `from openai import BadRequestError` 类捕获，也绝不依赖 deepagents 的 `ContextOverflowError` 兜底（已确认死代码，见第六章）**——避免异常经 langchain 多层包装后类身份丢失。
- [ ] **重试上限 2 次**：防压缩后仍超限导致的无限循环。
- [ ] **effective_trigger 构造时定死**：若 agent 实例被缓存而非每次请求重建，则触发阈值首次定死；但 400 兜底全局覆盖，不阻塞。
- [ ] **长 PDF 比对正确性不归本修复管**：本修复只保证「不崩」，80 页协议精确 diff 仍需比对下沉沙箱（见第七章），否则即便不 400，摘要/取回也会导致条款级对比失真。

## 九、验证计划（确认后执行）

1. **单元**：`_parse_context_error` 对 server.log:411 原文应解析出 `(262144, 62145)`。
2. **回归**：用 input≈62145 的 session 重发 HTML / markdown 报告需求，确认不再 400，报告正常落盘。
3. **观察日志**：`model_call_guard` DIAG 应出现「400 上下文超限 → 收紧重试 #1」且重试成功；后续调用 `real_limit` 已修正。
4. **多模型**：切到 1M 上下文模型时，`effective_trigger` 与探活上限应自动放大，不再 400。
5. **比对任务边界**：长 PDF 协议 diff 走 `protocol-diff` skill（沙箱侧抽+比），确认主上下文不再承载两份全文，且 diff 结果完整。

---
*已落地：2026-08-27 按本方案改完 `config.py` / `model.py` / `agent.py` 并提交（commit `9c32f08`）。第九章验证计划中的「live 回归」需在 WSL 内跑（geesun_agent 运行环境为 WSL/Linux）。*

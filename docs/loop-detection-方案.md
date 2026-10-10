# GraphRecursionError 循环防护 — B 方案落地设计

> 目标：防止 agent「工具全成功但整体不收敛」烧满 recursion_limit=200 抛 GraphRecursionError。
> 参考：deer-flow `loop_detection_middleware.py`（硬剥 tool_calls）+ deepseek-harness `repeat-tool-reminder`（软注入提醒）。
> 状态：✅ **已实现编码并合入 agent.py + 全部验证通过**。
> - `src/core/loop_detection.py`：移植版中间件（已写码）
> - `src/services/agent.py`：middleware list 链尾已追加 `LoopDetectionMiddleware`（2026-09-08 合入）
> - `src/core/config.py`：新增 4 个 `loop_detect_*` 阈值
> - `tests/core/test_loop_detection.py`：5 个单测，**5/5 通过**
> - 验证：`create_deep_agent` 整链组装冒烟通过（graph.get_graph() 可见 `LoopDetectionMiddleware.before_agent/after_model/after_agent` 节点）

## 0. 根因回顾（为什么现有 4 道防护失明）

| 防护 | 位置 | 判定 | 这次为何绕过 |
|---|---|---|---|
| 工具连续失败检测 | chat.py:972 | 只数 `is_error=true` | 工具每次返回 OK，计数恒 0 |
| 无进展循环检测 | chat.py:994 | `_tool_intent_sig` 指纹连续重复 | 每次参数/结果都变，指纹不重复 |
| 工具失败阈值 | config.py:84 | 只数失败 | 无失败 |
| M3 完成门 | chat.py:1266 | 只管 /reports/ 文件 | 内容型任务/无新文件不拦截 |

## 1. 核心事实（源码实锤，决定"能否直接抄"）

- **deepagents 就是 langchain 的薄封装**：
  - `deepagents/graph.py:13` `from langchain.agents import create_agent`
  - `deepagents/graph.py:865` `return create_agent(...)` — `create_deep_agent` 内部委托
  - `deepagents/graph.py:24` `from langgraph.graph.state import CompiledStateGraph` — 返回 langgraph graph
  - `deepagents/graph.py:878` `.with_config(...)`
- **我们 agent.py:821 已经传 middleware=[...]**，其中 `_SummarizationAccurate`（agent.py:244）已用 `awrap_model_call`。
- **langchain-1.3.13 `AgentMiddleware` 基类**（middleware/types.py）含 deer-flow 用到的全部 hook：
  `before_agent`(419)/`abefore_agent`(430)、`after_model`(467)/`aafter_model`(478)、
  `wrap_model_call`(491)/`awrap_model_call`(586)。

**结论：deer-flow 的 `LoopDetectionMiddleware` 可直接移植，无需改 deepagents 任何 API。**
只需作为一个新的 middleware 类 append 到 agent.py:821 的 list。

## 2. 设计：两段式（软提醒 + 硬剥 tool_calls）

做成**单个 `LoopDetectionMiddleware`**，内部两层检测 + 两种介入：

### Layer 1：hash 级（重复工具调用组）
- 对每个 AIMessage 的 `tool_calls` 做稳定 hash（名 + 参数摘要，见 deer-flow `_hash_tool_calls`）
- 同一 hash ≥ `warn_threshold`(3) 次 → 注入软提醒（human msg）
- 同一 hash ≥ `hard_limit`(5) 次 → **剥掉 tool_calls** 逼模型输出最终文本

### Layer 2：频次级（同工具类型高频，捕获"换参数/换文件"型空转）
- 统计同 tool 名在滑动窗口内出现次数（deer-flow 默认 warn=30/hard=50，我们需要调低）
- ≥ 阈值 → 同上软提醒 / 硬剥

### 介入方式（关键，对齐 deer-flow）
- **软提醒**：`awrap_model_call` 里把 `HumanMessage(name="loop_warning")` 追加到消息末尾
  （必须在 wrap_model_call 注入，绝不能 after_model——否则破坏 tool_calls↔ToolMessage 配对，OpenAI/Moonshot/Anthropic 都会拒）
- **硬剥**：`aafter_model` 里返回 update，把 AIMessage 的 `tool_calls=[]` + `content=` 末尾拼 `[FORCED STOP]`
  → 模型下一轮被迫出纯文本，循环自然结束，**SSE 不中断**（不抛异常）

## 3. 需要适配的点

> **⚠ 修订说明（2026-10-10，会话 GY24428:aaf95335）**
> 本节标题原写作"需要适配的 4 个点（**不是改 API**，是我们环境的微调）"。
> 这句"不是改 API"事后证明是**误判**：真正咬人的是下面第 **0** 行 ——
> 上游中间件读的字段名，是**它自己那套工具集**的契约；换了工具集，这一层必须
> 逐项重新核对，没有"默认继承"这回事。漏掉它导致 `read_file` 的稳定指纹恒为
> `:0-0`（读哪个文件、读哪一段都不影响）→ 窗口内攒够 5 次必然硬停。
> 完整取证与检查单见 `AGENTS.md` 同名条目。

| # | deer-flow 原版 | 我们调整 | 原因 |
|---|---|---|---|
| **0** | `_stable_tool_key` 读 `read_file` 的 `path` / `start_line` / `end_line` | **字段名改为从工具真实 `args_schema` 反查**（槽位表 + `_resolve_field`），并在出现 schema 外字段时 ERROR 告警 | deer-flow 的 `read_file` 是它**自研**的沙箱工具（`sandbox/tools.py:2148`，签名就是 `path`/`start_line`/`end_line`），我们的是 **deepagents 内置**（`file_path`/`offset`/`limit`）——**不是同一套契约**。硬编码上游字段名 → 三个都取不到 → 静默降级成常量指纹 → 必然误判。**这是 2026-10-10 事故的直接根因** |
| 1 | `from deerflow.config.loop_detection_config import LoopDetectionConfig` | 删掉，阈值直接构造器传入 / 从 settings 读 | 我们没这 config 模块，且阈值要可配 |
| 2 | `runtime.context["thread_id"]` | **用 `runtime.execution_info.thread_id`**（适配点，源码实锤） | `Runtime` **不含 config**（langgraph/runtime.py:131 明示 "Runtime does not include config"），`runtime.context` 拿不到 thread_id；`ExecutionInfo.thread_id`（runtime.py:39）才是 chat.py:341 注入的 `graph_config.configurable.thread_id` 的落点 |
| 3 | `_DEFAULT_TOOL_FREQ_WARN=30 / HARD=50` | **调低到 warn=12 / hard=20** | 我们 recursion_limit=200，正常流程 25-35 步；30 次同工具才拦太晚，可能已被烧满 |
| 4 | `_stop_reason` 机制（loop_capped）暴露给子代理 | 简化：不暴露 stop_reason（我们无子代理 executor），只内部触发 | 降低移植面 |
| 5 | `BoundedDict`（deer-flow 自带） | 换成 we 的 `collections.OrderedDict` 或直接 dict | 少一个依赖，LRU 清理可为可选 |

> **约定**：本方案文档中 "we use `runtime.context["thread_id"]`" 为**迁移期错误表述**，实际落地第 2 点以 `runtime.execution_info.thread_id` 为准（见上表）。

### 3.1 工具层契约的落地形态（2026-10-10 新增）

字段名不再由 `loop_detection.py` 断言，而是**从实际持有的工具反查**：

| 环节 | 实现 | 说明 |
|---|---|---|
| 契约来源 | `ModelRequest.tools`（`wrap_model_call` 内，langchain `types.py:99`） | 模型侧工具全集；首次学习后缓存，之后零成本 |
| 契约提取 | `_extract_schema_fields(tool)` | 吃下 `BaseTool.args_schema` / OpenAI function dict / 裸 properties；**取不到返回 `None`**（"契约未知"≠"无字段"） |
| 槽位解析 | `_resolve_field(args, schema_fields, candidates)` | schema 权威（本次 args 没传也算命中）+ args 探测兜底（schema 未知时） |
| 工具分类 | 按 schema 特征，不按工具名 | 分页读取型（路径+行段）/ 内容敏感型（含 payload 字段）/ 其余（显著字段） |
| 漂移告警 | `_detect_contract_drift` + `_check_contract_drift` | args 出现 schema 外的槽位字段 → **ERROR 一次（限流）**；把"静默降级"变成"可观测" |
| 不变量 | 指纹永不塌成常量 | 路径取不到值时退到全参 hash（**最坏漏报，绝不误杀**） |

顺带修掉的同类问题：内容敏感分支原写 `{"write_file", "str_replace"}` —— `str_replace`
是 deer-flow 的工具名，本项目叫 `edit_file`，该分支**从未命中**；现按 schema 判定后
`edit_file` 自动覆盖。

## 4. 插入位置（agent.py:821 middleware list）

先看当前顺序（agent.py:821-831）：
```
_FreshSkillsMiddleware,   # 链首：刷新 skill
switch_model,             # 模型切换
file_to_image,            # 视觉文件转 image_url
summarization_mw,         # 历史 offload
model_call_guard,         # 引擎真实 token 写入缓存
```

**插入建议**：放在 `model_call_guard` 之后（链尾），作为**最后一道守卫**：
```
LoopDetectionMiddleware(...)  # ← 新增，链表尾
```
原因：
- 它读 `state["messages"]` 末尾的 AIMessage.tool_calls（after_model 时），应排在所有会改 messages 的 middleware（如 file_to_image）之后，避免误读已转换的消息。
- 它 `awrap_model_call` 追加 human msg，应在 `model_call_guard` 之后（不干扰其 token 缓存写回）。

## 5. 需要改的文件清单

| 文件 | 改动 | 状态 |
|---|---|---|
| `src/core/loop_detection.py` | **新文件**：`LoopDetectionMiddleware` 移植版中间件（两段式：软提醒 + 硬剥） | ✅ 已写码 |
| `src/core/loop_detection.py` | **契约反查层**（2026-10-10）：`_extract_schema_fields` / `_resolve_field` / `_detect_contract_drift` + `wrap_model_call` 学习工具契约 | ✅ 已合入 |
| `src/services/agent.py` | ① 新增 `LoopDetectionMiddleware` import；② middleware list 链尾追加它 | ✅ 已合入（2026-09-08） |
| `src/core/config.py` | 新增 4 个可配项：`loop_detect_warn_threshold=3`、`loop_detect_hard_limit=5`、`loop_detect_tool_freq_warn=12`、`loop_detect_tool_freq_hard=20` | ✅ 已合入 |
| `tests/core/test_loop_detection.py` | 单测（原 5 个 → 现 **20 个**）：含契约反查、漂移告警、真实 schema 守门人 | ✅ 20/20 通过 |
| `AGENTS.md` | 新增「移植上游中间件时的工具层契约漂移」条目 + 移植检查单（5 项） | ✅ 已合入（2026-10-10） |
| （可选）`src/api/endpoints/chat.py` | 在 `except Exception` 前加 `except GraphRecursionError`：转成 error 事件，提示"已达步数上限"，避免纯断流（A 方案兜底） | ⬜ 待批（未落地） |

> 建议 A 方案（chat.py catch GraphRecursionError）也一起做——它是兜底，即使 loop 检测有漏网，也不会再"看似卡死"。

## 6. 验证方案

1. **单元测试（spike-then-verify）**：写一个 mock 的 agent，重复调同一工具 6 次，断言：
   - 第 3 次后 messages 出现 loop_warning human msg
   - 第 5 次后 AIMessage.tool_calls 被剥空
   - ✅ **已完成**：`tests/core/test_loop_detection.py` 5 个用例 5/5 通过
2. **本地回归**：跑一个正常不循环的任务，断言不触发（无误报）。—— ⬜ **真实任务回归待执行**
   - ✅ **单元级回归已补**（2026-10-10）：`test_no_false_hard_stop_when_reading_different_files`
     （连续读 6 个不同文件不得硬停）+ `test_real_repeat_still_caught_with_real_contract`
     （真重复仍须硬停）双向对照 —— 修复不得把检测能力一起废掉。
   - ⬜ 真实任务回归（起服务跑一个逐文件读取的普通任务）仍待执行：本次事故正是
     因为这一项当时标着 ⬜ 未执行，缺陷才绕过所有单测漏到生产。
3. **生产冒烟（67）**：真实触发一次循环任务（或构造），观察不再烧满 200，且 SSE 正常结束。
   - ✅ **组装冒烟已过**：`create_deep_agent` 整链编译通过（venv 实测）
   - ⬜ 生产真实触发验证待 67 执行

## 7. 风险与取舍

- **误报风险（最重要）**：正常长任务（如逐章节读 40 个文件）会触发 Layer 2 频次检测。
  **对策**：① 频次阈值调够高（不轻易触发）；② `read_file` 等读型工具默认豁免
  （⬜ **未采用** —— 2026-10-10 选择先除根因：`read_file` 指纹不再是常量后，
  "读不同文件被误判"的必然性已消除；只读豁免是否仍需要，等真实任务回归数据再定）；
  ③ 只在"零新交付物"窗口才算重复（对齐我们已有 `_files_in_window` 思路）。
- **硬剥 tool_calls 可能丢中间结果**：剥掉时模型还没最终答案。**对策**：硬剥消息内容里带
  `[FORCED STOP] ... summarizing results so far`，让模型收尾而非丢弃。
- **跨会话状态**：middleware 实例长驻，需按 thread_id 清理（deer-flow 已有 LRU evict），
  我们按 session_id 做 key。

---
**待你确认**：是否按此落地？需不需要我先把 deer-flow 的 `LoopDetectionMiddleware` 完整读出来做成可运行的移植版（含上述 4 点适配）给你 review，再合入 agent.py？

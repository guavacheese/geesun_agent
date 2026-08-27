# 前端 UI 交互范式重构方案（完全借鉴 deer-flow）

> 目标：把 geesun_agent_web 的"顶部状态条 + 独立卡片"式交互，整体重构为 deer-flow 的"**消息流内联 + 时间线 + 计时动画**"范式——状态跟着内容走，思考过程可读可折叠，工具执行成时间线，全程有实时反馈。
> 范围：仅前端 `geesun_agent_web`（main，`app/chat/components/`）。**后端零改动**（所需数据事件 reasoning/token/tool_call(id)/tool_result 均已具备，上一轮已核对）。
> 关联：`前端交互状态机与循环检测重构方案.md` 解决"**状态怎么算**"（派生式，已实施）；本文档解决"**状态怎么显示**"（UI 范式）。两者正交互补。
> 状态：**设计稿，待评审**。评审通过后按 Phase 逐阶段实施，每阶段独立 commit+push。

---

## 1. 核心范式转变（为什么这么做）

| 维度 | 现状（geesun） | 借鉴后（deer-flow 范式） |
|---|---|---|
| 状态指示位置 | 顶部整行色块条（`ChatArea.tsx:583` AgentStatusBar） | **内联在消息流**，跟随正在生成的消息（3 点跳动 + 计时） |
| 思考过程 | 气泡内 `<details>`，mono 11px 原文，永远折叠（`MessageItem.tsx:158-168`） | 独立可折叠块，**流式展开 / 完成自动收起**，标题实时计时"思考中 (12s)"，内容 markdown |
| 工具执行 | ToolCallCard 列表，裸工具名 + 参数 basename | **垂直时间线**：类型图标 + 人话动作标签 + 连接线 + 结果内联 badge |
| 吐 token | 顶部 bar"生成中…" | 文本流式即指示本身（无需额外 bar）；prefill 等待期用 3 点 + 计时 |
| 反馈密度 | 无计时、无耗时 | 思考计时、完成耗时"共 27s"、每轮用时 |

**核心原则**：状态是内容的影子，不是独立的 UI 层。用户的眼睛跟着内容走，指示器就出现在下一个字要出现的位置。

---

## 2. 目标消息单元解剖（一条 AI 消息的最终结构）

```
[ReasoningBlock]  思考过程（可折叠）
  ├─ 流式中：🧠 思考中 (12s)         ← shimmer + 实时计时，默认展开
  └─ 完成后：🧠 思考过程 (共 27s)     ← 1s 后自动收起，可再点开，内容 markdown

[答案 markdown]   流式渲染（现有 MarkdownRenderer 复用）

[ToolTimeline]    工具执行时间线（垂直）
  ├─ [icon] 上传规格文档 010154_C.pdf        ✔ 完成
  ├─ [icon] 生成差异报告 diff_10154_10957.json  ⏳ 执行中（当前步高亮）
  └─ 展开 N 步（步骤多时前序折叠）

[GeneratedFileCard] 文件卡（现有，不动）

[耗时] 本轮用时 38s
```

---

## 3. 组件级改造清单（对照 deer-flow 真实源码）

| 新组件/能力 | deer-flow 参照（已核对） | 我们落点 | 数据来源 |
|---|---|---|---|
| `StreamingIndicator`：3 点错峰跳动 | `streaming-indicator.tsx:10-31`（0/0.2s/0.4s 延迟） | 新建，供思考/prefill 内联指示 | 无（纯 UI） |
| `useElapsedTimer`：1s 实时计时 hook | `reasoning.tsx:137-157` LiveTimer | 新建 | 流起始时间戳（runStream 记录） |
| `ReasoningBlock`：Brain+计时+自动开合+markdown | `reasoning.tsx:48-124`（流式展开/结束 1s 自动收起 L92-103）、`chain-of-thought.tsx:87-114` header | 新建，替换 `MessageItem.tsx:158-168` 的 `<details>` | `message.reasoning`（已有，改 markdown 渲染） |
| `ToolTimeline` + `ToolStep`：垂直连接线 + 状态 | `chain-of-thought.tsx:123-163`（icon + 竖线 + complete/active/pending） | 改造 `ToolCallTimeline.tsx` | `message.tool_calls`（已有） |
| `toolActionLabel`：工具名+args → 人话动作文案 | `message-group.tsx:545-572/694-698/815-862`（"Read file: path"等） | 新建映射表 | `tool_call.tool` + `.args`（已有） |
| `toolIcon`：按工具类型映射 lucide 图标 | `message-group.tsx:3-17`（search/folder/book/globe/monitor…） | 新建映射表 | `tool_call.tool` |
| `ResultBadge`：路径/关键结果内联 | `chain-of-thought.tsx:167-191` | 改造 `ToolCallCard.tsx` 折叠态 | `tool_call.result` |
| 前序步骤折叠"展开 N 步" | `message-group.tsx:321-334` | 改造 `ToolCallTimeline.tsx` | — |
| 状态内联（思考/生成/工具） | `streaming-indicator.tsx` + `run-duration.tsx:30-39` | 移除顶部 `AgentStatusBar`（`ChatArea.tsx:583`），改消息流内联 | 派生式 `agentStatus`（上一轮已实施） |

**保留不动**：`MarkdownRenderer`、`GeneratedFileCard`、`FilePreviewModal`、`MessageInput`、`WelcomeScreen`、派生式状态机逻辑（ChatArea 的 streamSig/deriveAgentStatus）。

---

## 4. 视觉设计规格

### 4.1 状态文案与配色（沿用现有 tailwind 体系，对齐浅/深色）

| 状态 | 文案 | 视觉 |
|---|---|---|
| thinking（prefill/CoT） | `思考中 (12s)` | 3 点跳动（accent 色）+ 灰色文字 + 计时 |
| running_tool | 时间线当前步高亮（accent ring + pulse） | 步骤图标旋转/呼吸 |
| generating | 无额外 bar（流式文本即指示） | — |
| success/error（工具步） | 步尾 ✔ / ✗ | emerald / destructive |

### 4.2 计时格式

- 思考中：`思考中 (12s)`（流式，LiveTimer）
- 思考完成：`思考过程 (共 27s)`（收起后标题）
- 每轮用时：消息底部 `本轮用时 38s`

### 4.3 工具图标 + 动作标签映射（起始表，可扩展）

| 工具名（前缀匹配） | lucide 图标 | 中文动作模板 |
|---|---|---|
| upload_to_sandbox / decrypt_and_upload_to_sandbox | UploadCloud | 上传文件 `{file_path basename}` 到沙箱 |
| download_from_sandbox | Download | 从沙箱下载 `{remote_path}` |
| run_pdf_diff_stage3 / 含 diff 类 | FileDiff | 生成差异报告 `{json basename}` |
| read_file / write_file | BookOpen / NotebookPen | 读取/写入文件 `{path}` |
| bash / exec / shell | SquareTerminal | 执行命令 `{command 截断}` |
| glob / find / grep / search | Search | 搜索 `{pattern}` |
| 其它 | Wrench | 使用工具 `{name}` |

---

## 5. 实施阶段（每阶段独立 commit+push）

### Phase 1 — 基础设施（纯新增，零风险）
新建 3 个无依赖模块：
- `StreamingIndicator.tsx`：3 点跳动动画（tailwind 自定义 keyframes）
- `useElapsedTimer.ts`：`useElapsedTimer(startAt: number | null)` → 每秒更新 elapsed
- `tool-visuals.ts`：`toolActionLabel(tool, args)` + `toolIcon(tool)` 映射表（含 4.3 起始表）

**验收**：tsc 通过；组件可在 Storybook/任意页面独立渲染。

### Phase 2 — ReasoningBlock 思考块升级
- 新建 `ReasoningBlock.tsx`（Brain + 计时 + 自动开合 + markdown 渲染）
- 替换 `MessageItem.tsx:158-168` 的 `<details>` 块
- 计时起点：`message` 对应流的开始（ChatArea 传 `streamStartedAt`，runStream 起始记录）

**验收**：流式时标题跳秒"思考中 (12s)"，结束 1s 后自动收起，内容 markdown 渲染、可再点开。

### Phase 3 — 工具时间线
- `ToolCallCard.tsx`：折叠态改人话标签 + 类型图标 + 结果 badge；展开态保留参数/结果详查
- `ToolCallTimeline.tsx`：卡片间加垂直连接线；步骤 > N（如 4）时前序折叠"展开 N 步"

**验收**：多工具调用呈时间线；运行中步骤高亮；旧步骤可折叠。

### Phase 4 — 状态内联化（范式收口）
- `ChatArea.tsx`：移除 `AgentStatusBar`（L583 整行条）
- 在消息流末尾（最后一条 AI 消息下方）内联渲染：
  - thinking/prefill → `StreamingIndicator` + `思考中 (Ns)`
  - running_tool → 由时间线当前步承担（无需额外元素）
- `AgentStatusBar.tsx` 删除（或保留为"顶部极简进行中指示"可选开关，默认关）

**验收**：无顶部条；长对话中状态跟内容走；prefill 等待期有 3 点 + 计时。

### Phase 5 — 打磨（可选增强）
- 每轮耗时：消息底部"本轮用时 Xs"（`useElapsedTimer` 复用）
- 深色模式/窄屏回归；动画性能（计时器仅 1 个 interval，消息完成即清理）

---

## 6. 风险与回滚

| 风险 | 缓解 | 回滚 |
|---|---|---|
| 移除顶部状态条后长对话可感知性下降 | 思考/工具状态已内联且带计时；如需可开"顶部极简点"开关（默认关） | 恢复 `AgentStatusBar` 渲染一行 |
| 计时器泄漏/性能 | 每个计时器在消息完成/卸载时清理；单 interval 复用 | Phase 1 组件可独立删除 |
| 工具标签误判（动作文案写错） | 前缀匹配 + 兜底"使用工具 {name}"；映射表集中维护 | 改表即可，无逻辑耦合 |
| reasoning 含非 markdown 内容渲染异常 | 复用 MarkdownRenderer 既有容错；异常降级纯文本 | — |
| 与派生式状态机耦合 | 仅消费 `agentStatus`/`message.reasoning`/`tool_calls`，不改其计算逻辑 | 纯 UI 层，可整体 revert |

---

## 7. 回归验证清单（fe27a95a 重跑 + 手工）

1. prefill 等待期（30-60s）：消息流内 3 点跳动 + "思考中 (Ns)"，无顶部条。
2. 思考过程：流式展开、结束 1s 自动收起、markdown 渲染、点开可回看。
3. 工具执行：多步呈时间线、当前步高亮、成功/失败图标明确、结果 badge 内联。
4. 吐 token：文本流式正常，无"思考中"误标伴随答案（派生式已保证）。
5. 刷新会话：文件卡/时间线完整（依赖既有 DB+目录扫描兜底，不受影响）。
6. `tsc --noEmit` 0 错误；浅/深色模式均无不可读。

---

## 8. 参考源码位置（已核对）

- deer-flow：`frontend/src/components/workspace/streaming-indicator.tsx`（3 点动画）、`frontend/src/components/ai-elements/reasoning.tsx`（计时/自动开合）、`frontend/src/components/ai-elements/chain-of-thought.tsx`（时间线步骤）、`frontend/src/components/workspace/messages/message-group.tsx`（工具人话标签/图标/结果 badge）、`frontend/src/components/workspace/messages/run-duration.tsx`（耗时）、`frontend/src/components/ai-elements/shimmer.tsx`（文字扫光）。
- geesun：`app/chat/components/AgentStatusBar.tsx`、`MessageItem.tsx:158-168`（details 思考块）、`ToolCallCard.tsx`、`ToolCallTimeline.tsx`、`ChatArea.tsx:583`（顶部状态条）、`ChatArea.tsx:218-277`（reasoning/tool_call/tool_result 事件→信号，已派生式）。

---

## 9. 落地顺序建议

Phase 1（基础设施）→ Phase 2（思考块）→ Phase 3（工具时间线）→ Phase 4（状态内联化）→ Phase 5（打磨）。
每阶段 commit+push 至 `geesun_agent_web` main，message 沿用 problem/root cause/fix/impact 四段式；本文档 commit 至 `geesun_agent` master。

# Agent 平台运行规则

## 文件系统规则（严格遵守）
你的所有操作都在以下目录中进行。系统会告诉你每个会话的真实路径，
你只需要把 {user_id} 和 {session_id} 替换成系统给的实际值。

| 路径 | 用途 | 权限 |
|---|---|---|
| /uploads/{user_id}/{session_id}/ | 输入文件（PLC源码、Excel等） | 只读 |
| /reports/{user_id}/{session_id}/ | 输出报告 | 写入 |
| /workspace/memories/ | 用户偏好与长期记忆 | 读写 |

规则：
- 输入文件从 /uploads/{user_id}/{session_id}/ 读取，不要尝试其他路径
- 所有报告写入 /reports/{user_id}/{session_id}/
- 用户偏好写入 /workspace/memories/，持久化到数据库
- 不要使用 /workspace/、/code/、/mnt/ 等路径

## MCP 工具使用规范
以下所有 MCP 工具**都不经过 LLM 上下文**，文件直接在宿主机 ↔ 沙箱之间传输：

| 文件类型 | 使用工具 | 说明 |
|---------|---------|------|
| 任意文件（含加密） | `upload_to_sandbox` | **统一入口**：自动检测 `%TSD-Header` 加密头，命中即内部切换为解密上传（等价 `decrypt_and_upload_to_sandbox`，明文不落盘）；非加密文件直传沙箱 |
| Skill 脚本 | `copy_script_to_sandbox` | 从 skills 目录传沙箱 |
| 输出报告 | `download_from_sandbox` | 从沙箱拉到 /reports/ |

> `decrypt_and_upload_to_sandbox` 仍保留可用，但模型**无需再手动区分**加密/非加密——一律用 `upload_to_sandbox` 即可，工具会自行兜底解密。

## 正确写入沙箱的方式
**write_file 只能写虚拟文件系统路径（/reports/、/workspace/memories/），不能写 /home/user/！**
往沙箱写文件只能通过 MCP 工具：

- 任意文件（加密/非加密统一）：`upload_to_sandbox(file_path="/uploads/...", remote_path="/home/user/文件名", sandbox_id="...")`
  - 工具会自动检测 DLP 加密头（`%TSD-Header`），命中即内部解密上传，无需手动选工具
  - （`decrypt_and_upload_to_sandbox` 仍可用，行为等价，但不再需要区分调用）

不要使用 write_file /home/user/xxx —— write_file 写不到沙箱里。

## write_file 路径规则（严格）

**⚠️ 严重警告：文件只能写到 `/reports/{user_id}/{session_id}/`，写错位置会导致文件无法预览和下载。**

- `/uploads/` 是**用户上传的输入文件**目录，**只读**，不可写入
- `/reports/` 是**Agent 生成的输出文件**目录，**可写**，所有给用户下载的文件必须写到这里
- 写错到 `/uploads/` 的文件虽然会存在宿主机磁盘上，但**不会触发 `file_generated` 事件**，导致前端不显示文件卡片，用户无法预览和下载

**判断准则（每次 write_file 前必须确认）：**
1. 文件是给用户预览/下载的 → **必须**写 `/reports/{user_id}/{session_id}/`
2. 文件是用户偏好 → 写 `/workspace/memories/`
3. 其他路径（`/uploads/`、`/home/user/`、`/tmp/` 等）→ **不允许**

## write_file 与 download_from_sandbox 的生命周期关系（极其重要）
**write_file 写到 /reports/ 的文件，已经在宿主机上，不需要再 download。**

write_file 通过 filesystem middleware 直写宿主机磁盘，不经沙箱。
因此：
- **如果你用 write_file 创建了 /reports/... 下的文件，该文件已经在宿主机的报告目录中，立即可用。**
- **不要对 write_file 刚写好的文件再调 download_from_sandbox**——那会试图从沙箱下载同一个文件，不仅多余，而且在沙箱未创建时必定失败。
- **download_from_sandbox 的唯一适用场景**：在沙箱内用 `execute` 运行脚本，脚本在沙箱文件系统 `/home/user/` 下生成了输出文件，需要拉回宿主机。
- 判断准则：文件路径是 `/home/user/` → 需要 download；文件路径是 `/reports/` 且刚用 write_file 创建 → 不需要 download。

## 工具与路径对应关系（极其重要）
- read_file / write_file / ls / glob / grep → 访问 /uploads/、/reports/、/workspace/memories/
- execute → 在沙箱中运行命令，沙箱内没有 /uploads/ 和 /reports/
- write_file 只能用于写 /reports/ 下的报告和 /workspace/memories/ 下的用户偏好
- 向沙箱写文件只能用 MCP 工具：`upload_to_sandbox`（统一入口，自动解密 DLP 加密文件）或 `decrypt_and_upload_to_sandbox`（等价，显式解密）
- 不要在 execute 中访问 /uploads/ 或 /reports/ 路径

## 记忆存储规则
- 用户偏好写入 /workspace/memories/user-preferences.md
- write_file /workspace/memories/user-preferences.md   ← 正确
- write_file /uploads/.../user-preferences.md         ← 错误，不会持久化到数据库

## Skill 脚本在沙箱中的执行规范
skill 指令中提到的 Python 脚本存在于宿主机，**不在沙箱内**。
沙箱是一个通用执行环境，**不会预装任何针对特定技能的依赖包**。

**严禁用 read_file 读取脚本内容**——脚本可能很大（如 3000+ 行），读一次就会撑爆上下文并耗尽步数。
必须使用 MCP 工具 `copy_script_to_sandbox` 直传沙箱，不经过 LLM 上下文：

```text
copy_script_to_sandbox(
    script_name="脚本名.py",
    sandbox_path="/home/user/脚本名.py",
    sandbox_id="<沙箱ID>",
    skill_name="<技能名>"
)   # 自动搜索 __system__ → __agent__ → __user_*__，无需指定来源

# 然后安装依赖并执行
# 安装依赖（沙箱下载包速度一般，设置 timeout=600 最多等10分钟）
execute pip install <所需依赖> timeout=600
execute python /home/user/脚本名.py <参数> -o /home/user/

# 脚本输出的报告在沙箱内，用 download_from_sandbox 拉回宿主机
download_from_sandbox(
    sandbox_id="<沙箱ID>",
    sandbox_path="/home/user/输出报告文件名",
    host_path="/reports/{user_id}/{session_id}/报告名"
)
```
注意：永远不要在 execute 中引用 /skills/、/uploads/、/reports/ 路径（沙箱内不存在）。

## 解密规则
- **`upload_to_sandbox` 已内置 DLP 兜底**：自动检测 `%TSD-Header` 加密头，命中即内部切换为解密上传（与 `decrypt_and_upload_to_sandbox` 等价，明文不落盘）。因此**一律用 `upload_to_sandbox` 即可，无需判断文件是否加密、也无需手动换工具**。
- `decrypt_and_upload_to_sandbox` 仍保留可用，行为完全等价，仅在需要显式表达"我要解密"语义时使用。
- **判断文件是否加密：不靠扩展名，靠文件头**。公司 DLP 加密软件会在文件开头写入 `%TSD-Header` 魔数——**txt/py 等文本类文件也可能被加密**。不确定时：
  1. 先尝试 `read_file`——如果被系统拒绝（二进制拦截提示），说明是加密/二进制文件
  2. 或直接走 `upload_to_sandbox`（它内部已自动检测加密头并兜底解密，非加密文件同样直传）
- **禁止用 read_excel 直接读取 /uploads/ 下的加密 Office 文件**：`/uploads/` 是虚拟路径，只在虚拟文件系统（read_file/ls/glob/grep）和 MCP 传输工具里有效；read_excel 底层用真实文件系统 open()，宿主机与沙箱均无 `/uploads` 目录，必然报 No such file；且文件为密文，路径通了也读不出内容
- 加密文件的正确读取流程：`upload_to_sandbox(file_path="/uploads/...", remote_path="/home/user/文件名", sandbox_id="...")` → 工具自动解密 → 沙箱 `/home/user/` 下用 execute / read 处理
- 同一目录下的文件名可用 `ls /uploads/{user_id}/{session_id}/` 确认（虚拟文件系统可列出），但**不要**用 execute 在沙箱里验证 /uploads（沙箱内不存在）
- **PDF 解析必须用标准库 API，禁止手动遍历 PDF 内部对象**：
  1. `fitz`（PyMuPDF）：`doc = fitz.open(path)` → `doc[页码].get_text()` 提取文本
  2. 或 `pdfplumber`：`with pdfplumber.open(path) as pdf` → `page.extract_text()`
  3. **禁止**自己写脚本遍历 PDF 的 CMap 字符映射/对象流——那会把二进制映射数据当文本输出，表现为"乱码"（2026-08-13 实测：AI 手动遍历 CMap 得到 65309 条乱码段；同文件用 fitz.get_text() 解析完全正常）
  4. 若 `get_text()` 返回空/乱码，先检查文件头：`%TSD-Header` 说明仍是密文（未解密），重新 `upload_to_sandbox`（会自动解密）；`%PDF-` 才是明文
- **沙箱内 pip 装包默认走内网 devpi 源**（沙箱创建时已配置
  `http://192.168.10.136:3141/root/pypi/+simple/`），直接 `pip install <包名>` 即可；
  **不要改 pip 源/加 --trusted-host**——外网 pypi https 被公司上网认证网关
  MITM 重签证书，公共 CA 不认，现场改源必然失败（2026-08-14 实测）
- pymupdf/pdfplumber 等常用库已预装在镜像内，通常无需再装；装其他包（如 openpyxl）直接 pip install

## 沙箱内文件写入（v3.1）
- **write_file 支持直接写沙箱路径**：`/home/user/xxx` 和 `/tmp/xxx` 已放行，会经 e2b 上传通道写入沙箱（仅 UTF-8 文本）
- 写脚本/中间文件直接用 `write_file /home/user/脚本.py`，或 `execute` 里 shell 创建，二选一即可
- `/root/`、`/mnt/`、`/code/`、`/var/` 是沙箱内系统级/挂载路径，禁止写入
- **沙箱内文件不算交付物**：要给用户看的报告必须 `download_from_sandbox` 到 `/reports/{user_id}/{session_id}/`

## Skill 创建（v3.1）
- Skill 分三层：`/skills/__system__/`（预装，只读）、`/skills/__agent__/`（agent 自创，**可写**）、`/skills/__user_{user_id}__/`（用户上传，归 /api/v1/skill/upload 管，AI 只读）
- **创建/更新自创 skill**：`write_file /skills/__agent__/<skill_name>/SKILL.md`（YAML frontmatter 需含 name + description，name 与目录名一致；格式不合法该 skill 不会被加载）
- **用户共享 skill**：通过 `/api/v1/skill/upload` 接口（前端"技能"面板上传），不要直接 write_file 用户目录
- 不要写 `/skills/__system__/`（系统预装只读）

## Skill 优先使用规则（强制，违反即白烧步数）
- **接到任务先查 skill**：`ls /skills/__system__/`、`ls /skills/__agent__/`、
  `ls /skills/__user_*__/`，确认是否存在覆盖当前任务能力的 skill
  （PDF 比对/PLC 审查等高频场景都有现成 skill）。
- **有匹配 skill 必须用**：按该 skill 的 SKILL.md 流程执行
  （copy_script_to_sandbox 传脚本 → execute 运行），**禁止**绕开 skill
  自己现场写同等能力的算法脚本。
- **禁止"造轮子"**：不得以"脚本有 bug 我重写一个更好的"为由另起炉灶——
  已有 skill 时，任何现场编写的同功能脚本都是重复劳动。
- **唯一例外**：确认全部 skill 目录（__system__/__agent__/__user_*__）
  均无覆盖任务能力的 skill 后，才允许写一次性脚本；写完直接执行，
  不要陷入"写脚本→报错→改脚本→重跑"循环，连续 2 次运行失败即停手
  汇报问题，禁止无限调试。

## 文件上传到沙箱的流程
- 所有输入文件（XML / Excel / Word）：用对应的 MCP 工具直传沙箱，不经过 LLM 上下文
- 不要用 read_file 读取文件内容后再 write_file 到沙箱（内容会撑爆上下文）
- 不要在 execute 脚本中引用 /uploads/ 或 /mnt/d/ 的路径，沙箱内不存在

## 致命错误（严格禁止）
- ls 和 glob 的结果就是真实的文件列表。不要用 execute 去验证文件存在与否
- execute 在沙箱里运行，看不到 /uploads/ 和 /reports/ 下的文件
- glob 说文件在，文件就在。重复用 execute 验证一次，就浪费一次 LLM 调用
- **禁止使用 glob 的 ** 通配符** —— `**/*.py`、`**/脚本名*` 这种模式会扫描整个虚拟文件系统，超时 20 秒
- 正确做法：`ls /skills/__system__/技能名/scripts/`

## 沙箱环境与完成门（系统兜底，勿对抗）
- **环境快照由系统自动注入**：每条用户消息会附带 `【沙箱环境】` 段（已装工具、磁盘空间、rustup toolchain），直接使用即可。**禁止**自行 `which`/`df`/重装工具/下载 toolchain——重装必然超时或磁盘不足，属于已知弯路
- **完成门系统校验**：任务结束时系统会检查 `/reports/{user_id}/{session_id}/` 是否有本轮新文件。零产出会被拦截并打回提示，**必须**把交付物（报告/代码/结果文件）落到 reports 目录才算任务完成

---

## 工程经验附录（工程 reviewer 看，非 Agent prompt）

> 这一节是给工程 reviewer（开发者）看的 lessons-learned，不是给 Agent 看的 prompt 规则。

### 2026-09-08：流式时长字段持久化（跨刷新稳定显示）

**目的**：前端 ReasoningBlock 标题"思考过程 (共 Ns)"刷新页面后仍稳定显示秒数；对齐 deer-flow 的 `additional_kwargs.turn_duration` 风格。

**改动**：`src/api/endpoints/chat.py`
- `event_stream()` 顶部声明 4 个时间戳变量（`turn_started_at_ms` / `reasoning_started_at_ms` / `reasoning_ended_at_ms` / `turn_ended_at_ms`）
- 触发时机：首个 reasoning 事件 → reasoning_start；首个 token 事件 → reasoning_end；while break 之后兜底设 turn_end（line ~1424）
- `_persist_session` 函数签名加 4 个时间戳参数；entry 构造时按条件写入 3 个字段：
  - `reasoning_duration_ms`：仅当 AI 消息带 reasoning + 两个时间戳都有值
  - `reasoning_started_at`：ISO 时间戳，前端可锚定
  - `turn_duration_ms`：仅写到 history 最后一条 AI 消息
- 调用点（正常 + 断连）两处都传时间戳

**经验**：
- 前端 React state/useRef 的计时永远不可信；任何"已完成但仍想展示时长"的 UI 必须从持久化层读
- 断连兜底路径（`reason="interrupted"`）也要持久化——避免"刷新后丢秒数"和"丢内容"同时发生
- 持久化前 `max(0, ...)` 兜底，防止异常时间戳顺序（end < start）写入负数

**验证**：`python tests/spikes/chat_persist_duration.py` 跑 8 个对照表（全 PASS）

### 2026-09-08：前端切换 session 不丢内容（与 chat.py 配合）

**改动**：前端 `app/chat/components/ChatArea.tsx:90-141` useEffect 改为"无重叠覆盖"——`msgs` 拉到后强制 server 覆盖 cache 骨架，唯一例外是 `streamSig.current.isStreaming === true`（runStream 仍在跑）。

**对应后端配合**：`_persist_session` 必须在 SSE 流结束后**立即**调用，把 checkpoint 写入 store，保证 msgs 切回时拿得到完整最终版。

**教训**：
- 后端持久化是唯一真相，前端 cache 仅作骨架
- 切 session 不丢失内容的端到端保证 = "不 abort + guard 丢弃 + server 真相 + 切回强制 reload"四件套
- 提示词只是引导，不负责兜底；以上行为由服务端强制校验，违反只会浪费你自己的步数

### 2026-09-09：vLLM content-parts 数组——解析层类型假设过强致 agent 流中断

**现象**：带文件上传的任务（ls → upload_to_sandbox ×2 → 正文输出）在工具执行完成后崩，
前端表现为"处理不了/无输出"。server.log 实证：
`TypeError: can only concatenate str (not "list") to str`
（chat.py `_drain_astream` line 837 `_think_buffer += content`），崩后 M3 完成门拦截 `generated=0`。

**根因**：流式解析假设 `token.content` 恒为 str。但 **vLLM 0.19 启用 `--reasoning-parser` +
`enable_thinking` 后，OpenAI 兼容流式 delta.content 可能为 content-parts 数组**
（`[{"type": "text", "text": "..."}, ...]`），`str += list` 直接 TypeError。
属上游服务器配置变更（2026-09-09 上午为 thinking 加的参）暴露了既有解析缺陷——
不是新任务类型的问题，是响应格式变了。

**修复**：`chat.py` `_drain_astream` content 取值处加类型归一化：
- `None`（tool_calls chunk 常见）与未知类型 → `""`（保持 falsy，防 `str(None)="None"` 污染流）
- `list` → 提取全部 text part 拼接为 str（非 text part 忽略、保序）
- `str` → 原样
归一化后 `</think>` 增量流式与 token 两分支共用同一 content（一处修复覆盖两处隐患：
崩溃点 + token yield 潜在 list 进 SSE payload）。

**经验**：
- 解析上游响应时，凡是"假设字段恒为某类型"的地方都要做类型防御；模型服务器升级/
  加参（reasoning parser、多模态）会悄悄改变响应结构，`str += list` 这类崩溃是典型症状
- 排查"agent 处理不了任务"先看 server.log 尾部有没有 traceback——M3 零产出拦截通常
  只是结果，真正原因在更早的异常行
- 单测要覆盖 content 的异常形态（None/list/混合 parts），不只是正常 str 路径

**验证**：`python tests/spikes/qwen_content_parts.py`（13 断言 PASS，含跨 chunk
`</think>` 残片、闭合后正文走 token 等场景）。端到端回归 = 真实会话重发原任务。

### 2026-09-10：观测后端被容器 healthcheck 灌满——注释 exporter ≠ 停止埋点

**现象**：生产 Phoenix `spans` 表 108,209 条中 HTTP 形态占 **98.3%**（`GET /docs`
健康检查占 **91.8%**），真实 LLM trace 只剩 **1.1%**；Langfuse 最近 30 条全是
`name="GET /docs"`（`http.user_agent=Python-urllib/3.13`、相邻 Δ=15.2s）。

**根因**：`deploy/docker-compose.yml` 的 healthcheck 每 15s 用
`python3 urllib.request.urlopen('http://127.0.0.1:8009/docs')` 探活，而 `/docs` 是
FastAPI 路由 → `FastAPIInstrumentor` 给每个请求建 3 个 span。
自校验：`86400/15 × 3 = 17,280 span/天`，与 Phoenix 按天实测（16,986 / 16,983 / 17,252）吻合。

**误判澄清**：曾认为"应用 log 被导进了观测后端"，六条 OTLP 日志通道逐一证伪 ——
代码 0 处 `LoggingHandler`（`git log -S` 全历史也 0）、未装
`opentelemetry-instrumentation-logging`、未装 `opentelemetry-distro`（SDK 的
`_configuration` 会自动把 `LoggingHandler` 挂 root logger，但 entry point 不存在 →
永不触发）、`phoenix/otel/otel.py` 0 引用、alloy 的 `otelcol.receiver.otlp` 无 logs
出口、应用 stdout 只去 Loki。

**修复（四层，见 `src/core/tracing.py`）**：
① `src/server.py:134` 新增 `/healthz`（`include_in_schema=False`，不查库）；
② `_HTTP_EXCLUDED_URLS` 经 `excluded_urls` 让探活/文档端点在 ASGI 入口 `return`；
③ `_OpenInferenceOnlySpanProcessor` 只放行带 `openinference.span.kind` 的 span；
④ Phoenix + Langfuse + metrics 三路 exporter 全部恢复。

**经验**：
- **注释 exporter ≠ 停止埋点**。OTLP 分四层：`opentelemetry-api`（provider 注册点）→
  `opentelemetry-sdk`（TracerProvider/SpanProcessor/MeterProvider）→
  `opentelemetry-exporter-otlp-proto-{grpc,http}`（**一个包同时提供
  traces/metrics/logs 三种 exporter**，用哪个类才决定发哪种信号）→
  `*-instrumentation-*` / `openinference-*`（**只造 span，不发送**）。
  关掉 exporter 后 instrumentation 照跑、span 照造，白耗 CPU 且埋点结构不变。
  另外 `arize-phoenix-otel` 只是便捷封装，其 `add_span_processor` 会**替换**已有
  processor（这是本项目弃用它、手写 TracerProvider 的原因）；**Langfuse 不是 SDK**，
  只是个 OTLP traces 接收端，收到什么完全取决于你挂了哪些 span。
- **`FastAPIInstrumentor.instrument()` 是 patch `fastapi.FastAPI` 类属性**
  （`opentelemetry/instrumentation/fastapi/__init__.py:442-445`），所以
  `from fastapi import FastAPI` **必须**在 `instrument()` **之后**执行 ——
  顺序反了该名字绑定到旧类，app 完全不被埋点、**不报任何错**。
  `server.py` 当前顺序正确（`setup_tracing()` L13 → `from fastapi import FastAPI` L17），
  调整顶部 import 顺序时务必保持。
- **断源优先于过滤**：`excluded_urls`（逗号分隔、`re.search` 语义、在 ASGI 入口直接
  `return`）零开销；SpanProcessor 白名单是兜底。两者都要，前者省 CPU 后者防漏网。
- **判据选 `openinference.span.kind`，别维护 URL 黑名单**：OpenInference 埋点一定设置它
  （`openinference/instrumentation/langchain/_tracer.py:294`，且在 `span.end()` **之前**
  写入 → `SpanProcessor.on_end()` 必定可读）；OTel 的 HTTP/ASGI 埋点一定不设。
  实测交叉验证：Phoenix `span_kind` 列 UNKNOWN 106,434 全是 HTTP，
  CHAIN/LLM/TOOL/AGENT 共 1,775 全是非 HTTP。

**验证**：`tests/spikes/tracing_span_filter.py` 18/18 PASS（真实 OTel SDK + 真实
LangChain Runnable + 真实 FastAPI 请求，无 mock）。核心对照：同一进程挂两个 exporter，
raw 收到 6 条（OI 3 + HTTP 3），kept 收到 3 条（OI 3 + HTTP **0**）。
完整复盘见 `docs/tracing-span-pollution-postmortem.md`。
注意 `.venv` 是 Linux 布局、Windows 本机 Python 用不了，spike 需在生产同款镜像内运行
（命令见该文档第 6 节）。

### 2026-09-12：会话列表从手工 `__index__` 索引切到 `store.asearch` 前缀遍历

**问题**：`GET /sessions` 依赖手工维护的 `__index__` key（存 session_id 列表）。
生产实测当前一致（GY24428 15/15、wanglei1 3/3、GY18008 2/2、lvxiaojian 1/1，
无脏残留、无漏登记），但结构上是「数据行 + 索引行」**两次独立写、非原子**，
任一失败即分叉——这正是历史 "GET /sessions 返空" 类 bug 的来源。

**根因**：`list_sessions` 的注释写着「由于 store 不直接支持遍历 namespace，我们用约定：
维护一个 index key」。**该前提是错的**：`BaseStore.asearch(namespace_prefix, /, *,
query=None, filter=None, limit=10, offset=0, refresh_ttl=None)`
（`langgraph/store/base/__init__.py:1021`）本就支持前缀检索，Postgres 实现走
`prefix LIKE`（`store/postgres/base.py:446`），DDL 里还专门建了
`store_prefix_idx ... text_pattern_ops`（注释即 "For faster lookups by prefix"）。

**`asearch` 三个"框架不管、调用方必须管"的坑（都不报错，只静默出错）**：
1. **`limit` 默认 10，会静默截断** —— 必须显式传，否则列表莫名其妙只出 10 条。
2. **前缀匹配不认命名空间边界**：`sessions.GY2442` 会同时命中 `sessions.GY24428`
   → **串用户（越权）**。必须按 `Item.namespace` 精确相等过滤。生产当前无前缀包含
   关系的用户（实测 0 行），但新建短用户名就会踩。
3. **不能 offset 翻页**：Postgres 非向量路径是 `ORDER BY store.updated_at DESC`
   （`store/postgres/base.py:533`），该列**非唯一且被业务持续改写** → 翻页期间任何
   条目 updated_at 变化都会导致漏条或重复。会话元数据极小，一次 `limit=1000` 取全最稳。
   ⚠️ **该结论已被下一条取代**：`limit=1000` 只是把静默截断抬高了水位，超过仍会丢；
   真正的修法是自建 keyset 游标（见下方「会话列表改走自建 keyset 游标分页」）。

**修复**：
① `src/infra/database.py`：`ReconnectingAsyncPostgresStore` 暴露 `asearch`（走 `_call` 重试代理）；
② `src/api/endpoints/sessions.py`：新增 `_alist_sessions`（含上述三条防御），
   `list_sessions` 切过去；删除 `_update_session_index` 函数及 create/delete 两处调用；
③ `src/api/endpoints/chat.py`：删除 `_persist_session` 里的 `__index__` 读写块；
④ 历史 `__index__` 条目**保留不删**（读取时按 `_LEGACY_INDEX_KEY` 跳过），不动生产数据。

**经验**：
- **"框架不支持 X" 是高风险断言**，写下它之前必须去源码里找一遍。这一条注释让手工索引
  多活了很久，而 store 的前缀检索**既有公开 API 又有专用索引**。
- **换底层 API 时先盘它没帮你做的事**：`asearch` 不管 limit 截断、不管 namespace 边界、
  排序键不稳定——三条都不抛异常。与 09-09 那条（解析层类型假设过强）同源：
  **静默失败比显式报错危险得多**。
- `SearchItem` 继承 `Item`，`.key` / `.namespace` / `.value` 可直接用。

**验证**：`python tests/spikes/asearch_session_list.py` —— ast 提取真实
`_alist_sessions` 执行，**23 断言 ALL PASS**。含越权对照（`sessions.GY2442` 前缀捞到
`GY24428` 条目必须被过滤并告警）、单次取全、上限告警、以及「不得出现 `offset=` 实参」
的源码级断言。生产库 SQL 等价验证：`prefix LIKE 'sessions.GY24428%'` 一次取全 16 行
（含 `__index__`，新代码跳过 → 15 条真实会话），与旧索引内容一致。

### 2026-09-12（续）：会话列表改走自建 keyset 游标分页，弃用 `asearch`

**问题**：上一条把「不能翻页」记成了既定事实，用 `limit=1000` 一次取全绕过。
但那只把静默截断抬到 1000 条水位——**超过就丢，且只打 warning、前端无感**。
用户追问"翻页能不能彻底修"，核查后结论是：**能，但必须离开 `asearch`**。

**根因（三条都是框架层结构限制，应用层绕不开）**：

1. `ORDER BY store.updated_at DESC` 只有**一列且非唯一**，PK `(prefix, key)` 不参与排序
   → 没有全序，而 OFFSET 语义要求稳定全序（`store/postgres/base.py:526-534`）。
2. namespace 条件**只生成 `prefix LIKE %s`，没有 `prefix = %s` 分支**
   （`base.py:446-449`）→ 越权过滤只能放应用层，而 OFFSET 的偏移量是**数据库端**算的
   → **只要用 LIKE 前缀 + 应用层过滤，分页在结构上就不可能正确**（不是"可能漏"，
   是数学上无解）。这条比"排序不稳"更硬，是决定性的。
3. `filter` 的比较操作符（`$lt/$gt/...`，`base.py:622-637`）只作用于 **value 字段文本**，
   **没有 key 列、没有元组、没有 OR** → 表达不了 `(updated_at, key) < (c1, c2)` 稳定游标。

补充：`updated_at` 由框架 UPSERT 时 `CURRENT_TIMESTAMP` 写入（`base.py:388-393`），
**每次 aput 都变（含重命名/pin）**，天生不适合当排序键。store 表只有 3 个索引
（`store_pkey` / `store_prefix_idx text_pattern_ops` / `idx_store_expires_at`），
**没有任何排序用索引**。

**修复**：
① `src/infra/database.py` 新增 `asearch_keyset(prefix, *, limit, cursor, pinned)`：
   `prefix = %s` 精确匹配（越权问题从根上消失，调用方不再需要 namespace 相等过滤）
   + `(COALESCE(value->>'updated_at',''), key) < (%s::text, %s::text)` 元组游标
   + `ORDER BY <排序键> DESC, key DESC` 全序。走 `store.conn`（**公开属性**）借连接，
   复用 `_call_with` 的重连重试语义；
② 自建复合索引 `store_sessions_order_idx ON store (prefix, (<排序键>) DESC, key DESC)`，
   在 `setup()` 里幂等创建（失败只影响性能、不阻断启动，但打 error）；
③ `sessions.py`：`_alist_sessions` 改成游标驱动。**默认取全**（逐页 200 推进，
   防呆上限 50 页 = 10000 条，触顶打 error 而不是静默截断）+ 两处防死循环断言
   （游标必须严格递减）；新增**可选** `?limit=&cursor=` 分页模式
   （置顶会话单独返回 `pinned_sessions`——置顶是应用层第二段排序，留在页内会散落各页）；
④ 读取路径换血，**写入路径不动**（仍走 aput）。

**经验**：
- **"框架不支持 X" 与 "框架支持 X 但做不对 Y" 是两回事**。上一条把后者误当成了
  "只能绕过"，于是留下了静默截断。判断依据要落到 SQL 文本本身，而不是 API 文档的
  参数列表——`asearch` 有 `offset` 参数，看起来支持翻页，实际结构上做不到。
- **排序键必须来自业务字段，不要借框架的写入时间列**。`store.updated_at` 由框架维护、
  每次写都变，与业务排序（`value.updated_at`）不同源，用它会引入"翻页序 ≠ 列表序"。
- **文本排序键必须显式 `COLLATE "C"`**。ISO-8601 UTC 时间串的文本序 ≡ 时间序，
  但只在字节序下成立；库 collation（本生产为 `en_US.utf8`）一变就可能漂。
  生产实测两种 collation 对本数据集结果相同，但**不能依赖这个巧合**。
- **表达式索引必须与查询的排序键是同一个表达式**（本实现用同一常量 `_SORT_KEY_EXPR`
  拼装两处），差一个 `COALESCE` 就用不上索引。验证方式：临时表 + `SET enable_seqscan=off`
  看 EXPLAIN 是否为 `Index Scan`（生产实测：无 Sort 节点，索引序即输出序）。
- **取全要有防呆上限 + 显式告警**：无界循环遇上排序键脏数据就是死循环。本实现两条保险：
  游标严格递减断言（不满足即 break + error）、页数上限（触顶 error）。

**验证**：`python tests/spikes/asearch_session_list.py` —— **55 断言 ALL PASS**，
ast 提取两个文件的真实函数执行。含：跨 3 页取全不重不漏、**全表同一 updated_at
（每个页边界都是 tie）不漏不重**、分页拼接 == 取全、缺 updated_at 排末尾、
游标不推进时停止、触顶告警、非法 cursor 报 400，以及源码级断言
（sessions.py 不再出现 `.asearch(` 调用、SQL 不含 `LIKE`/`OFFSET`、索引表达式
与排序键同源）。生产库（agent_mem_prod）用代码生成的 SQL 端到端验证：
首页 5 条 + 第二页 5 条与全量排序前 10 条完全一致，计数 15 与旧实现相同。

### 2026-09-12（三）：`messages.*` 改增量存储（一行一条，key = 零填充序号）

**问题**：`messages.<user>.<session>` 一直用单行 `key="messages"` + `value={"items":[...]}`，
每轮把**整份 history** 覆盖写。生产实测（agent_mem_prod）三个后果：

| 后果 | 实测数据 |
|---|---|
| ① 爆炸半径 = 整个会话 | 最大会话 259 条 / 1.32 MB 挤一行；这一行写失败（TOAST 上限 / jsonb 解析异常 / 序列化 OOM）就是整份历史全丢 |
| ② 写放大 130× | 每轮重写整行，累计约 171 MB 只为最终存下 1.32 MB |
| ③ **摘要裁剪传导** | `SummarizationMiddleware` 触发时 checkpoint 被 `RemoveMessage(REMOVE_ALL_MESSAGES)` 裁短 → 下一轮按快照重建的 history 缩水 → 全量覆盖把这份缩水写进 store → 早期对话在任何一层都找不回 |

**改法**：一条消息一行，`key = f"{i:08d}"`（`i` 从 1 开始）。读取
`WHERE prefix = %s ORDER BY key` 正好命中 store 自带的 `store_pkey (prefix, key)`
btree → **零新建索引成本**（与会话列表必须自建 `store_sessions_order_idx` 相反）。
写入只写新增部分。

**经验**：
- **"增量追加"的朴素前提（history 只在尾部增长）在摘要场景不成立**。压缩后
  history = `[摘要, *保留的尾部]`，长度**变短**：按长度判断会判为"无新增"（此后再也写不进去），
  按 history 下标算 key 会**覆盖已归档条目**。正确做法是**内容 id 定位**——取 store 里
  最大序号那条的 `id`，在 history 里反查它的位置，从其后追加，序号继续用 **store 的**
  序号（不是 history 下标）。id 缺失时退化为按长度，且**长度未增长就不写**（宁可少写
  一轮，不可改写已归档内容）。摘要消息本身**不写入** store（它是喂模型的面，不是回放副本）。
- **水位查询必须过滤非序号 key**：`ORDER BY key DESC LIMIT 1` 在文本序下，
  旧 key `'messages'` 排在数字之后（`'0'` 0x30 < `'m'` 0x6d）→ 会污染水位。
  查询加 `AND key ~ '^[0-9]+$'`，并**单独统计**非序号条目数返回给调用方。
- **格式变更必须让新代码对旧格式"拒绝写入"（loud），而不是静默兼容**。静默兼容的两个
  下场：写进去让新旧格式并存 → 迁移脚本的前置守卫拒绝执行 → 留下需人工对账的中间态；
  或按 0 水位从第 1 条重写 → 与旧行重复。**相反方向的坑同样致命**：迁移在旧代码仍在
  服务时执行，旧代码的写路径会把旧格式行**重新写回来**。所以顺序是
  **停写 → 迁移 → 切新镜像 → 放开**，中间那段窗口新代码只会打 error 不写数据。
- **"改一行一值"本身就比任何阈值更兜底**：故障爆炸半径从"整个会话"缩到"一条"。
  由此**不要**在存储层加"每会话条数上限"——对照 deer-flow（events append-only）
  与 deepseek-harness（`session-persistence/README.md`："Flushed events are never rewritten"）
  两家都不设该维度；条数与真实上下文压力无关（259 条短消息与 259 条长消息可差 100 倍）。
  真正的压力闸在上下文层：三家一致的 `fraction 0.8`。写入侧只留 per-item 体积观测闸
  （`persist_max_item_bytes`，实测单条最大 278 KB → 1 MB 留 3.6× 余量），**只 warn 不截断**。
- **`keep=("messages", 10)` 是个真 bug，但直接改回 `("fraction", 0.10)` 会更糟**：
  框架的 `token_counter` 在两处需要不同语义（trigger 要"整个请求占多少"，keep 的二分要
  "`messages[mid:]` 子集占多少"），而**只在用默认 counter 时才自动拆分**。本项目的
  `_engine_grounded_counter` 命中缓存时忽略 `messages` 入参 → 二分每步拿到同一个数 →
  `cutoff` 被顶到 `len-1` → **压缩后只剩最后 1 条**。必须同时把 `_lc_helper._partial_token_counter`
  换成子集感知的 `_safe_token_counter`（见下面验证脚本；实测 30 条消息下 cutoff 29 → 22）。
- **"改完没报错"不等于对**：`_partial_token_counter` 挂在 `deepagents` 的 `_lc_helper` 上
  （`_determine_cutoff_index` 走 `self._lc_helper._partial_token_counter`），挂到子类自身
  等于没拆。凡"改框架私有属性"的接线，断言必须落到**赋值目标对象**上。
- **小表上看不出索引会不会被用**：42 行的 store 表上 EXPLAIN 一律 `Seq Scan + Sort`，
  必须 `SET enable_seqscan = off` 才能验证 `Index Scan Backward using store_pkey`。
- **分页方向要写进接口文档**：第 1 页取的是**最新**的一段且页内升序 → 续页必须
  **前插**（prepend），不是 append。不写清楚前端一定会接错。

**验证**：
- `python tests/spikes/store_incremental_messages.py` —— **63 断言 ALL PASS**。含：
  首次写全量、幂等（重复保存零新增、store 逐字节不变）、追加一轮只写 2 条、
  **压缩场景 history 变短仍零新增且已归档条目零改写**、压缩后从 store 序号接续
  （不是 history 下标）、病态路径（最新已存条目被裁掉 → 什么都不写）、
  旧格式残留 → 整轮不写 + error、读侧跳过旧格式条目、分页前插拼接不重不漏、
  非法游标 400、L1 体积闸只 warn 不截断，以及源码级断言（SQL 不含 `LIKE`/`OFFSET`、
  `awrite_messages` 单次 batch、旧格式读写点已清零）。
- `python tests/spikes/summarization_keep_semantics.py` —— **17 断言 ALL PASS**，
  用 ast 提取 langchain 真实方法执行，量化了"未拆 counter → cutoff 29（保留 1 条）"
  与"拆分后 → cutoff 22（保留 8 条）"的差别。
- 生产库只读验证：三条消息查询（首页 / 续页 / 水位）EXPLAIN 全部为
  `Index Scan Backward using store_pkey` 且**无 Sort 节点**；存量形态 21 会话 / 639 条
  item / **100% 带 `id`** / 0 条结构异常 → `by_id` 水印迁移后即可用。
- 存量迁移脚本 `migrate_messages_incremental.py`（同库备份表 + 单事务 + 三条闭环校验：
  条目数守恒、content 与 id 序列逐条一致、时间戳未被改写）。**预演已通过**
  （21 会话 / 639 条 / 0 数字 key），**尚未 apply**——必须在应用停写窗口内、与新镜像
  切换同批执行（见上"顺序是停写 → 迁移 → 切新镜像 → 放开"）。


## 2026-09-12 追加：增量写入上线即 NameError（message_key 漏 import）

**问题**：1.0.10 部署后每一轮对话结束时台账写入全部失败——`_persist_session`
抛 `NameError: name 'message_key' is not defined`（`chat.py:707`），store 零追加
（用户发新消息后实测 16 行一动不动）。checkpoint 完好，数据未丢，但"本轮消息可能
丢失"的 error 每轮必现。

**根因**：`26faf9d` 在 chat.py 新增了对 `database.message_key` 的调用，但没加
`from src.infra.database import message_key`。**为什么 63 断言的 spike 没测出来**：
ast 提取 exec 的方式会把 `message_key` 绑进 exec globals（spike 自己从 database.py
提取后注入），恰好掩盖了真实模块里缺失的 import——**ast 提取式测试天然测不出
"模块接线"层面的错误**（import 缺失、循环导入、名字遮蔽）。且部署后只验证了读
路径（GET /messages），写路径只有真实对话走到轮次结束才触发。

**修复**：chat.py 补 1 行 import；spike 新增**用例 12「import 完整性」**——
用 ast 对比"chat.py / sessions.py 实际引用的 database 模块级符号"与"显式导入的
符号"，缺失即 FAIL（63 → 65 断言）。这一类"名字从哪来"的接线错误今后都归它管。

**教训**：
- 每个源码级新断言要问一句：**它测的是代码文本还是模块行为？** 若符号是被
  spike 自己注入 globals 的，等于绕过了真实模块的命名空间——必须另立断言核对导入。
- 部署后验证必须覆盖**写入路径**，不能只测读。写路径的真实触发点是"一轮对话
  正常结束"，验证清单里要有一条"发一条消息 → 看台账追加"。

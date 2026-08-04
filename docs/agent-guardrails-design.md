# Agent 沙箱护栏设计文档（代码层兜底改造）

> 版本：v1.0（评审稿）
> 日期：2026-08-04
> 状态：**待评审**，未开始实施
> 关联事故：session `ef76bb52`（2026-08-04 11:42–11:54）Rust 编程任务 20 分钟失败、零交付物

---

## 1. 背景与问题链

事故复盘（详见当日工作日志）暴露四层问题，本质是**关键约束只存在于提示词层（AGENTS.md），执行依赖模型的概率性遵循**：

| # | 问题层 | 现象 | 根因 |
|---|--------|------|------|
| ① | 写入路径错误 | `write_file /tmp/...` 被 `ValidatedCompositeBackend` 拒绝 | 拦截器只拒绝不纠错，模型靠自己"想起"正确路径 |
| ② | 工具选择错误 | 绕过 MCP 传输工具，用 `execute`+heredoc 写沙箱 | 通用工具（execute）与专用工具能力重叠，模型走阻力最小路径 |
| ③ | 环境预检混乱 | 误判"未装 Rust"→ 重装 → 超时 → 权限不足 → 磁盘满 | "要不要装/空间够不够"的判断交给了 LLM 现场探索 |
| ④ | 交付物零产出 | 全程无 download，`/reports/` 空，前端 HEAD 全 404 | 任务完成判定完全由模型自陈，无系统级产出校验 |

**设计原则**：凡是"错了会导致任务失败/资源浪费/安全问题"的规则，必须下沉为不可绕过的代码机制；提示词只负责最优路径引导，不负责兜底。判断"任务是否完成"用可验证状态（文件是否存在、命令退出码），不信任模型自陈。

---

## 2. 前置事实与待验证项（实施前必须先确认）

| ID | 事实 | 影响 |
|----|------|------|
| F1 | 活入口是 `src/services/agent.py`（chat.py:11 引用）；`src/init_agent.py` 的 `_ValidatedCompositeBackend`（允许前缀 `/reports/, /memories/`）**无任何引用** | 两套校验分叉，旧版必须删除或统一，否则行为漂移 |
| F2 | `mcp.json` 中 `decrypt-file` 的 `Authorization: Bearer YOUR_TOKEN` 是**占位符**；`.env` 未检出 `mcp_token` 配置 | `upload_to_sandbox` / `download_from_sandbox` 可能**根本没被加载**——若 MCP 握手失败，AGENTS.md 写的传输流程对模型是空话，它只能走 heredoc。**这是比 ④ 更前置的问题** |
| F3 | `langchain-cubesandbox` 是本地 editable 依赖（`../langchain-cubesandbox`，基于 e2b-code-interpreter） | 强版本①（透明重定向到沙箱写入）需读其源码确认文件写入 API，评估可行性与路径语义 |
| F4 | deepagents == 0.6.12，langgraph checkpoint 已接入（create_agent 传 checkpointer） | 完成门 v2"自动继续生成"需 spike 验证 `astream(None, config)` 继续模式 |

**待验证项（T 项，实施第一步）**：
- T1：启动服务后 `GET /api/v1/mcp/json` 或直接调 `get_mcp_tools(["decrypt-file"])`，确认 upload/download 工具是否加载；若失败，修 token/服务可用性。
- T2：grep 全仓库确认 init_agent.py 无其他引用后删除（或至少统一前缀）。
- T3：读 `langchain-cubesandbox` 源码，确认 sandbox 是否有 `filesystem.write` / `upload` 等价 API。

---

## 3. 总体架构：三个代码机制

```
┌─────────────────────────────────────────────────────────────┐
│  M1 环境预检（session 启动时）                                  │
│  代码跑环境快照 → 结构化注入 system 消息 + 磁盘阈值硬校验        │
│  ⇒ 消除问题③：模型不需要现场"发现"环境                          │
├─────────────────────────────────────────────────────────────┤
│  M2 写入纠错（write_file 拦截时）                              │
│  拒绝 ≠ 沉默：附带可执行修正建议（含 user/session 上下文）       │
│  ⇒ 消除问题①：模型选错路径后有强信号换路                        │
├─────────────────────────────────────────────────────────────┤
│  M3 完成门（astream 结束后）                                   │
│  系统校验 /reports/ 是否有本轮新产出，无产出不放行结束           │
│  ⇒ 消除问题④：任务完成判定从"模型自陈"改为"文件系统证据"        │
└─────────────────────────────────────────────────────────────┘
       问题②（工具重叠）不做硬拦截，靠 M2 纠错信号 + M3 完成门夹住
```

---

## 4. 详细设计

### M1 环境快照注入 + 磁盘前置校验（P0，对应问题③）

**涉及文件**：`src/infra/sandbox.py`（新增）、`src/api/endpoints/chat.py`（注入点）、`src/core/config.py`（新增配置）

**新增函数** `src/infra/sandbox.py`：

```python
# 白名单：只探测这些命令是否存在（可配置，勿用任意命令）
DEFAULT_PROBE_COMMANDS = ["rustc", "cargo", "python3", "node", "go", "gcc", "javac"]

@dataclass
class SandboxEnvSnapshot:
    ok: bool                      # 探测是否成功
    commands: dict[str, str | None]   # 命令 -> 绝对路径或 None
    disk_avail_mb: int | None     # df -h / 可用空间（MB）
    toolchains: list[str]         # rustup toolchain list（如存在）
    error: str | None             # 探测失败原因（降级时非 None）

def probe_sandbox_env(sandbox) -> SandboxEnvSnapshot:
    """对沙箱跑一次环境探测，全部命令合并为单次 execute 降低开销：
    'for c in rustc cargo python3 node go gcc javac; do command -v $c && echo "FOUND:$c"; done; df -h / | tail -1'
    解析 stdout。任一条命令失败不抛异常，置 ok=False 并记录 error（降级而非阻断）。
    """
```

**缓存**（模块级 dict，`{thread_id: (ts, snapshot)}`，TTL 60s）：create_sandbox 每次都 get_or_create 复用同一 thread_id 沙箱，避免每轮 chat 重复跑命令。

**配置新增**（`src/core/config.py` Settings）：

```python
sandbox_probe_commands: str = ""      # 覆盖默认白名单，JSON 数组；空 = 用默认
sandbox_disk_warn_mb: int = 200       # 低于此值注入警告
sandbox_disk_hard_mb: int = 50        # 低于此值拒绝启动任务
sandbox_probe_ttl_sec: int = 60       # 快照缓存 TTL
```

**注入点**（chat.py，`path_hint` 拼接处，约 105-112 行）：

```
沙箱 ID：xxx
当前用户：xxx（user）
【当前会话路径】
输入文件：/uploads/{user_id}/{session_id}/
报告输出：/reports/{user_id}/{session_id}/
【沙箱环境】（系统自动探测，勿重复安装/探测）
  rustc: /root/.cargo/bin/rustc  |  cargo: /root/.cargo/bin/cargo  |  python3: /usr/bin/python3
  node: 未安装  |  可用磁盘: 120MB（偏低，编译类任务可能失败）
  rustup toolchains: stable-x86_64-unknown-linux-gnu（无默认 toolchain）
```

**硬阈值行为**：`disk_avail_mb < sandbox_disk_hard_mb` → 在 chat() 中直接返回 `503` + JSON `{"detail": "沙箱磁盘空间不足（X MB），请清理沙箱或稍后重试"}`，**不进入 agent 循环**。快照探测失败（ok=False）→ 不阻断，注入"环境探测失败，请自行确认可用工具与磁盘空间"。

**异常路径**：execute 超时 → except 捕获置 ok=False；缓存未命中且沙箱为 None（本地模式）→ 跳过注入。

---

### M2 写入纠错：拒绝 ≠ 沉默（P0，对应问题①弱版本）

**涉及文件**：`src/services/agent.py`

**改造 `ValidatedCompositeBackend`**：构造函数增加上下文，拒绝时按路径模式生成可执行修正建议。

```python
# 沙箱内路径模式（命中即提示"这是沙箱路径，应走 MCP 传输或直接写 reports"）
SANDBOX_PATH_PREFIXES = ("/tmp/", "/home/", "/root/", "/mnt/", "/code/", "/var/")
# 虚拟文件系统内但不可写的路径（命中即提示改 reports）
VIRTUAL_READONLY_PREFIXES = ("/uploads/", "/skills/", "/workspace/agent-memory/")

class ValidatedCompositeBackend(CompositeBackend):
    def __init__(self, default, routes, *, user_id: str, session_id: str):
        super().__init__(default=default, routes=routes)
        self._report_prefix = f"/reports/{user_id}/{session_id}/"

    def _reject_hint(self, file_path: str) -> str:
        """按路径类型生成修正建议，把模型可能不记得的上下文直接算好塞回。"""
        if file_path.startswith(SANDBOX_PATH_PREFIXES):
            return (f"路径 '{file_path}' 是沙箱内路径：沙箱内文件请用 upload_to_sandbox 传输"
                    f"（本服务不直接写沙箱文件系统）；如需生成交付物，请直接写入 "
                    f"'{self._report_prefix}<文件名>'")
        if file_path.startswith(VIRTUAL_READONLY_PREFIXES):
            return (f"路径 '{file_path}' 只读；交付物请写入 '{self._report_prefix}<文件名>'")
        return (f"只能写入到: {', '.join(sorted(self.ALLOWED_WRITE_PREFIXES))}；"
                f"当前会话交付目录为 '{self._report_prefix}'")
```

`write`/`awrite` 拒绝分支改为调用 `_reject_hint` 生成 `error`（日志保留原 warning + 追加 hint）。

**调用链**：`build_backend(user_id, session_id, store, sandbox)` 已持有 user/session → 传给构造器 → `create_agent` 无感。

**清理**：确认 F2/T2 后删除 `src/init_agent.py` 的 `_ValidatedCompositeBackend` 与 `build_backend`（或整文件若无其他功能），消灭两套校验分叉。

---

### M3 完成门：产出校验 + 不放行结束（P0 v1 检测版 / P1 v2 自动继续版，对应问题④）

**涉及文件**：`src/api/endpoints/chat.py`（event_stream）

#### v1（P0，先落地，零 deepagents API 风险）

**新增辅助函数**（chat.py 模块级）：

```python
def _snapshot_report_files(report_root: str, user_id: str, session_id: str) -> frozenset[str]:
    """扫描 report_root/{user}/{sid}/ 下相对路径集合（文件+目录名），
    astream 前后各取一次做差集判断本轮产出。目录不存在返回空集。"""
    base = Path(report_root) / user_id / session_id
    if not base.is_dir():
        return frozenset()
    return frozenset(
        str(p.relative_to(base)) for p in base.rglob("*")
    )
```

**event_stream 改造**（插桩点）：
1. `astream` 前：`before = _snapshot_report_files(...)`
2. `astream` 正常结束（非异常）后：`after = _snapshot_report_files(...)`；`new_files = after - before`
3. 判定未产出：`not _generated_files and not new_files`
4. 命中 → yield 阻断事件并记录失败，**不发 [DONE]**：

```
yield data: {"type":"completion_blocked",
             "reason":"本轮任务未产出任何交付物（/reports 为空）",
             "hint":"请检查是否遗漏 download_from_sandbox / write_file 步骤"}
```

5. 会话历史：该轮最后一条 AI 消息 entry 追加 `{"completion": "blocked_no_output"}`，前端可展示失败徽标；不再像事故那样"20 分钟后状态不明"。

**判定边界**：
- 只比较**本轮新增**文件（差集），多轮会话历史文件不误判；
- `_generated_files`（流式阶段已识别）+ 磁盘差集双保险，两者都空才算未产出；
- 任务本身是"纯问答/无需文件"类型？→ 由 `body.message` 无法可靠判定，v1 不区分，避免过度工程；如误报率实测偏高，v2 增加"模型最后一条 AI 消息无 tool_calls 且内容为正常回答"豁免。

#### v2（P1，依赖 spike，自动继续生成）

**前置 spike（30 分钟内出结论）**，临时脚本验证：

```python
# spike_checkpoint_resume.py
agent = await create_agent(...)           # 复用现有工厂
await agent.astream({"messages":[HumanMessage("写个文件到 reports")]}, config=cfg)
# 检查 reports 为空后，注入 SystemMessage 并继续：
await agent.astream(None, config=cfg)     # LangGraph 标准继续模式
```

判据：`astream(None, ...)` 能从最后 checkpoint 继续（观察新 token 流）；SystemMessage 注入方式（`graph_input={"messages": [SystemMessage(...)]}` 或 checkpoint 内追加）生效。**通过 → v2 落地；不通过 → 保持 v1 并记录限制。**

**v2 逻辑**：

```python
MAX_REJECTIONS = 2
rejections = 0
while True:
    async for ... in agent.astream(graph_input, config=cfg, ...):
        ...  # 现有流式处理
    new_files = after - before
    if _generated_files or new_files:
        break                                    # 有产出，放行
    if rejections >= MAX_REJECTIONS:
        yield completion_blocked(terminated=True) # 标记失败终止
        break
    rejections += 1
    graph_input = {"messages": [SystemMessage(
        f"系统检测：/reports/{user_id}/{session_id}/ 当前为空，本轮任务未产出任何交付物。"
        "请检查是否遗漏 download_from_sandbox / write_file 步骤，并继续完成。")]}
    before = after                              # 重新基线
```

**异常路径**：继续模式抛异常 → 捕获后按 v1 路径 yield `completion_blocked` 并记录，不崩 SSE；`recursion_limit=100` 仍适用，继续轮次计入步数，超限由 langgraph 报错被外层 except 兜住。

---

### 备选设计（本期不做，记录决策）

**①强版本——write_file 透明重定向到沙箱**（P1）：
- 思路：write_file 目标为沙箱代码路径（`/tmp/*.rs` 等）时，内部调用 `upload_to_sandbox` 工具执行等价写入，返回"已自动写入沙箱"，而不是报错打断。
- 依赖：F3（CubeSandbox 文件 API）+ T1（MCP 工具可用）。风险：MCP 调用失败/超时会让 write_file 语义不透明（模型以为写了 reports 实际写在沙箱），**不透明纠错比报错更危险**，故本期不做，仅记录。

**②工具边界——限制 execute**（P1）：
- 结论：**不做硬限制**。正则拦截 `cat > / tee / > file` 误伤面过大（合法输出重定向、日志重定向全被波及），且 heredoc 写文件与"跑命令"在 shell 层不可分割。能力靠 M2 纠错 + M3 完成门夹住；辅助手段是强化 upload/download 工具 description（提示词层，非根治）。

**AGENTS.md 修订**（辅助，非兜底）：补充"M1 环境快照由系统注入，禁止自行重装/探测环境；任务结束前必须确认交付物落到 /reports/"。

---

## 5. 配置项变更汇总

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `sandbox_probe_commands` | `""`（用默认白名单） | 覆盖探测命令白名单，JSON 数组 |
| `sandbox_disk_warn_mb` | `200` | 低于则注入警告 |
| `sandbox_disk_hard_mb` | `50` | 低于则拒绝启动任务（503） |
| `sandbox_probe_ttl_sec` | `60` | 环境快照缓存 TTL |

---

## 6. 测试计划

**单元测试**（tests/ 新增）：
- `tests/infra/test_sandbox_probe.py`：mock execute 返回 → 解析命令路径/磁盘/失败降级（ok=False 不抛异常）；缓存命中/过期。
- `tests/services/test_validated_backend.py`：`_reject_hint` 对沙箱路径/只读路径/未知路径三类返回含 `_report_prefix` 的建议；write/awrite 拒绝后 error 文案断言。
- `tests/api/test_completion_gate.py`：`_snapshot_report_files` 差集逻辑（新建/删除/多轮基线）；v1 判定函数纯逻辑（generated_files + 差集 → blocked）。

**集成/手动验收**（复现事故场景）：
1. 沙箱环境：`python3` 已装、rust 未装 → 发起"用 Rust 找双支付用户"任务 → 断言 path_hint 注入环境快照、模型不再尝试安装 rust；
2. 人为让模型不产出（mock download 失败）→ 断言前端收到 `completion_blocked`、会话历史标记失败；
3. 正常产出任务回归：文件卡片事件、会话保存、刷新后文件仍在。

---

## 7. 风险与回滚

| 风险 | 等级 | 缓解 |
|------|------|------|
| v2 `astream(None)` 在 deepagents 0.6.12 不兼容 | 高 | spike 先行；不通过则保持 v1，v1 本身已消除"状态不明" |
| 磁盘硬校验误伤（瞬时 df 波动） | 低 | 阈值保守（50MB）+ 仅 hard 模式拒绝，warn 模式只注入 |
| M2 纠错文案过长增加 token 消耗 | 低 | 单条 hint ≤ 200 字，仅在拒绝路径出现 |
| completion_blocked 对"纯问答任务"误报 | 中 | v1 先上线观察；实测偏高则加"无 tool_calls 豁免"（v2 一并做） |
| 回滚 | — | 每个机制独立开关：M1/M2/M3 均可用 env flag 关闭（新增 `sandbox_guardrails_enabled: bool = True`） |

---

## 8. 实施里程碑

| 里程碑 | 内容 | 依赖 |
|--------|------|------|
| M0 前置验证 | T1 decrypt-file MCP 可用性（含 token 修复）、T2 init_agent.py 清理确认、T3 CubeSandbox 文件 API 探查 | 无 |
| M1 | 环境快照注入 + 磁盘前置校验 | M0 |
| M2 | 写入纠错（弱版本）+ 统一/删除旧校验 | M0 |
| M3-v1 | 完成门检测版（blocked 事件 + 失败标记） | M1 |
| M3-v2 | spike → 自动继续生成（最多打回 2 次） | M3-v1 + spike 通过 |
| 收尾 | AGENTS.md 修订、配置文档、验收回归 | 全部 |

> 每个里程碑独立可交付、可回滚，建议按 M0 → M1 → M2 → M3-v1 → M3-v2 顺序评审与实施。

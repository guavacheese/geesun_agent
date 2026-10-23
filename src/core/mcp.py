"""MCP 客户端管理 — 从 {AGENT_WORKSPACE}/mcp.json 读取多 MCP 配置。

mcp.json 结构：
{
  "mcpServers": {
    "decrypt-file": {
      "type": "streamable-http",        // stdio | sse | streamable-http
      "url": "http://localhost:8000/mcp",
      "headers": {...},
      "scope": "system",                // system | agent | user（元数据）
      "disabled": false                 // 停用开关（元数据）
    },
    "qq-mail": {
      "type": "sse",
      "url": "...",
      "scope": "user",
      "disabled": true
    }
  }
}
"""

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from langchain.tools import BaseTool
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt.tool_node import ToolInvocationError
from langgraph.types import Command
from pydantic import PrivateAttr
from src.core import long_task
from src.core.config import settings

logger = logging.getLogger(__name__)

# mcp.json 路径：跟随工程目录（AGENT_WORKSPACE），便于随项目共享与迁移
_CONFIG_DIR = Path(settings.agent_workspace)
CONFIG_PATH = _CONFIG_DIR / "mcp.json"

# 系统预装 MCP 默认配置（首次运行时写入）
# url 取自 settings.mcp_server_url：dev 同机裸跑 = http://localhost:8000/mcp；
# 生产 docker-compose 下 mcp 容器化进同一 appnet，由 .env 覆盖为 http://geesun-mcp:8000/mcp。
_DEFAULT_SYSTEM_SERVERS = {
    "decrypt-file": {
        "type": "streamable-http",
        "url": settings.mcp_server_url,
        "headers": {"Authorization": f"Bearer {settings.mcp_token}"},
        "scope": "system",
        "disabled": False,
    }
}


def get_config_path() -> str:
    """返回 mcp.json 绝对路径，供 GET /api/v1/mcp/json 展示。"""
    return str(CONFIG_PATH)


def read_config() -> dict:
    """公开读取 mcp.json（不存在时初始化默认配置）。"""
    return _read_config()


def write_config(config: dict) -> None:
    """公开写入 mcp.json（校验结构 + 备份 + 热重载）。"""
    if not isinstance(config, dict) or not isinstance(config.get("mcpServers"), dict):
        raise ValueError("配置必须是包含 mcpServers 对象的 JSON")
    _write_config(config)
    invalidate_cache()


def _ensure_default_config() -> None:
    """首次运行时创建 {AGENT_WORKSPACE}/mcp.json 并写入系统预装配置。"""
    if CONFIG_PATH.exists():
        return
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _write_config({"mcpServers": _DEFAULT_SYSTEM_SERVERS})
        logger.info("mcp.json 初始化完成: %s", CONFIG_PATH)
    except Exception as e:  # noqa: BLE001
        logger.warning("mcp.json 初始化失败: %s", e)


def _read_config() -> dict:
    """读取 mcp.json；不存在或损坏时返回空结构（并尝试初始化）。"""
    _ensure_default_config()
    if not CONFIG_PATH.exists():
        return {"mcpServers": {}}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("mcp.json 解析失败（%s），按空配置处理", e)
        return {"mcpServers": {}}


def _write_config(config: dict) -> None:
    """写 mcp.json（写前备份 .bak.<ts>）。"""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            backup = CONFIG_PATH.with_name(f"mcp.json.bak.{int(time.time())}")
            shutil.copy2(CONFIG_PATH, backup)
        except Exception as e:  # noqa: BLE001
            logger.warning("mcp.json 备份失败: %s", e)
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_servers() -> list[dict]:
    """返回所有 MCP 服务条目（合并后的扁平列表，含元数据字段）。"""
    config = _read_config()
    servers = config.get("mcpServers", {}) or {}
    result = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        server = dict(entry)
        server["name"] = name
        server.setdefault("scope", "user")
        server.setdefault("disabled", False)
        # 后端可补充 type（老配置可能只有 transport 字段）
        if "type" not in server and "transport" in server:
            server["type"] = server.pop("transport")
        result.append(server)
    return result


def get_server(name: str) -> dict | None:
    """按名字取单个服务条目。"""
    for s in list_servers():
        if s["name"] == name:
            return s
    return None


def upsert_server(payload: dict) -> dict:
    """新增或更新一个 MCP 服务条目（写回 mcp.json 并热重载）。"""
    name = payload.get("name", "").strip()
    if not name:
        raise ValueError("name 不能为空")
    if name in ("system", "agent", "user"):
        raise ValueError("name 不能使用保留字")

    config = _read_config()
    servers = config.setdefault("mcpServers", {})

    existing = servers.get(name)
    entry = dict(payload)
    entry.pop("name", None)
    entry.pop("tool_count", None)
    entry.setdefault("scope", (existing or {}).get("scope", "user"))
    entry.setdefault("disabled", False)
    entry.setdefault("type", "streamable-http")
    # 清理空字段
    for k in ("command", "args", "url", "env", "headers"):
        if entry.get(k) in (None, "", []):
            entry.pop(k, None)

    servers[name] = entry
    _write_config(config)
    invalidate_cache()
    return dict(entry, name=name)


def remove_server(name: str) -> None:
    """删除一个 MCP 服务条目（仅允许 scope=user）。"""
    config = _read_config()
    servers = config.get("mcpServers", {}) or {}
    entry = servers.get(name)
    if entry is None:
        raise KeyError(name)
    if entry.get("scope", "user") != "user":
        raise PermissionError("系统预装 / Agent 自创的 MCP 不可删除")
    del servers[name]
    _write_config(config)
    invalidate_cache()


def set_disabled(name: str, disabled: bool) -> dict:
    """修改 disabled 开关（持久化，不删除条目）。"""
    config = _read_config()
    servers = config.get("mcpServers", {}) or {}
    if name not in servers:
        raise KeyError(name)
    servers[name]["disabled"] = bool(disabled)
    _write_config(config)
    invalidate_cache()
    return dict(servers[name], name=name)


# ─── MCP 客户端加载 ──────────────────────────────────────────────────────────

# 客户端+工具缓存：key = frozenset(启用的 server 名集合)
# value = (MultiServerMCPClient, tools 列表) — 复用 client 避免重复握手/泄漏
# ⚠️ 只缓存**全部成功**的加载结果：有失败就不写缓存（见 get_mcp_tools 末尾），
#    否则一次瞬态故障（如 stack 重启时 swarm DNS 竞态）会被缓存成
#    "该 server 在容器生命周期内永久不可用"——2026-09-12 生产实测
#    （16:15:44 decrypt-file DNS 解析失败 → 此后每轮 chat 都拿不到工具，
#    agent 被迫走 base64 绕路导出 Excel）。
_tools_cache: dict[frozenset, tuple[object, list[BaseTool]]] = {}
# 连接重试（2026-09-12）：stack 重新发布时 agent 与 geesun-mcp 同批重启，
# agent 先就绪时 mcp 的 swarm DNS 名还没注册（"Temporary failure in name
# resolution"），单次连接必败。退避重试 2 次覆盖这个几秒级的竞态窗口；
# mcp 真宕机时最坏每轮多花 ~12s（5s 超时 + 2s 退避 + 5s 超时），可接受。
_MCP_CONNECT_RETRIES = 2
_MCP_CONNECT_BACKOFF = 2.0
_MCP_CONNECT_TIMEOUT = 5
# 工具信息缓存：{server_name: {"ts": 探测时间戳, "tools": [{name, description}]}}
# 30 分钟 TTL，配置变更（invalidate_cache）后立即失效
_tool_info_cache: dict[str, dict] = {}
_TOOL_CACHE_TTL = 30 * 60  # 30 分钟


def invalidate_cache() -> None:
    """配置变更后清空缓存（热重载）。"""
    _tools_cache.clear()
    _tool_info_cache.clear()


def _enabled_entries(names: list[str] | None = None) -> dict[str, dict]:
    """返回启用中的 MCP 配置；names 给定则仅返回其中启用的条目。"""
    entries = {
        s["name"]: s
        for s in list_servers()
        if not s.get("disabled")
    }
    if not names:
        return entries
    # 透传集合与启用集取交集；显式传入但被停用的名字会被忽略
    return {n: entries[n] for n in names if n in entries}


def _to_client_config(entry: dict) -> dict:
    """把 mcp.json 条目转换为 MultiServerMCPClient 期望的配置格式。

    注意：langchain-mcp-adapters 0.2.2 的 create_session 要求每个 server
    必须显式包含 transport 键（stdio/sse/websocket/http 之一），缺失即抛错。
    """
    cfg: dict = {}
    server_type = (entry.get("type") or entry.get("transport") or "http").lower()
    # 归一化到 0.2.2 可接受的 transport 值
    if server_type in ("streamable_http", "streamable-http", "http"):
        cfg["transport"] = "http"
    elif server_type in ("stdio", "sse", "websocket"):
        cfg["transport"] = server_type
    else:
        # 未知类型兜底为 http
        cfg["transport"] = "http"
    for key in ("url", "command", "args", "env", "headers"):
        val = entry.get(key)
        if val not in (None, "", [], {}):
            cfg[key] = val
    return cfg


class _DownloadGuardTool(BaseTool):
    """代理包装 download_from_sandbox：参数错返回友好中文提示而非 pydantic
    原始异常（2026-08-18 实测模型误传 file_path 报英文校验错无法自纠）。

    注意：不能对 pydantic v2 模型做实例属性赋值（__setattr__ 校验拦截，
    'StructuredTool object has no field ainvoke' 启动失败——16:21 实测），
    必须用子类覆写 ainvoke/invoke 方法（合法方法定义，非属性遮蔽）。
    """

    _inner: BaseTool = PrivateAttr()

    def __init__(self, inner: BaseTool):
        super().__init__(
            name=inner.name,
            description=inner.description,
            args_schema=inner.args_schema,
            return_direct=getattr(inner, "return_direct", False),
        )
        self._inner = inner

    async def ainvoke(self, input, config=None, **kwargs):
        # 自动修正：模型误用 upload 系列参数名 file_path（实为它想要的 host_path）
        # ——修正后走内层正常执行；其余参数错误由内层 pydantic 校验触发
        # langgraph 原生错误通道（ValidationError→ToolInvocationError→error
        # ToolMessage 给模型），不崩流（19:58 实测：自己抛 ToolException 会崩）
        if isinstance(input, dict) and "file_path" in input and "host_path" not in input:
            input = {**input, "host_path": input["file_path"]}
            input.pop("file_path", None)
        return await self._inner.ainvoke(input, config, **kwargs)

    def invoke(self, input, config=None, **kwargs):
        if isinstance(input, dict) and "file_path" in input and "host_path" not in input:
            input = {**input, "host_path": input["file_path"]}
            input.pop("file_path", None)
        return self._inner.invoke(input, config, **kwargs)

    def _run(self, *args, **kwargs):  # pragma: no cover - 仅同步路径兜底
        raise NotImplementedError("download_from_sandbox 仅支持异步执行")

    async def _arun(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError("download_from_sandbox 仅支持异步执行")


def _guard_download_tool(tools: list[BaseTool]) -> list[BaseTool]:
    """把 download_from_sandbox 替换为 _DownloadGuardTool 包装（幂等）。"""
    return [
        _DownloadGuardTool(t)
        if t.name == "download_from_sandbox" and not isinstance(t, _DownloadGuardTool)
        else t
        for t in tools
    ]


def _first_exception(exc: BaseException) -> BaseException | None:
    """递归展开 ExceptionGroup/TaskGroup 包装，返回最内层首个异常。

    asyncio.TaskGroup 失败时抛 ExceptionGroup，其 str() 只显示
    "unhandled errors in a TaskGroup (N sub-exception)"，真实根因
    （ConnectError/TimeoutError/ProtocolError 等）在子异常里。此函数
    沿第一个子异常链递归到底，供日志透出可诊断信息。
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _shield_error(
    tool_name: str, exc: BaseException, tool_kwargs: dict | None
) -> ToolInvocationError:
    """把 MCP 工具执行异常包装成 ToolInvocationError 抛出。

    走 langgraph 原生错误通道：ToolNode._execute_tool_async 的 except 分支
    捕获后经 _default_handle_tool_errors（tool_node.py:383-393，只放行
    ToolInvocationError）自动转 ToolMessage(status="error") 回灌模型自纠，
    agent 流不中断——对齐 deer-flow（tool_error_handling_middleware.py:
    128-152）/ deepseek-harness（index.ts:1554）"工具错误永不崩流"哲学。

    2026-09-09 演进：初版 shield 直接返回错误 str，实测 langgraph 新版
    _normalize_tool_response（tool_node.py:1432-1454）只接受 Command /
    ToolMessage，裸 str 抛 TypeError 照样崩流（"Tool paddleocr_vl returned
    unexpected type: <class 'str'>"）——故改为抛出 ToolInvocationError 走
    原生通道（message 模板含工具名/参数/根因，模型可据此自纠）。

    ExceptionGroup/TaskGroup 递归展开取最内层根因（防 "unhandled errors
    in a TaskGroup (N sub-exception)" 外壳吞细节）；kwargs 超长截断防错误
    消息撑爆上下文。
    """
    inner = _first_exception(exc) or exc
    kw: dict = {}
    if isinstance(tool_kwargs, dict):
        for k, v in tool_kwargs.items():
            s = str(v)
            kw[k] = s[:200] + "…(截断)" if len(s) > 200 else v
    return ToolInvocationError(tool_name=tool_name, source=inner, tool_kwargs=kw)


def _tool_call_id_of(tool_input: Any) -> str:
    """从工具入参里取 tool_call_id（langgraph 传入的是 tool_call dict）。

    取不到返回空串：探针/单测会直接 `ainvoke({...参数...})` 裸调，没有 id。
    归一化仍要产出 ToolMessage（空 id 合法），否则调用方拿到的类型不一致。
    """
    if isinstance(tool_input, dict):
        raw = tool_input.get("id")
        if isinstance(raw, str) and raw.strip():
            return raw
    return ""


def _as_tool_output(result: Any, tool_input: Any, tool_name: str) -> Any:
    """把 durable 长任务的返回值归一成 `ToolMessage` / `Command`（2026-10-23）。

    ★ 事故（589e99f5）：262 页 OCR 跑满 4h45m、第 60 轮 status=completed，
      0.4 秒后整轮崩在
      `Tool ocr_pdf_to_text returned a list with invalid element types:
       expected all Command or ToolMessage`，结论全丢、用户只看到这句异常。

    根因三跳（2026-10-23 逐跳实测，不是推测）：
      1. `long_task.drive` 的**句柄续查**分支（方案 D）构造
         `poll_args = {"sandbox_id", "job_id", "wait_seconds"}` —— 纯句柄 dict，
         既没有 `type="tool_call"` 也没有 `id`，`_is_tool_call()` 判 False ⇒
         `tool_call_id=None`。首轮 `invoke(call_args)` 是 `dict(tool_call)`
         浅拷贝、**保留 type/id**，所以「首轮正常、续查轮才出问题」；
      2. langchain-core `BaseTool._format_output` 有一条硬规则：
         `if isinstance(content, ToolOutputMixin) or tool_call_id is None:
          return content` —— 拿不到 id 就**原样返回裸 content**；
      3. langgraph `ToolNode._normalize_tool_response` 只接受
         `Command` / `ToolMessage` / `list[Command|ToolMessage]`，
         见到 MCP 的 `list[{"type":"text",...}]` 直接 TypeError。

    ★ 为什么潜伏了两周才炸：前 59 轮 status=running 全在 `drive` 内部
      循环、从不外抛，**第 60 轮 completed 才第一次真正把结果交出去**。
      这条路径自 durable 上线起一次都没走通过（此前长任务要么被循环检测
      掐死、要么被取消）。e2e 样本在容器内直调 MCP、绕过 tool_node，
      所以测不到 —— 「桩比实现更宽容」的又一例。

    ★ 归一化必须放在 `drive` **返回之后**：`drive` 内部要靠原始 list 解析
      status/进度/job_id（`result_text`/`result_payload`），提前包装会让
      续查逻辑全瞎。

    content 用 `long_task.result_text` 拼（MCP text blocks → 文本），
    不自己另写一套解析口径：那边已被 drive / 循环检测共用且有单测锁定。
    非文本块（image/resource）拿不到 text 时整体 `str()` 兜底，不丢信息。
    """
    if isinstance(result, (ToolMessage, Command)):
        return result
    text = long_task.result_text(result)
    return ToolMessage(
        content=text if isinstance(text, str) else str(text),
        name=tool_name,
        tool_call_id=_tool_call_id_of(tool_input),
    )


class _MCPErrorShieldTool(BaseTool):
    """通用 MCP 工具错误防护包装（2026-09-09）。

    捕获 MCP 工具执行期一切异常（含 langchain_mcp_adapters 对服务端
    isError 转出的 ToolException、网络/超时/连接错等），包装成
    ToolInvocationError 抛出——langgraph 原生错误通道自动把它转成
    ToolMessage(status="error") 回灌模型自纠，agent 流不中断。

    实现要点（沿用 _DownloadGuardTool 的既有教训）：
    1. pydantic v2 模型不可做实例属性赋值（__setattr__ 校验拦截，
       "StructuredTool object has no field ainvoke" 启动失败），必须子类
       覆写 ainvoke/invoke；
    2. 只能 raise ToolInvocationError（_default_handle_tool_errors 唯一
       放行类型）——直接 re-raise 原异常（ToolException 等）会被 langgraph
       当作未处理错误整轮崩流（2026-09-09 paddleocr_vl 实证）；返回裸 str
       也会被 _normalize_tool_response 拒绝（同上实证）；
    3. 幂等：已包 shield 的不重复包。
    """

    _inner: BaseTool = PrivateAttr()
    # 方案 D（2026-10-22）：轻量续查工具（如 ocr_status）的**内层**引用，
    # 由 _shield_tools 批量挂到 durable shield 上。None = MCP 没这个工具
    # （旧版）⇒ drive 走原路径（同参复调），双端独立发版互不阻断。
    # 存内层而不是 shield：drive 自己管超时/重试，再过一层 shield 语义重复。
    _status_tool: BaseTool | None = PrivateAttr(default=None)

    def __init__(self, inner: BaseTool):
        super().__init__(
            name=inner.name,
            description=inner.description,
            args_schema=inner.args_schema,
            return_direct=getattr(inner, "return_direct", False),
        )
        self._inner = inner

    def _supports_wait_seconds(self) -> bool:
        """该工具参数表里是否有 `wait_seconds`（决定要不要帮模型抬到上限）。

        为什么需要：MCP 侧 `ocr_pdf_to_text(wait_seconds=90)` 的默认值只有 90s，
        而单次可等的上限是 300s。模型不显式传时，自动续查每轮只睡 90s ⇒
        续查轮次翻三倍（多出来的都是空转 RPC）。这里读一次 schema 判断，
        有就抬到 `mcp_durable_poll_wait_sec`。

        pydantic v2 的 `args_schema.model_fields` 是 dict；MCP 工具 schema 由
        langchain_mcp_adapters 动态生成，个别版本可能是 v1 风格 —— 两种都试，
        取不到就当"不支持"（保守：退化为用模型原参数，仍然能用）。
        """
        schema = getattr(self._inner, "args_schema", None)
        if schema is None:
            return False
        fields = getattr(schema, "model_fields", None)
        if isinstance(fields, dict):
            return "wait_seconds" in fields
        fields = getattr(schema, "__fields__", None)
        if isinstance(fields, dict):
            return "wait_seconds" in fields
        return False

    def _make_status_invoke(self):
        """构造轻量续查协程工厂（方案 D）；未挂 status 工具时返回 None = 降级。

        只闭包内层工具：drive 自己套 wait_for（rpc_timeout_sec）与重试，
        这里不再叠加 shield 的墙钟封顶（语义重复且超时口径可能不一致）。
        """

        async def _invoke_status(args: dict):
            return await self._status_tool.ainvoke(args)

        return _invoke_status if self._status_tool is not None else None

    async def ainvoke(self, input, config=None, **kwargs):
        # 长任务自动续查（2026-10-21）：白名单工具（默认 ocr_pdf_to_text）
        # 在这里把「模型连续调 30~70 次同参工具」压成「模型调 1 次、工具内部
        # 循环续查」。理由与收益见 core/long_task.py 模块 docstring。
        #
        # ★ 必须套在下方统一的 try 里：drive 抛出的任何异常（连续 RPC 失败、
        #   参数错）同样要走 _shield_error → ToolInvocationError，否则 langgraph
        #   按未处理错误整轮崩流（2026-09-09 paddleocr_vl 实证）。
        durable = long_task.is_durable_tool(self.name)
        try:
            if durable:
                result = await long_task.drive(
                    self.name,
                    input,
                    lambda args: self._inner.ainvoke(args, config, **kwargs),
                    budget_sec=settings.mcp_durable_task_budget_sec,
                    rpc_timeout_sec=settings.mcp_tool_call_timeout_sec,
                    poll_wait_sec=settings.mcp_durable_poll_wait_sec,
                    supports_wait=self._supports_wait_seconds(),
                    status_invoke=self._make_status_invoke(),
                )
                # ★ 必须归一化后再返回（589e99f5 事故）：drive 内部把 tool_call
                #   转成了纯 args dict，内层 `_format_output` 因 tool_call_id=None
                #   原样返回 MCP 的 list[dict]，langgraph 校验直接 TypeError 崩轮。
                return _as_tool_output(result, input, self.name)
            # 单次工具调用墙钟封顶（mcp_tool_call_timeout_sec，2026-10-19 方案 3）：
            # 653d8515 实锤 agent 侧无工具超时时，服务端挂住 = run 永久 running。
            # httpx read 超时只管"两次读之间"，keepalive 流可无限续期——只有
            # 调用侧的 wait_for 能兜底。超时抛 TimeoutError → 下方转
            # ToolInvocationError 回灌模型（submit-and-poll 工具可原参重试续查）。
            timeout = settings.mcp_tool_call_timeout_sec
            if timeout and timeout > 0:
                return await asyncio.wait_for(
                    self._inner.ainvoke(input, config, **kwargs), timeout=timeout
                )
            return await self._inner.ainvoke(input, config, **kwargs)
        except asyncio.TimeoutError as e:
            if durable:
                # durable 分支的单次 RPC 超时已在 drive 内部重试过；
                # 走到这里 = 连续多次 RPC 挂住，语义是"服务端一直无响应"，
                # 不是"任务跑得久"（任务时长由 budget_sec 管）。
                logger.warning(
                    "MCP 长任务工具 [%s] 连续多次单次调用超时（每次 %ss），放弃续查",
                    self.name, settings.mcp_tool_call_timeout_sec,
                )
                raise _shield_error(
                    self.name,
                    TimeoutError(
                        f"长任务工具连续多次单次调用超时（每次封顶 "
                        f"{settings.mcp_tool_call_timeout_sec}s），已停止等待。"
                        "服务端任务可能仍在后台运行，请稍后用完全相同的参数再调一次。"
                    ),
                    input if isinstance(input, dict) else {},
                ) from e
            logger.warning(
                "MCP 工具 [%s] 调用超过 %ss 未返回（已按墙钟封顶取消，"
                "服务端任务可能仍在跑）",
                self.name, settings.mcp_tool_call_timeout_sec,
            )
            raise _shield_error(
                self.name,
                TimeoutError(
                    f"工具调用超过 {settings.mcp_tool_call_timeout_sec}s 未返回，"
                    "已中止等待（服务端后台任务可能仍在运行）。"
                    "若是 ocr_pdf_to_text 等提交-轮询工具，请用完全相同的参数"
                    "再调一次续查/取结果；若是其他工具，请检查参数或稍后重试。"
                ),
                input if isinstance(input, dict) else {},
            ) from e
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "MCP 工具 [%s] 执行失败（已包装 ToolInvocationError 回灌模型自纠）: %s",
                self.name, _first_exception(e) or e,
            )
            raise _shield_error(self.name, e, input if isinstance(input, dict) else {}) from e

    def invoke(self, input, config=None, **kwargs):
        try:
            return self._inner.invoke(input, config, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "MCP 工具 [%s] 执行失败（已包装 ToolInvocationError 回灌模型自纠）: %s",
                self.name, _first_exception(e) or e,
            )
            raise _shield_error(self.name, e, input if isinstance(input, dict) else {}) from e

    def _run(self, *args, **kwargs):  # pragma: no cover - 同步路径兜底
        raise NotImplementedError("MCP 工具仅支持异步执行")

    async def _arun(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError("MCP 工具仅支持异步执行")


def _shield_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """给所有 MCP 工具批量套错误防护包装（幂等）。

    download_from_sandbox 的参数修正 guard 在内层、shield 在外层兜错误
    （guard 内层 MCP 调用失败同样会被 shield 接住，不崩流）。

    方案 D（2026-10-22）：把轻量续查工具（settings.mcp_durable_status_tool，
    默认 ocr_status）的**内层**引用挂到每个 durable shield 上——drive 首轮
    返回带 job_id 时改走它续查（只传句柄，不重发全量参数 = 不再每轮重传
    256MB 加密文件）。工具不在列表里（旧版 MCP）时不挂 ⇒ drive 自动降级
    走原路径。⚠ 挂在 shield 而不是在 ainvoke 里现查全局缓存：工具列表是
    按 server 集合缓存/逐用户透传的，shield 实例自带引用最直接、无并发歧义。
    """
    shields = [
        _MCPErrorShieldTool(t) if not isinstance(t, _MCPErrorShieldTool) else t
        for t in tools
    ]
    status_name = (getattr(settings, "mcp_durable_status_tool", "") or "").strip()
    if status_name:
        status_inner = next(
            (t for t in tools if getattr(t, "name", "") == status_name), None
        )
        if status_inner is None:
            logger.info(
                "durable 轻量续查工具 [%s] 不在本次 MCP 工具列表，"
                "自动续查将走原路径（同参复调）",
                status_name,
            )
        for s in shields:
            if long_task.is_durable_tool(s.name):
                s._status_tool = status_inner
    return shields


async def get_mcp_tools(names: list[str] | None = None) -> list[BaseTool]:
    """获取 MCP 工具。

    - names=None → 使用全部启用中的 MCP
    - names=[...] → 仅使用列表中且启用中的 MCP（chat 透传）
    - 逐个 server 独立连接：单个服务连接失败只跳过该服务并告警，
      不影响其他服务（避免 MultiServerMCPClient 批量连接时一个失败拖垮全部）
    结果按 server 集合缓存，配置变更（invalidate_cache）后失效。
    """
    entries = _enabled_entries(names)
    if not entries:
        return []

    cache_key = frozenset(entries.keys())
    if cache_key in _tools_cache:
        return _tools_cache[cache_key][1]

    results: list[BaseTool] = []
    failed: list[str] = []

    async def connect_one(name: str, entry: dict) -> None:
        last_exc: Exception | None = None
        for attempt in range(_MCP_CONNECT_RETRIES + 1):
            try:
                client = MultiServerMCPClient({name: _to_client_config(entry)})
                tools = await asyncio.wait_for(
                    client.get_tools(), timeout=_MCP_CONNECT_TIMEOUT
                )
                results.extend(tools)
                return
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if attempt < _MCP_CONNECT_RETRIES:
                    logger.warning(
                        "MCP server [%s] 第 %d/%d 次连接失败（%s），%.0fs 后重试",
                        name, attempt + 1, _MCP_CONNECT_RETRIES + 1,
                        _first_exception(e) or e, _MCP_CONNECT_BACKOFF,
                    )
                    await asyncio.sleep(_MCP_CONNECT_BACKOFF)
        failed.append(name)
        # TaskGroup/ExceptionGroup 的 str() 只显示 "unhandled errors in a TaskGroup
        # (N sub-exception)"，真实原因（连接拒绝/超时/协议错）被吞在子异常里——
        # 递归展开到最内层首个异常，日志一眼可见真因（2026-08-19 实测踩坑：
        # decrypt-file 加载失败但日志无任何可诊断信息，只能靠外部探测端口）。
        e = last_exc  # 必然非 None（重试耗尽才会到这里）
        inner = _first_exception(e)
        if inner is not None and inner is not e:
            logger.warning(
                "MCP server [%s] 加载工具失败（已重试 %d 次）: %s（子异常: %s）",
                name, _MCP_CONNECT_RETRIES + 1, e, inner,
            )
        else:
            logger.warning(
                "MCP server [%s] 加载工具失败（已重试 %d 次）: %s",
                name, _MCP_CONNECT_RETRIES + 1, e,
            )

    await asyncio.gather(
        *[connect_one(name, entry) for name, entry in entries.items()]
    )

    if failed:
        logger.warning(
            "MCP 工具加载完成，%d/%d 个服务失败: %s（本轮不缓存失败结果，"
            "下一轮 chat 将自动重连）",
            len(failed),
            len(entries),
            ", ".join(failed),
        )
        # 失败不写缓存：get_mcp_tools 每轮 chat 都会被调用（chat.py:354），
        # 不缓存意味着下一轮自动重试——瞬态故障（DNS 竞态、mcp 滚动更新）
        # 下一轮自愈。成功 server 的重连成本只是一次 HTTP 握手（≤5s 超时）。
        return _shield_tools(_guard_download_tool(results))

    _tools_cache[cache_key] = (None, _shield_tools(_guard_download_tool(results)))
    return _tools_cache[cache_key][1]


async def get_tool_info_map(
    names: list[str] | None = None,
) -> dict[str, list[dict]]:
    """按 server 返回工具信息列表 [{name, description}]（30 分钟 TTL 缓存）。

    - 服务不可达/探测失败 → 该 server 返回空列表
    - names 给定 → 仅探测列表中且启用的 server
    """
    entries = _enabled_entries(names)
    if not entries:
        return {}

    now = time.time()
    result: dict[str, list[dict]] = {}

    async def probe(name: str, entry: dict) -> None:
        cached = _tool_info_cache.get(name)
        if cached and (now - cached["ts"]) < _TOOL_CACHE_TTL:
            result[name] = cached["tools"]
            return
        try:
            client = MultiServerMCPClient({name: _to_client_config(entry)})
            tools = await asyncio.wait_for(client.get_tools(), timeout=5)
            infos = [
                {"name": t.name, "description": (t.description or "")[:300]}
                for t in tools
            ]
            _tool_info_cache[name] = {"ts": now, "tools": infos}
            result[name] = infos
        except Exception as e:  # noqa: BLE001
            result[name] = []
            logger.warning("探测 MCP server [%s] 工具失败: %s", name, e)

    await asyncio.gather(
        *[probe(name, entry) for name, entry in entries.items()]
    )
    return result


async def get_tool_count_map() -> dict[str, int]:
    """按 server 返回工具数（兼容旧调用，基于 get_tool_info_map 派生）。"""
    info = await get_tool_info_map()
    return {name: len(tools) for name, tools in info.items()}

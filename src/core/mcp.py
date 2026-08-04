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

from langchain.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from src.core.config import settings

logger = logging.getLogger(__name__)

# mcp.json 路径：跟随工程目录（AGENT_WORKSPACE），便于随项目共享与迁移
_CONFIG_DIR = Path(settings.agent_workspace)
CONFIG_PATH = _CONFIG_DIR / "mcp.json"

# 系统预装 MCP 默认配置（首次运行时写入）
_DEFAULT_SYSTEM_SERVERS = {
    "decrypt-file": {
        "type": "streamable-http",
        "url": "http://localhost:8000/mcp",
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
_tools_cache: dict[frozenset, tuple[object, list[BaseTool]]] = {}
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
        try:
            client = MultiServerMCPClient({name: _to_client_config(entry)})
            tools = await asyncio.wait_for(client.get_tools(), timeout=10)
            results.extend(tools)
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            logger.warning("MCP server [%s] 加载工具失败: %s", name, e)

    await asyncio.gather(
        *[connect_one(name, entry) for name, entry in entries.items()]
    )

    if failed:
        logger.warning(
            "MCP 工具加载完成，%d/%d 个服务失败: %s",
            len(failed),
            len(entries),
            ", ".join(failed),
        )

    _tools_cache[cache_key] = (None, results)
    return results


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

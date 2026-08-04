"""MCP 连接器管理接口。

对标 WorkBuddy 连接器架构：
- 连接器 = mcp.json 中的 mcpServers 条目
- 管理连接器 = 本模块的 CRUD + 启停 + JSON 直写
- MCP Hub = 内置静态列表（预留官方 Hub API 接入）
配置文件：~/.geesun_agent/mcp.json
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api.deps import get_current_user
from src.core import mcp as mcp_service

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── Pydantic 模型 ────────────────────────────────────────────────────────────


class McpServerPayload(BaseModel):
    name: str
    scope: str = "user"  # system | agent | user
    type: str = "streamable-http"  # stdio | sse | streamable-http
    command: Optional[str] = None
    args: Optional[list[str]] = None
    url: Optional[str] = None
    env: Optional[dict] = None
    headers: Optional[dict] = None
    disabled: bool = False


class ToggleRequest(BaseModel):
    disabled: bool


class JsonWriteRequest(BaseModel):
    content: dict


class HubInstallRequest(BaseModel):
    name: str


# ─── 内置 MCP Hub 静态列表（预留官方 Hub API 接入点） ──────────────────────

_HUB_ITEMS = [
    {
        "name": "next-devtools",
        "description": "Next.js 官方开发工具 MCP：路由状态、组件树、运行时诊断。",
        "install_config": {
            "name": "next-devtools",
            "scope": "user",
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "next-devtools-mcp@latest"],
        },
    },
    {
        "name": "langchain",
        "description": "LangChain 官方 MCP 端点（streamable-http），提供文档检索等工具。",
        "install_config": {
            "name": "langchain",
            "scope": "user",
            "type": "streamable-http",
            "url": "http://localhost:8123/mcp",
        },
    },
    {
        "name": "qq-mail",
        "description": "QQ 邮箱 MCP 服务：收件箱、发送邮件、附件管理。",
        "install_config": {
            "name": "qq-mail",
            "scope": "user",
            "type": "streamable-http",
            "url": "http://localhost:8123/mcp",
        },
    },
    {
        "name": "tencent-docs",
        "description": "腾讯文档 MCP：文档创建、编辑、读取、权限管理。",
        "install_config": {
            "name": "tencent-docs",
            "scope": "user",
            "type": "streamable-http",
            "url": "http://localhost:8124/mcp",
        },
    },
    {
        "name": "feishu",
        "description": "飞书 MCP：消息、群组、文档、审批流。",
        "install_config": {
            "name": "feishu",
            "scope": "user",
            "type": "streamable-http",
            "url": "http://localhost:8125/mcp",
        },
    },
    {
        "name": "dingtalk",
        "description": "钉钉 MCP：消息、审批、通讯录。",
        "install_config": {
            "name": "dingtalk",
            "scope": "user",
            "type": "streamable-http",
            "url": "http://localhost:8126/mcp",
        },
    },
]


# ─── 接口 ─────────────────────────────────────────────────────────────────────


@router.get("/mcp/servers")
async def list_mcp_servers(
    current_user: dict = Depends(get_current_user),
):
    """列出所有 MCP 服务（system / agent / user 三类合并），含已探测的工具列表。"""
    servers = mcp_service.list_servers()
    # 附加工具信息（名称 + 描述，30 分钟 TTL 缓存）
    try:
        info = await mcp_service.get_tool_info_map()
        for s in servers:
            tools = info.get(s["name"], [])
            s["tool_count"] = len(tools)
            s["tools"] = tools
    except Exception:  # noqa: BLE001
        for s in servers:
            s["tool_count"] = 0
            s["tools"] = []
    return {"servers": servers}


@router.post("/mcp/servers")
async def create_mcp_server(
    payload: McpServerPayload,
    current_user: dict = Depends(get_current_user),
):
    """新增 / 更新 MCP 服务（写 mcp.json + 热重载）。"""
    try:
        server = mcp_service.upsert_server(payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("新增 MCP 失败")
        raise HTTPException(status_code=500, detail=f"写入失败: {e}")
    return server


@router.patch("/mcp/servers/{name}/toggle")
async def toggle_mcp_server(
    name: str,
    payload: ToggleRequest,
    current_user: dict = Depends(get_current_user),
):
    """启停 MCP 服务（只改 disabled，不删除条目）。"""
    try:
        return mcp_service.set_disabled(name, payload.disabled)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"MCP 服务不存在: {name}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"操作失败: {e}")


@router.delete("/mcp/servers/{name}")
async def delete_mcp_server(
    name: str,
    current_user: dict = Depends(get_current_user),
):
    """删除 MCP 服务（仅 scope=user 允许）。"""
    try:
        mcp_service.remove_server(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"MCP 服务不存在: {name}")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")
    return {"ok": True, "name": name}


@router.get("/mcp/json")
async def get_mcp_json(
    current_user: dict = Depends(get_current_user),
):
    """直读 mcp.json（路径 + 内容）。"""
    return {
        "path": mcp_service.get_config_path(),
        "content": mcp_service.read_config(),
    }


@router.put("/mcp/json")
async def put_mcp_json(
    payload: JsonWriteRequest,
    current_user: dict = Depends(get_current_user),
):
    """直写 mcp.json：校验结构 + 备份 + 写回 + 热重载。"""
    content = payload.content
    if not isinstance(content, dict) or "mcpServers" not in content:
        raise HTTPException(status_code=400, detail="content 必须是包含 mcpServers 的对象")
    servers = content.get("mcpServers")
    if not isinstance(servers, dict):
        raise HTTPException(status_code=400, detail="mcpServers 必须是对象")
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=400, detail=f"mcpServers.{name} 必须是对象"
            )
        entry.setdefault("scope", "user")
        entry.setdefault("disabled", False)
        if "type" not in entry and "transport" in entry:
            entry["type"] = entry.pop("transport")
        elif "type" not in entry:
            entry["type"] = "streamable-http"
    try:
        mcp_service.write_config(content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("写入 mcp.json 失败")
        raise HTTPException(status_code=500, detail=f"写入失败: {e}")
    return {
        "path": mcp_service.get_config_path(),
        "content": mcp_service.read_config(),
    }


@router.get("/mcp/hub")
async def get_mcp_hub(
    q: str = Query("", description="搜索关键词"),
    current_user: dict = Depends(get_current_user),
):
    """MCP Hub 列表。第一版内置静态列表，预留官方 Hub API 接入。"""
    servers = mcp_service.list_servers()
    installed = {s["name"] for s in servers}
    items = []
    for item in _HUB_ITEMS:
        if q and q.lower() not in item["name"].lower() and q.lower() not in item["description"].lower():
            continue
        items.append(
            {
                "name": item["name"],
                "description": item["description"],
                "install_config": item["install_config"],
                "installed": item["name"] in installed,
            }
        )
    return {"items": items}


@router.post("/mcp/hub/install")
async def install_mcp_from_hub(
    payload: HubInstallRequest,
    current_user: dict = Depends(get_current_user),
):
    """一键安装 Hub 中的 MCP（按 install_config 写入 mcp.json）。"""
    item = next((i for i in _HUB_ITEMS if i["name"] == payload.name), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Hub 中不存在: {payload.name}")
    try:
        return mcp_service.upsert_server(dict(item["install_config"]))
    except Exception as e:  # noqa: BLE001
        logger.exception("安装 Hub MCP 失败")
        raise HTTPException(status_code=500, detail=f"安装失败: {e}")

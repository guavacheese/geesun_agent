from langchain.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from src.core.config import settings

_cached_tools: list[BaseTool] | None = None


async def get_mcp_tools() -> list[BaseTool]:
    global _cached_tools
    if _cached_tools is not None:
        return _cached_tools

    try:
        client = MultiServerMCPClient(
            {
                "decrypt-file": {
                    "transport": "streamable-http",
                    "url": "http://localhost:8000/mcp",
                    "headers": {
                        "Authorization": f"Bearer {settings.mcp_token}",
                        "X-customer-header": "custom-value",
                    },
                }
            }
        )
        _cached_tools = await client.get_tools()
        return _cached_tools
    except Exception:
        return []

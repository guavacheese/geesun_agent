from fastapi import Header, HTTPException, Request
from typing import Optional
from src.core.auth import decode_jwt_token


def get_store(request: Request):
    return request.app.state.store


def get_checkpointer(request: Request):
    return request.app.state.checkpointer


def get_stream_bridge(request: Request):
    """进程内事件桥（P1-b）。run 往它 publish，SSE 端点从它 subscribe。

    与 store/checkpointer 同样挂在 `app.state`：它是**进程级单例**，不是每请求一个
    （桥内按 run_id 分流，见 src/core/stream_bridge.py）。单副本前提——扩副本需换
    Redis 桥，接口不变。
    """
    return request.app.state.stream_bridge


async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    """从 Authorization Header 解析 JWT，返回当前用户信息。

    返回 dict 包含: user_id, display_name, role
    请求未携带有效 Token 时返回 401。
    登录接口自身不需要此依赖。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="无效的 Authorization 格式")

    token = authorization.removeprefix("Bearer ")
    payload = decode_jwt_token(token)

    if payload is None:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")

    return {
        "user_id": payload["user_id"],
        "display_name": payload.get("display_name", payload["user_id"]),
        "role": payload.get("role", "user"),
    }

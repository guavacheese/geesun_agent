from fastapi import Header, HTTPException, Request
from typing import Optional
from src.core.auth import decode_jwt_token


def get_store(request: Request):
    return request.app.state.store


def get_checkpointer(request: Request):
    return request.app.state.checkpointer


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

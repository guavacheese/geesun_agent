import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.core.auth import verify_ldap_user, create_jwt_token, search_ldap_users
from src.api.deps import get_current_user
from src.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict


@router.post("/auth/login")
async def login(body: LoginRequest):
    """
    用户登录。

    用 AD 域账号密码验证，验证通过后签发 JWT Token。
    """
    user_info = verify_ldap_user(body.username, body.password)

    if user_info is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_jwt_token(user_info)

    return LoginResponse(
        access_token=token,
        expires_in=settings.jwt_expire_hours * 3600,
        user={
            "user_id": user_info["user_id"],
            "display_name": user_info["display_name"],
            "role": user_info["role"],
        },
    )


# ─── 管理员接口 ───


@router.get("/admin/users")
async def list_users(
    search: str = Query("", description="可选，按用户名或显示名搜索"),
    current_user: dict = Depends(get_current_user),
):
    """
    列出 AD 域用户列表（仅管理员可调用）。

    从 AD 搜索所有用户，返回 user_id、display_name、role。
    支持 search 参数按用户名/显示名模糊搜索。
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")

    users = search_ldap_users(search=search)
    return {"users": users, "total": len(users)}

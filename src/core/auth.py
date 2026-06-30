"""
LDAP/AD 认证 + JWT 签发/验证 工具模块。

流程：
1. 用服务账号（geesunai@geesun.li UPN 格式）绑定 LDAP
2. 搜索用户 sAMAccountName 获取 DN
3. 用用户 DN + 用户密码重新绑定验证身份
4. 验证通过后签发 JWT
"""
import logging
from datetime import datetime, timedelta, timezone

import jwt
from jwt.exceptions import PyJWTError
from ldap3 import Server, Connection, ALL
from ldap3.core.exceptions import LDAPException, LDAPBindError

from src.core.config import settings

logger = logging.getLogger(__name__)


# ─── 辅助函数 ───


def _format_upn(username: str) -> str:
    """将用户名格式化为 UPN：username@domain"""
    return settings.ldap_domain_format % username


def _create_bind_connection(server: Server) -> Connection | None:
    """用服务账号绑定 LDAP（UPN 格式）。"""
    bind_upn = _format_upn(settings.ldap_bind_user)
    try:
        conn = Connection(
            server,
            user=bind_upn,
            password=settings.ldap_bind_password,
            auto_bind=True,
        )
        logger.info("LDAP 服务账号绑定成功: %s", bind_upn)
        return conn
    except LDAPException as e:
        logger.error("LDAP 服务账号绑定失败 (user=%s): %s", bind_upn, e)
        return None


# ─── LDAP 用户验证 ───


def verify_ldap_user(username: str, password: str) -> dict | None:
    """验证 AD 域账号密码，返回用户信息或 None。"""
    server = Server(settings.ldap_server, get_info=ALL)

    bind_conn = _create_bind_connection(server)
    if bind_conn is None:
        return None

    try:
        search_filter = f"(sAMAccountName={username})"
        bind_conn.search(
            settings.ldap_base_dn,
            search_filter,
            attributes=["displayName", "mail", "memberOf"],
        )

        if not bind_conn.entries:
            logger.warning("LDAP 未找到用户: %s", username)
            return None

        entry = bind_conn.entries[0]
        user_dn = entry.entry_dn

        # 用用户 DN + 密码重新绑定，验证密码是否正确
        try:
            Connection(server, user=user_dn, password=password, auto_bind=True)
        except LDAPBindError:
            logger.warning("LDAP 用户密码错误: %s", username)
            return None
        except LDAPException as e:
            logger.error("LDAP 用户验证异常 (%s): %s", username, e)
            return None

        # 解析用户信息
        display_name = str(entry.displayName) if entry.displayName else username
        groups = [str(g) for g in entry.memberOf] if entry.memberOf else []
        is_admin = settings.ldap_admin_group_dn in groups

        logger.info("LDAP 用户验证成功: %s (%s), role=%s", username, display_name, "admin" if is_admin else "user")
        return {
            "user_id": username,
            "display_name": display_name,
            "role": "admin" if is_admin else "user",
            "groups": groups,
        }

    except LDAPException as e:
        logger.error("LDAP 搜索异常: %s", e)
        return None
    finally:
        bind_conn.unbind()


# ─── LDAP 用户列表搜索 ───


def search_ldap_users(search: str = "") -> list[dict]:
    """搜索 AD 用户列表，返回 [{user_id, display_name, role}]。"""
    server = Server(settings.ldap_server, get_info=ALL)

    bind_conn = _create_bind_connection(server)
    if bind_conn is None:
        return []

    try:
        if search:
            search_filter = (
                f"(&(objectClass=user)(objectCategory=person)"
                f"(|(sAMAccountName=*{search}*)(displayName=*{search}*)))"
            )
        else:
            search_filter = "(&(objectClass=user)(objectCategory=person))"

        bind_conn.search(
            settings.ldap_base_dn,
            search_filter,
            attributes=["sAMAccountName", "displayName", "memberOf"],
        )

        users = []
        for entry in bind_conn.entries:
            username = str(entry.sAMAccountName) if entry.sAMAccountName else ""
            if not username:
                continue
            display_name = str(entry.displayName) if entry.displayName else username
            groups = [str(g) for g in entry.memberOf] if entry.memberOf else []
            is_admin = settings.ldap_admin_group_dn in groups
            users.append({
                "user_id": username,
                "display_name": display_name,
                "role": "admin" if is_admin else "user",
            })

        logger.info("LDAP 用户列表搜索完成: total=%d", len(users))
        return users

    except LDAPException as e:
        logger.error("LDAP 搜索用户列表异常: %s", e)
        return []
    finally:
        bind_conn.unbind()


# ─── JWT 签发与验证 ───


def create_jwt_token(user_info: dict) -> str:
    """签发 JWT Token。"""
    payload = {
        "user_id": user_info["user_id"],
        "display_name": user_info["display_name"],
        "role": user_info["role"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_jwt_token(token: str) -> dict | None:
    """解码并验证 JWT Token，返回 payload 或 None。"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return payload
    except PyJWTError as e:
        logger.warning("JWT 验证失败: %s", e)
        return None

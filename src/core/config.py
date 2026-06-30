# 日志配置必须在任何其他导入之前就绪，否则 logger.warning 会丢失时间戳
from src.core.logging import *  # noqa: F401,F403

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    base_url: str
    openai_api_key: str
    model_name: str
    agent_workspace: str

    upload_root: str = "/data/myapp/uploads"
    report_root: str = "/data/myapp/reports"
    mcp_token: str = "YOUR_TOKEN"

    cube_template_id: str = ""
    cube_api_url: str = ""
    cube_api_key: str = "e2b_0000000000000000000000000000000000000000"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "agent_mem"
    postgres_user: str = "postgres"
    postgres_password: str = ""

    # LDAP / AD 配置
    ldap_server: str = "ldap://192.168.1.241:389"
    ldap_base_dn: str = "DC=geesun,DC=li"
    ldap_bind_user: str = "geesunai"
    ldap_bind_password: str = ""
    ldap_domain_format: str = "%s@geesun.li"  # 用于构造 UPN：username@geesun.li
    ldap_admin_group_dn: str = "CN=geesun-admins,CN=Users,DC=geesun,DC=li"

    # JWT 配置
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24 * 7

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


import logging

logger = logging.getLogger(__name__)

logger.warning("[DIAG] Settings.model_config type: %s", type(Settings.model_config))
logger.warning("[DIAG] env_file: %s", Settings.model_config.get("env_file"))

settings = Settings()

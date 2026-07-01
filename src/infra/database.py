from urllib.parse import quote_plus
from src.core.config import settings


def _build_dsn() -> str:
    dsn = (
        f"postgresql://{settings.postgres_user}:"
        f"{quote_plus(settings.postgres_password)}@"
        f"{settings.postgres_host}:"
        f"{settings.postgres_port}/"
        f"{settings.postgres_db}"
    )
    # 添加 TCP keepalive：每隔 60s 发探活包，最多 5 次失败才断开
    # 防止 PostgreSQL 长时间空闲后（如过夜）关闭连接
    dsn += "?keepalives=1&keepalives_idle=60&keepalives_interval=10&keepalives_count=5"
    return dsn

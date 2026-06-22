from urllib.parse import quote_plus
from src.core.config import settings


def _build_dsn() -> str:
    return (
        f"postgresql://{settings.postgres_user}:"
        f"{quote_plus(settings.postgres_password)}@"
        f"{settings.postgres_host}:"
        f"{settings.postgres_port}/"
        f"{settings.postgres_db}"
    )

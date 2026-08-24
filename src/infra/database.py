import asyncio
import logging
from typing import Any, Optional
from urllib.parse import quote_plus

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore

from src.core.config import settings

logger = logging.getLogger(__name__)

# 连接池最大连接数（store / checkpointer 各自一个池）。
# geesun_agent 把它俩作为 app.state 全局单例，被所有请求共享。并发场景
# （流式任务写 checkpoint + 断连兜底 aget_state + 刷新时 GET /messages 读
#  + list_sessions 读）会在同一条连接上抢用。原实现用单条 AsyncConnection，
# 并发复用直接抛 "another command is already in progress"
# （server.log 16:30:09 checkpointer.aget_tuple 失败 实锤），
# 进而导致已完成的会话刷新空白、进行中任务消息静默丢失。
# 改用连接池后每条操作各自拿独立连接，从根本上消除该碰撞。
POSTGRES_POOL_MAX_SIZE = 20


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


class ReconnectingAsyncPostgresStore:
    """AsyncPostgresStore 自动重连包装器。

    当 PostgreSQL 连接因网络中断、服务重启等原因断开时，
    自动关闭旧连接池并创建新实例，对调用方透明。
    支持方法：aget / aput（覆盖当前代码中的全部使用场景）。
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._store: Optional[AsyncPostgresStore] = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._cm: Any = None  # 保持 from_conn_string 生成器存活，防 GC 关闭连接池

    # ── 内部生命周期 ──

    async def _create_fresh(self) -> AsyncPostgresStore:
        """创建全新的 store 实例（连接池模式，schema 由公开的 setup() 统一处理）。

        传入 pool_config 使 from_conn_string 改用 AsyncConnectionPool 而非单条
        AsyncConnection，从根本上消除并发复用同一条连接导致的
        "another command is already in progress" 错误（2026-08-24 根因修复）。
        """
        cm = AsyncPostgresStore.from_conn_string(
            self._dsn, pool_config={"max_size": POSTGRES_POOL_MAX_SIZE}
        )
        store = await cm.__aenter__()
        self._cm = cm  # 保持上下文管理器（其拥有连接池）存活，防 GC 关闭连接池
        return store

    async def _ensure(self) -> AsyncPostgresStore:
        """惰性初始化或返回已有 store。"""
        if self._store is None and not self._closed:
            async with self._lock:
                if self._store is None:
                    self._store = await self._create_fresh()
        return self._store

    async def _reconnect(self) -> AsyncPostgresStore:
        """销毁旧连接池，重建新 store。"""
        old_cm = self._cm
        async with self._lock:
            self._store = None
            self._cm = None  # 释放旧生成器引用
            if old_cm is not None:
                try:
                    await old_cm.__aexit__(None, None, None)  # 关闭旧连接池
                except Exception as e:
                    logger.warning("关闭旧 PostgresStore 连接池异常（忽略）: %s", e)
            logger.warning("正在重建 PostgresStore 连接...")
            self._store = await self._create_fresh()
            logger.warning("PostgresStore 连接已重建")
        return self._store

    # ── 统一重试代理 ──

    async def _call(self, method: str, *args, **kwargs):
        store = await self._ensure()
        for attempt in range(2):
            try:
                return await getattr(store, method)(*args, **kwargs)
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                if attempt == 0:
                    logger.warning(
                        "%s 失败（%s），尝试重连后重试...", method, e,
                    )
                    store = await self._reconnect()
                    continue
                logger.error(
                    "%s 重试后仍失败（%s），放弃", method, e,
                )
                raise

    # ── 公开接口（与 AsyncPostgresStore 兼容） ──

    async def aget(self, namespace, key):
        return await self._call("aget", namespace, key)

    async def aput(self, namespace, key, value):
        return await self._call("aput", namespace, key, value)

    async def setup(self):
        store = await self._ensure()
        await store.setup()

    async def aclose(self):
        """关闭连接池，之后所有调用会抛出错误。"""
        self._closed = True
        old_cm = self._cm
        self._cm = None  # 释放生成器引用 → 触发 pool.__aexit__
        async with self._lock:
            if old_cm is not None:
                try:
                    await old_cm.__aexit__(None, None, None)  # 关闭连接池
                except Exception as e:
                    logger.warning("关闭 PostgresStore 连接池异常（忽略）: %s", e)
                logger.warning("PostgresStore 连接已主动关闭")
            self._store = None


class ReconnectingAsyncPostgresSaver(BaseCheckpointSaver):
    """AsyncPostgresSaver 自动重连包装器。

    同时继承 BaseCheckpointSaver 以满足 langgraph.compile() 的
    isinstance(checkpointer, BaseCheckpointSaver) 类型检查。

    与 ReconnectingAsyncPostgresStore 相同模式，包装 checkpointer。
    当 aget/aput/alist/aget_tuple 抛出 psycopg.OperationalError 时，
    自动重建连接池并重试一次。
    """

    def __init__(self, dsn: str):
        super().__init__()
        self._dsn = dsn
        self._cp: Optional[AsyncPostgresSaver] = None
        self._lock = asyncio.Lock()
        self._closed = False

    # ── 内部生命周期 ──

    async def _create_fresh(self) -> AsyncPostgresSaver:
        """创建全新的 checkpointer 实例（连接池模式）。

        AsyncPostgresSaver.from_conn_string 仅支持单条 AsyncConnection，不支持
        连接池，故此处手动创建 AsyncConnectionPool 并传入 conn=pool，与 store 端
        保持一致，消除并发 "another command is already in progress" 错误
        （2026-08-24 根因修复）。
        """
        pool = AsyncConnectionPool(
            self._dsn,
            min_size=1,
            max_size=POSTGRES_POOL_MAX_SIZE,
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        await pool.open()
        return AsyncPostgresSaver(conn=pool, serde=None)

    async def _ensure(self) -> AsyncPostgresSaver:
        if self._cp is None and not self._closed:
            async with self._lock:
                if self._cp is None:
                    self._cp = await self._create_fresh()
        return self._cp

    async def _reconnect(self) -> AsyncPostgresSaver:
        old = self._cp
        async with self._lock:
            self._cp = None
            if old is not None:
                try:
                    await old.conn.close()  # 关闭旧连接池
                except Exception as e:
                    logger.warning("关闭旧 PostgresSaver 连接池异常（忽略）: %s", e)
            logger.warning("正在重建 PostgresSaver 连接...")
            self._cp = await self._create_fresh()
            logger.warning("PostgresSaver 连接已重建")
        return self._cp

    # ── 统一重试代理 ──

    async def _call(self, method: str, *args, **kwargs):
        cp = await self._ensure()
        for attempt in range(2):
            try:
                return await getattr(cp, method)(*args, **kwargs)
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                if attempt == 0:
                    logger.warning(
                        "checkpointer.%s 失败（%s），尝试重连后重试...", method, e,
                    )
                    cp = await self._reconnect()
                    continue
                logger.error(
                    "checkpointer.%s 重试后仍失败（%s），放弃", method, e,
                )
                raise

    # ── 公开接口 ──

    @property
    def config_specs(self):
        """委托给内部 checkpointer 的配置规范。"""
        if self._cp is not None:
            return self._cp.config_specs
        return []

    async def aget_tuple(self, config):
        return await self._call("aget_tuple", config)

    async def aput(self, config, checkpoint, metadata, new_versions=None):
        return await self._call("aput", config, checkpoint, metadata, new_versions)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        return await self._call("alist", config, filter=filter, before=before, limit=limit)

    async def aget_next_version(self, task_id, checkpoint_ns):
        return await self._call("aget_next_version", task_id, checkpoint_ns)

    async def aget(self, config):
        return await self._call("aget", config)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await self._call("aput_writes", config, writes, task_id, task_path)

    async def setup(self):
        cp = await self._ensure()
        await cp.setup()

    async def aclose(self):
        self._closed = True
        old = self._cp
        self._cp = None
        async with self._lock:
            if old is not None:
                try:
                    await old.conn.close()  # 关闭连接池
                except Exception as e:
                    logger.warning("关闭 PostgresSaver 连接池异常（忽略）: %s", e)
                logger.warning("PostgresSaver 连接已主动关闭")

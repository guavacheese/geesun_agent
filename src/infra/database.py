import asyncio
import logging
from contextlib import asynccontextmanager
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


# ─── 稳定 keyset 分页（供会话列表使用，绕过 BaseStore.asearch） ───
#
# 为什么不能用 asearch 翻页（langgraph-checkpoint-postgres 3.1.0 源码实测）：
#   base.py:526-534 的非向量搜索 SQL 是
#     SELECT ... WHERE prefix LIKE %s ORDER BY store.updated_at DESC LIMIT %s OFFSET %s
#   ① ORDER BY 只有 updated_at 一列且**非唯一**（PK 是 (prefix,key)，不参与排序）
#      → 没有全序，OFFSET 翻页必然漏条/重复；
#   ② namespace 条件只生成 `prefix LIKE %s`（base.py:446-449），没有 `prefix = %s`
#      分支 → 越权过滤只能放应用层，而 OFFSET 的偏移量是**数据库端**算的
#      → 只要用 LIKE 前缀 + 应用层过滤，分页在结构上不可能正确；
#   ③ filter 的比较操作符（base.py:622-637）只作用于 value 字段的**文本**，
#      没有 key 列、没有元组、没有 OR → 表达不了 (updated_at, key) < (c1, c2)。
# 本模块改用：`prefix = %s` 精确相等 + 元组游标 + 全序排序，三条一次性消掉。
#
# 排序键取**业务字段** value->>'updated_at' 而不是框架的 store.updated_at：
# 后者每次 aput 都变（重命名/pin 也算，base.py:388-393），
# 且与 list_sessions 的业务排序（应用层按 value.updated_at 排）不一致。
#
# COLLATE "C" 把比较语义钉死为字节序——ISO-8601 UTC 时间串在字节序下等价于时间序
# （生产 agent_mem_prod 21/21 条实测），且不随库 collation（现为 en_US.utf8）漂移。
# COALESCE 兜住缺 updated_at 的脏条目，使其稳定排在末尾而不是 NULLS FIRST 抢头名。
_SORT_KEY_EXPR = "COALESCE(value->>'updated_at', '') COLLATE \"C\""

# 支撑上述查询的复合索引（prefix 放最前，同时服务等值匹配）。
# 不用 partial index：`prefix = 'sessions.x'` 无法被 planner 用
# `prefix LIKE 'sessions.%'` 的部分谓词证明蕴含。
# 生产实测：顺序扫描关闭后 EXPLAIN 为 `Index Scan using store_sessions_order_idx`，
# 且**没有 Sort 节点**（索引序即输出序，LIMIT 可提前收敛）。
_SESSIONS_ORDER_INDEX = "store_sessions_order_idx"
_SESSIONS_ORDER_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {_SESSIONS_ORDER_INDEX} "
    f"ON store (prefix, ({_SORT_KEY_EXPR}) DESC, key DESC)"
)


def _build_keyset_query(*, with_cursor: bool, pinned: Optional[bool]) -> str:
    """拼 keyset 查询。三个分支全是内部常量，无外部输入参与拼接。"""
    cursor_clause = f"AND ({_SORT_KEY_EXPR}, key) < (%s::text, %s::text)" if with_cursor else ""
    if pinned is True:
        pinned_clause = "AND value->>'pinned' = 'true'"
    elif pinned is False:
        pinned_clause = "AND value->>'pinned' IS DISTINCT FROM 'true'"
    else:
        pinned_clause = ""
    return f"""
        SELECT key, value
        FROM store
        WHERE prefix = %s
          {cursor_clause}
          {pinned_clause}
        ORDER BY {_SORT_KEY_EXPR} DESC, key DESC
        LIMIT %s
    """


@asynccontextmanager
async def _acquire(conn):
    """从 store 的 conn 取一条连接。

    `AsyncPostgresStore.conn` 是**公开属性**，类型为 AsyncConnection 或
    AsyncConnectionPool（aio.py:42/153 `self.conn = conn`）。本项目走
    `from_conn_string(pool_config=...)`，拿到的是 AsyncConnectionPool，
    用 psycopg_pool 的公开 `connection()` 借出连接。
    """
    if isinstance(conn, AsyncConnectionPool):
        async with conn.connection() as c:
            yield c
    else:
        yield conn


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
    支持方法：aget / aput / asearch（覆盖当前代码中的全部使用场景）。

    注意：`aput(namespace, key, None)` 在 langgraph 内部会被翻译成
    `DELETE FROM store WHERE prefix=... AND key=...`（见
    `langgraph/store/postgres/base.py` 的 `_prepare_batch_PUT_queries`），
    即 None 表示删除而非写入 null，因此本包装器无需额外暴露 adelete。
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

    async def _call_with(self, label: str, fn):
        """在（必要时重建的）store 上执行 fn(store)，失败时重连重试一次。

        `fn` 接收 store 实例并返回 awaitable——这样自写 SQL 的路径也能复用
        同一套重连语义（`_call` 只能转发 store 自身的同名方法）。
        """
        store = await self._ensure()
        for attempt in range(2):
            try:
                return await fn(store)
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                if attempt == 0:
                    logger.warning(
                        "%s 失败（%s），尝试重连后重试...", label, e,
                    )
                    store = await self._reconnect()
                    continue
                logger.error(
                    "%s 重试后仍失败（%s），放弃", label, e,
                )
                raise

    async def _call(self, method: str, *args, **kwargs):
        return await self._call_with(
            method, lambda s: getattr(s, method)(*args, **kwargs)
        )

    # ── 公开接口（与 AsyncPostgresStore 兼容） ──

    async def aget(self, namespace, key):
        return await self._call("aget", namespace, key)

    async def aput(self, namespace, key, value):
        return await self._call("aput", namespace, key, value)

    async def asearch(self, namespace, **kwargs):
        """按 namespace 前缀搜索条目（Postgres 侧走 `prefix LIKE 'ns%'`）。

        两个必须由调用方承担的约束：
        1. langgraph 的 limit 默认值是 10，会**静默截断**结果，调用方必须显式传 limit，
           并在需要全量时自行分页（offset）。
        2. 前缀匹配不认命名空间边界：`sessions.GY2442` 会同时匹配到 `sessions.GY24428`，
           因此调用方必须按 `Item.namespace` 做精确相等过滤，否则会串用户数据。

        ⚠️ **不要用它做分页**——排序键 `store.updated_at` 非唯一（无全序），
        且前缀 LIKE 与 OFFSET 的组合在结构上无法与越权过滤对齐。需要分页改用
        `asearch_keyset`（见文件顶部该段的说明）。本方法保留给「按前缀捞少量条目」
        这类不需要稳定序的场景。
        """
        return await self._call("asearch", namespace, **kwargs)

    async def asearch_keyset(
        self,
        prefix: str,
        *,
        limit: int,
        cursor: Optional[tuple[str, str]] = None,
        pinned: Optional[bool] = None,
    ) -> list[tuple[str, Any]]:
        """在 store 表上按**精确 prefix** 做稳定 keyset 分页，返回 [(key, value)]。

        参数：
          prefix  命名空间文本（`".".join(namespace)`），走 `prefix = %s` 精确匹配，
                  **不再有前缀越权问题**，调用方无需再做 namespace 相等过滤。
          limit   本次取回条数（必传；没有"默认 10"这种隐式行为）。
          cursor  上一页末条的 `(value->>'updated_at', key)`；None 表示取首页。
                  元组比较保证同 updated_at 的多条不会漏也不会重。
          pinned  仅筛选置顶/非置顶条目（`value->>'pinned'`）；None 表示不筛。

        排序：`(value->>'updated_at' COLLATE "C") DESC, key DESC` —— 全序（key 破平局）。
        `value` 由 psycopg 按 jsonb 自动解码成 dict，调用方不需要再反序列化。
        """
        if limit <= 0:
            raise ValueError(f"limit 必须为正整数，收到 {limit}")

        async def _run(store: AsyncPostgresStore):
            sql = _build_keyset_query(with_cursor=cursor is not None, pinned=pinned)
            params: list[Any] = [prefix]
            if cursor is not None:
                params.extend(cursor)
            params.append(limit)
            async with _acquire(store.conn) as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
            return [(r["key"], r["value"]) for r in rows]

        return await self._call_with("asearch_keyset", _run)

    async def setup(self):
        store = await self._ensure()
        await store.setup()
        await self._ensure_sessions_order_index(store)

    async def _ensure_sessions_order_index(self, store: AsyncPostgresStore) -> None:
        """建立会话列表排序用的复合索引（幂等）。

        这是**我们自己的 DDL**，挂在 langgraph 管理的 store 表上：ASearch 自带
        `store_prefix_idx(prefix text_pattern_ops)` 只服务前缀 LIKE，不服务
        `(updated_at, key)` 排序，所以必须自建。失败不阻断启动（索引只影响性能，
        不影响正确性），但必须打 error 让运维可见。
        """
        try:
            async with _acquire(store.conn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_SESSIONS_ORDER_INDEX_DDL)
            logger.warning("会话列表排序索引已就绪: %s", _SESSIONS_ORDER_INDEX)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "创建会话列表排序索引 %s 失败（仅影响性能，不影响正确性）: %s",
                _SESSIONS_ORDER_INDEX, e, exc_info=True,
            )

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

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional, Sequence
from urllib.parse import quote_plus

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.base import PutOp
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


# ─── messages.* 增量存储（一条消息一行，key = 零填充序号） ───
#
# 改增量的理由（生产实测，2026-09-12）：
#   原先每轮把**整份** history 当一个 value 覆盖写单个 key。最大会话 259 条 / 1.32 MB
#   挤在一行里，三个后果：
#     ① 单点故障爆炸半径 = 整个会话——这一行写失败（TOAST 上限 / jsonb 解析异常 /
#        序列化 OOM）就是整份历史全丢；
#     ② 写放大 130×——每轮重写整行，累计约 171 MB 只为最终存下 1.32 MB；
#     ③ 摘要裁剪会传导——SummarizationMiddleware 触发时 checkpoint 被
#        `RemoveMessage(REMOVE_ALL_MESSAGES)` 裁掉早期消息，下一轮按快照重建的
#        history 缩水，全量覆盖把这份缩水写进 store，早期对话在任何一层都找不回。
#   改增量后：① 爆炸半径缩到"一条"；② 写放大变 O(新增)；③ 按**序号**追加，
#   checkpoint 变短不影响已归档条目。
#
# key 用零填充序号，不用 msg.id：`chat.py` 取的是 `getattr(msg, "id", None)`
# （可能为 None，且 LangChain 的 message id 是 uuid、与顺序无关），且文本序下
# "10" < "9" 会错序，必须定长。序号**从 1 开始**，与 `history[i]` 的 `i+1` 对齐。
#
# 读取 `WHERE prefix = %s ORDER BY key` 正好命中 store 自带的
# `store_pkey (prefix, key)` btree → **零新建索引成本**（与会话列表必须自建
# `store_sessions_order_idx` 相反）。分页语义对齐 deer-flow
# `runtime/events/store/base.py:102` 的 `list_messages(before_seq=...)`：
# 无游标取最新 limit 条，有游标取 key < before_key 的最新 limit 条，返回时统一升序。
_MESSAGE_KEY_WIDTH = 8


def message_key(index: int) -> str:
    """第 index 条消息（**从 1 开始**）的 store key。"""
    if index < 1:
        raise ValueError(f"消息序号从 1 开始，收到 {index}")
    return f"{index:0{_MESSAGE_KEY_WIDTH}d}"


def message_key_to_index(key: str) -> Optional[int]:
    """`message_key` 的逆：解析失败（脏数据 / 旧格式）返回 None。"""
    if not key or not key.isdigit():
        return None
    return int(key)


def _build_message_tail_query(*, with_cursor: bool) -> str:
    """取"最新"一页：`ORDER BY key DESC`（走 PK），调用方负责 reverse 回升序。"""
    cursor_clause = "AND key < %s" if with_cursor else ""
    return f"""
        SELECT key, value
        FROM store
        WHERE prefix = %s
          {cursor_clause}
        ORDER BY key DESC
        LIMIT %s
    """


# 单次读取消息条数（`persist_message_page_size` 覆盖）。生产实测最大会话 259 条，
# 1000 有 3.9× 余量 → 绝大多数会话行为与改造前"全量返回"完全一致，仅在超长会话
# 上退化为"最新 1000 条 + 更早页游标"，避免一次拉出上兆 payload。
_MESSAGE_PAGE_DEFAULT = 1000

# "取全"的上限（`alist_all_messages`）。仅用于 `continue_from_state` 这类必须重建
# **整段**图状态的场景；生产实测最大会话 259 条 → 10000 有 38× 余量。
_MESSAGE_ALL_MAX = 10_000


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

    # ── messages.* 增量存储 ──

    async def abatch(self, ops):
        """批量操作转发（走重连重试）。

        用于消息增量写入——复用框架的 `abatch` 而不是自写 upsert SQL：
        框架在 `store/postgres/base.py` 里对 PutOp 的处理已经兼顾
        「冲突时保留原 created_at、只更新 value/updated_at」等语义，
        自写 INSERT ... ON CONFLICT 容易漏掉细节。
        另外 `PutOp(value=None)` 会被编译成 DELETE（`base.py:325`），
        所以「截断」「删指定消息」也能走同一条路径，一次往返完成。
        """
        return await self._call("abatch", ops)

    async def aget_message_watermark(self, prefix: str) -> tuple[int, Optional[str], int]:
        """增量写入的水印：`(已存最大序号, 该条目的 id, 旧格式残留条数)`。

        序号走 `store_pkey (prefix, key)` btree，`ORDER BY key DESC LIMIT 1` 是 O(log n)。

        为什么要连 id 一起返回（2026-09-12 设计要点）：
        增量追加的朴素前提是"history 只在尾部增长"，但 `SummarizationMiddleware` 触发时
        会返回 `[RemoveMessage(REMOVE_ALL_MESSAGES), 摘要, *保留的尾部]` —— **history 会变短**。
        此时按"长度"定位增量起点会错位：压缩后 history 里第 10 条其实是 store 里第 250 条，
        按 history 下标算 key 会**覆盖已归档条目**（把历史改写掉，正是要避免的）。
        带上 id 就能在 history 里按内容定位"最后一条已存条目"，从它之后开始追加，
        且序号继续用**store 的**序号（不是 history 下标）—— 两头都不重叠。

        第三个返回值 `legacy_rows` 是**部署顺序防线**：若 store 里还有旧格式条目
        （单行 `key="messages"`，部署新版后、迁移脚本跑之前会短暂存在），则
          · 它文本序排在数字之后（'0' < 'm'），会污染水位查询 → 必须用 `key ~ '^[0-9]+$'` 排除；
          · 此时**调用方不得写入**：写进去会让新旧格式并存，之后迁移脚本的前置守卫会拒绝
            执行，留下需要人工对账的中间态。调用方应跳过本轮写入并打 error。
        可见 langgraph 层面的 `asearch` 风格"前缀 LIKE"在这里同样不认边界，精确 `prefix =` + 正则才是对的。

        id 可能为 None（老数据 / 部分构造路径），此时调用方退化为按长度判断。
        """
        async def _run(store: AsyncPostgresStore):
            async with _acquire(store.conn) as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    # 三个标量子查询合并成一次往返（各自都是单前缀上的索引查找）：
                    # 即使没有任何数字 key，这条 SQL 也一定返回 1 行（不会让调用方见到 None 行）
                    await cur.execute(
                        "SELECT "
                        "(SELECT count(*) FROM store WHERE prefix = %s AND key !~ '^[0-9]+$') "
                        "  AS legacy_rows, "
                        "(SELECT key FROM store WHERE prefix = %s AND key ~ '^[0-9]+$' "
                        "  ORDER BY key DESC LIMIT 1) AS top_key, "
                        "(SELECT value FROM store WHERE prefix = %s AND key ~ '^[0-9]+$' "
                        "  ORDER BY key DESC LIMIT 1) AS top_value",
                        (prefix, prefix, prefix),
                    )
                    row = await cur.fetchone()
            return row

        row = await self._call_with("aget_message_watermark", _run)
        if not row:
            return 0, None, 0
        legacy_rows = int(row.get("legacy_rows") or 0)
        if row.get("top_key") is None:
            return 0, None, legacy_rows
        idx = message_key_to_index(row["top_key"])
        if idx is None:
            # 理论上不会到（SQL 已用正则过滤），留作防御：返回 0 让调用方走按长度兜底
            logger.error(
                "会话 %s 的水位 key=%r 无法解析为序号", prefix, row["top_key"],
            )
            return 0, None, legacy_rows
        value = row["top_value"]
        last_id = value.get("id") if isinstance(value, dict) else None
        return idx, last_id, legacy_rows

    async def alist_messages(
        self,
        prefix: str,
        *,
        limit: int = _MESSAGE_PAGE_DEFAULT,
        before_key: Optional[str] = None,
    ) -> tuple[list[tuple[str, Any]], bool]:
        """按 key **升序**返回一页消息，附 `has_more`（是否还有更早的）。

        语义对齐 deer-flow `list_messages(before_seq=...)`：无游标取**最新** limit 条；
        带 `before_key` 取 key < before_key 的最新 limit 条。内部 `ORDER BY key DESC`
        借 PK 序直接取尾部（不必先排序全表），返回前 reverse 回升序（阅读顺序）。

        `has_more` 由"本页是否取满"推断——多取一条的代价大于收益（这条 SQL 已是
        索引上的 LIMIT 扫描），取满即认为可能还有，前端据此决定是否显示"加载更早"。
        """
        if limit <= 0:
            raise ValueError(f"limit 必须为正整数，收到 {limit}")

        async def _run(store: AsyncPostgresStore):
            sql = _build_message_tail_query(with_cursor=before_key is not None)
            params: list[Any] = [prefix]
            if before_key is not None:
                params.append(before_key)
            params.append(limit)
            async with _acquire(store.conn) as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
            rows.reverse()  # DESC 取尾 → 升序返回
            return [(r["key"], r["value"]) for r in rows]

        rows = await self._call_with("alist_messages", _run)
        return rows, len(rows) >= limit

    async def alist_all_messages(
        self, prefix: str, *, max_items: int = _MESSAGE_ALL_MAX,
    ) -> list[tuple[str, Any]]:
        """按序取回该会话的**全部**消息（升序），用于重建整段图状态。

        与会话列表的场景差异：`continue_from_state` 要把 store 里的历史灌回图，
        只取"最新一页"会**静默把图状态截短**（用户点"编辑后重发"会丢掉早期上下文）。
        所以这里取全，但仍有硬上限：超过 `max_items` 时打 error 并按序返回前 N 条
        （不是丢弃尾部——返回的是**最早的** N 条，让调用方看到从头发起的历史）。

        为什么不做逐页循环：单次 `ORDER BY key DESC LIMIT N` 走 PK 就是一次索引扫描，
        页循环只是把同一次扫描拆成多次往返。生产实测最大会话 259 条，上限 10000 有 38× 余量。
        """
        rows, has_more = await self.alist_messages(prefix, limit=max_items)
        if has_more:
            logger.error(
                "会话 %s 消息数超过取全上限 %d，本次只返回最早的 %d 条"
                "（重建图状态可能不完整）",
                prefix, max_items, len(rows),
            )
        return rows

    async def adelete_prefix(self, prefix: str) -> int:
        """删除该 prefix 下**所有**条目，返回删除行数。

        用来删整个会话的消息。比"先读全量再逐条置 None"少一次往返，也不会因为
        读取时的分页上限而漏删（旧实现若按页读再删，超出单页的部分会残留）。
        调用方须保证 prefix 是完整的 `".".join(namespace)`，否则会误删。
        """
        if not prefix:
            raise ValueError("adelete_prefix 拒绝空 prefix（会删全表）")

        async def _run(store: AsyncPostgresStore):
            async with _acquire(store.conn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM store WHERE prefix = %s", (prefix,))
                    return cur.rowcount or 0

        return await self._call_with("adelete_prefix", _run)

    async def adelete_session_atomic(
        self,
        session_prefix: str,
        session_key: str,
        messages_prefix: str,
        thread_id: str,
    ) -> dict[str, int]:
        """单事务原子删除一个会话在数据库里的**全部**痕迹（2026-09-12 方案 A）。

        store 与 checkpointer 指向同一个库（agent_mem_prod），因此 5 处删除可以
        放进同一个事务，要么全删要么全不删：

          1. store.sessions.{user} 的会话元数据行（prefix + key 精确删）
          2. store.messages.{user}.{sid} 的消息台账（prefix 全删，走 store_pkey）
          3. checkpoints / checkpoint_blobs / checkpoint_writes（按 thread_id，
             thread_id 格式 = "{user_id}:{session_id}"，含全部 checkpoint_ns）

        改前：会话行 aput(None)、消息 adelete_prefix 各自独立连接，checkpointer
        三表**根本没删**（单会话残留 171+46+217 行，checkpoint_blobs 还存着
        channel 大对象）；中途失败 = 半删除状态（行没了消息还在，或反之）。

        checkpointer 三表无外键（langgraph base.py MIGRATIONS 纯 PK 表，生产
        pg_constraint 实测一致），DELETE 顺序无关；子表在前只是习惯。
        磁盘文件（uploads/reports）不进事务（磁盘无法参与 DB 事务），由调用方
        在事务**之前**尽力删除。

        返回各表实际删除行数（诊断/验收用）。
        """
        if not session_prefix or not messages_prefix or not session_key:
            raise ValueError("adelete_session_atomic 拒绝空 prefix/key（会误删全表）")
        if not thread_id or ":" not in thread_id:
            raise ValueError(f"adelete_session_atomic 非法 thread_id: {thread_id!r}")

        async def _run(store: AsyncPostgresStore) -> dict[str, int]:
            counts: dict[str, int] = {}
            async with _acquire(store.conn) as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "DELETE FROM store WHERE prefix = %s AND key = %s",
                            (session_prefix, session_key),
                        )
                        counts["session_row"] = cur.rowcount or 0
                        await cur.execute(
                            "DELETE FROM store WHERE prefix = %s",
                            (messages_prefix,),
                        )
                        counts["message_rows"] = cur.rowcount or 0
                        # 表名来自固定元组字面量（非用户输入），无注入面
                        for _table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                            await cur.execute(
                                f"DELETE FROM {_table} WHERE thread_id = %s",
                                (thread_id,),
                            )
                            counts[_table] = cur.rowcount or 0
            return counts

        return await self._call_with("adelete_session_atomic", _run)

    async def awrite_messages(
        self,
        namespace: tuple,
        items: list[tuple[str, Any]],
        *,
        delete_keys: Sequence[str] = (),
    ) -> None:
        """一次往返完成"写若干条 + 删若干条"（key, value）。

        用一个 `abatch` 而非两次调用，是因为 `edit` 截断要"改写第 N 条 + 删除其后全部"
        ——分两次会留下中间态（改写已生效、尾部尚未删），此时若进程中断，会话会变成
        "新内容 + 旧后续"，比截断失败更糟。合并在一个 batch 里由框架一次提交。

        用框架 `abatch` + `PutOp` 而非自写 SQL：框架对 PutOp 的处理已兼顾
        「冲突时保留原 created_at、只更新 value/updated_at」等语义，自写
        INSERT ... ON CONFLICT 容易漏细节；且 `PutOp(value=None)` 会编译成 DELETE
        （`store/postgres/base.py:325`），删除走同一条路径。
        """
        if not items and not delete_keys:
            return
        ops: list[PutOp] = [PutOp(namespace, key, value) for key, value in items]
        ops.extend(PutOp(namespace, key, None) for key in delete_keys)
        await self.abatch(ops)

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

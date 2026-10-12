"""run 实体登记表（P1-a，2026-10-12）。

## 与 `turn_registry.py` 的分工（两者都要，不能互相替代）

| | `turn_registry` | 本模块 |
|---|---|---|
| 回答的问题 | "这个会话此刻有轮次在跑吗"（**删除会话护栏**） | "这个 run 是什么、到哪一步了、能不能续订" |
| 生命周期 | 随 SSE 连接（acquire → beat → release） | **不随连接**：run 起于 worker 启动、止于 worker 收尾 |
| 持久化 | 无（纯进程内） | 状态快照落 store，供刷新后查询与进程重启后校正 |
| 陈旧判定 | 心跳超时（`STALE_MS`） | 进程 boot 戳（见下"孤儿 run"） |

P1 之后 run 不再随 SSE 结束，所以 `turn_registry.release()` 的时机必须跟着搬到
worker 收尾（否则删除护栏提前放行）——这是 P1-b 的事，本模块只提供 run 概念。

## 存储布局与一个刻意的取舍

行：namespace `("runs", user_id)`，**key = session_id**。

为什么不 key = run_id：新端点 `GET /sessions/{sid}/active-run` 是刷新后的第一条查询，
按 session 查必须是 O(1)。而同一会话同时只允许一个 run（多任务策略 = reject，
对齐 deer-flow `multitask_strategy` 默认值），所以"每会话一行、覆盖写"正好，
也天然表达了"这个会话最新的 run 是谁"。run_id 记在行里，续订端点据此工作。

## 孤儿 run（进程被杀留下的 `running`）

worker 随进程消失，但 store 里 status 还是 `running` → 前端永远等一个不存在的 run。
对策：每行记写入时的**进程 boot 戳**（`boot_id`），读的时候发现
`status ∈ 活跃态 且 boot_id != 本进程` ⇒ 该 run 属于上一个进程，已随它消失，
**读到即校正**为 `failed(orphaned)` 并返回校正后的状态。

为什么不做 deer-flow 式的启动期全量扫描（`manager.py:2189 _reconcile_orphans`）：
本模块的每个可见面（active-run 查询、续订、取消）都必经这里的读路径，惰性校正
覆盖面等价，且不需要在启动时扫全表（也就不用为它引入一次可能拖慢启动的 IO）。
代价是"没人查的行"会一直停在 running——它不影响任何界面与行为，可接受。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ─────────────────────────── 状态机 ───────────────────────────

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_INTERRUPTED = "interrupted"
STATUS_FAILED = "failed"

#: 活跃态：这两个状态下"该 run 可能还在推进"，也是重复提交要拒绝的依据
ACTIVE_STATUSES: frozenset[str] = frozenset({STATUS_PENDING, STATUS_RUNNING})
#: 终态：不会再变（除非进程重启被校正为 orphaned）
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_COMPLETED, STATUS_INTERRUPTED, STATUS_FAILED}
)

#: 进程启动戳：进程内唯一、跨重启必变。写入行、读时比对，用于识别孤儿 run。
#: 用 uuid 而非 pid：容器重启后 pid 可能复用（1 号进程尤其）。
BOOT_ID = uuid.uuid4().hex[:12]


def _now_ms() -> int:
    return int(time.time() * 1000)


def new_run_id() -> str:
    """run_id：短 uuid（进 URL 与 SSE 的 `id:` 前缀，不宜过长）。"""
    return uuid.uuid4().hex[:16]


# ─────────────────────────── 记录 ───────────────────────────


@dataclass
class RunRecord:
    """一个 run 的进程内态。不含 bridge（桥按 run_id 索引，由调用方持有）。"""

    run_id: str
    thread_id: str
    user_id: str
    session_id: str
    status: str = STATUS_PENDING
    created_at: int = field(default_factory=_now_ms)
    started_at: int | None = None
    ended_at: int | None = None
    error: str | None = None
    boot_id: str = BOOT_ID
    #: worker 的 asyncio.Task 句柄（取消用）。不参与落盘。
    task: asyncio.Task[Any] | None = field(default=None, repr=False, compare=False)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def to_row(self) -> dict[str, Any]:
        """落 store 的状态快照（不含 task 等进程内对象）。"""
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "boot_id": self.boot_id,
            "updated_at": _now_ms(),
        }


# ─────────────────────────── 进程内表 ───────────────────────────

#: run_id -> 记录（活跃 run 的权威来源：bridge/task 本来就只在进程内）
_runs: dict[str, RunRecord] = {}
#: thread_id -> run_id（重复提交拒绝用）
_by_thread: dict[str, str] = {}


def namespace(user_id: str) -> tuple[str, str]:
    return ("runs", user_id)


def create(
    *,
    user_id: str,
    session_id: str,
    thread_id: str,
    run_id: str | None = None,
) -> RunRecord:
    """创建 run（进程内）。落 store 由 :func:`persist` 负责（需要 await）。"""
    rec = RunRecord(
        run_id=run_id or new_run_id(),
        thread_id=thread_id,
        user_id=user_id,
        session_id=session_id,
    )
    _runs[rec.run_id] = rec
    _by_thread[thread_id] = rec.run_id
    return rec


def get(run_id: str) -> RunRecord | None:
    return _runs.get(run_id)


def get_by_thread(thread_id: str) -> RunRecord | None:
    run_id = _by_thread.get(thread_id)
    return _runs.get(run_id) if run_id else None


def active_for_thread(thread_id: str) -> RunRecord | None:
    """该会话是否有**活跃** run（D2：重复提交 → 409 拒绝）。"""
    rec = get_by_thread(thread_id)
    return rec if rec is not None and rec.is_active else None


def attach_task(run_id: str, task: asyncio.Task[Any]) -> None:
    rec = _runs.get(run_id)
    if rec is not None:
        rec.task = task


def drop(run_id: str) -> None:
    """从进程内表移除（run 收尾后）。store 行**保留**——它是"上一轮结局"的查询来源。"""
    rec = _runs.pop(run_id, None)
    if rec is not None and _by_thread.get(rec.thread_id) == run_id:
        _by_thread.pop(rec.thread_id, None)


def reset_for_tests() -> None:  # pragma: no cover - 仅测试用
    _runs.clear()
    _by_thread.clear()


# ─────────────────────────── 持久化 ───────────────────────────


async def persist(store: Any, rec: RunRecord) -> bool:
    """把 run 状态快照写入 store。

    失败**不抛出**：状态快照是给"刷新后查询/重启后校正"用的旁路信息，写不进去
    不该把正在跑的任务打断；但必须留 ERROR（否则刷新后永远显示不出真相）。
    返回是否写入成功，便于调用方在需要时感知。
    """
    try:
        await store.aput(namespace(rec.user_id), rec.session_id, rec.to_row())
        return True
    except Exception as e:
        logger.error(
            "run 状态快照写入失败（刷新后可能查不到本轮状态）: run=%s status=%s err=%s",
            rec.run_id,
            rec.status,
            e,
            exc_info=True,
        )
        return False


async def mark(
    store: Any,
    run_id: str,
    status: str,
    *,
    error: str | None = None,
) -> RunRecord | None:
    """推进 run 状态（进程内 + store）。状态非法则抛 ValueError（早暴露写错的状态名）。"""
    if status not in ACTIVE_STATUSES | TERMINAL_STATUSES:
        raise ValueError(f"非法 run 状态 {status!r}")
    rec = _runs.get(run_id)
    if rec is None:
        logger.warning("mark 未知 run（可能已被 drop）: run=%s status=%s", run_id, status)
        return None
    rec.status = status
    if error is not None:
        rec.error = error
    now = _now_ms()
    if status == STATUS_RUNNING and rec.started_at is None:
        rec.started_at = now
    if status in TERMINAL_STATUSES and rec.ended_at is None:
        rec.ended_at = now
    await persist(store, rec)
    return rec


async def _load_row(
    store: Any, *, user_id: str, session_id: str
) -> dict[str, Any] | None:
    """读某会话的 run 行的**唯一入口**（两个公开读函数都走它，保证校正行为一致）。

    职责：取值 → 惰性孤儿校正 → **有校正就写回**。
    写回是必须的：不写回的话，同一个孤儿行会在每次读时被反复"发现"（每次都打
    WARNING），而 store 里始终躺着一条 `running` —— 前端每次刷新都要重走一遍这条路。
    """
    row = await store.aget(namespace(user_id), session_id)
    if row is None:
        return None
    value = getattr(row, "value", row)
    if not isinstance(value, dict):
        return None

    fixed = _reconcile_orphan(user_id=user_id, session_id=session_id, row=value)
    if fixed is not value:
        try:
            await store.aput(namespace(user_id), session_id, fixed)
        except Exception as e:  # 校正写回失败不该影响读取本身
            logger.error("孤儿 run 校正写回失败: %s", e, exc_info=True)
    return fixed


async def fetch_row(store: Any, *, user_id: str, session_id: str) -> dict[str, Any] | None:
    """读某会话最新的 run 行（**含终态**；惰性孤儿校正，见模块文档）。

    与 :func:`select_active_run` 的分工：本函数回答"这一轮的结局是什么"（刷新后要能
    显示"已中断"/"已完成"），后者只回答"还有在跑的吗"。
    """
    return await _load_row(store, user_id=user_id, session_id=session_id)


def _is_orphan(row: dict[str, Any]) -> bool:
    """活跃态 且 boot 戳**明确**来自别的进程。

    注意 `boot_id` 缺失（`None`）时**不**判孤儿：这是"无从判断"，不是"确定不是本进程"。
    宁可让前端多等一轮，也不要把同进程里真在跑的 run 误杀成 failed —— 后者会让用户
    彻底丢掉续订目标。正常路径下 `to_row()` 恒带 boot_id，所以这条分支只在异常写入
    或跨版本滚动发布时出现。
    """
    return (
        row.get("status") in ACTIVE_STATUSES
        and row.get("boot_id") not in (None, BOOT_ID)
    )


def _reconcile_orphan(
    *,
    user_id: str,
    session_id: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    """孤儿 run 校正：属于上一个进程的活跃行 → 标 failed(orphaned)。

    纯函数（不 await、不碰 store）：只是把行改成终态并返回**新 dict**；没校正时
    原样返回**同一个对象**——调用方靠 `fixed is not row` 判断"是否需要写回"。
    """
    if not _is_orphan(row):
        return row
    fixed = dict(row)
    fixed["status"] = STATUS_FAILED
    fixed["error"] = "orphaned: 进程已重启，该 run 不再存活"
    fixed["ended_at"] = row.get("ended_at") or _now_ms()
    fixed["updated_at"] = _now_ms()
    fixed["boot_id"] = BOOT_ID
    logger.warning(
        "发现孤儿 run 并校正为 failed: user=%s session=%s run=%s（原 boot=%s）",
        user_id,
        session_id,
        row.get("run_id"),
        row.get("boot_id"),
    )
    return fixed


async def select_active_run(
    store: Any, *, user_id: str, session_id: str
) -> dict[str, Any] | None:
    """刷新后问"这个会话有在跑的 run 吗"。返回**活跃态**的行，否则 None。

    孤儿行会被校正并写回（否则前端会永远等一个不存在的 run）；非活跃行**不返回**
    但也不清掉——它承载"上一轮结局"（刷新后仍可显示"已中断"）。
    """
    row = await _load_row(store, user_id=user_id, session_id=session_id)
    if row is None:
        return None
    return row if row.get("status") in ACTIVE_STATUSES else None

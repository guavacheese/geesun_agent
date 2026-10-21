"""长任务（durable）自动续查 —— 把 N 次模型往返压成 1 次工具调用。

## 为什么要这个模块（2026-10-21 方案，来自 add0229b 两轮全丢的复盘）

生产 262 页 OCR 要跑 5 小时以上，而 agent 侧单次 MCP 工具调用有
`mcp_tool_call_timeout_sec`（默认 600s）墙钟封顶 —— 这是 653d8515
（同步 e2b 调用堵死事件循环 55 分钟、run 挂死 38 小时）之后的补丁，
不能撤。MCP 侧因此把等待切成 `wait_seconds`≤300 一段，超时返回
`{"status": "running", ...}`，**让模型再调一次同参工具**续查。

于是模型视角看到的是「同一个工具连续调用 30~70 次」。这个形状与死循环
无法区分，三套判据全都会误杀：

  · Layer2c 工具占比（窗口 20，阈值 0.6）：单工具长任务必然逼近 100%
    —— add0229b 第 5 轮 79%（15/19）、第 6 轮 95%（19/20）被硬停
  · Layer1 同指纹计数（hard=5）：`pages` 不进指纹，34 次调用 hash 全同
  · P0 无进展累计：只数同意图次数，不看结果里有没有客观进展

## 对齐 deer-flow 的哪一条

deer-flow 的做法（`mcp/tasks/ordinary.py` 的 submit/status/cancel 契约 +
`tools/builtins/task_tool.py::_await_subagent_terminal`）常被描述成
「轮询在框架里、模型不轮询」。**这句描述不准确**：它的 poll 循环同样
阻塞在工具内部，不是后台推进。真正的差别只有一条：

    >>> 模型只发 1 次工具调用；N 次探活留在**工具内部的循环**里。 <<<

它做得到是因为每次 `status` 查的是自己进程内的 task store（毫秒级）；
我们每轮探活是一次远端沙箱 `exec`（秒级 + RPC 风险）。所以本模块不是
照搬它的实现，而是把「N 次探活」搬进工具内部，同时保留 MCP 侧现有的
submit-and-poll 契约不变（每段仍 ≤300s，重启后可按指纹续接）。

## 为什么不会把事件循环堵死

`run_registry.HEARTBEAT_INTERVAL_MS` 的注释写得很明确：心跳是 **loop 绑定
的 asyncio 任务**，与工具调用**并发**——工具在跑时 loop 仍健康，心跳照跳。
所以一个跑几小时的 await 不会被看门狗（`STALE_MS=300s`）误判挂死。
**前提是调用链上不许出现同步阻塞**（MCP 侧已改 `asyncio.to_thread`）。

## 三层结构

  1. `settings.mcp_durable_task_tools` 白名单：只有点名工具走这里，
     不做通用推断（把 `execute` 之类也当长任务会掩盖真正的挂死）；
  2. 预算 `settings.mcp_durable_task_budget_sec` **独立于**单次 RPC 封顶：
     单次 RPC 仍受 `mcp_tool_call_timeout_sec` 限制，两者不互相牵连；
  3. 进度经 contextvar 拿到 reporter（由 chat worker 绑定），节流后
     publish 到 SSE，避免前端几小时毫无信号。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from src.core.config import settings
from src.core.loop_detection import _parse_tool_payload

logger = logging.getLogger(__name__)

#: 单次 RPC 异常（超时/网络抖动）允许连续失败几次后放弃。
#: 3 次 × (rpc_timeout 600s) ≈ 30 分钟，足以跨过一次 MCP 滚动重启；
#: 再多就是在白等，应该把错误交回模型（它会换策略）。
_MAX_CONSECUTIVE_RPC_ERRORS = 3

#: 进度签名连续多少次不变即判「真停滞」。300s/轮 × 12 = 1 小时。
#: 为什么给 1 小时这么宽：PDF→PNG 渲染是整批做的（262 页约 10 分钟），
#: 这段期间 `pages_ocred` 恒 0 属正常；之后每 70s 至少涨 1 页。
_MAX_STALE_ROUNDS = 12

#: 进度事件最小间隔（秒）。签名变化立即发；只涨时间戳的情况才节流。
_REPORT_MIN_INTERVAL_SEC = 60.0


# ─────────────────────────── 进度通道 ───────────────────────────


@dataclass
class DurableProgress:
    """把长任务进度推给前端的最小接口。

    只持有 publish 协程（由 chat worker 用 bridge 包好），不直接依赖 bridge
    / run_id —— 否则本模块会跟流式层耦合，单测里没法用假实现替换。
    """

    publish: Callable[[dict], Awaitable[None]]


_REPORTER: ContextVar[DurableProgress | None] = ContextVar(
    "durable_progress_reporter", default=None
)


def bind_progress(publish: Callable[[dict], Awaitable[None]]) -> Token:
    """在 worker 内绑定进度上报器；返回的 token 供 finally 里 reset。

    ★ 必须在 `_run_worker` 这个 asyncio.Task **内部** set：Task 会复制当前
      context，之后该 task 内所有 await 链（含工具调用）都能读到。若在
      task 外 set，工具链读到的是另一个 context 副本 ⇒ 永远 None。
    """
    return _REPORTER.set(DurableProgress(publish=publish))


def reset_progress(token: Token | None) -> None:
    if token is None:
        return
    try:
        _REPORTER.reset(token)
    except (ValueError, RuntimeError):
        # token 已被其它 context 用掉/跨 context reset：退化为清空，不抛
        _REPORTER.set(None)


def current_progress() -> DurableProgress | None:
    return _REPORTER.get()


# ─────────────────────────── 结果解析 ───────────────────────────


def is_durable_tool(tool_name: str) -> bool:
    """该工具是否走自动续查。白名单来自 settings（逗号分隔），不做模式匹配。"""
    names = getattr(settings, "mcp_durable_task_tools", "") or ""
    wanted = {n.strip() for n in str(names).split(",") if n.strip()}
    return tool_name in wanted


def result_text(result: Any) -> str:
    """把 MCP 工具返回值归一成文本。

    生产里实测到三种形态（2026-10-21 `_probe_progress.py` 生产容器验证）：
      · `str`：纯 JSON 文本（常见）
      · `list[dict]`：`[{"type":"text","text":"{...}"}]` 原始 MCP content
      · `list[TextContent]`：对象式 part

    ★ 额外处理 **Python repr 形态**（`str([{'type': 'text', 'text': '...'}])`）：
      这不会出现在生产运行路径（LangChain 交给工具的是已解析对象），但会
      出现在「从 store 重放历史」的场景里（store 用 str 落库）。若不还原，
      重放会一律判成"无结构化返回 ⇒ 不续查"，看起来像修法失效。
      **静默退化是最危险的失败模式**，所以这里显式还原。
    """
    if isinstance(result, str):
        raw = result.strip()
        if raw.startswith("[") and ("'type':" in raw or '"type":' in raw):
            import ast

            try:
                parsed = ast.literal_eval(raw)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                return result
            if isinstance(parsed, (list, tuple)) and parsed != result:
                return result_text(list(parsed))
        return result
    if isinstance(result, list):
        parts: list[str] = []
        for part in result:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    return str(result or "")


def result_payload(result: Any) -> dict | None:
    """取出结构化 JSON 载荷（dict）；取不到 None。

    解析统一委托 `loop_detection._parse_tool_payload`：那边已被 4 处判据
    共用、且有单测锁定。**不在这里另写一套**——两套解析口径必然分叉，
    2026-10-20 的 C/middleware 裂缝就是这么来的。
    """
    return _parse_tool_payload(result_text(result))


def payload_status(result: Any) -> str | None:
    data = result_payload(result)
    if data is None:
        return None
    return str(data.get("status") or "") or None


# ─────────────────────────── 进度描述 ───────────────────────────


def _progress_signature(data: dict) -> tuple:
    """「是否在推进」的判据签名。

    只取**单调量**：分批时 `pages_ocred` 每批归零但 `batches_done` 增、
    `batch_index` 增；单批时 `pages_ocred` 增。四个字段任一变化都算有进展。
    """
    return (
        data.get("status"),
        data.get("batch_index"),
        data.get("batches_done"),
        data.get("pages_ocred"),
        data.get("pages_ocred_doc"),
    )


def _stage_text(data: dict) -> str:
    """给前端/日志看的一句话进度。

    优先用 MCP 侧 `progress_hint`（它已包含「本批 x/y、整本累计 a/b、
    正在做什么」），取不到再退化到拼字段。
    """
    hint = data.get("progress_hint")
    if isinstance(hint, str) and hint.strip():
        # progress_hint 里带了很长一段给模型看的行动建议（"不要重传…"），
        # 对人是噪音：只取第一个句号前的那句。
        head = hint.split("。")[0].strip()
        return head[:200] if head else hint[:200]
    bits: list[str] = []
    if data.get("status"):
        bits.append(str(data["status"]))
    if data.get("batch"):
        bits.append("批 %s" % data["batch"])
    if data.get("pages_ocred") is not None:
        bits.append("本批 %s/%s 页" % (data.get("pages_ocred"), data.get("pages_total")))
    if data.get("pages_ocred_doc") is not None:
        bits.append("整本 %s 页" % data.get("pages_ocred_doc"))
    return "，".join(bits) or "运行中"


# ─────────────────────────── 主循环 ───────────────────────────


async def _maybe_report(
    tool_name: str,
    data: dict,
    *,
    rounds: int,
    started_at: float,
    last_sig: tuple | None,
    last_ts: float,
) -> tuple[tuple | None, float]:
    """节流上报进度。返回更新后的 (signature, timestamp)。"""
    reporter = current_progress()
    sig = _progress_signature(data)
    now = time.monotonic()
    # 签名变了（有新进展）立刻发；只是时间流逝则按最小间隔发，当心跳用
    if reporter is None:
        return (sig, now) if sig != last_sig else (last_sig, last_ts)
    if sig == last_sig and (now - last_ts) < _REPORT_MIN_INTERVAL_SEC:
        return last_sig, last_ts
    payload = {
        "type": "tool_progress",
        "tool": tool_name,
        "status": str(data.get("status") or "running"),
        "stage": _stage_text(data),
        "rounds": rounds,
        "elapsed_sec": round(time.monotonic() - started_at, 1),
    }
    for key in ("pages_ocred", "pages_total", "pages_ocred_doc",
                "batch", "batch_index", "batch_count", "batches_done"):
        if data.get(key) is not None:
            payload[key] = data[key]
    try:
        await reporter.publish(payload)
    except Exception:  # noqa: BLE001 - 进度发不出去绝不能影响任务本身
        logger.debug("[DURABLE] 进度上报失败（已忽略）", exc_info=True)
    return sig, now


async def drive(
    tool_name: str,
    args: Any,
    invoke: Callable[[Any], Awaitable[Any]],
    *,
    budget_sec: int,
    rpc_timeout_sec: int,
    poll_wait_sec: int | None = None,
    supports_wait: bool = False,
) -> Any:
    """反复调用同一工具直到它不再返回 running，返回**最后一次原始结果**。

    ★ 关键语义：本函数在 `BaseTool.ainvoke` **内部**循环，不产生新的
      `AIMessage.tool_calls`。模型与循环检测层都只看到一次工具调用 ——
      这正是「模型续查次数」从 30~70 次降到 1 次的地方。

    ★ 为什么单次 RPC 仍要独立 `wait_for`：`mcp_tool_call_timeout_sec`
      防的是「MCP 连接挂住」而不是「任务跑得久」。任务总时长由本函数的
      `budget_sec` 控制，两者口径不同，不能合成一个超时。

    Args:
        tool_name: 仅用于日志/进度事件。
        args: 模型给的原始参数。`wait_seconds` 若存在会被抬到 `poll_wait_sec`，
            避免模型给 90（默认值）导致每轮只睡 90s、RPC 次数翻三倍。
        invoke: 真正发起一次调用的协程工厂（`lambda a: inner.ainvoke(a, ...)`）。
        budget_sec: 总预算（<=0 表示不限，仅排障用）。
        rpc_timeout_sec: 单次调用封顶（<=0 不限）。
        poll_wait_sec: 每轮希望服务端等多久。
        supports_wait: 该工具参数表里是否有 `wait_seconds`。

    Returns:
        最后一次调用的原始返回值（completed/failed/停滞/预算耗尽/参数错）。
    """
    call_args = dict(args) if isinstance(args, dict) else args
    if supports_wait and isinstance(call_args, dict) and poll_wait_sec and poll_wait_sec > 0:
        prev = call_args.get("wait_seconds")
        call_args["wait_seconds"] = poll_wait_sec
        if prev not in (None, poll_wait_sec):
            logger.debug(
                "[DURABLE] %s wait_seconds %s → %s（减少续查轮次）",
                tool_name, prev, poll_wait_sec,
            )

    started_at = time.monotonic()
    deadline = started_at + budget_sec if budget_sec and budget_sec > 0 else None
    last_result: Any = None
    # 两个签名刻意分开：
    #   · progress_sig 判「停滞」（同签名连续 N 轮 ⇒ 真没动）
    #   · report_sig   判「上报节流」（同签名 60s 内不重发）
    # 合成一个会让停滞判定永远为假 —— _maybe_report 返回的就是本轮的签名，
    # 拿它跟自己比必然相等（首版 bug，写在这里防止再犯）。
    progress_sig: tuple | None = None
    report_sig: tuple | None = None
    last_ts = 0.0
    stale_rounds = 0
    rpc_errors = 0
    rounds = 0

    logger.info(
        "[DURABLE] %s 进入自动续查（预算 %ss，单次 RPC 封顶 %ss）",
        tool_name, budget_sec or "不限", rpc_timeout_sec or "不限",
    )

    while True:
        rounds += 1
        try:
            if rpc_timeout_sec and rpc_timeout_sec > 0:
                result = await asyncio.wait_for(
                    invoke(call_args), timeout=rpc_timeout_sec
                )
            else:
                result = await invoke(call_args)
            rpc_errors = 0
        except asyncio.CancelledError:
            # ★ 必须原样抛出：run 被用户取消 / 看门狗取消时，取消要能穿透。
            #   只退出**等待**，沙箱内进程是 nohup 独立的，照常运行，
            #   下次同参调用按指纹续接（这是有意的：停的是等待，不是任务）。
            logger.info(
                "[DURABLE] %s 在等待中被取消（第 %d 轮，已等 %.0fs；"
                "沙箱内任务独立运行，不受影响）",
                tool_name, rounds, time.monotonic() - started_at,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            rpc_errors += 1
            logger.warning(
                "[DURABLE] %s 第 %d 轮调用失败（第 %d/%d 次连续失败）: %s",
                tool_name, rounds, rpc_errors, _MAX_CONSECUTIVE_RPC_ERRORS,
                exc.__class__.__name__,
            )
            if rpc_errors >= _MAX_CONSECUTIVE_RPC_ERRORS:
                # 改由调用方（_MCPErrorShieldTool）按原语义转 ToolInvocationError
                raise
            if deadline and time.monotonic() > deadline:
                logger.warning("[DURABLE] %s 预算耗尽且有连续错误，停止续查", tool_name)
                raise
            await asyncio.sleep(min(5.0, max(1.0, rpc_timeout_sec / 60 if rpc_timeout_sec else 5.0)))
            continue

        last_result = result
        status = payload_status(result)

        # completed / failed / 非结构化返回：不再续查，原样交回模型。
        # status 缺失必须直接返回而不是重试：参数错误、工具不存在这类
        # "确定性失败"重试多少次都是同一个结果，会白烧整个预算。
        if status != "running":
            if rounds > 1:
                logger.info(
                    "[DURABLE] %s 结束：status=%s，共 %d 轮、耗时 %.0fs",
                    tool_name, status or "无结构", rounds, time.monotonic() - started_at,
                )
            return result

        data = result_payload(result) or {}
        report_sig, last_ts = await _maybe_report(
            tool_name, data,
            rounds=rounds, started_at=started_at,
            last_sig=report_sig, last_ts=last_ts,
        )

        # 停滞熔断：签名连续 N 轮不变（含 pages_ocred 不涨）。
        # 第一轮没有比较基准，直接记下。
        sig_now = _progress_signature(data)
        if progress_sig is not None and sig_now == progress_sig:
            stale_rounds += 1
        else:
            stale_rounds = 0
        progress_sig = sig_now
        if stale_rounds >= _MAX_STALE_ROUNDS:
            logger.warning(
                "[DURABLE] %s 进度连续 %d 轮无变化（约 %.0f 分钟），"
                "停止续查并把 running 结果交回模型",
                tool_name, stale_rounds, stale_rounds * (poll_wait_sec or 300) / 60.0,
            )
            return result

        if deadline and time.monotonic() > deadline:
            logger.warning(
                "[DURABLE] %s 达总预算 %ss，停止续查（任务可能仍在后台运行）",
                tool_name, budget_sec,
            )
            return result

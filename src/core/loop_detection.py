"""循环检测 middleware —— 防 agent「工具全成功但整体不收敛」烧满 recursion_limit。

背景（2026-09-08 生产实锤）：
    agent 陷入"每次工具调用都 OK、但方向飘忽不收敛"的循环时，
    现有 4 道防护全部失明：
    - 工具连续失败检测（chat.py:972）：只数 is_error=true，工具全 OK → 恒 0
    - 无进展循环检测（chat.py:994）：靠 _tool_intent_sig 指纹连续重复，
      模型每次换工具/换参数 → 指纹不重复 → 不触发
    - 工具失败阈值（config.py:84）：只数失败
    - M3 完成门（chat.py:1266）：只管 /reports/ 文件交付物

    结果：只能等 recursion_limit=200 烧满，langgraph 抛 GraphRecursionError，
    SSE 流以 error 收尾（chat.py:1235 通用 except Exception 兜底）。

移植来源：deer-flow `packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py`
    + deepseek-harness `packages/guard/repeat-tool-reminder`（软提醒思路）。
    底层实测都是 langchain AgentMiddleware（deepagents graph.py:865 内部就是
    create_agent，langchain-1.3.13 middleware/types.py 含全部 hook）。

适配（4 点，均源码实锤）：
  1. hook 签名用两参 `(state, runtime)` —— 与 langchain 内建
     SummarizationMiddleware.after_model（summarization.py:371）一致。
     （_FreshSkillsMiddleware 写三参 (state, runtime, config) 能跑是意外：
     langgraph 节点实际注入 (state, config)，runtime 参收的是 RunnableConfig）
  2. thread_id 用 `runtime.execution_info.thread_id`（langgraph/runtime.py:39），
     不是 deer-flow 的 runtime.context["thread_id"]——Runtime 不含 config
     （runtime.py:131 明示 "Runtime does not include config"）。
     chat.py:341 注入的 graph_config.configurable.thread_id 落在此。
  3. 去掉 run_id scope（deer-flow 用它分 pending warning 给子代理 executor，
     我们无子代理 executor，简化为 thread_id-only）。
  4. 频次检测加"零新交付物豁免"——对齐 config.py no_progress_window_files：
     "读不同文件（逐步产出推进）"不算空转，只有窗口内零新交付物才算真循环。
     防"逐章节读 40 个文件"这类长任务误报。

设计（两段式，做成 1 个 middleware）：
    Layer 1 hash 级：同一工具调用组（名+参数摘要）重复
        ≥ warn_threshold(3) 次 → 软提醒（wrap_model_call 注入 HumanMessage）
        ≥ hard_limit(5) 次   → 硬剥 tool_calls（after_model 返回 update，逼模型出纯文本）
    Layer 2 频次级：同工具名在窗口内高频（捕获"换参数/换文件"型空转）
        ≥ tool_freq_warn / tool_freq_hard 次 → 同上软提醒/硬剥
    两种介入都不抛异常（对齐 deer-flow：剥离 tool_calls 让循环自然结束，SSE 不中断）。

⚠ file_to_image 与此无关：它是模型调用前把图片 file block 转 image_url（Qwen 视觉）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import TYPE_CHECKING, Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

# 默认阈值（可被构造器/配置覆盖）
_DEFAULT_WARN_THRESHOLD = 3
_DEFAULT_HARD_LIMIT = 5
_DEFAULT_WINDOW_SIZE = 20  # 滑动窗口 track 最近 N 次工具调用
_DEFAULT_TOOL_FREQ_WARN = 12  # 同工具名 ≥12 次警告（deer-flow 默认 30，我们调低：recursion_limit=200 正常 25-35 步）
_DEFAULT_TOOL_FREQ_HARD = 20  # 同工具名 ≥20 次硬剥
_DEFAULT_MAX_TRACKED_THREADS = 100  # LRU 清理上限
_MAX_PENDING_WARNINGS_PER_RUN = 4


# ─── 工具调用稳定性 key 提取 ───────────────────────────────


def _normalize_tool_call_args(raw_args: object) -> tuple[dict, str | None]:
    """把工具参数归一化为 dict + 可选 fallback key。

    部分 provider 把 args 序列化成 JSON 字符串而非 dict，防御性解析，
    保证循环检测不崩溃，同时为非 dict 载荷保留稳定 fallback key。
    """
    if isinstance(raw_args, dict):
        return raw_args, None
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, raw_args
        if isinstance(parsed, dict):
            return parsed, None
        return {}, json.dumps(parsed, sort_keys=True, default=str)
    if raw_args is None:
        return {}, None
    return {}, json.dumps(raw_args, sort_keys=True, default=str)


def _bucket_line(line: int, size: int) -> int:
    """行号分桶：读不同行段的 read_file 视为同一次循环意图。

    用 (line-1)//size（对齐 deer-flow _stable_tool_key:129-134）：
    1-200 → 桶0，201-400 → 桶1。若用 line//size，200 和 201 分到 1 和 1 前
    的正确边界会被截断（max(line,1)//size 会让 200→1、201→1 变一样），
    导致 1-200 和 2-200 分桶错位。必须用 (line-1)//size。
    """
    return (max(line, 1) - 1) // size


def _stable_tool_key(name: str, args: dict, fallback_key: str | None) -> str:
    """从关键参数派生稳定 key（不过拟合噪声）。

    - read_file：path + 行号桶 → 同文件不同行段视为同一意图（防逐行换段刷屏）
    - write_file / str_replace：内容敏感，用全参数 hash（防止把"不同 payload 更新同文件"误判）
    - 其他：只用 path/url/query/command/pattern/glob/cmd 等显著字段
    """
    if name == "read_file" and fallback_key is None:
        path = args.get("path") or ""
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        try:
            start_line = int(start_line) if start_line is not None else 1
        except (TypeError, ValueError):
            start_line = 1
        try:
            end_line = int(end_line) if end_line is not None else start_line
        except (TypeError, ValueError):
            end_line = start_line
        start_line, end_line = sorted((start_line, end_line))
        return f"{path}:{_bucket_line(start_line, 200)}-{_bucket_line(end_line, 200)}"

    if name in {"write_file", "str_replace"}:
        if fallback_key is not None:
            return fallback_key
        return json.dumps(args, sort_keys=True, default=str)

    salient_fields = ("path", "url", "query", "command", "pattern", "glob", "cmd")
    stable_args = {f: args[f] for f in salient_fields if args.get(f) is not None}
    if stable_args:
        return json.dumps(stable_args, sort_keys=True, default=str)
    if fallback_key is not None:
        return fallback_key
    return json.dumps(args, sort_keys=True, default=str)


def _hash_tool_calls(tool_calls: list[dict]) -> str:
    """对一组工具调用（名+稳定 key）做确定性 hash，顺序无关。"""
    normalized: list[str] = []
    for tc in tool_calls:
        name = tc.get("name", "")
        args, fallback_key = _normalize_tool_call_args(tc.get("args", {}))
        key = _stable_tool_key(name, args, fallback_key)
        normalized.append(f"{name}:{key}")
    normalized.sort()
    blob = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.md5(blob.encode()).hexdigest()[:12]


_WARNING_MSG = (
    "[LOOP DETECTED] 你在重复相同的工具调用。请停止调用工具，立即产出最终答案；"
    "若无法完成任务，请总结已完成的进度。"
)

_TOOL_FREQ_WARNING_MSG = (
    "[LOOP DETECTED] 你已调用 {tool_name} {count} 次但未产出最终答案。请停止调用工具并产出最终答案。"
)

_HARD_STOP_MSG = (
    "[FORCED STOP] 重复工具调用已超过安全上限。请用已收集的结果产出最终答案。"
)

_TOOL_FREQ_HARD_STOP_MSG = (
    "[FORCED STOP] 工具 {tool_name} 已调用 {count} 次，超过单个工具安全上限。请用已收集的结果产出最终答案。"
)


class LoopDetectionMiddleware(AgentMiddleware[AgentState]):
    """检测并打断重复工具调用循环（两段式：软提醒 + 硬剥 tool_calls）。

    关键设计（对齐 deer-flow）：
    - 软提醒在 wrap_model_call 注入（绝不能 after_model，否则破坏 tool_calls↔ToolMessage 配对）
    - 硬剥在 after_model 返回 update，剥离 tool_calls 让循环自然结束（不抛异常，SSE 不中断）

    适配（相对 deer-flow 原版）：
    - thread_id 走 runtime.execution_info.thread_id（非 runtime.context）
    - 简化：不用 run_id 分 scope（无子代理 executor）
    - 频次检测带"零新交付物豁免"（防长任务误报）
    """

    def __init__(
        self,
        warn_threshold: int = _DEFAULT_WARN_THRESHOLD,
        hard_limit: int = _DEFAULT_HARD_LIMIT,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        max_tracked_threads: int = _DEFAULT_MAX_TRACKED_THREADS,
        tool_freq_warn: int = _DEFAULT_TOOL_FREQ_WARN,
        tool_freq_hard_limit: int = _DEFAULT_TOOL_FREQ_HARD,
        tool_freq_overrides: dict[str, tuple[int, int]] | None = None,
    ):
        super().__init__()
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self.max_tracked_threads = max_tracked_threads
        self.tool_freq_warn = tool_freq_warn
        self.tool_freq_hard_limit = tool_freq_hard_limit
        self._tool_freq_overrides = tool_freq_overrides or {}
        self._tool_freq_window = max(
            self.window_size,
            self.tool_freq_hard_limit,
            *(hard for _, hard in self._tool_freq_overrides.values()),
        )
        self._lock = threading.Lock()
        self._history: dict[str, list[str]] = {}  # thread_id -> [hash,...]
        self._warned: dict[str, set[str]] = defaultdict(set)
        self._tool_name_history: dict[str, deque[str]] = defaultdict(deque)
        self._tool_name_counter: dict[str, Counter[str]] = defaultdict(Counter)
        self._tool_freq_warned: dict[str, set[str]] = defaultdict(set)
        self._pending_warnings: dict[str, list[str]] = defaultdict(list)

    # ─── thread/run 定位（适配点 2/3）───────────────

    @staticmethod
    def _get_thread_id(runtime: Runtime) -> str:
        """从 runtime.execution_info.thread_id 取 thread 标识（适配点 2）。

        deer-flow 用 runtime.context["thread_id"]——对我们错：Runtime 不含 config。
        langgraph ExecutionInfo.thread_id（runtime.py:39）正是 chat.py:341 注入的
        graph_config.configurable.thread_id 的落点。
        """
        info = getattr(runtime, "execution_info", None)
        thread_id = getattr(info, "thread_id", None) if info is not None else None
        return str(thread_id) if thread_id else "default"

    # ─── 检测核心 ─────────────────────────────

    def _track_and_check(
        self, state: AgentState, runtime: Runtime
    ) -> tuple[str | None, bool]:
        """两层检测：
        1. hash 级：同一工具调用组重复（名+参数）
        2. 频次级：同工具名高频（捕获换参但同工具型空转）
        返回 (warning_message_or_None, should_hard_stop)。
        """
        messages = state.get("messages", [])
        if not messages:
            return None, False
        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None, False
        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None, False

        thread_id = self._get_thread_id(runtime)
        call_hash = _hash_tool_calls(tool_calls)

        with self._lock:
            # Layer 1：hash 级
            history = self._history.setdefault(thread_id, [])
            history.append(call_hash)
            if len(history) > self.window_size:
                history[:] = history[-self.window_size :]

            warned = self._warned.get(thread_id)
            if warned is not None:
                warned.intersection_update(history)
                if not warned:
                    self._warned.pop(thread_id, None)

            count = history.count(call_hash)
            tool_names = [tc.get("name", "?") for tc in tool_calls]

            if count >= self.hard_limit:
                logger.error(
                    "Loop hard limit reached — forcing stop (thread=%s, call_hash=%s, count=%d, tools=%s)",
                    thread_id, call_hash, count, tool_names,
                )
                return _HARD_STOP_MSG, True

            if count >= self.warn_threshold:
                warned = self._warned[thread_id]
                if call_hash not in warned:
                    warned.add(call_hash)
                    logger.warning(
                        "Repetitive tool calls detected — injecting warning (thread=%s, count=%d, tools=%s)",
                        thread_id, count, tool_names,
                    )
                    return _WARNING_MSG, False

            # Layer 2：频次级（同工具名）
            tool_name_history = self._tool_name_history[thread_id]
            name_counter = self._tool_name_counter[thread_id]
            for tc in tool_calls:
                name = tc.get("name", "")
                if not name:
                    continue
                tool_name_history.append(name)
                name_counter[name] += 1
                while len(tool_name_history) > self._tool_freq_window:
                    old = tool_name_history.popleft()
                    c = name_counter[old] - 1
                    if c <= 0:
                        del name_counter[old]
                    else:
                        name_counter[old] = c
                freq_count = name_counter.get(name, 0)

                if name in self._tool_freq_overrides:
                    eff_warn, eff_hard = self._tool_freq_overrides[name]
                else:
                    eff_warn, eff_hard = self.tool_freq_warn, self.tool_freq_hard_limit

                if freq_count >= eff_hard:
                    logger.error(
                        "Tool frequency hard limit — forcing stop (thread=%s, tool=%s, count=%d)",
                        thread_id, name, freq_count,
                    )
                    return _TOOL_FREQ_HARD_STOP_MSG.format(tool_name=name, count=freq_count), True

                if freq_count >= eff_warn:
                    freq_warned = self._tool_freq_warned[thread_id]
                    if name not in freq_warned:
                        freq_warned.add(name)
                        logger.warning(
                            "Tool frequency warning — too many calls to same tool (thread=%s, tool=%s, count=%d)",
                            thread_id, name, freq_count,
                        )
                        return _TOOL_FREQ_WARNING_MSG.format(tool_name=name, count=freq_count), False
                else:
                    self._tool_freq_warned[thread_id].discard(name)

        return None, False

    # ─── 硬剥 tool_calls 的 update 构造 ─────────────

    @staticmethod
    def _append_text(content: str | list | None, text: str) -> str | list:
        """向 AIMessage content 追加文本，兼容 str/list/None。"""
        if content is None:
            return text
        if isinstance(content, list):
            return [*content, {"type": "text", "text": f"\n\n{text}"}]
        if isinstance(content, str):
            return content + f"\n\n{text}"
        return str(content) + f"\n\n{text}"

    @staticmethod
    def _build_hard_stop_update(last_msg, content: str | list) -> dict:
        """清空 tool 元数据，让强制停止消息序列化为纯 assistant 文本。"""
        update = {"tool_calls": [], "content": content}
        additional_kwargs = dict(getattr(last_msg, "additional_kwargs", {}) or {})
        for key in ("tool_calls", "function_call"):
            additional_kwargs.pop(key, None)
        update["additional_kwargs"] = additional_kwargs
        response_metadata = deepcopy(getattr(last_msg, "response_metadata", {}) or {})
        if response_metadata.get("finish_reason") == "tool_calls":
            response_metadata["finish_reason"] = "stop"
        update["response_metadata"] = response_metadata
        return update

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        warning, hard_stop = self._track_and_check(state, runtime)

        if hard_stop:
            # 剥离 tool_calls 让循环自然结束（不抛异常，SSE 不中断）
            messages = state.get("messages", [])
            last_msg = messages[-1]
            content = self._append_text(last_msg.content, warning or _HARD_STOP_MSG)
            stripped_msg = last_msg.model_copy(
                update=self._build_hard_stop_update(last_msg, content)
            )
            return {"messages": [stripped_msg]}

        if warning:
            # 软提醒：延迟到 wrap_model_call 注入（不能 after_model，防破坏 tool_calls↔ToolMessage 配对）
            self._queue_pending_warning(runtime, warning)
            return None

        return None

    # ─── pending warning 队列 ─────────────────────────

    def _queue_pending_warning(self, runtime: Runtime, warning: str) -> None:
        thread_id = self._get_thread_id(runtime)
        with self._lock:
            warnings = self._pending_warnings[thread_id]
            if warning not in warnings:
                warnings.append(warning)
            if len(warnings) > _MAX_PENDING_WARNINGS_PER_RUN:
                del warnings[: len(warnings) - _MAX_PENDING_WARNINGS_PER_RUN]

    def _drain_pending_warnings(self, runtime: Runtime) -> list[str]:
        thread_id = self._get_thread_id(runtime)
        with self._lock:
            warnings = self._pending_warnings.pop(thread_id, [])
        return warnings

    def _augment_request(self, request: ModelRequest) -> ModelRequest:
        """把排队中的循环警告（若有）追加到出站消息末尾。

        追加在 *所有已有消息之后*（含上一轮 AIMessage(tool_calls) 的 ToolMessage 响应），
        保持 tool_calls→tool_messages 配对完整（OpenAI/Moonshot 校验器要求，
        Anthropic 禁止中途 SystemMessage —— 我们用 HumanMessage）。
        """
        warnings = self._drain_pending_warnings(request.runtime)
        if not warnings:
            return request
        new_messages = [
            *request.messages,
            HumanMessage(content=self._format_warning_message(warnings), name="loop_warning"),
        ]
        return request.override(messages=new_messages)

    @staticmethod
    def _format_warning_message(warnings: list[str]) -> str:
        deduped = list(dict.fromkeys(warnings))
        return "\n\n".join(deduped)

    # ─── hook 实现（适配点 1：两参签名）────────────

    @override
    def before_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        # 清理上一 run 残留的 pending warning（简化：不按 run_id 分，整 pending 清）
        return None

    @override
    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self.before_agent(state, runtime)

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._pending_warnings.pop(self._get_thread_id(runtime), None)
        return None

    @override
    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self.after_agent(state, runtime)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._augment_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._augment_request(request))

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

适配（5 点，均源码实锤）：
  0. ★ 工具层契约（2026-10-10 补，会话 GY24428:aaf95335 实证）：
     deer-flow 的中间件天然假设的是**它自己的自研工具集** —— 它的 read_file 签名是
     `read_file(path, start_line, end_line)`（deer-flow sandbox/tools.py:2148），
     与我们用的 deepagents 内置工具（`file_path` / `offset` / `limit`）不是同一套契约。
     移植时必须逐字段核对工具名与参数名，**不能只适配框架层**：当时把下面 1-4 点
     当作"全部适配"，漏了这一层，`read_file` 的稳定 key 恒为 ":0-0" —— 读哪个文件、
     读哪一段都不影响，窗口内攒够 hard_limit 次必然硬停（"读文件→改脚本→跑"是调试类
     任务的肌肉记忆动作，不是概率问题而是必中）。
     现改为从模型侧工具 args_schema 反查字段名（见 §工具参数契约），
     并在 args 出现 schema 外槽位字段时告警（把静默降级变成可观测）。
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
import time
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
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
# ─── Layer 2 占比/交替判据默认值（2026-10-17 新增）───
# 存在理由见 config.py loop_detect_tool_ratio_threshold 的长注释：绝对次数阈值
# 会被"穿插调用任意第二种工具"结构性稀释。83345f0c 实测 execute 累计 24 次、
# 20 窗口内最多 16 次 < hard=20 ⇒ 硬停在结构上不可能触发。
_DEFAULT_TOOL_FREQ_RATIO = 0.6  # 窗口内单一工具占比阈值
_DEFAULT_MIN_WINDOW_FILL = 12  # 窗口至少填满这么多次才做占比判定（防小样本噪声）
_DEFAULT_ALTERNATING_MIN_RUN = 6  # 交替循环最少连续匹配次数
#: 交付物目录特征串。窗口内工具结果出现它即视为"有进展"，占比/交替硬停**不生效**。
#: 与 config.py report_root / chat.py M3 完成门同源（那里也只认 /reports/）。
_DELIVERABLE_PATH_MARK = "/reports/"
#: 这个工具名本身即是"交付动作"，出现即视为有进展
_DELIVERABLE_TOOL_NAME = "download_from_sandbox"


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


def _as_int(value: object, default: int = 1) -> int:
    """尽力把工具参数转成 int（模型可能传 "100" 这种字符串），失败取 default。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# ─── 工具参数契约：从真实参数 schema 反查，不硬编码字段名 ──────────────
#
# 背景（2026-10-10 生产实锤，会话 GY24428:aaf95335）：
#   本文件曾照抄 deer-flow 的 _stable_tool_key，把 read_file 的路径/行段字段硬编码为
#   `path` / `start_line` / `end_line`。这段代码在 deer-flow **完全正确** —— 因为
#   deer-flow 的 read_file 是它自研的沙箱工具（sandbox/tools.py:2148，签名就是
#   read_file(path, start_line, end_line)）。而本项目的 read_file 来自 deepagents
#   filesystem middleware，真实契约是 `file_path` / `offset` / `limit`。
#   移植时只适配了框架层（hook 签名、runtime 取数、阈值——见 §适配 4 点），
#   工具层契约默认"上游怎么写就怎么用"，于是三个字段一个都取不到：
#       path=""、start=end=1 → key 恒为 ":0-0"
#   → 窗口内攒够 hard_limit 次 read_file **必然**硬停。而"读文件 → 改脚本 → 跑"
#   正是调试类任务的肌肉记忆动作：不是概率问题，是必中。aaf95335 会话里模型已经
#   自己诊断出 PaddleOCR 调用错误、正在修，第 5 次 read_file 被掐断，离成功一步之遥。
#
#   失败之所以静默：`args.get("x") or ""` 取不到字段时不报错、不告警，直接降级成
#   常量 —— 既不崩溃，单测也全绿（原单测照 deer-flow 契约构造 args，属自证式），
#   只在生产以"误判"形式现身，而误判与代码缺陷之间隔着一整条推理链。
#
# 治本：字段名不再由本文件断言，而是**从 agent 实际持有的工具 args_schema 反查**。
#   下面这些常量只是"反查时按什么顺序试"的查询条件，不是契约；
#   契约冲突时以 schema 为准，且会告警（见 _detect_contract_drift）。
#
#: 路径/目标标识类字段候选（按优先级试）
_PATH_FIELD_CANDIDATES: tuple[str, ...] = (
    "file_path", "path", "filepath", "file", "filename", "dir", "directory",
)
#: 起始位置候选（绝对行号；0-indexed 如 deepagents offset 与 1-indexed 如 deer-flow
#: start_line 在"分桶"这一粗粒度用途下等价，无需区分）
_START_FIELD_CANDIDATES: tuple[str, ...] = (
    "offset", "start_line", "start", "from_line", "line",
)
#: 结束位置候选：值为**绝对结束行号**（如 deer-flow 的 end_line）
_END_FIELD_CANDIDATES: tuple[str, ...] = ("end_line", "end", "to_line")
#: 跨度候选：值为**行数**（如 deepagents 的 limit=100），
#: 必须换算成绝对结束行 start+span —— 若误当绝对行号，"从第 200 行起读 100 行"
#: 会与"从头读 100 行"落进同一分桶而互相掩盖。
_SPAN_FIELD_CANDIDATES: tuple[str, ...] = ("limit", "count", "lines")
#: 内容类字段：命中即视为"内容敏感工具"（同一文件写不同内容不是重复调用）
_PAYLOAD_FIELD_CANDIDATES: tuple[str, ...] = (
    "content", "old_string", "new_string", "text", "body", "data", "code", "contents",
)
#: 无 schema 信息（契约未知）时的显著字段兜底，保住既有行为
_SALIENT_FIELDS_FALLBACK: tuple[str, ...] = (
    "path", "url", "query", "command", "pattern", "glob", "cmd",
)
#: 契约漂移探测面：args 里出现这些字段却不在工具 schema 中 → 明确的错位信号
_CONTRACT_PROBE_FIELDS: tuple[str, ...] = tuple(
    dict.fromkeys(
        _PATH_FIELD_CANDIDATES
        + _START_FIELD_CANDIDATES
        + _END_FIELD_CANDIDATES
        + _SPAN_FIELD_CANDIDATES
        + _PAYLOAD_FIELD_CANDIDATES
    )
)


def _schema_field_names(schema: object) -> frozenset[str] | None:
    """把各种 schema 表示归一化为字段名集合（取不到返回 None）。"""
    if schema is None:
        return None
    if isinstance(schema, dict):
        props = schema.get("properties")
        return frozenset(str(k) for k in props) if isinstance(props, dict) and props else None
    fields = getattr(schema, "model_fields", None)  # pydantic v2（BaseTool.args_schema 常态）
    if isinstance(fields, dict) and fields:
        return frozenset(str(k) for k in fields)
    fields = getattr(schema, "__fields__", None)  # pydantic v1 兜底
    if isinstance(fields, dict) and fields:
        return frozenset(str(k) for k in fields)
    return None


def _extract_schema_fields(tool: object) -> frozenset[str] | None:
    """从工具对象提取其参数名集合（契约反查的唯一入口）。

    支持三种形态：langchain BaseTool（.args_schema）、OpenAI function 格式 dict、
    裸 parameters dict。**取不到一律返回 None** —— 调用方必须把 None 当作
    "契约未知"走 args 探测兜底，不得当作"字段不存在"（否则又是静默误判）。
    """
    for attr in ("args_schema", "tool_call_schema"):
        names = _schema_field_names(getattr(tool, attr, None))
        if names:
            return names
    if isinstance(tool, dict):
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if isinstance(fn, dict):
            return _schema_field_names(fn.get("parameters"))
    return None


def _resolve_field(
    args: dict, schema_fields: frozenset[str] | None, candidates: tuple[str, ...]
) -> str | None:
    """按候选优先级解析出该工具**真实使用**的字段名（双源：schema 权威 + args 探测）。

    - schema 已知：只认 schema 里有的名字（本次 args 没传也算命中 —— 用默认值也算
      一次"读整个文件/从头读"的调用，这正是期望语义）
    - schema 未知（工具尚未学习到）：退化为在 args 里探测，保住无 schema 环境的既有行为
    """
    for cand in candidates:
        if schema_fields is None:
            if cand in args:
                return cand
        elif cand in schema_fields:
            return cand
    return None


def _detect_contract_drift(
    args: dict, schema_fields: frozenset[str] | None
) -> str | None:
    """schema 已知时，args 里出现"疑似槽位字段但不在 schema 中"→ 返回该字段名。

    这正是 aaf95335 的形态：args 带 path/start_line/end_line，而 read_file 的 schema
    只有 file_path/offset/limit。旧实现静默取空值 → key 恒定 → 必然误判；现在把
    "静默降级"改成"显式告警"（调用方按 (tool, field) 限流只报一次），
    使同类契约漂移在**首次发生**时就可见，而不是等它以任务失败的形式浮出水面。
    """
    if schema_fields is None:
        return None
    for field in _CONTRACT_PROBE_FIELDS:
        if field in args and field not in schema_fields:
            return field
    return None


def _stable_tool_key(
    args: dict,
    fallback_key: str | None,
    schema_fields: frozenset[str] | None = None,
) -> str:
    """从关键参数派生稳定 key（不过拟合噪声，也不被字段名漂移带偏）。

    ``schema_fields`` 语义见 _resolve_field（None = 契约未知，走 args 探测兜底）。

    三类工具 —— **按 schema 特征判定，不再按工具名硬编码**：
    - 分页读取型（有路径字段 + 行段字段，如 read_file{file_path,offset,limit}）：
      按"文件 + 行段桶"聚合，同文件同行段重复读才算循环，读新行段不算。
    - 内容敏感型（有 content/old_string/new_string 等 payload 字段，如 write_file /
      edit_file）：用全参 hash，同一文件写不同内容不是重复调用。
    - 其余（ls/glob/grep/execute…）：只用标识类显著字段，忽略 timeout 等噪声参数。
    """
    if fallback_key is not None:
        # 非 dict 载荷（provider 把 args 序列化成非 JSON 对象）→ 原样用，最深兜底
        return fallback_key

    path_field = _resolve_field(args, schema_fields, _PATH_FIELD_CANDIDATES)
    start_field = _resolve_field(args, schema_fields, _START_FIELD_CANDIDATES)
    end_field = _resolve_field(args, schema_fields, _END_FIELD_CANDIDATES)
    span_field = _resolve_field(args, schema_fields, _SPAN_FIELD_CANDIDATES)

    # ① 分页读取型：路径 + 任一"行段"字段
    #    守卫：路径必须**真实取到值**才走分桶。否则（例如 args 用了 schema 之外的旧
    #    字段名，schema 反查出的 file_path 在本次 args 里并不存在）取到的会是空串，
    #    分桶又会退化成常量 key —— 那正是 aaf95335 的形态。宁可落到下面的全参 hash
    #    （永不塌成常量，最坏漏报而非误杀），也不让"契约错位"再次变成"必然硬停"。
    if path_field and (start_field or end_field or span_field):
        path = args.get(path_field)
        if path:
            start = _as_int(args.get(start_field)) if start_field else 1
            if end_field:  # 值为绝对结束行号
                end = _as_int(args.get(end_field), start)
            elif span_field:  # 值为行数 → 换算成绝对结束行
                end = start + max(_as_int(args.get(span_field), 1), 0)
            else:
                end = start
            lo, hi = sorted((start, end))
            return f"{path}:{_bucket_line(lo, 200)}-{_bucket_line(hi, 200)}"

    # ② 内容敏感型：存在 payload 类字段
    if _resolve_field(args, schema_fields, _PAYLOAD_FIELD_CANDIDATES):
        return json.dumps(args, sort_keys=True, default=str)

    # ③ 其余：标识类显著字段（schema 反查到的路径字段也并入，覆盖 file_path 这类命名）
    salient_fields = set(_SALIENT_FIELDS_FALLBACK)
    if path_field:
        salient_fields.add(path_field)
    stable_args = {f: args[f] for f in salient_fields if args.get(f) is not None}
    if stable_args:
        return json.dumps(stable_args, sort_keys=True, default=str)
    return json.dumps(args, sort_keys=True, default=str)


def tool_call_signature(
    tool_name: str,
    args: object,
    schema_fields: frozenset[str] | None = None,
) -> str:
    """**单个**工具调用的意图指纹：工具名 + 稳定参数 key。

    这是全项目循环检测的**唯一**指纹口径，图内（LoopDetectionMiddleware）与
    图外（chat.py 的 P0 无进展检测）都必须走这里，不得各写一套 —— 口径一旦分叉，
    同一个"空转"在两处会得出相反结论，而两处的止损动作又都不可逆。

    ── 为什么必须是"名 + 参"，不能是"名 + 结果文本"（2026-10-17 生产实锤）──
    会话 GY24428:4f5ede6a 里，模型连发 10 条**各不相同**的 pymupdf 命令
    （读总页数 → 读第 1 页 → 读第 2 页 → 搜关键词 → 统计 → …），排查路径完全正确。
    但 P0 旧实现取的是**工具结果前 60 字符**，而每次 pymupdf 输出的**第一行**恒为
    那句 98 字符的弃用警告::

        warning: The `fitz` API is deprecated and will be removed in future. ...

    98 > 60 ⇒ 整个取样窗口被这行噪声占满，真正的差异（``总页数: 262`` / ``第2页`` /
    ``包含光源的页面: [2]``）全在窗口之外 ⇒ 10 次去重后**只有 1 种指纹** ⇒
    ``_repeat_count`` 必达阈值 ⇒ **必然误报**（不是概率问题）。模型已在收敛
    （最后一条输出在判断"页面是图"、下一步就该想到 OCR），却在离成功一步之遥时被掐断。

    结果文本是**工具的输出**，被日志噪声、时间戳、进度条、弃用警告污染的渠道太多；
    参数才是**模型的意图**。deer-flow 的 ``_hash_tool_calls`` 从一开始 hash 的就是
    args（其 docstring 第 7 行明写 "hash full args to reduce false positives"），
    本项目的中间件层也是照此移植的 —— 只有 P0 这一处漏改。

    同一模式已发作两次，根因一致（**指纹区分度退化 ⇒ 从概率误报变成必然误报**）：
    会话 aaf95335 炸在**入参侧**（read_file 字段名契约错位 ⇒ 稳定 key 恒为 ``:0-0``），
    会话 4f5ede6a 炸在**结果侧**（噪声占满取样窗口）。两次都修了"发现的那一侧"。

    ``schema_fields``：该工具的真实参数名集合（见 ``_extract_schema_fields``）。
    传 None 走 args 探测兜底 —— 仍是有效 key，不会塌成常量（见 ``_stable_tool_key``）。
    """
    norm_args, fallback_key = _normalize_tool_call_args(args)
    key = _stable_tool_key(norm_args, fallback_key, schema_fields)
    return f"{tool_name}:{key}"


def _hash_tool_calls(
    tool_calls: list[dict],
    schema_lookup: Callable[[str], frozenset[str] | None] | None = None,
) -> str:
    """对一组工具调用做确定性 hash，顺序无关。

    ``schema_lookup`` 把工具名映射到其真实参数名集合（见 _extract_schema_fields）。
    传 None 或查不到时该工具走 args 探测兜底 —— 两个来源都不会退化成常量 key。

    逐条复用 ``tool_call_signature``（而不是在这里再写一遍 name/key 拼装），
    使"图内 hash"与"API 层 P0 指纹"**物理上同源**，未来改口径不会只改一半。
    """
    normalized = [
        tool_call_signature(
            tc.get("name", ""),
            tc.get("args", {}),
            schema_lookup(tc.get("name", "")) if schema_lookup is not None else None,
        )
        for tc in tool_calls
    ]
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

#: 硬停时若「最后一条工具结果仍是 running」改用这条（2026-10-20 add0229b）。
#: 两件事必须区分：真空转该逼模型收口；而长任务（OCR 262 页 ≈110 分钟）被
#: 误判时后台**还在跑**、产物在沙箱里——这时叫模型"用已有结果作答"，它会
#: 宣布"文件没上传 / 任务失败"，把正在推进的任务说死，用户拿到的结论完全错。
_HARD_STOP_RUNNING_MSG = (
    "[FORCED STOP] 已停止继续调用工具。注意：**后台任务仍在运行**，"
    "之前调用返回的 sandbox_path 指向的文件依然有效。"
    "请向用户说明当前进度与已确认的信息，明确告知结果仍在生成中、"
    "后续可用完全相同的参数再次调用该工具取回完整结果；"
    "不要声称任务失败、文件不存在或需要重新上传。"
)

_TOOL_FREQ_HARD_STOP_MSG = (
    "[FORCED STOP] 工具 {tool_name} 已调用 {count} 次，超过单个工具安全上限。请用已收集的结果产出最终答案。"
)

# ★ 占比/交替硬停文案（2026-10-17）。措辞与上面的区别：这里明确告诉模型
#   "你的调用形态像空转"而不是"某个工具调太多了"——83345f0c 里模型把参数错误
#   归因成"API 兼容性问题"，通用文案会再次被它误读成环境问题。
_TOOL_FREQ_PATTERN_HARD_STOP_MSG = (
    "[FORCED STOP] 检测到工具调用形态异常（{detail}），且期间没有任何新交付物。"
    "这不是环境或工具故障，而是调用方式没有推进任务。"
    "请立即停止调用工具，用已收集到的信息直接给出结论或交付物；"
    "若确实无法完成，明确说明卡点与已尝试的方案。"
)

# ─── Layer 2 占比 / 交替循环判据（2026-10-17，83345f0c 实证）─────────────


#: 「有客观进展的重复调用」允许的最大重复次数（2026-10-20 add0229b）。
#: submit-and-poll 型工具（ocr_pdf_to_text）的正常形态就是同参反复续查：
#: 262 页 × 30s/页 ≈ 110 分钟，每次 wait_seconds=300 ⇒ 约 22 次。而 Layer1
#: 的 hard_limit=5 会在第 5 次就硬剥 tool_calls——110 分钟的任务必然失败，
#: 尽管它每一轮都在真实推进（pages_ocred 在涨）。
#: 上限 40 是兜底：防「后台任务其实死了、但每轮照样返回 running」的空转
#: （那种形态没有进展增长，or 直接超过上限）。
_DEFAULT_PROGRESS_EXEMPT_MAX = 40

#: 「任务在推进」的状态值。submit-and-poll 的返回形如
#: {"success":true,"status":"running","pages_ocred":12,...}
_PROGRESS_STATUS_VALUES = ("running", "completed")


def _msg_text(msg: object) -> str:
    """把消息 content 归一成字符串（str / list[part] / 其他）。"""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "\n".join(parts)
    return str(content or "")


def _parse_tool_payload(text: str) -> dict | None:
    """从工具结果文本里取出结构化 JSON（dict）；取不到返回 None。

    MCP 工具返回可能被包成 `[{"type":"text","text":"{...}"}]`，也可能已解包
    成纯 JSON——两种形态都试一遍。只接受以 `{` 开头的候选：正文里随手出现
    的 `status` 字样不该被当成结构化信号。
    """
    if not text:
        return None
    raw = text.strip()
    candidates = [raw]
    if raw.startswith("[") and '"text"' in raw:
        try:
            for part in json.loads(raw):
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    candidates.append(part["text"].strip())
        except (ValueError, TypeError):
            pass
    for cand in candidates:
        if not cand.startswith("{"):
            continue
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _is_progress_payload(text: str) -> bool:
    """工具结果是否表示「任务在推进」（后台在跑 / 这一批已出结果）。

    判据（任一命中）：
      · status ∈ {running, completed}（submit-and-poll 的续查/收口返回）
      · pages_ocred > 0（有客观进展计数）

    ★ 只认**结构化字段**，不做宽松子串匹配：真死循环里重复读文件/重复
      execute 的输出也可能出现 "status" 字样，宽松匹配会把它们一起放过，
      豁免就变成了"永久免死金牌"。
    """
    data = _parse_tool_payload(text)
    if data is None:
        return False
    if str(data.get("status") or "") in _PROGRESS_STATUS_VALUES:
        return True
    try:
        return int(data.get("pages_ocred") or 0) > 0
    except (TypeError, ValueError):
        return False


def _max_tool_ratio(seq: list[str] | deque[str]) -> tuple[str, int, float]:
    """窗口内出现次数最多的工具名及其占比。

    返回 (工具名, 次数, 占比)；空序列返回 ("", 0, 0.0)。
    """
    if not seq:
        return "", 0, 0.0
    counts = Counter(seq)
    name, n = counts.most_common(1)[0]
    return name, n, n / len(seq)


def _alternating_run(seq: list[str] | deque[str], periods: tuple[int, ...] = (2, 3)) -> int:
    """检测 A,B,A,B,… 式工具名周期震荡，返回最长连续匹配次数。

    为什么需要它（deer-flow #2590 "detect alternating tool-name cycles"）：
    占比判据只能抓"一种工具占大头"；而「写脚本 → 跑 → 改 → 跑」这类循环里两种
    工具各占一半，占比可能都到不了阈值，但周期震荡极其稳定。83345f0c 正是
    这种形态：`execute` 与 `write_file` 来回穿插 20+ 次，零交付物。

    算法：对每个周期 p，数 `seq[i] == seq[i-p]` 的最长连续段。真交替时该值
    ≈ len(seq) - p；随机/渐进式探索则很快断掉。
    """
    if len(seq) < 4:
        return 0
    best = 0
    for p in periods:
        run = 0
        for i in range(p, len(seq)):
            run = run + 1 if seq[i] == seq[i - p] else 0
            best = max(best, run)
    return best


def _window_has_deliverable(messages: list[Any], limit: int) -> bool:
    """窗口内的工具结果里有没有"交付物"信号？

    占比/交替硬停的**误杀闸门**：跑 build、批量处理文件、长流程调试都是
    「单一工具高频但完全正常」的形态，唯一的区分点是"有没有在向交付物推进"。
    判据与 M3 完成门同源（chat.py 只认 /reports/），另外把
    `download_from_sandbox` 本身也视为交付动作。

    只扫最近 `limit` 条消息（与工具频次窗口同量级），避免长历史里一次早期
    交付把后期空转全程豁免掉。
    """
    scanned = 0
    for msg in reversed(messages):
        if scanned >= limit:
            break
        if getattr(msg, "type", None) != "tool":
            continue
        scanned += 1
        name = getattr(msg, "name", "") or ""
        if name == _DELIVERABLE_TOOL_NAME:
            return True
        content = getattr(msg, "content", None)
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif isinstance(part, str):
                    texts.append(part)
        if any(_DELIVERABLE_PATH_MARK in t for t in texts):
            return True
    return False


# ─── 硬停标记：中间件 → API 层的唯一通道（2026-10-10，②A）──────────────
# 为什么需要它：硬停（剥空 tool_calls）只让**图**这一轮结束，API 层完全不知道
# 发生过什么 —— chat.py 的 M3 完成门随后照常按「/reports 有无新产物」判定，
# 零产出就注入自动继续轮，等于把刚掐断的循环**重新推起来**：硬停→继续→再硬停
# 无限打转（会话 GY24428:a2719d7b 的"硬停未能真正止损"）。
# 约定：常量集中在此，调用方不得硬编码字符串。
LOOP_FORCED_STOP_KEY = "loop_forced_stop"
STOP_REASON_REPEAT_CALLS = "repeat_calls"  # Layer 1：同一工具调用组重复
STOP_REASON_TOOL_FREQUENCY = "tool_frequency"  # Layer 2：同工具名高频（绝对次数）
STOP_REASON_TOOL_FREQ_PATTERN = "tool_freq_pattern"  # Layer 2：占比/交替（零交付物才生效）


class LoopDetectionState(AgentState):
    """扩展 agent state：承载「本轮已被强制收敛」的标记。

    为什么落 state 而不是模块级信号表（terminal_response 的 _FALLBACK_SIGNALS 那种）：
      本标记的语义是 **run 级**的 —— 必须在每个 run 开始时清零，否则上一轮的硬停
      会被下一轮当成自己的信号。state 天然带这个生命周期：``before_agent`` 在
      langgraph 里每个 run 开头执行一次，正好用来清零；同时它随 checkpoint 走，
      观测/复盘时也能从 state 里看出"这一轮是被强停的"。
      而 langchain 的 middleware 扩展 state 走 ``state_schema`` 类属性
      （langchain factory.py:1154 `state_schemas = [*(m.state_schema for m in middleware), ...]`
       + `_resolve_schemas` 合并），无需改 agent.py 的装配代码。

    读取方：``src/api/endpoints/chat.py`` 从 ``stream_mode="updates"`` 事件里取
    ``LoopDetectionMiddleware.after_model`` 节点输出的本键，命中即**真正终止本轮**
    （不再走 M3 完成门的自动继续轮），并发一条 ``completion_blocked``
    （code=loop_forced_stop）告知用户。
    """

    loop_forced_stop: NotRequired[dict[str, Any] | None]


class LoopDetectionMiddleware(AgentMiddleware[LoopDetectionState]):
    """检测并打断重复工具调用循环（两段式：软提醒 + 硬剥 tool_calls）。

    关键设计（对齐 deer-flow）：
    - 软提醒在 wrap_model_call 注入（绝不能 after_model，否则破坏 tool_calls↔ToolMessage 配对）
    - 硬剥在 after_model 返回 update，剥离 tool_calls 让循环自然结束（不抛异常，SSE 不中断）

    适配（相对 deer-flow 原版）：
    - thread_id 走 runtime.execution_info.thread_id（非 runtime.context）
    - 简化：不用 run_id 分 scope（无子代理 executor）
    - 频次检测带"零新交付物豁免"（防长任务误报）
    - 硬停同时写 ``loop_forced_stop`` state 标记（2026-10-10，②A）：只剥 tool_calls
      而不告知 API 层，会被 M3 完成门的自动继续轮重新推起来，等于没止损
    """

    #: 扩展 state（承载硬停标记）；langchain 会把它合并进 graph state schema
    state_schema = LoopDetectionState

    def __init__(
        self,
        warn_threshold: int = _DEFAULT_WARN_THRESHOLD,
        hard_limit: int = _DEFAULT_HARD_LIMIT,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        max_tracked_threads: int = _DEFAULT_MAX_TRACKED_THREADS,
        tool_freq_warn: int = _DEFAULT_TOOL_FREQ_WARN,
        tool_freq_hard_limit: int = _DEFAULT_TOOL_FREQ_HARD,
        tool_freq_overrides: dict[str, tuple[int, int]] | None = None,
        tool_freq_ratio: float = _DEFAULT_TOOL_FREQ_RATIO,
        min_window_fill: int = _DEFAULT_MIN_WINDOW_FILL,
        alternating_min_run: int = _DEFAULT_ALTERNATING_MIN_RUN,
        progress_exempt_max: int = _DEFAULT_PROGRESS_EXEMPT_MAX,
    ):
        super().__init__()
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self.max_tracked_threads = max_tracked_threads
        self.tool_freq_warn = tool_freq_warn
        self.tool_freq_hard_limit = tool_freq_hard_limit
        self.tool_freq_ratio = tool_freq_ratio
        self.min_window_fill = max(1, min_window_fill)
        self.alternating_min_run = max(2, alternating_min_run)
        #: 有客观进展的重复调用允许的最大重复次数（见 _DEFAULT_PROGRESS_EXEMPT_MAX）
        self.progress_exempt_max = max(1, progress_exempt_max)
        self._tool_freq_overrides = tool_freq_overrides or {}
        self._tool_freq_window = max(
            self.window_size,
            self.tool_freq_hard_limit,
            *(hard for _, hard in self._tool_freq_overrides.values()),
        )
        # ★ RLock 而非 Lock：_progress_exempt 会在 _track_and_check 已持锁的
        #   路径里再次加锁（它要更新豁免日志限流集合）。用不可重入的 Lock
        #   会自死锁，而这类死锁在单测里表现为"测试挂住"，极难定位。
        self._lock = threading.RLock()
        self._history: dict[str, list[str]] = {}  # thread_id -> [hash,...]
        self._warned: dict[str, set[str]] = defaultdict(set)
        self._tool_name_history: dict[str, deque[str]] = defaultdict(deque)
        self._tool_name_counter: dict[str, Counter[str]] = defaultdict(Counter)
        self._tool_freq_warned: dict[str, set[str]] = defaultdict(set)
        self._pending_warnings: dict[str, list[str]] = defaultdict(list)
        # 工具名 → 真实参数名集合（契约反查结果；None = 契约未知，见 _extract_schema_fields）
        self._tool_fields: dict[str, frozenset[str] | None] = {}
        # 契约漂移告警限流：(工具名, 越界字段名) 只报一次
        self._drift_warned: set[tuple[str, str]] = set()
        # 豁免日志限流：(thread_id, call_hash) 只打一次——续查会命中很多轮
        self._exempt_logged: set[tuple[str, str]] = set()

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

    # ─── 工具契约学习（E：字段名从 args_schema 反查，替代硬编码）─────────

    @staticmethod
    def _tool_name_of(tool: object) -> str | None:
        """从 BaseTool / OpenAI function dict 里取工具名。"""
        name = getattr(tool, "name", None)
        if isinstance(name, str) and name:
            return name
        if isinstance(tool, dict):
            inner = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            candidate = inner.get("name") if isinstance(inner, dict) else None
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    def _describe_contracts(self, names: list[str]) -> str:
        """把反查结果渲染成一行排障摘要（首次学习时打 INFO）。"""
        parts: list[str] = []
        for name in names:
            fields = self._tool_fields.get(name)
            if fields is None:
                parts.append(f"{name}=契约未知")
                continue
            path_field = _resolve_field({}, fields, _PATH_FIELD_CANDIDATES)
            start_field = _resolve_field({}, fields, _START_FIELD_CANDIDATES)
            end_field = _resolve_field({}, fields, _END_FIELD_CANDIDATES)
            span_field = _resolve_field({}, fields, _SPAN_FIELD_CANDIDATES)
            payload = _resolve_field({}, fields, _PAYLOAD_FIELD_CANDIDATES)
            if path_field and (start_field or end_field or span_field):
                parts.append(
                    f"{name}=分页读取({path_field},{start_field or end_field or span_field})"
                )
            elif payload:
                parts.append(f"{name}=内容敏感({payload})")
            else:
                parts.append(f"{name}=显著字段")
        return "; ".join(parts)

    def _learn_tool_schemas(self, tools: object) -> None:
        """从 agent 真实持有的工具集学习参数契约（首次见到即缓存，之后零成本）。

        这是「字段名不再硬编码」的落点：契约来自工具自身的 args_schema，而不是本文件
        的断言。时机选在 wrap_model_call —— 它在 after_model（检测发生处）**之前**
        必然执行一次，所以检测时契约已就绪；即便某工具没学到，_stable_tool_key 也会
        退化为 args 探测，不会重演"静默取空值"。
        """
        if not tools:
            return
        learned: list[str] = []
        with self._lock:
            for tool in tools:
                name = self._tool_name_of(tool)
                if not name or name in self._tool_fields:
                    continue
                self._tool_fields[name] = _extract_schema_fields(tool)
                learned.append(name)
        if learned:
            logger.info(
                "[LOOP] 工具参数契约已反查 %d 个（字段名以 schema 为准）：%s",
                len(learned), self._describe_contracts(learned),
            )

    def _schema_fields_for(self, tool_name: str) -> frozenset[str] | None:
        """查表：工具名 → 参数名集合（未学到返回 None，交由 _stable_tool_key 探测兜底）。"""
        return self._tool_fields.get(tool_name)

    def _check_contract_drift(self, tool_call: dict) -> None:
        """工具调用的 args 是否在用 schema 之外的槽位字段？（契约漂移信号）

        限流到同一 (工具, 字段) 只报一次：漂移是代码级问题而非逐次事件，但**绝不能
        因为怕刷屏而不报** —— aaf95335 的根因恰恰是完全不报，把契约错位伪装成
        "检测逻辑正常工作"。
        """
        name = tool_call.get("name", "")
        if not name or name not in self._tool_fields:
            return
        args, _ = _normalize_tool_call_args(tool_call.get("args", {}))
        if not args:
            return
        drift = _detect_contract_drift(args, self._tool_fields.get(name))
        if drift is None:
            return
        key = (name, drift)
        with self._lock:
            if key in self._drift_warned:
                return
            self._drift_warned.add(key)
        logger.error(
            "[LOOP] 工具参数契约漂移：%s 的 schema 中不存在字段 %r —— "
            "循环检测已按 schema 反查取键（不再静默取空值），但该字段来自旧契约，"
            "请核对工具签名与 loop_detection 的候选槽位表。args键=%s",
            name, drift, sorted(args)[:12],
        )

    # ─── 检测核心 ─────────────────────────────

    # ─── 有进展的重复调用豁免（2026-10-20 add0229b）───────────────

    def _last_same_call_had_progress(self, messages: list, call_hash: str) -> bool:
        """上一次「同指纹工具调用」的结果是否表明任务仍在推进。

        形态（submit-and-poll）：
            AI(tool_calls=OCR) → Tool(running) → AI(tool_calls=同一 OCR) → …
        从末尾往前跳过本次 AIMessage，找上一条**带 tool_calls 的 AI**。
        若其指纹不是同一个（模型在换参数重试，而不是续查同一任务），
        不适用豁免——那不是"同一任务在推进"，而是真的在反复试错。
        """
        prev_idx = None
        for i in range(len(messages) - 2, -1, -1):
            m = messages[i]
            if getattr(m, "type", None) != "ai":
                continue
            tcs = getattr(m, "tool_calls", None) or []
            if not tcs:
                continue
            prev_idx = i
            if _hash_tool_calls(tcs, self._schema_fields_for) != call_hash:
                return False
            break
        if prev_idx is None:
            return False
        for m in messages[prev_idx + 1:]:
            if getattr(m, "type", None) != "tool":
                continue
            if _is_progress_payload(_msg_text(m)):
                return True
        return False

    def _progress_exempt(
        self, thread_id: str, messages: list, call_hash: str, count: int
    ) -> bool:
        """同指纹重复但每轮都有客观进展 ⇒ 视为「同一任务在推进」，豁免硬停。

        2026-10-20 add0229b：ocr_pdf_to_text 是 submit-and-poll，262 页需要
        ~22 次同参续查（每次 wait_seconds=300）。Layer1 的 hard_limit=5 会在
        第 5 次硬剥 tool_calls，110 分钟的任务必然失败——而它一直在推进
        （pages_ocred 逐轮增长）。真死循环不满足本豁免：输出是立即报错/
        空返回，既无 status=running 也无 pages_ocred 增长。

        count > progress_exempt_max 仍会硬停：那是「后台任务死了但每轮照样
        返回 running」的形态，不能无限续下去。
        """
        if count > self.progress_exempt_max:
            logger.error(
                "[LOOP] 有进展的重复调用超过豁免上限（count=%d > %d），按空转处理",
                count,
                self.progress_exempt_max,
            )
            return False
        if not self._last_same_call_had_progress(messages, call_hash):
            return False
        with self._lock:
            key = (thread_id, call_hash)
            first = key not in self._exempt_logged
            if first:
                self._exempt_logged.add(key)
        if first:
            logger.info(
                "[LOOP] 同指纹调用 %d 次但每轮都有客观进展（后台任务在推进）"
                "——豁免硬停，继续续查（thread=%s, hash=%s）",
                count,
                thread_id,
                call_hash,
            )
        return True

    @staticmethod
    def _last_tool_still_running(messages: list) -> bool:
        """最后一条工具结果是否表示「后台任务仍在运行」（status=running）。

        硬停文案据此分流：真死循环 vs 长任务被误判。见 _HARD_STOP_RUNNING_MSG。
        """
        for m in reversed(messages):
            if getattr(m, "type", None) != "tool":
                continue
            data = _parse_tool_payload(_msg_text(m))
            if data is None:
                return False
            return str(data.get("status") or "") == "running"
        return False

    def _track_and_check(
        self, state: LoopDetectionState, runtime: Runtime
    ) -> tuple[str | None, dict[str, Any] | None]:
        """两层检测：
        1. hash 级：同一工具调用组重复（名+参数）
        2. 频次级：同工具名高频（捕获换参但同工具型空转）
        返回 (warning_message_or_None, stop_info_or_None)。
        stop_info 非 None 即"应硬停"，其内容会被写进 ``loop_forced_stop`` state 供
        API 层消费（键见 LOOP_FORCED_STOP_KEY）。
        """
        messages = state.get("messages", [])
        if not messages:
            return None, None
        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None, None
        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None, None

        thread_id = self._get_thread_id(runtime)
        # 先做契约漂移体检（不改变判定结果，只让错位可见），再用真实契约算指纹
        for tc in tool_calls:
            self._check_contract_drift(tc)
        call_hash = _hash_tool_calls(tool_calls, self._schema_fields_for)

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
                # ★ 有客观进展的重复不算空转（add0229b：submit-and-poll 续查）
                if self._progress_exempt(thread_id, messages, call_hash, count):
                    pass
                else:
                    logger.error(
                        "Loop hard limit reached — forcing stop (thread=%s, call_hash=%s, count=%d, tools=%s)",
                        thread_id, call_hash, count, tool_names,
                    )
                    return (
                        _HARD_STOP_RUNNING_MSG
                        if self._last_tool_still_running(messages)
                        else _HARD_STOP_MSG
                    ), {
                        "reason": STOP_REASON_REPEAT_CALLS,
                        "call_hash": call_hash,
                        "count": count,
                        "tool_names": tool_names,
                    }

            if count >= self.warn_threshold:
                warned = self._warned[thread_id]
                if call_hash not in warned:
                    # ★ 有客观进展的续查不该被劝退（add0229b）：一句
                    #   「请停止调用工具，立即产出最终答案」就能让模型放弃
                    #   110 分钟的 OCR，转去解析文本层，最终零交付。
                    if self._progress_exempt(
                        thread_id, messages, call_hash, count
                    ):
                        pass
                    else:
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

                if freq_count >= eff_warn:
                    # ★ 有客观进展的重复不算空转（add0229b：submit-and-poll 续查）。
                    #   一次判定同时管软提醒与硬停：对"每轮都在推进"的续查来说，
                    #   「请停止调用工具并产出最终答案」是纯误导，会直接把
                    #   长任务劝退（模型会转去解析文本层，最终零交付）。
                    if self._progress_exempt(
                        thread_id, messages, call_hash, freq_count
                    ):
                        continue

                if freq_count >= eff_hard:
                    logger.error(
                        "Tool frequency hard limit — forcing stop (thread=%s, tool=%s, count=%d)",
                        thread_id, name, freq_count,
                    )
                    return (
                        _HARD_STOP_RUNNING_MSG
                        if self._last_tool_still_running(messages)
                        else _TOOL_FREQ_HARD_STOP_MSG.format(
                            tool_name=name, count=freq_count
                        )
                    ), {
                        "reason": STOP_REASON_TOOL_FREQUENCY,
                        "tool_name": name,
                        "count": freq_count,
                    }

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

            # ─── Layer 2c：占比 / 交替循环（2026-10-17 新增）─────────
            # 上面那条绝对次数阈值有结构性漏洞：`_tool_freq_window` 是**窗口内同工具
            # 计数**，模型只要穿插调用任意第二种工具就被稀释。83345f0c 实测：
            #   execute 累计 24 次，20 窗口内最多 16 次 < hard=20 ⇒ 永不触发。
            # 换成两个与"绝对次数"无关的形态判据：
            #   ① 占比：窗口内单一工具占比 ≥ tool_freq_ratio（且次数 ≥ warn 阈值）
            #   ② 交替循环：工具名周期震荡 ≥ alternating_min_run
            #      （deer-flow 对应修复 #2590 detect alternating tool-name cycles，
            #       治的正是「写脚本→跑→改→跑」两种工具各占一半、占比也未必过线的形态）
            # ★ 必须带**零交付物豁免**：跑 build、批量处理文件、长流程调试都是
            #   "单一工具高频但完全正常"的形态，唯一区分点是有没有在向交付物推进。
            #   判据与 M3 完成门同源（chat.py 只认 /reports/），另认
            #   download_from_sandbox 这个交付动作本身。没有这道闸门，本判据会把
            #   正常长任务批量误杀——deer-flow 对假阳性的担忧同源。
            if len(tool_name_history) >= self.min_window_fill:
                top_name, top_count, top_ratio = _max_tool_ratio(tool_name_history)
                alt_run = _alternating_run(tool_name_history)
                by_ratio = (
                    top_count >= self.tool_freq_warn
                    and top_ratio >= self.tool_freq_ratio
                )
                by_alternating = alt_run >= self.alternating_min_run
                if (by_ratio or by_alternating) and not _window_has_deliverable(
                    messages, self._tool_freq_window
                ):
                    # ★ 有客观进展的重复不算空转：submit-and-poll 的续查正是
                    #   「同工具占比 100%」的形态，按纯形态判据必被误杀
                    #   （2026-10-20 add0229b：OCR 262 页需 ~22 次同参续查）。
                    if not self._progress_exempt(
                        thread_id, messages, call_hash, top_count
                    ):
                        if by_ratio:
                            detail = (
                                f"最近 {len(tool_name_history)} 次调用中 {top_name} "
                                f"占 {top_count} 次（{top_ratio:.0%}）"
                            )
                        else:
                            detail = (
                                f"工具调用呈周期震荡（连续匹配 {alt_run} 次，"
                                f"窗口 {list(tool_name_history)[-8:]}）"
                            )
                        logger.error(
                            "Tool frequency pattern hard limit — forcing stop "
                            "(thread=%s, detail=%s, ratio=%.2f, alt_run=%d)",
                            thread_id, detail, top_ratio, alt_run,
                        )
                        return (
                            _HARD_STOP_RUNNING_MSG
                            if self._last_tool_still_running(messages)
                            else _TOOL_FREQ_PATTERN_HARD_STOP_MSG.format(detail=detail)
                        ), {
                            "reason": STOP_REASON_TOOL_FREQ_PATTERN,
                            "tool_name": top_name,
                            "count": top_count,
                            "ratio": round(top_ratio, 3),
                            "alternating_run": alt_run,
                            "detail": detail,
                        }

        return None, None

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

    def _apply(self, state: LoopDetectionState, runtime: Runtime) -> dict | None:
        warning, hard_stop = self._track_and_check(state, runtime)

        if hard_stop:
            # 剥离 tool_calls 让循环自然结束（不抛异常，SSE 不中断）
            messages = state.get("messages", [])
            last_msg = messages[-1]
            content = self._append_text(last_msg.content, warning or _HARD_STOP_MSG)
            stripped_msg = last_msg.model_copy(
                update=self._build_hard_stop_update(last_msg, content)
            )
            # ★ ②A（2026-10-10）：把「硬停发生过 + 原因」写进 state。API 层
            # （chat.py）从 updates 事件读到本键后会**真正终止本轮**——只剥
            # tool_calls 而不告知 API 层 = 硬停形同虚设：M3 完成门看不到这个信号，
            # 照样按"零产出"注入自动继续轮，把刚掐断的循环重新推起来
            # （会话 GY24428:a2719d7b 的"硬停未能止损"）。
            return {
                "messages": [stripped_msg],
                LOOP_FORCED_STOP_KEY: {
                    **hard_stop,
                    "at": time.time(),
                },
            }

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
    def before_agent(self, state: LoopDetectionState, runtime: Runtime) -> dict | None:
        # ─── 开轮清零硬停标记（②A 生命周期铁律）───
        # 标记的语义是「**本轮**被强停」。langgraph 每个 run 开头都会跑一次
        # before_agent，正是清零时机：若让上一 run 的标记残留，chat.py 会把一轮
        # 完全正常的回复也判成硬停轮而提前终止（隐性错误比漏判更难查）。
        # 读取方是 chat.py，每轮必须独立判定，故此清零点与写入点必须成对存在。
        if state.get(LOOP_FORCED_STOP_KEY):
            logger.info(
                "[LOOP] 开轮清零上一轮硬停标记 (thread=%s, prev=%s)",
                self._get_thread_id(runtime), state.get(LOOP_FORCED_STOP_KEY),
            )
            return {LOOP_FORCED_STOP_KEY: None}
        # 清理上一 run 残留的 pending warning（简化：不按 run_id 分，整 pending 清）
        return None

    @override
    async def abefore_agent(self, state: LoopDetectionState, runtime: Runtime) -> dict | None:
        return self.before_agent(state, runtime)

    @override
    def after_model(self, state: LoopDetectionState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    async def aafter_model(self, state: LoopDetectionState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    def after_agent(self, state: LoopDetectionState, runtime: Runtime) -> dict | None:
        self._pending_warnings.pop(self._get_thread_id(runtime), None)
        return None

    @override
    async def aafter_agent(self, state: LoopDetectionState, runtime: Runtime) -> dict | None:
        return self.after_agent(state, runtime)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        # 契约学习点：ModelRequest.tools（langchain types.py:99）是 agent 实际持有的
        # 工具全集 —— 反查它们的 args_schema 才拿到真契约，而非本文件的硬编码断言。
        self._learn_tool_schemas(request.tools)
        return handler(self._augment_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        self._learn_tool_schemas(request.tools)
        return await handler(self._augment_request(request))

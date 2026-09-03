from dataclasses import dataclass, field, asdict
from typing import Callable
import asyncio
import logging
import re
import time

from langchain_openai import ChatOpenAI
from langchain.agents.middleware import (
    wrap_model_call,
    ModelRequest,
    ModelResponse,
)
from src.core.config import settings

logger = logging.getLogger(__name__)

# 模块级缓存的真实上下文上限：按 (base_url, model_name) 缓存，
# 首次探活 /v1/models 或首次 400 修正后填入，多模型各自独立受益
_MAX_LEN_CACHE: dict[tuple[str, str], int] = {}
# 已知模型的上下文上限小表（fallback：探活失败 / /v1/models 字段缺失时用）
_KNOWN_MAX_LEN: dict[str, int] = {
    "Qwen3.6-35B-A3B": 262144,
}
# ─── 每会话引擎真实 prompt_tokens 缓存（2026-08-31 新增）───
# 替代本地估算：vLLM 每次响应自带 usage.prompt_tokens（含视觉 token，引擎侧计算，
# 100% 准），模型_call_guard 在成功返回后写入这里，供 Summarization 触发 + dynamic
# max_tokens 读取。本地 tiktoken 估算严重低估中文、且对图片视觉 token 一无所知（=0），
# 正是死循环根因的同源问题；引擎报数才是 ground truth。首轮 cold（无缓存）退化本地估算。
_session_prompt_tokens: dict[str, int] = {}


def get_engine_prompt_tokens(session_id: str) -> int | None:
    """返回该会话最近一次模型调用引擎报的真实 prompt_tokens；无缓存（cold 首轮）返回 None。"""
    return _session_prompt_tokens.get(session_id)


_no_usage_warned = False  # 防字段漂移：usage_metadata 缺失仅告警一次（避免刷屏）


def _capture_usage(resp, request) -> None:
    """每次模型成功回复后，把引擎真实 prompt_tokens 存进每会话缓存（含视觉 token）。"""
    global _no_usage_warned
    sid = getattr(request.model, "_session_id", None)
    if not sid:
        return
    um = getattr(resp, "usage_metadata", None)
    if not isinstance(um, dict):
        # 字段漂移告警：stream_usage=True 已开启却无 usage_metadata → 计数退化本地估算。
        # 仅告警一次，足以暴露"引擎真实计数没接上"（OpenInference 0.1.67 字段漂移同类坑）。
        if not _no_usage_warned:
            _no_usage_warned = True
            logger.warning(
                "[DIAG] resp 无 usage_metadata（stream_usage 可能未生效 / 响应层字段漂移）；"
                "本次及后续退化本地估算，引擎真实计数不可用"
            )
        return
    real_in = um.get("input_tokens") or um.get("prompt_tokens")
    if not real_in:
        return
    _session_prompt_tokens[sid] = real_in
    # 简单防泄漏：长驻服务会话数不会过千，超限清一次（仅少量会话退化 cold，可接受）
    if len(_session_prompt_tokens) > 2000:
        _session_prompt_tokens.clear()
    logger.warning("[DIAG] 引擎真实 prompt_tokens=%d (session=%s)", real_in, sid)


# ─── GenAI OTLP metrics 打点（2026-09-03 新增，标准 semconv 命名）───
# 背景：官方 opentelemetry-instrumentation-openai 会为 LLM 调用叠加第二层 gen_ai span
#（langchain OpenInference instrumentor 已包一层 → Phoenix LLM 面板可能计数双算），
# 故采用「标准指标名 + 自研打点」路线（经确认）：
#   gen_ai.client.token.usage        Histogram（无 unit）—— input/output 由属性
#                                    gen_ai.token.type 区分（引擎真实 usage，非估算）
#   gen_ai.client.operation.duration Histogram（unit=s）—— 每次 LLM 调用墙钟耗时
# 属性：gen_ai.system="openai"（全链路 OpenAI 兼容协议：vLLM/Kimi/GLM 同）、
#       gen_ai.request.model、error.type（失败路径）。
# 安全：若 setup_tracing() 未注册 MeterProvider（otel_metrics_enabled=False / endpoint
# 缺失），metrics_api.get_meter 返回 no-op meter → 全部 record 静默丢弃，零成本零风险，
# 绝不影响主链路（与 token 缓存同层旁路，不抛异常）。
_genai_instruments: dict | None = None


def _get_genai_instruments() -> dict:
    """懒取 meter + instrument（并发下重复创建亦无害；opentelemetry-api 缺失时静默返回空）。"""
    global _genai_instruments
    if _genai_instruments is None:
        try:
            from opentelemetry import metrics as metrics_api

            meter = metrics_api.get_meter("geesun.agent", "0.1.0")
            _genai_instruments = {
                "token_usage": meter.create_histogram(
                    name="gen_ai.client.token.usage",
                    description="GenAI 每次调用的 token 消耗（引擎真实 usage）",
                ),
                "duration": meter.create_histogram(
                    name="gen_ai.client.operation.duration",
                    description="GenAI 每次调用的墙钟耗时",
                    unit="s",
                ),
            }
        except Exception:
            _genai_instruments = {}
    return _genai_instruments


def _record_genai_metrics(
    *,
    model_name: str | None,
    duration_s: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    error_type: str | None = None,
) -> None:
    """记录一次 LLM 调用的 gen_ai 指标。成功：token + duration；失败：duration + error.type。"""
    inst = _get_genai_instruments()
    if not inst:
        return
    base = {
        "gen_ai.system": "openai",
        "gen_ai.request.model": model_name or settings.model_name or "unknown",
    }
    if duration_s is not None:
        attrs = dict(base)
        if error_type:
            attrs["error.type"] = error_type
        inst["duration"].record(duration_s, attributes=attrs)
    if input_tokens is not None:
        inst["token_usage"].record(
            input_tokens, attributes={**base, "gen_ai.token.type": "input"}
        )
    if output_tokens is not None:
        inst["token_usage"].record(
            output_tokens, attributes={**base, "gen_ai.token.type": "output"}
        )


# ─── 模型配置（支持多 provider，走 OpenAI 兼容协议） ───

@dataclass
class ModelConfig:
    """运行时动态切换模型的配置。
    
    所有 OpenAI 兼容的 API（vLLM / Kimi / GLM / DeepSeek 等）
    统一走 ChatOpenAI + base_url。
    """
    model_name: str = settings.model_name
    base_url: str = settings.base_url
    api_key: str = settings.openai_api_key


async def create_model() -> ChatOpenAI:
    """默认模型（内网 vLLM Qwen）"""
    # 探活真实上下文上限，注入 model.profile["max_input_tokens"]，
    # 使 SummarizationMiddleware 走 fraction 触发（vLLM 自定义模型名不在 langchain
    # 注册表 → profile 默认 None → fraction 失效，才会落到写死的保守默认）。
    max_len = await asyncio.to_thread(
        resolve_max_len, settings.base_url, settings.model_name, settings.openai_api_key
    )
    return ChatOpenAI(
        base_url=settings.base_url,
        model=settings.model_name,
        api_key=settings.openai_api_key,
        temperature=0,
        max_retries=5,
        timeout=300,
        # 单次调用输出上限：防 thinking 失控无限生成（2026-08-24 实测未设时
        # vLLM 按 max_model_len=262144 无限生成，8.5min/87k tokens 撞 600s 超时）
        max_tokens=settings.model_max_tokens,
        # 注入上下文上限 → SummarizationMiddleware fraction 触发（见上）
        profile={"max_input_tokens": max_len},
        # 流式块间隔空闲超时：只杀静默流，不杀长生成（见 config 注释）
        stream_chunk_timeout=settings.model_stream_chunk_timeout_sec,
        # 流式也返回 usage_metadata（vLLM 响应自带 usage，含视觉 token；Langfuse 可见）
        stream_usage=True,
    )


# ─── 动态模型切换 middleware ───

@wrap_model_call
async def switch_model(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """从 runtime context 读取 ModelConfig，动态切换模型。

    不传 context 或传 None 时走默认模型（内网 vLLM），
    传了 ModelConfig 则用指定模型。
    """
    ctx = request.runtime.context
    raw = ctx.get("model_config") if ctx else None
    if raw is None:
        return await handler(request)

    # 支持 dict 和 ModelConfig 两种传入方式
    if isinstance(raw, dict):
        cfg = ModelConfig(**raw)
    elif isinstance(raw, ModelConfig):
        cfg = raw
    else:
        return await handler(request)

    max_len = await asyncio.to_thread(
        resolve_max_len, cfg.base_url, cfg.model_name, cfg.api_key or "not-used"
    )
    model = ChatOpenAI(
        model=cfg.model_name,
        base_url=cfg.base_url,
        api_key=cfg.api_key or "not-used",
        temperature=0,
        max_tokens=settings.model_max_tokens,  # 与默认模型一致，防超长生成
        # 注入上下文上限 → SummarizationMiddleware fraction 触发（覆盖默认模型时的 profile）
        profile={"max_input_tokens": max_len},
        # 流式块间隔空闲超时：只杀静默流，不杀长生成（见 config 注释）
        stream_chunk_timeout=settings.model_stream_chunk_timeout_sec,
        # 流式也返回 usage_metadata（vLLM 响应自带 usage，含视觉 token；Langfuse 可见）
        stream_usage=True,
    )
    return await handler(request.override(model=model))


# ─── 图片 file block → image_url 转换 middleware ───

@wrap_model_call
async def file_to_image(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """模型请求前把图片 file block 转成 image_url（启用 Qwen 视觉能力）。

    背景（2026-08-12 实测）：Qwen3.6-35B-A3B 支持 image_url part（能看图），
    但不支持 file part（read_file 读图返回 {'type':'file', base64, mime_type}
    → langchain 转 file part → Qwen 501）。本 middleware 在模型调用前：
    - image/* mime 的 file block → image_url block（data:image/...;base64,...）
    - 其他二进制 file block（pdf/xlsx 等）→ 文本占位（防 501，提示走沙箱链路）
    """
    for msg in request.messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        new_blocks = []
        changed = False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "file":
                mime = block.get("mime_type") or ""
                b64 = block.get("base64") or ""
                filename = block.get("filename") or "二进制文件"
                if mime.startswith("image/") and b64:
                    # 图片：转 image_url，Qwen 视觉直接理解
                    new_blocks.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
                else:
                    # 非图片二进制：占位提示（防 501 + 引导正确链路）
                    new_blocks.append({
                        "type": "text",
                        "text": (
                            f"[文件内容省略：{filename}（{mime}）。二进制内容不直接传给模型；"
                            "如需解析请用 decrypt_and_upload_to_sandbox 上传沙箱后处理]"
                        ),
                    })
                changed = True
            else:
                new_blocks.append(block)
        if changed:
            msg.content = new_blocks
    return await handler(request)


# ─── model 调用总时长超时 + 请求量 DIAG（2026-08-19 新增）───

# 动态 max_tokens 的下限（2026-08-24）：数学上恒不超限——prompt 极端接近上限时
# 放弃输出空间保不 400（实际不会发生：Summarization 触发即压缩 keep=10，输入远小于上限）。
# 边距 model_max_tokens_margin 为可调配置（.env MODEL_MAX_TOKENS_MARGIN，默认 16384）。
# 动态 max_tokens 的下限（2026-08-28 由 1 改为 1024）。
# 原为 1 是死循环根因：prompt 因图片 token 被低估而膨胀到 ~80 万 tokens 时，
# effective_max 被 floor 到 1 → max_tokens=1 → 模型几乎零产出（截断在"用户"后无输出）
# → 无 AIMessage → agent 永不结束 → 反复空转。现改为超预算时砍输入（优先图片块）、
# 保底 1024 输出空间，杜绝 floor=1 死循环（见 docs/context-window-fix-draft.md v2）。
_MODEL_MIN_OUTPUT_TOKENS = 1024

# ─── 上下文超限解析 + 探活（2026-08-27 新增）───

def _parse_context_error(text: str) -> tuple[int, int] | None:
    """从 vLLM 400 错误 message 解析 (real_limit, real_input)。

    样例（server.log:411，API 真实返回，100% 可靠）：
      "This model's maximum context length is 262144 tokens.
       However, you requested 200000 output tokens and your prompt contains
       at least 62145 input tokens"
    → 返回 (262144, 62145)。非上下文超限错误返回 None。
    """
    if "context length" not in text and "maximum context" not in text:
        return None
    m_limit = re.search(r"maximum context length is (\d+)", text)
    if not m_limit:
        return None
    limit = int(m_limit.group(1))
    m_input = re.search(r"(\d+) input tokens", text)
    inp = int(m_input.group(1)) if m_input else 0
    return limit, inp


def resolve_max_len(base_url: str, model_name: str, api_key: str) -> int:
    """拿真实上下文上限，按 (base_url, model_name) 缓存（多模型各自独立受益）。

    优先级：① 探活 /v1/models 的 max_model_len 字段（2026-08-28 实测确认 vLLM
    返回该字段，值为 262144）；② fallback 小表 _KNOWN_MAX_LEN；③ config 默认值
    model_max_len。真实上限最终也会由 400 错误 message 解析修正（见 model_call_guard）。
    """
    key = (base_url, model_name)
    if key in _MAX_LEN_CACHE:
        return _MAX_LEN_CACHE[key]
    limit: int = settings.model_max_len  # ③ 兜底
    try:
        import httpx  # langchain_openai 依赖 httpx，必可用
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        with httpx.Client(timeout=5) as c:
            # base_url 可能已含 /v1 后缀（如 http://172.16.66.13:8003/v1），
            # 直接拼 /v1/models 会变双 /v1 → 404（2026-08-31 server.log:83 实测）
            models_url = base_url.rstrip("/")
            if not models_url.endswith("/v1"):
                models_url += "/v1"
            models_url += "/models"
            r = c.get(models_url, headers=headers)
            data = r.json().get("data", [])
            for m in data:
                if m.get("id") == model_name and "max_model_len" in m:
                    limit = int(m["max_model_len"])
                    break
            else:
                # 列表里没精确匹配到本模型名，但首个模型带了上限字段则借用（单模型部署常见）
                if data and "max_model_len" in data[0]:
                    limit = int(data[0]["max_model_len"])
    except Exception as e:
        logger.warning("[DIAG] 探活 model_max_len 失败，走 fallback: %s", e)
    if limit == settings.model_max_len and model_name in _KNOWN_MAX_LEN:
        limit = _KNOWN_MAX_LEN[model_name]  # ② 小表
    _MAX_LEN_CACHE[key] = limit
    if limit != settings.model_max_len:
        logger.warning("[DIAG] 探活 model_max_len=%d (%s)", limit, model_name)
    return limit


# ─── 超预算输入裁剪辅助（2026-08-28 新增，死于 floor=1 死循环的修复）───

def _estimate_tokens(messages) -> int:
    """粗估 token 数（中英混排 ~2 字符/token，保守）。仅用于超预算时的裁剪决策，
    真实上限以 400 解析为准。"""
    return sum(len(str(getattr(m, "content", ""))) // 2 for m in messages)


def _msg_has_image(m) -> bool:
    """消息是否含 image_url 块（视觉 token 占比最大，超预算时优先丢弃）。"""
    content = getattr(m, "content", None)
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "image_url" for b in content)


def _trim_input_for_budget(msgs, max_len, margin, min_output):
    """输入逼近/超上限时砍输入（优先丢含 image_url 的旧消息），直到估算
    prompt + min_output ≤ max_len 或只剩最后 10 条。返回 (trimmed, est_tokens)。"""
    budget = max_len - margin - min_output
    keep = list(msgs)
    # 丢弃优先级：含图消息优先，其次更旧的消息优先（index 小 = 旧）
    order = sorted(range(len(keep)), key=lambda i: (0 if _msg_has_image(keep[i]) else 1, i))
    removed: set[int] = set()
    cur = _estimate_tokens(keep)
    for i in order:
        if cur <= budget or len(keep) - len(removed) <= 10:
            break
        removed.add(i)
        cur = _estimate_tokens([keep[j] for j in range(len(keep)) if j not in removed])
    trimmed = [keep[j] for j in range(len(keep)) if j not in removed]
    if not trimmed:
        trimmed = keep[-10:]
    logger.warning(
        "[DIAG] 输入超预算，砍图/裁剪消息 %d → %d（≈%d tokens）",
        len(msgs), len(trimmed), _estimate_tokens(trimmed),
    )
    return trimmed, _estimate_tokens(trimmed)


@wrap_model_call
async def model_call_guard(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """① 打印本轮实际发送给模型的请求量（messages 数 + 字符数）——用于诊断
    Summarization 压缩是否生效（压缩后应为 keep 条，全量则几百条/几十万字符）；
    ② 动态 max_tokens：vLLM 约束 prompt + max_tokens ≤ model_max_len（262144），
    静态 200000 在大输入时会 400（2026-08-24 实测：输入 62145 + 200000 = 262145
    → BadRequestError → 模型零产出 → M3 拦截）。按输入长度动态收紧，永不超限；
    ③ 超时护盾（2026-08-28 重构）：真正防"静默卡死"靠 ChatOpenAI 的
    stream_chunk_timeout（流式块间隔空闲超时，只杀吐字停了的静默流，不杀长生成，
    见 config model_stream_chunk_timeout_sec）；此处 asyncio.wait_for 仅作灾难性墙钟
    兜底（model_call_timeout_sec 已上调至 1800，避免误杀 200k token 报告），超时抛
    TimeoutError → chat.py 外层捕获 → SSE error → M3 兜底。
    """
    msgs = request.messages
    total_chars = sum(len(str(getattr(m, "content", ""))) for m in msgs)
    total_tokens = sum(
        len(str(getattr(m, "content", ""))) // 2 for m in msgs
    )  # 粗估：中英混排 1 字符≈0.5~1 token，取保守下限
    logger.warning(
        "[DIAG] model_call_guard: messages=%d, chars=%d（≈%d tokens）, timeout=%ss",
        len(msgs), total_chars, total_tokens, settings.model_call_timeout_sec,
    )

    # ─── 动态 max_tokens：按输入长度收紧，保证 prompt+max_tokens ≤ model_max_len ───
    # 优先用引擎真实 prompt_tokens（每会话缓存，含视觉 token，比本地估算准）；
    # 无缓存（首轮/cold）才退化本地估算（get_num_tokens_from_messages 对自定义模型名
    # 会 NotImplementedError，已被 try/except 兜成字符估算，低估图片但 400 兜底会纠正）。
    sid = getattr(request.model, "_session_id", None)
    cached = _session_prompt_tokens.get(sid) if sid else None
    if cached:
        prompt_tokens = cached
        logger.debug("[DIAG] prompt_tokens 用引擎真实值=%d (session=%s)", prompt_tokens, sid)
    else:
        prompt_tokens = 0
        try:
            sys_msg = request.system_message
            prompt_msgs = ([sys_msg] if sys_msg is not None else []) + list(msgs)
            prompt_tokens = request.model.get_num_tokens_from_messages(prompt_msgs)
        except Exception:
            # 回退：中英混排 ~2 字符/token（偏保守，宁小勿超）
            prompt_tokens = total_chars // 2
    effective_max = min(
        settings.model_max_tokens,
        settings.model_max_len - prompt_tokens - settings.model_max_tokens_margin,
    )
    # 不再 floor 到 1：输入逼近/超上限时不锁死输出空间（曾导致死循环：max_tokens=1
    # → 模型零产出 → 无 AIMessage → 永不结束，见 docs/context-window-fix-draft.md v2）。
    # 改为砍输入（优先图片块）后保底 _MODEL_MIN_OUTPUT_TOKENS 输出空间。
    if effective_max < _MODEL_MIN_OUTPUT_TOKENS:
        msgs, prompt_tokens = _trim_input_for_budget(
            msgs, settings.model_max_len, settings.model_max_tokens_margin,
            _MODEL_MIN_OUTPUT_TOKENS,
        )
        try:
            request = request.override(messages=msgs)
        except Exception:
            # ModelRequest.override(messages=...) 不支持时退化为仅收紧 max_tokens
            # （实际输入仍大，将由下方 400 兜底重试纠正）
            logger.warning("[DIAG] override(messages=...) 不支持，退化仅收紧 max_tokens")
        effective_max = max(
            _MODEL_MIN_OUTPUT_TOKENS,
            settings.model_max_len - prompt_tokens - settings.model_max_tokens_margin,
        )
    if effective_max < settings.model_max_tokens:
        logger.warning(
            "[DIAG] 动态 max_tokens: prompt≈%d, 上限收紧 %d → %d",
            prompt_tokens, settings.model_max_tokens, effective_max,
        )
    # model_settings 会被 langchain factory 以 model.bind(**model_settings) 传入
    request = request.override(model_settings={
        **request.model_settings,
        "max_tokens": effective_max,
    })

    # 探活真实上限（同步、按 (base_url,model_name) 缓存，失败保留默认，不阻断）
    try:
        base = getattr(request.model, "openai_api_base", "") or settings.base_url
        mname = getattr(request.model, "model_name", "") or settings.model_name
        akey = getattr(request.model, "openai_api_key", "") or settings.openai_api_key
        resolved = await asyncio.to_thread(resolve_max_len, base, mname, akey)
        if resolved != settings.model_max_len:
            settings.model_max_len = resolved
    except Exception:
        pass

    # ─── 400 兜底：上下文超限 → 解析真实上限 → 收紧 max_tokens/trim → 重试 ───
    # 这是唯一能扛住「token 计数低估中文」+「model_max_len 写死不准」的硬保险：
    # 直接吃 API 自己的判决，不依赖任何本地假设，也**不依赖 deepagents 的
    # ContextOverflowError 兜底**（已确认是死代码，见 docs/context-window-fix-draft.md）。
    # 捕获用 `except Exception` + message 关键字，而非 `from openai import BadRequestError`
    # 类捕获——避免异常经 langchain 多层包装后类身份丢失。最多重试 2 次，防无限循环。
    _MAX_CTX_RETRIES = 2
    attempt = 0
    while True:
        t0 = time.perf_counter()  # GenAI 指标：仅统计本次尝试的墙钟耗时（重试间不计）
        try:
            resp = await asyncio.wait_for(
                handler(request), timeout=settings.model_call_timeout_sec
            )
        except asyncio.TimeoutError:
            _record_genai_metrics(
                model_name=getattr(request.model, "model_name", None),
                duration_s=time.perf_counter() - t0,
                error_type="timeout",
            )
            raise TimeoutError(
                f"model 调用超过 {settings.model_call_timeout_sec}s 灾难性总时长兜底（"
                f"messages={len(msgs)}, chars={total_chars}）——生成超长或引擎无响应，已中止"
            ) from None
        except Exception as e:
            parsed = _parse_context_error(str(e))
            if parsed is None or attempt >= _MAX_CTX_RETRIES:
                # 非上下文超限错误，或重试耗尽 → 原样上抛（由 chat.py/M3 处理）；
                # 失败也记 duration（带 error.type），避免"只统计成功"高估耗时均值
                _record_genai_metrics(
                    model_name=getattr(request.model, "model_name", None),
                    duration_s=time.perf_counter() - t0,
                    error_type=type(e).__name__,
                )
                raise  # 非上下文超限错误，或重试耗尽 → 原样上抛（由 chat.py/M3 处理）
            real_limit, real_input = parsed
            # 用 API 真实上限修正 settings（后续所有调用永久受益）
            settings.model_max_len = real_limit
            logger.warning(
                "[DIAG] 400 上下文超限 → 收紧重试 #%d: real_limit=%d, real_input=%d",
                attempt + 1, real_limit, real_input,
            )
            # 重算 max_tokens：留 margin，保证 input + max_tokens ≤ real_limit
            safe_max = max(
                _MODEL_MIN_OUTPUT_TOKENS,
                real_limit - real_input - settings.model_max_tokens_margin,
            )
            # 极端情况：input 本身就逼近/超过总上限，连 safe_max 都 ≤0 → 必须砍 input
            if safe_max <= _MODEL_MIN_OUTPUT_TOKENS:
                keep = max(4, len(msgs) - (attempt + 1) * 10)
                trimmed = msgs[-keep:]
                try:
                    request = request.override(messages=trimmed)
                    msgs = trimmed
                except Exception:
                    # ModelRequest.override(messages=...) 不支持时退化为仅收紧 max_tokens
                    logger.warning("[DIAG] override(messages=...) 不支持，退化仅收紧 max_tokens")
                safe_max = max(
                    _MODEL_MIN_OUTPUT_TOKENS,
                    real_limit - real_input // 2 - settings.model_max_tokens_margin,
                )
            request = request.override(model_settings={
                **request.model_settings,
                "max_tokens": safe_max,
            })
            attempt += 1
            continue
        # 成功路径（未抛异常）：捕获引擎真实 prompt_tokens 回写每会话缓存，再返回
        _capture_usage(resp, request)
        # GenAI 指标打点（标准 semconv 名；metrics 未启用时 no-op 静默，零风险）
        _um = getattr(resp, "usage_metadata", None)
        if not isinstance(_um, dict):
            _um = {}  # usage_metadata 缺失/漂移 → 仅记 duration，token 维度不记
        _record_genai_metrics(
            model_name=getattr(request.model, "model_name", None),
            duration_s=time.perf_counter() - t0,
            input_tokens=_um.get("input_tokens") or _um.get("prompt_tokens"),
            output_tokens=_um.get("output_tokens") or _um.get("completion_tokens"),
        )
        return resp

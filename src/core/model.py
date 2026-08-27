from dataclasses import dataclass, field, asdict
from typing import Callable
import asyncio
import logging
import re

from langchain_openai import ChatOpenAI
from langchain.agents.middleware import (
    wrap_model_call,
    ModelRequest,
    ModelResponse,
)
from src.core.config import settings

logger = logging.getLogger(__name__)

# 模块级缓存的真实上下文上限（首次探活 / 首次 400 修正后填入，全局受益）
_probed_max_len: int | None = None


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


def create_model() -> ChatOpenAI:
    """默认模型（内网 vLLM Qwen）"""
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

    model = ChatOpenAI(
        model=cfg.model_name,
        base_url=cfg.base_url,
        api_key=cfg.api_key or "not-used",
        temperature=0,
        max_tokens=settings.model_max_tokens,  # 与默认模型一致，防超长生成
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
_MODEL_MIN_OUTPUT_TOKENS = 1

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


async def probe_model_max_len() -> int:
    """启动探活 /v1/models 拿真实上下文上限（best-effort）。

    ⚠️ vLLM /v1/models 是否含 max_model_len 字段需实测确认；
       解析失败 / 字段缺失 → 保留 config 默认值，不影响主流程。
       真实上限最终以 400 错误 message 解析为准（见 model_call_guard）。
    """
    global _probed_max_len
    if _probed_max_len is not None:
        return _probed_max_len
    try:
        import httpx  # langchain_openai 依赖 httpx，必可用
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.base_url.rstrip('/')}/v1/models")
            data = r.json().get("data", [])
            if data and "max_model_len" in data[0]:
                _probed_max_len = int(data[0]["max_model_len"])
                settings.model_max_len = _probed_max_len
                logger.warning("[DIAG] 探活 model_max_len=%d", _probed_max_len)
    except Exception as e:
        logger.warning("[DIAG] 探活 model_max_len 失败，用默认值: %s", e)
    return _probed_max_len or settings.model_max_len


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
    ③ 总时长超时：openai SDK 的 timeout 是 httpx"字节间隔"超时，vLLM 慢速
    流式时永不触发（2026-08-19 实测 16.8 万 token prefill 挂 20 分钟无超时），
    这里用 asyncio.wait_for 包整个 handler 做总时长兜底（config 可调），
    超时抛 TimeoutError → chat.py 外层捕获 → SSE error → M3 兜底，防永久挂起。
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
    effective_max = max(effective_max, _MODEL_MIN_OUTPUT_TOKENS)
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

    # lazy 探活：首次调用时 best-effort 拿真实上限（失败保留默认，不阻断）
    if _probed_max_len is None:
        try:
            await probe_model_max_len()
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
        try:
            return await asyncio.wait_for(
                handler(request), timeout=settings.model_call_timeout_sec
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"model 调用超过 {settings.model_call_timeout_sec}s 总时长（"
                f"messages={len(msgs)}, chars={total_chars}）——生成超长或引擎无响应，已中止"
            ) from None
        except Exception as e:
            parsed = _parse_context_error(str(e))
            if parsed is None or attempt >= _MAX_CTX_RETRIES:
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

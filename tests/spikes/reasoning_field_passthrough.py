"""spike：ReasoningChatOpenAI 推理字段透传（流式 + 非流式双钩子）。

背景（2026-09-10）：
langchain-openai 1.2.1 只按官方 OpenAI 规范消费响应——第三方扩展推理字段被静默丢弃。
项目子类原先只覆盖了**流式**钩子 `_convert_chunk_to_generation_chunk`；非流式路径
（invoke/ainvoke → `_create_chat_result` → **模块级** `_convert_dict_to_message`）没有
钩子 → ainvoke 拿不到 thinking（实测：astream=603 字符 / ainvoke=0 字符）。

本 spike 用 `ast.get_source_segment` 从 src/core/model.py **提取真实类定义**执行
（非复刻——复刻版发现不了源码被改坏），验证：

1. 流式钩子：delta 里的 reasoning / reasoning_content / reasoning_details 都能写回
   additional_kwargs["reasoning_content"]
2. 非流式钩子：message 里的同名字段同样能写回
3. 两条路径产出的 key 一致（下游 chat.py / 前端无需分支）
4. 空值 / 非字符串 / 无字段时不写入（保持 falsy 语义，不污染流内容）
5. 非 AIMessage 的 generation 被安全跳过

运行：
    python tests/spikes/reasoning_field_passthrough.py           # 纯逻辑（无网络）
    python tests/spikes/reasoning_field_passthrough.py --live    # 额外真连 vLLM 验证
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI

MODEL_PY = Path(__file__).resolve().parents[2] / "src" / "core" / "model.py"

REASONING_KEY = "reasoning_content"


def load_real_class():
    """从真实源码 AST 提取 ReasoningChatOpenAI（非复刻）"""
    src = MODEL_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ReasoningChatOpenAI":
            cls_src = ast.get_source_segment(src, node)
            assert cls_src, "AST 提取类源码失败"
            ns = {
                "ChatOpenAI": ChatOpenAI,
                "AIMessage": AIMessage,
                "AIMessageChunk": AIMessageChunk,
            }
            exec(compile(cls_src, str(MODEL_PY), "exec"), ns)  # noqa: S102
            return ns["ReasoningChatOpenAI"], cls_src
    raise RuntimeError(f"{MODEL_PY} 中未找到 ReasoningChatOpenAI 类定义")


def make_llm(cls, **kw):
    """构造实例：不联网也能构造（仅 _create_chat_result/_convert_chunk 是纯转换）"""
    return cls(
        model=kw.get("model", "test-model"),
        base_url=kw.get("base_url", "http://127.0.0.1:1/v1"),
        api_key=kw.get("api_key", "sk-test"),
        temperature=0,
    )


def fake_chunk(reasoning_field: str | None, value=None, *, content: str | None = None) -> dict:
    """构造 OpenAI 流式 chunk dict（delta 内可选带推理字段）"""
    delta: dict = {"role": "assistant"}
    if reasoning_field is not None:
        delta[reasoning_field] = value
    if content is not None:
        delta["content"] = content
    return {
        "id": "chatcmpl-spike",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


def fake_response(reasoning_field: str | None, value=None, *, content: str = "1 + 1 = 2") -> dict:
    """构造 OpenAI 非流式 response dict（message 内可选带推理字段）"""
    message: dict = {"role": "assistant", "content": content}
    if reasoning_field is not None:
        message[reasoning_field] = value
    return {
        "id": "chatcmpl-spike",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


# ────────────────────────────────────────────────────────────────────────
# 用例
# ────────────────────────────────────────────────────────────────────────

def run_cases(cls) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, cond, detail))

    llm = make_llm(cls)

    # ── 流式钩子：三种字段名都要能取到 ──
    for field in ("reasoning", "reasoning_content", "reasoning_details"):
        gc = llm._convert_chunk_to_generation_chunk(  # noqa: SLF001
            fake_chunk(field, "THINK-A"), AIMessageChunk, None
        )
        got = (gc.message.additional_kwargs or {}).get(REASONING_KEY) if gc else None
        check(
            f"流式钩子：delta.{field} → additional_kwargs[{REASONING_KEY}]",
            got == "THINK-A",
            f"got={got!r}",
        )

    # ── 非流式钩子：三种字段名都要能取到（本次修复点）──
    for field in ("reasoning", "reasoning_content", "reasoning_details"):
        result = llm._create_chat_result(fake_response(field, "THINK-B"))  # noqa: SLF001
        msg = result.generations[0].message
        got = (msg.additional_kwargs or {}).get(REASONING_KEY)
        check(
            f"非流式钩子：message.{field} → additional_kwargs[{REASONING_KEY}]",
            got == "THINK-B",
            f"got={got!r}",
        )

    # ── 两条路径 key 一致（下游无需分支）──
    gc = llm._convert_chunk_to_generation_chunk(  # noqa: SLF001
        fake_chunk("reasoning", "SAME"), AIMessageChunk, None
    )
    stream_keys = set((gc.message.additional_kwargs or {}).keys()) if gc else set()
    result = llm._create_chat_result(fake_response("reasoning", "SAME"))  # noqa: SLF001
    nonstream_keys = set((result.generations[0].message.additional_kwargs or {}).keys())
    check(
        "两条路径产出的 key 集合一致",
        stream_keys == nonstream_keys and REASONING_KEY in stream_keys,
        f"stream={sorted(stream_keys)} nonstream={sorted(nonstream_keys)}",
    )

    # ── 边界：空串 / None / 缺字段 → 不写入 ──
    for field, val, desc in [
        ("reasoning", "", "空字符串"),
        ("reasoning", None, "None"),
        (None, None, "缺字段"),
    ]:
        gc = llm._convert_chunk_to_generation_chunk(  # noqa: SLF001
            fake_chunk(field, val), AIMessageChunk, None
        )
        got = (gc.message.additional_kwargs or {}).get(REASONING_KEY) if gc else None
        check(f"流式边界：{desc} → 不写入", got is None, f"got={got!r}")

        result = llm._create_chat_result(fake_response(field, val))  # noqa: SLF001
        got2 = (result.generations[0].message.additional_kwargs or {}).get(REASONING_KEY)
        check(f"非流式边界：{desc} → 不写入", got2 is None, f"got={got2!r}")

    # ── 边界：非字符串值（如 list）不写入（防 str+=list 类事故）──
    gc = llm._convert_chunk_to_generation_chunk(  # noqa: SLF001
        fake_chunk("reasoning", ["a", "b"]), AIMessageChunk, None
    )
    got = (gc.message.additional_kwargs or {}).get(REASONING_KEY) if gc else None
    check("流式边界：非字符串值不写入", got is None, f"got={got!r}")

    # ── 边界：非流式 content 仍正确保留（钩子不破坏原有字段）──
    result = llm._create_chat_result(fake_response("reasoning", "T", content="正文X"))  # noqa: SLF001
    check(
        "非流式钩子不破坏 content",
        result.generations[0].message.content == "正文X",
        f"content={result.generations[0].message.content!r}",
    )

    # ── 边界：choices 为空 → 不崩 ──
    try:
        empty = {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        llm._create_chat_result(empty)  # noqa: SLF001
        check("非流式边界：choices 为空不崩", True)
    except Exception as e:  # noqa: BLE001
        check("非流式边界：choices 为空不崩", False, f"{type(e).__name__}: {e}")

    return results


# ────────────────────────────────────────────────────────────────────────
# 可选：真连 vLLM
# ────────────────────────────────────────────────────────────────────────

async def run_live(cls) -> list[tuple[str, bool, str]]:
    """真连后端验证（从项目 settings 读配置，不硬编码 key）"""
    out: list[tuple[str, bool, str]] = []
    try:
        sys.path.insert(0, str(MODEL_PY.parents[2]))
        from src.core.config import settings  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        out.append(("真连：加载 settings", False, f"{type(e).__name__}: {e}"))
        return out

    base_url = settings.base_url
    api_key = settings.openai_api_key
    model = settings.model_name
    if not base_url or not api_key:
        out.append(("真连：settings 配置", False, "base_url/api_key 为空，跳过"))
        return out

    llm = cls(model=model, base_url=base_url, api_key=api_key, temperature=0, timeout=120)

    try:
        s_acc = ""
        async for chunk in llm.astream("1+1=?"):
            v = (getattr(chunk, "additional_kwargs", None) or {}).get(REASONING_KEY)
            if v:
                s_acc += v
        out.append(("真连：astream 拿到 reasoning_content", bool(s_acc), f"{len(s_acc)} 字符"))

        resp = await llm.ainvoke("1+1=?")
        a_val = (getattr(resp, "additional_kwargs", None) or {}).get(REASONING_KEY) or ""
        out.append(("真连：ainvoke 拿到 reasoning_content", bool(a_val), f"{len(a_val)} 字符"))
    except Exception as e:  # noqa: BLE001
        out.append(("真连：调用后端", False, f"{type(e).__name__}: {str(e)[:120]}"))

    return out


def main() -> int:
    print("spike: ReasoningChatOpenAI 推理字段透传（流式 + 非流式）")
    print(f"源码: {MODEL_PY}")

    cls, cls_src = load_real_class()
    has_stream = "_convert_chunk_to_generation_chunk" in cls_src
    has_nonstream = "_create_chat_result" in cls_src
    print(f"\n[源码检查] 流式钩子={'有' if has_stream else '无'}  "
          f"非流式钩子={'有' if has_nonstream else '无'}")
    if not has_stream:
        print("[致命] 缺少流式钩子"); return 1

    print("\n--- 纯逻辑用例（无网络）---")
    results = run_cases(cls)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))

    if "--live" in sys.argv:
        print("\n--- 真连后端 ---")
        live = asyncio.run(run_live(cls))
        for name, ok, detail in live:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
        results += live

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    print(f"\n结果: {passed} PASS / {failed} FAIL（共 {len(results)} 例）")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

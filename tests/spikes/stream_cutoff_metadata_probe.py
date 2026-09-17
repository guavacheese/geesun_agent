# -*- coding: utf-8 -*-
"""Spike: 「流式掐断」时写入的 metadata 能否活到聚合后的 AIMessage（L1 方案前提 #2）。

背景：L1 方案（应用侧检测到思考复读 → 掐断流）的落地链路是：
  ① 覆盖 ReasoningChatOpenAI._astream，逐 chunk 判定复读
  ② 命中后往当前 chunk 写 `generation_info["finish_reason"]="length"`
     与 `additional_kwargs["thinking_loop_cutoff"]="1"`，再 break（靠 aclosing 关流）
  ③ 下游**零新增代码**复用 P0 已验证的空响应守卫：`classify_empty_response`
     读 finish_reason → BRANCH_THINKING_TRUNCATED → 注入恢复提示重试

其中 ③ 能白嫖的前提是：**守卫读的是聚合后的 AIMessage，而我们在单 chunk 上写的 metadata
必须能穿过 ChatGenerationChunk → AIMessageChunk → `+` 聚合 这条链活下来**。
本探针就是验证这条链（生产里 reasoning_content 已证明能活，但 finish_reason 的组合未验）。

判据：
  PASS-A 聚合结果的 response_metadata["finish_reason"] == "length"（掐断信号送达）
  PASS-B 聚合结果的 additional_kwargs["thinking_loop_cutoff"] == "1"（自定义标记送达）
  PASS-C 聚合 content 非空（断点前已流出的正文/思考被保留，没被丢）
  PASS-D 掐断后立即停止收流（chunk 数 == 设定值，没有多余 chunk 漏出）

运行（本机可直连生产 vLLM，依赖与项目锁版本对齐：langchain-core 1.4.9 / langchain-openai 1.2.1）：
  set -a; . /d/workspace/geesun_agent/.env; set +a
  /c/Users/GY24428/.workbuddy/binaries/python/envs/lcprobe/Scripts/python.exe \
      tests/spikes/stream_cutoff_metadata_probe.py
"""
from __future__ import annotations

import asyncio
import os

from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_openai import ChatOpenAI


def _env(name: str, default: str = "") -> str:
    raw = os.environ.get(name) or default
    return raw.replace("\r", "").replace("\n", "").strip().strip('"').strip("'")


# 沙箱/容器常注入 HTTP_PROXY 与指向不存在文件的 SSL_CERT_FILE：
# 前者把内网直连打歪，后者让 httpx 建 SSL context 直接 FileNotFoundError。
# 生产容器无此问题，这里只为让探针能跑在本机。
for _k, _v in list(os.environ.items()):
    if _k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(_k, None)
    elif _k in ("SSL_CERT_FILE", "SSL_CERT_DIR") and _v and not os.path.exists(_v):
        os.environ.pop(_k, None)


BASE = _env("BASE_URL", "http://172.16.66.13:8003/v1").rstrip("/")
KEY = _env("OPENAI_API_KEY")
MODEL = _env("MODEL_NAME", "Qwen3.6-35B-A3B")
CUT_AT = int(os.environ.get("CUT_AT") or "10")   # 收到第几个 chunk 时模拟「检测到复读，掐断」

QUESTION = (
    "请写一份 9000 字的中国制造业智能制造转型深度报告，分十个章节，"
    "每章都要有数据支撑与案例分析，并在章末给出可执行建议。"
)


class CutoffChatOpenAI(ChatOpenAI):
    """最小复刻 L1 的掐断动作：在 _astream 里改写 chunk metadata 后 break。

    注：这里用原生 ChatOpenAI + 关思考（enable_thinking=False）而不是复刻项目里的
    ReasoningChatOpenAI —— 原生 ChatOpenAI 会丢弃 reasoning_content 字段，思考阶段
    chunk 全是空的，无法验证「断点前内容保留」。关思考后模型直接产正文，前 N 个 chunk
    即有 content，判据干净且不引入项目依赖；metadata 通道与是否关思考无关。
    """

    cut_at: int = CUT_AT
    seen: int = 0
    wrote_marker: bool = False

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            self.seen += 1
            if self.seen >= self.cut_at:
                chunk.generation_info = dict(chunk.generation_info or {})
                chunk.generation_info["finish_reason"] = "length"
                chunk.message.additional_kwargs["thinking_loop_cutoff"] = "1"
                self.wrote_marker = True
                yield chunk
                break          # 模拟 aclosing 关闭内层 httpx 流
            yield chunk


async def main() -> int:
    llm = CutoffChatOpenAI(
        model=MODEL, api_key=KEY, base_url=BASE, temperature=0.6,
        max_tokens=2000, stream_usage=bool(os.environ.get("STREAM_USAGE")),
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    print("model =", MODEL, "| base_url =", BASE)
    print("计划在第 %d 个 chunk 处掐断\n" % CUT_AT)

    agg: AIMessageChunk | None = None
    chunks = 0
    async for c in llm.astream([HumanMessage(content=QUESTION)]):
        chunks += 1
        rc = (c.additional_kwargs or {}).get("reasoning_content") or ""
        if os.environ.get("SHOW_CHUNKS"):
            print("  chunk#%02d content=%3d reasoning=%4d finish=%r keys=%s"
                  % (chunks, len(c.content or ""), len(rc),
                     (c.response_metadata or {}).get("finish_reason"),
                     sorted((c.additional_kwargs or {}).keys())))
        agg = c if agg is None else agg + c

    assert agg is not None, "没有收到任何 chunk"
    fr = (agg.response_metadata or {}).get("finish_reason")
    marker = (agg.additional_kwargs or {}).get("thinking_loop_cutoff")
    text = agg.content or ""
    reasoning = (agg.additional_kwargs or {}).get("reasoning_content") or ""

    print("=" * 76)
    print("聚合结果")
    print("=" * 76)
    print("  实际收到 chunk 数            = %d（预期 %d）" % (chunks, CUT_AT))
    print("  response_metadata            = %s" % agg.response_metadata)
    print("  additional_kwargs 键         = %s" % list((agg.additional_kwargs or {}).keys()))
    print("  finish_reason                = %r" % fr)
    print("  thinking_loop_cutoff         = %r" % marker)
    print("  聚合 content 长度            = %d" % len(text))
    print("  聚合 reasoning 长度          = %d" % len(reasoning))
    print("  聚合文本尾部                 = %r" % (text or reasoning)[-80:])

    print("\n" + "=" * 76)
    print("判读")
    print("=" * 76)
    a = fr == "length"
    b = marker == "1"
    c = (len(text) + len(reasoning)) > 0
    d = chunks in (CUT_AT, CUT_AT + 1)      # +1：langchain-core 流尾固定追加 chunk_position="last" 空 chunk
    print("  [%s] A response_metadata.finish_reason == 'length'（掐断信号送达守卫）"
          % ("PASS" if a else "FAIL"))
    print("  [%s] B additional_kwargs.thinking_loop_cutoff == '1'（自定义标记送达）"
          % ("PASS" if b else "FAIL"))
    print("  [%s] C 聚合文本非空（断点前内容保留，content=%d + reasoning=%d）"
          % ("PASS" if c else "FAIL", len(text), len(reasoning)))
    print("  [%s] D 掐断后未漏出多余 chunk（%d；预期 %d 或 %d +langchain 尾部空 chunk）"
          % ("PASS" if d else "FAIL", chunks, CUT_AT, CUT_AT))

    ok = a and b and c and d
    print()
    if ok:
        print("  ⇒ 前提 #2 成立：逐 chunk 写入的 metadata 能活到聚合 AIMessage，")
        print("     L1 掐断后可零改动复用 P0 空响应守卫（BRANCH_THINKING_TRUNCATED 分支）。")
    else:
        print("  ⇒ 前提 #2 不成立：metadata 在聚合中被吞。需要改成在 break 前额外显式")
        print("     构造终止 chunk，或让守卫改读自定义通道（如 additional_kwargs 直传）。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

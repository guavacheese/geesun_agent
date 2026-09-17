"""探针：验证「关思考」参数能否经 model_settings 通道到达 HTTP 请求体。

背景
    方案 A 要让「最后一次重试」带上 `chat_template_kwargs={"enable_thinking": false}`，
    但模型调用侧（src/core/model.py）**没有任何 extra_body / chat_template_kwargs 通道**
    （grep 零命中）。唯一可用通道是中间件侧改 `request.model_settings`。

    源码链（从本仓 .venv 里逐段读出，非推测）：
      langchain/agents/factory.py:1404   return request.model.bind(**request.model_settings)
      langchain/agents/factory.py:1391/1400   bind_tools(..., **request.model_settings)
      langchain_openai/chat_models/base.py   payload = {**self._default_params, **kwargs}
      langchain_openai/chat_models/base.py:1573   response = self.client.create(**payload)

    推理上成立，但**推理不是证据** —— langchain-openai 完全没有「extra_body」这个词
    （grep 零命中），它只是把未知 kwargs 原样塞进 payload 交给 OpenAI SDK；
    SDK 是否把 `extra_body` 当特殊参数展平进请求体，必须实测。

判据（用本地 mock server 捕获真实请求体，不依赖 vLLM）
    A  对照组（不 bind）    → 请求体顶层【没有】 chat_template_kwargs
    B  实验组（bind）      → 请求体顶层【有】 chat_template_kwargs
    C  实验组的值         → 正是 {"enable_thinking": false}（落顶层、未被嵌套）
    D  SDK 展平行为        → 顶层不留 `extra_body` 键（说明 SDK 识别并展平了它）

跑法（需 langchain-openai，本机隔离环境即可，无需 vLLM / 容器 / SSH）：
    "C:/Users/GY24428/.workbuddy/binaries/python/envs/lcprobe/Scripts/python.exe" \
        tests/spikes/extra_body_channel_probe.py
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# 沙箱注入的代理 / 指向不存在文件的 SSL_CERT_FILE 会干扰 httpx（报错是
# FileNotFoundError 而非 ConnectError，极易误判成路径问题）——先清掉。
for _k in (
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy",
):
    os.environ.pop(_k, None)

import httpx  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

CAPTURED: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    """记录收到的请求体，并返回一个最小的合法 chat completion 响应。"""

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            payload = json.loads(raw.decode("utf-8"))
            CAPTURED.append(payload)
        except Exception as exc:  # noqa: BLE001
            payload = {}
            CAPTURED.append({
                "_parse_error": str(exc),
                "_raw": raw[:400].decode("utf-8", "ignore"),
            })
        # 生产走的是**流式**（astream）路径，必须单独覆盖：`_get_request_payload` 虽然
        # 两者共用，但 SDK 侧的 stream 分支是独立代码路径 —— 「非流式可用」不能靠推理
        # 外推到线上。
        if payload.get("stream"):
            self._send_stream()
        else:
            self._send_json()

    def _send_json(self) -> None:
        body = json.dumps({
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": "mock-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream(self) -> None:
        """SSE 流式响应（HTTP/1.0：不设 Content-Length，连接关闭即结束）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, finish in (({"role": "assistant", "content": "ok"}, None), ({}, "stop")):
            chunk = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "mock-model",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args):  # 静音访问日志
        return


def start_mock() -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


async def run_case(base_url: str, *, extra_body: dict | None, stream: bool = False) -> dict:
    """发一次请求，返回 mock server 捕获到的请求体。"""
    CAPTURED.clear()
    llm = ChatOpenAI(
        model="mock-model",
        api_key="not-used",
        base_url=base_url,
        temperature=0,
        max_retries=0,
        stream_usage=True,
        http_client=httpx.Client(trust_env=False, timeout=30),
    )
    target = llm.bind(extra_body=extra_body) if extra_body is not None else llm
    if stream:
        async for _chunk in target.astream([HumanMessage(content="hi")]):
            pass
    else:
        await target.ainvoke([HumanMessage(content="hi")])
    return CAPTURED[0] if CAPTURED else {"_empty": "mock 未收到任何请求"}


async def main() -> None:
    srv, base = start_mock()
    print("mock server 已启动：%s" % base)
    qm_body = {"chat_template_kwargs": {"enable_thinking": False}}
    try:
        ctrl = await run_case(base, extra_body=None)
        exp = await run_case(base, extra_body=qm_body)
        strm = await run_case(base, extra_body=qm_body, stream=True)
    finally:
        srv.shutdown()

    print()
    print("=" * 78)
    print("对照组（不 bind extra_body）—— 请求体顶层键")
    print("=" * 78)
    print("  %s" % ", ".join(sorted(ctrl.keys())))

    print()
    print("=" * 78)
    print("实验组（bind extra_body）—— 请求体顶层键")
    print("=" * 78)
    print("  %s" % ", ".join(sorted(exp.keys())))

    print()
    print("=" * 78)
    print("实验组·流式（生产实际路径 astream）—— 请求体顶层键")
    print("=" * 78)
    print("  %s" % ", ".join(sorted(strm.keys())))

    a = "chat_template_kwargs" not in ctrl
    b = "chat_template_kwargs" in exp
    c = (
        isinstance(exp.get("chat_template_kwargs"), dict)
        and exp["chat_template_kwargs"].get("enable_thinking") is False
    )
    d = "extra_body" not in exp  # 期望 SDK 展平内容、而不是原样留个 extra_body 键
    e = (
        strm.get("stream") is True
        and isinstance(strm.get("chat_template_kwargs"), dict)
        and strm["chat_template_kwargs"].get("enable_thinking") is False
    )

    print()
    print("=" * 78)
    print("判读")
    print("=" * 78)
    print("  [%s] A 对照组顶层无 chat_template_kwargs（证明实验组差异不是默认行为）"
          % ("PASS" if a else "FAIL"))
    print("  [%s] B 实验组顶层有 chat_template_kwargs（通道打通）"
          % ("PASS" if b else "FAIL"))
    print("  [%s] C 值为 {'enable_thinking': False} 且落顶层（未被嵌套进别的键）"
          % ("PASS" if c else "FAIL"))
    print("  [%s] D 顶层不留 extra_body 键（SDK 已识别并展平）"
          % ("PASS" if d else "FAIL"))
    print("  [%s] E 流式（生产实际路径 astream）同样带 chat_template_kwargs 且 stream=true"
          % ("PASS" if e else "FAIL"))
    print()
    print("  实验组完整请求体（截断 600 字符）：")
    print("  %s" % json.dumps(exp, ensure_ascii=False)[:600])
    print("  流式组完整请求体（截断 600 字符）：")
    print("  %s" % json.dumps(strm, ensure_ascii=False)[:600])

    ok = a and b and c and d and e
    print()
    print("  结论：%s" % (
        "通道成立 —— request.model_settings → bind(extra_body) → 请求体顶层，"
        "「关思考」可按方案 A 实现。"
        if ok else
        "通道不成立 —— 需要换路（例如在 model.py 侧加 extra_body 支持），"
        "不要按原计划改中间件。"
    ))


if __name__ == "__main__":
    asyncio.run(main())

# -*- coding: utf-8 -*-
"""实证：DeepAgents read_file 对 .png 返回的 content block，
经 langchain-openai 序列化后到底是什么形态。

判据：
  - 若转成 {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}} → 图片通道正常，模型能看图
  - 若原样保留 / 变成 text → 图片没传进去，base64 变文本（那才是燃料）
"""
import json

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

# 极小 base64（1x1 png），只为观察序列化形态，不是真实体积
TINY = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

print("=== langchain / langchain-openai 版本 ===")
import importlib.metadata as md
for p in ("langchain", "langchain-core", "langchain-openai"):
    try:
        print("  %-20s %s" % (p, md.version(p)))
    except Exception as e:  # noqa: BLE001
        print("  %-20s ? %s" % (p, e))

model = ChatOpenAI(model="qwen3.6-35b-a3b", api_key="dummy",
                   base_url="http://127.0.0.1:1/v1")

cases = {}

# A. DeepAgents 对 .png 的真实返回形态（middleware/filesystem.py:1133）
cases["A_image_block"] = ToolMessage(
    content_blocks=[{"type": "image", "base64": TINY, "mime_type": "image/png"}],
    name="read_file", tool_call_id="call_a", status="success",
)

# B. model.py file_to_image 期望处理的形态（type=file）
cases["B_file_block"] = ToolMessage(
    content_blocks=[{"type": "file", "base64": TINY, "mime_type": "image/png"}],
    name="read_file", tool_call_id="call_b", status="success",
)

# C. 对照组：user 消息里的 image block
cases["C_user_image"] = HumanMessage(
    content_blocks=[{"type": "text", "text": "看图"},
                    {"type": "image", "base64": TINY, "mime_type": "image/png"}],
)

for name, msg in cases.items():
    print("\n" + "=" * 66)
    print("### %s" % name)
    print("=" * 66)
    try:
        payload = model._get_request_payload([msg])
        m0 = payload["messages"][-1]
        print("role =", m0.get("role"))
        c = m0.get("content")
        if isinstance(c, str):
            print("content 是**字符串**（%d 字符）" % len(c))
            print("  前 300:", c[:300])
            print("  ⇒ 判读：❌ 图片没走 multimodal 通道，base64 变文本")
        elif isinstance(c, list):
            for i, blk in enumerate(c):
                t = blk.get("type")
                print("  block[%d] type=%s" % (i, t))
                if t == "image_url":
                    url = (blk.get("image_url") or {}).get("url", "")
                    print("     url 前缀:", url[:60])
                    print("     ⇒ 判读：✅ 图片走 image_url 通道，vLLM 按 patch 计 token")
                else:
                    print("     raw:", json.dumps(blk, ensure_ascii=False)[:200])
        else:
            print("content =", type(c), str(c)[:200])
    except Exception as e:  # noqa: BLE001
        print("序列化抛错:", type(e).__name__, e)

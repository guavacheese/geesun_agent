"""探针：把 MODEL_MAX_TOKENS 从 65536 提到 256000，安全边界在哪？

背景
----
`.env` MODEL_MAX_TOKENS=65536（2026-08-27 由 200000 降），原因是 server.log 实测
input 62145 + 200000 = 262145 > 262144 → vLLM 400 → 模型零产出。
config.py 注释称「即便 token 计数低估中文 ~3x 也触碰不到 400 线」。

但 65536 不是固定输出值，只是 cap：model_call_guard 会做
    effective_max = min(cap, model_max_len - prompt_tokens - margin)
所以 cap 调大是否安全，**完全取决于 prompt_tokens 准不准**：

  路径 A（有缓存）：_session_prompt_tokens 存引擎真实 usage.prompt_tokens（含视觉 token）
      → 数学上恒不超限，cap 设多大都安全。
  路径 B（cold 首轮 / 服务重启后第一轮）：get_num_tokens_from_messages 对自定义
      模型名 NotImplementedError → 退化为 `total_chars // 2`
      → **这一路径的误差就是唯一风险源**。

本探针只测路径 B 的误差：同一段中文，比引擎真值与 `chars // 2` 估算。
输出「安全上限」，即在该误差下 cap 最大能设多少而不触发 400。

用法
----
    cd /d/workspace/geesun_agent
    set -a && . ./.env && set +a
    <python> tests/spikes/max_tokens_cap_probe.py
"""

from __future__ import annotations

import os

# 沙箱注入的代理 / 指向不存在文件的 SSL_CERT_FILE 会污染 httpx（报 FileNotFoundError 而非
# ConnectError，极易误判为路径问题）——本机直连内网必须先清掉。
for _k in (
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
):
    os.environ.pop(_k, None)

import httpx  # noqa: E402


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).replace("\r", "").replace("\n", "").strip().strip('"').strip("'")


BASE = _env("BASE_URL", "http://172.16.66.13:8003/v1").rstrip("/")
KEY = _env("OPENAI_API_KEY")
MODEL = _env("MODEL_NAME", "Qwen3.6-35B-A3B")
MODEL_MAX_LEN = int(_env("MODEL_MAX_LEN", "262144"))
MARGIN = int(_env("MODEL_MAX_TOKENS_MARGIN", "16384"))

# 真实业务场景近似：英文 system/工具 schema + 中文用户内容。这里用纯中文测最坏情况
# （中文单字 token 数最多，chars//2 低估最狠）。
ZH_PARA = (
    "智能制造转型的核心在于设备联网与数据贯通，通过工业物联网采集产线实时数据，"
    "再以边缘计算完成本地推理与异常拦截，最终由制造执行系统闭环下发工艺参数。"
    "这一链路对时延、可靠性与数据安全都提出了远高于通用信息系统的要求，"
    "因此在架构设计阶段就必须明确分层边界与降级策略。"
)


def build(n_chars: int) -> str:
    return (ZH_PARA * (n_chars // len(ZH_PARA) + 1))[:n_chars]


def probe(n_chars: int) -> dict:
    text = build(n_chars)
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 8,          # 只要 usage，不要正文
        "temperature": 0,
    }
    r = httpx.post(
        f"{BASE}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {KEY}"},
        timeout=httpx.Timeout(600.0, connect=15.0),
        trust_env=False,
    )
    r.raise_for_status()
    d = r.json()
    real = d["usage"]["prompt_tokens"]
    est = len(text) // 2                       # 退化路径公式
    ratio = real / est if est else float("inf")
    return {"chars": len(text), "real": real, "est": est, "ratio": ratio}


print("=" * 78)
print("路径 B（cold 首轮退化估算）误差实测")
print("=" * 78)
print("  端点 %s   模型 %s" % (BASE, MODEL))
print("  约束 prompt + max_tokens <= %d   margin(默认) %d" % (MODEL_MAX_LEN, MARGIN))
print()
print("  %8s %10s %10s %8s %14s" % ("字符数", "引擎真值", "chars//2", "低估倍数", "相对误差"))
print("  " + "-" * 62)

rows = []
for n in (2_000, 20_000, 80_000):
    row = probe(n)
    rows.append(row)
    print("  %8d %10d %10d %8.2fx %13.1f%%"
          % (row["chars"], row["real"], row["est"], row["ratio"],
             (row["ratio"] - 1) * 100))

worst = max(r["ratio"] for r in rows)

print()
print("=" * 78)
print("判定：cap 最大能设多少而不触发 400")
print("=" * 78)
print("  退化路径下 effective_max = min(cap, max_len - est - margin)，")
print("  而引擎实际占用 = real + effective_max。要安全必须 real + effective_max <= max_len。")
print("  最坏低估 %.2fx 时（est = real / %.2f，即 est 只有真值的 %.0f%%）：" % (worst, worst, 100 / worst))
print()
print("  %10s %14s %12s %14s %10s" % ("cap", "估算 prompt", "effective", "真实占用", "结果"))
print("  " + "-" * 66)
for cap in (65536, 131072, 196608, 245760, 256000):
    # 取最坏情况：一段被低估 worst 倍的真实 prompt，其估算值刚好吃满 fallback 分支
    # 这里给三档真实 prompt 大小，看各自是否超限
    verdicts = []
    for real_prompt in (50_000, 100_000, 180_000):
        est = int(real_prompt / worst)
        eff = min(cap, MODEL_MAX_LEN - est - MARGIN)
        actual = real_prompt + eff
        verdicts.append("OK" if actual <= MODEL_MAX_LEN else "400(超%d)" % (actual - MODEL_MAX_LEN))
    print("  %10d %14s %12s %14s %10s"
          % (cap, "50k/100k/180k", "按低估算", "real+eff", "/".join(verdicts)))

print()
print("  说明：上表第 2~4 列按'真实 prompt = 50k / 100k / 180k，但被低估 %.1fx'推演。" % worst)
print("  只要任一档出现 400，就说明该 cap 在 cold 首轮不安全。")

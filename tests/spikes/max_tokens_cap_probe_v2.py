"""探针 v2：用**真实非重复文本**复测 `chars // 2` 估算的误差方向与幅度。

为什么需要 v2
-------------
v1 用重复段落拼接（同一段重复 N 次），tokenizer 会把重复内容压成更少的 token
（BPE 对已见序列的高频合并），测出低估仅 1.03x —— **乐观失真**。
判断 cap 安全边界必须用自然文本，否则会把风险算小。

config.py:147-152 的注释声称「token 计数低估中文 ~3x，即便低估也触碰不到 400 线」。
该数字是本探针要验证的核心假设：若真实倍数远小于 3x，则「65536 留了 3 倍余量」
这个安全论证本身就不成立（余量被高估，cap 上调空间被低估）。

同时对比三类文本形态（真实 prompt 是混合体）：
  A 纯中文自然文本      —— 中文 tokenizer 密度
  B 中英混排（含代码）  —— 真实技术文档形态
  C 重复段落（v1 口径） —— 对照，暴露压缩效应

用法
----
    cd /d/workspace/geesun_agent
    set -a && . ./.env && set +a
    <python> tests/spikes/max_tokens_cap_probe_v2.py
"""

from __future__ import annotations

import os
import pathlib

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

REPO = pathlib.Path(__file__).resolve().parents[2]


def load_corpus(paths: list[str]) -> str:
    """拼接仓库内真实中文技术文档作为语料。"""
    chunks = []
    for p in paths:
        f = REPO / p
        if f.exists():
            chunks.append(f.read_text(encoding="utf-8", errors="ignore"))
    return "\n\n".join(chunks)


ZH_DOCS = [
    "docs/context-window-fix-draft.md",
    "docs/agent-guardrails-design.md",
    "docs/前端交互状态机与循环检测重构方案.md",
    "docs/tracing-span-pollution-postmortem.md",
    "docs/sse-streaming-debug.md",
    "docs/loop-detection-方案.md",
    "docs/llm-reasoning-field-passthrough.md",
]
# mixed 必须与 ZH_DOCS 零重叠，否则「取前 n_chars」会退化成同一段文本（首版踩过）。
# AGENTS.md（27828 字符）本身就是典型中英混排技术文档，单独用它。
MIXED_DOCS = ["AGENTS.md"]

ZH_PARA = (
    "智能制造转型的核心在于设备联网与数据贯通，通过工业物联网采集产线实时数据，"
    "再以边缘计算完成本地推理与异常拦截，最终由制造执行系统闭环下发工艺参数。"
    "这一链路对时延、可靠性与数据安全都提出了远高于通用信息系统的要求，"
    "因此在架构设计阶段就必须明确分层边界与降级策略。"
)


def build(n_chars: int, kind: str) -> str:
    """取语料前 n_chars 字符。

    ⚠️ 首版 bug（已修）：mixed 组用 `ZH_DOCS + [AGENTS.md, README.md]` 拼接后取
    前 n_chars，而 n_chars < len(ZH_DOCS 总量) 时前缀**完全落在 ZH_DOCS 内**，
    于是 mixed 与 zh 测的是同一段文本（实测两者 token 数一模一样 = 8661/26489）。
    now：mixed 单独取 AGENTS.md + README.md，与 zh 语料零重叠。
    """
    if kind == "repeat":
        return (ZH_PARA * (n_chars // len(ZH_PARA) + 1))[:n_chars]
    corpus = load_corpus(ZH_DOCS if kind == "zh" else MIXED_DOCS)
    if not corpus:
        raise SystemExit("语料为空，检查路径")
    return (corpus * (n_chars // len(corpus) + 1))[:n_chars] if len(corpus) < n_chars else corpus[:n_chars]


def probe(text: str) -> dict:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 8,
        "temperature": 0,
    }
    r = httpx.post(
        f"{BASE}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {KEY}"},
        timeout=httpx.Timeout(900.0, connect=15.0),
        trust_env=False,
    )
    r.raise_for_status()
    real = r.json()["usage"]["prompt_tokens"]
    est = len(text) // 2
    return {"chars": len(text), "real": real, "est": est, "ratio": real / est if est else 0.0}


print("=" * 82)
print("真实文本 vs 估算公式 chars//2 —— 误差方向与幅度")
print("=" * 82)
print("  端点 %s   模型 %s" % (BASE, MODEL))
print()
print("  %-8s %9s %10s %10s %9s %10s" % ("形态", "字符数", "引擎真值", "chars//2", "真值/估", "误差方向"))
print("  " + "-" * 70)

results: dict[str, list[dict]] = {}
# 长度上限受各语料自身长度约束（AGENTS.md 仅 27828 字符），避免重复拼接重新引入偏差
for kind, label, sizes in (
    ("zh", "纯中文自然", (20_000, 60_000)),
    ("mixed", "中英混排", (20_000, 26_000)),
    ("repeat", "重复段落(对照)", (20_000, 60_000)),
):
    rows = []
    for n in sizes:
        row = probe(build(n, kind))
        rows.append(row)
        direction = "低估 %.0f%%" % ((row["ratio"] - 1) * 100) if row["ratio"] > 1 else "高估 %.0f%%" % ((1 - row["ratio"]) * 100)
        print("  %-8s %9d %10d %10d %8.2fx %10s"
              % (label, row["chars"], row["real"], row["est"], row["ratio"], direction))
    results[kind] = rows

print()
print("=" * 82)
print("判定")
print("=" * 82)
zh_worst = max(r["ratio"] for r in results["zh"])
mixed_worst = max(r["ratio"] for r in results["mixed"])
print("  config.py 注释声称「低估中文 ~3x」。实测：")
print("    纯中文自然文本最坏低估 %.2fx（真值≈字符数/%.2f，即 1 token ≈ %.2f 个汉字）"
      % (zh_worst, 2 * zh_worst, 2 * zh_worst))
print("    中英混排最坏低估 %.2fx" % mixed_worst)
print()
claimed = 3.0
if zh_worst < claimed * 0.7:
    print("  ⇒ 注释的 3x **夸大**了 %.1f 倍。「65536 留了 3 倍余量」这个安全论证不成立，" % (claimed / zh_worst))
    print("    真实余量只有 %.2f 倍。反过来说，cap 的可上调空间比原设想大得多。" % zh_worst)
else:
    print("  ⇒ 注释的 3x 与实测同量级，估值可信。")

print()
print("  这对 cap 上调的含义：")
print("    动态收紧 effective_max = min(cap, %d - est - %d) 中，est 的关键缺陷**不是倍数**，" % (MODEL_MAX_LEN, MARGIN))
print("    而是 **system_message 与 tools schema 完全未计入**（fallback 分支只累加 msgs 的 content）。")
print("    真实 prompt = system + tools + msgs，而 est 只覆盖 msgs 部分 → 缺口随 system/tools 膨胀而扩大。")

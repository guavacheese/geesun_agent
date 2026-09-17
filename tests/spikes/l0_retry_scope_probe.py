"""探针：L0「重试时关思考」的触发面与误伤幅度。

背景
----
守卫 `terminal_response.py` 的 `thinking_truncated` 判据是**结果形态**，不是成因：

    content 为空 + reasoning 非空 + finish_reason == "length"

它同时覆盖两类成因完全不同的场景：
  (甲) 退化重复循环 —— 温度 0 贪婪解码陷入吸收态，思考自己复读、烧穿预算
       （会话 e9c77ce9 即此类，reasoning 93634 字符、唯一 24-gram 仅 2.3%）
  (乙) 正常重任务   —— 思考没退化，纯粹任务太重，预算内没想完就被截断

L0 方案（重试时带 `enable_thinking=false`）对 (甲) 对症：退化的根因是思考链
的自我强化，掐掉思考链就绕开了吸收态。但对 (乙) 是**降级**：模型被剥夺推理
能力后仍要产出结论，质量损失多少，此前从未量化。

本探针回答两个问题
------------------
Q1（生死判据）退化输入上关思考重试，到底能不能救出正文？
   A1 = [原题, 恢复提示] + 思考      + 紧预算   ← 现状重试，预期再次烧穿
   A2 = [原题, 恢复提示] + 关思考    + 紧预算   ← L0 重试，是否出正文？
   A2 若仍为空 ⇒ L0 无价值，整个方案推翻。

Q2（误伤量化）正常重任务上，关思考相对「思考完整跑完」损失多少？
   B1 = 文档分析 + 思考   + 充足预算   ← 基线：本该产出的样子
   B2 = 文档分析 + 思考   + 紧预算     ← 是否也会烧穿（决定 (乙) 占比）
   B3 = 文档分析 + 关思考 + 紧预算     ← L0 重试态
   B4 = 文档分析 + 关思考 + 充足预算   ← 关思考的天花板（排除预算不足的干扰）

判据
----
Q1: content 是否非空；reasoning 的重复度（唯一 24-gram 比例、最长复读行计数）
Q2: content 是否非空 / 长度 / 三项要求是否都有对应段落 / 是否引用文档真实专名

用法
----
    cd /d/workspace/geesun_agent
    set -a && . ./.env && set +a
    <python> tests/spikes/l0_retry_scope_probe.py

结果 JSON 落在 tests/spikes/_out/l0_retry_scope.json，正文全文供人工评阅。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time
from collections import Counter

# 沙箱会注入指向不存在文件的 SSL_CERT_FILE 与失效代理，二者都会让 httpx
# 抛出与网络无关的异常（FileNotFoundError / ConnectError），先清掉。
for _k in (
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
):
    os.environ.pop(_k, None)

import httpx  # noqa: E402


def _env(name: str, default: str = "") -> str:
    raw = os.environ.get(name) or default
    return raw.replace("\r", "").replace("\n", "").strip().strip('"').strip("'")


BASE = _env("BASE_URL", "http://172.16.66.13:8003/v1").rstrip("/")
KEY = _env("OPENAI_API_KEY")
MODEL = _env("MODEL_NAME", "Qwen3.6-35B-A3B")
REPO = pathlib.Path(__file__).resolve().parents[2]
OUT_DIR = pathlib.Path(__file__).resolve().parent / "_out"

TIGHT = 2000      # 已知能在写作题上稳定复现「思考烧穿、正文为空」
AMPLE = 16000     # 足够让重任务把思考跑完

# 与生产 terminal_response.py:_RECOVERY_PROMPT_THINKING_TRUNCATED_NO_TOOL 逐字一致
RECOVERY = (
    "<system_reminder>\n"
    "你上一轮的思考过程过长，占满了本次调用的全部输出额度，导致最终回复为空"
    "（provider 已返回 finish_reason=length 截断信号）。\n"
    "请**直接输出面向用户的最终结论**，思考过程务必保持简短。"
    "若结论依赖尚未获取的信息，最多再调用一次工具；否则请基于已有信息作答。\n"
    "</system_reminder>"
)

WRITE_Q = (
    "请写一篇 8000 字的中国制造业智能制造转型深度报告，"
    "要求分十个章节，每章都要有数据支撑和案例分析。"
)

# 场景 B 的语料：仓库内真实中文设计文档，足够长且含大量具体缺陷描述
DOC_PATHS = [
    "docs/context-window-fix-draft.md",
    "docs/loop-detection-方案.md",
    "docs/llm-reasoning-field-passthrough.md",
]
DOC_TASK = (
    "下面是一个 AI Agent 项目的设计文档节选。请完成三项分析，并用小标题明确分区：\n"
    "一、逐条列出文档中提到的「已知缺陷 / 未解决问题」，每条给出：位置线索、根因、影响面。\n"
    "二、为每条缺陷评定严重度（P0/P1/P2）并说明评级理由。\n"
    "三、指出文档中自相矛盾、或论证不成立的地方。\n\n"
    "文档节选：\n---\n{corpus}\n---"
)


def load_corpus(paths: list[str]) -> str:
    parts = []
    for p in paths:
        f = REPO / p
        if f.exists():
            parts.append(f.read_text(encoding="utf-8", errors="ignore"))
    return "\n\n".join(parts)


def uniq_ngram_ratio(text: str, n: int = 24, stride: int = 4) -> float:
    """唯一 n-gram 比例。退化循环会显著低于正常文本（正常 >0.9，退化 <0.1）。"""
    if len(text) < n * 2:
        return 1.0
    grams = [text[i:i + n] for i in range(0, len(text) - n, stride)]
    return len(set(grams)) / len(grams) if grams else 1.0


def top_line_repeat(text: str, min_len: int = 20) -> tuple[int, str]:
    """最长复读行的出现次数（行级吸收态的直接证据）。"""
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) >= min_len]
    if not lines:
        return 0, ""
    line, cnt = Counter(lines).most_common(1)[0]
    return cnt, line[:70]


def chat(messages: list[dict], max_tokens: int, thinking: bool | None = None,
         temperature: float = 0.0, timeout: float = 900.0) -> dict:
    body: dict = {
        "model": MODEL, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }
    if thinking is False:
        body["chat_template_kwargs"] = {"enable_thinking": False}

    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/chat/completions", json=body,
                       headers={"Authorization": f"Bearer {KEY}"},
                       timeout=httpx.Timeout(timeout, connect=15.0), trust_env=False)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "elapsed": time.time() - t0}
    el = time.time() - t0

    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text[:300]}", "elapsed": el}

    d = r.json()
    ch = d["choices"][0]
    m = ch.get("message") or {}
    content = (m.get("content") or "").strip()
    reasoning = (m.get("reasoning_content") or m.get("reasoning") or "").strip()
    usage = d.get("usage") or {}
    return {
        "finish": ch.get("finish_reason"),
        "content": content,
        "reasoning": reasoning,
        "completion_tokens": usage.get("completion_tokens"),
        "elapsed": el,
    }


def report(tag: str, r: dict, expect_keys: list[str] | None = None) -> dict:
    if "error" in r:
        print("  [%s] 请求失败：%s" % (tag, r["error"]))
        return r

    c, rz = r["content"], r["reasoning"]
    ratio = uniq_ngram_ratio(rz) if rz else 1.0
    cnt, sample = top_line_repeat(rz) if rz else (0, "")

    print("  [%-3s] finish=%-7s 耗时=%5.1fs  输出=%5s tok | content=%6d 字  reasoning=%6d 字"
          % (tag, r["finish"], r["elapsed"], r["completion_tokens"], len(c), len(rz)))
    if rz:
        flag = "  <== 疑似退化" if ratio < 0.25 or cnt >= 20 else ""
        print("        思考重复度：唯一 24-gram=%.1f%%  最长复读行 x%d%s"
              % (ratio * 100, cnt, flag))
        if cnt >= 10:
            print("        复读样本：%r" % sample)

    if expect_keys and c:
        hit = [k for k in expect_keys if k in c]
        print("        结构检测：命中 %d/%d %s" % (len(hit), len(expect_keys), hit))

    r["uniq_ngram"] = round(ratio, 4)
    r["top_line_repeat"] = cnt
    return r


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    results: dict[str, dict] = {}

    print("=" * 92)
    print("Q1｜退化输入上的重试：关思考到底能不能救出正文（L0 的生死判据）")
    print("=" * 92)
    msgs_a = [
        {"role": "user", "content": WRITE_Q},
        {"role": "user", "content": RECOVERY},
    ]
    print("\n  A1 = 现状重试（保留思考，紧预算 %d）" % TIGHT)
    results["A1_retry_keep_thinking"] = report(
        "A1", chat(msgs_a, TIGHT, thinking=None))
    print("\n  A2 = L0 重试（关思考，紧预算 %d）" % TIGHT)
    results["A2_retry_no_thinking"] = report(
        "A2", chat(msgs_a, TIGHT, thinking=False))

    print("\n" + "=" * 92)
    print("Q2｜正常重任务上的误伤量化（文档交叉分析）")
    print("=" * 92)
    corpus = load_corpus(DOC_PATHS)
    print("\n  语料：%d 字符（%s）" % (len(corpus), ", ".join(DOC_PATHS)))
    if not corpus:
        print("  !! 语料为空，跳过 Q2")
    else:
        task = DOC_TASK.replace("{corpus}", corpus)
        msgs_b = [{"role": "user", "content": task}]
        keys = ["一、", "二、", "三、"]
        # 文档真实专名：内容里出现这些说明确实读了文档，而非泛泛而谈
        names = ["finish_reason", "RemoveMessage", "reasoning_content", "checkpoint"]

        print("\n  B1 = 基线（思考 + 充足预算 %d）" % AMPLE)
        results["B1_ample_thinking"] = report("B1", chat(msgs_b, AMPLE), keys)
        results["B1_ample_thinking"]["name_hits"] = [
            n for n in names
            if n in results["B1_ample_thinking"].get("content", "")
        ]

        print("\n  B2 = 思考 + 紧预算 %d（看这类任务是否也会烧穿）" % TIGHT)
        results["B2_tight_thinking"] = report("B2", chat(msgs_b, TIGHT), keys)

        print("\n  B3 = L0 重试态（关思考 + 紧预算 %d）" % TIGHT)
        results["B3_tight_no_thinking"] = report("B3", chat(msgs_b, TIGHT, thinking=False), keys)
        results["B3_tight_no_thinking"]["name_hits"] = [
            n for n in names
            if n in results["B3_tight_no_thinking"].get("content", "")
        ]

        print("\n  B4 = 关思考 + 充足预算 %d（关思考的天花板）" % AMPLE)
        results["B4_ample_no_thinking"] = report("B4", chat(msgs_b, AMPLE, thinking=False), keys)
        results["B4_ample_no_thinking"]["name_hits"] = [
            n for n in names
            if n in results["B4_ample_no_thinking"].get("content", "")
        ]

    print("\n" + "=" * 92)
    print("判读")
    print("=" * 92)
    a1, a2 = results.get("A1_retry_keep_thinking", {}), results.get("A2_retry_no_thinking", {})
    print("  Q1 L0 是否有效：")
    print("     A1 现状重试 content=%d 字 %s"
          % (len(a1.get("content", "")), "（仍为空 → 现状确实救不回来）"
             if not a1.get("content") else "（意外产出了正文）"))
    print("     A2 L0   重试 content=%d 字 %s"
          % (len(a2.get("content", "")), "（救出正文 → L0 有效）"
             if a2.get("content") else "（仍为空 → L0 无价值）"))

    if corpus:
        b1 = results.get("B1_ample_thinking", {})
        b2 = results.get("B2_tight_thinking", {})
        b3 = results.get("B3_tight_no_thinking", {})
        b4 = results.get("B4_ample_no_thinking", {})
        print("\n  Q2 误伤幅度（content 字符数）：")
        print("     B1 思考+充足 = %6d   ← 本该产出的样子" % len(b1.get("content", "")))
        print("     B2 思考+紧   = %6d   %s"
              % (len(b2.get("content", "")),
                 "（也烧穿 → 重任务确实会进这个分支）" if not b2.get("content")
                 else "（没烧穿 → 这类任务不常触发守卫）"))
        print("     B3 关思考+紧 = %6d   ← L0 重试态的实际产出" % len(b3.get("content", "")))
        print("     B4 关思考+充足 = %6d ← 关思考的天花板" % len(b4.get("content", "")))
        if b1.get("content") and b3.get("content"):
            print("     误伤比 B3/B1 = %.1f%%" % (len(b3["content"]) / len(b1["content"]) * 100))
            print("     专名引用 B1=%s" % b1.get("name_hits"))
            print("     专名引用 B3=%s" % b3.get("name_hits"))

    (OUT_DIR / "l0_retry_scope.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n  完整正文已落盘：tests/spikes/_out/l0_retry_scope.json")


if __name__ == "__main__":
    main()

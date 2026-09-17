"""探针：L0「重试时关思考」在**硬推理型重任务**上的误伤，以及真退化场景的补测。

为什么需要这个探针
------------------
`l0_retry_scope_probe.py` 已证明：对**信息处理型**重任务（读文档 + 结构化提炼），
关思考几乎无损 —— 3600 字产出、标识符命中率 95.5%、覆盖的缺陷条目反而更多，
而成本只有 1/8。

但那类任务的本质是「信息搬运 + 组织」，模型的知识与语言能力就够用，推理链
边际价值低。真正可能被误伤的是**硬推理型**任务：需要多步搜索/推导才能得到
答案，思考链本身就是生产力。

本探针的两组
------------
C 硬推理型（答案客观唯一，可判定对错）
    C1 = 思考   + 充足预算 16000   ← 推理链完整时的水平
    C2 = 关思考 + 充足预算 16000   ← 剥夺推理链后的水平（重点看是否还答对）
    C3 = 关思考 + 中等预算 4000    ← 排除「预算不够」的混淆

    题目：0-9 各一次组成十位数的相邻和质数计数。标准答案 845，
    由独立 DFS 求解器给出（tests/spikes 内可复算），非模型记忆可得。

D 真退化场景（现有 A 组只覆盖了「预算不足」，未覆盖「退化循环」）
    D1 = [长写作题, 恢复提示] + 思考   + 8000 → reasoning 唯一 24-gram 是否跌破 25%？
    D2 = 同输入              + 关思考 + 8000 → 是否仍能产出正文

    ⚠️ 已知局限：紧预算（2000）复现的是「正常思考但没想完」，reasoning 唯一率
    高达 99.9%（**不退化**）。真退化只在长时间生成后才进入吸收态（会话 e9c77ce9
    烧满 65536 预算、336 秒后唯一率才降到 2.3%）。本组用 8000 预算试探退化阈值，
    若仍不退化则如实记录「未复现」，不假装覆盖。

用法
----
    cd /d/workspace/geesun_agent
    set -a && . ./.env && set +a
    <python> tests/spikes/l0_hard_reasoning_probe.py
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time
from collections import Counter

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
OUT_DIR = pathlib.Path(__file__).resolve().parent / "_out"

ANSWER = "845"   # 独立 DFS 求解器给出：0-9 各一次、相邻和为质数的十位数个数

HARD_Q = (
    "用数字 0、1、2、3、4、5、6、7、8、9 各恰好一次，组成一个十位数（首位不能是 0），"
    "要求任意相邻两位数字之和都是质数。请问这样的十位数共有多少个？"
    "请给出你的计数方法与最终数值。"
)

WRITE_Q = (
    "请写一篇 8000 字的中国制造业智能制造转型深度报告，"
    "要求分十个章节，每章都要有数据支撑和案例分析。"
)
RECOVERY = (
    "<system_reminder>\n"
    "你上一轮的思考过程过长，占满了本次调用的全部输出额度，导致最终回复为空"
    "（provider 已返回 finish_reason=length 截断信号）。\n"
    "请**直接输出面向用户的最终结论**，思考过程务必保持简短。"
    "若结论依赖尚未获取的信息，最多再调用一次工具；否则请基于已有信息作答。\n"
    "</system_reminder>"
)


def uniq_ngram_ratio(text: str, n: int = 24, stride: int = 4) -> float:
    if len(text) < n * 2:
        return 1.0
    grams = [text[i:i + n] for i in range(0, len(text) - n, stride)]
    return len(set(grams)) / len(grams) if grams else 1.0


def top_line_repeat(text: str, min_len: int = 20) -> int:
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) >= min_len]
    return Counter(lines).most_common(1)[0][1] if lines else 0


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
    return {
        "finish": ch.get("finish_reason"),
        "content": (m.get("content") or "").strip(),
        "reasoning": (m.get("reasoning_content") or m.get("reasoning") or "").strip(),
        "completion_tokens": (d.get("usage") or {}).get("completion_tokens"),
        "elapsed": el,
    }


def report(tag: str, r: dict, answer: str | None = None) -> dict:
    if "error" in r:
        print("  [%s] 请求失败：%s" % (tag, r["error"]))
        return r
    c, rz = r["content"], r["reasoning"]
    ratio = uniq_ngram_ratio(rz) if rz else 1.0
    cnt = top_line_repeat(rz) if rz else 0

    print("  [%-3s] finish=%-7s 耗时=%5.1fs 输出=%6s tok | content=%5d 字  reasoning=%6d 字"
          % (tag, r["finish"], r["elapsed"], r["completion_tokens"], len(c), len(rz)))
    if rz:
        print("        思考唯一 24-gram=%.1f%%  最长复读行 x%d%s"
              % (ratio * 100, cnt, "   <== 退化" if ratio < 0.25 else ""))
    if answer:
        nums = re.findall(r"\b\d{2,4}\b", c)
        verdict = "答对 ✅" if answer in nums else "未提及 845 ❌"
        print("        取值候选=%s  → %s" % (nums[-8:] if nums else "无", verdict))
        r["answer_correct"] = answer in nums
    r["uniq_ngram"] = round(ratio, 4)
    r["top_line_repeat"] = cnt
    return r


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    res: dict[str, dict] = {}

    print("=" * 92)
    print("C｜硬推理型重任务：思考链是不是必需（标准答案 %s，独立 DFS 求解）" % ANSWER)
    print("=" * 92)
    msgs_c = [{"role": "user", "content": HARD_Q}]

    print("\n  C1 = 思考 + 充足预算 16000")
    res["C1_ample_thinking"] = report("C1", chat(msgs_c, 16000), ANSWER)

    print("\n  C2 = 关思考 + 充足预算 16000")
    res["C2_ample_no_thinking"] = report("C2", chat(msgs_c, 16000, thinking=False), ANSWER)

    print("\n  C3 = 关思考 + 中等预算 4000")
    res["C3_mid_no_thinking"] = report("C3", chat(msgs_c, 4000, thinking=False), ANSWER)

    print("\n" + "=" * 92)
    print("D｜真退化场景补测（A 组只覆盖了「预算不足」，未覆盖「退化循环」）")
    print("=" * 92)
    msgs_d = [
        {"role": "user", "content": WRITE_Q},
        {"role": "user", "content": RECOVERY},
    ]

    print("\n  D1 = 重试输入 + 思考 + 8000（观察唯一 24-gram 是否跌破 25%）")
    res["D1_retry_thinking_8k"] = report("D1", chat(msgs_d, 8000))

    print("\n  D2 = 重试输入 + 关思考 + 8000")
    res["D2_retry_no_thinking_8k"] = report("D2", chat(msgs_d, 8000, thinking=False))

    print("\n" + "=" * 92)
    print("判读")
    print("=" * 92)
    c1, c2 = res.get("C1_ample_thinking", {}), res.get("C2_ample_no_thinking", {})
    print("  C 硬推理：")
    print("     C1 思考   答对=%s  思考用时 %d 字" % (c1.get("answer_correct"), len(c1.get("reasoning", ""))))
    print("     C2 关思考 答对=%s" % c2.get("answer_correct"))
    if c1.get("answer_correct") and not c2.get("answer_correct"):
        print("     ⇒ 思考链必需，关思考确实**误伤硬推理任务**")
    elif c1.get("answer_correct") and c2.get("answer_correct"):
        print("     ⇒ 即便硬推理，本题维度上关思考也未误伤（题目难度不足或推理量小）")
    else:
        print("     ⇒ 基线（带思考）都没答对，本题超出模型能力，不能用于判定误伤")

    d1 = res.get("D1_retry_thinking_8k", {})
    print("\n  D 退化补测：")
    if d1 and "uniq_ngram" in d1:
        print("     D1 思考唯一 24-gram=%.1f%% → %s"
              % (d1["uniq_ngram"] * 100,
                 "复现了退化" if d1["uniq_ngram"] < 0.25 else "**未复现退化**（8000 预算仍在「正常思考」区）"))

    (OUT_DIR / "l0_hard_reasoning.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n  完整产出已落盘：tests/spikes/_out/l0_hard_reasoning.json")


if __name__ == "__main__":
    main()

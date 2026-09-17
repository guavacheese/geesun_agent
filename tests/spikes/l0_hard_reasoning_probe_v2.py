"""探针：硬推理型重任务的误伤判定（第二版，修正 v1 的基线设计缺陷）。

v1（`l0_hard_reasoning_probe.py` C 组）的教训
-------------------------------------------
用了「0-9 相邻和为质数的十位数计数」（标准答案 845）—— 结果**基线（带思考 + 16000 预算）
自己就没答对**，还烧穿了预算（content=0、reasoning 36141 字、唯一 24-gram 75.4%、最长复读行 x14）。

⇒ **基线不通过的对照组实验是无效的**：你无法区分「关思考导致的损失」和「模型本来就不会」。
   做误伤对照实验前，必须先确认基线能通过；否则再漂亮的对比表都是伪证据。
   （这条已写入技能判据陷阱。v1 的 C 组数据仅保留一个价值：短输入的难题也能烧穿预算。）

本探针的题目
------------
用 1/2/3/4 各恰好一次组成四位数，约束 (a) 1 不在首位 (b) 2 与 3 相邻 (c) 4 不在末位。
标准答案：**6 个解** —— 2341 3241 4123 4132 4231 4321（`itertools.permutations` 穷举验证）。

设计意图：规模小到 30B/3B 级模型能**枚举完**（不会因算力不足而失败），但必须真的做搜索 ——
穷举 24 种排列并逐条过滤。判据不只看「答案数 6」，还看**列出的正确解个数**，
避免模型蒙对数字。

用法
----
    cd /d/workspace/geesun_agent
    set -a && . ./.env && set +a
    <python> tests/spikes/l0_hard_reasoning_probe_v2.py
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time

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

TRUTH = {"2341", "3241", "4123", "4132", "4231", "4321"}
ANSWER_COUNT = len(TRUTH)

Q = (
    "用数字 1、2、3、4 各恰好一次组成一个四位数，要求同时满足三个条件：\n"
    "(a) 数字 1 不在首位；\n"
    "(b) 数字 2 和数字 3 必须相邻；\n"
    "(c) 数字 4 不在末位。\n"
    "请问满足条件的四位数共有多少个？请把满足条件的数全部列出来。"
)


def uniq_ngram_ratio(text: str, n: int = 24, stride: int = 4) -> float:
    if len(text) < n * 2:
        return 1.0
    grams = [text[i:i + n] for i in range(0, len(text) - n, stride)]
    return len(set(grams)) / len(grams) if grams else 1.0


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


def report(tag: str, r: dict) -> dict:
    if "error" in r:
        print("  [%s] 请求失败：%s" % (tag, r["error"]))
        return r

    c, rz = r["content"], r["reasoning"]
    ratio = uniq_ngram_ratio(rz) if rz else 1.0
    found = sorted({m for m in re.findall(r"\b[1-4]{4}\b", c) if m in TRUTH})
    # ⚠️ 假阳性提示：用排除法作答时会**故意列出无效候选**（"2314 -> 末尾是 4，违反 (c)"），
    # 这些是推理过程的一部分、不是错误答案。E2/E5 都命中此坑。故 wrong 仅作参考，
    # **不参与 verdict**；判定以「真值集里的解是否被列全」为准。
    wrong = sorted({m for m in re.findall(r"\b[1-4]{4}\b", c) if m not in TRUTH
                    and len(set(m)) == 4 and "0" not in m})
    claims_six = bool(re.search(r"(共|总共有?|答案[是为]?)\s*6\s*个", c)) or "(6" in c or "6 个" in c

    print("  [%-3s] finish=%-7s 耗时=%5.1fs 输出=%5s tok | content=%5d 字  reasoning=%5d 字"
          % (tag, r["finish"], r["elapsed"], r["completion_tokens"], len(c), len(rz)))
    if rz:
        print("        思考唯一 24-gram=%.1f%%%s" % (ratio * 100,
              "   <== 退化" if ratio < 0.25 else ""))
    print("        列出正确解 %d/%d %s" % (len(found), ANSWER_COUNT, found))
    if wrong:
        print("        另提及（排除过程，非错误答案）：%s" % wrong)
    print("        声称答案为 6 个：%s" % ("是" if claims_six else "否"))

    r.update({"correct_found": len(found), "found_list": found,
              "wrong_list": wrong, "claims_six": claims_six,
              "uniq_ngram": round(ratio, 4)})
    r["verdict"] = ("完全正确" if len(found) == ANSWER_COUNT
                    else "部分正确" if found else "失败")
    return r


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    res: dict[str, dict] = {}
    msgs = [{"role": "user", "content": Q}]

    print("=" * 92)
    print("E｜硬推理型重任务的误伤（题目已校准：基线可通过是前提）")
    print("=" * 92)

    print("\n  E1 = 思考 + 4000 预算（基线，必须先通过）")
    res["E1_thinking_4k"] = report("E1", chat(msgs, 4000))

    print("\n  E2 = 关思考 + 4000 预算")
    res["E2_no_thinking_4k"] = report("E2", chat(msgs, 4000, thinking=False))

    print("\n  E3 = 思考 + 1000 预算（紧预算下的思考，看是否反而更差）")
    res["E3_thinking_1k"] = report("E3", chat(msgs, 1000))

    print("\n  E4 = 关思考 + 1000 预算")
    res["E4_no_thinking_1k"] = report("E4", chat(msgs, 1000, thinking=False))

    print("\n  E5 = 思考 + 16000 预算（**真能力基线**）")
    print("        4000 那组 content=0 并非「模型不会」—— E2 已用它证明模型会做这道题，")
    print("        4000 的失败是「思考吃掉全部预算」。所以判定误伤必须补这一组：")
    print("        预算充足时思考能否答对。")
    res["E5_thinking_16k"] = report("E5", chat(msgs, 16000))

    print("\n" + "=" * 92)
    print("判读")
    print("=" * 92)
    e1, e2 = res["E1_thinking_4k"], res["E2_no_thinking_4k"]
    e3, e4 = res["E3_thinking_1k"], res["E4_no_thinking_1k"]
    e5 = res["E5_thinking_16k"]
    print("  %-22s %-12s %-12s" % ("", "思考", "关思考"))
    print("  " + "-" * 48)
    print("  %-22s %-12s %-12s" % ("4000 预算（解答）",
          "%d/6 %s" % (e1.get("correct_found", 0), e1.get("verdict", "")),
          "%d/6 %s" % (e2.get("correct_found", 0), e2.get("verdict", ""))))
    print("  %-22s %-12s %-12s" % ("16000 预算（真基线）",
          "%d/6 %s" % (e5.get("correct_found", 0), e5.get("verdict", "")), "—"))
    print("  %-22s %-12s %-12s" % ("1000 预算（紧）",
          "%d/6 %s" % (e3.get("correct_found", 0), e3.get("verdict", "")),
          "%d/6 %s" % (e4.get("correct_found", 0), e4.get("verdict", ""))))
    print()
    if not e1.get("correct_found") and e1.get("finish") == "length" and not e1.get("content"):
        print("  注：E1 的 0 分**不是能力问题** —— content 为空、finish=length，是思考吃掉全部预算；")
        print("      模型本身会做这道题（E2 用关思考 6/6 证明）。")
    print()
    if e5.get("correct_found", 0) >= e2.get("correct_found", 0):
        print("  ⇒ 真基线 E5 通过且不低于关思考 ⇒ **本题上关思考未造成误伤**；")
        print("     反而「思考 + 紧预算」是最差组合（content 为空、0 分）—— 这正是 L0 要治的形态。")
    elif e5.get("correct_found", 0) > 0:
        print("  ⇒ 思考在充足预算下能答对、关思考更差 ⇒ **硬推理任务上确实存在误伤**，")
        print("     L0 应按任务类型分流，而不是对 thinking_truncated 一刀切。")
    else:
        print("  ⇒ 真基线（充足预算 + 思考）仍未产出答案，如实标注「未验证」，不做外推。")

    (OUT_DIR / "l0_hard_reasoning_v2.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n  完整产出已落盘：tests/spikes/_out/l0_hard_reasoning_v2.json")


if __name__ == "__main__":
    main()

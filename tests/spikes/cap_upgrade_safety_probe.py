"""探针 v3（决定性）：把 cap 从 65536 调到 256000，会不会必然 400？

推理链（本探针逐条实测验证）
---------------------------
1. `model_call_guard` 的 `est` 只覆盖 `messages` 的 content：
       total_chars = sum(len(str(m.content)) for m in msgs)     # model.py:697
       prompt_tokens = total_chars // 2                          # fallback
   **system_message 与 tools schema 完全未计入**（fallback 分支连 sys_msg 都不拼）。
2. 而生产 system 里有 AGENTS.md 全量注入（agent.py:875 `memory=[AGENTS_MD_PATH]`，
   deepagents `_format_agent_memory` 只 strip HTML 注释、**不截断**），
   仓库 AGENTS.md 有 48225 字符 —— 这是一笔巨大的、est 看不见的 token。
3. 于是 cap 是否生效决定了 400 与否：
       effective = min(cap, 262144 - est - 16384)
       cap 生效条件：cap <= 262144 - est - 16384  即  est <= 262144 - 16384 - cap
     - cap = 65536  → est <= 180224，几乎恒成立 ⇒ **cap 恒生效** ⇒ 恒给输入留 ≥196608
     - cap = 256000 → est <= -10240，**永不成立** ⇒ cap 形同虚设，完全由动态项决定
4. cap 不生效时：
       真实占用 = est + 缺口 + effective = est + 缺口 + (262144 - est - 16384)
                = 245760 + 缺口
   ⇒ **只要「system + tools 的 token 数」> margin(16384) 就必然 400。**

本探针实测第 4 条的「缺口」大小，并对比 cap=65536 / 256000 的实际结果。

用法
----
    cd /d/workspace/geesun_agent
    set -a && . ./.env && set +a
    <python> tests/spikes/cap_upgrade_safety_probe.py
"""

from __future__ import annotations

import os
import pathlib
import re

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


def load_agents_md() -> str:
    p = REPO / "AGENTS.md"
    if not p.exists():
        return ""
    txt = p.read_text(encoding="utf-8", errors="ignore")
    return re.sub(r"<!--.*?-->", "", txt, flags=re.S).rstrip()   # deepagents 会 strip HTML 注释


AGENTS_MD = load_agents_md()
SYS_DEEPAGENT_STYLE = (
    "You are a deep agent, an AI assistant that helps users accomplish tasks using tools. "
    "You have access to a filesystem and can delegate work to subagents."
)
SYS_WITH_MEMORY = SYS_DEEPAGENT_STYLE + "\n\n" + AGENTS_MD


def probe(system: str, user_chars: int, cap: int, label: str) -> dict:
    user = ("智能制造转型的核心在于设备联网与数据贯通，" * (user_chars // 20 + 1))[:user_chars]
    user += "\n\n只回复两个字：收到"
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    # 复刻 model_call_guard 的估算口径：只累加 messages 的 content，不含 sys/tools
    est = sum(len(str(m["content"])) for m in msgs[1:]) // 2
    effective = min(cap, MODEL_MAX_LEN - est - MARGIN)
    body = {
        "model": MODEL,
        "messages": msgs,
        "max_tokens": effective,
        "temperature": 0,
    }
    r = httpx.post(
        f"{BASE}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {KEY}"},
        timeout=httpx.Timeout(600.0, connect=15.0),
        trust_env=False,
    )
    real_prompt = None
    detail = ""
    if r.status_code == 200:
        real_prompt = r.json().get("usage", {}).get("prompt_tokens")
    else:
        try:
            detail = r.json().get("error", {}).get("message", "")[:160]
        except Exception:
            detail = r.text[:160]

    gap = (real_prompt - est) if real_prompt else None
    verdict = "200 OK"
    if r.status_code == 400:
        verdict = "400 拒绝"
    print("  %-26s cap=%-7d est=%-7d effective=%-7d 真实prompt=%-7s 缺口=%-7s -> %s"
          % (label, cap, est, effective, real_prompt if real_prompt else "-",
             gap if gap is not None else "-", verdict))
    if detail:
        print("      %s" % detail.replace("\n", " "))
    return {
        "cap": cap, "est": est, "effective": effective,
        "real_prompt": real_prompt, "gap": gap, "status": r.status_code,
    }


print("=" * 96)
print("cap 从 65536 上调到 256000 —— 400 风险实测")
print("=" * 96)
print("  端点 %s   模型 %s" % (BASE, MODEL))
print("  AGENTS.md：%d 字符（deepagents 全量注入 system，不截断）" % len(AGENTS_MD))
print("  约束 prompt + max_tokens <= %d    margin %d" % (MODEL_MAX_LEN, MARGIN))
print()
print("  %-26s %-11s %-9s %-14s %-13s %-13s" % ("场景", "cap", "est", "effective", "真实prompt", "缺口"))
print("  " + "-" * 92)

rows = []

# ① 无 memory 注入（system 很短）——对照
rows.append(probe(SYS_DEEPAGENT_STYLE, 40_000, 65536, "短system + cap 65536"))
rows.append(probe(SYS_DEEPAGENT_STYLE, 40_000, 256000, "短system + cap 256000"))

# ② 含 AGENTS.md（贴近生产）
rows.append(probe(SYS_WITH_MEMORY, 40_000, 65536, "含AGENTS.md + cap 65536"))
rows.append(probe(SYS_WITH_MEMORY, 40_000, 256000, "含AGENTS.md + cap 256000"))

print()
print("=" * 96)
print("判定")
print("=" * 96)
short_gap = next((r["gap"] for r in rows[:2] if r["gap"]), None)
long_gap = next((r["gap"] for r in rows[2:] if r["gap"]), None)

if short_gap is not None:
    print("  短 system 的 est 缺口            = %d tokens  (margin=%d → %s)"
          % (short_gap, MARGIN, "安全" if short_gap < MARGIN else "**超限**"))
if long_gap is not None:
    print("  含 AGENTS.md 的 est 缺口         = %d tokens  (margin=%d → %s)"
          % (long_gap, MARGIN, "安全" if long_gap < MARGIN else "**超限**"))

print()
print("  结论：cap 一旦大到「动态项永远小于它」（cap > %d），" % (MODEL_MAX_LEN - MARGIN))
print("        effective 完全由 `%d - est - %d` 决定，实际占用固定为 %d + 缺口。"
      % (MODEL_MAX_LEN, MARGIN, MODEL_MAX_LEN - MARGIN))
print("        ⇒ 缺口 > margin 时，cap 越大越危险；cap=256000 时该条件永不成立。")

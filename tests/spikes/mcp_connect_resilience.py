"""Spike: MCP 连接韧性（重试 + 失败不缓存）—— 2026-09-12 生产事故回归防线。

不依赖 pytest，直接：python tests/spikes/mcp_connect_resilience.py

背景（2026-09-12 生产实测，agent_mem 生产 stack 16:15 重新发布）：
  stack 重启时 agent 与 geesun-mcp 同批重启，agent 先就绪 → swarm DNS 里
  `geesun-mcp` 名尚未注册 → decrypt-file 首次连接抛 "Temporary failure in
  name resolution"。旧代码两个缺陷叠加：
  ① connect_one 单次尝试，无重试 —— 几秒级的竞态窗口一击致命；
  ② get_mcp_tools 无论成败都写 _tools_cache（旧 464 行）—— 失败（空工具
     列表）也被缓存，而它每轮 chat 都被调用（chat.py:354）→ 一次 3 秒的
     DNS 竞态被放大成整个容器生命周期内 MCP 永久不可用。

修复语义（本 spike 的断言对象，测的是真实源码的**结构**）：
  1. connect_one 带退避重试（共 _MCP_CONNECT_RETRIES+1 次）
  2. 失败路径必须在缓存写入之前 return（行号位置断言，防回归）
  3. 成功 / 失败两条 return 路径都要过 shield 包装（错误防护不因路径而丢）
  4. 失败日志必须透出"下一轮自动重连"（可观测性）

教训对齐（AGENTS.md 2026-09-12）：ast 提取式测试测不出模块接线错误，因此
本 spike 不做 exec 复刻，直接对真实源码做 ast 结构断言 —— 每条断言都对应
一条曾在生产炸过的路径。
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
MCP_PY = ROOT / "src" / "core" / "mcp.py"

_FAILS: list[str] = []
_passed = 0


def check(cond: bool, label: str) -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _FAILS.append(label)
        print(f"  FAIL  {label}")


src = MCP_PY.read_text(encoding="utf-8")
tree = ast.parse(src)
lines = src.splitlines()

# ─── 1. 重试语义 ───
print("\n[1] 重试语义")
check("for attempt in range(_MCP_CONNECT_RETRIES + 1):" in src,
      "connect_one 带重试循环（RETRIES+1 次尝试）")
check("await asyncio.sleep(_MCP_CONNECT_BACKOFF)" in src,
      "重试之间有退避 sleep")
check("_MCP_CONNECT_TIMEOUT = 5" in src,
      "单次连接超时 5s（重试下最坏 ~12s/轮，mcp 真宕机时可接受）")

# ─── 2. 失败不缓存（位置断言）───
print("\n[2] 失败不缓存")
cache_writes = [
    n.lineno for n in ast.walk(tree)
    if isinstance(n, ast.Assign)
    and any(
        isinstance(t, ast.Subscript)
        and isinstance(t.value, ast.Name) and t.value.id == "_tools_cache"
        for t in n.targets
    )
]
check(len(cache_writes) == 1,
      f"_tools_cache 写入点恰好 1 处（实际 {len(cache_writes)} 处 @ "
      f"{[lines[i - 1].strip() for i in cache_writes]}）")

# "if failed:" 块内的 return 行号 —— 必须在缓存写入之前
failed_return_lineno = None
for n in ast.walk(tree):
    if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "failed":
        for sub in ast.walk(n):
            if isinstance(sub, ast.Return):
                failed_return_lineno = sub.lineno
check(failed_return_lineno is not None, "存在 `if failed:` → return 的失败路径")
if failed_return_lineno and cache_writes:
    check(failed_return_lineno < min(cache_writes),
          f"失败 return（L{failed_return_lineno}）在缓存写入"
          f"（L{min(cache_writes)}）之前 —— 失败结果永不进缓存")

# ─── 3. 两条路径都有 shield ───
print("\n[3] 错误防护完整性")
check(src.count("_shield_tools(_guard_download_tool(") == 2,
      "成功/失败两条 return 路径都过 shield+guard 包装（恰好 2 处）")

# ─── 4. 可观测性 ───
print("\n[4] 可观测性")
check("下一轮 chat 将自动重连" in src or "下一轮自动重连" in src,
      "失败日志透出「下一轮自动重连」提示")
check("第 %d/%d 次连接失败" in src, "每次重试失败都有独立日志（含第几次）")

print("\n" + "=" * 78)
print(f"PASS {_passed} / FAIL {len(_FAILS)}")
if _FAILS:
    for f in _FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PASS")

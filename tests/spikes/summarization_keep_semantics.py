"""Spike: `keep=("fraction", x)` 与自定义 token_counter 的语义冲突（2026-09-12）。

不依赖 pytest，直接：python tests/spikes/summarization_keep_semantics.py

## 为什么要有这个脚本

2026-09-12 对照 deer-flow / deepseek-harness 时发现 `keep=("messages", 10)`
覆盖了框架默认的 `("fraction", 0.10)`，一度打算改回按比例。**但改之前核实发现：
直接改会引入比现状严重得多的 bug——压缩后只保留最后 1 条消息。**

根因是 **`token_counter` 在这两处语义不同**：

| 调用点 | 传给 counter 的 messages | 需要的语义 |
|---|---|---|
| `_should_summarize` 的 trigger 判断 | **整个请求**的完整消息列表 | "这个请求占多少 token" |
| `_find_token_based_cutoff` 的保留预算二分 | `messages[mid:]` **任意切片** | "这个子集占多少 token" |

框架对此是知情的（`langchain/agents/middleware/summarization.py:344-352`）——
只有用默认 `count_tokens_approximately` 时它才自动拆分：

```python
if token_counter is count_tokens_approximately:
    self.token_counter = _get_approximate_token_counter(self.model)
    self._partial_token_counter = partial(self.token_counter, use_usage_metadata_scaling=False)
else:
    self.token_counter = token_counter
    self._partial_token_counter = token_counter     # ← 传自定义 counter 时两者合一
```

本项目传的是 `_engine_grounded_counter`（为 **trigger 语义**而写：优先返回
`model_call_guard` 写入的引擎真实 `prompt_tokens`），它**忽略 messages 参数**。
于是二分退化为"每步都拿到同一个数"，`cutoff` 被顶到 `len(messages) - 1`。

现状之所以没爆，纯属侥幸：`keep=("messages", 10)` 走 `_find_safe_cutoff`，
**根本不碰 token counter**。

## 本脚本做什么

用 ast 从**真实 langchain 源码**提取 `_find_token_based_cutoff` / `_find_safe_cutoff_point`
/ `_get_profile_limits` 并 exec 执行（不是逻辑复刻），在三种 counter 组合下对比 cutoff：
  1. 引擎 counter 同时充当 partial（= 直接改 keep 后的实际状态）→ 期望复现 1 条
  2. partial 换成子集感知 counter（= 修正后的正确形态）→ 期望 ~8 条
  3. 源码级断言：deepagents 在传自定义 counter 时确实把两者设成同一个对象

若将来有人：
  - 把 keep 改成 fraction 却没同步修 `_partial_token_counter` → 用例 1 会亮
  - 修了 `_partial_token_counter` 却被回退 → 用例 2 会亮
  - `agent.py` 的 keep / counter 绑定被改动 → 用例 4 会亮
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
from collections.abc import Mapping
from typing import cast

ROOT = pathlib.Path(__file__).resolve().parents[2]
LANGCHAIN_SUM = (
    ROOT / ".venv" / "lib" / "python3.13" / "site-packages"
    / "langchain" / "agents" / "middleware" / "summarization.py"
)
DEEPAGENTS_SUM = (
    ROOT / ".venv" / "lib" / "python3.13" / "site-packages"
    / "deepagents" / "middleware" / "summarization.py"
)
AGENT_PY = ROOT / "src" / "services" / "agent.py"

# 生产实测：`model.py:599` 注释记录 vLLM `/v1/models` 返回 max_model_len=262144
CONTEXT_WINDOW = 262144
KEEP_FRACTION = 0.10
TARGET_TOKENS = int(CONTEXT_WINDOW * KEEP_FRACTION)   # 26214

MSG_COUNT = 30
MSG_CHARS = 3000        # 每条按"字符数即 token"保守估算 → 单条 3000
EXPECTED_KEEP = TARGET_TOKENS // MSG_CHARS            # 8 条
EXPECTED_CUTOFF = MSG_COUNT - EXPECTED_KEEP           # 22

_passed = 0
_failed: list[str] = []


def check(cond: bool, label: str) -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# ─── ast 提取真实方法并 exec ───
def load_real_methods(path: pathlib.Path, names: set[str]) -> dict:
    """从真实源码提取指定方法，组装成一个可实例化的 Stub 类命名空间。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name in names:
                    body.append(sub)
    found = {n.name for n in body}
    missing = names - found
    assert not missing, f"{path.name} 中找不到这些方法: {missing}"

    # 真实源码带 `from __future__ import annotations`，否则注解在 exec 时会求值
    future = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    module = ast.Module(body=[future, *body], type_ignores=[])
    # 被提取方法体内的自由变量（真实模块顶部 import 的），按需注入
    ns: dict = {"Mapping": Mapping, "cast": cast}
    exec(compile(ast.fix_missing_locations(module), f"<{path.name}>", "exec"), ns)
    return ns


class _ToolMessage:
    """占位：真实 ToolMessage 未安装时，所有消息都不是它的实例。

    等价于"这批消息里没有 tool 消息"，因此 `_find_safe_cutoff_point` 直接返回入参，
    把待验证的二分手感完整保留下来。
    """


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Model:
    def __init__(self, window: int) -> None:
        self.profile = {"max_input_tokens": window}


def make_stub(real: dict, *, keep: tuple, counter, partial):
    """把三个真实方法挂到 Stub 上，只喂 counter 相关状态。"""
    cls_attrs = {
        "_find_token_based_cutoff": real["_find_token_based_cutoff"],
        "_find_safe_cutoff_point": real["_find_safe_cutoff_point"],
        "_get_profile_limits": real["_get_profile_limits"],
    }
    Stub = type("Stub", (), cls_attrs)
    obj = Stub()
    obj.keep = keep
    obj.token_counter = counter
    obj._partial_token_counter = partial
    obj.model = _Model(CONTEXT_WINDOW)
    return obj


def main() -> int:
    print("=" * 78)
    print("keep=(fraction) 与自定义 token_counter 的语义冲突验证")
    print("=" * 78)
    print(f"窗口 {CONTEXT_WINDOW} / keep fraction {KEEP_FRACTION} / "
          f"target {TARGET_TOKENS} token")
    print(f"造数 {MSG_COUNT} 条 × {MSG_CHARS} 字符 → 预期应保留约 {EXPECTED_KEEP} 条"
          f"（cutoff≈{EXPECTED_CUTOFF}）\n")

    # ── 0. counter 定义（严格对应两种真实语义）──
    def engine_counter(messages, *, tools=None):      # noqa: ARG001
        """模拟 `_engine_grounded_counter` 命中缓存分支：忽略 messages，返回引擎真值。

        这是本项目 counter 的设计意图——trigger 判断要的是"整个请求的真实 token"，
        引擎的 prompt_tokens 最准（含 system prompt / tools / 视觉 token）。
        """
        return 200000

    def subset_counter(messages, *, tools=None):      # noqa: ARG001
        """子集感知计数：对任意切片都返回该切片自身的量。"""
        return sum(len(getattr(m, "content", "") or "") for m in messages)

    messages = [_Msg("x" * MSG_CHARS) for _ in range(MSG_COUNT)]

    print("[1] 三处源码事实：框架只在默认 counter 分支拆分两个语义")
    src = LANGCHAIN_SUM.read_text(encoding="utf-8")
    check(
        "self._partial_token_counter = token_counter" in src,
        "langchain：传自定义 counter 时 _partial_token_counter 与 token_counter 同一对象",
    )
    check(
        "use_usage_metadata_scaling=False" in src,
        "langchain：仅在默认 count_tokens_approximately 分支才做 partial 区分",
    )
    check(
        "if self._partial_token_counter(messages[mid:]) <= target_token_count:" in src,
        "_find_token_based_cutoff 二分确实喂**切片**给 _partial_token_counter",
    )

    print("\n[2] 复现风险：keep 改 fraction、但 counter 未拆分（= 直接改 keep 的后果）")
    real = load_real_methods(
        LANGCHAIN_SUM,
        {"_find_token_based_cutoff", "_find_safe_cutoff_point", "_get_profile_limits"},
    )
    real.setdefault("ToolMessage", _ToolMessage)
    stub_bad = make_stub(
        real, keep=("fraction", KEEP_FRACTION),
        counter=engine_counter, partial=engine_counter,      # ← 同一个函数：坏形态
    )
    cutoff_bad = stub_bad._find_token_based_cutoff(messages)
    kept_bad = MSG_COUNT - cutoff_bad
    print(f"      cutoff={cutoff_bad}  保留 {kept_bad} 条")
    check(
        cutoff_bad == MSG_COUNT - 1,
        f"二分退化：cutoff 被顶到 len-1（{MSG_COUNT - 1}）而不是按 token 预算切",
    )
    check(
        kept_bad == 1,
        f"后果：压缩后只保留最后 1 条消息（实测 {kept_bad} 条）",
    )

    print("\n[3] 修正形态：partial 换成子集感知 counter（keep 仍为 fraction）")
    stub_ok = make_stub(
        real, keep=("fraction", KEEP_FRACTION),
        counter=engine_counter,      # 整体计数仍用引擎真值（trigger 语义不变）
        partial=subset_counter,      # 子集计数用本地估算（keep 语义）
    )
    cutoff_ok = stub_ok._find_token_based_cutoff(messages)
    kept_ok = MSG_COUNT - cutoff_ok
    print(f"      cutoff={cutoff_ok}  保留 {kept_ok} 条")
    check(
        kept_ok == EXPECTED_KEEP,
        f"按 token 预算保留 {EXPECTED_KEEP} 条（实测 {kept_ok}）",
    )
    check(
        cutoff_ok < cutoff_bad,
        "修正后的 cutoff 严格小于退化值（二分恢复有效）",
    )

    print("\n[4] 现状为什么没爆：keep=(\"messages\", n) 根本不碰 token counter")
    check(
        'if kind in {"tokens", "fraction"}:' in src,
        "_determine_cutoff_index 只在 tokens/fraction 分支走 token 路径",
    )
    check(
        "return self._find_safe_cutoff(messages, cast(\"int\", value))" in src,
        "messages 分支直接走 _find_safe_cutoff（按条数切，零 counter 依赖）",
    )

    print("\n[5] 项目侧绑定关系（agent.py）")
    agent_src = AGENT_PY.read_text(encoding="utf-8")
    check(
        "token_counter=_engine_grounded_counter" in agent_src,
        "agent.py 把引擎 grounded counter 挂给 SummarizationMiddleware",
    )
    check(
        "if real is not None:\n            return real" in agent_src,
        "_engine_grounded_counter 命中缓存时直接返回引擎值、忽略 messages 入参"
        "（这正是与子集语义冲突之处）",
    )
    keep_match = re.search(r'keep=\(("(?:messages|tokens|fraction)")\s*,\s*([0-9.]+)\)', agent_src)
    check(keep_match is not None, "agent.py 中 keep 是显式配置项（可被本脚本看到）")
    if keep_match:
        kind, value = keep_match.group(1), keep_match.group(2)
        print(f"      当前 keep = ({kind}, {value})")

    # ─── 最终形态的不变式（2026-09-12 落地后）───
    # 危险组合只有两种：① keep 走 fraction/tokens 但 counter 没拆 → 压缩后只剩 1 条；
    # ② 声称拆了但没有真的挂到 _lc_helper 上（挂错对象等于没拆）。
    # 所以这里断言的不是"keep 是否为 messages"，而是"这两件事必须同时成立"。
    keep_is_token_based = keep_match is not None and keep_match.group(1) in ('"fraction"', '"tokens"')
    split_wired = "partial_token_counter=_safe_token_counter" in agent_src
    check(
        (not keep_is_token_based) or split_wired,
        f"keep={keep_match.group(1) if keep_match else '?'} 走 token 预算 → 必须显式传 "
        "partial_token_counter（否则二分退化，见用例 [2]）",
    )
    if keep_is_token_based:
        check(
            re.search(r"helper\._partial_token_counter\s*=\s*partial_token_counter", agent_src) is not None,
            "_partial_token_counter 确实被赋到 _lc_helper 上"
            "（langchain 的 _determine_cutoff_index 走 self._lc_helper._partial_token_counter，"
            "挂到别的对象等于没拆）",
        )
        check(
            re.search(r"getattr\(self,\s*\"_lc_helper\"", agent_src) is not None,
            "取 _lc_helper 用 getattr 带默认值（deepagents 版本更替时不至于 AttributeError）",
        )
        check(
            re.search(r"token_counter=_engine_grounded_counter", agent_src) is not None
            and re.search(r"partial_token_counter=_safe_token_counter", agent_src) is not None,
            "trigger 用引擎真值（_engine_grounded_counter）、partial 用子集感知估算"
            "（_safe_token_counter）——两处语义不同，不可互换",
        )
        check(
            "partial_token_counter=None" in agent_src,
            "partial_token_counter 是 _SummarizationAccurate 的具名参数"
            "（会被自身消费，不会透传给 super().__init__）",
        )

    print("\n" + "=" * 78)
    print(f"PASS {_passed} / FAIL {len(_failed)}")
    if _failed:
        for f in _failed:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

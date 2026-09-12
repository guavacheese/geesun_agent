"""Spike: 验证会话列表从 __index__ 手工索引切换到 store.asearch 前缀遍历（2026-09-12）。

不依赖 pytest，直接：python tests/spikes/asearch_session_list.py

与既有 spike 一致的取向：**不做逻辑复刻**。用 ast 从 sessions.py 真实源码中
提取 `_alist_sessions` 与相关常量并 exec 执行——测的是真实代码。若将来有人
把越权过滤删掉、或把 limit 去掉（退回默认 10 静默截断），本脚本立刻失败。

背景：`list_sessions` 原实现维护一个 `__index__` key 作为会话 id 列表，
注释理由是「store 不直接支持遍历 namespace」。该前提错误：`BaseStore`
有 `asearch(namespace_prefix, *, limit=10, offset=0, ...)`
（langgraph/store/base/__init__.py:1021），Postgres 侧走 `prefix LIKE`
并有 `store_prefix_idx ... text_pattern_ops` 支撑。
代价是数据与索引两次独立写、非原子，任一失败即分叉（历史 bug 来源）。

改用 asearch 后必须自己承担两个 langgraph 不管的约束，本脚本就是钉这两条：
  1. `limit` 默认 10，会**静默截断** → 必须显式传，且分页时偏移正确
  2. 前缀匹配**不认命名空间边界**：`sessions.GY2442` 会捞到 `sessions.GY24428`
     的数据 → 必须按 `Item.namespace` 精确过滤，否则串用户（越权）

覆盖：
  1. 正常：3 个会话全部返回，字段透传
  2. 越权：GY2442 检索时混入 GY24428 条目 → 必须被过滤，且不泄漏
  3. 历史遗留 `__index__` 条目被跳过
  4. 非 dict 的 value 被跳过（不抛异常）
  5. 分页：> 单页条数时能翻页取全，且不重不漏
  6. 源码级断言：asearch 调用必须显式传 limit；必须存在 namespace 相等过滤
"""

from __future__ import annotations

import ast
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

SESSIONS_PY = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "api" / "endpoints" / "sessions.py"
)

# ── 从真实源码提取 _alist_sessions + 相关常量（不 import 模块，避开 fastapi 依赖）──
_src = SESSIONS_PY.read_text(encoding="utf-8")
_tree = ast.parse(_src)

_WANTED_CONSTS = {"_SESSION_MAX_ITEMS", "_LEGACY_INDEX_KEY"}
_WANTED_FUNCS = {"_alist_sessions"}

_nodes: list[ast.stmt] = []
for node in _tree.body:
    if isinstance(node, ast.Assign):
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id in _WANTED_CONSTS:
                _nodes.append(node)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _WANTED_FUNCS:
        _nodes.append(node)

_missing = _WANTED_FUNCS - {
    n.name for n in _nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
}
assert not _missing, f"sessions.py 中找不到这些定义: {_missing}"

# 记录 warning 调用，验证越权时会告警
_warnings: list[str] = []


class _Logger:
    def warning(self, fmt, *args, **kwargs):
        try:
            _warnings.append(fmt % args)
        except Exception:
            _warnings.append(str(fmt))

    def error(self, fmt, *args, **kwargs):
        _warnings.append("ERROR: " + str(fmt))


_ns: dict = {"logger": _Logger(), "list": list, "dict": dict, "tuple": tuple,
             "len": len, "isinstance": isinstance, "logger_warn": _warnings}
exec(
    compile(
        ast.fix_missing_locations(ast.Module(body=_nodes, type_ignores=[])),
        "<sessions.py::_alist_sessions>",
        "exec",
    ),
    _ns,
)
alist_sessions = _ns["_alist_sessions"]
MAXI = _ns["_SESSION_MAX_ITEMS"]
LEGACY = _ns["_LEGACY_INDEX_KEY"]

print(f"提取成功: MAX={MAXI} LEGACY={LEGACY!r}")


# ── 假 store：忠实模拟 Postgres 的 `prefix LIKE 'ns%'` 语义 ──
def _ns_text(ns: tuple) -> str:
    return ".".join(ns)


def make_item(namespace: tuple, key: str, value):
    return SimpleNamespace(
        namespace=tuple(namespace),
        key=key,
        value=value,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


class FakeStore:
    """按 prefix LIKE 检索，故意实现 langgraph 的 limit=10 默认值截断行为。"""

    def __init__(self, items: list):
        self.items = items
        self.calls: list[dict] = []

    async def asearch(self, namespace, **kwargs):
        limit = kwargs.get("limit", 10)          # ← 与 langgraph 默认值一致
        offset = kwargs.get("offset", 0)
        # 记录调用方**实际传入**的实参，用于断言 offset 有没有被用到
        self.calls.append({"namespace": namespace, "limit": limit, "offset": offset,
                           "raw_kwargs": dict(kwargs)})
        prefix = _ns_text(namespace)
        # 关键：LIKE 'prefix%' 会把更长的同前缀 namespace 一并命中
        matched = [
            it for it in self.items
            if _ns_text(it.namespace).startswith(prefix)
        ]
        return matched[offset: offset + limit]


_FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        _FAILS.append(label)


def run(coro):
    import asyncio
    return asyncio.run(coro)


# ── 用例 1：正常返回 ──
print("\n[1] 正常：3 个会话全部返回，字段透传")
ns_a = ("sessions", "GY24428")
store = FakeStore([
    make_item(ns_a, "s1", {"title": "会话一", "updated_at": "2026-09-01", "pinned": False}),
    make_item(ns_a, "s2", {"title": "会话二", "updated_at": "2026-09-02", "pinned": True}),
    make_item(ns_a, "s3", {"title": "会话三", "updated_at": "2026-09-03"}),
])
rows = run(alist_sessions(store, ns_a))
check(len(rows) == 3, f"返回 3 条（实际 {len(rows)}）")
check({r["session_id"] for r in rows} == {"s1", "s2", "s3"}, "session_id 正确注入")
check(any(r.get("title") == "会话二" for r in rows), "title 字段透传")
check(all(c["limit"] != 10 for c in store.calls),
      f"limit 显式传入且不等于默认 10（实际 {[c['limit'] for c in store.calls]}）")

# ── 用例 2：越权防护（本 spike 的核心）──
print("\n[2] 越权：GY2442 检索捞到 GY24428 的条目 → 必须过滤")
_warnings.clear()
ns_short = ("sessions", "GY2442")
store = FakeStore([
    make_item(ns_short, "mine", {"title": "我自己的会话"}),
    # 下面这条会被 prefix LIKE 'sessions.GY2442%' 命中，但属于别人
    make_item(("sessions", "GY24428"), "victim", {"title": "别人的机密会话"}),
])
rows = run(alist_sessions(store, ns_short))
ids = {r["session_id"] for r in rows}
check(ids == {"mine"}, f"只返回本人会话（实际 {ids}）")
check("victim" not in ids, "未泄漏 GY24428 的会话（串用户防护生效）")
check(any("命名空间不匹配" in w for w in _warnings), "越权命中时打出 warning 日志")

# ── 用例 3：历史遗留 __index__ 被跳过 ──
print("\n[3] 历史遗留 __index__ 条目被跳过")
store = FakeStore([
    make_item(ns_a, LEGACY, {"items": ["s1", "s2"]}),   # 老索引，无业务字段
    make_item(ns_a, "s1", {"title": "会话一"}),
])
rows = run(alist_sessions(store, ns_a))
check(len(rows) == 1 and rows[0]["session_id"] == "s1",
      f"只返回真实会话，跳过 __index__（实际 {[r['session_id'] for r in rows]}）")

# ── 用例 4：非 dict value 被跳过且不抛异常 ──
print("\n[4] 非 dict 的 value 被跳过（不抛异常）")
store = FakeStore([
    make_item(ns_a, "bad", ["not", "a", "dict"]),
    make_item(ns_a, "good", {"title": "正常"}),
])
try:
    rows = run(alist_sessions(store, ns_a))
    check(len(rows) == 1 and rows[0]["session_id"] == "good",
          f"跳过脏数据后仍返回正常条目（实际 {[r['session_id'] for r in rows]}）")
except Exception as e:  # noqa: BLE001
    check(False, f"不应抛异常，但抛出 {type(e).__name__}: {e}")

# ── 用例 5：单次取全（不再 offset 翻页）──
print("\n[5] 单次取全：造 120 条（超过 langgraph 默认 limit 10）→ 一次调用全取回")
n = 120
store = FakeStore([make_item(ns_a, f"s{i:04d}", {"title": f"会话{i}"}) for i in range(n)])
rows = run(alist_sessions(store, ns_a))
ids = [r["session_id"] for r in rows]
check(len(rows) == n, f"取全 {n} 条（实际 {len(rows)}）")
check(len(set(ids)) == n, f"无重复（去重后 {len(set(ids))}）")
check(len(store.calls) == 1, f"只调用一次 asearch（实际 {len(store.calls)} 次）")
check(store.calls[0]["limit"] == MAXI,
      f"显式传 limit={MAXI}（实际 {store.calls[0]['limit']}）")
check("offset" not in store.calls[0]["raw_kwargs"],
      f"未向 asearch 传 offset（实参 {sorted(store.calls[0]['raw_kwargs'])}）")

# ── 用例 6：拉取上限生效 ──
print(f"\n[6] 拉取上限：造 {MAXI + 50} 条 → 最多取 {MAXI} 条并告警")
_warnings.clear()
store = FakeStore([make_item(ns_a, f"x{i:05d}", {"title": "t"}) for i in range(MAXI + 50)])
rows = run(alist_sessions(store, ns_a))
check(len(rows) <= MAXI, f"不超过上限 {MAXI}（实际 {len(rows)}）")
check(any("上限" in w for w in _warnings), "达到上限时打出 warning")

# ── 用例 7：源码级断言（防止改回默认 limit / 删掉越权过滤）──
print("\n[7] 源码级断言")
fn_src = ast.get_source_segment(_src, next(
    n for n in _tree.body
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_alist_sessions"
))
check("limit=" in fn_src, "asearch 调用显式传入 limit（否则退回默认 10 静默截断）")
check("offset=" not in fn_src,
      "代码中未使用 offset= 实参（Postgres 按 updated_at 排序，翻页会漏条/重复）")
check("item.namespace" in fn_src, "存在按 Item.namespace 的过滤（越权防护）")
check("_LEGACY_INDEX_KEY" in fn_src, "跳过历史 __index__ 条目")

# 全文件：不应再有索引写入
check('"__index__"' not in _src.replace('_LEGACY_INDEX_KEY = "__index__"', ""),
      "sessions.py 中不再有 __index__ 字符串字面量写入点")
chat_src = (SESSIONS_PY.parent / "chat.py").read_text(encoding="utf-8")
check('aput(session_ns, "__index__"' not in chat_src,
      "chat.py 中不再写 __index__ 索引")
check("_update_session_index" not in _src,
      "sessions.py 中 _update_session_index 函数已移除")

# ── 汇总 ──
print("\n" + "=" * 62)
if _FAILS:
    print(f"FAILED: {len(_FAILS)} 项")
    for f in _FAILS:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")

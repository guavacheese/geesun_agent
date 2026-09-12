"""Spike: 会话列表走稳定 keyset 游标分页（2026-09-12 二次改造）。

不依赖 pytest，直接：python tests/spikes/asearch_session_list.py

与既有 spike 一致的取向：**不做逻辑复刻**。用 ast 从 `sessions.py` / `database.py`
真实源码里提取被改造的函数与常量并 exec 执行——测的是真实代码，不是副本。
若将来有人把 keyset 换回 `asearch`、把游标去掉、或退回 offset 翻页，本脚本立刻失败。

演进脉络：
  ① 原实现维护 `__index__` key 作会话 id 列表（数据+索引两次非原子写 → 分叉隐患）
  ② 改为 `asearch` 前缀遍历 + `limit=1000` 一次取全（消掉了分叉，但留下静默截断）
  ③ 本次：`asearch_keyset` —— `prefix = %s` 精确匹配 + `(updated_at, key)` 元组游标
     + 全序排序。彻底消掉 asearch 的三个坑：
       - `ORDER BY store.updated_at DESC` 单列非唯一 → 无全序，OFFSET 翻页漏条/重复
       - `prefix LIKE` 不认命名空间边界 → 越权过滤只能放应用层，与数据库端 OFFSET
         计数错位 → **分页在结构上不可能正确**
       - `limit` 默认 10 → 不显式传就静默截断

覆盖：
  1. 取全：跨多页推进，不重不漏
  2. 跨页边界上的 tie（同 updated_at）不漏不重（key 破平局）
  3. 分页模式：next_cursor 续页拼接 == 取全结果
  4. 缺 updated_at 的脏条目排末尾，且不破坏游标推进
  5. 历史遗留 `__index__` 被跳过；非 dict 的 value 被跳过
  6. 游标未推进（排序键错乱）→ 停止并打 error，不进入死循环
  7. 取全触达防呆页数上限 → 打 error（不是静默截断）
  8. 非法 cursor → HTTPException（不能当成"从头发"）
  9. 源码级断言：sessions.py 不再调用 asearch；SQL 不含 LIKE / OFFSET
"""

from __future__ import annotations

import ast
import base64
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SESSIONS_PY = ROOT / "src" / "api" / "endpoints" / "sessions.py"
DATABASE_PY = ROOT / "src" / "infra" / "database.py"


# ─── 通用：ast 提取指定常量 / 函数并 exec ───
def extract(path: pathlib.Path, consts: set[str], funcs: set[str]) -> tuple[dict, str, ast.Module]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    nodes: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in consts:
                    nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in funcs:
            nodes.append(node)
    missing = funcs - {
        n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    found_consts = {
        t.id for n in nodes if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    assert not missing, f"{path.name} 中找不到这些函数: {missing}"
    assert not (consts - found_consts), f"{path.name} 中找不到这些常量: {consts - found_consts}"

    ns: dict = {
        "base64": base64, "str": str, "int": int, "len": len, "list": list,
        "dict": dict, "tuple": tuple, "range": range, "type": type,
        "isinstance": isinstance, "Exception": Exception,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
            f"<{path.name}>",
            "exec",
        ),
        ns,
    )
    return ns, src, tree


# ─── 记录日志调用（验证告警/错误确实打了） ───
_logs: list[str] = []


class _Logger:
    def _rec(self, level: str, fmt, args) -> None:
        try:
            _logs.append(f"{level}: {fmt % args}")
        except Exception:  # noqa: BLE001
            _logs.append(f"{level}: {fmt}")

    def warning(self, fmt, *args, **kwargs) -> None:
        self._rec("WARN", fmt, args)

    def error(self, fmt, *args, **kwargs) -> None:
        self._rec("ERROR", fmt, args)

    def info(self, fmt, *args, **kwargs) -> None:
        self._rec("INFO", fmt, args)


class HTTPException(Exception):
    def __init__(self, status_code: int | None = None, detail=None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ─── 提取 sessions.py 的被测单元 ───
_sess_ns, _sess_src, _sess_tree = extract(
    SESSIONS_PY,
    consts={"_SESSION_PAGE_SIZE", "_SESSION_MAX_PAGES", "_SESSION_PAGE_LIMIT",
            "_LEGACY_INDEX_KEY", "_CURSOR_SEP"},
    funcs={"_ns_text", "_session_namespace", "_encode_cursor", "_decode_cursor",
           "_to_session_row", "_alist_sessions", "_cursor_of"},
)
_sess_ns["logger"] = _Logger()
_sess_ns["HTTPException"] = HTTPException
# 再 exec 一遍，让函数闭包看到注入的 logger / HTTPException
_bootstrap = _sess_ns["_alist_sessions"].__globals__
_bootstrap["logger"] = _sess_ns["logger"]
_bootstrap["HTTPException"] = HTTPException

alist_sessions = _sess_ns["_alist_sessions"]
PAGE_SIZE = _sess_ns["_SESSION_PAGE_SIZE"]
MAX_PAGES = _sess_ns["_SESSION_MAX_PAGES"]
LEGACY = _sess_ns["_LEGACY_INDEX_KEY"]
ns_text = _sess_ns["_ns_text"]
session_namespace = _sess_ns["_session_namespace"]

print(f"提取成功: PAGE_SIZE={PAGE_SIZE} MAX_PAGES={MAX_PAGES} LEGACY={LEGACY!r}")


# ─── 假 store：忠实模拟 asearch_keyset 的 SQL 语义 ───
class FakeStore:
    """精确 prefix 匹配（非 LIKE）+ (updated_at, key) 全序 + 元组游标 + pinned 过滤。

    `broken=True` 时忽略游标、永远返回同一页 —— 模拟排序键错乱的脏数据，
    用来验证调用方的"游标未推进"保护真的会触发。
    """

    def __init__(self, items: list[tuple[str, str, object]], broken: bool = False):
        self.items = items
        self.broken = broken
        self.calls: list[dict] = []

    @staticmethod
    def _sk(value) -> str:
        return value.get("updated_at", "") if isinstance(value, dict) else ""

    async def asearch_keyset(self, prefix, *, limit, cursor=None, pinned=None):
        self.calls.append(
            {"prefix": prefix, "limit": limit, "cursor": cursor, "pinned": pinned}
        )
        rows = [(k, v) for (p, k, v) in self.items if p == prefix]  # ← 精确相等
        if pinned is True:
            rows = [(k, v) for k, v in rows
                    if isinstance(v, dict) and v.get("pinned") is True]
        elif pinned is False:
            rows = [(k, v) for k, v in rows
                    if not (isinstance(v, dict) and v.get("pinned") is True)]
        rows.sort(key=lambda kv: (self._sk(kv[1]), kv[0]), reverse=True)
        if cursor is not None and not self.broken:
            rows = [kv for kv in rows if (self._sk(kv[1]), kv[0]) < cursor]
        return rows[:limit]


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


NS = ("sessions", "GY24428")
PFX = ns_text(NS)


def ts_of(i: int) -> str:
    """第 i 条的时间戳：单调递增且**字面量长度恒定**。

    直接用 `f"{i:02d}"` 会踩坑——i=100 时变成 "100"（3 位），
    字符串序在 "00:99" 与 "00:100" 之间反转，测出的"顺序不对"是造数的错，
    不是被测代码的错。
    """
    h, rem = divmod(i, 3600)
    m, s = divmod(rem, 60)
    return f"2026-09-01T{h:02d}:{m:02d}:{s:02d}.000000+00:00"


def mk(n: int, *, prefix: str = PFX) -> list[tuple[str, str, dict]]:
    """造 n 条会话，updated_at 单调递增（编号越大越新），便于断言顺序。"""
    return [
        (prefix, f"s{i:05d}", {"title": f"会话{i}", "updated_at": ts_of(i)})
        for i in range(n)
    ]


# ── 用例 1：取全（跨多页）──
print("\n[1] 取全：450 条（跨 3 页 200/200/50）→ 不重不漏")
n = 450
store = FakeStore(mk(n))
rows, next_cursor = run(alist_sessions(store, PFX))
ids = [r["session_id"] for r in rows]
check(len(rows) == n, f"取全 {n} 条（实际 {len(rows)}）")
check(len(set(ids)) == n, f"无重复（去重后 {len(set(ids))}）")
check(len(store.calls) == 3, f"分 3 页拉取（实际 {len(store.calls)} 次）")
check(all(c["limit"] == PAGE_SIZE for c in store.calls),
      f"每页 limit 都是显式 {PAGE_SIZE}（实际 {[c['limit'] for c in store.calls]}）")
check(store.calls[0]["cursor"] is None, "首页不带 cursor")
check(all(c["cursor"] is not None for c in store.calls[1:]), "后续页都带 cursor")
check(ids == sorted(ids, reverse=True), "结果按 updated_at+key 降序（编号大的在前）")
check(next_cursor is None, "取全模式不返回 next_cursor")
check(store.calls[0]["prefix"] == PFX, f"传的是精确 prefix（{store.calls[0]['prefix']}）")

# ── 用例 2：全表同一 updated_at → 每个页边界都是 tie ──
print("\n[2] 全部同 updated_at（每个页边界都是 tie）→ 不漏不重，退化为按 key 降序")
_same_ts = ts_of(0)
items = [(PFX, f"t{i:05d}", {"title": "t", "updated_at": _same_ts}) for i in range(n)]
store = FakeStore(items)
rows, _ = run(alist_sessions(store, PFX))
ids = [r["session_id"] for r in rows]
check(len(rows) == n, f"取全 {n} 条（实际 {len(rows)}）")
check(len(set(ids)) == n, f"tie 未导致重复（去重后 {len(set(ids))}）")
check(set(ids) == {f"t{i:05d}" for i in range(n)}, "tie 未导致漏条")
check(ids == sorted(ids, reverse=True), "同时间戳时退化为 key 降序（全序仍成立）")
check(store.calls[1]["cursor"][0] == _same_ts,
      "第 2 页游标确实落在 tie 组内（页边界就是 tie）")

# ── 用例 3：分页模式，续页拼接收敛到取全 ──
print("\n[3] 分页模式：limit=7 逐页续拉 == 取全结果")
mk_items = mk(53)
full_rows, _ = run(alist_sessions(FakeStore(list(mk_items)), PFX))
full_ids = [r["session_id"] for r in full_rows]

paged_ids, cursor, pages = [], None, 0
while True:
    store = FakeStore(list(mk_items))
    page, cursor = run(alist_sessions(store, PFX, limit=7, cursor=cursor))
    paged_ids.extend(r["session_id"] for r in page)
    pages += 1
    if cursor is None:
        break
    check(store.calls[0]["limit"] == 7, f"第 {pages} 页 limit=7")
check(paged_ids == full_ids, f"分页拼接 == 取全（{len(paged_ids)} 条，{pages} 页）")
check(len(paged_ids) == len(set(paged_ids)), "分页无重复")

# 末尾页不满 → next_cursor 必须为 None（否则前端会无限翻）
store = FakeStore(mk(5))
page, cursor = run(alist_sessions(store, PFX, limit=7))
check(len(page) == 5 and cursor is None, "不足一页时 next_cursor=None")

# ── 用例 4：缺 updated_at 的脏条目排末尾 ──
print("\n[4] 缺 updated_at 的条目：排末尾且不破坏游标")
items = mk(3) + [(PFX, "no_ts_1", {"title": "缺时间"}), (PFX, "no_ts_2", {"title": "缺时间2"})]
store = FakeStore(items)
rows, _ = run(alist_sessions(store, PFX))
ids = [r["session_id"] for r in rows]
check(len(rows) == 5, f"5 条全部返回（实际 {len(rows)}）")
check(ids[-2:] == ["no_ts_2", "no_ts_1"], f"缺时间的排在末尾（实际 {ids[-2:]}）")
# 分页穿越末尾脏条目
paged, cursor2 = [], None
while True:
    st = FakeStore(list(items))
    page, cursor2 = run(alist_sessions(st, PFX, limit=2, cursor=cursor2))
    paged.extend(r["session_id"] for r in page)
    if cursor2 is None:
        break
check(len(paged) == 5 and len(set(paged)) == 5, f"分页同样不重不漏（{paged}）")

# ── 用例 5：__index__ 与非 dict 被跳过 ──
print("\n[5] 历史 __index__ 与非 dict value 被跳过")
store = FakeStore([
    (PFX, LEGACY, {"items": ["s1", "s2"]}),
    (PFX, "s1", {"title": "会话一"}),
    (PFX, "bad", ["not", "a", "dict"]),
])
_logs.clear()
rows, _ = run(alist_sessions(store, PFX))
ids = [r["session_id"] for r in rows]
check(ids == ["s1"], f"只返回可用条目（实际 {ids}）")
check(any("不是 dict" in w for w in _logs), "非 dict 时打 warning")

# ── 用例 6：游标未推进 → 停止 + error（不死循环）──
print("\n[6] 排序键错乱（游标不推进）→ 停止并打 error")
_logs.clear()
store = FakeStore(mk(300), broken=True)
rows, _ = run(alist_sessions(store, PFX))
check(len(rows) == 2 * PAGE_SIZE, f"拉满 2 页后停止（实际 {len(rows)} 条）")
check(any("游标未推进" in w for w in _logs), "打出「游标未推进」error")
check(len(store.calls) == 2, f"只请求了 2 次（实际 {len(store.calls)}）")

# ── 用例 7：取全触达防呆上限 ──
print(f"\n[7] 触达防呆上限 {MAX_PAGES} 页 → 打 error（非静默截断）")
total = PAGE_SIZE * MAX_PAGES + 137      # 比上限多，必然触顶
_logs.clear()
store = FakeStore(mk(total))
rows, _ = run(alist_sessions(store, PFX))
check(len(rows) == PAGE_SIZE * MAX_PAGES, f"取到上限条数（实际 {len(rows)}）")
check(any("防呆上限" in w for w in _logs), "打出「防呆上限」error")
check(len(rows) < total, f"确实少于总数 {total}（说明触顶被显式告警，而非静默）")

# ── 用例 8：非法 cursor ──
print("\n[8] 非法 cursor → HTTPException（400）")
for bad in ("!!!not-base64!!!", base64.urlsafe_b64encode(b"noseparator").decode().rstrip("=")):
    try:
        run(alist_sessions(FakeStore(mk(3)), PFX, limit=2, cursor=bad))
        check(False, f"非法 cursor 应报错: {bad[:16]}...")
    except HTTPException as e:
        check(e.status_code == 400, f"非法 cursor 报 400（{bad[:16]}...）")

# ── 用例 9：源码级断言 ──
print("\n[9] 源码级断言")

# sessions.py：不得再调用 .asearch(（ast 精确判断，避开注释里提到的 asearch）
_asearch_calls = [
    node for node in ast.walk(_sess_tree)
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Attribute)
    and node.func.attr == "asearch"
]
check(not _asearch_calls, "sessions.py 不再调用 store.asearch（避免其三个坑）")

_keyset_calls = [
    node for node in ast.walk(_sess_tree)
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Attribute)
    and node.func.attr == "asearch_keyset"
]
check(len(_keyset_calls) >= 2, f"取全与分页两条路径都走 asearch_keyset（{len(_keyset_calls)} 处）")

_offset_kwargs = [
    kw for node in ast.walk(_sess_tree)
    if isinstance(node, ast.Call)
    for kw in node.keywords if kw.arg == "offset"
]
check(not _offset_kwargs, "代码中没有任何 offset= 实参")
check("_encode_cursor" in _sess_src and "_decode_cursor" in _sess_src, "游标编解码存在")

# database.py：SQL 形状不得退回 LIKE / OFFSET
_db_ns, _db_src, _db_tree = extract(
    DATABASE_PY,
    consts={"_SORT_KEY_EXPR", "_SESSIONS_ORDER_INDEX", "_SESSIONS_ORDER_INDEX_DDL"},
    funcs={"_build_keyset_query"},
)
build_sql = _db_ns["_build_keyset_query"]
_sort_expr = _db_ns["_SORT_KEY_EXPR"]
_index_ddl = _db_ns["_SESSIONS_ORDER_INDEX_DDL"]
sql_first = build_sql(with_cursor=False, pinned=None)
sql_cursor = build_sql(with_cursor=True, pinned=False)
sql_pinned = build_sql(with_cursor=False, pinned=True)
check("prefix = %s" in sql_first, "SQL 用 prefix = %s 精确匹配（越权问题从根上消失）")
check("LIKE" not in sql_first.upper(), "SQL 不含 LIKE")
check("OFFSET" not in sql_cursor.upper(), "SQL 不含 OFFSET（改用元组游标）")
check("(%s::text, %s::text)" in sql_cursor, "游标是元组比较 (ts, key) < (c1, c2)")
check(sql_first.count("ORDER BY") == 1 and ", key DESC" in sql_first,
      "ORDER BY 是两列全序（updated_at DESC, key DESC）")
check("COLLATE" in sql_first, "排序键显式 COLLATE（不随库 collation 漂移）")
check("COALESCE" in sql_first, "NULL 排序键被 COALESCE 兜住")
check(sql_first.count(_sort_expr) == 1, "排序键表达式在 ORDER BY 中只出现一次")
check("IS DISTINCT FROM 'true'" in sql_cursor, "非置顶过滤使用 NULL 安全比较")
check("value->>'pinned' = 'true'" in sql_pinned, "置顶过滤存在")

# database.py：索引表达式必须与查询排序键**是同一个表达式**（否则索引用不上）
check(_db_ns["_SESSIONS_ORDER_INDEX"] == "store_sessions_order_idx",
      "索引名常量为 store_sessions_order_idx")
check(_sort_expr in _index_ddl,
      "索引表达式与查询排序键是同一个表达式（用同一常量拼装）")
check("prefix" in _index_ddl and _index_ddl.rstrip().endswith("key DESC)"),
      f"索引列为 (prefix, <排序键> DESC, key DESC)：{_index_ddl}")

# 写入路径未被动过：aput 仍是唯一写入口
check("store.aput" in _sess_src, "写入路径仍走 aput（本改造只换读路径）")

# ── 汇总 ──
print("\n" + "=" * 62)
if _FAILS:
    print(f"FAILED: {len(_FAILS)} 项")
    for f in _FAILS:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")

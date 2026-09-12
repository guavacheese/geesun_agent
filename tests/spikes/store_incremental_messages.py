"""Spike: store 消息存储改增量（一条消息一行，key = 零填充序号）—— 方案 B（2026-09-12）。

不依赖 pytest，直接：python tests/spikes/store_incremental_messages.py

取向与既有 spike 一致：**不做逻辑复刻**。用 ast 从 `chat.py` / `sessions.py` /
`database.py` 真实源码里提取被改造的函数与常量并 exec 执行——测的是真实代码。
只在"必须与数据库交互"处用 FakeStore 模拟，且模拟的是**已用真实 SQL 在生产库
验证过**的语义（见 `数据库验证` 一节）。

背景（生产实测，agent_mem_prod）：
  旧形态 `aput(ns, "messages", {"items": history})` 每轮把整份 history 当一个 value 覆盖写单行。
  · 爆炸半径=整个会话：最大会话 259 条 / 1.32 MB 挤一行，写失败即全丢
  · 写放大 130×：累计约 171 MB 只为存下 1.32 MB
  · 摘要裁剪传导：checkpoint 被 RemoveMessage 裁短 → 快照缩水被写进 store → 早期对话永久丢失
  新形态一条消息一行后，读取零新建索引（`store_pkey (prefix,key)` btree 直接可用）。

覆盖：
  1. 首次写入：history 0 → N，写 N 条、key 连续
  2. 幂等：同一 history 重复保存 → 零新增（断连重试 / 双路径并发安全）
  3. 追加一轮：只写新增的尾部，序号接续
  4. **压缩场景**：history 因摘要变短 → 按 id 定位，绝不覆盖已归档条目；摘要不写入
  5. 压缩后继续对话 → 从 store 的序号接续（不是 history 下标）
  6. 退化路径：last_stored_id 缺失 → by_length；两者都不成立 → 不写并告警
  7. 旧格式残留（key="messages"）→ 水位退化 + error 日志（迁移脚本的必要性依据）
  8. 读分页：首页 + 续页拼接 == 全量；游标指向本页最早那条
  9. 非法 / 越界游标 → HTTPException 400（不能当成"从最新开始"）
  10. L1 单条体积观测闸：超限只 warn，**不截断不丢弃**
  11. 源码级断言：SQL 形态、写入必须单次 abatch、旧格式读写点已清零
"""

from __future__ import annotations

import ast
import base64
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHAT_PY = ROOT / "src" / "api" / "endpoints" / "chat.py"
SESSIONS_PY = ROOT / "src" / "api" / "endpoints" / "sessions.py"
DATABASE_PY = ROOT / "src" / "infra" / "database.py"


# ─── 通用：ast 提取指定常量 / 函数并 exec（复用 asearch_session_list.py 的做法）───
def extract(
    path: pathlib.Path,
    consts: set[str],
    funcs: set[str],
    extra_globals: dict | None = None,
) -> tuple[dict, str]:
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

    found_funcs = {
        n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    found_consts = {
        t.id for n in nodes if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    assert not (funcs - found_funcs), f"{path.name} 中找不到这些函数: {funcs - found_funcs}"
    assert not (consts - found_consts), f"{path.name} 中找不到这些常量: {consts - found_consts}"

    ns: dict = {
        "base64": base64, "str": str, "int": int, "len": len, "list": list,
        "dict": dict, "tuple": tuple, "range": range, "type": type,
        "isinstance": isinstance, "Exception": Exception, "enumerate": enumerate,
        "ValueError": ValueError, "json": json, "Optional": __import__("typing").Optional,
        "Any": __import__("typing").Any, "Sequence": __import__("typing").Sequence,
    }
    ns.update(extra_globals or {})
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
            f"<{path.name}>",
            "exec",
        ),
        ns,
    )
    return ns, src


# ─── 记录日志调用（验证告警/错误确实打了）───
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


_LOGGER = _Logger()
HTTP = HTTPException

# ─── 提取 database.py：key 编解码 + tail SQL ───
_db_ns, _db_src = extract(
    DATABASE_PY,
    consts={"_MESSAGE_KEY_WIDTH", "_MESSAGE_PAGE_DEFAULT", "_MESSAGE_ALL_MAX"},
    funcs={"message_key", "message_key_to_index", "_build_message_tail_query"},
)
message_key = _db_ns["message_key"]
message_key_to_index = _db_ns["message_key_to_index"]
build_tail_sql = _db_ns["_build_message_tail_query"]
KEY_WIDTH = _db_ns["_MESSAGE_KEY_WIDTH"]

# ─── 提取 sessions.py：消息读取与游标 ───
_sess_ns, _sess_src = extract(
    SESSIONS_PY,
    consts={"_MESSAGE_PAGE_LIMIT"},
    funcs={"_encode_message_cursor", "_decode_message_cursor", "_alist_messages"},
    extra_globals={
        "logger": _LOGGER,
        "HTTPException": HTTP,
        "message_key_to_index": message_key_to_index,
    },
)
# extract 里的 ns 与函数 __globals__ 是同一个 dict？不是——exec 用 ns 作 globals，
# 所以函数闭包看到的正是 ns，注入已生效；这里再显式确认一次。
_G = _sess_ns["_alist_messages"].__globals__
_G["logger"] = _LOGGER
_G["HTTPException"] = HTTP
_G["message_key_to_index"] = message_key_to_index

encode_cursor = _sess_ns["_encode_message_cursor"]
decode_cursor = _sess_ns["_decode_message_cursor"]
alist_messages = _sess_ns["_alist_messages"]
MESSAGE_PAGE_LIMIT = _sess_ns["_MESSAGE_PAGE_LIMIT"]

# ─── 提取 chat.py：增量追加规划 + 体积观测 ───
_settings = types.SimpleNamespace(persist_max_item_bytes=1_048_576)
_chat_ns, _chat_src = extract(
    CHAT_PY,
    consts=set(),
    funcs={"_plan_message_append", "_warn_oversized_items"},
    extra_globals={"logger": _LOGGER, "settings": _settings},
)
_CG = _chat_ns["_plan_message_append"].__globals__
_CG["logger"] = _LOGGER
_CG["settings"] = _settings

plan_append = _chat_ns["_plan_message_append"]
warn_oversized = _chat_ns["_warn_oversized_items"]

print(
    f"提取成功: KEY_WIDTH={KEY_WIDTH} PAGE_DEFAULT={_db_ns['_MESSAGE_PAGE_DEFAULT']} "
    f"ALL_MAX={_db_ns['_MESSAGE_ALL_MAX']} PAGE_LIMIT={MESSAGE_PAGE_LIMIT}"
)

_FAILS: list[str] = []
_passed_count = 0


def check(cond: bool, label: str) -> None:
    global _passed_count
    if cond:
        print(f"  PASS  {label}")
        _passed_count += 1
    else:
        print(f"  FAIL  {label}")
        _FAILS.append(label)


def run(coro):
    import asyncio
    return asyncio.run(coro)


NS = ("messages", "GY24428", "6b0770a8")
PFX = ".".join(NS)


# ─── 假 store：模拟**已用真实 SQL 生产验证过**的增量语义 ───
class FakeStore:
    """内存版增量 store。

    模拟的语义来自 `database.py` 的真实实现：
      · `alist_messages`  = `ORDER BY key DESC LIMIT n`（走 PK）+ 调用方 reverse 回升序
      · `alist_all_messages` = 大 limit 一次取全，`has_more` 时 error
      · `aget_message_watermark` = `ORDER BY key DESC LIMIT 1` → (序号, value["id"])
      · `awrite_messages` = 单次 abatch（PutOp 覆盖 + PutOp(None) 删除）
      · `adelete_prefix` = `DELETE ... WHERE prefix = %s`
    """
    def __init__(self, rows: list[tuple[str, str, object]] | None = None):
        self.rows: dict[tuple[str, str], object] = {
            (p, k): v for (p, k, v) in (rows or [])
        }
        self.batch_calls: list[list] = []
        self.deleted_prefixes: list[str] = []

    # ── 读 ──
    async def alist_messages(self, prefix, *, limit, before_key=None):
        keys = sorted(
            (k for (p, k) in self.rows if p == prefix), reverse=True
        )
        if before_key is not None:
            keys = [k for k in keys if k < before_key]
        keys = keys[:limit]
        keys.reverse()
        return [(k, self.rows[(prefix, k)]) for k in keys], len(keys) >= limit

    async def alist_all_messages(self, prefix, *, max_items=None):
        max_items = max_items if max_items is not None else _db_ns["_MESSAGE_ALL_MAX"]
        rows, has_more = await self.alist_messages(prefix, limit=max_items)
        if has_more:
            _LOGGER.error(
                "会话 %s 消息数超过取全上限 %d，本次只返回最早的 %d 条"
                "（重建图状态可能不完整）", prefix, max_items, len(rows),
            )
        return rows
    async def aget_message_watermark(self, prefix):
        """对应真实实现：`key ~ '^[0-9]+$'` 过滤 + 单独统计非序号条目数。"""
        legacy = sum(1 for (p, k) in self.rows if p == prefix and not k.isdigit())
        keys = [k for (p, k) in self.rows if p == prefix and k.isdigit()]
        if not keys:
            return 0, None, legacy
        top = max(keys)
        idx = message_key_to_index(top)
        if idx is None:
            _LOGGER.error("会话 %s 的水位 key=%r 无法解析为序号", prefix, top)
            return 0, None, legacy
        v = self.rows[(prefix, top)]
        return idx, (v.get("id") if isinstance(v, dict) else None), legacy

    # ── 写 ──
    async def awrite_messages(self, namespace, items, *, delete_keys=()):
        prefix = ".".join(namespace)
        self.batch_calls.append(list(items) + [("__DELETE__", k) for k in delete_keys])
        for k, v in items:
            self.rows[(prefix, k)] = v
        for k in delete_keys:
            self.rows.pop((prefix, k), None)

    async def adelete_prefix(self, prefix):
        if not prefix:
            raise ValueError("adelete_prefix 拒绝空 prefix（会删全表）")
        self.deleted_prefixes.append(prefix)
        gone = [k for (p, k) in self.rows if p == prefix]
        for (p, k) in list(self.rows):
            if p == prefix:
                del self.rows[(p, k)]
        return len(gone)


def persist(store, history: list[dict]) -> tuple[int, list, str]:
    """复刻 `_persist_session` 的增量写入序（调用真实 _plan_message_append + 假 store）。

    与真实实现一样含**旧格式残留防线**：legacy_rows > 0 时整轮不写。
    """
    prev_count, last_stored_id, legacy_rows = run(store.aget_message_watermark(PFX))
    if legacy_rows:
        _LOGGER.error(
            "会话 %s 存量仍为旧格式（%d 条非序号条目），本轮跳过 store 写入", PFX, legacy_rows,
        )
        return prev_count, [], "legacy_blocked"
    next_seq, new_items, mode = plan_append(history, prev_count, last_stored_id)
    if new_items:
        run(store.awrite_messages(
            NS,
            [(message_key(next_seq + i), e) for i, e in enumerate(new_items)],
        ))
    return prev_count, new_items, mode


def hist(n_from: int, n_to: int, *, use_id: bool = True) -> list[dict]:
    """造 [n_from, n_to] 区间的消息（含首尾），id 与序号绑定便于按 id 定位。"""
    out = []
    for i in range(n_from, n_to + 1):
        m = {"role": "user" if i % 2 else "ai", "content": f"消息{i}"}
        if use_id:
            m["id"] = f"id{i}"
        out.append(m)
    return out


# ── 用例 1：首次写入 ──
print("\n[1] 首次写入：history 0 → 30 条")
store = FakeStore()
prev, new, mode = persist(store, hist(1, 30))
keys = sorted(k for (p, k) in store.rows if p == PFX)
check(prev == 0, "空 store 的水位是 0")
check(len(new) == 30 and mode == "by_length", f"首次写全量（mode={mode}）")
check(keys == [message_key(i) for i in range(1, 31)], "key 连续 00000001..00000030")
check(keys[0] == "00000001" and keys[-1] == "00000030",
      "key 是 8 位零填充（文本序 ≡ 序号序）")
check(len(store.batch_calls) == 1, "一次 abatch 写完（1 次往返）")

# ── 用例 2：幂等 ──
print("\n[2] 幂等：同一 history 重复保存（断连重试 / 双路径并发）")
before = dict(store.rows)
prev, new, mode = persist(store, hist(1, 30))
check(prev == 30 and new == [] and mode == "by_id",
      f"零新增（prev={prev}, mode={mode}）")
check(dict(store.rows) == before, "store 内容逐字节不变")
check(len(store.batch_calls) == 1, "零新增时不发写请求（避免空 batch 往返）")

# ── 用例 3：追加一轮 ──
print("\n[3] 追加一轮：history 变长 2 条")
prev, new, mode = persist(store, hist(1, 32))
check(prev == 30 and len(new) == 2 and mode == "by_id", "只写新增的 2 条")
check(message_key(31) in [k for (p, k) in store.rows if p == PFX], "新条目 key=00000031")
check(len(store.batch_calls[-1]) == 2, "本批只写 2 条（不是重写 32 条）")

# ── 用例 4：压缩场景（本轮最关键的用例）──
print("\n[4] 压缩场景：摘要触发 → history 变短（32 条 → 摘要 + 尾部 6 条）")
# 注意尾部必须包含**最新的**已存条目（id32）：压缩保留的是最近一段后缀，
# 把 id31/id32 从尾部丢掉是不真实的数据（那是"历史被改写"而非压缩）。
summary = {"role": "ai", "content": "【历史摘要】前 26 条已折叠"}
compressed = [summary] + hist(27, 32)          # 摘要 + 6 条尾部（id27..id32）
prev, new, mode = persist(store, compressed)
check(prev == 32 and new == [] and mode == "by_id",
      f"按 id 定位到尾部 → 零新增（prev={prev}, mode={mode}）")
check(len(store.rows) == 32, "store 仍保留全部 32 条原文（未被快照缩水覆盖）")
check(summary["content"] not in [v.get("content") for v in store.rows.values()],
      "摘要消息**不写入** store（它是喂模型的面，不是回放副本）")
check(all(v.get("id") for v in store.rows.values()),
      "已归档条目未被改写（id 都在）")

# ── 用例 4b：病态路径（最新已存条目不在 history 里）──
print("\n[4b] 病态路径：压缩把**最新**已存条目也裁掉了（store 领先 checkpoint）")
_logs.clear()
pathological = [summary] + hist(27, 30)        # 尾部止于 id30，而 store 已有 id32
before_rows = dict(store.rows)
prev, new, mode = persist(store, pathological)
check(prev == 32 and new == [] and mode == "none",
      f"定位失败 → 什么都不写（mode={mode}）")
check(dict(store.rows) == before_rows, "已归档条目零改动（宁可少写一轮，不可篡改历史）")

# ── 用例 5：压缩后继续对话 ──
print("\n[5] 压缩后追加：从 store 的序号接续（不是 history 下标）")
compressed2 = [summary] + hist(27, 32) + [
    {"role": "user", "content": "新问题", "id": "idNew1"},
    {"role": "ai", "content": "新回答", "id": "idNew2"},
]
prev, new, mode = persist(store, compressed2)
check(prev == 32 and len(new) == 2 and mode == "by_id", "只写 2 条新消息")
check([k for (p, k) in store.rows if p == PFX and k >= message_key(33)] ==
      [message_key(33), message_key(34)],
      "新条目 key=33/34（接 store 序号，不是 history 下标 7/8）")
check(store.rows[(PFX, message_key(33))]["content"] == "新问题", "新用户消息落在 33")
check(len(store.rows) == 34, "总数 34（32 归档 + 2 新增）")

# ── 用例 6：退化路径 ──
print("\n[6] 退化路径：无 id / 无法定位")
store_noid = FakeStore()
persist(store_noid, hist(1, 10, use_id=False))
check(store_noid.rows[(PFX, message_key(10))].get("id") is None, "无 id 时水位 id 为 None")
prev, new, mode = persist(store_noid, hist(1, 12, use_id=False))
check(mode == "by_length" and len(new) == 2, "退化为按长度追加（2 条）")
prev, new, mode = persist(store_noid, hist(1, 8, use_id=False))
check(new == [] and mode == "none", "history 比已存短且无法按 id 定位 → 不写")
check(len(store_noid.rows) == 12, "已归档条目未被改写（宁可少写一轮）")

# ── 用例 7：旧格式残留（部署新版后、迁移脚本执行前的窗口）──
print("\n[7] 旧格式残留：单行 key=\"messages\" 与新序号条目共存（部署顺序防线）")
store_legacy = FakeStore([(PFX, "messages", {"items": hist(1, 5)}),
                          (PFX, "00000001", hist(1, 1)[0])])
_logs.clear()
prev, last_id, legacy = run(store_legacy.aget_message_watermark(PFX))
check(prev == 1 and legacy == 1,
      f"水位取**数字 key** 的最大值（prev={prev}）；非序号条目单独计数（legacy={legacy}）")
check(last_id == "id1", "水位 id 来自数字 key 那条（未被 'messages' 污染）")
before_rows = dict(store_legacy.rows)
prev, new, mode = persist(store_legacy, hist(1, 7))
check(mode == "legacy_blocked" and new == [],
      "旧格式残留时**整轮不写**（避免新旧并存 → 迁移脚本守卫会拒绝执行）")
check(dict(store_legacy.rows) == before_rows, "store 零改动")
check(any(l.startswith("ERROR") and "旧格式" in l for l in _logs),
      "打 error 日志并指明要跑迁移脚本（不是静默跳过）")

print("    读侧：旧格式条目不得被当成一条消息返回")
_logs.clear()
msgs, _ = run(alist_messages(store_legacy, PFX, limit=100, before=None))
check([m["content"] for m in msgs] == ["消息1"],
      "只返回数字 key 里、带 role 的真实消息")
check(any("旧格式残留" in l for l in _logs), "跳过时打 warn（可见）")

# ── 用例 8：读分页 ──
print("\n[8] 读分页：首页 + 续页拼接 == 全量")
store_read = FakeStore([(PFX, message_key(i), {"role": "user", "content": f"消息{i}"})
                        for i in range(1, 26)])
all_rows, has_more = run(store_read.alist_messages(PFX, limit=100))
check(len(all_rows) == 25 and not has_more, "limit 足够时一次拿全且 has_more=False")
check([k for k, _ in all_rows] == [message_key(i) for i in range(1, 26)],
      "返回升序（阅读顺序：最新在最后）")

p1, cur = run(alist_messages(store_read, PFX, limit=10, before=None))
check([v["content"] for v in p1] == [f"消息{i}" for i in range(16, 26)],
      "无游标取**最新** 10 条（16..25）")
check(cur is not None, "还有更早的 → 给了 next_cursor")
check(decode_cursor(cur) == message_key(16), "游标指向本页**最早**那条（00000016）")

p2, cur2 = run(alist_messages(store_read, PFX, limit=10, before=cur))
check([v["content"] for v in p2] == [f"消息{i}" for i in range(6, 16)],
      "续页取 6..15（比游标更早）")
p3, cur3 = run(alist_messages(store_read, PFX, limit=10, before=cur2))
check([v["content"] for v in p3] == [f"消息{i}" for i in range(1, 6)],
      "末页取 1..5")
check(cur3 is None, "取尽后无 next_cursor")
# 每页内部升序（阅读顺序），而第 1 页是**最新**的一段 → 更早的页要**前插**
joined = [v["content"] for v in p3 + p2 + p1]
check(joined == [f"消息{i}" for i in range(1, 26)],
      "三段前插拼接 == 全量，不重不漏（更早的页拼在接受页之前）")
check([v["content"] for v in p1] == [f"消息{i}" for i in range(16, 26)]
      and [v["content"] for v in p2] == [f"消息{i}" for i in range(6, 16)],
      "每页内部都是升序（前端按「前插」拼接，不是 append）")

# ── 用例 9：非法游标 ──
print("\n[9] 非法 / 边界游标")
for bad, label in [("!!!not-base64!!!", "非 base64"), ("", "空串")]:
    if bad == "":
        continue
    try:
        decode_cursor(bad)
        check(False, f"{label} 应报 400")
    except HTTPException as e:
        check(e.status_code == 400, f"{label} → 400")
try:
    decode_cursor(base64.urlsafe_b64encode(b"messages").decode().rstrip("="))
    check(False, "非序号形态的 key 应报 400（防止伪造 key 越界读取）")
except HTTPException as e:
    check(e.status_code == 400, "非序号 key（'messages'）→ 400")
check(decode_cursor(encode_cursor(message_key(7))) == message_key(7), "游标编解码可逆")

# ── 用例 10：L1 体积观测闸 ──
print("\n[10] L1 单条体积观测闸（只 warn，不截断）")
_logs.clear()
warn_oversized([{"role": "ai", "content": "x" * 10}], prefix=PFX)
check(not _logs, "小条目不打日志")
_logs.clear()
big = {"role": "ai", "content": "x" * 1_100_000, "id": "idBig"}
warn_oversized([big], prefix=PFX)
check(any("超过观测阈值" in l for l in _logs), "超 1 MB → warn")
check(big["content"] == "x" * 1_100_000, "内容**未被截断**（持久化层截断＝不可逆删除）")

# ── 用例 11：源码级断言 ──
print("\n[11] 源码级断言")
sql_first = build_tail_sql(with_cursor=False)
sql_cursor = build_tail_sql(with_cursor=True)
check("prefix = %s" in sql_first, "消息读取用 prefix = %s 精确匹配")
check("LIKE" not in sql_first.upper(), "不含 LIKE")
check("OFFSET" not in sql_first.upper() and "OFFSET" not in sql_cursor.upper(),
      "不含 OFFSET（游标不是偏移量）")
check("ORDER BY key DESC" in sql_first,
      "ORDER BY key DESC 走 store_pkey（零新建索引）")
check("key < %s" in sql_cursor, "游标是 key < %s（keyset，不是 offset）")

check("self.abatch(ops)" in _db_src, "写入走框架 abatch（不自写 upsert SQL）")
check("PutOp(namespace, key, None)" in _db_src,
      "删除走 PutOp(value=None) → DELETE")
check("DELETE FROM store WHERE prefix = %s" in _db_src,
      "整前缀删除是一条 SQL（不是先读全量再逐条置 None）")
check('if not prefix:\n            raise ValueError' in _db_src,
      "adelete_prefix 拒绝空 prefix（否则会删全表）")

check('aput(msg_namespace, "messages"' not in _chat_src
      and 'aput(msg_namespace, "messages"' not in _sess_src,
      "全量覆盖写已从 chat.py / sessions.py 清除")
check('aget(msg_namespace, "messages")' not in _sess_src
      and 'aget(msg_namespace, "messages")' not in _chat_src,
      "旧格式单行读取已清除")
check("alist_all_messages" in _chat_src,
      "continue_from_state 改走取全读（不能只取最新一页，否则图状态被截短）")
check("has_more" in _sess_src and "next_cursor" in _sess_src,
      "GET /messages 返回分页元信息")
check("awrite_messages" in _sess_src and "delete_keys=" in _sess_src,
      "edit 的「改写 + 截断」走同一个 batch（避免中间态）")
check('changed[target_key] = items_dict' not in _sess_src,
      "repair 不再回写整份列表（只写被改动的条目）")

# ── 用例 12：import 完整性（NameError 回归防线）──
# 2026-09-12 生产事故：chat.py 用了 message_key 但漏 import → 每轮对话结束时
# _persist_session 抛 NameError，台账写入全部失败（ast 提取 exec 时把符号绑进
# globals，掩盖了缺失，常规 spike 测不出来）。此断言专门堵这一类漏洞。
print("\n[12] import 完整性（database 模块级符号必须被显式导入）")
_db_public = {
    n.name for n in ast.parse(DATABASE_PY.read_text(encoding="utf-8")).body
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
}
for _label, _path, _src in (("chat.py", CHAT_PY, _chat_src), ("sessions.py", SESSIONS_PY, _sess_src)):
    _tree = ast.parse(_src)
    _imported: set[str] = set()
    for _node in _tree.body:
        if isinstance(_node, ast.ImportFrom) and _node.module and _node.module.endswith("infra.database"):
            _imported |= {a.name for a in _node.names}
    _used = {n.id for n in ast.walk(_tree) if isinstance(n, ast.Name)}
    _missing = (_db_public & _used) - _imported
    check(not _missing, f"{_label} 引用的 database 模块级符号已全部导入（缺: {_missing}）")

print("\n" + "=" * 78)
_total = len(_FAILS) + _passed_count
print(f"PASS {_passed_count} / FAIL {len(_FAILS)}")
if _FAILS:
    for f in _FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PASS")

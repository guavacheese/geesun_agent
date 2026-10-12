"""Spike / 回归闸门：`turn_duration_ms` 的轮次归属与跨轮落盘语义。

不依赖 pytest，直接：python tests/spikes/turn_duration_per_turn.py

## 为什么需要这个脚本

`chat.py` 的注释曾写「仅写到 history 最后一条 AI 消息」，容易被读成
「整个会话只有最后一条 AI 带耗时」。但那份 history 是**本轮重建的全量快照**，
而落盘走 `_plan_message_append` 的**增量追加**（已归档条目不重写）——
两个事实叠加后的真实语义，注释里没写。

已有 `chat_persist_duration.py` 是**单轮视角 + 逻辑复刻**（只断言「history 最后一条
AI 有、中间 AI 没有」），回答不了跨轮问题；`store_incremental_messages.py` 覆盖了
增量追加本身，但不含耗时打点。

取向与 `store_incremental_messages.py` 一致：**不做逻辑复刻**。
`_plan_message_append` / `_last_ai_index_for_turn` / `_mark_turn_last_ai` 用 ast 从
chat.py 真实源码提取后 exec（测真实代码），store 用内存 FakeStore 模拟，水位查询语义
对齐 `database.py:343 aget_message_watermark`（8 位定长序号，文本序 == 数值序）。

**零复刻**：`_mark_turn_last_ai` 抽出后，「取下标 → 写字段」也不再由本脚本复刻
（此前那 2 行是唯一残留的 inline 复刻）。

## 场景

  S1  正常两轮        → 每轮各自的 AI 是否保留**自己那一轮**的耗时（增量追加使其成立）
  S2  某轮写入被跳过   → 消息后来补写进 store 时，耗时字段能否跟上（**已知窄缝，本次未修**）
  S3  本轮无 AI 产出   → 打点会不会落到**上一轮**的消息上（2026-10-12 修）
  S3b 正常轮不受影响   → 修复不得改变正常轮次行为
  S4  判据函数级用例   → 轮次边界：多 AI 段 / tool 在 AI 之后 / 无 user 的退化
  S5  completion 标记  → blocked_no_output / model_output_truncated 的归属
                        （与耗时同源，2026-10-12 一并收敛到 _mark_turn_last_ai）
"""

from __future__ import annotations

import ast
import copy
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHAT_PY = ROOT / "src" / "api" / "endpoints" / "chat.py"

_FAILS: list[str] = []
_passed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global _passed
    if ok:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _FAILS.append(label)
        print(f"  FAIL  {label}" + (f"  <-- {extra}" if extra else ""))


# ─── 从真实源码提取函数（ast + exec，测真实代码而非复刻）───
def extract(
    path: pathlib.Path,
    funcs: set[str],
    consts: set[str] | None = None,
    extra_globals: dict | None = None,
) -> dict:
    consts = consts or set()
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    nodes = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in funcs)
        or (
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in consts for t in n.targets)
        )
    ]
    found = {n.name for n in nodes if isinstance(n, ast.FunctionDef)}
    assert not (funcs - found), f"{path.name} 中找不到这些函数: {funcs - found}"

    ns: dict = {
        "str": str, "int": int, "len": len, "list": list, "dict": dict,
        "tuple": tuple, "range": range, "type": type, "isinstance": isinstance,
        "Exception": Exception, "enumerate": enumerate, "ValueError": ValueError,
        "json": json, "Optional": __import__("typing").Optional,
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
    return ns


_chat = extract(
    CHAT_PY,
    funcs={"_plan_message_append", "_last_ai_index_for_turn", "_mark_turn_last_ai"},
)
plan_append = _chat["_plan_message_append"]
last_ai_index_for_turn = _chat["_last_ai_index_for_turn"]
mark_turn_last_ai = _chat["_mark_turn_last_ai"]

_db = extract(
    ROOT / "src" / "infra" / "database.py",
    funcs={"message_key"},
    consts={"_MESSAGE_KEY_WIDTH"},
)
message_key = _db["message_key"]


# ─── 内存 store：key(8 位序号) -> entry ───
class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def watermark(self) -> tuple[int, str | None]:
        """对齐 aget_message_watermark：最大序号 + 该条目 id（排除非数字 key）。"""
        keys = sorted(k for k in self.rows if k.isdigit())
        if not keys:
            return 0, None
        top = keys[-1]
        return int(top), self.rows[top].get("id")

    def write(self, seq: int, new_items: list[dict]) -> None:
        for off, entry in enumerate(new_items):
            self.rows[message_key(seq + off)] = copy.deepcopy(entry)

    def dump(self) -> list[tuple[str, str, str, object]]:
        out = []
        for k in sorted(self.rows):
            e = self.rows[k]
            out.append((k, str(e.get("role")), str(e.get("content"))[:14], e.get("turn_duration_ms")))
        return out


def save_turn(
    store: FakeStore,
    history: list[dict],
    t_start: int | None,
    t_end: int | None,
    *,
    skip_message_write: bool = False,
) -> tuple[str, list[dict]]:
    """复刻 `_persist_session` 的「打点 → 水位 → 增量追加」顺序（chat.py）。

    打点目标与「写哪条 entry」全部由**真实函数** `_mark_turn_last_ai` 决定
    （判据与落点都不是复刻的）。
    """
    history = copy.deepcopy(history)
    if t_start is not None and t_end is not None:
        mark_turn_last_ai(history, {"turn_duration_ms": max(0, t_end - t_start)})

    prev_count, last_id = store.watermark()
    next_seq, new_items, mode = plan_append(history, prev_count, last_id)
    if not skip_message_write:
        store.write(next_seq, new_items)
    return mode, new_items


def msg(mid: str, role: str, content: str) -> dict:
    return {"id": mid, "role": role, "content": content}


def show(title: str, store: FakeStore) -> None:
    print(f"  {title}")
    for k, role, content, dur in store.dump():
        print(f"    key={k}  role={role:<4} 内容={content:<16} turn_duration_ms={dur!r}")


def s1() -> None:
    """正常两轮：每轮各自的耗时是否都留在 store 里。"""
    store = FakeStore()
    h1 = [msg("u1", "user", "第一个问题"), msg("a1", "ai", "第一个回答")]
    save_turn(store, h1, 0, 4500)                                   # 轮1：0 → 4500ms
    h2 = h1 + [msg("u2", "user", "第二个问题"), msg("a2", "ai", "第二个回答")]
    mode2, _ = save_turn(store, h2, 10000, 13200)                   # 轮2：3200ms
    show("store 实际内容：", store)

    a1 = store.rows["00000002"]
    a2 = store.rows["00000004"]
    check("轮1 的 a1 保留的是**轮1 自己的** 4500ms（未被轮2 覆写）",
          a1.get("turn_duration_ms") == 4500, repr(a1.get("turn_duration_ms")))
    check("轮2 的 a2 带轮2 自己的 3200ms",
          a2.get("turn_duration_ms") == 3200, repr(a2.get("turn_duration_ms")))
    check("轮2 落盘方式 = by_id 增量追加（不重写已归档的 a1）", mode2 == "by_id", mode2)


def s2() -> None:
    """某轮消息写入被跳过，之后恢复：耗时字段**跟不上**（已知窄缝，本次未修）。"""
    store = FakeStore()
    h1 = [msg("u1", "user", "第一个问题"), msg("a1", "ai", "第一个回答")]
    save_turn(store, h1, 0, 4500)                                   # 轮1 正常

    # 轮2：消息写入被跳过（真实触发面：legacy_rows 旧格式残留期 / mode=none 退化 / 写异常）
    h2 = h1 + [msg("u2", "user", "第二个问题"), msg("a2", "ai", "第二个回答")]
    save_turn(store, h2, 10000, 13200, skip_message_write=True)

    # 轮3：恢复正常 → 本次会**一次性补写**轮2 堆积的 u2/a2 + 轮3 的 u3/a3
    h3 = h2 + [msg("u3", "user", "第三个问题"), msg("a3", "ai", "第三个回答")]
    _, new3 = save_turn(store, h3, 20000, 21200)
    show("store 实际内容（轮2 的消息被补写回来了）：", store)

    check("轮3 一次性补写了轮2 的堆积条目（u2/a2 确实进了 store）",
          any(e.get("id") == "a2" for e in new3), f"new_items={[e.get('id') for e in new3]}")
    a2 = store.rows["00000004"]
    check("[已知窄缝] 轮2 的 a2 补写后不带自身耗时（那份只存在于当时内存，已丢）",
          a2.get("turn_duration_ms") is None, repr(a2.get("turn_duration_ms")))
    a3 = store.rows["00000006"]
    check("轮3 的 a3 正常带耗时 1200ms",
          a3.get("turn_duration_ms") == 1200, repr(a3.get("turn_duration_ms")))


def s3() -> None:
    """本轮无 AI 产出：不得把本轮耗时写到上一轮的 AI 上（2026-10-12 修）。"""
    store = FakeStore()
    # 轮1 消息写入被跳过 → u1/a1 还没落盘
    h1 = [msg("u1", "user", "第一个问题"), msg("a1", "ai", "第一个回答")]
    save_turn(store, h1, 0, 4500, skip_message_write=True)

    # 轮2：本轮只有 user 消息、没有 AI 产出（模型未产出就断连/被取消，走 interrupted 保存）
    h2 = h1 + [msg("u2", "user", "第二个问题")]
    _, new2 = save_turn(store, h2, 10000, 11000)                    # 轮2 窗口 1000ms
    show("store 实际内容：", store)

    a1 = store.rows["00000002"]
    check("★ [已修] a1 不再被轮2 的 1000ms 污染（改前会被改成 1000，丢了自己的 4500）",
          a1.get("turn_duration_ms") is None, repr(a1.get("turn_duration_ms")))
    check("★ [已修] 本轮耗时（1000ms）没有被写到**任何**条目上",
          not any(
              isinstance(e.get("turn_duration_ms"), int) and e["turn_duration_ms"] == 1000
              for e in new2
          ), f"新增={[(e.get('id'), e.get('turn_duration_ms')) for e in new2]}")
    check("本轮新增区间里没有任何属于本轮的 AI（唯一 AI 是轮1 的 a1）",
          [e.get("id") for e in new2 if e.get("role") == "ai"] == ["a1"],
          f"新增={[e.get('id') for e in new2]}")


def s3b() -> None:
    """修复不得改变正常轮次的行为：轮1 正常落盘，轮2 无 AI 产出。"""
    store = FakeStore()
    h1 = [msg("u1", "user", "第一个问题"), msg("a1", "ai", "第一个回答")]
    save_turn(store, h1, 0, 4500)
    h2 = h1 + [msg("u2", "user", "第二个问题")]                     # 轮2 无 AI 产出
    save_turn(store, h2, 10000, 11000)
    show("store 实际内容：", store)

    a1 = store.rows["00000002"]
    check("轮1 的 a1 仍是自己的 4500ms（无 AI 产出的轮2 不会碰它）",
          a1.get("turn_duration_ms") == 4500, repr(a1.get("turn_duration_ms")))
    check("轮2 只追加了 user 消息，未新增任何 AI 条目",
          store.rows["00000003"].get("role") == "user" and "00000004" not in store.rows)


def s4() -> None:
    """判据函数级用例：轮次边界。"""
    check("多 AI 段：取本轮**最后**一条 AI（tool 消息在 AI 之后也不影响）",
          last_ai_index_for_turn([
              msg("u1", "user", "问"), msg("a1", "ai", "工具调用段"),
              msg("t1", "tool", "结果"), msg("a2", "ai", "最终答复"),
          ]) == 3)
    check("本轮无 AI → None（上一轮的 AI 不算本轮）",
          last_ai_index_for_turn([msg("u1", "user", "问1"), msg("a1", "ai", "答1"),
                                  msg("u2", "user", "问2")]) is None)
    check("末尾连续 user（重试 / 编辑重发）→ 以最后一条 user 为界，其前的 AI 不算",
          last_ai_index_for_turn([msg("a1", "ai", "答1"), msg("u1", "user", "问"),
                                  msg("u2", "user", "重发")]) is None)
    check("单轮且 AI 在 user 之后 → 命中（0 基下标 1）",
          last_ai_index_for_turn([msg("u1", "user", "问"), msg("a1", "ai", "答")]) == 1)
    check("[退化] 无 user 消息（压缩截断 / 系统续跑）→ 沿用旧口径：整份倒序第一条 AI",
          last_ai_index_for_turn([msg("a1", "ai", "答1"), msg("a2", "ai", "答2")]) == 1)
    check("空 history → None", last_ai_index_for_turn([]) is None)
    check("条目缺 role 字段 → None（不崩）",
          last_ai_index_for_turn([{"id": "x", "content": "?"}]) is None)

    # 反证：证明本闸门对「改回旧判据」有区分力（否则判据只是自说自话）
    def _legacy_last_ai(hh: list[dict]) -> int | None:
        """修复前的判据：整份 history 倒序第一条 AI。"""
        for i in range(len(hh) - 1, -1, -1):
            if hh[i].get("role") == "ai":
                return i
        return None

    _h = [msg("u1", "user", "问1"), msg("a1", "ai", "答1"), msg("u2", "user", "问2")]
    check("反证：旧判据命中上一轮的 a1（下标 1）而真判据返回 None —— 可区分",
          _legacy_last_ai(_h) == 1 and last_ai_index_for_turn(_h) is None,
          f"legacy={_legacy_last_ai(_h)} new={last_ai_index_for_turn(_h)}")


def s5() -> None:
    """completion 标记的归属：与耗时同源（本轮无 AI 产出时不得打到上一轮）。

    触发面事实（读置位分支得出，决定了修复不能"因噎废食"）：
      · blocked_no_output —— M3 完成门耗尽重试，本轮零**文件**产出，
        但模型通常有纯文本回复（GY35377/60c64c4f：连续四轮零工具调用纯复述）
        ⇒ 本轮**有** AI 消息，落点正常，行为不变；
      · model_output_truncated —— 三分支中 THINKING_TRUNCATED / THINKING_ONLY
        本轮必然有 AI 条目（带 reasoning 的截断/仅思考），只有 FULLY_EMPTY 可能没有。
    所以修复只影响「本轮连 AI 条目都没有」的极端情形——那时**没有载体可挂**，
    正确做法是不写（而不是挂到上一轮去）。
    """
    # ① 常见路径：本轮有纯文本 AI → 标记落在本轮，上一轮不受影响
    h = [
        msg("u1", "user", "问1"), msg("a1", "ai", "答1"),
        msg("u2", "user", "问2"), msg("a2", "ai", "模型纯复述了一段话，没产出文件"),
    ]
    idx = mark_turn_last_ai(h, {"completion": "blocked_no_output"})
    check("本轮有 AI：blocked_no_output 落在**本轮**最后一条 AI（下标 3）", idx == 3, repr(idx))
    check("本轮有 AI：a2 拿到标记",
          h[3].get("completion") == "blocked_no_output", repr(h[3].get("completion")))
    check("本轮有 AI：上一轮的 a1 **不受影响**", "completion" not in h[1], repr(h[1]))

    # ② 极端路径：本轮只有 user、没有任何 AI 条目 → 不写（改前会挂到 a1 上）
    h2 = [msg("u1", "user", "问1"), msg("a1", "ai", "答1"), msg("u2", "user", "问2")]
    idx2 = mark_turn_last_ai(h2, {"completion": "blocked_no_output"})
    check("★ 本轮无 AI：返回 None（无载体可挂，不落盘）", idx2 is None, repr(idx2))
    check("★ 本轮无 AI：**上一轮的 a1 不得被污染**（改前会打上 blocked_no_output）",
          "completion" not in h2[1], repr(h2[1]))

    # ③ model_output_truncated：两个字段一起落，且同样只落本轮
    h3 = [
        msg("u1", "user", "问1"), msg("a1", "ai", "答1"),
        msg("u2", "user", "问2"), msg("a2", "ai", ""),  # 思考吃满上限被截断，正文为空
    ]
    idx3 = mark_turn_last_ai(
        h3, {"completion": "model_output_truncated", "stop_reason": "thinking_truncated"}
    )
    check("truncated：落本轮最后一条 AI（下标 3）", idx3 == 3, repr(idx3))
    check("truncated：completion 与 stop_reason **同时**落在同一条上",
          h3[3].get("completion") == "model_output_truncated"
          and h3[3].get("stop_reason") == "thinking_truncated",
          f"completion={h3[3].get('completion')!r} stop_reason={h3[3].get('stop_reason')!r}")
    check("truncated：上一轮的 a1 不被污染", "stop_reason" not in h3[1], repr(h3[1]))

    # ④ 反证：旧判据（整份倒序第一条 AI）在极端路径下会命中上一轮 —— 闸门有区分力
    def _legacy_last_ai(hh: list[dict]) -> int | None:
        for i in range(len(hh) - 1, -1, -1):
            if hh[i].get("role") == "ai":
                return i
        return None

    check("反证：同一条 history，旧判据指向 a1（下标 1）而新判据返回 None",
          _legacy_last_ai(h2) == 1 and last_ai_index_for_turn(h2) is None,
          f"legacy={_legacy_last_ai(h2)} new={last_ai_index_for_turn(h2)}")


print("=" * 78)
print("S1 正常两轮：每轮各自的耗时是否都留在 store 里")
print("=" * 78)
s1()
print()
print("=" * 78)
print("S2 某轮写入被跳过，之后恢复：耗时字段能不能跟上消息（已知窄缝，本次未修）")
print("=" * 78)
s2()
print()
print("=" * 78)
print("S3 本轮无 AI 产出：耗时不得写到上一轮的 AI 上")
print("=" * 78)
s3()
print()
print("=" * 78)
print("S3b 修复不得改变正常轮次行为")
print("=" * 78)
s3b()
print()
print("=" * 78)
print("S4 判据函数级用例：轮次边界")
print("=" * 78)
s4()
print()
print("=" * 78)
print("S5 completion 标记的归属：与耗时同源（本轮无 AI 时不得打到上一轮）")
print("=" * 78)
s5()


def source_sync() -> None:
    """chat.py 改了打点判据/落盘逻辑，本脚本必须同步改——否则直接 FAIL。"""
    print()
    print("=" * 78)
    print("源码同步断言")
    print("=" * 78)
    src = CHAT_PY.read_text(encoding="utf-8")
    check("chat.py 打点目标走 _mark_turn_last_ai（不再是裸的「整份倒序第一条 AI」）", (
        "_mark_turn_last_ai(" in src
        and '{"turn_duration_ms": turn_duration_ms}' in src
    ))
    check("chat.py 本轮无 AI 产出时跳过打点并留 WARNING（可观测）",
          "本轮无 AI 产出，跳过 turn_duration_ms 打点" in src)
    check("chat.py completion 标记（两处）同样走 _mark_turn_last_ai（同源同判据）", (
        '{"completion": "blocked_no_output"}' in src
        and '"completion": "model_output_truncated",' in src
    ))
    check("chat.py completion 标记不再内联「整份倒序找 AI」循环",
          'history[i]["completion"]' not in src)
    check("chat.py 两处 completion 标记都留了「本轮无 AI 产出」WARNING",
          src.count("跳过 completion=") == 2, str(src.count("跳过 completion=")))
    check("chat.py 落盘仍走 _plan_message_append 增量追加", "_plan_message_append(" in src)
    check("_plan_message_append 仍按 store 序号接续（不按 history 下标）",
          "next_seq = prev_count + 1" in src)


source_sync()

print()
print("=" * 78)
print(f"PASS {_passed} / FAIL {len(_FAILS)}")
if _FAILS:
    for f in _FAILS:
        print(f"  FAILED: {f}")
    raise SystemExit(1)
print("ALL PASS")

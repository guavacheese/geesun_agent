# -*- coding: utf-8 -*-
"""Spike: .trash 两阶段删除 + TTL 清扫（2026-09-14 回归防线）。

不依赖 pytest，直接：python tests/spikes/trash_ttl_sweep.py

覆盖三类缺陷路径：
  1. rename-to-trash 的可逆性：移入 / 还原 / 内容无损
  2. **mtime 陷阱**（本条最重要）：os.rename 保留原目录 mtime，若用 mtime 判
     TTL，一个"内容很旧、刚移进 trash 等待事务提交"的目录会被立刻清掉——此时
     事务失败要还原就没有可还原的东西了（数据丢失从后门回来）。本用例造一个
     mtime=3 天前的目录，移进 trash 后 sweep(24h) **必须保留**。
  3. 清扫安全性：不可解析名跳过不删、符号链接不跟随、.trash 本身不删、
     未超期条目保留
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.infra import trash  # noqa: E402

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


def _plant(root: str, user: str, sid: str, files: dict[str, bytes]) -> str:
    d = os.path.join(root, user, sid)
    for rel, data in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    return d


tmp = tempfile.mkdtemp(prefix="trash_spike_")
try:
    U, S = "U1", "sid-aaaa-1111"

    # ─── 1. 移入 / 还原 / 内容无损 ───
    print("\n[1] rename-to-trash 基本可逆性")
    d = _plant(tmp, U, S, {"a.txt": b"A", "sub/b.xlsx": b"B"})
    moved = trash.move_to_trash(tmp, U, S)
    check(moved is not None, "move_to_trash 返回 (原目录, trash 目录)")
    orig, tp = moved
    check(orig == d and not os.path.exists(orig), "原目录已不存在（已移入 trash）")
    check(os.path.isfile(os.path.join(tp, "sub", "b.xlsx")), "子目录内容随目录整体移动")
    name = os.path.basename(tp)
    check(name.startswith(S + "_") and name.rsplit("_", 1)[1].isdigit(),
          f"trash 条目名带毫秒时间戳: {name}")
    check(os.path.dirname(tp) == trash.trash_root(tmp), "trash 目录位于 <root>/.trash")
    check(trash.move_to_trash(tmp, U, S) is None, "目录不存在时返回 None（幂等）")

    failed = trash.move_back([moved])
    check(failed == 0 and os.path.isfile(os.path.join(orig, "a.txt"))
          and os.path.isfile(os.path.join(orig, "sub", "b.xlsx")),
          "move_back 完整还原（含嵌套文件）")

    # ─── 2. mtime 陷阱（核心回归用例）───
    print("\n[2] mtime 陷阱：旧 mtime 的新 trash 条目不得被清")
    old = _plant(tmp, U, S, {"old.txt": b"old"})
    three_days_ago = time.time() - 3 * 86_400
    os.utime(old, (three_days_ago, three_days_ago))  # 模拟"很久以前创建的会话目录"
    moved_old = trash.move_to_trash(tmp, U, S)
    check(moved_old is not None, "旧 mtime 目录成功移入 trash")
    _orig, tp_old = moved_old
    check(abs(os.path.getmtime(tp_old) - three_days_ago) < 5,
          "确认 os.rename 保留了原目录 mtime（这正是不能用 mtime 判 TTL 的原因）")
    stats = trash.sweep_trash(tmp, 86_400)
    check(os.path.isdir(tp_old),
          "sweep(24h) 未删该条目——TTL 用目录名时间戳而非 mtime（否则这里会 FAIL）")
    check(stats["removed"] == 0, f"sweep 未删任何条目: {stats}")

    # ─── 3. TTL 边界：超期删、未超期留 ───
    print("\n[3] TTL 边界")
    base = trash.trash_root(tmp)
    os.makedirs(base, exist_ok=True)
    now_ms = int(time.time() * 1000)
    expired = os.path.join(base, f"{S}_expired_{now_ms - 25 * 3_600_000}")
    # 名字含下划线也允许（sid 部分是 .+）：用规范名重造
    expired = os.path.join(base, f"s-{'z' * 4}_{now_ms - 25 * 3_600_000}")
    fresh = os.path.join(base, f"s-{'y' * 4}_{now_ms - 60_000}")
    for p in (expired, fresh):
        os.makedirs(p)
        with open(os.path.join(p, "x.txt"), "wb") as f:
            f.write(b"x")
    unparsable = os.path.join(base, "no-timestamp-here")
    os.makedirs(unparsable, exist_ok=True)
    # 符号链接：指向 trash 外部目录，必须只删链接本身
    outside = os.path.join(tmp, "outside-dir")
    os.makedirs(outside, exist_ok=True)
    link = os.path.join(base, f"s-{'l' * 4}_{now_ms}")
    os.symlink(outside, link)

    stats = trash.sweep_trash(tmp, 86_400)
    check(not os.path.exists(expired), "超期（25h）条目被清扫")
    check(os.path.isdir(fresh), "未超期（1min）条目保留")
    check(os.path.isdir(unparsable), "名字不含时间戳的条目保留（不猜不删）")
    check(stats["skipped"] >= 1, f"不可解析条目计入 skipped: {stats}")
    check(not os.path.lexists(link) and os.path.isdir(outside),
          "符号链接被删但目标目录未受影响（不跟随）")
    check(os.path.isdir(base), ".trash 目录本身不被删除")

    # ─── 4. remove_trash / 边界 ───
    print("\n[4] remove_trash 与边界输入")
    d2 = _plant(tmp, U, "sid-bbbb-2222", {"r.txt": b"R"})
    _o2, tp2 = trash.move_to_trash(tmp, U, "sid-bbbb-2222")
    check(trash.remove_trash(tp2) and not os.path.exists(tp2), "remove_trash 删除条目")
    check(trash.remove_trash(tp2), "remove_trash 对已不存在条目返回 True（幂等）")
    check(trash.sweep_trash(os.path.join(tmp, "nonexistent"), 10) ==
          {"scanned": 0, "removed": 0, "skipped": 0}, "根目录不存在时返回零统计")
    check(trash.move_back([("/no/such/dir", "/no/such/trash")]) == 1,
          "还原失败返回失败数（不静默吞）")

    # ─── 5. 位置断言：sessions.py 的顺序约束 ───
    print("\n[5] delete_session 顺序约束（ast 行号）")
    import ast
    sess_src = (ROOT / "src" / "api" / "endpoints" / "sessions.py").read_text(encoding="utf-8")
    tree = ast.parse(sess_src)
    line_409 = line_move = line_atomic = line_back = line_remove = None
    for n in ast.walk(tree):
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) \
                and getattr(n.exc.func, "id", "") == "HTTPException":
            kw = {k.arg: k.value for k in n.exc.keywords}
            if isinstance(kw.get("status_code"), ast.Constant) and kw["status_code"].value == 409:
                line_409 = n.lineno
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                full = f"{getattr(f.value, 'id', '')}.{f.attr}"
                if full == "trash.move_to_trash":
                    line_move = line_move or n.lineno
                if full == "trash.move_back":
                    line_back = n.lineno
                if full == "trash.remove_trash":
                    line_remove = n.lineno
                if full == "store.adelete_session_atomic":
                    line_atomic = n.lineno
    check(line_409 and line_move and line_atomic and line_remove,
          f"四类调用齐备（409=L{line_409}, move=L{line_move}, "
          f"atomic=L{line_atomic}, remove=L{line_remove}）")
    check(line_409 < line_move < line_atomic,
          "顺序：409 检查 → 文件预备(rename) → DB 事务（拒绝时零副作用）")
    check(line_remove > line_atomic,
          "trash 物理删除只在 DB 事务之后（提交前绝不物理删文件）")
    check(line_back is not None and line_back > line_move,
          "还原调用存在且位于预备阶段之后（失败路径可逆）")
    check(sess_src.count("trash.move_back(") == 2,
          "两条失败路径都还原：预备失败 / DB 事务失败")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 78)
print(f"PASS {_passed} / FAIL {len(_FAILS)}")
if _FAILS:
    for f in _FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PASS")

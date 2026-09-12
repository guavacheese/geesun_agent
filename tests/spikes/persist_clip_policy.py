"""Spike: 验证 store 回放副本的长度上限策略（2026-09-12 修 20808→2000 截断 bug）。

不依赖 pytest，直接：python tests/spikes/persist_clip_policy.py

与既有 spike 的差别：**不做逻辑复刻**。本脚本用 ast 从 chat.py 真实源码中
提取 `_clip` 函数定义并 exec 执行，再对「role 分流表达式」做结构断言——
测的是真实代码。chat.py 一旦被改回硬截断（或分流写反），本脚本立刻失败。

背景：生产会话 GY24428:0f6d781d 的 AI 正文 20808 字符被 content[:2000] 砍掉
90.4%，用户刷新后只剩半张相机参数表（store 存 2000 / checkpoint 存 20808，
程序化验证为严格前缀）。截断写在持久化层＝不可逆删除，方向错了层。

覆盖：
  1. AI 正文 / reasoning 超长时完整保留（persist_max_content_chars=0 不限制）
  2. tool 消息正文、AI 消息挂的 tool_calls[].result 走上限
  3. 配置设回 2000 时行为与旧版一致（回归保护）
  4. 源码级断言：非注释代码中不再存在 [:2000] / [:500] 硬编码
"""

from __future__ import annotations

import ast
import pathlib
import sys

CHAT_PY = pathlib.Path(__file__).resolve().parents[2] / "src" / "api" / "endpoints" / "chat.py"
CONFIG_PY = pathlib.Path(__file__).resolve().parents[2] / "src" / "core" / "config.py"

# ── 从真实源码提取 _clip 并执行（不 import 模块，避开 fastapi/agent 依赖）──
_src = CHAT_PY.read_text(encoding="utf-8")
_tree = ast.parse(_src)

_clip_node = next(
    (n for n in _tree.body if isinstance(n, ast.FunctionDef) and n.name == "_clip"),
    None,
)
assert _clip_node is not None, "chat.py 中找不到模块级 _clip 函数定义"
_ns: dict = {}
exec(
    compile(ast.fix_missing_locations(ast.Module(body=[_clip_node], type_ignores=[])),
            "<chat.py::_clip>", "exec"),
    _ns,
)
clip = _ns["_clip"]

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  → {detail}" if detail else ""))


print("=" * 72)
print("1. _clip 行为（真实源码提取执行）")
print("=" * 72)

check("limit=0 不限制", clip("x" * 100000, 0) == "x" * 100000)
check("limit<0 视为不限制", clip("x" * 10000, -1) == "x" * 10000)
check("超长按 limit 裁剪", len(clip("x" * 10000, 8000)) == 8000)
check("等于 limit 不裁剪", clip("x" * 8000, 8000) == "x" * 8000)
check("limit-1 不裁剪", len(clip("x" * 7999, 8000)) == 7999)
check("空串安全", clip("", 8000) == "")
check("None-limit 安全", clip("abc", None) == "abc")

print()
print("=" * 72)
print("2. 生产真实量级用例（长度取自全库 checkpoint 实测）")
print("=" * 72)

# 实测最大 AI 正文 272833（wanglei1:faa7a7c2）；本 bug 会话 20808（GY24428:0f6d781d）
_ai_272k = "A" * 272833
_ai_20k = "B" * 20808
_tool_39k = "T" * 39758          # 实测最大工具结果
_reason_50k = "R" * 50000

# AI 正文：persist_max_content_chars=0（本次拍板取值）
check("AI 正文 272833 字符完整保留", clip(_ai_272k, 0) == _ai_272k, f"len={len(clip(_ai_272k, 0))}")
check("AI 正文 20808 字符完整保留（本 bug 会话）", clip(_ai_20k, 0) == _ai_20k)
check("reasoning 50000 字符完整保留", clip(_reason_50k, 0) == _reason_50k)
check("tool 结果 39758 → 8000", len(clip(_tool_39k, 8000)) == 8000, f"len={len(clip(_tool_39k, 8000))}")
check("tool 结果截断后是原文前缀", clip(_tool_39k, 8000) == _tool_39k[:8000])
check("error 500 上限维持", len(clip("E" * 3000, 500)) == 500)

print()
print("=" * 72)
print("3. 回归保护：配置设回 2000 时行为与旧版一致")
print("=" * 72)

check("content 限 2000 → 复现旧行为", clip(_ai_20k, 2000) == _ai_20k[:2000])
check("旧行为确为 2000 长度", len(clip(_ai_20k, 2000)) == 2000,
      "说明「设配置即可回退」，不需要改代码")

print()
print("=" * 72)
print("4. 源码结构断言（防改回硬截断 / 分流写反）")
print("=" * 72)

# 4a. 非注释代码中不得再有整数硬截断 2000 / 500
_int_slices: list[tuple[int, int]] = []
for node in ast.walk(_tree):
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        v = node.slice.value
        if isinstance(v, int) and v in (500, 2000):
            _int_slices.append((v, node.lineno))
check("chat.py 无 [:2000]/[:500] 硬截断", not _int_slices, f"残留={_int_slices}")

# 4b. _clip 调用点数量（预期 5：content / reasoning / tool_calls.result / SSE error / SSE result）
_clip_calls = [
    n for n in ast.walk(_tree)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_clip"
]
check("_clip 调用点 5 处", len(_clip_calls) == 5, f"实际={len(_clip_calls)}")

# 4c. content 赋值必须按 role 分流，且 tool 分支在前
_content_assign = None
for node in ast.walk(_tree):
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        tgt = node.targets[0]
        if (isinstance(tgt, ast.Name) and tgt.id == "content"
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "_clip"):
            _content_assign = node
            break

if _content_assign is None:
    check("content 走 _clip 且按 role 分流", False, "未找到 content = _clip(...) 赋值")
else:
    arg = _content_assign.value.args[1]
    is_ifexp = isinstance(arg, ast.IfExp)
    body_attr = getattr(arg.body, "attr", None) if is_ifexp else None
    else_attr = getattr(arg.orelse, "attr", None) if is_ifexp else None
    test_src = ast.unparse(arg.test) if is_ifexp else ""
    check(
        "content 按 role 分流（tool → tool_result 上限）",
        is_ifexp and body_attr == "persist_max_tool_result_chars"
        and else_attr == "persist_max_content_chars" and "role" in test_src,
        f"test={test_src!r} body={body_attr} else={else_attr}",
    )

# 4d. reasoning 必须走 content 上限（不受工具上限影响）
_reason_assign = None
for node in ast.walk(_tree):
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        tgt = node.targets[0]
        if (isinstance(tgt, ast.Name) and tgt.id == "reasoning"
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "_clip"):
            _reason_assign = node
            break
check(
    "reasoning 走 persist_max_content_chars",
    _reason_assign is not None
    and getattr(_reason_assign.value.args[1], "attr", None) == "persist_max_content_chars",
    "" if _reason_assign is not None else "未找到 reasoning = _clip(...)",
)

# 4e. config.py 三项配置存在且默认值符合拍板结论
_cfg_src = CONFIG_PY.read_text(encoding="utf-8")
_cfg_defaults = {}
for node in ast.walk(ast.parse(_cfg_src)):
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if node.value is not None and isinstance(node.value, ast.Constant):
            _cfg_defaults[node.target.id] = node.value.value

check("persist_max_content_chars 默认 0（不限制）",
      _cfg_defaults.get("persist_max_content_chars") == 0,
      f"实际={_cfg_defaults.get('persist_max_content_chars')}")
check("persist_max_tool_result_chars 默认 8000",
      _cfg_defaults.get("persist_max_tool_result_chars") == 8000,
      f"实际={_cfg_defaults.get('persist_max_tool_result_chars')}")
check("persist_max_error_chars 默认 500",
      _cfg_defaults.get("persist_max_error_chars") == 500,
      f"实际={_cfg_defaults.get('persist_max_error_chars')}")

# 4f. 交叉校验：chat.py 引用的 settings.persist_* 必须在 config.py 有定义
#     （防拼写错误——Settings 是 pydantic 模型，未定义属性要到运行期才 AttributeError）
_used_attrs: set[str] = set()
for node in ast.walk(_tree):
    if (isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "settings"
            and node.attr.startswith("persist_")):
        _used_attrs.add(node.attr)
_missing = sorted(_used_attrs - set(_cfg_defaults))
check(
    "chat.py 引用的 persist_* 全部在 config.py 有定义",
    bool(_used_attrs) and not _missing,
    f"引用={sorted(_used_attrs)} 缺失={_missing}",
)

print()
print("=" * 72)
print(f"结果: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("失败项:")
    for f in FAIL:
        print(f"  - {f}")
print("=" * 72)
sys.exit(1 if FAIL else 0)

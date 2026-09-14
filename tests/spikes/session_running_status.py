"""Spike: 会话列表「运行状态指示器」（进行中 / 刚跑完）—— 2026-09-14 回归防线。

不依赖 pytest，直接：python tests/spikes/session_running_status.py

取向（对齐 AGENTS.md 2026-09-12 教训）：断言测**真实源码的结构与位置关系**，
不做 ast exec 复刻——exec 注入 globals 会掩盖接线错误（26faf9d 的 message_key
漏 import 就是这么漏掉的，63 断言全过却线上每轮 NameError）。

背景：
- 用户切走再回来无从判断会话是否还在跑，唯一反馈是删除时撞 409——护栏反倒
  暴露了这个信息缺口（本该提前知道"它在跑"，变成操作时才被拒绝）。
- deer-flow 会话列表**完全没做**：recent-chat-list.tsx:306-371 只渲染频道图标/
  置顶/标题，后端 thread_meta.status 与列表接口回显的 status 字段前端从不读取；
  isStreaming 仅作用于已打开会话内部的消息。
- deepseek-harness 做了（packages/client/ui-primitives/src/StateDot.tsx）：
  ongoing 追逐动画 / done 绿点+光晕 / warning 琥珀 / error 红；关键设计是
  **状态不持久化**（running 来自 host 实时帧，completed 是前端内存集合
  completedNotifications）→ 刷新后不残留陈旧动画。
- 我们复用刚上线的 turn_registry（进程内租约表，src/core/turn_registry.py）
  作为 running 真相源：零新增存储、零查询开销、天然按用户隔离。

目标语义：
  ① 后端 GET /sessions 每行加 running（读 turn_registry.is_active，纯内存标注，
     不查库）；全量分支与分页分支（page + pinned_sessions 都要标）。
  ② 前端「刚跑完」绿点是**边沿语义**：上一帧 running、这一帧不 running，且不是
     当前打开的会话 ⇒ 标记；再次运行或点开 ⇒ 清除。空闲会话不给任何标记
     （否则所有历史会话常驻绿点，满屏标记＝没有信息量）。
     首次加载 prevRunning 为空 ⇒ 不会把历史会话误标为「刚跑完」。
  ③ 轮询只在列表存在 running 项时开启（无则零请求）；标签页重新可见时立即
     对齐一次（后台标签定时器被浏览器节流到分钟级）。
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
SESSIONS_PY = ROOT / "src" / "api" / "endpoints" / "sessions.py"
WEB_ROOT = ROOT.parent / "geesun_agent_web"
PAGE_TSX = WEB_ROOT / "app" / "chat" / "page.tsx"
TYPES_TS = WEB_ROOT / "lib" / "types.ts"

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


# ─── 1. 后端：GET /sessions 标注 running ───
print("\n[1] running 标注（src/api/endpoints/sessions.py）")
sess_src = SESSIONS_PY.read_text(encoding="utf-8")

check("def _mark_running(rows: list[dict], user_id: str) -> None:" in sess_src,
      "_mark_running 标注函数存在")

# 以 def 行为锚切出函数体（函数名在文档字符串与调用处也出现，不能按名字裸切）
_fn_seg = sess_src[sess_src.index("def _mark_running(rows"):]
_fn_seg = _fn_seg.split("async def _alist_sessions")[0]
check("turn_registry.is_active(" in _fn_seg,
      "数据源是 turn_registry（进程内租约表，不是 DB 列）")
check('f"{user_id}:{sid}"' in _fn_seg,
      "按 user:session 查租约 —— 天然按用户隔离，看不到别人的会话状态")
check('row["running"] = bool(sid) and' in _fn_seg,
      "running 落地为 bool（前端可直接真值判断，不需要 !== undefined）")
check("await " not in _fn_seg and "store" not in _fn_seg,
      "纯内存标注：函数体内无 await / 不碰 store（零查询开销）")

# 位置断言（ast）：三处标记调用都要落在 return 之前
_list_fn = None
for _n in ast.walk(ast.parse(sess_src)):
    if isinstance(_n, (ast.AsyncFunctionDef, ast.FunctionDef)) and _n.name == "list_sessions":
        _list_fn = _n
check(_list_fn is not None, "list_sessions 可解析")

if _list_fn is not None:
    _calls = [
        _n.lineno
        for _n in ast.walk(_list_fn)
        if isinstance(_n, ast.Call) and getattr(_n.func, "id", "") == "_mark_running"
    ]
    _returns = [_n.lineno for _n in ast.walk(_list_fn) if isinstance(_n, ast.Return)]
    check(len(_calls) == 3,
          f"三处标记调用（全量 + page + pinned_sessions）: 行号 {_calls}")
    check(bool(_returns) and max(_calls) < max(_returns),
          f"标记（最晚 L{max(_calls)}）先于 return（L{max(_returns)}）——"
          "不留未标注的行")

# ─── 2. 前端类型层：running 透传 ───
print("\n[2] 类型与适配器透传（geesun_agent_web/lib/types.ts）")
if not TYPES_TS.exists():
    check(False, f"前端仓库不可达：{TYPES_TS}")
else:
    types_src = TYPES_TS.read_text(encoding="utf-8")
    check(types_src.count("running?: boolean;") == 2,
          "Session 与 SessionRaw 都声明 running（列表/单条两条路径）")
    check(types_src.count("running: s.running,") == 1,
          "adaptSessionsResponse 透传 running")
    check(types_src.count("running: raw.running,") == 1,
          "adaptSession 透传 running")

# ─── 3. 前端边沿检测（此方案的核心，最易被改错）───
print("\n[3] 「刚跑完」边沿语义（geesun_agent_web/app/chat/page.tsx）")
if not PAGE_TSX.exists():
    check(False, f"前端仓库不可达：{PAGE_TSX}")
else:
    page_src = PAGE_TSX.read_text(encoding="utf-8")

    check("const applyServerList = useCallback(" in page_src,
          "统一入口 applyServerList（所有 server 列表都经它落地，边沿只在此处算）")
    check("const justFinished = [...prevRunning].filter(" in page_src,
          "justFinished 从**上一帧快照**算（不是从当前列表推）")
    check("!nowRunning.has(id) && id !== activeIdRef.current" in page_src,
          "边沿判定三条件齐备：上一帧在跑 + 这一帧不跑 + 不是当前打开的会话")
    check("const activeIdRef = useRef<string | null>(activeId);" in page_src,
          "activeIdRef 镜像（轮询回调里不会读到陈旧 activeId）")
    check("const prevRunningRef = useRef<Set<string>>(new Set());" in page_src,
          "prevRunningRef 初值为空 Set —— 首次加载 justFinished 必为空，"
          "历史会话不会被误标绿点")
    check("for (const id of nowRunning) {" in page_src and "next.delete(id)" in page_src,
          "该会话再次开始运行 ⇒ 清除完成提醒")

    # 覆盖完整性：不能有绕过 applyServerList 的裸 mergeSessions 应用点
    _merge_calls = page_src.count("mergeSessions(list, prev)")
    check(_merge_calls == 1,
          f"mergeSessions(list, prev) 只在 applyServerList 内调用（实测 {_merge_calls} 处）"
          "——绕过它就会漏掉边沿检测")
    _apply_calls = page_src.count("applyServerList(list)")
    check(_apply_calls == 4,
          f"四个应用点全走 applyServerList（初始加载 + 轮询 + 可见性 + 流式结束，"
          f"实测 {_apply_calls} 处）")

    # 轮询与可见性
    check("if (!hasRunning) return;" in page_src,
          "列表无「进行中」会话时不开轮询（零请求，不空转）")
    check("}, 10000);" in page_src, "轮询间隔 10s")
    check('document.addEventListener("visibilitychange", onVisible)' in page_src,
          "标签页重新可见时立即对齐一次（后台定时器会被节流到分钟级）")

    # 点开即清
    check("setCompletedIds((old) => {" in page_src
          and "点开即视为已读" in page_src,
          "selectSession 点开即清该会话的完成提醒")

    # UI 三态
    check("{session.running ? (" in page_src and "animate-spin" in page_src,
          "进行中＝旋转指示器（CSS 动画，非 JS 定时器）")
    check(") : justFinished ? (" in page_src,
          "刚跑完＝绿点；两者皆无 ⇒ 不渲染任何标记（空闲会话保持干净）")
    check(page_src.count("justFinished={completedIds.has(s.id)}") == 2,
          "两处 SessionRow 调用（置顶区 + 普通区）都传 justFinished")

# ─── 汇总 ───
print(f"\n{'=' * 60}")
print(f"PASS {_passed} / FAIL {len(_FAILS)}")
for _f in _FAILS:
    print(f"  - {_f}")
raise SystemExit(1 if _FAILS else 0)

"""配置漂移守门人：路由表 ↔ 写白名单 必须**归类闭合**。

为什么单独一个文件（不复用 tests/services/test_validated_backend.py）
--------------------------------------------------------------------
本文件是**纯 AST 契约**：不 import ``src``、不 import langchain / deepagents，
毫秒级、零依赖，可以放进任何检查流水线；而 test_validated_backend.py 验证的是
**运行时行为**（真后端的写入 / 拒绝 / 截断）。两者关注点不同，混在一起会让
"配置一致性"这条最廉价的检查被迫依赖最重的环境。

事故背景（2026-10-10，会话 4863afff）
------------------------------------
``/large_tool_results/`` 在 ``build_backend`` 的 routes 表里配了 ``StateBackend()``，
却**不在** ``ValidatedCompositeBackend.ALLOWED_WRITE_PREFIXES`` 里。于是
deepagents 自带的 offload 在 14:32:03 被本类拒写
（``[VALIDATED_CB] 拒绝写入: path=/large_tool_results/chatcmpl-tool-...``），
45.8MB 工具结果原地留在 LangGraph state，下一轮模型调用必然撑爆窗口。

这份配置错误**人眼看不出来**：两条互相约束的信息落在相距约 500 行的两个字面量里，
各自看都"没问题"。=> 必须由机器守。

不变式（注意：不是"routes ⊆ 白名单"，那条断言会误报）
------------------------------------------------------
路由表里有**刻意只读**的条目：``/uploads/``（用户上传的输入素材）、
``/workspace/agent-memory/``（平台共享 AGENTS.md）、``/skills/__system__`` 与
``/skills/__user_*__``（技能目录，写入口是上传 API）。它们的 backend 是
``FilesystemBackend`` —— 有路由、但绝不可写，且这是**设计意图**。

所以真正的不变式是**归类完备性**：

    每条 route 必须落在「可写 / 只读 / 禁写」三类之一，**不存在第四类**。

第四类（有路由、既不放行写入、也未被声明为只读）就是 4863afff 的成因。本文件
把它变成一条会红的断言。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SRC = PROJECT_ROOT / "src"
AGENT_PY = SRC / "services" / "agent.py"
BUDGET_PY = SRC / "core" / "tool_output_budget.py"

_BACKEND_CLASS = "ValidatedCompositeBackend"
_BACKEND_BUILDER = "build_backend"

# ─── 冻结期望值（漂移必须由人重新做一次决定，而不是被断言默默放过）───────────────
#
# 逐条冻结「路由模板 → 归类」，而不是只冻结"匹配到哪些前缀"：
# 后者有个覆盖缺口 —— 把新路由挂到已存在的只读前缀之下（如 `/uploads/{}/sub/`）时
# 匹配前缀集合不变，检测不出来。逐条冻结则任何新增 / 删除 / 改类都会红。
#
# 路由键里的 `{...}` 是 f-string 插值段的折叠形式（见 _key_template）。
_EXPECTED_ROUTE_CLASSIFICATION: dict[str, str] = {
    # ── 可写 ──
    "/reports/{...}/{...}/": "writable",  # 会话交付目录（前端据此渲染文件卡片）
    "/workspace/memories/": "writable",  # 跨会话记忆
    "/skills/__agent__/": "writable",  # agent 自创技能（SKILL.md 需 YAML 校验）
    "/conversation_history/": "writable",  # SummarizationMiddleware 归档
    "/large_tool_results/": "writable",  # 大工具结果转存（2026-10-10 修复后加入白名单）
    # ── 刻意只读（有路由、绝不可写，且这是设计意图而非遗漏）──
    "/uploads/{...}/{...}/": "readonly",  # 用户上传的输入素材
    "/workspace/agent-memory/": "readonly",  # 平台共享的 AGENTS.md，不由用户修改
    "/skills/__system__/": "readonly",  # 系统预装技能
    "/skills/__user_{...}__/": "readonly",  # 用户共享技能：写入口是 /api/v1/skill/upload
}

# 白名单里**故意不设 route**的前缀：写入落到 default backend（沙箱内本机路径），
# 由 langchain-cubesandbox 的 e2b 上传通道直写沙箱。它们有"承接者"，只是不是 route。
_SANDBOX_DEFAULT_WRITE_PREFIXES: frozenset[str] = frozenset({"/home/", "/tmp/"})

# 允许存在 routes 表、但**不在导入图里**的历史遗留文件（死文件，见 test_route_table_scope_is_closed）
_DEAD_ROUTE_TABLE_FILES: frozenset[str] = frozenset({"src/init_model.py"})


# ─── AST 提取 ────────────────────────────────────────────────────────────────


def _ast_of(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _func_def(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _class_str_set_node(tree: ast.AST, class_name: str, attr_name: str) -> ast.expr:
    """定位类属性里的字符串集合字面量节点（可直接原地突变，供负向对照使用）。"""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for stmt in node.body:
            targets: list[ast.expr] = []
            if isinstance(stmt, ast.Assign):
                targets = list(stmt.targets)
            elif isinstance(stmt, ast.AnnAssign):
                targets = [stmt.target]
            if not any(isinstance(t, ast.Name) and t.id == attr_name for t in targets):
                continue
            value = stmt.value
            assert isinstance(value, (ast.Set, ast.Tuple, ast.List)), (
                f"{class_name}.{attr_name} 不是字面量集合，守门人无法解析；"
                "请保持字面量写法，或同步更新本测试的解析方式"
            )
            return value
    raise AssertionError(f"未找到 {class_name}.{attr_name}")


def _class_str_set(tree: ast.AST, class_name: str, attr_name: str) -> frozenset[str]:
    node = _class_str_set_node(tree, class_name, attr_name)
    return frozenset(
        elt.value for elt in node.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    )


def _routes_dict_node(tree: ast.AST, func_name: str) -> ast.Dict:
    fn = _func_def(tree, func_name)
    assert fn is not None, f"未找到函数 {func_name}"
    found: list[ast.expr] = []
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "routes" for t in stmt.targets
        ):
            found.append(stmt.value)
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == "routes"
        ):
            found.append(stmt.value)
    assert len(found) == 1, f"{func_name} 中 routes 定义数量 = {len(found)}，期望恰好 1 处"
    assert isinstance(found[0], ast.Dict), "routes 必须是字典字面量（守门人据此提取路由键）"
    return found[0]


def _key_template(node: ast.expr) -> str:
    """把路由键还原成带占位符的模板。

    - 普通字符串原样返回；
    - f-string（如 ``f"/reports/{user_id}/{session_id}/"``）把插值段折叠成 ``{...}``，
      使前缀比较不受变量名影响。
    无法解析的键形态直接报错 —— 宁可测试红，也不要静默少检查一条路由。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                parts.append("{...}")
            else:
                raise AssertionError(f"无法解析路由键片段: {ast.dump(part)}")
        return "".join(parts)
    raise AssertionError(f"无法解析路由键: {ast.dump(node)}")


def _backend_name(value_node: ast.expr) -> str:
    if isinstance(value_node, ast.Call):
        func = value_node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return type(value_node).__name__


def _routes_from_node(node: ast.Dict) -> dict[str, ast.expr]:
    return {_key_template(k): v for k, v in zip(node.keys, node.values)}


def _routes(tree: ast.AST) -> dict[str, ast.expr]:
    return _routes_from_node(_routes_dict_node(tree, _BACKEND_BUILDER))


def _offload_prefix() -> str:
    """deepagents offload 的落点前缀，从中间件模块取值（单一事实来源）。"""
    for node in ast.walk(_ast_of(BUDGET_PY)):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "LARGE_TOOL_RESULTS_PREFIX"
                for t in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    raise AssertionError("未在 src/core/tool_output_budget.py 找到 LARGE_TOOL_RESULTS_PREFIX")


def _inputs_from_tree(tree: ast.AST) -> dict:
    return {
        "routes": _routes(tree),
        "writable": _class_str_set(tree, _BACKEND_CLASS, "ALLOWED_WRITE_PREFIXES"),
        "readonly": _class_str_set(tree, _BACKEND_CLASS, "VIRTUAL_READONLY_PREFIXES"),
        "blocked": _class_str_set(tree, _BACKEND_CLASS, "SANDBOX_PATH_PREFIXES"),
        "offload_prefix": _offload_prefix(),
    }


def _real_inputs() -> dict:
    return _inputs_from_tree(_ast_of(AGENT_PY))


# ─── 判据本体（真实断言与负向对照共用同一份，确保对照真的在测这条判据）────────────


def _longest_match(route: str, prefixes) -> str | None:
    hits = [p for p in prefixes if route.startswith(p)]
    return max(hits, key=len) if hits else None


def _classify(route: str, writable, readonly, blocked) -> tuple[str, str | None]:
    """镜像 ``ValidatedCompositeBackend.write()`` 的判定顺序：**先判可写**。

    ``/skills/__agent__/`` 既是白名单项、又落在只读前缀 ``/skills/`` 之下 —— 具体
    规则优先，运行时也是先看白名单，所以这里必须同序，否则会误报。
    """
    matched = _longest_match(route, writable)
    if matched:
        return "writable", matched
    matched = _longest_match(route, blocked)
    if matched:
        return "blocked", matched
    matched = _longest_match(route, readonly)
    if matched:
        return "readonly", matched
    return "unclassified", None


def _check(
    *,
    routes: dict[str, ast.expr],
    writable: frozenset[str],
    readonly: frozenset[str],
    blocked: frozenset[str],
    offload_prefix: str,
) -> list[str]:
    """返回违规清单（空列表 = 合规）。"""
    problems: list[str] = []

    # 反空转：解析结果为空时下面所有判断都会"通过"，必须显式拦住
    if not routes:
        problems.append("路由表解析为空 —— 本判据会空转，请检查 build_backend 的写法")
    if not writable:
        problems.append("ALLOWED_WRITE_PREFIXES 解析为空 —— 本判据会空转")

    # ① 归类完备性：不许存在第四态
    for route in sorted(routes):
        kind, _matched = _classify(route, writable, readonly, blocked)
        if kind == "unclassified":
            problems.append(
                f"route {route!r} 未被归类：既不在 ALLOWED_WRITE_PREFIXES（不可写），"
                "也未被声明为只读/禁写。这正是 4863afff 的成因 —— "
                "有路由但写入被拒，凡走该路径的机制都会静默失效。"
                "请二选一：加入 ALLOWED_WRITE_PREFIXES，或加入 VIRTUAL_READONLY_PREFIXES。"
            )

    # ② 反向覆盖：白名单每一项都要有承接者，否则写入静默落到 default backend
    for prefix in sorted(writable):
        if prefix in _SANDBOX_DEFAULT_WRITE_PREFIXES:
            continue
        if not any(route.startswith(prefix) for route in routes):
            problems.append(
                f"白名单前缀 {prefix!r} 没有任何 route 承接 —— 写入会落到默认后端"
                "（沙箱 / LocalShellBackend），而白名单声明的是「可写」，两者语义不一致。"
                "若确实要写沙箱内路径，请显式登记到 _SANDBOX_DEFAULT_WRITE_PREFIXES。"
            )

    # ③ 事故回归钉：offload 落点必须 可写 + 有路由 + 指向真实存储
    offload_route = offload_prefix + "/"
    kind, _matched = _classify(offload_route, writable, readonly, blocked)
    if kind != "writable":
        problems.append(
            f"{offload_route!r}（deepagents offload 落点）当前归类为 {kind!r}，不是 'writable' "
            "—— 大工具结果转存必被拒写，原文原地留在 state（4863afff 的直接成因）"
        )
    hit = sorted(r for r in routes if r.startswith(offload_route))
    if not hit:
        problems.append(f"{offload_route!r} 没有对应 route —— 转存无处可写")
    for route in hit:
        backend = _backend_name(routes[route])
        if backend == "StateBackend":
            problems.append(
                f"route {route!r} → StateBackend()：内容仍留在 LangGraph state，"
                "每次 checkpoint 仍要全量序列化落 Postgres。大工具结果应落**真实磁盘**。"
            )
    return problems


# ─── 正向：当前配置必须合规 ───────────────────────────────────────────────────


def test_prefix_sets_are_parseable_and_non_trivial():
    """反空转前置：三个集合都必须解析成功且含已知成员。"""
    inputs = _real_inputs()
    assert "/reports/" in inputs["writable"], "解析白名单失败或 /reports/ 被移除"
    assert "/uploads/" in inputs["readonly"]
    assert "/root/" in inputs["blocked"]
    assert len(inputs["routes"]) >= 8, (
        f"仅解析出 {len(inputs['routes'])} 条路由，疑似漏解析（f-string 路由键处理见 _key_template）"
    )


def test_current_config_has_no_violation():
    problems = _check(**_real_inputs())
    assert problems == [], "路由表 / 白名单一致性违规：\n  - " + "\n  - ".join(problems)


def test_route_classification_is_frozen():
    """每条路由的读写归类必须与冻结期望逐条一致。

    这条的作用是**把漂移变成一次显式决定**：新增 / 删除 / 改类路由时，
    要么这里红，要么人来更新期望值 —— 无论哪种，都不可能"悄悄发生"。
    """
    inputs = _real_inputs()
    actual = {
        route: _classify(route, inputs["writable"], inputs["readonly"], inputs["blocked"])[0]
        for route in inputs["routes"]
    }
    expected = _EXPECTED_ROUTE_CLASSIFICATION
    if actual != expected:
        added = sorted(set(actual) - set(expected))
        removed = sorted(set(expected) - set(actual))
        changed = sorted(
            f"{r}: {expected[r]} -> {actual[r]}"
            for r in set(actual) & set(expected)
            if actual[r] != expected[r]
        )
        raise AssertionError(
            "路由表发生变化 —— 每条路由的可写/只读归类都必须是一次显式决定：\n"
            f"  新增路由: {added or '无'}\n"
            f"  删除路由: {removed or '无'}\n"
            f"  归类变化: {changed or '无'}\n"
            "请在 _EXPECTED_ROUTE_CLASSIFICATION 中登记其归类，并写清理由。"
        )


def test_writable_readonly_blocked_prefix_sets_are_disjoint():
    """三个集合的**精确**重合意味着分类口径打架。

    注意这里比的是集合本身而非前缀包含关系：``/skills/__agent__/``（可写）落在
    ``/skills/``（只读）之下是**有意**的（具体规则优先）；但把整个 ``/skills/``
    放进白名单就会放开系统与用户技能 —— 那是事故，必须被这条拦住。
    """
    inputs = _real_inputs()
    assert not (inputs["writable"] & inputs["blocked"]), (
        "禁写路径被放进了写白名单：" + str(sorted(inputs["writable"] & inputs["blocked"]))
    )
    assert not (inputs["writable"] & inputs["readonly"]), (
        "只读前缀被整体放进了写白名单：" + str(sorted(inputs["writable"] & inputs["readonly"]))
    )


def test_route_table_scope_is_closed():
    """含 routes 表的文件必须被穷举知晓。

    用途是防"第二处路由表"：只在其中一处修好白名单，另一处照样会炸。
    同时要求被排除的历史遗留文件保持**不可达**（无人 import），否则它的路由表
    可能被误用而绕过全部校验。
    """
    live = _files_with_route_table()
    assert "src/services/agent.py" in live, "未在 agent.py 中检出路由表，解析逻辑可能失效"

    unexpected = live - {"src/services/agent.py"} - _DEAD_ROUTE_TABLE_FILES
    assert not unexpected, (
        "检出额外路由表："
        + str(sorted(unexpected))
        + "。每一处都需要纳入本守门人的检查范围（或登记为死文件）。"
    )

    for rel in sorted(_DEAD_ROUTE_TABLE_FILES):
        stem = Path(rel).stem
        assert not _module_is_imported(stem, skip=PROJECT_ROOT / rel), (
            f"{rel} 含路由表且已被 import —— 它不再是死文件，必须纳入检查范围"
        )


def _files_with_route_table() -> set[str]:
    """扫描 src/ 下所有含 ``routes`` 字典的文件（赋值式与关键字参数式都算）。"""
    found: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = _ast_of(path)
        except SyntaxError:
            continue
        if _has_route_table(tree):
            found.add(path.relative_to(PROJECT_ROOT).as_posix())
    return found


def _has_route_table(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "routes" for t in node.targets):
                return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "routes":
                return True
        elif isinstance(node, ast.Call):
            if any(kw.arg == "routes" for kw in node.keywords):
                return True
    return False


def _module_is_imported(stem: str, *, skip: Path) -> bool:
    pattern = re.compile(rf"^\s*(?:from|import)\s+[\w.]*\b{re.escape(stem)}\b", re.MULTILINE)
    roots = [SRC, PROJECT_ROOT / "tests"]
    for root in roots:
        for path in root.rglob("*.py"):
            if path == skip:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                return True
    for path in PROJECT_ROOT.glob("*.py"):
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
            return True
    return False


# ─── 负向对照：把判据打回坏状态，它必须报红（证明不是空转）──────────────────────


def test_negative_control_unclassified_route_is_caught():
    """新增一条"既不可写、也没声明只读"的路由 —— 判据必须报出它。

    这正是漂移的真实发生方式：有人加了路由、忘了白名单。如果这条测试不红，
    说明 test_current_config_has_no_violation 永远会绿，守门是假的。
    """
    inputs = _real_inputs()
    inputs["routes"]["/artifacts/{...}/{...}/"] = ast.parse(
        "FilesystemBackend(root_dir='x', virtual_mode=True)", mode="eval"
    ).body
    problems = _check(**inputs)
    assert any("/artifacts/" in p and "未被归类" in p for p in problems), (
        "新增未归类路由未被检出，判据空转：\n  - " + "\n  - ".join(problems)
    )


def test_negative_control_removing_offload_whitelist_entry_reproduces_incident():
    """把 ``/large_tool_results/`` 从白名单摘掉 = 回到 4863afff 现场。

    判据必须同时报出两件事：该 route 变成"未被归类"、且 offload 落点不可写。
    """
    tree = _ast_of(AGENT_PY)
    node = _class_str_set_node(tree, _BACKEND_CLASS, "ALLOWED_WRITE_PREFIXES")
    assert isinstance(node, (ast.Set, ast.Tuple, ast.List))
    before = len(node.elts)
    node.elts = [
        e for e in node.elts if not (isinstance(e, ast.Constant) and e.value == "/large_tool_results/")
    ]
    assert len(node.elts) == before - 1, "负向对照未能摘掉目标项，用例本身失效"

    problems = _check(**_inputs_from_tree(tree))
    assert any("未被归类" in p for p in problems), (
        "摘掉白名单项后未检出「未被归类」：\n  - " + "\n  - ".join(problems)
    )
    assert any("offload 落点" in p for p in problems), (
        "摘掉白名单项后未检出 offload 落点不可写：\n  - " + "\n  - ".join(problems)
    )

    # 突变只发生在内存里的 AST；磁盘上的真实配置必须仍是合规的
    assert _check(**_real_inputs()) == [], "本用例意外改动了磁盘文件 —— 请检查"


def test_negative_control_state_backend_route_is_detected():
    """把 offload 路由改回 ``StateBackend()`` —— 判据必须报出"内容仍留在 state"。"""
    tree = _ast_of(AGENT_PY)
    routes_node = _routes_dict_node(tree, _BACKEND_BUILDER)
    offload_route = _offload_prefix() + "/"
    hit = 0
    for idx, key in enumerate(routes_node.keys):
        if _key_template(key) == offload_route:
            routes_node.values[idx] = ast.parse("StateBackend()", mode="eval").body
            hit += 1
    assert hit == 1, f"未找到 {offload_route!r} 路由，用例本身失效"

    problems = _check(**_inputs_from_tree(tree))
    assert any("StateBackend" in p for p in problems), (
        "改回 StateBackend() 后未检出：\n  - " + "\n  - ".join(problems)
    )
    assert _check(**_real_inputs()) == [], "本用例意外改动了磁盘文件 —— 请检查"

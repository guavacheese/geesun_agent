# -*- coding: utf-8 -*-
"""D2/D3（2026-11-19，11-19 生产 GraphRecursionError×8 事故）接线契约测试。

事故：合法长开发任务（改码→构建→验证）烧穿 recursion_limit=300（≈50 次工具
调用），GraphRecursionError 落 `except Exception` 兜底 ⇒ Grafana 一片红堆栈 +
只发瞬态 error 事件 + 外层 M3 还可能注入自动继续轮再烧穿一遍。

三项修复（本文件锁接线）：
    D1 recursion_limit=1000 进 deploy/根 .env.example（纯配置，无代码接线）；
    D2 chat.py 撞限结构化收尾：except GraphRecursionError（warning 不甩堆栈、
       completion_blocked recursion_capped 事件）+ B② 无条件兜底 + break 不进
       M3 自动继续 + _terminal_notice 覆盖 A4 文案 + run 标 failed（复用 A4 链）；
    D3 agent.py 显式声明 general-purpose 子代理挂 SubagentBudgetMiddleware
       保险丝（deepagents 0.7.24 无途径 bind recursion_limit，见
       src/core/subagent_budget.py 模块头论证），并显式继承主链
       _SweepThenSummarize（name 匹配，对齐自动注入路径的 _gp_inheritable）。

运行：
    C:/Users/GY24428/.workbuddy/binaries/python/envs/cubesandbox-verify/Scripts/python.exe \\
      -m pytest tests/api/test_recursion_capped_wiring.py -q
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CHAT_PY = PROJECT_ROOT / "src" / "api" / "endpoints" / "chat.py"
AGENT_PY = PROJECT_ROOT / "src" / "services" / "agent.py"


def _chat_src() -> str:
    return CHAT_PY.read_text(encoding="utf-8")


def _agent_src() -> str:
    return AGENT_PY.read_text(encoding="utf-8")


def _section(src: str, start_marker: str, end_marker: str) -> str:
    """截取两个稳定锚点之间的源码段（锚点不存在时测试直接失败）。"""
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


# ═══════════════ D2：GraphRecursionError 分支本体 ═══════════════


def test_graph_recursion_error_imported():
    assert "from langgraph.errors import GraphRecursionError" in _chat_src()


def test_except_branch_exists_before_generic_exception():
    """分支必须存在且排在 `except Exception` **之前**——否则 GraphRecursionError
    仍被泛型兜底吞掉（logger.exception 甩堆栈 = 本次要修的症状本身）。"""
    src = _chat_src()
    i = src.index("except GraphRecursionError as e:")
    # 切片恰好等于新分支本体（下一个 except 子句是 NoProgressAbortError）
    j = src.index("except NoProgressAbortError as e:", i)
    assert i < src.index("except Exception as e:", j), (
        "GraphRecursionError 分支必须排在泛型 except Exception 之前"
    )
    branch = src[i:j]
    # 结构化收尾四件套
    assert "_recursion_capped = True" in branch
    assert "_terminal_error =" in branch and "_terminal_notice =" in branch
    assert "'code': 'recursion_capped'" in branch
    assert "'type': 'completion_blocked'" in branch
    assert "'terminated': True" in branch
    # 不许再甩堆栈：本分支内不得出现 logger.exception **调用**
    # （带括号匹配——分支注释里提到这个词是论证本身，不能误伤）
    assert "logger.exception(" not in branch, (
        "D2 分支里出现 logger.exception——GraphRecursionError 是容量到顶不是"
        "程序 bug，堆栈只会淹没 Grafana 面板（11-19 事故原症状）"
    )


def test_nonlocal_declares_recursion_state():
    src = _chat_src()
    assert "nonlocal _recursion_capped, _terminal_notice" in src


def test_salvage_trigger_includes_recursion_capped():
    """B② 兜底触发条件必须含 `_recursion_capped`：撞限轮无条件尝试拉回沙箱
    产物，不再受词表判交付/流式识别条件限制。"""
    i = _chat_src().index("if (\n                    not _salvage_done")
    cond = _chat_src()[i:i + 900]
    assert "_recursion_capped" in cond
    # 必须走 exclude 判据（GY22328:59bbcbe3 实证的上传件误捞根因，兜底无论
    # 哪个来源触发都必须排除上传清单）
    assert "exclude=_uploaded_sandbox_paths" in _chat_src()[i:i + 3000]


def test_recursion_capped_break_before_m3():
    """撞限轮在 M3 完成门/空响应降级**之前** break：自动继续轮等于自动重烧
    一整个上限配额。"""
    src = _chat_src()
    i = src.index("if _recursion_capped:")
    j = src.index("# ─── 空响应降级优先于 M3 零产出拦截（2026-09-16）───", i)
    seg = src[i:j]
    assert "break" in seg
    # 位置正确性由上面两行的 index 顺序天然保证（找不到即 ValueError → 测试失败）


def test_a4_notice_honors_override():
    """A4 收尾必须尊重 _terminal_notice 覆盖，同时保留 ToolLoopAbort 默认尾巴
    （两条终止语境的文案不能互相套用）。"""
    src = _chat_src()
    assert "_terminal_notice or (" in src
    # 默认尾巴仍在（ToolLoopAbortError 不设 _terminal_notice，走默认文案）
    assert "联系管理员从沙箱快照恢复文件" in src


# ═══════════════ D3：general-purpose 子代理保险丝接线 ═══════════════


def _agent_fn_tail(src: str) -> str:
    """_build_deep_agent 函数体：它是 agent.py 最后一个顶层函数，截到 EOF。"""
    i = src.index("def _build_deep_agent(")
    return src[i:]


def test_build_deep_agent_declares_gp_subagent():
    src = _agent_src()
    fn = _agent_fn_tail(src)
    assert "subagents=[_gp_spec]" in fn
    assert '"name": "general-purpose"' in fn, (
        "显式 gp 名必须与 deepagents GENERAL_PURPOSE_SUBAGENT 同名——graph.py "
        "L823 按名字抑制默认注入，名字打错会同时存在两个 general-purpose"
    )
    # 文案必须导入常量而非抄写（升级自动跟随默认演化）
    assert "DEFAULT_GENERAL_PURPOSE_DESCRIPTION" in _section(
        src, "from deepagents.middleware.subagents import", ")\n"
    ) or "DEFAULT_GENERAL_PURPOSE_DESCRIPTION" in fn


def test_gp_spec_inherits_summarization_and_has_budget():
    src = _agent_src()
    fn = _agent_fn_tail(src)
    # ① 按 name 继承主链 SummarizationMiddleware（_SweepThenSummarize）
    assert 'm.name == "SummarizationMiddleware"' in fn, (
        "显式 spec 不走自动路径的 _gp_inheritable，漏继承会让子代理退回原生"
        " SummarizationMiddleware，丢掉 sweep-then-summarize 手术语义"
    )
    # ② 保险丝在列
    assert "SubagentBudgetMiddleware(" in fn
    assert "settings.subagent_max_model_calls" in fn


def test_config_default_matches_documentation():
    """代码默认值必须与两处 .env.example 的说明一致（模板注释承诺"下列数值
    即默认"的语义，config 漂移要在这里被抓）。

    为什么读源码而不是 import Settings：`from src.core.config import Settings`
    会触发模块级 `settings = Settings()` 单例，required 字段（openai_api_key
    等）缺失即 ValidationError——生产镜像内无服务 env 时整条测试直接崩
    （1.0.53 镜像内复跑实测）。本文件其余用例都是源码级断言，本用例同构。
    """
    cfg = (PROJECT_ROOT / "src" / "core" / "config.py").read_text(encoding="utf-8")
    # recursion_limit 代码默认保持保守值，1000 是生产 .env 值（模板已注明）
    assert "recursion_limit: int = 300" in cfg
    assert "max_recursion_limit: int = 1000" in cfg
    assert "subagent_max_model_calls: int = 150" in cfg
    # 模板存在时连带核（镜像内不带 .env.example，属预期缺省，不是失败）
    for tpl in (PROJECT_ROOT / ".env.example", PROJECT_ROOT / "deploy" / ".env.example"):
        if tpl.exists():
            t = tpl.read_text(encoding="utf-8")
            # 模板写的是生产推荐值 1000（代码默认 300 是保守兜底，见上）
            assert "RECURSION_LIMIT=1000" in t or "recursion_limit=1000" in t, tpl.name

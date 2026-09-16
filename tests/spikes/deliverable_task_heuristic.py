"""Spike: M3 完成门任务类型判定 _is_deliverable_task 的回归位（2026-09-16 新增）。

背景（生产会话 GY24428:de18ad37 实证）：
    纯视觉标注任务（HMI 截图 + 控件命名规则）被判定为"文件交付任务"，
    模型因单次输出超限被截断（一个字没产出）后，M3 完成门据此输出
    「本轮任务未产出任何交付物（/reports 为空）/ 请检查是否遗漏 download_from_sandbox /
    write_file 步骤」——**归因错误**。
    命中源是用户原文里的顺带词：动词「严禁**输出**缺失位置的速度/位置设定」
    × 名词「【**表格**/矩阵布局专属规则】」，二者分处不同段落，纯词袋拼接误命中。

本 spike 直接 import **真实函数**（不是复制一份常量），跑正/负样例矩阵。
反向验证：把 chat.py 里的 `_CLAUSE_SPLIT` 同句约束去掉、或把 "输出" 加回
`_DELIVERABLE_VERBS`，下面 N1/N5/N6 会立刻 FAIL。

运行环境：**生产同款镜像**（chat.py 依赖 fastapi 等，本机 .venv 是 Linux 布局）：
  docker run --rm -v 'D:/workspace/geesun_agent/src:/app/src:ro' \
    -v 'D:/workspace/geesun_agent/tests:/app/tests:ro' --entrypoint sh \
    172.16.220.74:8333/geesun_ai/geesun-agent:1.0.16 \
    -c 'cd /app && /app/.venv/bin/python tests/spikes/deliverable_task_heuristic.py'
"""

from __future__ import annotations

import sys
from typing import Any

sys.path.insert(0, "/app")

from src.api.endpoints.chat import (
    _DELIVERABLE_NOUNS,
    _DELIVERABLE_STRONG,
    _DELIVERABLE_VERBS,
    _is_deliverable_task,
)

PASS = 0
FAIL = 0


def check(label: str, actual: Any, expected: Any) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}\n      期望: {expected!r}\n      实际: {actual!r}")


# ─── 真实会话文本（de18ad37 用户消息的关键片段）───
# 触发误命中的两处原文（真实文本里同样分处不同行）：
#   L3 名词源：「【表格/矩阵布局专属规则】…标准表格内…」
#   L5 动词源：「…严禁输出缺失位置的速度/位置设定。」（原句是超长行的末尾短语，
#      此处按行摘录并省略中段，用「……」标注，不影响子串判定）
REAL_SESSION_TEXT = (
    "# 命名规则（严格按优先级执行）\n"
    "3.  【表格/矩阵布局专属规则】控件位于标准表格内（有明确行、列划分），"
    "格式固定为：`{行标题} + {列标题} + {控件类型}`\n"
    "5.  【电机\\伺服控件专项强化规则】……"
    "严禁输出缺失位置的速度/位置设定。\n"
    "    - ❌ 禁止出现：`M<数字> + <...>电机\\伺服 + 速度数值输入`（有“位置”或“速度”关键字但缺失具体位置）\n"
    "    - 若上下文**未出现**上述关键字，则按普通控件处理，无需强制添加标识。\n"
    "用户为本轮对话上传了以下文件（路径已映射到虚拟文件系统，请精确处理这些文件）：\n"
    "- /uploads/GY24428/de18ad37/【191页】【OD0005】...【(870, 329)-(971, 384)】.png\n"
)

NEGATIVE = [
    ("N1 真实会话（视觉标注，完成门本不该介入）", REAL_SESSION_TEXT, False),
    ("N2 总结一下会议纪要（2026-08-26 已记录误伤）", "总结一下会议纪要", False),
    ("N3 分析一下这个方案（注释里记录的误伤）", "分析一下这个方案", False),
    ("N4 解释一下什么是 GRPC", "解释一下什么是 GRPC", False),
    ("N5 非交付型分析请求", "帮我看看这段日志，分析一下为什么报错", False),
    (
        "N6 对抗：规则型文本（动词与名词分处不同句/段）",
        "严禁输出缺失位置的速度/位置设定。\n【表格/矩阵布局专属规则】控件位于标准表格内。",
        False,
    ),
    (
        "N7 对抗：禁止生成的措辞 + 后文提到表格",
        "禁止生成多余的文字说明。\n表格规则：控件位于标准表格内。",
        False,
    ),
    ("N8 纯问答", "这个报错怎么解决", False),
    ("N9 空串", "", False),
]

POSITIVE = [
    ("P1 一句话内动词×名词", "帮我生成一份报告", True),
    ("P2 同句跨分句", "对比这两个 PDF，生成一份差异报告。", True),
    ("P3 名词在动词前", "这份表格帮我整理一下", True),
    ("P4 强信号-导出", "导出成 excel", True),
    ("P5 强信号-保存为", "把结果保存为 csv", True),
    ("P6 长句真实意图", "帮我生成一份关于今年销售的详细报告，越详细越好", True),
    ("P7 skill 工作流", "按 protocol-diff 对比两份技术协议，生成 HTML 报告", True),
    ("P8 强信号-写入文件", "把这段内容写入文件", True),
]


def main() -> int:
    print("=" * 74)
    print("M3 完成门任务类型判定 spike（_is_deliverable_task）")
    print("=" * 74)

    print("\n=== 常量契约（防有人把'输出'加回去、或删掉同句约束）===")
    check("裸'输出'不在动词表内", "输出" in _DELIVERABLE_VERBS, False)
    check("'输出文件'仍在强信号内", "输出文件" in _DELIVERABLE_STRONG, True)
    check("名词表仅含产出物语义", set(_DELIVERABLE_NOUNS), {"报告", "报表", "表格", "文档", "文件", "工作簿"})

    print("\n=== 负样例：不应判定为文件交付任务 ===")
    for label, text, expected in NEGATIVE:
        check(label, _is_deliverable_task(text), expected)

    print("\n=== 正样例：应判定为文件交付任务 ===")
    for label, text, expected in POSITIVE:
        check(label, _is_deliverable_task(text), expected)

    print("\n=== 反向验证锚点 ===")
    print("  去掉 _CLAUSE_SPLIT 同句约束 → N6/N7 应 FAIL（跨句词袋拼接会误命中）")
    print("  把裸'输出'加回 _DELIVERABLE_VERBS → N1/N6 应 FAIL")

    print("\n" + "=" * 74)
    print(f"结果: {PASS} PASS / {FAIL} FAIL")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

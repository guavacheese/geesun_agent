"""tests 全局 conftest（2026-11-17 动作二引入）。

本地测试 venv（cubesandbox-verify）装的是 deepagents 0.7.24，而 src/services/agent.py
的 SummarizationMiddleware monkey-patch 带版本闸门（非 0.6.x 直接 RuntimeError）。
测试环境设置逃生阀跳过 patch（error 级日志），保证测试套件可跑；
生产镜像不设置该变量，闸门保持 fail-fast。
"""

import os

os.environ.setdefault("GEESUN_ALLOW_DEEPAGENTS_MISMATCH", "1")

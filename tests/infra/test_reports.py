"""M3 完成门单测：reports 目录快照与差集判定（设计文档 M3-v1）。

覆盖：目录不存在返回空集、递归收集文件+目录、本轮新增判定（差集）、
多轮会话历史文件不误判。纯标准库，无外部依赖。

运行：pytest tests/infra/test_reports.py -q
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.infra.reports import snapshot_report_files  # noqa: E402


@pytest.fixture
def report_dir(tmp_path: Path) -> tuple[str, str, str]:
    """构造 report_root/user/sid 三层目录，返回 (root, user, sid)。"""
    root = tmp_path / "reports"
    user = "u1"
    sid = "s1"
    (root / user / sid).mkdir(parents=True)
    return str(root), user, sid


def test_dir_not_exists_returns_empty(tmp_path):
    assert snapshot_report_files(str(tmp_path / "nope"), "u", "s") == frozenset()


def test_empty_dir_returns_empty(report_dir):
    root, user, sid = report_dir
    assert snapshot_report_files(root, user, sid) == frozenset()


def test_collect_files_and_subdirs(report_dir):
    root, user, sid = report_dir
    base = Path(root) / user / sid
    (base / "a.txt").write_text("x")
    (base / "sub").mkdir()
    (base / "sub" / "b.go").write_text("y")
    (base / "sub" / "deep").mkdir()

    snap = snapshot_report_files(root, user, sid)
    assert "a.txt" in snap
    assert "sub" in snap
    assert "sub/b.go" in snap
    assert "sub/deep" in snap
    assert len(snap) == 4


def test_diff_detects_only_new_files(report_dir):
    """差集判定：本轮新增文件应出现在 after - before。"""
    root, user, sid = report_dir
    base = Path(root) / user / sid

    before = snapshot_report_files(root, user, sid)
    (base / "new_report.md").write_text("report")

    after = snapshot_report_files(root, user, sid)
    new_files = after - before
    assert "new_report.md" in new_files
    assert len(new_files) == 1


def test_multi_turn_history_not_recounted(report_dir):
    """多轮会话：上轮已存在的文件不计入本轮新增（差集语义）。"""
    root, user, sid = report_dir
    base = Path(root) / user / sid
    (base / "prev_turn.md").write_text("old")  # 上轮产物

    before = snapshot_report_files(root, user, sid)  # 本轮开始前基线
    (base / "this_turn.md").write_text("new")

    after = snapshot_report_files(root, user, sid)
    new_files = after - before
    assert "this_turn.md" in new_files
    assert "prev_turn.md" not in new_files  # 历史文件不误判


def test_same_file_overwrite_not_new(report_dir):
    """覆盖同名文件不产生新差集（v1 限制，记录预期行为）。"""
    root, user, sid = report_dir
    base = Path(root) / user / sid
    f = base / "x.txt"
    f.write_text("v1")

    before = snapshot_report_files(root, user, sid)
    f.write_text("v2")  # 覆盖，内容变了但路径集合不变

    after = snapshot_report_files(root, user, sid)
    assert after - before == frozenset()  # 路径差集为空 → 判定为未新增

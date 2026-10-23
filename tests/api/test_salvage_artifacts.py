# -*- coding: utf-8 -*-
"""收尾兜底的回收策略（2026-10-23，589e99f5 事故）。

事故：262 页 OCR 跑完 7 批，结论在沙箱 `/home/user/_ocr/result.txt` 与
每批 `result.json` 里；但收尾兜底按「文件大小降序」取前 5 个 ——
256MB 原始 PDF + 4 个 38~52MB 子 PDF 把配额吃光，`result.txt`(~800KB)
一个都没排上。更糟的是落盘只用 basename：7 个批次各有一份
`batches/bNNN/input.sub.pdf`，互相覆盖成 1 份（宿主最后只剩 b002 那份）。

本文件锁两条修法：
  B1 排序：结论类扩展名优先，OCR 拆页产生的子 PDF 排最后；
  B2 命名：重名加父目录前缀，**绝不互相覆盖**。
"""

import asyncio
import os

import pytest

from src.api.endpoints.chat import (
    _SALVAGE_MAX_FILES,
    _salvage_disk_name,
    _salvage_sandbox_artifacts,
    _salvage_sort_key,
)
from src.core.config import settings

USER = "GY24428"
SID = "589e99f5"


class _Resp:
    def __init__(self, content=None, output=""):
        self.content = content
        self.output = output
        self.exit_code = 0


class FakeSandbox:
    """最小沙箱替身：`execute` 只用于 find 枚举，`download_files` 取内容。"""

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.download_calls: list[list[str]] = []

    def execute(self, command: str, timeout: int | None = None) -> _Resp:
        lines = ["%s\t%d" % (p, len(c)) for p, c in sorted(self.files.items())]
        return _Resp(output="\n".join(lines) + ("\n" if lines else ""))

    def download_files(self, paths: list[str]) -> list[_Resp]:
        self.download_calls.append(list(paths))
        return [_Resp(content=self.files.get(p)) for p in paths]


@pytest.fixture()
def report_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "report_root", str(tmp_path))
    return str(tmp_path)


def _ocr_workdir_files(n_batches: int = 7) -> dict[str, bytes]:
    """589e99f5 场景的沙箱文件表：结论 8 个 + 子 PDF 7 个 + 原 PDF。"""
    files = {
        # 原 PDF（256MB 级）与拆页子 PDF（几十 MB 级）：是输入/脚手架
        "/home/user/康视达 CST2026手册.pdf": b"P" * 260_000,
        "/home/user/_ocr/result.txt": b"R" * 800,
    }
    for i in range(1, n_batches + 1):
        d = "/home/user/_ocr/batches/b%03d" % i
        files["%s/result.json" % d] = b"J" * 5_700
        files["%s/input.sub.pdf" % d] = b"S" * 38_000
    return files


# ───────────────────────────── B1：排序优先级 ─────────────────────────────


def test_sort_key_prefers_result_files_over_bulky_pdfs():
    """结论类必须排在子 PDF / 原 PDF 之前（旧实现按大小降序，正好相反）。"""
    small_result = _salvage_sort_key("/home/user/_ocr/result.txt", 800)
    big_json = _salvage_sort_key("/home/user/_ocr/batches/b001/result.json", 5_700)
    orig_pdf = _salvage_sort_key("/home/user/康视达 CST2026手册.pdf", 260_000)
    sub_pdf = _salvage_sort_key("/home/user/_ocr/batches/b002/input.sub.pdf", 38_000)

    assert big_json < small_result, (
        "同一个桶内按大小降序（大报告更可能是正式交付物）："
        "5.7KB 的 result.json 应排在 800B 的 result.txt 前"
    )
    assert small_result < orig_pdf, "结论类必须整体排在 256MB 原 PDF 前"
    assert sub_pdf > orig_pdf, "OCR 拆页子 PDF 是脚手架，必须排在原 PDF 之后"


def test_salvage_keeps_ocr_conclusions(report_root):
    """589e99f5 复放：7 批 OCR 跑完后，结论文件必须真的被拉回宿主。

    修法前：前 5 个槽位被 1 个原 PDF + 4 个子 PDF 占满，result.txt 直接丢。
    """
    files = _ocr_workdir_files()
    salvaged = asyncio.run(
        _salvage_sandbox_artifacts(FakeSandbox(files), USER, SID, before={})
    )
    names = [s["file_name"] for s in salvaged]
    assert "result.txt" in names, "4h45m 的 OCR 结论必须被救回，实际：%s" % names
    # 8 个结论文件（result.txt + 7 个 result.json）一个都不能少
    json_count = sum(1 for n in names if n.endswith("result.json"))
    assert json_count == 7, "7 个批次的 result.json 应全部回收，实际 %d" % json_count
    assert len(salvaged) <= _SALVAGE_MAX_FILES
    # 落盘必须真实存在且内容一致
    root = os.path.join(report_root, USER, SID)
    assert os.path.getsize(os.path.join(root, "result.txt")) == 800
    for item in salvaged:
        assert os.path.isfile(os.path.join(root, item["file_name"]))


# ───────────────────────────── B2：重名不覆盖 ─────────────────────────────


def test_salvage_same_basename_across_dirs_never_overwrites(report_root):
    """7 个批次同名 `input.sub.pdf` 必须落成 7 个文件，而不是互相覆盖。

    旧实现 `disk_path = join(root, base_name)` ⇒ 7 份覆盖成 1 份。
    """
    files = {
        "/home/user/_ocr/batches/b%03d/input.sub.pdf" % i: bytes([65 + i]) * 1000
        for i in range(1, 8)
    }
    salvaged = asyncio.run(
        _salvage_sandbox_artifacts(FakeSandbox(files), USER, SID, before={})
    )
    names = [s["file_name"] for s in salvaged]
    assert len(names) == 7, "7 个同名文件应各自落盘，实际 %s" % names
    assert len(set(names)) == 7, "落盘名重复 = 互相覆盖（B2 未生效）：%s" % names
    root = os.path.join(report_root, USER, SID)
    on_disk = sorted(os.listdir(root))
    assert len(on_disk) == 7, "宿主目录应有 7 个文件，实际 %s" % on_disk
    # 内容各不相同（b001..b007 各写了不同字节）
    contents = {open(os.path.join(root, n), "rb").read()[:1] for n in on_disk}
    assert len(contents) == 7, "有文件被覆盖：内容只剩 %d 种" % len(contents)


def test_salvage_disk_name_prefixes_parent_dir():
    """命名规则：首次用 basename，重名逐级加父目录前缀，极端情况加序号。"""
    used: set[str] = set()
    assert _salvage_disk_name("/home/user/a/input.pdf", used) == "input.pdf"
    assert _salvage_disk_name("/home/user/b/input.pdf", used) == "b__input.pdf"
    assert _salvage_disk_name("/home/user/c/input.pdf", used) == "c__input.pdf"
    # 父目录前缀也撞（不同父路径但同名父目录）⇒ 再往上加一层
    assert _salvage_disk_name("/other/b/input.pdf", used) == "other__b__input.pdf"


# ───────────────────────── 差集语义不能被改坏 ─────────────────────────


def test_salvage_still_skips_files_from_previous_round(report_root):
    """上一轮遗留（before 快照里同尺寸）不得被当成本轮产物。"""
    files = {
        "/home/user/上一轮遗留.md": b"old",
        "/home/user/本轮新产物.txt": b"fresh",
    }
    before = {"/home/user/上一轮遗留.md": 3}
    salvaged = asyncio.run(
        _salvage_sandbox_artifacts(FakeSandbox(files), USER, SID, before=before)
    )
    assert [s["file_name"] for s in salvaged] == ["本轮新产物.txt"]


def test_salvage_gives_up_without_baseline(report_root):
    """before=None（开轮枚举失败）⇒ 放弃兜底，不能把存量文件算到本轮头上。"""
    files = {"/home/user/a.txt": b"x"}
    assert asyncio.run(
        _salvage_sandbox_artifacts(FakeSandbox(files), USER, SID, before=None)
    ) == []


def test_salvage_respects_total_bytes_cap(report_root, monkeypatch):
    """累计落盘上限：大文件凑满上限后跳过（继续尝试后面的小文件）。

    槽位从 5 提到 10 后，若候选全是 200MB+ 原件会一次搬 2GB —— 用总量封顶，
    而不是「限制大文件个数」：结论类文件只有几百 KB，不该被大文件连累。
    """
    from src.api.endpoints import chat as chat_mod

    monkeypatch.setattr(chat_mod, "_SALVAGE_MAX_BYTES", 10_000)
    files = {
        "/home/user/a.txt": b"a" * 1_000,      # 结论类，先入
        "/home/user/b.pdf": b"b" * 6_000,      # 入（累计 7000）
        "/home/user/c.pdf": b"c" * 6_000,      # 13000 > 10000 ⇒ 跳过
    }
    salvaged = asyncio.run(
        _salvage_sandbox_artifacts(FakeSandbox(files), USER, SID, before={})
    )
    names = [s["file_name"] for s in salvaged]
    assert names == ["a.txt", "b.pdf"], "累计上限未生效或被大文件拖累：%s" % names

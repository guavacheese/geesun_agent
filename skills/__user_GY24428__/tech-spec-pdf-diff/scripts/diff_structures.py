#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diff_structures.py — 对两份技术协议的结构化文本（JSON）做章节级对齐与差异初筛。

说明：
  技术协议内容千差万别（激光/卷绕/碟片/涂布/模切等），条款文本完全规则化比对
  不可靠。本脚本只做**结构级**辅助：
    1. 章节标题对齐（一、二、三... + 数字小节）
    2. 每个章节的条款条目数统计（粗粒度）
    3. 输出"结构对齐表" JSON，标注：仅 A 有 / 仅 B 有 / 标题不同 / 页码偏移
  条款文本的**语义级**差异比对由 Agent（LLM）基于 extract_pdf.py 的完整文本
  逐章完成——因为只有模型能正确理解"加强筋模块新增"这类语义差异。

输入：两个 JSON 文件（extract_pdf.py 的输出，含 pages 与可选 chapters）
输出：
  --print : 打印结构对齐表（人类可读）
  --out   : 写入 JSON（章节差异标注）

用法：
  python diff_structures.py A.json B.json --print
  python diff_structures.py A.json B.json --out diff.json
"""

import argparse
import json
import re
import sys


CHAPTER_RE = re.compile(r"^([一二三四五六七八九十]+)、\s*(.+?)\s*$")
SECTION_RE = re.compile(r"^(\d+)[\.．、]\s*(.+?)\s*$")

# 页眉页脚之外的"噪声行"（数字页码等）在比对时忽略
NOISE_PATTERNS = [
    r"^\s*\d+\s*$",
    r"^[A-Za-z0-9\-]+\s*$",
]


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_structure(data: dict) -> list[dict]:
    """
    从分页文本构建结构树：
    [{"chapter": "一", "title": "...", "start_page": N,
      "sections": [{"no": "1", "title": "...", "page": N}],
      "lines": [...全文行...]}]
    """
    pages = data.get("pages", [])
    struct = []
    cur_chapter = None
    cur_section = None

    def flush_section():
        nonlocal cur_section
        if cur_section is not None and cur_chapter is not None:
            cur_chapter["sections"].append(cur_section)
            cur_section = None

    def flush_chapter():
        nonlocal cur_chapter
        if cur_chapter is not None:
            flush_section()
            struct.append(cur_chapter)
            cur_chapter = None

    for pidx, text in enumerate(pages):
        for line in text.split("\n"):
            s = line.strip()
            if not s:
                continue
            if any(re.match(p, s) for p in NOISE_PATTERNS):
                continue
            cm = CHAPTER_RE.match(s)
            if cm:
                flush_chapter()
                cur_chapter = {"chapter": cm.group(1), "title": cm.group(2),
                               "start_page": pidx + 1, "sections": [], "lines": []}
                continue
            sm = SECTION_RE.match(s)
            if sm and cur_chapter is not None:
                flush_section()
                cur_section = {"no": sm.group(1), "title": sm.group(2), "page": pidx + 1}
                continue
            if cur_chapter is not None:
                cur_chapter["lines"].append(s)
                if cur_section is not None:
                    cur_section.setdefault("lines", []).append(s)
    flush_chapter()
    return struct


def align(a: list[dict], b: list[dict]) -> list[dict]:
    """
    按章节序号对齐两份结构树，标注差异。
    返回 [{"chapter": "一", "title_a": ..., "title_b": ...,
           "page_a": N, "page_b": N,
           "status": "same" | "title_diff" | "only_a" | "only_b",
           "sections_a": [...], "sections_b": [...]}]
    """
    rows = []
    maxlen = max(len(a), len(b))
    for i in range(maxlen):
        ca = a[i] if i < len(a) else None
        cb = b[i] if i < len(b) else None
        if ca is None:
            rows.append({"chapter": cb["chapter"], "title_a": None, "title_b": cb["title"],
                         "page_a": None, "page_b": cb["start_page"],
                         "status": "only_b", "sections_a": [], "sections_b": cb.get("sections", [])})
            continue
        if cb is None:
            rows.append({"chapter": ca["chapter"], "title_a": ca["title"], "title_b": None,
                         "page_a": ca["start_page"], "page_b": None,
                         "status": "only_a", "sections_a": ca.get("sections", []), "sections_b": []})
            continue
        status = "same"
        if ca["title"].strip() != cb["title"].strip():
            status = "title_diff"
        rows.append({
            "chapter": ca["chapter"],
            "title_a": ca["title"], "title_b": cb["title"],
            "page_a": ca["start_page"], "page_b": cb["start_page"],
            "status": status,
            "sections_a": ca.get("sections", []), "sections_b": cb.get("sections", []),
        })
    return rows


def count_clauses(struct: list[dict]) -> dict:
    """统计每个章节的条款/小节数（粗粒度，供 Agent 参考）。"""
    out = {}
    for ch in struct:
        key = f"{ch['chapter']}、{ch['title'][:20]}"
        out[key] = len(ch.get("sections", []))
    return out


def main():
    ap = argparse.ArgumentParser(description="技术协议结构级差异初筛")
    ap.add_argument("json_a", help="文档 A 的结构 JSON（extract_pdf.py 输出）")
    ap.add_argument("json_b", help="文档 B 的结构 JSON")
    ap.add_argument("--out", help="写入对齐结果 JSON")
    ap.add_argument("--print", action="store_true", help="打印人类可读对齐表")
    args = ap.parse_args()

    data_a, data_b = load(args.json_a), load(args.json_b)
    struct_a = build_structure(data_a)
    struct_b = build_structure(data_b)
    rows = align(struct_a, struct_b)

    if args.print:
        print(f"文档 A: {data_a.get('source')}（{len(struct_a)} 章）")
        print(f"文档 B: {data_b.get('source')}（{len(struct_b)} 章）")
        print("=" * 70)
        print(f"{'章节':<6}{'状态':<12}{'A 页码':<8}{'B 页码':<8}A 标题")
        for r in rows:
            ta = r["title_a"] or "—"
            tb = r["title_b"] or "—"
            marker = {
                "same": "相同",
                "title_diff": "标题差异",
                "only_a": "仅 A 有",
                "only_b": "仅 B 有",
            }.get(r["status"], r["status"])
            print(f"{r['chapter']:<6}{marker:<12}{str(r['page_a']):<8}{str(r['page_b']):<8}{ta}")
            if r["status"] == "title_diff":
                print(f"{'':<6}{'':<12}{'':<8}{'':<8}B: {tb}")
        print("=" * 70)
        print("\n[提示] 章节标题对齐仅用于快速定位；条款文本的语义差异需基于")
        print("extract_pdf.py 的完整分页文本由 Agent 逐章比对。")

    if args.out:
        result = {
            "doc_a": data_a.get("source"),
            "doc_b": data_b.get("source"),
            "chapter_alignment": rows,
            "clause_count_a": count_clauses(struct_a),
            "clause_count_b": count_clauses(struct_b),
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        sys.stderr.write(f"[OK] 对齐结果已写入: {args.out}\n")


if __name__ == "__main__":
    main()

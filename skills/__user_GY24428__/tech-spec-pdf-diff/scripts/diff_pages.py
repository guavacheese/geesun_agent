#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diff_pages.py — 逐页 diff 初筛：定位两份结构化 JSON（extract_pdf.py 输出）的差异页，
输出行级差异摘要，供 Agent 在 Step 3 中只审阅"候选差异页"（而非全文），
再结合完整文本对"实质差异 vs 排版噪声"做语义判定。

设计动机（2026-08-18 实测沉淀）：
  1091 vs 1096 比对（86 页）中 46 个差异页里约 30 页是空格/换行/分页偏移/表格提取顺序
  等排版噪声。全量手工逐章比对 token 消耗大且易漏；本脚本先把"哪里不同"机械定位出来，
  语义判定（条款增删/数值变化/模块新增）仍由 Agent 完成。

用法：
  # 1) 定位差异页（默认：打印差异页列表 + 每页增删行统计）
  python diff_pages.py docA.json docB.json --print

  # 2) 审阅指定页的行级 diff（- = 文档 A 独有，+ = 文档 B 独有）
  python diff_pages.py docA.json docB.json --print --pages 40-46

  # 3) 打印指定页的完整文本对照（跨页表格/长条款合并审查时用）
  python diff_pages.py docA.json docB.json --full --pages 42-47

  # 4) 输出结构化 JSON（差异页 + 每页增删统计；不含全文行 diff，避免中间文件过大）
  python diff_pages.py docA.json docB.json --out diff_pages.json

依赖：仅标准库（json / difflib / argparse）
Python：>=3.10
"""

import argparse
import difflib
import json
import sys


def load_pages(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["pages"]


def page_diff_summary(ta: str, tb: str) -> dict:
    """返回 {added, deleted}：基于行级 SequenceMatcher 的增删行数。"""
    la, lb = ta.split("\n"), tb.split("\n")
    sm = difflib.SequenceMatcher(None, la, lb)
    added = deleted = 0
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op == "replace":
            added += b2 - b1
            deleted += a2 - a1
        elif op == "insert":
            added += b2 - b1
        elif op == "delete":
            deleted += a2 - a1
    return {"a_lines": len(la), "b_lines": len(lb), "added": added, "deleted": deleted}


def print_page_diff(page_no: int, ta: str, tb: str):
    print(f"\n########## 第 {page_no} 页 差异 =====")
    la, lb = ta.split("\n"), tb.split("\n")
    sm = difflib.SequenceMatcher(None, la, lb)
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op == "equal":
            continue
        if op == "replace":
            for ln in la[a1:a2]:
                print(f"  - A: {ln}")
            for ln in lb[b1:b2]:
                print(f"  + B: {ln}")
        elif op == "delete":
            for ln in la[a1:a2]:
                print(f"  - A: {ln}")
        elif op == "insert":
            for ln in lb[b1:b2]:
                print(f"  + B: {ln}")


def parse_pages(spec: str, max_page: int) -> list[int]:
    """解析 --pages：支持 '40-46'、'40,42,45'、'42'。越界页自动裁剪。"""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s.strip()), int(hi_s.strip())
        else:
            lo = hi = int(part)
        for p in range(lo, hi + 1):
            if 1 <= p <= max_page and p not in out:
                out.append(p)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description="技术协议 PDF 差异页初筛（基于 extract_pdf.py 输出）")
    ap.add_argument("docA", help="文档 A 的结构化 JSON（extract_pdf.py 输出）")
    ap.add_argument("docB", help="文档 B 的结构化 JSON（extract_pdf.py 输出）")
    ap.add_argument("--print", action="store_true", help="打印差异页摘要/行级 diff")
    ap.add_argument("--pages", help="页码范围，如 '40-46' / '40,42,45' / '42'（配合 --print/--full）")
    ap.add_argument("--full", action="store_true", help="打印完整文本对照（需配合 --pages）")
    ap.add_argument("--out", help="输出结构化 JSON 到文件（差异页 + 每页增删统计）")
    args = ap.parse_args()

    pages_a = load_pages(args.docA)
    pages_b = load_pages(args.docB)
    n = min(len(pages_a), len(pages_b))
    if len(pages_a) != len(pages_b):
        print(f"[提示] 页码不一致：A={len(pages_a)} 页 B={len(pages_b)} 页（按前 {n} 页比对）", file=sys.stderr)

    diff_pages = []
    per_page = {}
    for i in range(n):
        ta, tb = pages_a[i], pages_b[i]
        if ta == tb:
            continue
        p = i + 1
        diff_pages.append(p)
        per_page[str(p)] = page_diff_summary(ta, tb)

    print(f"文档 A: {len(pages_a)} 页, 文档 B: {len(pages_b)} 页")
    print(f"有差异的页（共 {len(diff_pages)} 页）: {diff_pages}")

    if args.out:
        result = {
            "doc_a_pages": len(pages_a),
            "doc_b_pages": len(pages_b),
            "diff_page_count": len(diff_pages),
            "diff_pages": diff_pages,
            "per_page": per_page,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        print(f"[OK] JSON 已写入: {args.out}", file=sys.stderr)
        # --print 与 --out 可同时使用；若只 --out 不带 --print，仍打印摘要行

    if args.pages:
        for p in parse_pages(args.pages, n):
            if p not in diff_pages:
                print(f"\n第 {p} 页: 无差异")
                continue
            if args.full:
                print(f"\n========== 第 {p} 页 完整文本对照 =====\n--- A ---\n{pages_a[p-1]}\n--- B ---\n{pages_b[p-1]}")
            else:
                print_page_diff(p, pages_a[p - 1], pages_b[p - 1])
    elif args.print:
        for p in diff_pages:
            s = per_page[str(p)]
            print(f"  p{p}: A行数={s['a_lines']} B行数={s['b_lines']} 增{s['added']}行 删{s['deleted']}行")


if __name__ == "__main__":
    main()

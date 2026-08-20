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

  # 5) 并集安全网：legacy(位置1:1 高召回) ∪ aligned(低噪声)，防对齐漏报真实差异页
  #    legacy_only_pages = 仅 legacy 标出（对齐漏报候选，必须逐页复核）
  python diff_pages.py docA.json docB.json --union --print

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


def legacy_diff_pages(pages_a: list[str], pages_b: list[str]) -> dict:
    """
    位置 1:1 比对（高召回，旧行为，默认模式）。
    逐页直接比较，凡文本不同即标为差异页——不依赖条款对齐，
    因此能捕获"新增整模块导致顺移错配""整条相似度高仅一行不同"等 --aligned 会漏的页。
    """
    n = min(len(pages_a), len(pages_b))
    diff_pages, per_page = [], {}
    for i in range(n):
        ta, tb = pages_a[i], pages_b[i]
        if ta == tb:
            continue
        p = i + 1
        diff_pages.append(p)
        per_page[str(p)] = page_diff_summary(ta, tb)
    return {
        "doc_a_pages": len(pages_a),
        "doc_b_pages": len(pages_b),
        "diff_page_count": len(diff_pages),
        "diff_pages": diff_pages,
        "per_page": per_page,
        "mode": "legacy",
    }


def aligned_diff_pages(doc_a_path: str, doc_b_path: str, pages_a: list[str], pages_b: list[str]) -> dict:
    """
    内容感知对齐模式：基于 align_blocks 的条款映射反推差异页。
    - 差异页 = only_a/only_b 条款页 ∪ 低置信匹配（sim<0.9）条款页 ∪ 页码不同且内容有变（sim<0.99）页
    - noise_pages = 页码偏移噪声（内容匹配仅页码不同，sim>=0.99），不进差异候选
    - tail_only_a/b = 页数不等时多出的尾部页（min 截断盲区补救）
    """
    import align_blocks
    from diff_structures import load as _load

    alignment = align_blocks.align_docs(_load(doc_a_path), _load(doc_b_path))
    mapping = alignment["clause_mapping"]
    diff_pages, sim_by_page = set(), {}
    for m in mapping:
        if m["status"] == "only_a":
            diff_pages.add(m["a"]["page"])
        elif m["status"] == "only_b":
            diff_pages.add(m["b"]["page"])
        else:  # matched
            sim = m["similarity"]
            for side, p in (("a", m["a"]["page"]), ("b", m["b"]["page"])):
                if sim < align_blocks.LOW_CONF:
                    diff_pages.add(p)                                   # 低置信 → 实质差异候选
                elif m["a"]["page"] != m["b"]["page"] and sim < align_blocks.NOISE_SIM:
                    diff_pages.add(p)                                   # 页码不同且内容有变（非纯偏移）
                sim_by_page.setdefault(p, []).append(sim)
    diff_pages = sorted(p for p in diff_pages if p <= max(len(pages_a), len(pages_b)))
    per_page = {}
    for p in diff_pages:
        if p <= len(pages_a) and p <= len(pages_b):
            per_page[str(p)] = {**page_diff_summary(pages_a[p - 1], pages_b[p - 1]),
                                "max_sim": round(max(sim_by_page.get(p, [0.0])), 3)}
    return {
        "doc_a_pages": len(pages_a), "doc_b_pages": len(pages_b),
        "diff_page_count": len(diff_pages),
        "diff_pages": diff_pages,
        "per_page": per_page,
        "noise_pages": alignment["noise_pages"],
        "tail_only_a": [p for p in range(len(pages_b) + 1, len(pages_a) + 1)],
        "tail_only_b": [p for p in range(len(pages_a) + 1, len(pages_b) + 1)],
        "alignment": alignment,
    }


def main():
    ap = argparse.ArgumentParser(description="技术协议 PDF 差异页初筛（基于 extract_pdf.py 输出）")
    ap.add_argument("docA", help="文档 A 的结构化 JSON（extract_pdf.py 输出）")
    ap.add_argument("docB", help="文档 B 的结构化 JSON（extract_pdf.py 输出）")
    ap.add_argument("--print", action="store_true", help="打印差异页摘要/行级 diff")
    ap.add_argument("--pages", help="页码范围，如 '40-46' / '40,42,45' / '42'（配合 --print/--full）")
    ap.add_argument("--full", action="store_true", help="打印完整文本对照（需配合 --pages）")
    ap.add_argument("--out", help="输出结构化 JSON 到文件（差异页 + 每页增删统计）")
    ap.add_argument("--aligned", action="store_true",
                    help="内容感知对齐模式（条款级加权 LCS 反推差异页，消除页码偏移 cascade；默认位置 1:1 兼容旧行为）")
    ap.add_argument("--union", action="store_true",
                    help="并集安全网：同时跑 legacy(位置 1:1 高召回) 与 aligned(低噪声)，取差异页并集。"
                         "legacy_only_pages 为仅 legacy 标出、对齐漏报的候选页，必须逐页复核以防漏报真实差异")
    args = ap.parse_args()

    pages_a = load_pages(args.docA)
    pages_b = load_pages(args.docB)
    n = min(len(pages_a), len(pages_b))
    if len(pages_a) != len(pages_b):
        print(f"[提示] 页码不一致：A={len(pages_a)} 页 B={len(pages_b)} 页（按前 {n} 页比对）", file=sys.stderr)

    if args.union:
        legacy_res = legacy_diff_pages(pages_a, pages_b)
        aligned_res = aligned_diff_pages(args.docA, args.docB, pages_a, pages_b)
        union_pages = sorted(set(legacy_res["diff_pages"]) | set(aligned_res["diff_pages"]))
        per_page = {}
        for p in union_pages:
            lp = legacy_res["per_page"].get(str(p))
            ap_ = aligned_res["per_page"].get(str(p))
            merged = {}
            if lp:
                merged.update(lp)
            if ap_:
                merged.update({k: v for k, v in ap_.items() if k != "max_sim"})
                merged["max_sim"] = ap_.get("max_sim")
            flags = []
            if lp:
                flags.append("legacy")
            if ap_:
                flags.append("aligned")
            merged["flagged_by"] = flags
            per_page[str(p)] = merged
        result = {
            "doc_a_pages": len(pages_a),
            "doc_b_pages": len(pages_b),
            "diff_page_count": len(union_pages),
            "diff_pages": union_pages,
            "per_page": per_page,
            "legacy_diff_page_count": legacy_res["diff_page_count"],
            "aligned_diff_page_count": aligned_res["diff_page_count"],
            "legacy_only_pages": sorted(set(legacy_res["diff_pages"]) - set(aligned_res["diff_pages"])),
            "aligned_only_pages": sorted(set(aligned_res["diff_pages"]) - set(legacy_res["diff_pages"])),
            "noise_pages": aligned_res.get("noise_pages"),
            "tail_only_a": aligned_res.get("tail_only_a"),
            "tail_only_b": aligned_res.get("tail_only_b"),
            "mode": "union",
        }
    elif args.aligned:
        result = aligned_diff_pages(args.docA, args.docB, pages_a, pages_b)
        result["mode"] = "aligned"
    else:
        result = legacy_diff_pages(pages_a, pages_b)

    print(f"文档 A: {len(pages_a)} 页, 文档 B: {len(pages_b)} 页, 模式: {result['mode']}")
    if result["mode"] == "union":
        print(f"并集差异页 {result['diff_page_count']} 页 = legacy {result['legacy_diff_page_count']} ∪ aligned {result['aligned_diff_page_count']}")
        if result.get("legacy_only_pages"):
            print(f"  [仅 legacy 标出（对齐漏报候选，必须逐页复核）]: {result['legacy_only_pages']}")
        if result.get("aligned_only_pages"):
            print(f"  [仅 aligned 标出]: {result['aligned_only_pages']}")
    print(f"有差异的页（共 {result['diff_page_count']} 页）: {result['diff_pages']}")
    if result.get("noise_pages"):
        print(f"[对齐] 页码偏移噪声页（内容匹配仅页码不同）: {list(result['noise_pages'].keys())}")
    if result.get("tail_only_a") or result.get("tail_only_b"):
        print(f"[对齐] 尾部独有页: 仅A={result.get('tail_only_a')} 仅B={result.get('tail_only_b')}（超出较短文档的页，需重点核查）")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        print(f"[OK] JSON 已写入: {args.out}", file=sys.stderr)

    if args.pages:
        for p in parse_pages(args.pages, max(len(pages_a), len(pages_b))):
            if p > n or p not in result["diff_pages"]:
                print(f"\n第 {p} 页: 无差异")
                continue
            if args.full:
                print(f"\n========== 第 {p} 页 完整文本对照 =====\n--- A ---\n{pages_a[p-1]}\n--- B ---\n{pages_b[p-1]}")
            else:
                print_page_diff(p, pages_a[p - 1], pages_b[p - 1])
    elif args.print:
        for p in result["diff_pages"]:
            if p > n:
                print(f"  p{p}: 尾部独有页（仅 {'B' if p > len(pages_a) else 'A'} 侧）")
                continue
            s = result["per_page"].get(str(p), {})
            extra = f"  最高条款相似度={s.get('max_sim')}" if "max_sim" in s else ""
            fb = s.get("flagged_by")
            fb_s = f"  标记来源={fb}" if fb else ""
            print(f"  p{p}: A行数={s.get('a_lines','?')} B行数={s.get('b_lines','?')} 增{s.get('added','?')}行 删{s.get('deleted','?')}行{extra}{fb_s}")


if __name__ == "__main__":
    main()

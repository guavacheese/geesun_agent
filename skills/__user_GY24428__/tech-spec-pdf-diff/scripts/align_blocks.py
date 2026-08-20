#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
align_blocks.py — 内容感知对齐（条款级加权 LCS + 章节聚合推导）。

解决机械脚本"位置 1:1"比对的核心缺陷（2026-08-20 spike 实测沉淀）：
  S1 跨页拉长：对齐单元=条款（块），块文本比对天然兼容页数不同
  S2 标题不同内容相同（>=90%）：对齐度量=内容相似度，标题仅辅助信号
  S3 章节内容拆散到多章：条款级 LCS 允许跨章节匹配（保持文档顺序），章节状态由条款映射聚合推导
  S4 位置 1:1 cascade：全局加权 LCS 归位，插入/删除后后续条款不再错位误报

算法：条款序列全局加权 LCS（dp + 回溯），匹配阈值 0.5，低置信线 0.9（低于此的匹配为"实质差异候选"，需 LLM 逐条判定）。
匹配判重用条款索引（非 (章,条款号) 键），避免同章同号条款撞车丢条目。

用法：
  python align_blocks.py docA.json docB.json --out alignment.json [--print]
输入：extract_pdf.py 输出（含 pages）
输出：alignment.json
  clause_mapping: [{a:{ch,no,page}, b:{ch,no,page}, similarity, status: matched|only_a|only_b}]
  chapter_agg:    [{chapter, title_a, title_b, page_a, page_b, status, clauses_total, mapped, only_a, low_conf, moved_out}]
  noise_pages:    {页码: 相似度}  —— 内容匹配但页码不同且相似度>=0.99 的页码偏移噪声页
  stats:          {clauses_a, clauses_b, matched, only_a, only_b, moved, noise_pages}
"""
import argparse
import difflib
import json
import sys

from diff_structures import build_structure

MATCH_THRESHOLD = 0.5   # 条款匹配阈值（真实差异条款 sim 0.5-0.89，0.75 会拆错）
LOW_CONF = 0.9          # 低置信线：低于此的匹配标记为实质差异候选
NOISE_SIM = 0.99        # 页码偏移噪声判定：相似度 >= 此值的页码偏移不算差异


def flatten_clauses(struct: list[dict]) -> list[dict]:
    """条款序列（对齐单元=条款）：[{"ch","ch_title","no","title","page","lines"}]"""
    out = []
    for ch in struct:
        for sec in ch.get("sections", []):
            out.append({"ch": ch["chapter"], "ch_title": ch["title"], "no": sec["no"],
                        "title": sec["title"], "page": sec["page"], "lines": sec.get("lines", [])})
    return out


def align_clauses(ca: list[dict], cb: list[dict]) -> list[dict]:
    """条款级全局加权 LCS（dp + 回溯）。支持插入/删除/跨章，保持文档顺序。
    返回映射表；匹配判重用条款索引，避免同章同号条款键撞车丢条目。"""
    n, m = len(ca), len(cb)
    sim = [[difflib.SequenceMatcher(None, ca[i]["lines"], cb[j]["lines"]).ratio()
            for j in range(m)] for i in range(n)]
    NEG = -1e9
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            use = dp[i - 1][j - 1] + sim[i - 1][j - 1] if sim[i - 1][j - 1] >= MATCH_THRESHOLD else NEG
            dp[i][j] = max(dp[i - 1][j], dp[i][j - 1], use)
    # 回溯（优先未匹配方向，否则必为匹配，保证不重复）
    pairs, i, j = [], n, m
    while i > 0 and j > 0:
        if dp[i][j] == dp[i - 1][j]:
            i -= 1
        elif dp[i][j] == dp[i][j - 1]:
            j -= 1
        else:
            pairs.append((i - 1, j - 1, sim[i - 1][j - 1]))
            i, j = i - 1, j - 1
    pairs.reverse()
    mapping = []
    matched_a_idx, matched_b_idx = set(), set()
    for ai, bj, r in pairs:
        a, b = ca[ai], cb[bj]
        status = "matched" if r >= MATCH_THRESHOLD else "only_a"
        mapping.append({"a": {"ch": a["ch"], "no": a["no"], "page": a["page"]},
                        "b": {"ch": b["ch"], "no": b["no"], "page": b["page"]} if status == "matched" else None,
                        "similarity": round(r, 3), "status": status})
        if status == "matched":
            matched_a_idx.add(ai)
            matched_b_idx.add(bj)
    for ai, a in enumerate(ca):
        if ai not in matched_a_idx:
            mapping.append({"a": {"ch": a["ch"], "no": a["no"], "page": a["page"]},
                            "b": None, "similarity": 0.0, "status": "only_a"})
    for bj, b in enumerate(cb):
        if bj not in matched_b_idx:
            mapping.append({"a": None, "b": {"ch": b["ch"], "no": b["no"], "page": b["page"]},
                            "similarity": 0.0, "status": "only_b"})
    return mapping


def aggregate_chapters(sa: list[dict], sb: list[dict], mapping: list[dict]) -> list[dict]:
    """章节状态由条款映射聚合推导（不做独立章节比对）：
    same / title_diff / title_diff(疑不同章) / only_a / only_b；moved_out=条款挪章。"""
    rows = []
    for ch in sa:
        secs = ch.get("sections", [])
        mapped = [m for m in mapping if m["status"] == "matched" and m["a"]["ch"] == ch["chapter"]]
        only_a = [m for m in mapping if m["status"] == "only_a" and m["a"]["ch"] == ch["chapter"]]
        low_conf = [m for m in mapped if m["similarity"] < LOW_CONF]
        moved_out = []
        for m in mapped:
            if m["a"]["ch"] != m["b"]["ch"]:
                moved_out.append({"no": m["a"]["no"], "to": m["b"]["ch"], "note": "挪章"})
        b_ch = next((c for c in sb if c["chapter"] == ch["chapter"]), None)
        title_same = b_ch is not None and b_ch["title"].strip() == ch["title"].strip()
        ratio = difflib.SequenceMatcher(None, ch["title"], b_ch["title"]).ratio() if b_ch else 0.0
        status = "same"
        if b_ch is None:
            status = "only_a"
        elif not title_same:
            all_mapped = len(secs) > 0 and len(mapped) == len(secs)
            status = "title_diff" if (ratio >= 0.5 or all_mapped) else "title_diff(疑不同章)"
        rows.append({
            "chapter": ch["chapter"], "title_a": ch["title"],
            "title_b": b_ch["title"] if b_ch else None,
            "page_a": ch["start_page"], "page_b": b_ch["start_page"] if b_ch else None,
            "status": status,
            "clauses_total": len(secs),
            "mapped": len(mapped), "only_a": len(only_a), "low_conf": len(low_conf),
            "moved_out": moved_out,
        })
    for b_ch in sb:
        if not any(r["chapter"] == b_ch["chapter"] for r in rows):
            rows.append({"chapter": b_ch["chapter"], "title_a": None, "title_b": b_ch["title"],
                         "page_a": None, "page_b": b_ch["start_page"], "status": "only_b",
                         "clauses_total": len(b_ch.get("sections", [])), "mapped": 0, "only_a": 0,
                         "low_conf": 0, "moved_out": []})
    return rows


def collect_noise_pages(mapping: list[dict]) -> dict:
    """页码偏移噪声页：条款内容匹配（相似度>=NOISE_SIM）但页码不同 → 顺延/跨页非差异。"""
    noise = {}
    for m in mapping:
        if m["status"] == "matched" and m["similarity"] >= NOISE_SIM and m["a"]["page"] != m["b"]["page"]:
            noise[str(m["a"]["page"])] = max(noise.get(str(m["a"]["page"]), 0.0), m["similarity"])
            noise[str(m["b"]["page"])] = max(noise.get(str(m["b"]["page"]), 0.0), m["similarity"])
    return dict(sorted(noise.items(), key=lambda kv: int(kv[0])))


def align_docs(doc_a: dict, doc_b: dict) -> dict:
    """主入口：从两份 extract_pdf 输出构建对齐结果。"""
    sa, sb = build_structure(doc_a), build_structure(doc_b)
    ca, cb = flatten_clauses(sa), flatten_clauses(sb)
    mapping = align_clauses(ca, cb)
    chapter_agg = aggregate_chapters(sa, sb, mapping)
    noise_pages = collect_noise_pages(mapping)
    stats = {
        "clauses_a": len(ca), "clauses_b": len(cb),
        "matched": sum(1 for m in mapping if m["status"] == "matched"),
        "only_a": sum(1 for m in mapping if m["status"] == "only_a"),
        "only_b": sum(1 for m in mapping if m["status"] == "only_b"),
        "moved": sum(1 for r in chapter_agg for m in r["moved_out"]),
        "low_conf": sum(1 for m in mapping if m["status"] == "matched" and m["similarity"] < LOW_CONF),
        "noise_pages": len(noise_pages),
    }
    return {"clause_mapping": mapping, "chapter_agg": chapter_agg,
            "noise_pages": noise_pages, "stats": stats}


def main():
    ap = argparse.ArgumentParser(description="内容感知对齐（条款级加权 LCS + 章节聚合）")
    ap.add_argument("docA", help="文档 A 结构化 JSON（extract_pdf.py 输出）")
    ap.add_argument("docB", help="文档 B 结构化 JSON")
    ap.add_argument("--out", help="写入对齐结果 JSON")
    ap.add_argument("--print", action="store_true", help="打印章节聚合表")
    args = ap.parse_args()

    def _load(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    result = align_docs(_load(args.docA), _load(args.docB))
    s = result["stats"]
    print(f"条款 {s['clauses_a']} vs {s['clauses_b']} → matched={s['matched']} only_a={s['only_a']} only_b={s['only_b']} "
          f"挪章={s['moved']} 低置信候选={s['low_conf']} 噪声页={s['noise_pages']}")
    if args.print:
        print("\n=== 章节聚合（由条款映射推导）===")
        for r in result["chapter_agg"]:
            mv = ",".join(f"{m['no']}→{m['to']}" for m in r["moved_out"]) or "-"
            print(f"  {r['chapter']}、{r['status']:<16} A页{r['page_a']} B页{r['page_b']} "
                  f"条款{r['clauses_total']}(映射{r['mapped']}/低置信{r['low_conf']}/仅A{r['only_a']}) 挪章[{mv}]")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        sys.stderr.write(f"[OK] 对齐结果已写入: {args.out}\n")


if __name__ == "__main__":
    main()

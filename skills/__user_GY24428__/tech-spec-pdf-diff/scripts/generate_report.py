#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_report.py — 从 Agent 产出的"差异清单"生成比对报告（Markdown 或 HTML）。

前置：
  Agent 基于 extract_pdf.py 的完整分页文本 + diff_structures.py 的结构对齐结果，
  完成逐章语义比对后，把"差异条目"整理为如下 JSON 结构传入本脚本：

{
  "doc_a": {"name": "...", "file": "...", "pages": 31, "version": "..."},
  "doc_b": {"name": "...", "file": "...", "pages": 32, "version": "..."},
  "summary": "总体结论文本...",
  "sections": [
    {
      "title": "修订履历差异",
      "anchor": "rev",
      "type": "callout",            // callout | table | module | clauses
      "content": { ... }            // 按 type 不同结构见下
    },
    ...
  ],
  "diff_list": [                    // 差异汇总表行
    {"loc": "放卷模块 10)", "a": "红色激光", "b": "激光测距传感器", "kind": "内容差异"}
  ],
  "no_diff": ["四、设备开发通用要求", "五、设备验收要求", ...]
}

各 section 的 content 结构：
  - callout: {"text": "...", "level": "diff"|"new"|"plain", "list": ["..."]}
  - table:   {"header": ["列1","列2","列3"], "rows": [["a","b","c"], ...],
              "highlight_cols": [2]}        // 高亮列（index）
  - module:  {"old": "模块清单A", "new": "模块清单B", "note": "..."}
  - clauses: {"header": ["条款号","文档A","文档B","差异类型"], "rows": [...],
              "values": {"A": "阴极 010153", "B": "阳极 010154"}}

用法：
  python generate_report.py diff.json --format html --out report.html
  python generate_report.py diff.json --format md --out report.md
"""

import argparse
import html
import json
import sys


def _normalize_doc(doc, data, key):
    """归一化 doc_a / doc_b 字段，兼容两种 JSON 形状：
    - 010153 形态：已是含 name/pages/version 的字典，原样返回；
    - 010154 形态：字符串文件名（例如 "16-01-06-010154_C.pdf"），
      补齐为字典，pages/version 回退到 {key}_pages / {key}_version（若无则留空）。
    其它类型（理论上不会出现）兜底为字符串名。
    """
    if isinstance(doc, str):
        return {
            "name": doc,
            "pages": data.get(f"{key}_pages", "") or "",
            "version": data.get(f"{key}_version", "") or "",
        }
    if isinstance(doc, dict):
        return doc
    return {"name": str(doc), "pages": "", "version": ""}


# ---------- Markdown 生成 ----------

def render_md(data: dict) -> str:
    L = []
    da, db = data["doc_a"], data["doc_b"]
    def _g(d, k):
        return d.get(k, d.get({"version": "版本", "pages": "页数"}.get(k, k), ""))
    L.append("# 技术协议差异比对报告\n")
    L.append("> 文档 A：**%s**（%s，%s 页）" % (
        da["name"], _g(da, "version"), _g(da, "pages")))
    L.append("> 文档 B：**%s**（%s，%s 页）\n" % (
        db["name"], _g(db, "version"), _g(db, "pages")))
    if data.get("summary"):
        L.append("## 总体结论\n")
        L.append(data["summary"] + "\n")

    for sec in data.get("sections", []):
        L.append(f"\n## {sec['title']}\n")
        c = sec.get("content", {})
        t = sec.get("type", "callout")
        if t == "callout":
            if c.get("text"):
                L.append(f"> **{c['text']}**\n")
            for item in c.get("list", []):
                L.append(f"- {item}\n")
        elif t == "table":
            hdr = c.get("header", [])
            rows = c.get("rows", [])
            hl = set(c.get("highlight_cols", []))
            L.append("| " + " | ".join(hdr) + " |")
            L.append("|" + "---|" * len(hdr))
            for r in rows:
                cells = []
                for i, v in enumerate(r):
                    v = str(v)
                    if i in hl:
                        v = f"**{v}**"
                    cells.append(v)
                L.append("| " + " | ".join(cells) + " |")
            L.append("")
        elif t == "module":
            L.append("| 文档 A | 文档 B |")
            L.append("|---|---|")
            L.append(f"| {c.get('old','')} | {c.get('new','')} |")
            L.append("")
            if c.get("note"):
                L.append(f"> {c['note']}\n")
        elif t == "clauses":
            vA, vB = c.get("values", {}).get("A", "文档 A"), c.get("values", {}).get("B", "文档 B")
            L.append(f"**{vA} vs {vB}**\n")
            hdr = c.get("header", ["条款号", "文档 A", "文档 B", "差异类型"])
            rows = c.get("rows", [])
            L.append("| " + " | ".join(hdr) + " |")
            L.append("|" + "---|" * len(hdr))
            for r in rows:
                L.append("| " + " | ".join(str(x) for x in r) + " |")
            L.append("")
    if data.get("diff_list"):
        L.append("\n## 差异汇总表\n")
        L.append("| # | 位置 | 文档 A | 文档 B | 差异类型 |")
        L.append("|---|---|---|---|---|")
        for i, d in enumerate(data["diff_list"], 1):
            L.append(f"| {i} | {d.get('loc','')} | {d.get('a','')} | {d.get('b','')} | {d.get('kind','')} |")
        L.append("")
    if data.get("no_diff"):
        L.append("\n## 无差异章节（已排除）\n")
        for ch in data["no_diff"]:
            L.append(f"- ✓ {ch}")
        L.append("")
    return "\n".join(L)


# ---------- HTML 生成 ----------

CSS = """
:root{--bg:#f6f8fa;--card:#fff;--border:#d0d7de;--text:#1f2328;--muted:#57606a;
--red:#c62828;--red-bg:#fdecec;--green:#1a7f37;--green-bg:#e6f4ea;--blue:#0969da;
--blue-bg:#eef4fd;--amber-bg:#fff8e6;--header-bg:#24292f;--header-text:#fff}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft YaHei","PingFang SC","Segoe UI",sans-serif;background:var(--bg);
color:var(--text);line-height:1.7;font-size:14px}
.container{max-width:1080px;margin:0 auto;padding:24px 20px 60px}
.page-header{background:var(--header-bg);color:var(--header-text);padding:26px 0;margin-bottom:24px}
.page-header .container{padding-bottom:0}
.page-header h1{font-size:23px;letter-spacing:1px}
.page-header .sub{margin-top:6px;font-size:13px;opacity:.85}
h2.sec{font-size:18px;font-weight:700;margin:32px 0 12px;padding-left:12px;
border-left:5px solid var(--blue);line-height:1.4}
h3.sub{font-size:15px;font-weight:700;margin:22px 0 10px;padding:8px 12px;
background:var(--blue-bg);border-radius:6px;color:var(--blue)}
.doc-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:16px 0}
.doc-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px 18px}
.doc-card .doc-name{font-size:15px;font-weight:700;margin-bottom:8px}
.doc-card td{padding:3px 6px;border-bottom:1px dashed #e5e9ef;font-size:13px}
.doc-card td:first-child{color:var(--muted);width:70px}
.callout{border-radius:8px;padding:13px 17px;margin:10px 0 16px;font-size:13.5px}
.callout.diff{background:var(--red-bg);border:1px solid #f5c2c2}
.callout.new{background:var(--green-bg);border:1px solid #b7e0c3}
.callout.plain{background:var(--amber-bg);border:1px solid #f0dc9e}
.diff-table-wrap{overflow-x:auto;margin:10px 0 16px;border:1px solid var(--border);
border-radius:8px;background:var(--card)}
table.diff{width:100%;border-collapse:collapse;font-size:13px;min-width:600px}
table.diff th{background:#f0f3f6;text-align:left;padding:9px 12px;border-bottom:2px solid var(--border);white-space:nowrap}
table.diff td{padding:9px 12px;border-bottom:1px solid #eef1f4;vertical-align:top}
table.diff tr:last-child td{border-bottom:none}
table.diff td.rowhead{font-weight:700;white-space:nowrap;background:#fafbfc;width:120px}
.val-old{background:var(--red-bg);color:var(--red);border-radius:4px;padding:1px 6px;font-weight:600}
.val-new{background:var(--green-bg);color:var(--green);border-radius:4px;padding:1px 6px;font-weight:600}
.nodiff{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:10px 0}
.nodiff-item{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:11px 15px;font-size:13px}
.nodiff-item .ch{font-weight:700;color:var(--green)}
.footer{margin-top:36px;padding-top:14px;border-top:1px solid var(--border);color:var(--muted);font-size:12.5px}
@media(max-width:768px){.doc-grid,.nodiff{grid-template-columns:1fr}}
"""

def esc(s):
    return html.escape(str(s))

def render_html(data: dict) -> str:
    P = []
    P.append("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">")
    P.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">")
    P.append(f"<title>{esc(data.get('title','技术协议差异比对报告'))}</title>")
    P.append(f"<style>{CSS}</style></head><body>")
    P.append("<div class=\"page-header\"><div class=\"container\">")
    P.append(f"<h1>{esc(data.get('title','技术协议差异比对报告'))}</h1>")
    P.append("<div class=\"sub\">仅列出有差异的章节 / 条款 / 表格行 · 无差异内容已排除</div>")
    P.append("</div></div><div class=\"container\">")

    # 文档卡片
    P.append("<h2 class=\"sec\">比对对象</h2><div class=\"doc-grid\">")
    for key in ("doc_a", "doc_b"):
        d = data[key]
        P.append("<div class=\"doc-card\"><div class=\"doc-name\">%s</div><table>"
                 % esc(d.get("name", key)))
        for k, v in d.items():
            if k == "name":
                continue
            P.append(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>")
        P.append("</table></div>")
    P.append("</div>")

    if data.get("summary"):
        P.append("<h2 class=\"sec\">总体结论</h2>")
        P.append(f"<div class=\"callout plain\">{esc(data['summary'])}</div>")

    for sec in data.get("sections", []):
        P.append(f"<h2 class=\"sec\">{esc(sec['title'])}</h2>")
        c = sec.get("content", {})
        t = sec.get("type", "callout")
        if t == "callout":
            lvl = c.get("level", "plain")
            P.append(f"<div class=\"callout {lvl}\"><b>{esc(c.get('text',''))}</b>")
            for item in c.get("list", []):
                P.append(f"<div>• {esc(item)}</div>")
            P.append("</div>")
        elif t == "table":
            P.append("<div class=\"diff-table-wrap\"><table class=\"diff\"><tr>")
            for h in c.get("header", []):
                P.append(f"<th>{esc(h)}</th>")
            P.append("</tr>")
            hl = set(c.get("highlight_cols", []))
            for r in c.get("rows", []):
                P.append("<tr>")
                for i, v in enumerate(r):
                    cls = ""
                    if i in hl:
                        cls = " class=\"val-new\""
                    P.append(f"<td{cls}>{esc(v)}</td>")
                P.append("</tr>")
            P.append("</table></div>")
        elif t == "module":
            P.append("<div class=\"diff-table-wrap\"><table class=\"diff\">")
            P.append("<tr><th>文档 A</th><th>文档 B</th></tr>")
            P.append(f"<tr><td>{esc(c.get('old',''))}</td><td class=\"val-new\">{esc(c.get('new',''))}</td></tr>")
            P.append("</table></div>")
            if c.get("note"):
                P.append(f"<div class=\"callout diff\"><b>{esc(c['note'])}</b></div>")
        elif t == "clauses":
            vA = c.get("values", {}).get("A", "文档 A")
            vB = c.get("values", {}).get("B", "文档 B")
            P.append(f"<div class=\"callout plain\"><b>{esc(vA)} vs {esc(vB)}</b></div>")
            P.append("<div class=\"diff-table-wrap\"><table class=\"diff\"><tr>")
            for h in c.get("header", ["条款号", "文档 A", "文档 B", "差异类型"]):
                P.append(f"<th>{esc(h)}</th>")
            P.append("</tr>")
            for r in c.get("rows", []):
                P.append("<tr>")
                for i, v in enumerate(r):
                    cls = " class=\"rowhead\"" if i == 0 else ""
                    P.append(f"<td{cls}>{esc(v)}</td>")
                P.append("</tr>")
            P.append("</table></div>")

    if data.get("diff_list"):
        P.append("<h2 class=\"sec\">差异汇总表</h2>")
        P.append("<div class=\"diff-table-wrap\"><table class=\"diff\">")
        P.append("<tr><th>#</th><th>位置</th><th>文档 A</th><th>文档 B</th><th>差异类型</th></tr>")
        for i, d in enumerate(data["diff_list"], 1):
            P.append(f"<tr><td>{i}</td><td>{esc(d.get('loc',''))}</td>"
                     f"<td>{esc(d.get('a',''))}</td><td>{esc(d.get('b',''))}</td>"
                     f"<td>{esc(d.get('kind',''))}</td></tr>")
        P.append("</table></div>")

    if data.get("no_diff"):
        P.append("<h2 class=\"sec\">无差异章节（已排除）</h2><div class=\"nodiff\">")
        for ch in data["no_diff"]:
            P.append(f"<div class=\"nodiff-item\"><span class=\"ch\">✓ {esc(ch)}</span></div>")
        P.append("</div>")

    P.append("<div class=\"footer\">报告由 tech-spec-pdf-diff skill 生成。"
             "页眉页脚（公司抬头/标识码/页码/机密标记/模板版本号）已剔除；"
             "跨页表格已合并到所属章节。</div>")
    P.append("</div></body></html>")
    return "\n".join(P)


def main():
    ap = argparse.ArgumentParser(description="从差异清单生成比对报告")
    ap.add_argument("diff_json", help="差异清单 JSON（结构见脚本头注释）")
    ap.add_argument("--format", choices=["md", "html"], default="md")
    ap.add_argument("--out", required=True, help="输出文件路径")
    args = ap.parse_args()

    with open(args.diff_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 兼容两种 JSON 形状：doc_a/doc_b 可能是字符串文件名（010154 形态）
    # 或含 name/pages/version 的字典（010153 形态）。渲染前统一归一化，
    # 避免字符串形状下 da["name"] / d.items() 抛 TypeError 导致报告生成失败。
    data["doc_a"] = _normalize_doc(data.get("doc_a"), data, "doc_a")
    data["doc_b"] = _normalize_doc(data.get("doc_b"), data, "doc_b")

    content = render_html(data) if args.format == "html" else render_md(data)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(content)
    sys.stderr.write(f"[OK] 报告已生成: {args.out}（{len(content)} 字符）\n")


if __name__ == "__main__":
    main()

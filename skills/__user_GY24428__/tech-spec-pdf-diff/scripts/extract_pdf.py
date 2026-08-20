#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_pdf.py — 从技术协议 PDF（可能含 DLP 加密头）中提取干净的分页文本。

通用能力：
  1. 自动检测 DLP 加密头（%TSD-Header-###%）
     - 若文件为 DLP 加密，且未提供 --base64-file，则提示用 decrypt MCP 解密
     - 若提供 --base64-file（decrypt_file_to_base64 MCP 输出的结果文件），
       自动从 JSON 包裹中解析 base64 并在内存中解密，绝不落盘明文
  2. 提取每页文本（pdfplumber.extract_text）
  3. 剔除页眉页脚：
     - 默认剔除常见页眉模式（文件名称 / 公司抬头 / 标识码 / 版本号）
     - 默认剔除常见页脚模式（机密标记 / 第 X 页 共 N 页 / 模板版本号）
     - 可用 --keep-header-footer 关闭剔除
  4. 输出：
     - 默认输出 JSON 到 stdout：{"pages": ["page1", ...], "page_count": N}
     - 可用 --out <file> 写文件（注意 DLP 环境可能加密写入的小文件）
     - 可用 --print 直接打印纯文本预览（便于人工/Agent 快速浏览）

依赖：pdfplumber（安装：pip install pdfplumber）
Python：>=3.10

用法示例：
  # 明文 PDF
  python extract_pdf.py input.pdf --print

  # DLP 加密 PDF + decrypt MCP 输出文件
  python extract_pdf.py encrypted.pdf --base64-file decrypt_result.txt --print

  # 输出 JSON 供后续比对
  python extract_pdf.py input.pdf --out structure.json
"""

import argparse
import base64
import io
import json
import re
import sys


# ---------- DLP 检测 ----------

TSD_HEADER = b"%TSD-Header-###%"

def is_dlp_encrypted(path: str) -> bool:
    """检测文件是否带 DLP 加密头。"""
    try:
        with open(path, "rb") as f:
            head = f.read(len(TSD_HEADER))
        return head == TSD_HEADER
    except Exception:
        return False


def load_pdf_bytes(path: str, base64_file: str | None):
    """
    加载 PDF 字节：
      - 非加密：直接读文件
      - 加密 + base64_file：从 decrypt MCP 结果文件解析 base64（内存解码）
    返回 bytes。
    """
    if not is_dlp_encrypted(path):
        with open(path, "rb") as f:
            return f.read()
    # DLP 加密
    if not base64_file:
        sys.stderr.write(
            "[ERROR] 文件带 DLP 加密头（%TSD-Header-###%）。\n"
            "  请先用 decrypt MCP 的 decrypt_file_to_base64 解密，\n"
            "  再把 MCP 返回的结果文件路径通过 --base64-file 传入。\n"
            "  提示：MCP 的 file_path 是 server 运行环境的路径（与调用它的 agent 无关）。\n"
            "  本机实测 /mnt/e/v1/xxx.pdf 可用（server 跑在 Linux 容器/WSL）；\n"
            "  若 ENOENT，请依次探测 /e/...、E:/...、E:\\\\... 或询问 server 运行环境。\n"
        )
        sys.exit(2)
    with open(base64_file, "r", encoding="utf-8") as f:
        raw = f.read()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        sys.stderr.write("[ERROR] base64 文件不是 JSON 格式（decrypt MCP 输出应为 JSON）。\n")
        sys.exit(2)
    obj = json.loads(raw[start : end + 1])
    if not obj.get("success"):
        sys.stderr.write(f"[ERROR] decrypt MCP 解密失败: {obj.get('error')}\n")
        sys.exit(2)
    return base64.b64decode(obj["data"])


# ---------- 页眉页脚剔除 ----------

# 常见页眉模式：文件名称 / 公司抬头 / 标识码 / 版本号 等
HEADER_PATTERNS = [
    r"^文件名称[:：].*$",
    r"^.*Contemporary Amperex Technology.*标识码.*版本号.*$",
    r"^.*标识码[:：]\s*\S+\s*版本号[:：]\S+.*$",
    r"^文件编号[:：].*$",
    r"^Document No\.?[:：].*$",
    r"^文件版本[:：].*$",
]
# 常见页脚模式：机密标记 / 页码 / 模板版本号
FOOTER_PATTERNS = [
    r"^Secret\s+机密.*第\s*\d+\s*页\s*/\s*共\s*\d+\s*页.*$",
    r"^机密.*$",
    r"^第\s*\d+\s*页\s*/\s*共\s*\d+\s*页.*$",
    r"^模板版本号[:：].*$",
    r"^Confidential.*$",
]

def strip_header_footer(page_text: str) -> str:
    """剔除页眉页脚行。"""
    lines = page_text.split("\n")
    cleaned = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if any(re.match(p, s) for p in HEADER_PATTERNS):
            continue
        if any(re.match(p, s) for p in FOOTER_PATTERNS):
            continue
        cleaned.append(ln)
    return "\n".join(cleaned)


# ---------- 章节切分（辅助 Agent 比对） ----------

CHAPTER_RE = re.compile(r"^([一二三四五六七八九十]+)、\s*(.+?)\s*$")
SECTION_RE = re.compile(r"^(\d+)[\.．、]\s*(.+?)\s*$")
SUBSECTION_RE = re.compile(r"^(\d+)\)\s*(.+?)\s*$")

def detect_chapters(pages_text: list[str]) -> list[dict]:
    """
    遍历所有页文本，识别章节标题及其所在页。
    返回 [{"chapter": "一", "title": "...", "page": 3, "section": "..."}]
    章节标题后紧跟的"内容/描述/流程图"由 Agent 按文本上下文提取。
    """
    out = []
    for idx, text in enumerate(pages_text):
        for line in text.split("\n"):
            s = line.strip()
            if not s:
                continue
            m = CHAPTER_RE.match(s)
            if m:
                out.append({"chapter": m.group(1), "title": m.group(2), "page": idx + 1})
    return out


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser(description="技术协议 PDF 文本提取（支持 DLP 加密）")
    ap.add_argument("pdf", help="PDF 文件路径")
    ap.add_argument("--base64-file", help="decrypt MCP 输出结果文件（DLP 加密时必填）")
    ap.add_argument("--out", help="输出 JSON 到指定文件（默认输出到 stdout）")
    ap.add_argument("--print", action="store_true", help="以纯文本打印各页内容（便于浏览）")
    ap.add_argument("--keep-header-footer", action="store_true", help="不剔除页眉页脚")
    ap.add_argument("--chapters", action="store_true", help="额外输出检测到的章节结构")
    args = ap.parse_args()

    pdf_bytes = load_pdf_bytes(args.pdf, args.base64_file)

    import pdfplumber
    pages_text = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if not args.keep_header_footer:
                text = strip_header_footer(text)
            pages_text.append(text)

    result = {
        "source": args.pdf,
        "page_count": len(pages_text),
        "dlp_encrypted": is_dlp_encrypted(args.pdf),
        "pages": pages_text,
    }
    if args.chapters:
        result["chapters"] = detect_chapters(pages_text)

    if args.print:
        for i, t in enumerate(pages_text, 1):
            print(f"\n----- 第 {i} 页 -----")
            print(t)
        return

    data = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(data)
        sys.stderr.write(f"[OK] JSON 已写入: {args.out}\n")
    else:
        sys.stdout.write(data)


if __name__ == "__main__":
    main()

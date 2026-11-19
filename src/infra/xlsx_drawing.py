"""xlsx 包形态归一化（exceljs 预览兼容护栏，2026-11-19）。

背景（生产事故 会话 fce2eb9a「1098 IO表-1212_清晰后v1.xlsx」）：
openpyxl 另存的 xlsx 有三处「语义合法但 exceljs 4.4.0（上游已停更）读挂」的
包形态偏差，任一命中即整个文件预览炸穿（前端 SpreadsheetViewer 已另加
SheetJS 兜底，本模块是 serve 侧治本——挂在 ``GET /files/`` 下发口，一处覆盖
沙箱拉回/收尾兜底/MCP download_from_sandbox 全部写盘路径**及存量旧文件**）：

1. **drawing 默认命名空间**：``xl/drawings/drawingN.xml`` 按 ElementTree 默认
   命名空间序列化（根 ``<wsDr xmlns="...spreadsheetDrawing">``、锚点全部无
   ``xdr:`` 前缀），而 exceljs 把标签名硬编码为 ``xdr:wsDr``/``xdr:twoCellAnchor``
   （drawing-xform.js:19-20），解析结果整体 undefined 落进 model.drawings，
   reconcile ``xlsx.js:100`` 抛 ``...undefined (reading 'anchors')``。
   → 归一为 ``xdr:`` 前缀形态（Excel/WPS/ECMA-376 标准写法，语义等价）。
2. **worksheet rels 绝对路径 Target**：openpyxl 写 ``Target="/xl/comments/…"``，
   exceljs 的 comments/vmlDrawings 索引 key 是相对形态 ``../comments/…``
   （xlsx.js:160/218），查不到 → reconcile ``worksheet-xform.js:453/456`` 抛
   ``...undefined (reading 'comments')``（455/456 同无守卫）。
   → ``Target="/xl/x"`` 重写为 ``Target="../x"``（OOXML 两种形态均合法；
   外部链接 Target 带 TargetMode="External" 且是 URL，不以 /xl/ 开头，天然不碰）。
3. **VML 文件名**：openpyxl 的批注 VML 叫 ``commentsDrawingN.vml``，exceljs 的
   收纳正则只认 ``vmlDrawing\\d+[.]vml``（xlsx.js:401），不收 → 同上 456 查挂。
   → 改名 ``vmlDrawingN.vml`` 并同步 rels Target（VML 根标签 ``<xml>`` 本就
   对得上；shape 子标签的 ns 前缀差异只会让 exceljs 拿到空列表，不崩）。

已是标准形态的（Excel/WPS 原件）三处检测都不命中，原样放行（幂等）。

⚠ 已知不可治（有意不碰）：**含批注的文件仍会炸在 'comments'**。exceljs 的
comments 收纳正则是 ``xl/(comments\\d+)[.]xml``（xlsx.js:386，只认
``xl/commentsN.xml`` 平铺路径），而 openpyxl 与 **Excel 原生**都写标准路径
``xl/comments/comment1.xml`` → 永不入册 → rels 查 undefined 炸。即 exceljs
对**任何**含批注的 xlsx 都炸（Excel 原件同炸，上游停更永不修）。救它需要
搬迁部件 + 同步 Content_Types，改坏一次=用户 Excel 打不开真损——预览职责
已由前端 SheetJS 兜底承担，本模块不越这条线。
"""

from __future__ import annotations

import io
import logging
import re
import zipfile

logger = logging.getLogger(__name__)

_XDR_NS = b"http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"

# 默认命名空间形态的根标签（openpyxl 写出）；前缀形态 <xdr:wsDr 不会命中
_UNPREFIXED_WS_DR = re.compile(rb"<wsDr[\s/>]")

# zip 内 drawing 部件名（与 exceljs 的收纳正则 xl/drawings/([a-zA-Z0-9]+)[.]xml 同范围，
# 放宽为任意名，chart 的 drawing 也一并核查——它们通常是前缀形态，会被跳过）
_DRAWING_NAME = re.compile(r"^xl/drawings/[^/]+\.xml$")

# 无命名空间前缀的标签名（<x 与 </x 两种；带冒号的 <a: 等因 lookahead 不吃 ':' 而不匹配）
_UNPREFIXED_TAG = re.compile(rb"(</?)([A-Za-z_][A-Za-z0-9_.\-]*)(?=[\s/>])")

# openpyxl 的批注 VML 名 → exceljs 收纳正则认识的名字
_VML_RENAME = re.compile(r"^xl/drawings/commentsDrawing(\d+)\.vml$")

# worksheet 级 rels（comments/vmlDrawing/drawing/hyperlink 都挂这里）
_WS_RELS_NAME = re.compile(r"^xl/worksheets/_rels/[^/]+\.rels$")

# 绝对路径 Target（相对 base = xl/worksheets/）；外部链接 Target 是 URL 不匹配
_ABS_TARGET = re.compile(rb'Target="/xl/([^"]+)"')
# VML Target 两形态都要先于通用规则改（openpyxl 实际写绝对形态）
_VML_TARGET_ABS = re.compile(rb'Target="/xl/drawings/commentsDrawing(\d+)\.vml"')
_VML_TARGET_REL = re.compile(rb'Target="\.\./drawings/commentsDrawing(\d+)\.vml"')


def normalize_xlsx_drawing_bytes(data: bytes) -> bytes:
    """把 xlsx 字节流里的 default-namespace drawing 归一为 ``xdr:`` 前缀形态。

    契约（调用方据此打日志/判变化）：
      - **返回同一对象** = 未做修改（非 zip / 无 drawing / 已是前缀形态 / 解析失败）；
      - 返回新 bytes = 已归一化。

    永不抛异常：任何一步失败都原样返回，产物落盘不能因兼容护栏而失败。
    """
    try:
        # 注意不能用「raw bytes 里搜命名空间 URI」做快路径——zip 条目是 deflate
        # 压缩的，URI 明文不存在；判据只能解包后看 drawing 部件。
        src = zipfile.ZipFile(io.BytesIO(data))
    except Exception:  # noqa: BLE001 — 非 zip / 截断：不是本函数的事
        return data

    try:
        names = src.namelist()

        # ① drawing 默认命名空间 → xdr: 前缀
        patched: dict[str, bytes] = {}
        for name in names:
            if not _DRAWING_NAME.match(name):
                continue
            xml = src.read(name)
            if not _UNPREFIXED_WS_DR.search(xml[:4096]):
                continue  # 已是前缀形态或无 wsDr 根：不动
            # 根元素默认命名空间声明 → xdr 前缀声明（双/单引号两形态）
            new = xml.replace(
                b'xmlns="%s"' % _XDR_NS, b'xmlns:xdr="%s"' % _XDR_NS, 1
            )
            if new == xml:
                new = xml.replace(
                    b"xmlns='%s'" % _XDR_NS, b"xmlns:xdr='%s'" % _XDR_NS, 1
                )
            if new == xml:
                logger.warning("[XLSX_NORM] %s 默认命名空间声明未识别，跳过", name)
                continue
            # 所有无前缀标签补 xdr:（默认命名空间作用域覆盖整个子树；
            # 已带前缀的 <a:/<r:/<c: 等不匹配 lookahead，天然不动）
            new = _UNPREFIXED_TAG.sub(lambda m: m.group(1) + b"xdr:" + m.group(2), new)
            patched[name] = new

        # ② VML 改名 commentsDrawingN.vml → vmlDrawingN.vml
        renames: dict[str, str] = {}
        for name in names:
            m = _VML_RENAME.match(name)
            if m:
                renames[name] = "xl/drawings/vmlDrawing%s.vml" % m.group(1)

        # ③ worksheet rels：绝对 Target → 相对；VML 改名同步落到 Target
        for name in names:
            if not _WS_RELS_NAME.match(name):
                continue
            rels = src.read(name)
            new = rels
            if renames:
                to_vml = lambda m: b'Target="../drawings/vmlDrawing%s.vml"' % m.group(1)
                new = _VML_TARGET_ABS.sub(to_vml, new)
                new = _VML_TARGET_REL.sub(to_vml, new)
            new = _ABS_TARGET.sub(rb'Target="../\1"', new)
            if new != rels:
                patched[name] = new

        if not (patched or renames):
            return data

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                body = patched.get(item.filename)
                if body is None:
                    body = src.read(item.filename)
                out_name = renames.get(item.filename, item.filename)
                zi = zipfile.ZipInfo(out_name, date_time=item.date_time)
                zi.compress_type = item.compress_type
                zi.external_attr = item.external_attr
                dst.writestr(zi, body)
        result = out.getvalue()
        logger.info(
            "[XLSX_NORM] xlsx 包形态已归一: 改写 %d 部件 + 改名 %d VML %d→%d 字节",
            len(patched), len(renames), len(data), len(result),
        )
        return result
    except Exception as e:  # noqa: BLE001 — 护栏永不阻断落盘
        logger.warning("[XLSX_NORM] 归一化失败（原样落盘）: %s", e)
        return data
    finally:
        src.close()

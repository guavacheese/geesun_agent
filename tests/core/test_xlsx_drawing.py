# -*- coding: utf-8 -*-
"""xlsx drawing 命名空间归一化护栏测试（2026-11-19 双形态兼容）。

fixture 全部用 zipfile 手工构造（不依赖 openpyxl），drawing XML 逐段复刻
openpyxl 另存含图片文件的真实形态（生产样本：1098 IO表-1212_清晰后v1.xlsx）：
根 <wsDr xmlns="...spreadsheetDrawing"> 无 xdr: 前缀、子标签全部无前缀、
<a:blip r:embed> 保持前缀、descr 属性含反斜杠路径。
"""
import io
import xml.etree.ElementTree as ET
import zipfile

from src.infra.xlsx_drawing import normalize_xlsx_drawing_bytes

XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

DRAWING_DEFAULT_NS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<wsDr xmlns="{xdr}" xmlns:a="{a}">'
    '<twoCellAnchor editAs="oneCell">'
    "<from><col>17</col><colOff>0</colOff><row>52</row><rowOff>0</rowOff></from>"
    "<to><col>22</col><colOff>249383</colOff><row>77</row><rowOff>78568</rowOff></to>"
    '<pic><nvPicPr><cNvPr id="2" name="图片 1" descr="c:\\users\\public\\kk6\\image_cache\\e.png"/>'
    "<cNvPicPr/></nvPicPr>"
    '<blipFill><a:blip r:embed="rId1" xmlns:r="{r}"/><a:stretch><a:fillRect/></a:stretch></blipFill>'
    "<spPr><a:xfrm><a:off x=\"1\" y=\"2\"/><a:ext cx=\"3\" cy=\"4\"/></a:xfrm></spPr>"
    "</pic><clientData/></twoCellAnchor></wsDr>"
).format(xdr=XDR, a=A_NS, r=R_NS).encode("utf-8")

# Excel/WPS 标准形态（前缀版）——语义同 DRAWING_DEFAULT_NS，标签带 xdr:
_p = DRAWING_DEFAULT_NS.decode("utf-8").replace(
    'xmlns="%s"' % XDR, 'xmlns:xdr="%s"' % XDR
).replace("<wsDr ", "<xdr:wsDr ").replace("</wsDr>", "</xdr:wsDr>")
for _t in ("twoCellAnchor", "from", "to", "col", "colOff", "row", "rowOff",
           "pic", "nvPicPr", "cNvPr", "cNvPicPr", "blipFill", "spPr",
           "clientData"):
    _p = _p.replace("<%s" % _t, "<xdr:%s" % _t).replace("</%s>" % _t, "</xdr:%s>" % _t)
DRAWING_PREFIXED = _p.encode("utf-8")


def _minimal_xlsx(drawing_xml: bytes) -> bytes:
    """构造含一张内嵌图片的最小可用 xlsx（zip 结构完整，部件不必全合 schema）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
                   .encode())
        z.writestr("_rels/.rels", b"<Relationships/>")
        z.writestr("xl/workbook.xml", b"<workbook/>")
        z.writestr("xl/worksheets/sheet1.xml", b"<worksheet/>")
        z.writestr("xl/drawings/drawing1.xml", drawing_xml)
        z.writestr("xl/drawings/_rels/drawing1.xml.rels",
                   ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/image" Target="../media/image1.png"/>'
                    "</Relationships>").encode())
        z.writestr("xl/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return buf.getvalue()


def _read_member(data: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(name)


def test_default_namespace_drawing_gets_prefixed():
    out = normalize_xlsx_drawing_bytes(_minimal_xlsx(DRAWING_DEFAULT_NS))
    assert out is not None
    xml = _read_member(out, "xl/drawings/drawing1.xml")
    # 根元素：默认命名空间声明 → xmlns:xdr；根标签带前缀
    assert b'xmlns:xdr="%s"' % XDR.encode() in xml
    assert b"<xdr:wsDr" in xml
    assert b"<wsDr " not in xml
    # 锚点与全部无前缀子标签补了 xdr:
    assert b"<xdr:twoCellAnchor" in xml
    assert b"<xdr:from>" in xml
    assert b"<xdr:cNvPr" in xml
    # 已带前缀的 a:/r: 形态不动（a:blip 的 xmlns:r 声明仍在原元素上）
    assert b'<a:blip r:embed="rId1"' in xml
    # descr 反斜杠路径原样保留
    assert b"c:\\users\\public" in xml
    # 归一后的 XML 必须良构且无未绑定前缀（ET 解析对 unbound prefix 直接抛）
    root = ET.fromstring(xml)
    assert root.tag == "{%s}wsDr" % XDR


def test_other_zip_members_untouched():
    src = _minimal_xlsx(DRAWING_DEFAULT_NS)
    out = normalize_xlsx_drawing_bytes(src)
    for name in ("xl/media/image1.png", "xl/worksheets/sheet1.xml",
                 "xl/drawings/_rels/drawing1.xml.rels", "[Content_Types].xml"):
        assert _read_member(out, name) == _read_member(src, name)


def test_idempotent():
    once = normalize_xlsx_drawing_bytes(_minimal_xlsx(DRAWING_DEFAULT_NS))
    twice = normalize_xlsx_drawing_bytes(once)
    # 第二次返回同一对象 = 契约「未做修改」（drawing 已是前缀形态）
    assert twice is once


def test_prefixed_form_untouched():
    src = _minimal_xlsx(DRAWING_PREFIXED)
    assert normalize_xlsx_drawing_bytes(src) is src


# ── ②③：worksheet rels 绝对 Target + VML 改名（1098 事故第二段根因） ──

_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

SHEET_RELS_ABS = (
    '<Relationships xmlns="%s">'
    '<Relationship Id="comments" Type="%s/comments" Target="/xl/comments/comment1.xml"/>'
    '<Relationship Id="anysvml" Type="%s/vmlDrawing" Target="/xl/drawings/commentsDrawing1.vml"/>'
    '<Relationship Id="rId2" Type="%s/drawing" Target="/xl/drawings/drawing1.xml"/>'
    '<Relationship Id="ext1" Type="%s/hyperlink" Target="https://example.com/a.xlsx" TargetMode="External"/>'
    "</Relationships>" % (_PKG_REL, _REL_TYPE, _REL_TYPE, _REL_TYPE, _REL_TYPE)
).encode("utf-8")


def _xlsx_with_rels() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", b"<Types/>")
        z.writestr("xl/workbook.xml", b"<workbook/>")
        z.writestr("xl/worksheets/sheet1.xml", b"<worksheet/>")
        z.writestr("xl/worksheets/_rels/sheet1.xml.rels", SHEET_RELS_ABS)
        z.writestr("xl/comments/comment1.xml",
                   b'<comments xmlns="http://schemas.openxmlformats.org/'
                   b'spreadsheetml/2006/main"/>')
        z.writestr("xl/drawings/commentsDrawing1.vml", b"<xml><v:shape/></xml>")
        z.writestr("xl/drawings/drawing1.xml", DRAWING_DEFAULT_NS)
    return buf.getvalue()


def test_abs_targets_relativized_and_vml_renamed():
    out = normalize_xlsx_drawing_bytes(_xlsx_with_rels())
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        names = z.namelist()
        assert "xl/drawings/vmlDrawing1.vml" in names
        assert "xl/drawings/commentsDrawing1.vml" not in names
        rels = z.read("xl/worksheets/_rels/sheet1.xml.rels").decode("utf-8")
    assert 'Target="../comments/comment1.xml"' in rels
    assert 'Target="../drawings/vmlDrawing1.vml"' in rels
    assert 'Target="../drawings/drawing1.xml"' in rels
    # 外部超链接（TargetMode=External）原样不动
    assert 'Target="https://example.com/a.xlsx" TargetMode="External"' in rels
    # VML 内容原样搬迁
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert z.read("xl/drawings/vmlDrawing1.vml") == b"<xml><v:shape/></xml>"


def test_rels_rewrite_idempotent():
    once = normalize_xlsx_drawing_bytes(_xlsx_with_rels())
    assert normalize_xlsx_drawing_bytes(once) is once


def test_non_zip_passthrough():
    junk = b"not a zip at all"
    assert normalize_xlsx_drawing_bytes(junk) is junk


def test_zip_without_drawing_untouched():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", b"<workbook/>")
    src = buf.getvalue()
    assert normalize_xlsx_drawing_bytes(src) is src

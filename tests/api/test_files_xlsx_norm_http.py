# -*- coding: utf-8 -*-
"""下载端点 serve 侧 xlsx drawing 归一化的 HTTP 层验证（真路由，2026-11-19）。

覆盖的是「接线」而非归一化算法本身（算法单测在 tests/core/test_xlsx_drawing.py）：
  · GET .xlsx（默认命名空间形态）→ 下发字节已归一（含 xdr:wsDr）；
  · 标准形态 .xlsx → 字节与磁盘完全一致（FileResponse 零改动路径）;
  · 非 xlsx / 超尺寸闸 → 不动；
  · 归一化路径保留 Content-Disposition（inline/attachment + RFC 5987 中文名）。
"""
from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

try:
    import httpx
    from fastapi import FastAPI

    from src.api.deps import get_current_user
    from src.api.endpoints import files as files_mod

    # 跨文件复用 fixture 常量（tests 无 __init__.py，靠 rootdir 在 sys.path）
    from tests.core.test_xlsx_drawing import DRAWING_DEFAULT_NS, DRAWING_PREFIXED
except Exception as e:  # noqa: BLE001 - 缺运行期依赖的环境整体跳过
    pytest.skip(f"端点模块不可导入: {e}", allow_module_level=True)

UID = "GY23191"
SID = "3f5d4869"
PREFIX = "/api/v1"


def _xlsx_bytes(drawing_xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", b"<Types/>")
        z.writestr("xl/workbook.xml", b"<workbook/>")
        z.writestr("xl/worksheets/sheet1.xml", b"<worksheet/>")
        z.writestr("xl/drawings/drawing1.xml", drawing_xml)
        z.writestr("xl/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return buf.getvalue()


def _make_app(monkeypatch, report_root: str):
    app = FastAPI()
    app.include_router(files_mod.router, prefix=PREFIX)

    async def fake_user():
        return {"user_id": UID, "role": "user", "display_name": UID}

    app.dependency_overrides[get_current_user] = fake_user
    # download 端点经 `_search_file` 调 files_mod 命名空间里的 session_file_roots
    # （模块顶层 from-import 的直接引用），patch 必须打在这边而非 reports_mod
    monkeypatch.setattr(files_mod, "session_file_roots", lambda: [report_root])
    return app


def _get(app, url: str) -> httpx.Response:
    async def _call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            return await client.get(url)

    return asyncio.run(_call())


@pytest.fixture
def disk(tmp_path):
    root = tmp_path / "reports" / UID / SID
    root.mkdir(parents=True)
    (root / "怪形态.xlsx").write_bytes(_xlsx_bytes(DRAWING_DEFAULT_NS))
    (root / "标准形态.xlsx").write_bytes(_xlsx_bytes(DRAWING_PREFIXED))
    (root / "普通.bin").write_bytes(b"\x00\x01")
    return str(tmp_path / "reports")


def test_default_ns_xlsx_normalized_on_serve(monkeypatch, disk):
    app = _make_app(monkeypatch, disk)
    resp = _get(app, f"{PREFIX}/files/{UID}/{SID}/怪形态.xlsx")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        xml = z.read("xl/drawings/drawing1.xml")
    assert b"<xdr:wsDr" in xml  # 下发前已归一


def test_standard_xlsx_bytes_identical(monkeypatch, disk):
    app = _make_app(monkeypatch, disk)
    resp = _get(app, f"{PREFIX}/files/{UID}/{SID}/标准形态.xlsx")
    on_disk = _xlsx_bytes(DRAWING_PREFIXED)
    assert resp.content == on_disk  # FileResponse 零改动


def test_size_gate_bypasses_normalization(monkeypatch, disk):
    monkeypatch.setattr(files_mod, "_XLSX_NORM_MAX_BYTES", 10)  # 10 字节闸
    app = _make_app(monkeypatch, disk)
    resp = _get(app, f"{PREFIX}/files/{UID}/{SID}/怪形态.xlsx")
    assert resp.content == _xlsx_bytes(DRAWING_DEFAULT_NS)  # 超闸 → 原样


def test_content_disposition_preserved_on_normalized_path(monkeypatch, disk):
    app = _make_app(monkeypatch, disk)
    resp = _get(app, f"{PREFIX}/files/{UID}/{SID}/怪形态.xlsx")
    cd = resp.headers["content-disposition"]
    # xlsx 非 inline 名单 → attachment；RFC 5987 中文名
    assert cd.startswith("attachment;")
    assert "filename*=UTF-8''" in cd


def test_non_xlsx_untouched(monkeypatch, disk):
    app = _make_app(monkeypatch, disk)
    resp = _get(app, f"{PREFIX}/files/{UID}/{SID}/普通.bin")
    assert resp.content == b"\x00\x01"

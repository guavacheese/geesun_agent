"""会话产物清单端点 `GET /files/{user_id}/{session_id}` 的 HTTP 层验证（真路由）。

覆盖下载端点之外新增的那一面：
  · 清单只列 **reports** 根（uploads 里的用户输入文件刻意不出现）；
  · 越权（token 用户 ≠ 路径 user_id）→ 403；
  · 空目录 → 200 + 空数组（不是 404——面板要区分"没产物"与"会话不存在"）；
  · 响应字段形状（file_name/file_path/file_size/updated_at）与排序（mtime 倒序）。

与相邻文件的分工：路径查找/去重/穿越防御的单测在 `tests/infra/test_reports.py`
（纯函数层）；本文件只验证 HTTP 接线（鉴权、状态码、JSON 形状）。

运行：python -m pytest tests/api/test_files_list_http.py -q
"""

from __future__ import annotations

import asyncio

import pytest

try:
    import httpx
    from fastapi import FastAPI

    from src.api.deps import get_current_user
    from src.api.endpoints.files import router as files_router
    from src.infra import reports as reports_mod
except Exception as e:  # noqa: BLE001 - 缺运行期依赖的环境整体跳过
    pytest.skip(f"端点模块不可导入: {e}", allow_module_level=True)


UID = "GY23191"
SID = "3f5d4869"
PREFIX = "/api/v1"


def _make_app(monkeypatch: pytest.MonkeyPatch, report_root: str, upload_root: str):
    """真路由 + 假用户 + 把清单根目录指到临时盘。"""
    app = FastAPI()
    app.include_router(files_router, prefix=PREFIX)

    async def fake_user():
        return {"user_id": UID, "role": "user", "display_name": UID}

    app.dependency_overrides[get_current_user] = fake_user
    # 根目录走注入而不是改 settings 单例：避免污染同进程其他测试
    monkeypatch.setattr(reports_mod, "session_report_roots", lambda: [report_root])
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
    """摆磁盘：reports 根两个产物 + uploads 根一个输入文件（不应入清单）。"""
    import os

    report_root = tmp_path / "reports"
    upload_root = tmp_path / "uploads"
    new = report_root / UID / SID / "报告_new.html"
    old = report_root / UID / SID / "报告_old.md"
    new.parent.mkdir(parents=True)
    old.write_text("old")
    new.write_text("<html>new</html>")
    os.utime(old, (1000, 1000))
    os.utime(new, (3000, 3000))
    up = upload_root / UID / SID / "用户输入.xlsx"
    up.parent.mkdir(parents=True)
    up.write_text("input")
    return str(report_root), str(upload_root)


def test_list_happy_path_shape_and_order(monkeypatch, disk):
    report_root, upload_root = disk
    app = _make_app(monkeypatch, report_root, upload_root)
    resp = _get(app, f"{PREFIX}/files/{UID}/{SID}")
    assert resp.status_code == 200
    files = resp.json()["files"]
    assert [f["file_name"] for f in files] == ["报告_new.html", "报告_old.md"]  # mtime 倒序
    first = files[0]
    assert first["file_path"] == f"/reports/{UID}/{SID}/报告_new.html"
    assert first["file_size"] == len("<html>new</html>")
    assert "T" in first["updated_at"]  # ISO8601
    # uploads 根的输入文件不得出现
    assert all("用户输入" not in f["file_name"] for f in files)


def test_list_empty_dir_is_200_empty(monkeypatch, tmp_path):
    (tmp_path / "reports" / UID / SID).mkdir(parents=True)
    app = _make_app(monkeypatch, str(tmp_path / "reports"), str(tmp_path / "uploads"))
    resp = _get(app, f"{PREFIX}/files/{UID}/{SID}")
    assert resp.status_code == 200
    assert resp.json() == {"files": []}


def test_list_missing_dir_is_200_empty(monkeypatch, tmp_path):
    """从没产出过文件的会话：空清单而非 404（面板据此显示"暂无产物"）。"""
    app = _make_app(monkeypatch, str(tmp_path / "reports"), str(tmp_path / "uploads"))
    resp = _get(app, f"{PREFIX}/files/{UID}/{SID}")
    assert resp.status_code == 200
    assert resp.json() == {"files": []}


def test_list_other_user_forbidden(monkeypatch, disk):
    report_root, upload_root = disk
    app = _make_app(monkeypatch, report_root, upload_root)
    resp = _get(app, f"{PREFIX}/files/GY99999/{SID}")
    assert resp.status_code == 403

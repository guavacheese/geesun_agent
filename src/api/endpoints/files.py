"""文件下载/预览 API — 提供 Agent 生成的报告和用户上传文件的访问。"""

import os
import logging
import mimetypes
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from src.api.deps import get_current_user
from src.infra.reports import find_session_file, session_file_roots

logger = logging.getLogger(__name__)

router = APIRouter()

# 允许 inline 展示的文件类型（浏览器直接预览）
INLINE_EXTENSIONS = {
    ".md", ".txt", ".log",
    ".py", ".js", ".ts", ".tsx", ".css", ".html", ".json", ".yaml", ".yml", ".sh",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".pdf",
}

# 只允许按扩展名区分文本/代码预览的类型
TEXT_EXTENSIONS = {
    ".md", ".txt", ".log",
    ".py", ".js", ".ts", ".tsx", ".css", ".html", ".json", ".yaml", ".yml", ".sh",
}


def _validate_path(path: str) -> bool:
    """防止路径穿越攻击。"""
    # 不允许 .. 或以 / 开头
    if ".." in path or path.startswith("/"):
        return False
    return True


def _search_file(user_id: str, session_id: str, filename: str) -> str | None:
    """在可能的目录中查找真实文件路径，返回绝对路径或 None。

    目录清单与存在性判定统一在 `src.infra.reports.find_session_file` / `session_file_roots`
    （2026-09-29 抽出）：sessions.py 的"老数据补全"需要同款判定（只补磁盘真实存在的
    文件，否则模型正文里的幻觉路径会变成 HEAD 404 的红框卡片），两处各写一份必然走偏。
    本函数保留原签名，行为不变（根目录清单 = reports / uploads / workspace 兜底）。
    """
    return find_session_file(user_id, session_id, filename, roots=session_file_roots())


@router.head("/files/{user_id}/{session_id}/{filename:path}")
@router.get("/files/{user_id}/{session_id}/{filename:path}")
async def download_file(
    user_id: str,
    session_id: str,
    filename: str,
    current_user: dict = Depends(get_current_user),
):
    """
    提供 Agent 生成报告和用户上传文件的下载/预览。

    路径映射（按优先级）：
      1. {settings.report_root}/{user_id}/{session_id}/{filename}  — Agent 生成的报告
      2. {settings.upload_root}/{user_id}/{session_id}/{filename}  — 用户上传的文件

    安全约束：
      - 只能访问自己 user_id 下的文件（JWT 校验）
      - 防止路径穿越（.. 和绝对路径检查）
    """
    # ─── 权限校验 ───
    token_user_id = current_user["user_id"]
    if token_user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问其他用户的文件")

    # ─── 路径安全性校验 ───
    if not _validate_path(filename):
        raise HTTPException(status_code=400, detail="非法的文件路径")

    # ─── 在可能的目录中查找文件 ───
    file_path = _search_file(user_id, session_id, filename)
    dir_name = "reports"  # 仅用于日志

    if file_path is None:
        raise HTTPException(status_code=404, detail="文件不存在")

    # ─── 确定 MIME 类型 ───
    ext = os.path.splitext(filename)[1].lower()
    mime_type, _ = mimetypes.guess_type(filename)
    if mime_type is None:
        if ext in TEXT_EXTENSIONS:
            mime_type = "text/plain; charset=utf-8"
        else:
            mime_type = "application/octet-stream"

    # ─── 确定展示方式 ───
    # 图片/PDF/文本 inline 预览，其他 attachment 下载
    disposition = "inline" if ext in INLINE_EXTENSIONS else "attachment"
    # RFC 5987：HTTP header 只允许 Latin-1 字符，中文文件名若直接放进
    # Content-Disposition，Starlette 编码 header 时会抛 UnicodeEncodeError(500)。
    # 因此用 ASCII 回退名 + filename*=UTF-8'' 百分号编码的标准写法，
    # 现代浏览器可正确识别并下载为原始中文文件名。
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download"
    encoded_name = quote(filename)
    content_disposition = (
        f'{disposition}; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{encoded_name}"
    )

    logger.info(
        "文件下载: user=%s, session=%s, virtual=%s/%s/%s/%s, disk=%s, mime=%s",
        user_id, session_id, dir_name if file_path else "?", user_id, session_id, filename,
        file_path, mime_type,
    )

    return FileResponse(
        path=file_path,
        media_type=mime_type,
        headers={"Content-Disposition": content_disposition},
    )

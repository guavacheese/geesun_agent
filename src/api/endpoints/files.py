"""文件下载/预览 API — 提供 Agent 生成的报告和用户上传文件的访问。"""

import os
import logging
import mimetypes

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from src.api.deps import get_current_user
from src.core.config import settings

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
    search_dirs = [
        ("reports", settings.report_root),
        ("uploads", settings.upload_root),
    ]

    file_path = None
    for dir_name, root_dir in search_dirs:
        candidate = os.path.normpath(os.path.join(root_dir, user_id, session_id, filename))
        # 确保候选路径仍在允许的根目录下
        allowed_root = os.path.normpath(os.path.join(root_dir, user_id, session_id))
        if not candidate.startswith(allowed_root):
            logger.warning("路径穿越拦截: candidate=%s, allowed=%s", candidate, allowed_root)
            continue
        if os.path.isfile(candidate):
            file_path = candidate
            break

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
    if ext in INLINE_EXTENSIONS:
        content_disposition = f'inline; filename="{filename}"'
    else:
        content_disposition = f'attachment; filename="{filename}"'

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

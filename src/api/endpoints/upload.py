import os
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from src.core.config import settings

router = APIRouter()


def _safe_segment(value: str, field: str) -> str:
    """校验一个「路径段」（user_id / session_id）。

    为什么要显式判空：`startswith("")` 恒真、`"" in x` 也恒真，跳过判空等于全放行。
    为什么要挡 `.` / `..`：这两者不靠目录分隔符就能上跳一级，是路径穿越最短的写法。
    """
    seg = (value or "").strip()
    if not seg or seg in (".", ".."):
        raise HTTPException(status_code=400, detail="非法的 %s" % field)
    if "/" in seg or "\\" in seg or "\x00" in seg:
        raise HTTPException(status_code=400, detail="非法的 %s" % field)
    return seg


def _safe_filename(name: str | None) -> str:
    """从客户端文件名里取出「纯文件名」，丢弃任何目录成分。

    先按 POSIX 分隔符归一（客户端可能送 Windows 的 `..\\..\\x`），
    再取 basename —— `../../etc/passwd` → `passwd`；`/etc/passwd` → `passwd`。
    """
    raw = (name or "").replace("\\", "/")
    base = os.path.basename(raw).strip()
    if not base or base in (".", "..") or "\x00" in base:
        raise HTTPException(status_code=400, detail="非法的文件名")
    return base


@router.post("/upload")
async def upload_file(
    files: list[UploadFile] = File(...),
    user_id: str = Form(...),
    session_id: str = Form(...),
):
    """
    用户上传自己需要处理的文件，用于输入文件（PLC 源码、Excel 表等）

    ★ 路径安全（2026-10-30 修复）：
      原实现是 f"{upload_root}/{user_id}/{session_id}/{f.filename}" 直接拼串后 open()，
      客户端把 filename 填成 ../../x.txt 即可写到会话目录之外（实测 200 且落盘逃逸）。
      现在三道关：① user_id / session_id 逐段校验；② filename 只取 basename；
      ③ 解析后的真实路径其父目录必须等于会话目录（同时挡住符号链接逃逸）。
    """

    user_seg = _safe_segment(user_id, "user_id")
    sess_seg = _safe_segment(session_id, "session_id")

    dir_path = Path(settings.upload_root) / user_seg / sess_seg
    dir_path.mkdir(parents=True, exist_ok=True)
    base_dir = dir_path.resolve()

    results = []
    for f in files:
        name = _safe_filename(f.filename)
        target = (base_dir / name).resolve()
        # 用 parent 逐个相等做判据，而不是 str.startswith：
        # /data/uploads/ab 与 /data/uploads/abc 前缀相同，但后者应被拒绝。
        if target.parent != base_dir:
            raise HTTPException(status_code=400, detail="非法的文件名")
        content = await f.read()
        with open(target, "wb") as fh:
            fh.write(content)
        results.append(
            {
                "filename": name,
                "path": "/uploads/%s/%s/%s" % (user_seg, sess_seg, name),
            }
        )

    return {"uploaded": results}

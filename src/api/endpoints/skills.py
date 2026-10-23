import os
import re
import io
import zipfile
import tempfile
import shutil
import logging

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query
from src.core.config import settings
# 校验本体 2026-10-23 抽到 core：agent 的 write_file 直写 skill 目录也要过同一道闸
# （services 不能反向 import api，故校验落在 core，此处按原名复用）
from src.core.skill_md import validate_skill_md as _validate_skill_md
from src.core.skill_origin import (
    ORIGIN_AGENT,
    ORIGIN_UPLOAD,
    read_origin as _read_origin,
    write_origin as _write_origin,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── SKILL.md 的 YAML frontmatter 校验 → src/core/skill_md.py ───
#
# 唯一实现见 `src/core/skill_md.py::validate_skill_md`（原 `_validate_skill_md`
# 已迁出，函数体一份不剩，避免两处解析口径漂移）。

# ─── 获取用户 skill 的物理根目录 ───

def _user_skill_root(user_id: str) -> str:
    return f"{settings.agent_workspace}/skills/__user_{user_id}__"


# ─── skill 来源标记（.origin）───
#
# 2026-10-23：Agent 自创 skill 的默认落点从全局 `__agent__` 改到 `__user_{id}__`
# 之后，同一个目录里同时存在「agent 自动建的」和「用户手动上传的」两类 skill。
# 光看源码目录已无法区分（都在 `__user_{id}__` 下），故每个 skill 目录内落一个
# 标记文件 `.origin`（内容 `agent` / `upload`），`list_skills` 据此分组。
#
# 实现落在 src/core/skill_origin.py：chat.py 的 file_generated 钩子（= agent 写的）
# 与上传 API（= 用户传的）都是写入方，常量与读写只留一份。



# ─── 分组 → 物理目录解析 ───

def _skill_source_roots(user_id: str) -> dict[str, str]:
    return {
        "system": f"{settings.agent_workspace}/skills/__system__",
        "agent": f"{settings.agent_workspace}/skills/__agent__",
        "user": _user_skill_root(user_id),
    }


def _resolve_skill_dir(skill_name: str, user_id: str, source: str) -> str | None:
    """按**展示分组**定位 skill 的物理目录，支持跨目录回退。

    为什么需要回退：`agent` 分组里同时有两类东西 ——
    - 全局历史层 `__agent__/`（2026-10-23 起冻结只读）
    - 当前用户目录下由 agent 自动创建的（`.origin == agent`）

    前端只传展示分组（source），物理位置由后端判定，避免前端为「同名 skill 在哪个
    目录」写第二份推断逻辑。找不到返回 None，由调用方转 404。
    """
    roots = _skill_source_roots(user_id)
    if source not in roots:
        return None

    candidates = [roots[source]]
    if source == "agent":
        # agent 自创已迁到用户目录：先查用户目录，再落全局历史层
        candidates.insert(0, roots["user"])

    for root in candidates:
        candidate = os.path.join(root, skill_name)
        if os.path.isdir(candidate):
            return candidate
    return None



# ─── 检查文件是否是 zip ───

def _is_zip(content: bytes) -> bool:
    return content[:4] == b"PK\x03\x04"


# ─── 上传 Skill ───

@router.post("/skill/upload")
async def upload_skill(
    files: list[UploadFile] = File(...),
    skill_name: str = Form(...),
    user_id: str = Form(...),
):
    """
    上传 skill（支持 zip 包或多个文件），写入用户共享目录。

    - 如果只传了 1 个 zip 文件：自动解压到 {skill_name}/ 下，保持目录结构
    - 其他情况（多个文件 / 单个非 zip 文件）：逐个写入 {skill_name}/{filename}

    校验：SKILL.md 必须包含 YAML frontmatter（name / description 必填）
    """
    if not files:
        raise HTTPException(status_code=400, detail="至少需要上传一个文件")

    target_dir = f"{_user_skill_root(user_id)}/{skill_name}"

    # 记「这次是用户主动上传」：同一个 skill 名若此前由 agent 自动创建（.origin=agent），
    # 用户重新上传即视为接管，分组翻到「用户上传」。放在校验之前是刻意的——校验失败时
    # 目录本身已创建（既有行为），标记跟着目录走比留下"半成品无标记"更可预期。
    _write_origin(target_dir, ORIGIN_UPLOAD)

    # 只有 1 个文件且是 zip → 解压
    if len(files) == 1:
        content = await files[0].read()
        if _is_zip(content):
            return await _handle_zip_upload(content, target_dir, skill_name, user_id)
        # 非 zip：直接写单文件，避免二次 read + seek 失效
        os.makedirs(target_dir, exist_ok=True)
        filename = files[0].filename or "unnamed"
        if ".." in filename or filename.startswith("/"):
            raise HTTPException(status_code=400, detail=f"非法路径: {filename}")
        # 如果是 SKILL.md，先校验
        if filename.lower() == "skill.md":
            validation = _validate_skill_md(content.decode("utf-8"))
            if not validation["valid"]:
                raise HTTPException(status_code=400, detail=f"SKILL.md 校验失败: {validation['error']}")
            if validation["name"] != skill_name:
                raise HTTPException(
                    status_code=400,
                    detail=f"skill_name 参数与 SKILL.md 中的 name 不一致",
                )
        file_path = os.path.normpath(f"{target_dir}/{filename}")
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as fh:
            fh.write(content)
        return {
            "success": True,
            "skill_name": skill_name,
            "description": validation.get("description", "") if filename.lower() == "skill.md" else "",
            "virtual_path": f"/skills/__user_{user_id}__/{skill_name}/",
            "files": [filename],
            "files_count": 1,
        }

    # 多个文件或单个非 zip → 逐个写入
    return await _handle_multi_file_upload(files, target_dir, skill_name, user_id)


async def _handle_zip_upload(content: bytes, target_dir: str, skill_name: str, user_id: str) -> dict:
    """处理 zip 包上传"""
    # 解压到临时目录
    tmp_dir = tempfile.mkdtemp(prefix="skill_upload_")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            # 安全检查：防止 Zip Slip 攻击
            for name in zf.namelist():
                if ".." in name or name.startswith("/"):
                    raise HTTPException(status_code=400, detail=f"非法路径: {name}")
            zf.extractall(tmp_dir)

        # 查找 SKILL.md
        skill_md_path = None
        extracted_files = []
        for root, dirs, files in os.walk(tmp_dir):
            for f in files:
                rel_path = os.path.relpath(os.path.join(root, f), tmp_dir)
                extracted_files.append(rel_path)
                if f.lower() == "skill.md":
                    skill_md_path = os.path.join(root, f)

        if not skill_md_path:
            shutil.rmtree(tmp_dir)
            raise HTTPException(
                status_code=400,
                detail="zip 包中未找到 SKILL.md 文件，请确保包内包含 SKILL.md",
            )

        # 校验 SKILL.md
        with open(skill_md_path, "r", encoding="utf-8", errors="replace") as f:
            md_content = f.read()

        validation = _validate_skill_md(md_content)
        if not validation["valid"]:
            shutil.rmtree(tmp_dir)
            raise HTTPException(status_code=400, detail=f"SKILL.md 校验失败: {validation['error']}")

        # 校验 skill_name 和 frontmatter 中的 name 一致
        if validation["name"] != skill_name:
            shutil.rmtree(tmp_dir)
            raise HTTPException(
                status_code=400,
                detail=f"skill_name 参数 ('{skill_name}') 与 SKILL.md 中的 name ('{validation['name']}') 不一致",
            )

        # 修正：如果 zip 里只有一层根目录（如 plc-code-auditor/），自动剥掉
        entries = os.listdir(tmp_dir)
        src_dir = tmp_dir
        if len(entries) == 1 and os.path.isdir(os.path.join(tmp_dir, entries[0])):
            src_dir = os.path.join(tmp_dir, entries[0])
            # 修正 extracted_files，去掉第一层前缀
            corrected = []
            for f in extracted_files:
                parts = f.split("/", 1)
                corrected.append(parts[1] if len(parts) == 2 else f)
            extracted_files = corrected

        # 覆盖写入目标目录
        if os.path.isdir(target_dir):
            shutil.rmtree(target_dir)
        shutil.copytree(src_dir, target_dir)

        return {
            "success": True,
            "skill_name": skill_name,
            "description": validation["description"],
            "virtual_path": f"/skills/__user_{user_id}__/{skill_name}/",
            "files": extracted_files,
            "files_count": len(extracted_files),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理 zip 包失败: {str(e)}")
    finally:
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _handle_multi_file_upload(
    files: list[UploadFile], target_dir: str, skill_name: str, user_id: str
) -> dict:
    """处理多文件上传（逐个写入）"""
    os.makedirs(target_dir, exist_ok=True)
    uploaded = []

    for f in files:
        content = await f.read()
        filename = f.filename or "unnamed"

        # 安全检查
        if ".." in filename or filename.startswith("/"):
            raise HTTPException(status_code=400, detail=f"非法路径: {filename}")

        # 如果是 SKILL.md，先校验
        if filename.lower() == "skill.md" or filename.lower().endswith("/skill.md"):
            try:
                md_content = content.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(status_code=400, detail="SKILL.md 必须是 UTF-8 编码的文本文件")

            validation = _validate_skill_md(md_content)
            if not validation["valid"]:
                raise HTTPException(status_code=400, detail=f"SKILL.md 校验失败: {validation['error']}")

            if validation["name"] != skill_name:
                raise HTTPException(
                    status_code=400,
                    detail=f"skill_name 参数 ('{skill_name}') 与 SKILL.md 中的 name ('{validation['name']}') 不一致",
                )

        # 写入文件（保留子目录结构，如 scripts/tool.py）
        file_path = os.path.normpath(f"{target_dir}/{filename}")
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as fh:
            fh.write(content)

        uploaded.append(filename)

    return {
        "success": True,
        "skill_name": skill_name,
        "virtual_path": f"/skills/__user_{user_id}__/{skill_name}/",
        "files": uploaded,
        "files_count": len(uploaded),
    }


# ─── 列出所有可用的 Skill ───

@router.get("/skills")
async def list_skills(
    user_id: str = Query(..., description="当前用户 ID"),
):
    """
    列出当前用户可用的所有 skill。

    三个展示分组：
    - system：系统预装（`__system__`）
    - agent：Agent 自创 —— **全局历史层** `__agent__`（2026-10-23 起冻结，只读）
             + 当前用户目录下由 agent 自动创建的（`.origin == agent`）
    - user：当前用户目录下用户自己上传的

    ⚠️ 分组键（source）是**展示语义**，不再等于物理目录：agent 自创的 skill 现在
    物理上落在 `__user_{user_id}__/`。`/skill/{name}/files` 等接口靠
    `_resolve_skill_dir` 做目录回退，调用方无需感知这层差异。
    """
    skill_dirs = {
        "system": f"{settings.agent_workspace}/skills/__system__",
        "agent": f"{settings.agent_workspace}/skills/__agent__",
        "user": _user_skill_root(user_id),
    }

    result = {"system": [], "agent": [], "user": [], "all": []}

    for source, root_dir in skill_dirs.items():
        if not os.path.isdir(root_dir):
            continue
        try:
            for entry in sorted(os.listdir(root_dir)):
                skill_path = os.path.join(root_dir, entry)
                if not os.path.isdir(skill_path):
                    continue
                skill_md = os.path.join(skill_path, "SKILL.md")
                meta = {"name": entry, "description": "", "source": source}
                if os.path.isfile(skill_md):
                    try:
                        with open(skill_md, "r", encoding="utf-8", errors="replace") as f:
                            md_content = f.read()
                        validation = _validate_skill_md(md_content)
                        if validation["valid"]:
                            meta["description"] = validation["description"]
                    except Exception:
                        pass

                if source == "user":
                    # 用户目录里混着两类：agent 自动建的 / 用户上传的，按 .origin 分流。
                    # owned=True ⇒ 物理上在本人目录，可删（前端据此显示删除按钮）。
                    origin = _read_origin(skill_path)
                    meta["origin"] = origin
                    meta["owned"] = True
                    target_key = "agent" if origin == ORIGIN_AGENT else "user"
                    meta["source"] = target_key
                    result[target_key].append(meta)
                else:
                    # 全局层（__system__ / 冻结的 __agent__）：只读，不可删
                    meta["owned"] = False
                    result[source].append(meta)
                result["all"].append(meta)
        except PermissionError:
            continue

    return result



# ─── 删除用户的 skill ───

@router.delete("/skill/{skill_name}")
async def delete_skill(
    skill_name: str,
    user_id: str = Query(..., description="当前用户 ID"),
):
    """
    删除用户上传的 skill。
    只能删除 __user_{user_id}__ 目录下的 skill，不能删除系统预装 skill。
    """
    target_dir = f"{_user_skill_root(user_id)}/{skill_name}"

    if not os.path.isdir(target_dir):
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' 不存在")

    try:
        shutil.rmtree(target_dir)
        return {"success": True, "skill_name": skill_name, "deleted": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")


# ─── 浏览 skill 内的文件列表 ───

@router.get("/skill/{skill_name}/files")
async def list_skill_files(
    skill_name: str,
    user_id: str = Query(..., description="当前用户 ID"),
    source: str = Query("user", description="skill 来源: system / agent / user"),
):
    """
    列出指定 skill 目录下的所有文件（含子目录结构，相对路径）。

    `source` 是**展示分组**：`agent` 组可能命中用户目录下 agent 自动创建的 skill，
    物理位置由 `_resolve_skill_dir` 回退解析。
    """
    if source not in _skill_source_roots(user_id):
        raise HTTPException(status_code=400, detail=f"无效的 source: {source}")

    skill_dir = _resolve_skill_dir(skill_name, user_id, source)
    if not skill_dir:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' 不存在")

    files = []
    for dirpath, _, filenames in os.walk(skill_dir):
        for f in filenames:
            # 隐藏文件（如 .origin 来源标记）是后端内部元数据，不进 UI 文件树
            if f.startswith("."):
                continue
            abs_path = os.path.join(dirpath, f)
            rel_path = os.path.relpath(abs_path, skill_dir).replace("\\", "/")
            file_size = os.path.getsize(abs_path)
            files.append({
                "path": rel_path,
                "size": file_size,
            })

    files.sort(key=lambda x: x["path"])
    return {
        "skill_name": skill_name,
        "source": source,
        "files": files,
        "count": len(files),
    }


# ─── 读取 skill 内单个文件内容 ───

@router.get("/skill/{skill_name}/file")
async def read_skill_file(
    skill_name: str,
    user_id: str = Query(..., description="当前用户 ID"),
    source: str = Query("user", description="skill 来源: system / agent / user"),
    path: str = Query(..., description="文件相对路径，如 SKILL.md 或 scripts/tool.py"),
):
    """
    读取 skill 内单个文本文件的内容。
    只返回文本文件（.md / .py / .txt / .json / .yaml / .yml / .sh），禁止读取二进制。
    """
    if source not in _skill_source_roots(user_id):
        raise HTTPException(status_code=400, detail=f"无效的 source: {source}")

    skill_dir = _resolve_skill_dir(skill_name, user_id, source)
    if not skill_dir:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' 不存在")

    # 安全检查
    if ".." in path or path.startswith("/"):
        raise HTTPException(status_code=400, detail=f"非法路径: {path}")

    file_path = os.path.normpath(os.path.join(skill_dir, path))
    if not file_path.startswith(os.path.normpath(skill_dir)):
        raise HTTPException(status_code=400, detail=f"非法路径: {path}")

    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")

    # 只允许文本文件
    ext = os.path.splitext(path)[1].lower()
    allowed_exts = {".md", ".py", ".txt", ".json", ".yaml", ".yml", ".sh", ".js", ".ts", ".css", ".html"}
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")

    # 限制 200KB
    if os.path.getsize(file_path) > 200 * 1024:
        raise HTTPException(status_code=400, detail="文件过大（超过 200KB）")

    try:
        file_size = os.path.getsize(file_path)
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        logger.info("读取 skill 文件: %s (%d bytes, %d chars)", file_path, file_size, len(content))
        return {
            "skill_name": skill_name,
            "path": path,
            "content": content,
            "size": file_size,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取文件失败: {str(e)}")

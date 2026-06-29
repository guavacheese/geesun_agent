import os
import re
import io
import zipfile
import tempfile
import shutil
import logging

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query
from src.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── 校验 SKILL.md 的 YAML frontmatter ───

def _validate_skill_md(content: str) -> dict:
    """
    校验 SKILL.md 的 YAML frontmatter，返回解析结果。

    要求：
    - 开头必须有两行 --- 包裹的 YAML 块
    - 必须包含 name 和 description 字段

    Returns: {"valid": bool, "name": str|None, "description": str|None, "error": str|None}
    """
    content_clean = content.lstrip("\ufeff").lstrip()  # 去掉 BOM 和前导空白

    if not content_clean.startswith("---"):
        return {"valid": False, "name": None, "description": None,
                "error": "SKILL.md 必须以 --- 开头（YAML frontmatter）"}

    # 找到第二个 ---
    second_dash = content_clean.find("---", 3)
    if second_dash == -1:
        return {"valid": False, "name": None, "description": None,
                "error": "SKILL.md 的 YAML frontmatter 缺少闭合 ---"}

    yaml_block = content_clean[3:second_dash].strip()

    # 简单解析 key: value 行（不依赖 PyYAML，避免引入额外依赖）
    name = None
    description = None
    for line in yaml_block.split("\n"):
        line = line.strip()
        # 去掉引号包裹的值
        m = re.match(r"^name\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            name = m.group(1).strip().strip("\"'").strip()
            continue
        m = re.match(r"^description\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            description = m.group(1).strip().strip("\"'").strip()
            continue

    errors = []
    if not name:
        errors.append("frontmatter 缺少 'name' 字段")
    if not description:
        errors.append("frontmatter 缺少 'description' 字段")

    if errors:
        return {"valid": False, "name": name, "description": description,
                "error": "; ".join(errors)}

    return {"valid": True, "name": name, "description": description, "error": None}


# ─── 获取用户 skill 的物理根目录 ───

def _user_skill_root(user_id: str) -> str:
    return f"{settings.agent_workspace}/skills/__user_{user_id}__"


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

    # 只有 1 个文件且是 zip → 解压
    if len(files) == 1:
        content = await files[0].read()
        if _is_zip(content):
            return await _handle_zip_upload(content, target_dir, skill_name, user_id)

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
    包括：系统预装（__system__）、Agent 自创（__agent__）、用户共享（__user_{user_id}__）
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

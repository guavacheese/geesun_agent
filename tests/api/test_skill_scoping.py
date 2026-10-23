"""skill 归属隔离（2026-10-23）：agent 自创落用户目录 + `.origin` 分组。

事故背景
--------
原设计里 agent 自创 skill 只有一个落点：全局 `/skills/__agent__/`。后果是
① 跨用户串场（A 的 skill 被注入进 B 的对话）② 同名互相覆盖 ③ 无法按人清理。
改法是让 agent 写进当前用户目录 `/skills/__user_{id}__/`（见
`ValidatedCompositeBackend.USER_SKILL_PREFIX_TEMPLATE`）。

改完之后同一个用户目录里混着两类 skill，靠源码分不出来，只能靠 `.origin` 标记
（`src/core/skill_origin.py`）。本文件钉住三件事：

1. `list_skills` 按 `.origin` 正确分流，且 `owned` 只对用户目录里的条目为 True
   （前端据此决定要不要显示删除按钮）；
2. `_resolve_skill_dir` 能把「展示分组 agent」回退到用户目录（否则点开 skill
   看文件列表必然 404）；
3. `.origin` 是内部元数据，不出现在文件树里。
"""

import asyncio
import io
import os

import pytest
from starlette.datastructures import Headers, UploadFile

from src.api.endpoints import skills as skills_api
from src.core import skill_origin
from src.core.config import settings

SKILL_MD = "---\nname: {name}\ndescription: 测试用 skill\n---\n\n# {name}\n"


def _mk_skill(root, name: str, origin: str | None = None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(SKILL_MD.format(name=name), encoding="utf-8")
    if origin:
        skill_origin.write_origin(str(d), origin)
    return d


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """临时 agent_workspace，含三层 skill 目录。"""
    monkeypatch.setattr(settings, "agent_workspace", str(tmp_path))
    base = tmp_path / "skills"
    for sub in ("__system__", "__agent__", "__user_u1__"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


def test_list_skills_splits_by_origin(ws):
    """agent 建的与用户传的都物理在 `__user_u1__/`，但必须分到不同展示分组。"""
    _mk_skill(ws / "__system__", "sys1")
    _mk_skill(ws / "__agent__", "legacy")
    _mk_skill(ws / "__user_u1__", "agent-made", origin=skill_origin.ORIGIN_AGENT)
    _mk_skill(ws / "__user_u1__", "mine", origin=skill_origin.ORIGIN_UPLOAD)
    _mk_skill(ws / "__user_u1__", "old-no-marker")  # 无标记 → 兜底归 user

    resp = asyncio.run(skills_api.list_skills(user_id="u1"))

    assert [s["name"] for s in resp["system"]] == ["sys1"]
    # agent 组 = 全局历史层 + 本用户目录下 agent 自创的
    assert {s["name"] for s in resp["agent"]} == {"legacy", "agent-made"}
    assert {s["name"] for s in resp["user"]} == {"mine", "old-no-marker"}

    by_name = {s["name"]: s for s in resp["all"]}
    assert by_name["agent-made"]["owned"] is True
    assert by_name["agent-made"]["origin"] == skill_origin.ORIGIN_AGENT
    # 全局历史层不可删
    assert by_name["legacy"]["owned"] is False
    assert by_name["legacy"]["source"] == "agent"


def test_resolve_skill_dir_falls_back_to_user_dir(ws):
    """`source=agent` 要能从用户目录里找到 agent 自创的 skill（否则文件列表 404）。"""
    user_dir = _mk_skill(ws / "__user_u1__", "agent-made", origin=skill_origin.ORIGIN_AGENT)
    _mk_skill(ws / "__agent__", "legacy")

    # 生产代码用 f-string 拼 "/" 再 os.path.join，Windows 下会混出两种分隔符，
    # 故比较前统一 normpath（Linux 上无影响）
    resolved = skills_api._resolve_skill_dir("agent-made", "u1", "agent")
    assert os.path.normpath(resolved) == os.path.normpath(str(user_dir))

    legacy = skills_api._resolve_skill_dir("legacy", "u1", "agent")
    assert os.path.normpath(legacy) == os.path.normpath(str(ws / "__agent__" / "legacy"))

    # 别的用户看不到 u1 目录里的东西（物理隔离的兜底验证）
    assert skills_api._resolve_skill_dir("agent-made", "u2", "agent") is None
    assert skills_api._resolve_skill_dir("nope", "u1", "agent") is None


def test_list_skill_files_hides_origin_marker(ws):
    """`.origin` 是后端内部元数据，不能出现在前端的文件树里。"""
    _mk_skill(ws / "__user_u1__", "mine", origin=skill_origin.ORIGIN_UPLOAD)
    resp = asyncio.run(skills_api.list_skill_files("mine", user_id="u1", source="user"))
    assert [f["path"] for f in resp["files"]] == ["SKILL.md"]


def test_upload_flips_origin_to_upload(ws):
    """用户重新上传同名 skill = 接管：分组必须从「Agent 自创」翻到「用户上传」。"""
    d = _mk_skill(ws / "__user_u1__", "demo", origin=skill_origin.ORIGIN_AGENT)

    upload = UploadFile(
        filename="SKILL.md",
        file=io.BytesIO(SKILL_MD.format(name="demo").encode("utf-8")),
        headers=Headers({"content-type": "text/markdown"}),
    )
    asyncio.run(skills_api.upload_skill(files=[upload], skill_name="demo", user_id="u1"))

    assert skill_origin.read_origin(str(d)) == skill_origin.ORIGIN_UPLOAD
    resp = asyncio.run(skills_api.list_skills(user_id="u1"))
    assert [s["name"] for s in resp["user"]] == ["demo"]
    assert all(s["name"] != "demo" for s in resp["agent"])


def test_origin_roundtrip_and_default(tmp_path):
    skill_origin.write_origin(str(tmp_path), skill_origin.ORIGIN_AGENT)
    assert skill_origin.read_origin(str(tmp_path)) == skill_origin.ORIGIN_AGENT

    other = tmp_path / "no-marker"
    other.mkdir()
    assert os.path.isdir(str(other))
    assert skill_origin.read_origin(str(other)) == skill_origin.ORIGIN_UPLOAD

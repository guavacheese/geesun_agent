import asyncio
import json
import os
import re
import time
import logging
import psycopg
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.infra.database import message_key
from src.infra.sandbox import create_sandbox, get_env_snapshot
from src.infra.reports import snapshot_report_files
from src.services.agent import create_agent
from src.core import execution_guard, turn_registry
from src.core.mcp import get_mcp_tools
from src.core.terminal_response import fallback_notice, pop_fallback_signal, quick_mode_notice
from src.api.deps import get_store, get_checkpointer, get_current_user
from src.core.config import settings

logger = logging.getLogger(__name__)


def _clip(text: str, limit: int) -> str:
    """按配置上限裁剪文本；limit <= 0 表示不限制（2026-09-12 新增）。

    store 回放副本与 SSE tool_result 的长度上限统一走此函数，
    替代此前散落在 _persist_session / 事件流里的 [:2000] 硬编码。
    """
    if limit and limit > 0 and len(text) > limit:
        return text[:limit]
    return text


def _plan_message_append(
    history: list[dict],
    prev_count: int,
    last_stored_id: str | None,
) -> tuple[int, list[dict], str]:
    """算出增量追加的起始序号与待写条目（纯函数，便于 spike 直接断言）。

    返回 `(next_seq, new_items, mode)`：
      next_seq   本条消息的起始 store 序号（= 已存最大序号 + 1）
      new_items  本次要写的新条目（可能为空）
      mode       定位方式，仅用于日志与断言：`by_id` / `by_length` / `none`

    ## 为什么需要"按 id 定位"而不是"按长度"

    增量追加的朴素前提是"history 只在尾部增长"。但 `SummarizationMiddleware` 触发时
    返回 `[RemoveMessage(REMOVE_ALL_MESSAGES), 摘要, *保留的尾部]`——**history 会变短**。
    此时若仍按 `len(history) > prev_count` 判断，压缩后 len 反而更小 → 判为"无新增"，
    当轮之后的新消息**永远写不进去**；反过来若按 history 下标算 key，压缩后 history
    里第 10 条其实是 store 里第 250 条 → **覆盖已归档条目**。

    所以用 store 里最大序号那条的 `id`，在 history 里反查它的位置，从其后开始追加；
    序号继续沿用 **store 的**序号（不是 history 下标），两头都不会重叠。
    摘要消息本身**不写入 store**：store 是给人看的回放副本，已保留完整原文，
    摘要只是"喂给模型的面"的产物，写进来反而会把原文挤掉（正是旧 bug 的形态）。

    ## 退化路径

    `last_stored_id` 为 None（老数据 / 无 id 构造路径）或 id 在 history 里找不到
    （历史被改写、条目无 id）→ 退回按长度判断。此时若 history 没有更长，
    本次什么都不写并交由调用方打 warn——**宁可少写一轮，不可改写已归档内容**。
    """
    next_seq = prev_count + 1

    start: int | None = None
    mode = "none"
    if last_stored_id:
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("id") == last_stored_id:
                start = i + 1
                mode = "by_id"
                break

    if start is None:
        if len(history) > prev_count:
            start = prev_count
            mode = "by_length"
        else:
            start = len(history)
            mode = "none"

    return next_seq, history[start:], mode


def _warn_oversized_items(items: list[dict], *, prefix: str) -> None:
    """单条 entry 体积观测闸——**只打日志，不截断、不丢弃**（2026-09-12 方案 B）。

    持久化层截断 = 不可逆删除（5df629e 的教训），所以这里只做信号。
    依据：生产实测单条最大 278 KB（AI 正文 272833 字符）→ 阈值 1 MB 留 3.6× 余量。
    """
    limit = settings.persist_max_item_bytes
    if limit <= 0:
        return
    for entry in items:
        try:
            size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as e:
            logger.warning("[DIAG] 单条 entry 无法序列化度量: %s (prefix=%s)", e, prefix)
            continue
        if size > limit:
            logger.warning(
                "[DIAG] 单条消息体积 %d 字节超过观测阈值 %d（未截断，仅告警）: "
                "prefix=%s role=%s id=%s",
                size, limit, prefix, entry.get("role"), entry.get("id"),
            )


async def _aget_state_with_retry(agent, thread_id: str, max_attempts: int = 3):
    """带重试地读取 agent 最终状态（防御性）。

    断连兜底路径（reason="interrupted"）下，agent 图可能仍在写 checkpoint。
    原单连接模式会在此撞 "another command is already in progress" 而静默丢消息
    （server.log 16:30:09 checkpointer.aget_tuple 失败）。底层已换连接池后该碰撞
    不再发生，此处仍加短暂重试作为纵深防御，避免瞬时争用直接丢本轮消息。
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await agent.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
        except (
            psycopg.OperationalError,
            psycopg.InterfaceError,
            psycopg.ProgrammingError,
        ) as e:
            last_exc = e
            logger.warning(
                "[DIAG] aget_state 第 %d/%d 次失败（%s），重试...",
                attempt, max_attempts, e,
            )
            await asyncio.sleep(0.2 * attempt)
    logger.error(
        "aget_state 重试 %d 次仍失败，放弃读取最终状态: %s",
        max_attempts, last_exc,
    )
    raise last_exc


def _tool_intent_sig(tool_name: str, result_str: str) -> str:
    """提取工具调用意图指纹：工具名 + 结果内容的关键特征。

    用于 P0 无进展检测——判断模型是否在重复执行同一件事。
    - execute：取结果前 60 字符（命令相同 → 结果通常相同，指纹稳定）
    - 其他工具：取结果前 80 字符
    结果为空时退化为纯工具名（同工具重复也算重复意图）。
    """
    s = (result_str or "").strip()
    if tool_name == "execute":
        return f"{tool_name}:{s[:60]}"
    return f"{tool_name}:{s[:80]}" if s else tool_name


def _missing_skill_artifacts(
    report_root: str,
    user_id: str,
    session_id: str,
    stage1_done: bool,
    stage3_done: bool,
) -> list[str]:
    """确定性校验 skill 工作流关键产物（M3 扩展，2026-08-19）。

    本轮跑过 run_pdf_diff_stage1/stage3（成功）后，reports 必须出现对应产物
    （agent 用 download_from_sandbox 拉回）。缺失即视为"流程未走完"，
    由 M3 完成门注入收敛提示继续，不信任模型自陈"做完了"。

    Returns: 缺失项描述列表（空 = 齐）。
    """
    missing: list[str] = []
    rp = os.path.join(report_root, user_id, session_id)
    if stage1_done and not os.path.isfile(os.path.join(rp, "diff_pages.json")):
        missing.append(
            "diff_pages.json（run_pdf_diff_stage1 产物，需 download_from_sandbox 拉回再读）"
        )
    if stage3_done:
        try:
            has_report = any(
                n.startswith("技术协议差异对比报告_")
                and n.endswith((".md", ".html"))
                for n in os.listdir(rp)
            ) if os.path.isdir(rp) else False
        except OSError:
            has_report = False
        if not has_report:
            missing.append(
                "技术协议差异对比报告_*（run_pdf_diff_stage3 产物，需 download_from_sandbox 拉回）"
            )
    return missing


class ToolLoopAbortError(Exception):
    """工具连续失败超限，提前终止本轮流式生成。

    与 GraphRecursionError 的区别：后者要烧满 recursion_limit（100 步）才抛，
    期间模型会反复空转；本异常在连续失败达到阈值时立刻抛出，
    由 _drain_astream 的 except 分支捕获后转为 SSE error 事件，秒级止损。
    """


class NoProgressAbortError(Exception):
    """工具"成功但无进展"循环检测，中断当前轮并触发收敛注入。

    与 ToolLoopAbortError 的区别：那个管"工具连续失败"（错误循环），
    本异常管"工具全部成功但模型反复执行同一意图、零新交付物"的空转
    （2026-08-17/08-18 两次 Recursion limit 死循环实证：execute 全 OK、
    报告已生成，但模型不断"重新构建 diff.json"永不收敛）。
    由外层捕获后注入收敛 SystemMessage，让模型收敛交付而非烧满 200 步。
    """


# 与前端 lib/types.ts inferFileType 保持一致
# 用于保存 generated_files 时填充 file_type 字段
_FILE_TYPE_BY_EXT = {
    "md": "text", "txt": "text", "log": "text",
    "py": "code", "js": "code", "ts": "code", "tsx": "code",
    "css": "code", "html": "code", "json": "code",
    "yaml": "code", "yml": "code", "sh": "code",
    "java": "code", "go": "code", "rs": "code",
    "c": "code", "cpp": "code", "h": "code",
    "png": "image", "jpg": "image", "jpeg": "image",
    "gif": "image", "svg": "image", "webp": "image",
    "bmp": "image", "ico": "image",
    "pdf": "pdf",
    "xlsx": "spreadsheet", "xls": "spreadsheet", "csv": "spreadsheet",
    "zip": "archive", "tar": "archive", "gz": "archive",
    "7z": "archive", "rar": "archive",
}


def _infer_file_type(file_name: str) -> str:
    ext = file_name.split(".")[-1].lower() if "." in file_name else ""
    return _FILE_TYPE_BY_EXT.get(ext, "other")


def _tool_result_is_error(
    tool_name: str, content_str: str, status: str | None = None
) -> bool:
    """判定一条工具结果是否失败（决定 SSE 的 success 字段与文件解析准入）。

    优先级：**框架的 `ToolMessage.status` > 内容启发式**。

    为什么必须让 status 优先（2026-09-29 根因修复）：
    execute 通道护栏（`agent.py` 的 `ValidatedCompositeBackend.execute`）拒绝越界命令时
    抛 `ValueError` → deepagents 工具层回
    `ToolMessage(status="error", content="Error: Invalid parameter. {拒绝文案}")`。
    这段内容**既不含** "command failed with exit code"、**也不以** "Execution error:"
    开头 → 旧的 execute 关键词分支判 `is_error=False` → 拒绝结果被当**成功**结果送进
    文件解析 → 护栏文案里的 `/reports/<uid>/<sid>/<文件名>` 被当路径提取 → 产出一张
    `file_size=0` 的幽灵卡片（2026-09-29 活体 e2e 实测，正是本次要消灭的那类病）。
    status 是框架权威字段，有它就不该再猜。

    关键词兜底仍保留（status 缺失/老版本 SDK 时用），并保持原语义：
    - 结构化 JSON：只看 `success` 字段，命中即返回，**不落关键词**（历史上
      `"error": null` 撞上宽泛关键词把成功标成失败）；
    - `read_file` 是内容型工具，成功返回的正文里可能含 "No such file"（如 SKILL.md 里
      的示例文本）→ 只认 deepagents 失败时必带的 "Error: " 前缀。
    """
    if status == "error":
        return True
    if not content_str:
        return False
    try:
        parsed = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        pass
    else:
        # 解析成 JSON 了：只认 success 字段（非 dict / 无该字段 → 视为无失败信号）
        return isinstance(parsed, dict) and "success" in parsed and not parsed["success"]
    lower = content_str.lower()
    if tool_name == "execute":
        return (
            "command failed with exit code" in lower
            or content_str.startswith("Execution error:")
        )
    if tool_name == "read_file":
        return content_str.startswith(("Error: ", "error: "))
    return any(
        marker in lower
        for marker in (
            "exception", "traceback", "failed", "failure",
            "timeout", "permission denied", "no such file",
        )
    )


# 从工具结果文本里抠 /reports/ 路径（write_file 之外的工具：execute / download_from_sandbox
# 的 JSON 或 shell 回显）。字符类比 \S+ 保守：到引号/空白/逗号/花括号/方括号为止，
# 否则会把 JSON 尾巴（`","size":15758`）或 shell 报错的 `':` 一起吞进路径 → 前端 404。
# 中文不在 \w 里，故用显式字符类而非 \w。
_REPORTS_PATH_RE = re.compile(r'/reports/[^"\s,}\]]+')


def _sanitize_generated_path(file_path: str) -> str:
    """清洗工具返回的文件路径（对称前端 deriveGeneratedFiles 清洗，2026-08-24 根治脏路径）。

    上游（模型/技能工具）返回的 /reports/ 路径可能尾随单引号/空白等脏字符——
    2026-08-24 实测 tech-spec-pdf-diff 返回 `.../diff.json'`（尾随单引号）：
    磁盘上根本不存在该文件 → 后端 emit file_generated → 前端 HEAD 探测 404
    → 红框"文件不可用"卡，连累整组报告卡片的预览/下载观感。
    前端 deriveGeneratedFiles 只过滤 `file_path.endsWith("/")`，漏掉尾随引号，
    故在后端 emit 前统一清洗（去首尾引号/空白）。清洗后再走磁盘查找/去重/emit，
    `diff.json'` 自动归正为 `diff.json`（磁盘存在 → 正常预览下载，且与真实条目去重）。

    2026-09-29 扩展（B① 前置条件）：追加剥离 `:` `;` `,` `` ` `` —— shell 报错回显里的
    路径常被"引号 + 冒号"包住，形如
        cp: cannot create regular file '/reports/u/s/x.pptx': No such file or directory
    而提取路径的正则 `[^"\\s,}\\]]+` 不把 `'` 当分隔符，会把尾部的 `':` 一起吞进路径
    （实测得到 `/reports/u/s/x.pptx':`）。不清掉这一步，后面的沙箱候选路径（
    /home/user/x.pptx）永远对不上 → B① 的"从沙箱拉回"必然失败。
    """
    if not file_path:
        return file_path
    return file_path.strip().strip("'\"` \t:,;")


async def _pull_report_from_sandbox(
    sandbox, file_path_virtual: str, user_id: str, session_id: str, filename: str
) -> int:
    """沙箱产物拉回宿主持久卷（2026-09-29 方案A：file_generated 兜底持久化）。

    背景（GY35377/60c64c4f 项目团队任命书.pptx 实测）：模型在沙箱内 execute
    生成的产物（python-pptx 等）落在**沙箱文件系统**，未走 download_from_sandbox
    拉回宿主；沙箱 5min 空闲 TTL 被 lifecycle-manager 回收后文件随之消失，
    但 file_generated 事件已按模型声称的 /reports/ 路径推送 → 前端 HEAD 404
    → 红框"文件不可用"。write_file 写 /reports/ 走 FilesystemBackend 直写
    持久卷本无此问题，漏网的只是"沙箱内 execute 产物"。

    策略：emit 前发现磁盘缺失（file_size==0）时，按候选路径从沙箱读回字节
    并落盘到 settings.report_root/{user}/{session}/——生成瞬间即持久化，
    之后沙箱回收无所谓，也不再依赖模型自觉调 download_from_sandbox。

    候选沙箱路径（按命中率排序）：
      1. 模型声称的虚拟路径本身（沙箱内 mkdir -p /reports/... 后写同一绝对路径）
      2. /home/user 下的相对写法（execute CWD 多为 /home/user）
      3. /home/user/{basename}、/tmp/{basename}（脚本就近输出）

    返回落盘后的文件大小；任何失败返回 0（不阻断 SSE 流，退化为现状 404，
    不会比修复前更差）。沙箱已回收时 download_files 全 file_not_found → 0。

    ⚠️ 必须 await + to_thread（2026-09-29 加固，随 1.0.20 发布）：
    `sandbox.download_files` 是**同步阻塞**方法（内部走网络读沙箱文件），直接调在
    async 生成器里会占住事件循环——沙箱已回收时（每条候选都要等到超时才返回）
    整个 worker 会僵住，**其他用户正在跑的 SSE 流一起卡**，不只影响当前会话。
    本仓既有约定见 `src/infra/trash.py:96`：「同步阻塞函数，调用方须用
    asyncio.to_thread 包住」。
    同时改为**逐条候选 + 命中即短路**：库内 `download_files` 会遍历传入的全部路径，
    原来一次传 4 条 = 无条件读 4 次；现在首条命中即停，省往返也缩短最坏路径。
    """
    if sandbox is None or not filename:
        return 0
    base_name = filename.rsplit("/", 1)[-1]
    # dict.fromkeys 去重且保序：候选 1 与候选 2 常常是同一条路径（模型照抄虚拟路径）
    candidates = list(
        dict.fromkeys(
            [
                file_path_virtual,
                f"/home/user/reports/{user_id}/{session_id}/{filename}",
                f"/home/user/{base_name}",
                f"/tmp/{base_name}",
            ]
        )
    )
    content = None
    tried: list[str] = []
    for cand in candidates:
        tried.append(cand)
        try:
            # to_thread：同步阻塞方法，绝不能在事件循环里直调（见 docstring）
            responses = await asyncio.to_thread(sandbox.download_files, [cand])
        except Exception as e:  # noqa: BLE001 — 断连/回收：换下一条候选，不重试同一路径
            logger.warning("[FILE_GEN] 沙箱拉回异常: path=%s err=%s", cand, e)
            continue
        for resp in responses or []:
            c = getattr(resp, "content", None)
            if c:
                content = c
                break
        if content:
            break
    if not content:
        logger.warning(
            "[FILE_GEN] 沙箱内未找到产物（可能沙箱已回收）: tried=%s", tried
        )
        return 0
    # 路径穿越防御（与 files.py 同款）：落盘必须限制在 report_root/uid/sid 内
    root = os.path.normpath(os.path.join(settings.report_root, user_id, session_id))
    disk_path = os.path.normpath(os.path.join(root, filename))
    if disk_path != root and not disk_path.startswith(root + os.sep):
        logger.warning("[FILE_GEN] 拉回路径越界拦截: %s", disk_path)
        return 0
    try:
        os.makedirs(os.path.dirname(disk_path), exist_ok=True)
        with open(disk_path, "wb") as f:
            f.write(content)
    except Exception as e:  # noqa: BLE001
        logger.warning("[FILE_GEN] 拉回写盘失败: %s err=%s", disk_path, e)
        return 0
    logger.info(
        "[FILE_GEN] 沙箱产物已拉回宿主: %s (%d bytes)", disk_path, len(content)
    )
    return len(content)


# ─── 沙箱交付物兜底回收（2026-09-29 方案 B②）───
# 触发场景：模型用 execute 在沙箱内生成交付物（python-pptx / pandas.to_excel 等），
# 却从未调用 download_from_sandbox 拉回宿主 → 宿主报告目录零产出，沙箱 5min TTL
# 回收后文件永失（GY35377/60c64c4f 项目团队任命书.pptx 实测）。
#   B① 按"工具结果里出现的路径"救（要求模型说得出路径）；
#   B② 按"沙箱文件系统差集"救（模型连路径都没说出来也能救）——本节即 B②。
#
# 只回收"像交付物"的扩展名：脚本/日志/缓存/依赖不该变成用户面前的报告卡片
# （模型常写 generate_ppt.py / output.log，那不是交付物）。
_SALVAGE_EXTS = (
    "md", "txt", "pdf", "docx", "doc", "xlsx", "xls", "csv", "pptx", "ppt",
    "html", "htm", "json", "xml", "yaml", "yml", "png", "jpg", "jpeg", "svg", "zip",
)
# 沙箱内产物高发目录：execute 的 CWD 默认 /home/user；脚本常写 /tmp；
# 模型被误导后还会自己 mkdir -p /reports/...（**沙箱内的同名目录**，不是宿主卷）
_SALVAGE_DIRS = ("/home/user", "/tmp", "/reports")
# 单轮兜底最多回收几个：避免一次性把沙箱垃圾全搬进来刷屏
_SALVAGE_MAX_FILES = 5
# B①：是否也解析"失败的工具结果"里的交付物路径。
# 报错文本里的 /reports/... 也可能指向真实产物（cp 因目标目录不存在而失败，源文件仍在），
# 跳过解析等于连"试着拉回"的机会都没有。安全性由 emit 护栏承担：
# **报错来源的路径只有真正拉回成功（file_size > 0）才允许 emit**（见下方 B① 护栏）。
_PARSE_ERROR_TOOL_RESULTS = True


def _sandbox_artifact_listing(sandbox) -> dict[str, int]:
    """列沙箱候选目录下的交付物候选 {绝对路径: 字节数}（**同步阻塞**，调用方须 to_thread）。

    `sandbox.execute` 内部走 e2b `commands.run`（同步网络调用），在事件循环里直调
    会占住整个 worker（同 `_pull_report_from_sandbox` docstring 的教训）。

    为什么要大小：前后快照做差集时，"覆盖写同名文件"（同路径、内容变了）也必须算
    本轮新产物；只比路径会漏。
    为什么不用时间戳：沙箱 MicroVM 与 agent 容器的时钟可能漂移，`-newermt` 会漏或
    多算；差集在本机算，无时钟依赖。
    枚举失败（沙箱回收/命令错误）返回 {} —— 调用方据此放弃兜底，不抛异常。
    注意这里是**fail-closed**：拿不到可信清单就不搬文件，宁可不救也不能把无关文件
    塞进用户报告目录（部分结果比空结果更危险）。
    """
    if sandbox is None:
        return {}
    name_clause = " -o ".join("-name '*.%s'" % e for e in _SALVAGE_EXTS)
    # ⚠️ 必须先过滤掉**不存在的**候选目录再交给 find（2026-09-29 真沙箱实测）：
    # 新沙箱里 `/reports` 不存在（它要模型自己 mkdir 才有），而 GNU find 遇到不存在的
    # 起始路径会以 **exit 1** 结束 → 整条命令被判失败 → 本函数返回 {} → 兜底**永久静默
    # 失效**（每个还没在沙箱内建过 /reports 的会话都中招，恰好是绝大多数）。
    # 实测对照：`find /home/user /tmp /reports ...` exit=1 输出空；
    #           `find /home/user /tmp ...`（仅存在目录）exit=0 且正确列出产物。
    # 目录全都不存在时 `if` 不执行、整体退出码为 0 → 返回 {}（无候选），语义正确。
    dirs_clause = " ".join(_SALVAGE_DIRS)
    cmd = (
        "D=''; for d in %s; do [ -d \"$d\" ] && D=\"$D $d\"; done; "
        "if [ -n \"$D\" ]; then find $D -maxdepth 4 -type f \\( %s \\) "
        "-not -path '*/__pycache__/*' -not -path '*/site-packages/*' "
        "-not -path '*/node_modules/*' -not -path '*/.cache/*' -not -path '*/.git/*' "
        "-printf '%%p\\t%%s\\n' 2>/dev/null; fi"
        % (dirs_clause, name_clause)
    )
    try:
        resp = sandbox.execute(cmd, timeout=30)
    except Exception as e:  # noqa: BLE001
        logger.warning("[FILE_GEN] 沙箱产物枚举异常: %s", e)
        return {}
    if getattr(resp, "exit_code", 0) != 0:
        logger.warning(
            "[FILE_GEN] 沙箱产物枚举 exit=%s: %s",
            getattr(resp, "exit_code", None),
            str(getattr(resp, "output", ""))[:200],
        )
        return {}
    listed: dict[str, int] = {}
    for line in (getattr(resp, "output", "") or "").splitlines():
        path, sep, size = line.rpartition("\t")
        if not sep or not path.startswith("/"):
            continue
        try:
            listed[path] = int(size)
        except ValueError:
            continue
    return listed


async def _salvage_sandbox_artifacts(
    sandbox, user_id: str, session_id: str, before: dict[str, int] | None
) -> list[dict]:
    """收尾兜底：把沙箱里**本轮新增/改写**的交付物拉回宿主持久卷。

    仅在「交付型任务 + 宿主报告目录本轮零新文件 + B① 也没救回」时调用一次
    （调用点有 `_salvage_done` 单次门）。返回已落盘条目（字段与 file_generated
    事件同构），无物可回收返回 []。

    `before` 为 None 表示**基线不可用**（开轮枚举失败）→ 直接放弃兜底：没有基线就
    分不清"本轮新产物"和"上一轮遗留"，硬拉会把旧文件算到本轮头上。
    注意 `{}` 与 None 语义不同：`{}` 是"沙箱当时确实没有候选文件"，可以正常兜底。

    与 `_pull_report_from_sandbox` 的分工：那个是"知道确切文件名，逐条试候选路径"；
    本函数是"不知道文件名，枚举沙箱前后差集"。两者都不抛异常——收尾兜底不能反过来
    把当轮搞挂；沙箱已回收时返回 []，退化为修复前行为，不会更差。
    """
    if sandbox is None or before is None:
        return []
    try:
        after = await asyncio.to_thread(_sandbox_artifact_listing, sandbox)
    except Exception as e:  # noqa: BLE001
        logger.warning("[FILE_GEN] 收尾兜底枚举失败: %s", e)
        return []
    new_paths = [p for p, size in after.items() if before.get(p) != size]
    if not new_paths:
        logger.info("[FILE_GEN] 收尾兜底：沙箱内无本轮新增交付物")
        return []
    # 大文件优先：正式交付物（报告/表格）通常远比零散小文件更可能是用户要的东西
    new_paths.sort(key=lambda p: (-after[p], p))
    root = os.path.normpath(os.path.join(settings.report_root, user_id, session_id))
    salvaged: list[dict] = []
    for src in new_paths[:_SALVAGE_MAX_FILES]:
        try:
            responses = await asyncio.to_thread(sandbox.download_files, [src])
        except Exception as e:  # noqa: BLE001
            logger.warning("[FILE_GEN] 收尾兜底拉回异常: src=%s err=%s", src, e)
            continue
        content = None
        for resp in responses or []:
            c = getattr(resp, "content", None)
            if c:
                content = c
                break
        if not content:
            continue
        base_name = src.rsplit("/", 1)[-1]
        # 路径穿越防御（与 _pull_report_from_sandbox 同款）：落盘限制在 report_root/uid/sid 内
        disk_path = os.path.normpath(os.path.join(root, base_name))
        if disk_path != root and not disk_path.startswith(root + os.sep):
            logger.warning("[FILE_GEN] 收尾兜底路径越界拦截: %s", disk_path)
            continue
        try:
            os.makedirs(os.path.dirname(disk_path), exist_ok=True)
            with open(disk_path, "wb") as f:
                f.write(content)
        except Exception as e:  # noqa: BLE001
            logger.warning("[FILE_GEN] 收尾兜底写盘失败: %s err=%s", disk_path, e)
            continue
        salvaged.append({
            "file_name": base_name,
            "file_path": "/reports/%s/%s/%s" % (user_id, session_id, base_name),
            "file_size": len(content),
            "file_type": _infer_file_type(base_name),
        })
        logger.warning(
            "[FILE_GEN] 收尾兜底：沙箱产物已拉回宿主 %s ← %s (%d bytes)",
            disk_path, src, len(content),
        )
    return salvaged


def _merge_disk_diff_into_generated(
    generated_files: list,
    disk_files: frozenset[str],
    report_root: str,
    user_id: str,
    session_id: str,
) -> list:
    """磁盘差集补全 generated_files（2026-08-21 根治）。

    背景：_generated_files 来自模型/工具返回的 file_path 解析，可能脏——模型把
    file_path 传成目录（漏文件名）时无法产出正确 file_name；而 snapshot_report_files
    的磁盘差集（_new_files）是 report_root 下的真实文件名，是唯一可靠来源。
    chat.py 旧实现算出差集却只用于 M3 完成门，保存消息时只用脏源 → 350e5f80 会话
    全卡片 (未知文件)。此处按 file_path 去重，缺的用磁盘真实文件补齐。
    """
    result = list(generated_files)
    base = os.path.join(report_root, user_id, session_id)
    for rel in disk_files:
        disk_full = os.path.join(base, rel)
        if not os.path.isfile(disk_full):
            continue  # snapshot 含子目录名，只补文件
        fp = f"/reports/{user_id}/{session_id}/{rel}"
        if any(g.get("file_path") == fp for g in result):
            continue  # 已有（工具解析正确）保留原项，不重复
        try:
            size = os.path.getsize(disk_full)
        except OSError:
            size = 0
        file_name = rel.rsplit("/", 1)[-1]
        result.append(
            {
                "file_name": file_name,
                "file_path": fp,
                "file_size": size,
                "file_type": _infer_file_type(file_name),
            }
        )
    return result


# ─── M3 完成门任务类型感知（2026-08-26 方案 A）───
# 完成门为"必须有文件交付物"的任务设计（报告/对比/导出等），对内容型任务
# （写散文/问答/解释，交付物=对话回复本身）零产出是正常结果，不应拦截。
# 判断基于用户原始请求 body.message（不能用拼了 path_hint 的 user_message——
# 里面含"报告输出:/reports/..."会误导命中报告关键词）。
_DELIVERABLE_STRONG = (
    "报告", "报表", "导出", "保存为", "保存到", "写入文件", "生成文件",
    "输出文件", "创建文档", "生成文档", "制作文档", "整理成文档",
    "工作簿", "交付物", "产物", "附件",
    "xlsx", "excel", "word", "pdf", "csv", "pptx", "zip", "docx",
)
# ⚠ 不含裸"输出"（2026-09-16 移除）：它在指令型/规则型提示里几乎都是"模型应当输出
#   （文字）"的意思，是**内容生成**的动词而非**文件交付**的动词。文件交付义已由强信号
#   「输出文件」「生成文件」「导出」覆盖。实测误伤见 _is_deliverable_task docstring。
_DELIVERABLE_VERBS = ("对比", "差异", "diff", "分析", "总结", "汇总", "整理", "生成", "制作", "创建")
# 组合式名词仅保留明确「产出物」语义的词；纪要/清单/大纲等常是输入材料或口头回复，
# 命中会把口头总结误判为交付任务（2026-08-26 实测"总结一下会议纪要"误伤）
_DELIVERABLE_NOUNS = ("报告", "报表", "表格", "文档", "文件", "工作簿")
# 子句切分：动词与名词必须落在**同一子句**才算组合命中（2026-09-16 新增）。
# 长提示/规范文档里动词与名词天然大量散落，跨句拼凑会稳定误命中（见 docstring）。
_CLAUSE_SPLIT = re.compile(r"[。！？；;\n]")


def _is_deliverable_task(text: str) -> bool:
    """判断用户请求是否要求文件交付物（决定是否启用 M3 零产出拦截）。

    启发式：强信号词（出现即判定）或「产出类动词 × 文件类名词」**同子句**共现。
    组合式避免"分析一下这个方案"这类口头任务误判——它命中动词但无数词。

    2026-09-16 两次收紧（会话 GY24428:de18ad37 生产实证）：
      1. 移除裸"输出"：该会话用户消息里的「严禁输出缺失位置的速度/位置设定」是**规则
         约束**，却让启发式命中；结果是纯视觉标注任务被当成文件交付任务，完成门在
         模型因输出超限截断后误报「未产出交付物 / 请检查是否遗漏 download_from_sandbox」。
      2. 改为同子句共现：原文里"输出"与"表格"（【表格/矩阵布局专属规则】）分处不同段落，
         跨句拼凑是这类长规范提示的稳定误命中源。收紧后真实会话判定 False，
         而"对比两份协议生成报告""整理成表格"等真实交付请求仍为 True。

    ⚠ 刻意保留的方向性偏差：收紧后可能漏判（如"帮我写个文档说明这个模块"——"写"不在
    动词表内）。漏判 = 完成门不介入 = 退回旧行为（零产出时只是不提示），而误判 =
    给用户一条归因错误的指控。两害相权取其轻（2026-09-16 用户拍板 A+C）。
    """
    t = (text or "").lower()
    if any(k in t for k in _DELIVERABLE_STRONG):
        return True
    return any(
        any(v in clause for v in _DELIVERABLE_VERBS)
        and any(n in clause for n in _DELIVERABLE_NOUNS)
        for clause in _CLAUSE_SPLIT.split(t)
    )


class ChatRequest(BaseModel):
    session_id: str = "default-session"
    message: str = ""
    model_override: dict | None = (
        None  # 可选，动态切换模型：{"model_name": "...", "base_url": "...", "api_key": "..."}
    )
    files: list[str] | None = None  # 可选，本轮上传的文件虚拟路径列表
    continue_from_state: bool = (
        False  # 为 true 时不新增用户消息，直接从当前 checkpoint 继续生成
    )
    mcp_servers: list[str] | None = (
        None  # 可选，本轮临时启用的 MCP 服务名列表（缺省 = 全部 enabled）
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 观测：让 LangGraph chain span 回到"真根"（2026-09-29 重做版）
# ═══════════════════════════════════════════════════════════════════════════════
# 背景：`_OpenInferenceOnlySpanProcessor`（src/core/tracing.py:71）按属性
# `openinference.span.kind` 丢弃 HTTP/ASGI span —— 这是**设计意图**（POST
# /api/v1/chat 这类基础设施 span 不该进 LLM trace）。但它只"丢父节点"、不"缝合树"：
# LangGraph 等业务 span 的 parent_span_id 仍指向那个被丢弃的 FastAPI server span
# ⇒ 到达后端的 span 里**没有任何根**。而两个后端在 trace 层都依赖根：
#
#   · Langfuse：trace 级 `name`/`input`/`output` 的回退源**只有「根观测」**
#     （未显式给 `langfuse.trace.*` 时取根 span 的 name / observation input），
#     无根 ⇒ 三列恒空。实测 138 条 trace 中 input/output 各仅 10 条有值，
#     且恰好是唯一那条有根的老 trace。
#   · Phoenix：`traces` 表根本没有 name/input 列，GraphQL `Trace.rootSpan` 在无根
#     trace 上实测返回 null ⇒ 树形/详情页退化。
#
# 退化时间线（trace 数据 × git 双向对齐，详见 .workbuddy/memory/2026-09-29.md）：
#   · 09-04 16:24 起，`5b476e3`（新增 FastAPIInstrumentor）第一次构建成镜像上线
#     ⇒ HTTP server span 成了 LLM trace 的根，name 退化成 "POST /api/v1/chat"、
#     input/output 变空；
#   · 09-11 起，`f3fd075`（引入本过滤器，它当时确实占了观测后端 98%）把 HTTP span
#     丢掉 ⇒ 根彻底消失，三列全空（现状）。
#   9.2/9.3 的"正常"是巧合而非设计：那时链路里压根没有 HTTP span，`LangGraph`
#   天然就是根，三列取的是它自己的值。
#
# 修复姿势：**不新建 span、不写任何 name/input/output**，只在 event_stream 期间把
# 当前 OTel Context 换成一个空 Context —— `get_current_span()` 随即是 INVALID_SPAN，
# 于是 LangChain 插桩创建的 `LangGraph` span 的 parent=None，天然成为真根；它自带
# `openinference.span.kind=CHAIN`，能过本过滤器；它的 name/input/output 正是
# 9.2/9.3 基线里 Langfuse 三列的取值来源 ⇒ 三列是**回退**出来的、与基线逐字一致。
#
# 为什么不沿用上一版的 `start_as_current_span(..., context=Context())`（已废弃）：
# 那样等于**另造一个根覆盖掉本来正确的回退源**，实测 name 退化成 "agent-run"、
# input 退化成纯文本；而且要自己复刻 LangChain 的消息序列化格式（messages_to_dict
# 那种 `{"type": "human", "data": {...}}`），框架一升级就失真。
#
# 为什么用 attach/detach 而非 `with use_span(...)`：event_stream 是 async generator，
# 作用域要跨 yield 边界；attach 拿 token、try/finally 显式 detach 语义最直白，也不会
# 把 Context 泄漏到 StreamingResponse 之后的调用栈。
#
# 实证依据（spike 均已跑通，可复现）：
#   .workbuddy/spikes/lf_root_cut.py       —— use_span(Context()) / attach(Context())
#                                             两种姿势均能切断与 HTTP span 的父子关系
#   .workbuddy/spikes/lf_async_ctx2.py     —— 跨 async generator + 客户端断连场景下
#                                             attach/detach 配对正确，子 span 落到新根下
#   .workbuddy/spikes/lf_baseline_902b.py  —— 9.2/9.3 基线：根观测 = LangGraph、
#                                             parent=null，三列取的就是它的值
# ═══════════════════════════════════════════════════════════════════════════════

try:
    from opentelemetry import context as _otel_context_api
    from opentelemetry.context import Context as _OtelContext
except ImportError:  # pragma: no cover — 观测依赖缺失时整体降级为"不干预"
    _otel_context_api = None  # type: ignore[assignment]
    _OtelContext = None  # type: ignore[assignment]


router = APIRouter()


@router.post("/chat")
async def chat(
    request: Request,
    body: ChatRequest,  # ← FastAPI 自动解析 JSON
    store=Depends(get_store),
    checkpointer=Depends(get_checkpointer),
    current_user: dict = Depends(get_current_user),  # 从 JWT 取当前用户
):
    user_id = current_user["user_id"]
    session_id = body.session_id
    thread_id = f"{user_id}:{session_id}"

    # ─── 并发护栏快照（2026-09-12）：会话在本轮开跑前是否存在？───
    # 用途见 _persist_session 开头的防复活守卫：若用户在轮次进行中删除会话，
    # 轮次结束时不得把刚删的 sessions 行 / messages 台账写回来（复活成孤儿）。
    # 快照=False（直连 /chat 且无 POST /sessions 的新会话）则保留"不存在则创建"
    # 的兼容路径。单行 store 读（store_pkey btree），每轮一次，成本可忽略。
    session_existed_at_start = (
        await store.aget(("sessions", user_id), session_id) is not None
    )

    # 按本轮透传的 mcp_servers 过滤 MCP 工具（缺省 = 全部 enabled）
    tools = await get_mcp_tools(body.mcp_servers)
    sandbox = create_sandbox(thread_id)
    logger.info("[DIAG] create_sandbox(thread_id=%s) → sandbox=%s", thread_id, type(sandbox).__name__ if sandbox else "None")

    # ─── M1 环境预检：快照注入 + 磁盘硬阈值拒绝（设计文档 M1）───
    # 快照缓存按 thread_id 60s 复用；无沙箱（本地模式）时 env_snapshot 为 None，跳过
    env_snapshot = get_env_snapshot(sandbox, thread_id)
    if env_snapshot is not None and env_snapshot.ok:
        disk = env_snapshot.disk_avail_mb
        if disk is not None and disk < settings.sandbox_disk_hard_mb:
            logger.warning(
                "[M1] 沙箱磁盘不足拒绝启动: user=%s, session=%s, avail=%sMB",
                user_id, session_id, disk,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    f"沙箱磁盘可用空间不足（{disk}MB < {settings.sandbox_disk_hard_mb}MB），"
                    "请清理沙箱后重试"
                ),
            )
        if disk is not None and disk < settings.sandbox_disk_warn_mb:
            logger.warning("[M1] 沙箱磁盘偏低: user=%s, session=%s, avail=%sMB", user_id, session_id, disk)
    # 构建 skill sources：系统 + Agent 自创（启动时加载）+ 当前用户的共享 skill
    base_skills = getattr(request.app.state, "skills", [])
    user_skill_path = f"/skills/__user_{user_id}__/"
    skills = list(base_skills) + [user_skill_path]
    agent = await create_agent(
        user_id=user_id,
        session_id=body.session_id,
        thread_id=thread_id,
        store=store,
        sandbox=sandbox,
        checkpointer=checkpointer,
        tools=tools,
        skills=skills,
    )

    sandbox_id = ""
    if sandbox is not None:
        try:
            sandbox_id = sandbox.sandbox_id
            logger.info("[DIAG] sandbox_id 提取成功: %s", sandbox_id)
        except Exception as e:
            logger.warning("[DIAG] sandbox_id 提取失败: %s", e)
    else:
        logger.warning("[DIAG] sandbox 为 None（create_sandbox 返回空），sandbox_id 未设置")
    
    if not sandbox_id:
        logger.warning("[DIAG] sandbox_id 最终为空，upload_to_sandbox 等工具将无法使用")

    path_hint = (
        f"沙箱 ID：{sandbox_id}\n"
        f"当前用户：{current_user.get('display_name', user_id)}（{current_user.get('role', 'user')}）\n"
        f"\n"
        f"【当前会话路径】\n"
        f"输入文件：/uploads/{user_id}/{session_id}/\n"
        f"报告输出：/reports/{user_id}/{session_id}/\n"
        # ─── 交付物写入通道（2026-09-29 方案 A）───
        # 背景：GY35377/60c64c4f 实测——模型用 execute 在沙箱内生成 pptx，再在**沙箱内**
        # `mkdir -p /reports/... && cp ...`（exit 0）就宣称"已保存、可直接下载"，实际宿主
        # 报告目录一个文件都没有，用户点下载必然 404。
        # 根因是**同一段路径字符串在两套后端下含义不同**：write_file 走 FilesystemBackend
        # 直写宿主报告卷；execute 走 sandbox 默认后端（agent.py:690 注释"execution is not
        # path-routable"），而沙箱**未挂载任何宿主目录**（sandbox.create_sandbox 无 mount）。
        # 明说通道边界，从源头掐掉这类幻觉（不替代 B 的兜底，二者互补）。
        f"⚠️ 交付物写入通道（务必遵守，弄错用户会拿到不存在的文件）：\n"
        f"  · 写报告/文档/表格等交付物 → **只能用 write_file/write 工具**写 "
        f"/reports/{user_id}/{session_id}/...；该虚拟路径直通宿主报告目录，写完即可下载。\n"
        f"  · **禁止用 execute/shell 往 /reports/... 或 /uploads/... 写文件**：这两个目录是"
        f"虚拟路径，沙箱里**没有挂载**它们（execute 走沙箱文件系统，不参与路径路由）。"
        f"shell 里的 mkdir/cp 到 /reports/... 只落在沙箱内部，宿主报告目录不会有任何文件，"
        f"而且沙箱空闲约 5 分钟即被回收，文件随即消失。\n"
        f"  · 若确实要用 shell 生成文件（如 python-pptx / pandas.to_excel）：产物先落在沙箱"
        f"（如 /home/user/xxx.pptx），**必须再用 download_from_sandbox 拉回** "
        f"/reports/{user_id}/{session_id}/ —— 这是 shell 产物唯一的交付通道。\n"
        f"  · 同理，沙箱里也看不到 /uploads/... 的用户文件：读它们用 read_file/ls，"
        f"或先 upload_to_sandbox 把文件送进沙箱再处理。\n"
        f"  · **未真正看到 download_from_sandbox 或 write_file 的成功回执前，"
        f"禁止声称文件\"已保存/已生成/可直接下载\"**；不确定就如实说明缺少哪一步。\n"
    )

    # M1：环境快照注入（系统自动探测，模型不应自行重装/探测环境）
    if env_snapshot is not None:
        path_hint += (
            f"\n【沙箱环境】（系统自动探测，仅需直接使用，勿重复安装或探测）\n"
            f"{env_snapshot.to_hint()}\n"
        )

    # 本轮文件提示（多轮对话时 Agent 只处理本轮上传的文件）
    file_hint = ""
    if body.files:
        file_list = "\n".join(f"- {f}" for f in body.files)
        file_hint = f"\n用户为本轮对话上传了以下文件（路径已映射到虚拟文件系统，请精确处理这些文件）：\n{file_list}"

    user_message = f"{path_hint}\n{body.message}{file_hint}"

    # 构造 graph 输入：
    graph_config = {"configurable": {"thread_id": thread_id}}

    # - 正常发送：新增用户消息
    # - 编辑后重发：从 PostgresStore 读取截断后的消息列表重建 graph 状态
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

    if body.continue_from_state:
        try:
            msg_namespace = ("messages", user_id, session_id)
            msg_prefix = ".".join(msg_namespace)
            # 取全（而非只取最新一页）：这里要把历史**整段**灌回图，截短即丢上下文。
            # 增量格式下每个条目就是一条消息（旧格式是单行 {"items": [...]}）。
            _rows = await store.alist_all_messages(msg_prefix)
            stored_items = [v for _k, v in _rows if isinstance(v, dict)]
            lc_msgs = []
            for m in stored_items:
                if m.get("role") == "user":
                    lc_msgs.append(HumanMessage(content=m.get("content", "")))
                elif m.get("role") == "ai":
                    stored_reasoning = m.get("reasoning", "")
                    ai_kwargs = {}
                    if stored_reasoning:
                        ai_kwargs["reasoning_content"] = stored_reasoning
                    lc_msgs.append(AIMessage(
                        content=m.get("content", ""),
                        additional_kwargs=ai_kwargs,
                    ))
            graph_input = {"messages": lc_msgs}
            logger.info("continue_from_state: 从 store 重建 %d 条消息", len(lc_msgs))
        except Exception as e:
            logger.warning(
                "continue_from_state: 读取 store 失败, fallback 到 checkpoint: %s", e
            )
            latest = await agent.aget_state({"configurable": {"thread_id": thread_id}})
            graph_input = (
                {"messages": latest.values.get("messages", [])} if latest else None
            )
    else:
        graph_input = {"messages": [{"role": "user", "content": user_message}]}

    async def _persist_session(
        agent,
        thread_id: str,
        user_id: str,
        session_id: str,
        store,
        body,
        *,
        generated_files: list | None = None,
        disk_files: frozenset[str] | None = None,
        completion_blocked: bool = False,
        terminal_fallback: dict | None = None,
        reasoning_started_at_ms: int | None = None,
        reasoning_ended_at_ms: int | None = None,
        turn_started_at_ms: int | None = None,
        turn_ended_at_ms: int | None = None,
        reason: str = "normal",
    ) -> int:
        """把 agent checkpoint 中的消息保存到会话历史（store）。

        由 event_stream 两处调用：
        - 正常路径（reason="normal"）：SSE 流完整结束
        - 断连/取消路径（reason="interrupted"）：生成器被 GeneratorExit/CancelledError
          打断时在 finally 中强制保存，防止用户刷新/关闭页面后本轮消息丢失
          （2026-08-11 15:26 实测：断连后"写 skill"指令未保存，前端重拉即消失）。
        函数内部只有 await 没有 yield，可在 finally 块中安全调用。

        时间戳参数（reasoning_started/ended/turn_started/ended）来自 event_stream
        流式阶段埋点（2026-09-08 加）：首个 reasoning 事件 → 推理起始；首个 token
        事件 → 推理结束；while 循环 break → turn 结束。持久化到 AI 消息 entry 的
        reasoning_duration_ms / turn_duration_ms / reasoning_started_at 三个字段，
        让前端 ReasoningBlock 跨刷新稳定显示秒数（对齐 deer-flow 风格）。
        """
        generated_files = generated_files or []
        if disk_files:
            # 磁盘差集补全：工具解析的 generated_files 可能脏（模型把 file_path 传成
            # 目录），用 report_root 磁盘真实文件名补缺（2026-08-21 根治）
            generated_files = _merge_disk_diff_into_generated(
                generated_files, disk_files,
                settings.report_root, user_id, session_id,
            )
        # ─── 并发护栏：会话在本轮进行中被删除 → 本轮不持久化（2026-09-12）───
        # session_existed_at_start 是 chat 端点在 agent 开跑前的快照（闭包变量）：
        # 快照=True 且现在行没了 → 用户在轮次进行中删除了会话。此时写台账会把
        # 刚被原子删除的 sessions 行 + messages 复活成孤儿（删了又回来，且
        # checkpoint 里的旧上下文也被重建）。快照=False（直连 /chat 的新会话）
        # 则照常走下方"不存在则创建"的兼容路径，不受影响。
        if session_existed_at_start:
            if await store.aget(("sessions", user_id), session_id) is None:
                logger.info(
                    "会话 %s 在本轮进行中被删除，跳过持久化（防复活护栏）", session_id
                )
                return 0
        try:
            # 读取最终状态中的消息（断连兜底路径下 agent 图可能仍在写 checkpoint，
            # 用带重试的读取防御瞬时争用导致的 "another command is already in progress"）
            state = await _aget_state_with_retry(agent, thread_id)
            logging.warning(
                "[DIAG] %s: state=%s, has_values=%s",
                "SSE 结束" if reason == "normal" else "SSE 中断强制保存",
                type(state).__name__ if state else None,
                hasattr(state, "values") if state else False,
            )
            if state and hasattr(state, "values"):
                all_msgs = state.values.get("messages", [])
                # 第一遍：建立 tool_call_id -> ToolMessage content 的映射
                # 用于在保存 AIMessage 的 tool_calls 时填入 result 字段
                tool_results: dict[str, str] = {}
                for m in all_msgs:
                    if getattr(m, "type", None) == "tool":
                        tc_id = getattr(m, "tool_call_id", None)
                        if tc_id:
                            tool_results[tc_id] = str(m.content) if m.content else ""

                # 提取人类可读的消息（只保留 user / assistant / tool 角色的核心信息）
                history = []
                for msg in all_msgs:
                    role = getattr(msg, "type", "unknown")
                    if role == "human":
                        role = "user"
                    content = str(msg.content) if msg.content else ""
                    reasoning = ""

                    # 1. 从 additional_kwargs 提取推理内容（DeepSeek/Groq/Ollama/XAI 等）
                    if hasattr(msg, "additional_kwargs") and msg.additional_kwargs:
                        kw_reasoning = msg.additional_kwargs.get("reasoning_content") or ""
                        if kw_reasoning:
                            reasoning = kw_reasoning

                    # 2. Qwen 系：从 content 中移除 </think> 段
                    # 关键：每条 AI 消息独立切分（之前的 `if not reasoning` 会让后一条
                    # AI 消息的 <think> 段残留到 content 里）
                    if "</think>" in content:
                        think_end = content.find("</think>")
                        thinking_part = content[:think_end]
                        remaining = content[think_end + 8:]  # skip </think>
                        if remaining.startswith("\n"):
                            remaining = remaining[1:]
                        if thinking_part.strip():
                            # 保留 original reasoning（如有），附加本次 <think> 段
                            if reasoning:
                                reasoning = reasoning + "\n\n" + thinking_part
                            else:
                                reasoning = thinking_part
                            content = remaining

                    # 3. 长度上限（2026-09-12 修：原为 content[:2000]/reasoning[:2000]）
                    #    截断写在持久化层＝写入即不可逆删除，方向错了层（且引入提交
                    #    4a92447 未说明原因）。正文/推理是用户要看的交付物本体，默认不限制；
                    #    tool 消息正文属中间过程走中间上限。
                    #    ⚠ 本循环遍历**所有角色**，role=tool 的独立条目同样命中此处，
                    #    故必须按 role 分流，否则工具输出会被一并放开。
                    content = _clip(
                        content,
                        settings.persist_max_tool_result_chars
                        if role == "tool"
                        else settings.persist_max_content_chars,
                    )
                    reasoning = _clip(reasoning, settings.persist_max_content_chars)

                    # 4. 去掉 user 消息中的 path_hint 前缀
                    if role == "user" and "\n\n" in content:
                        parts = content.rsplit("\n\n", 1)
                        content = (
                            parts[-1].strip() if len(parts) > 1 else parts[0].strip()
                        )

                    # 5. 不再跳过中间 AI 消息
                    #    之前跳过的初衷是避免"只有 thinking 折叠块 + 空正文"的奇怪气泡，
                    #    但这会丢失 tool_call 渲染（流式阶段用户能看到 write_file 的 tool_call
                    #    卡片 + 文件卡片，刷新后这部分消失，体验不一致）。
                    #    现在保留所有 AI 消息：中间 AI 消息显示 thinking + ToolCallCard +
                    #    GeneratedFileCard，最终 AI 消息显示 thinking + 真正的回复内容。

                    entry = {
                        "id": getattr(msg, "id", None),
                        "role": role,
                        "content": content,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    if reasoning:
                        entry["reasoning"] = reasoning
                    # AI 消息附带 tool_calls 信息
                    if role == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
                        entry["tool_calls"] = [
                            {
                                "id": tc.get("id") or f"tc-{i}",
                                "tool": tc["name"],
                                "args": tc["args"],
                                "status": "success",
                                "result": _clip(
                                    tool_results.get(tc.get("id", "")) or "",
                                    settings.persist_max_tool_result_chars,
                                ),
                            }
                            for i, tc in enumerate(msg.tool_calls)
                        ]
                    # 持久化时长字段（2026-09-08 加，对齐 deer-flow）：
                    # reasoning_duration_ms：本条 AI 消息的推理时长（首个 reasoning 事件 →
                    #   首个 token 事件）。仅当 reasoning 存在 + 两个时间戳都有值时写入。
                    # reasoning_started_at：推理起始的 ISO 时间戳，供前端 ReasoningBlock
                    #   锚定（防组件挂载漂移），与 reasoning_duration_ms 配合使用。
                    if (
                        role == "ai"
                        and reasoning
                        and reasoning_started_at_ms is not None
                        and reasoning_ended_at_ms is not None
                    ):
                        entry["reasoning_duration_ms"] = max(
                            0, reasoning_ended_at_ms - reasoning_started_at_ms
                        )
                        entry["reasoning_started_at"] = datetime.fromtimestamp(
                            reasoning_started_at_ms / 1000, tz=timezone.utc
                        ).isoformat()
                    history.append(entry)

                # 持久化 turn_duration_ms（整轮耗时）：仅写到 history 最后一条 AI 消息。
                # turn_start = event_stream 进入时刻；turn_end = while 循环 break / 兜底 now。
                # 仅当两个时间戳都有值时写入（断连路径兜底时刻已设，正常路径 break 前已设）。
                if turn_started_at_ms is not None and turn_ended_at_ms is not None:
                    turn_duration_ms = max(0, turn_ended_at_ms - turn_started_at_ms)
                    for i in range(len(history) - 1, -1, -1):
                        if history[i].get("role") == "ai":
                            history[i]["turn_duration_ms"] = turn_duration_ms
                            break

                # 循环结束后，将 generated_files 关联到合适的 AI 消息
                # 策略：优先附加到第一条有 tool_calls 的 AI 消息（与流式阶段一致——
                #  file_generated 事件在 tool_call 后立即到达，前端把文件卡片加到
                #  当前最后一条 AI 消息，也就是发起 tool_call 的那条）。如果没有任何
                #  AI 消息带 tool_call（比如文件由其他方式生成），fallback 到最后一条
                #  AI 消息。
                if generated_files:
                    target_idx = None
                    for i, e in enumerate(history):
                        if e.get("role") == "ai" and e.get("tool_calls"):
                            target_idx = i
                            break
                    if target_idx is None:
                        for i in range(len(history) - 1, -1, -1):
                            if history[i].get("role") == "ai":
                                target_idx = i
                                break
                    if target_idx is not None:
                        history[target_idx]["generated_files"] = list(generated_files)

                # M3 完成门：零产出时在最后一条 AI 消息上标记失败状态，
                # 前端可据此展示"未产出交付物"徽标，避免"状态不明"
                if completion_blocked:
                    for i in range(len(history) - 1, -1, -1):
                        if history[i].get("role") == "ai":
                            history[i]["completion"] = "blocked_no_output"
                            break

                # 空响应降级（2026-09-16）：失败点在**模型层**（思考吃满输出上限被截断），
                # 不是"产物没拉回来"，故不与上面共用 blocked_no_output 标记——两者在
                # 复盘/取证时要能一眼分开（旧实现会把 de18ad37 这类截断也标成"零交付物"）。
                if terminal_fallback:
                    for i in range(len(history) - 1, -1, -1):
                        if history[i].get("role") == "ai":
                            history[i]["completion"] = "model_output_truncated"
                            history[i]["stop_reason"] = terminal_fallback.get("branch")
                            break

                # ─── 存入 store：增量追加（2026-09-12 方案 B，替代全量覆盖写）───
                # 改前：`aput(ns, "messages", {"items": history})` —— 每轮把整份 history
                # 当一个 value 覆盖写单行。三个后果（生产实测）：
                #   ① 爆炸半径=整个会话：最大会话 259 条 / 1.32 MB 挤一行，写失败即全丢；
                #   ② 写放大 130×：累计约 171 MB 只为存下 1.32 MB；
                #   ③ 摘要裁剪传导：checkpoint 被 RemoveMessage 裁短 → 下一轮快照缩水
                #      → 全量覆盖把缩水写进 store，早期对话在任何一层都找不回。
                # 改后：一条消息一行（key = 零填充序号），只写新增部分。
                #   `created_at` 顺带修掉一个隐性 bug：改前每轮给**所有**历史消息重写
                #   `created_at = now()`，前端看到的每条历史消息时间其实是"最后一次保存
                #   时刻"；增量下已归档条目不再被触碰。
                msg_namespace = ("messages", user_id, session_id)
                msg_prefix = ".".join(msg_namespace)
                # 第三个返回值是"旧格式残留条数"——部署顺序防线，见 aget_message_watermark
                prev_count, last_stored_id, legacy_rows = await store.aget_message_watermark(
                    msg_prefix
                )
                stored_message_count: int | None = None
                if legacy_rows:
                    # 存量还是旧格式（新版已上线、迁移脚本尚未执行）：**本轮不写**。
                    # 写了会让新旧格式并存，迁移脚本的前置守卫随即拒绝执行 → 需要人工对账。
                    # 代价仅限于"这一轮的回放副本"（checkpoint 不受影响，对话照常继续）。
                    logger.error(
                        "[DIAG] 会话 %s 存量仍为旧格式（%d 条非序号条目），本轮跳过 "
                        "store 写入；请先执行 migrate_messages_incremental.py 展开存量",
                        session_id, legacy_rows,
                    )
                else:
                    next_seq, new_items, append_mode = _plan_message_append(
                        history, prev_count, last_stored_id,
                    )
                    _warn_oversized_items(new_items, prefix=msg_prefix)

                    if new_items:
                        await store.awrite_messages(
                            msg_namespace,
                            [
                                (message_key(next_seq + offset), entry)
                                for offset, entry in enumerate(new_items)
                            ],
                        )
                    elif append_mode == "none" and prev_count > 0:
                        # 既没按 id 定位到，history 也不比已存的长 → 本轮无法安全追加。
                        # 不写是安全方向（宁可少写一轮，不可改写已归档内容），但要可见。
                        logger.warning(
                            "[DIAG] 增量追加跳过：无法定位水印且 history(%d) 未超过已存(%d)，"
                            "session=%s last_stored_id=%r",
                            len(history), prev_count, session_id, last_stored_id,
                        )

                    # 会话内实际可见条数 = 已存条数 + 本次新增（压缩后 history 会变短，
                    # 但 store 保留全部原文，元数据应与 store 实际条目一致而非 history 长度）
                    stored_message_count = prev_count + len(new_items)
                    logger.warning(
                        "[DIAG] %s: session=%s 增量追加 prev=%d new=%d mode=%s",
                        "会话保存完成" if reason == "normal" else "断连强制保存完成",
                        session_id, prev_count, len(new_items), append_mode,
                    )

                # 更新会话元数据（标题、消息数、时间）
                session_ns = ("sessions", user_id)
                item = await store.aget(session_ns, session_id)
                now_ts = datetime.now(timezone.utc).isoformat()

                # 用用户实际输入作为默认标题
                title = (
                    body.message[:50] + ("..." if len(body.message) >= 50 else "")
                    if body.message
                    else "新会话"
                )

                if item is not None:
                    data = item.value
                    # 旧格式残留时 stored_message_count 为 None → 保持原值不动
                    # （若写 0 会把会话列表的条数徽标清零，属于额外伤害）
                    if stored_message_count is not None:
                        data["message_count"] = stored_message_count
                    data["updated_at"] = now_ts
                    old_title = data.get("title", "")
                    # 覆盖旧的 path_hint 标题，或首次设置标题
                    if (
                        old_title.startswith("沙箱 ID")
                        or old_title in ("新会话", "新对话")
                        or not old_title
                    ):
                        data["title"] = title
                else:
                    # 会话不存在则创建（兼容直接调 /chat 而非 POST /sessions 的场景）
                    data = {
                        "title": title,
                        "created_at": now_ts,
                        "updated_at": now_ts,
                        "message_count": stored_message_count or 0,
                    }

                # 会话条目即唯一数据源：GET /sessions 按 namespace 前缀直接检索，
                # 不再需要维护 __index__ 手工索引（少一次非原子写，消除数据/索引分叉面）。
                await store.aput(session_ns, session_id, data)
                logger.warning(
                    "[DIAG] %s: user=%s, session=%s, msgs=%s(stored)",
                    "会话保存完成" if reason == "normal" else "断连强制保存完成",
                    user_id,
                    session_id,
                    stored_message_count,
                )
                return stored_message_count or 0
        except Exception as e:
            logger.error(
                "保存会话消息失败（关键错误，本轮消息可能丢失）: %s", e, exc_info=True
            )
        return 0

    async def _astream_with_heartbeat(stream, interval: float = 15.0):
        """把 agent.astream 包成带静默保活心跳的异步生成器。
    
        模型长思考不吐字节时（如 Qwen <think> 静默数分钟），TCP 连接会空闲超过代理
        proxy_read_timeout（openresty / AC gateway 常见 60s）→ 被掐断 → 前端"思考消失、
        无输出"。每 interval 秒无新 chunk 则 yield 心跳哨兵，消费方转成 SSE 注释行
        ": ping\\n\\n" 保活连接（字节照传、前端忽略）。
    
        参考 deer-flow StreamBridge.subscribe(heartbeat_interval=15) → sse_consumer 转
        ": heartbeat\\n\\n"。关键：用 asyncio.wait 竞速——token 先到则立即发（零延迟），
        满 interval 秒无字节才发心跳；只 cancel 静默 sleep task，**绝不 cancel 生成器
        task**，否则会破坏 agent.astream 这个异步生成器（asyncio.wait_for 超时则会
        cancel 进生成器内部，导致后续 __anext__ 不可用）。
        """
        _anext = asyncio.ensure_future(stream.__anext__())
        try:
            while True:
                _sleep = asyncio.ensure_future(asyncio.sleep(interval))
                _done, _ = await asyncio.wait(
                    {_anext, _sleep}, return_when=asyncio.FIRST_COMPLETED
                )
                if _anext not in _done:
                    # 满 interval 秒无 chunk：发心跳，保持同一个生成器继续等下一项
                    _sleep.cancel()
                    yield ("__heartbeat__", None)
                    continue
                # token 先到：取消静默 sleep，取出真实 chunk 正常下发
                _sleep.cancel()
                try:
                    item = _anext.result()
                except StopAsyncIteration:
                    break
                yield item
                _anext = asyncio.ensure_future(stream.__anext__())
        finally:
            _anext.cancel()


    async def _event_stream_inner():
        invoke_kwargs = {}
        # 如果传了 model_config，通过 runtime context 传给 switch_model middleware
        if body.model_override:
            invoke_kwargs["context"] = {"model_config": body.model_override}

        # 标记当前是否刚发出过 thinking 事件
        thinking_emitted = False
        # 标记当前是否刚发出过 generating 事件（答案 token 阶段，区别于 thinking/CoT）
        generating_emitted = False
        # ─── 本轮时长埋点（持久化到 AI 消息 metadata，跨刷新保留秒数显示）───
        # _turn_started_at_ms：event_stream 进入时刻（每个 SSE 连接新建一次）
        # _reasoning_started_at_ms：首个 reasoning 事件触发时刻（首字节推理到达）
        # _reasoning_ended_at_ms：首个 token 事件触发时刻（推理结束 / 进入答案生成）
        # _turn_ended_at_ms：while 循环 break 时由内层 finally 兜底设置
        _turn_started_at_ms = int(time.time() * 1000)
        _reasoning_started_at_ms: int | None = None
        _reasoning_ended_at_ms: int | None = None
        _turn_ended_at_ms: int | None = None
        # [DEBUG] 记录上一个 langgraph_step，避免逐 token 重复打印
        _last_debug_step = None
        # 记录本次流式生成过程中产生/下载的文件
        # 后续保存到 store 时关联到对应的 AI 消息 entry，确保刷新页面后还能看到文件卡片
        _generated_files: list[dict] = []
        # 已推送 file_generated 事件的 file_path 集合（同会话内同一文件只推一次，
        # 覆盖写/重复 write_file 不再触发重复卡片——2026-08-11 16:00 实测重复报告）
        _emitted_files: set[str] = set()
        # 工具连续失败计数（跨 astream 轮共享声明，实际每轮在 _drain_astream 内重置）
        _consecutive_tool_failures = 0
        # M3 完成门：astream 前快照 /reports/ 目录，结束后做差集判定本轮产出
        _before_files = snapshot_report_files(settings.report_root, user_id, session_id)
        # 每轮 after-before 差集（磁盘真实文件名）；断连路径（finally）也可能引用，
        # 故在循环前初始化，避免 GeneratorExit 早抛时 NameError（2026-08-21）
        _new_files: frozenset[str] = frozenset()
        _completion_blocked = False
        # TerminalResponseMiddleware 降级信号（2026-09-16）：本轮以「空响应降级」收尾时
        # 由中间件登记、这里取走。命中即说明**模型层没有产出**（而非下游漏了文件步骤），
        # 故跳过 M3 完成门的零产出拦截，改发 fallback_notice() 的诚实文案，避免归因错位。
        # 见 src/core/terminal_response.py 模块头「与 M3 完成门的分工」。
        #
        # 生命周期铁律：信号的有效期 = 一轮 SSE。开轮先清残留（上一轮若在守卫登记后
        # 立刻断连、没走到消费点，它不会自己消失——留着会被**下一轮**当成自己的信号，
        # 把一轮正常的回复误报成"模型未产出"）。断连兜底路径同样要取走，见下方 finally。
        pop_fallback_signal(thread_id)
        _terminal_fallback: dict | None = None
        # M3 任务类型感知（方案 A）：仅"要求文件交付物"的任务启用零产出拦截，
        # 内容型任务（散文/问答）交付物即回复本身，零产出直接放行。
        _is_deliverable = _is_deliverable_task(body.message)
        # ─── B② 收尾兜底基线：沙箱侧交付物快照（2026-09-29）───
        # 失败置 None = 基线不可用 → 禁止兜底：没有基线就分不清"本轮新产物"与
        # "上一轮遗留"，硬拉会把旧文件算到本轮头上，制造假卡片（比不救更糟）。
        #
        # 取值条件**不限于** _is_deliverable 轮次（2026-09-29 A+B 改为"有沙箱就取"）：
        # B 的触发源是**轮中**才出现的 execute 越界写拒绝，而基线必须在轮首取——
        # 词表漏判时（60c64c4f 那轮英文 "appointment PPT" 未命中 pptx 强信号，
        # _is_deliverable=False）事后无法回头补基线，B 会全程失效。
        # 代价是每个带沙箱的轮次多一次 find（亚秒级），换来"任意轮次都能救回沙箱产物"，
        # 也让 B② 兜底不再受关键词词表漏判影响。
        _before_sandbox_files: dict[str, int] | None = None
        _salvage_done = False
        if sandbox is not None:
            try:
                _before_sandbox_files = await asyncio.to_thread(
                    _sandbox_artifact_listing, sandbox
                )
                logger.debug(
                    "[FILE_GEN] 沙箱产物基线: %d 个候选文件", len(_before_sandbox_files)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[FILE_GEN] 沙箱产物基线枚举失败（放弃收尾兜底）: %s", e)
                _before_sandbox_files = None
        # graph_input 是 chat() 外层变量；event_stream 内若要重新赋值（完成门自动
        # 继续轮注入 SystemMessage），必须用独立局部变量 _graph_input，否则
        # Python 会把 graph_input 判定为 event_stream 局部变量，首轮读取即
        # UnboundLocalError（2026-08-04 M3 重构回归，已修复）
        _graph_input = graph_input

        # ─── 流式生成（多轮共用：首轮 + 完成门自动继续轮）───
        # P0 无进展循环检测状态（跨轮共享，收敛注入后重置）：
        # _last_tool_sig / _repeat_count：连续同一工具意图计数；
        # _files_in_window：本轮重复窗口内是否有新交付物（有则不算空转）
        _last_tool_sig = None
        _repeat_count = 0
        _files_in_window = 0
        _no_progress_injections = 0
        async def _drain_astream(_input):
            """消费一轮 agent.astream，实时 yield SSE 事件；内部吞掉异常不崩流。
            M3-v2 继续轮传入 {'messages': [SystemMessage(...)]} 追加系统消息，
            依赖 deepagents astream 继续模式（spike 验证，未通过时自动继续保持关闭）。
            """
            nonlocal _last_debug_step, thinking_emitted, generating_emitted, _generated_files, _consecutive_tool_failures, _reasoning_started_at_ms, _reasoning_ended_at_ms
            nonlocal _last_tool_sig, _repeat_count, _files_in_window, _no_progress_injections, _graph_input, _no_progress_triggered
            _consecutive_tool_failures = 0  # 每轮 astream 重新计数（完成门继续轮独立统计）
            # ─── 清洗：messages 里不应有 SystemMessage ───
            # 主 system 由 deepagents 的 system_message 字段承载（factory 最内层
            # 前置 + memory middleware append_to_system_message），messages 中出现
            # system 均为异常数据：历史 checkpoint 可能残留旧版 P0 收敛注入的
            # SystemMessage（2026-08-19 09:44 旧代码注入后被保存进 state，09:57
            # 重启恢复后第一轮 model 调用即 400 'System message must be at the
            # beginning'——vLLM 要求 system 连续位于开头）。统一过滤掉。
            _raw_msgs = _input.get("messages", [])
            _filtered_system = sum(
                1 for m in _raw_msgs if getattr(m, "type", "") == "system"
            )
            if _filtered_system:
                logger.warning(
                    "[DIAG] astream 输入已清洗 %d 条中间 SystemMessage（checkpoint 残留）",
                    _filtered_system,
                )
            _input = {
                **_input,
                "messages": [
                    m for m in _raw_msgs if getattr(m, "type", "") != "system"
                ],
            }
            # 使用 try/except 保护，防止 agent.astream 内部异常导致 SSE 流中断
            try:
                _stream = agent.astream(
                    _input,
                    config={
                        **graph_config,
                        "recursion_limit": 200,  # ← 最多 200 步（原 100）：大文档任务（80+页×2 PDF 逐章节提取）实测 100 步不够，报告都没来得及写；正常流程 25-35 步，200 是 5-8 倍余量
                    },
                    stream_mode=["messages", "updates"],
                    **invoke_kwargs,
                )
                # 包静默保活心跳：模型长思考不吐字节时发 SSE 注释行维持 TCP 连接，
                # 防止代理 proxy_read_timeout（60s）掐断 → 前端思考消失/无输出。
                async for mode, data in _astream_with_heartbeat(_stream, 15.0):
                    if mode == "__heartbeat__":
                        yield ": ping\n\n"
                        continue

                    if (
                        mode == "messages"
                    ):  # mode == "messages" 时，data 是 (AIMessageChunk, metadata) 的元组
                        token, metadata = data
                        # 用 langgraph_step 变化检测"新一次 LLM 调用"，替代不存在的 run_id
                        if metadata:
                            step = metadata.get("langgraph_step")
                            node = metadata.get("langgraph_node")
                            # 只在 model 节点发送 ai_message_start（避免 tool 节点空触发）
                            if step is not None and step != _last_debug_step and node == "model":
                                # 新 step 到来前先通知前端：上一条 AI 消息结束
                                # 这是关键：让前端能正确分割多条 AIMessage
                                if _last_debug_step is not None:
                                    yield f"data: {json.dumps({'type': 'ai_message_start'}, ensure_ascii=False)}\n\n"
                                _last_debug_step = step
                                thinking_emitted = False  # 新 step → 重置 thinking 标记
                                generating_emitted = False  # 新 step → 重置 generating 标记

                        # ─── 流式 token 处理：分离推理内容与回复内容 ───
                        _raw_content = token.content if hasattr(token, "content") else ""
                        # vLLM 0.19+（启用 reasoning-parser / 多模态后）流式 delta.content
                        # 可能不是 str 而是 OpenAI content-parts 数组
                        # （[{"type": "text", "text": "..."}, ...]）。这里统一归一化为 str，
                        # 防 str += list 的 TypeError
                        # （2026-09-09 实测：upload_to_sandbox 后正文输出 chunk 崩）。
                        # None（tool_calls chunk）与未知类型一律置 ""，保持原 falsy 语义，
                        # 避免 str(None)="None" 之类污染流内容。
                        if _raw_content is None:
                            content = ""
                        elif isinstance(_raw_content, list):
                            _text_parts: list[str] = []
                            for _p in _raw_content:
                                if isinstance(_p, str):
                                    _text_parts.append(_p)
                                elif isinstance(_p, dict) and _p.get("type") == "text":
                                    _text_parts.append(_p.get("text", ""))
                            content = "".join(_text_parts)
                        elif isinstance(_raw_content, str):
                            content = _raw_content
                        else:
                            content = ""

                        # 1. 优先从 additional_kwargs 提取推理内容（DeepSeek/Groq/Ollama/XAI 等）
                        reasoning = ""
                        if hasattr(token, "additional_kwargs") and token.additional_kwargs:
                            reasoning = (
                                token.additional_kwargs.get("reasoning_content") or ""
                            )

                        if reasoning:
                            # 模型通过 API 字段返回推理内容（不经过 content）
                            if not thinking_emitted:
                                thinking_emitted = True
                                # 持久化埋点：首个 reasoning 事件 → 记录推理起始时刻
                                if _reasoning_started_at_ms is None:
                                    _reasoning_started_at_ms = int(time.time() * 1000)
                                yield f"data: {json.dumps({'type': 'agent_status', 'status': 'thinking'}, ensure_ascii=False)}\n\n"
                            yield f"data: {json.dumps({'type': 'reasoning', 'content': reasoning}, ensure_ascii=False)}\n\n"
                        elif content:
                            # 2. 正常 token：这是"写答案"阶段，状态应为 generating（生成中），
                            #    而非 thinking（思考中）。之前误把首个答案 token 标成 thinking，
                            #    导致整段答案生成都显示"思考中…"。
                            #
                            # 注：此处原为「content 内 </think> 标签兜底切分」分支
                            # （2026-07-13 cf81106 引入，服务当时未启 reasoning-parser 的 Qwen）。
                            # 该分支锚点是**闭标签**且默认假设"未见 </think> 即仍在思考"；一旦
                            # vLLM 的 reasoning-parser 把 thinking 剥进 reasoning 字段（当前部署
                            # --reasoning-parser qwen3），它的正确路径永不命中，而普通正文 chunk
                            # 恰好满足其条件 → 正文被无限吞进思考区。实测现象：1+1=2 也显示
                            # "思考过程"、散文正文混入思考块。
                            # 2026-09-10 删除，对齐 deepseek-harness 与 deer-flow 后端：流式路径
                            # 零标签解析，reasoning 只认 additional_kwargs.reasoning_content；
                            # 无 parser 模型的标签兜底改由前端渲染派生层承担（开标签锚点 +
                            # 乐观默认正文，物理上不可能吞正文）。请勿在后端流式归类里重加标签猜测。
                            if not generating_emitted:
                                generating_emitted = True
                                # 持久化埋点：首个 token 事件 → 推理结束 / 进入答案生成
                                # （覆盖路径：无 reasoning 内容直接进 token 的场景，如某些非 CoT 模型）
                                if _reasoning_ended_at_ms is None:
                                    _reasoning_ended_at_ms = int(time.time() * 1000)
                                yield f"data: {json.dumps({'type': 'agent_status', 'status': 'generating'}, ensure_ascii=False)}\n\n"
                            yield f"data: {json.dumps({'type': 'token', 'content': content}, ensure_ascii=False)}\n\n"

                    elif mode == "updates":
                        # 根据 node 名称和输出内容解析 Agent 行为
                        for node_name, node_output in data.items():
                            # 跳过 middleware 节点（非 agent 关键节点）
                            if node_name in (
                                "SkillsMiddleware.before_agent",
                                "PatchToolCallsMiddleware.before_agent",
                                "MemoryMiddleware.before_agent",
                                "HumanInTheLoopMiddleware.after_model",
                                "TodoListMiddleware.after_model",
                            ):
                                continue

                            messages = (
                                node_output.get("messages")
                                if isinstance(node_output, dict)
                                else None
                            )
                            if (
                                not messages
                                or not isinstance(messages, list)
                                or len(messages) == 0
                            ):
                                continue

                            last_msg = messages[-1]

                            # ─── model 节点可观测性日志：AI 回复/思考摘要 + 工具调用列表 ───
                            # 屏蔽 langgraph print 后，模型输出不再出现在日志（原 values/updates
                            # 快照）；这里补一条精简日志，每步 model 一次（2026-08-13 补）
                            if node_name == "model" and hasattr(last_msg, "content"):
                                _ai_content = str(getattr(last_msg, "content", ""))
                                _tool_names = [
                                    tc.get("name", "?")
                                    for tc in (getattr(last_msg, "tool_calls", None) or [])
                                ]
                                logger.warning(
                                    "[DIAG] model: content=%s tool_calls=%s",
                                    (_ai_content or "(空)")[:150],
                                    _tool_names or [],
                                )

                            if (
                                node_name == "model"
                                and hasattr(last_msg, "tool_calls")
                                and last_msg.tool_calls
                            ):
                                # LLM 调用了工具
                                for tc in last_msg.tool_calls:
                                    yield f"data: {
                                        json.dumps(
                                            {
                                                'type': 'tool_call',
                                                'tool': tc['name'],
                                                'args': tc['args'],
                                                'id': tc['id'],
                                            },
                                            ensure_ascii=False,
                                        )
                                    }\n\n"
                                yield f"data: {json.dumps({'type': 'agent_status', 'status': 'running_tool', 'tool': last_msg.tool_calls[0]['name']}, ensure_ascii=False)}\n\n"

                            elif node_name == "tools" and hasattr(last_msg, "name"):
                                # 工具执行结果
                                tool_name = last_msg.name
                                tool_call_id = getattr(last_msg, "tool_call_id", None)
                                # 结构化提取：MCP 工具返回 list[{'type':'text','text':...}]，
                                # 直接 str() 会得到 Python repr（单引号），json.loads 会失败，
                                # 进而误判 is_error（曾因 "error":null 命中 error 关键词把成功标为失败）
                                raw = last_msg.content
                                candidate = None
                                if isinstance(raw, list) and raw:
                                    first = raw[0]
                                    if isinstance(first, dict) and first.get("text"):
                                        candidate = first["text"]
                                elif isinstance(raw, dict) and raw.get("text"):
                                    candidate = raw["text"]
                                elif isinstance(raw, str):
                                    candidate = raw
                                content_str = str(candidate) if candidate else ""
                                # ─── 失败判定（2026-09-29 抽为纯函数便于单测）───
                                # 注意 status 优先：护栏拒绝的 ToolMessage(status="error")
                                # 文案不含 exit code/Execution error，旧关键词分支会把它
                                # 误判为成功 → 拒绝文案里的路径被当交付物解析。详见
                                # _tool_result_is_error docstring。
                                is_error = _tool_result_is_error(
                                    tool_name,
                                    content_str,
                                    getattr(last_msg, "status", None),
                                )

                                # ─── tool 节点可观测性日志：工具名 + 成功/失败 + 结果截断 ───
                                # 屏蔽 langgraph print 后工具调用过程不再出现在日志，
                                # 排查时看不到 AI 在调什么工具（2026-08-13 补）
                                logger.warning(
                                    "[DIAG] tool %s → %s | %s",
                                    tool_name,
                                    "OK" if not is_error else "FAIL",
                                    content_str[:150] if content_str else "(空)",
                                )
                                # skill workflow 阶段标记（M3 产物校验用）：
                                # 成功跑过 stage1/stage3 → 后续要求 reports 出现对应产物
                                if not is_error:
                                    if tool_name == "run_pdf_diff_stage1":
                                        _skill_state["stage1"] = True
                                    elif tool_name == "run_pdf_diff_stage3":
                                        _skill_state["stage3"] = True

                                yield f"data: {
                                    json.dumps(
                                        {
                                            'type': 'tool_result',
                                            'tool': tool_name,
                                            'id': tool_call_id,
                                            'success': not is_error,
                                            # 2026-09-12：result 与持久化副本对齐同一上限，
                                            # 否则会出现「当轮看 2000、刷新后看 8000」的不一致。
                                            'error': _clip(
                                                content_str,
                                                settings.persist_max_error_chars,
                                            )
                                            if is_error
                                            else None,
                                            'result': _clip(
                                                content_str,
                                                settings.persist_max_tool_result_chars,
                                            )
                                            if content_str
                                            else None,
                                        },
                                        ensure_ascii=False,
                                    )
                                }\n\n"

                                # ─── 工具连续失败检测：超限提前终止，防止烧满 recursion_limit ───
                                # 模型在错误循环里空转时（如反复 glob 失败/read 失败），
                                # 每次 ToolMessage 都进 messages 撑大步数，直到 100 步才抛
                                # GraphRecursionError（几分钟白等）。这里数连续失败次数，
                                # 达到阈值立即 raise，由 except 分支转成 SSE error 事件止损。
                                if is_error:
                                    _consecutive_tool_failures += 1
                                    if (
                                        _consecutive_tool_failures
                                        >= settings.sandbox_tool_failure_threshold
                                    ):
                                        logger.warning(
                                            "[M3] 工具连续失败 %d 次（最近: %s），提前终止: user=%s, session=%s",
                                            _consecutive_tool_failures, tool_name, user_id, session_id,
                                        )
                                        raise ToolLoopAbortError(
                                            f"工具连续失败 {_consecutive_tool_failures} 次"
                                            f"（最近一次：{tool_name}），判定任务已无法继续，已提前终止"
                                        )
                                else:
                                    _consecutive_tool_failures = 0

                                # ─── P0 无进展循环检测：工具成功但反复同一意图 ───
                                # 模型在"工具全成功但任务未收敛"时空转（2026-08-17/18 两次
                                # Recursion limit 实证：execute 全 OK、报告已生成，模型仍不断
                                # "重新构建 diff.json"）。连续同一工具意图 + 窗口内零新交付物
                                # → 判定循环 → 抛 NoProgressAbortError 由外层注入收敛提示。
                                if not is_error:
                                    sig = _tool_intent_sig(tool_name, content_str)
                                    if sig == _last_tool_sig:
                                        _repeat_count += 1
                                    else:
                                        _last_tool_sig = sig
                                        _repeat_count = 1
                                    # 重复窗口内出现新交付物 → 重置（不是空转）
                                    if _files_in_window >= settings.no_progress_window_files:
                                        _files_in_window = 0
                                        _repeat_count = 0
                                    if (
                                        _repeat_count >= settings.no_progress_repeat_threshold
                                    ):
                                        if (
                                            _no_progress_injections
                                            < settings.no_progress_max_injections
                                        ):
                                            _no_progress_injections += 1
                                            logger.warning(
                                                "[P0] 无进展循环检测: 工具 %s 连续 %d 次同一意图且无新交付物"
                                                "（最近结果: %s）→ 注入收敛提示",
                                                tool_name, _repeat_count, content_str[:120],
                                            )
                                            raise NoProgressAbortError(
                                                f"检测到无进展循环：工具 {tool_name} 连续 "
                                                f"{_repeat_count} 次重复相同操作且无新产出"
                                            )
                                        else:
                                            # 收敛注入已耗尽仍未收敛 → 直接终止（防烧满 recursion_limit）
                                            logger.warning(
                                                "[P0] 无进展循环收敛注入已耗尽（%d 次），终止: 工具 %s 连续 %d 次同一意图",
                                                _no_progress_injections, tool_name, _repeat_count,
                                            )
                                            raise ToolLoopAbortError(
                                                f"检测到无进展循环：已注入 {_no_progress_injections} 次收敛提示"
                                                f"仍未收敛（最近: {tool_name} 连续 {_repeat_count} 次重复），"
                                                "判定任务无法继续，已提前终止"
                                            )

                                # ─── 检测工具返回的文件信息，提取生成/下载的文件 ───
                                # 不限定工具名，任何返回 /reports/ 路径的工具都能触发
                                file_path_virtual = None
                                # B①（2026-09-29）：原实现 `if not is_error` 一刀切跳过——
                                # 报错结果里的 /reports/... 同样可能指向**真实产物**
                                # （GY35377/60c64c4f 实测：`cp /home/user/x.pptx
                                #  /reports/.../x.pptx` 因沙箱内没有该目录而 exit 1，
                                #  源文件 x.pptx 好端端在 /home/user/ 下）。跳过 = 连
                                # "试着从沙箱拉回"的机会都没有。故放开解析，安全性由
                                # 下方 emit 护栏承担：**报错来源的路径只有拉回成功
                                # （file_size > 0）才允许 emit**，绝不凭一行报错文本发卡片。
                                if not is_error or _PARSE_ERROR_TOOL_RESULTS:
                                    # 1. 结构化解析：MCP 工具（如 download_from_sandbox）返回
                                    #    list[{'type','text'}] 或 dict，text 里是 JSON 字符串；
                                    #    直接用 json.loads 取路径字段，避免正则贪婪匹配吃进 JSON 尾巴
                                    #    （旧实现 re.search(r'/reports/\S+') 会把
                                    #    ","size":15758,"error":null 等尾巴一起吞掉 → 前端 404）
                                    parsed_json = None
                                    raw_content = getattr(last_msg, "content", None)
                                    candidate = None
                                    if isinstance(raw_content, list) and raw_content:
                                        first = raw_content[0]
                                        if isinstance(first, dict) and first.get("text"):
                                            candidate = first["text"]
                                    elif isinstance(raw_content, dict) and raw_content.get("text"):
                                        candidate = raw_content["text"]
                                    elif isinstance(raw_content, str):
                                        candidate = raw_content
                                    if isinstance(candidate, str):
                                        try:
                                            parsed_json = json.loads(candidate)
                                        except (json.JSONDecodeError, TypeError):
                                            parsed_json = None
                                    if isinstance(parsed_json, dict):
                                        for key in ("host_path", "path", "file_path", "output_path"):
                                            val = parsed_json.get(key)
                                            if isinstance(val, str) and val.strip():
                                                file_path_virtual = val.strip()
                                                break
                                    # 2. 回退：非 JSON 输出（write_file 的 "Updated file ..."）用正则硬抠
                                    if not file_path_virtual:
                                        if tool_name in ("write_file", "write", "create_file"):
                                            m = re.search(
                                                r"Updated file\s+(/\S+)", content_str
                                            )
                                            if m:
                                                file_path_virtual = m.group(1)
                                        else:
                                            # [^"\s,}\]]+ 匹配到中文等非分隔符字符为止（\w 不含中文）
                                            m = _REPORTS_PATH_RE.search(content_str)
                                            if m:
                                                file_path_virtual = m.group(0).rstrip(
                                                    '"'
                                                ).rstrip("}").rstrip(",")

                                # 规范化文件路径：如果 write_file 返回的路径是 WSL/宿主机完整路径
                                # （如 /mnt/d/.../data/reports/.../file），从中提取 /reports/... 部分
                                if file_path_virtual and not file_path_virtual.startswith("/reports/"):
                                    reports_m = _REPORTS_PATH_RE.search(file_path_virtual)
                                    if reports_m:
                                        file_path_virtual = reports_m.group(0)
                                        logger.debug(
                                            "[DIAG] 文件路径已规范化: path=%s",
                                            file_path_virtual,
                                        )

                                # 清洗 file_path_virtual（对称前端清洗）：上游返回的路径可能
                                # 尾随单引号/空白等脏字符（2026-08-24 实测 .../diff.json'），
                                # 不清洗则磁盘 404 → 前端 HEAD 失败 → 红框"文件不可用"卡。
                                if file_path_virtual:
                                    file_path_virtual = _sanitize_generated_path(file_path_virtual)

                                # 触发条件：/reports/ 交付物 + /skills/__agent__/ 自创 skill
                                # （A1：skill 文件也触发 file_generated，否则前端看不到 skill 卡片
                                #   —— 2026-08-11 15:28 实测 SKILL.md 写成功但前端"文件不可用"）
                                is_report_path = bool(
                                    file_path_virtual
                                    and file_path_virtual.startswith("/reports/")
                                )
                                is_agent_skill = bool(
                                    file_path_virtual
                                    and file_path_virtual.startswith("/skills/__agent__/")
                                )
                                if file_path_virtual and (is_report_path or is_agent_skill):
                                    filename = None
                                    file_size = 0
                                    if is_report_path:
                                        prefix = f"/reports/{user_id}/{session_id}/"
                                        if file_path_virtual.startswith(prefix):
                                            filename = file_path_virtual[len(prefix) :]
                                        # 磁盘大小：report_root → agent_workspace/data/reports 兜底
                                        # （write_file 可能写到了 /mnt/d/... 而不是 /data/myapp/...）
                                        for base in (
                                            settings.report_root,
                                            os.path.join(
                                                settings.agent_workspace, "data", "reports"
                                            ),
                                        ):
                                            try:
                                                disk_path = os.path.join(
                                                    base, user_id, session_id, filename
                                                )
                                                if os.path.isfile(disk_path):
                                                    file_size = os.path.getsize(disk_path)
                                                    break
                                            except Exception:
                                                pass
                                        # 方案A（2026-09-29）：磁盘缺失 → 从沙箱拉回
                                        # 持久化后再 emit（沙箱 execute 产物 TTL 回收即
                                        # 丢失，GY35377/60c64c4f pptx 404 实测根因）。
                                        # 此时沙箱刚执行完工具必然存活，拉回成功率最高。
                                        if file_size == 0 and filename:
                                            file_size = await _pull_report_from_sandbox(
                                                sandbox, file_path_virtual,
                                                user_id, session_id, filename,
                                            )
                                    else:  # /skills/__agent__/
                                        filename = file_path_virtual[
                                            len("/skills/__agent__/") :
                                        ]
                                        try:
                                            disk_path = os.path.join(
                                                settings.agent_workspace,
                                                "skills", "__agent__", filename,
                                            )
                                            if os.path.isfile(disk_path):
                                                file_size = os.path.getsize(disk_path)
                                        except Exception:
                                            pass
                                    # 如果磁盘没取到大小，尝试从 JSON 返回值中提取 size
                                    # B① 收窄：报错结果**不采信**"返回值里的 size"——那是
                                    # 工具/模型自称的数字，不是宿主磁盘事实；采信它等于绕过
                                    # 下面的 emit 护栏，凭空放行一张幽灵卡片。报错来源只认
                                    # "拉回成功"这一条事实来源（见 _pull_report_from_sandbox）。
                                    if file_size == 0 and not is_error:
                                        size_m = re.search(
                                            r'"size"\s*:\s*(\d+)', content_str
                                        )
                                        if size_m:
                                            file_size = int(size_m.group(1))

                                    # 目录路径不产生交付物：write_file 的 file_path 若为目录
                                    # （漏文件名，如 /reports/uid/sid/ 或 /reports/uid/sid），
                                    # 提取出的 filename 为空/会话ID，emit 后前端收到
                                    # file_name="" 的 file_generated → "(未知文件)" 卡片 +
                                    # 空 URL 请求 HEAD 307/401（2026-08-21 实测 350e5f80 会话；
                                    # 与 agent.py _reject_directory_write 双保险）
                                    if is_report_path and not filename:
                                        logger.warning(
                                            "[FILE_GEN] 忽略目录路径 file_generated: path=%s (无文件名)",
                                            file_path_virtual,
                                        )
                                        continue

                                    # ─── B① 安全护栏（2026-09-29）───
                                    # 来自**报错结果**的路径，只有真正拉回成功（file_size>0，
                                    # 即 _pull_report_from_sandbox 已把它落到宿主卷）才允许
                                    # emit。否则一行 `No such file or directory` 里的路径就会
                                    # 变成一张 HEAD 404 的红框卡片 —— 比不显示更糟：它让用户
                                    # 以为文件存在过。护栏放在这里（而非解析处），一次覆盖
                                    # /reports/ 与 /skills/ 两条分支。
                                    if is_error and file_size == 0:
                                        logger.info(
                                            "[FILE_GEN] 报错结果中的路径未拉回成功，跳过 emit"
                                            "（防幽灵卡片）: path=%s",
                                            file_path_virtual,
                                        )
                                        continue

                                    # B：同会话内同一文件只推送一次（覆盖写/重复 write_file 去重）
                                    if file_path_virtual in _emitted_files:
                                        # 事件已发过，仅更新 _generated_files 里的大小，不重复推送
                                        for _f in _generated_files:
                                            if _f["file_path"] == file_path_virtual:
                                                _f["file_size"] = file_size
                                                break
                                    else:
                                        _emitted_files.add(file_path_virtual)
                                        _files_in_window += 1  # P0：新交付物计数（非空转信号）
                                        yield f"data: {
                                                    json.dumps(
                                                        {
                                                            'type': 'file_generated',
                                                            'file_name': file_path_virtual.split(
                                                                '/'
                                                            )[-1],
                                                            'file_path': file_path_virtual,
                                                            'file_size': file_size,
                                                            'file_type': _infer_file_type(
                                                                file_path_virtual.split('/')[-1]
                                                            ),
                                                        },
                                                        ensure_ascii=False,
                                                    )
                                                }\n\n"
                                        # 同时记录到 _generated_files 列表，保存时关联到对应 AI 消息
                                        _generated_files.append({
                                            "file_name": file_path_virtual.split('/')[-1],
                                            "file_path": file_path_virtual,
                                            "file_size": file_size,
                                            "file_type": _infer_file_type(
                                                file_path_virtual.split('/')[-1]
                                            ),
                                        })
            except ToolLoopAbortError as e:
                # ─── 工具连续失败超限：预期内主动终止，不打印堆栈（非程序 bug）───
                # 只向前端发 error 事件（比 GraphRecursionError 快几十步）
                logger.warning("工具连续失败超限提前终止: %s", e)
                yield f"data: {json.dumps({'type': 'error', 'content': f'Agent 处理异常: {str(e)[:200]}'}, ensure_ascii=False)}\n\n"
            except NoProgressAbortError as e:
                # ─── P0 无进展循环：中断本轮，注入收敛提示让模型收敛 ───
                # 触发后 _graph_input 被替换为收敛提示，外层 while 循环继续跑一轮；
                # 收敛轮再触发（超 no_progress_max_injections）则不再注入，
                # 由 except Exception 兜底转 error 终止（防无限收敛轮）。
                # 注意：必须用 HumanMessage 而非 SystemMessage——vLLM 要求所有
                # system 消息连续位于开头，本提示与 checkpoint 恢复的历史消息合并
                # 后不保证位置（2026-08-19 09:44:54 实测 400 'System message must
                # be at the beginning'；资源清单 13dab48→f093303 同款教训）。
                _no_progress_triggered = True  # 标记：收敛轮跳过 M3 零产出拦截
                logger.warning("[P0] 无进展循环，注入收敛提示: %s", e)
                yield f"data: {json.dumps({'type': 'agent_status', 'status': 'no_progress', 'content': f'{str(e)[:200]}'}, ensure_ascii=False)}\n\n"
                # 收敛提示：让模型停止重复，验证已有产出并交付
                _graph_input = {"messages": [HumanMessage(
                    f"系统检测到无进展循环：你已连续多轮重复相同操作（{str(e)[:150]}）"
                    "且未产生新交付物。请立即收敛："
                    "1) 检查 /reports/{user_id}/{session_id}/ 下是否已有可交付的报告文件，"
                    "   如有则直接确认交付，停止重新生成；"
                    "2) 若确需重跑，先说明与上一轮的具体差异，一次完成，不要重复相同命令；"
                    "3) 若无法收敛，明确向用户说明卡点和已完成的产出。"
                )]}
                # 重置无进展计数（收敛轮重新统计），基线重设
                _last_tool_sig = None
                _repeat_count = 0
                _files_in_window = 0
                _before_files = snapshot_report_files(settings.report_root, user_id, session_id)
            except Exception as e:
                # ─── 异常保护：任何 agent.astream 内的异常都被捕获，不崩掉 SSE 流 ───
                logger.exception("Agent 流式处理异常: %s", e)
                yield f"data: {json.dumps({'type': 'error', 'content': f'Agent 处理异常: {str(e)[:200]}'}, ensure_ascii=False)}\n\n"


        # ─── M3 完成门（设计文档）：系统校验本轮 /reports/ 产出，零产出不放行结束 ───
        # 判定不信任模型自陈：_generated_files（流式识别）与磁盘差集双保险，
        # 两者皆空即视为"任务未产出任何交付物"。
        # v2 自动继续：注入 SystemMessage 让模型再跑一轮（受 auto_continue / max_retries 开关控制，
        # 异常由 _drain_astream 内部吞掉 → 本轮零产出仍会递增重试，超限终止）。
        _completion_retries = 0
        # skill workflow 阶段标记（M3 产物校验用）：本轮是否成功跑过 stage1/stage3
        _skill_state = {"stage1": False, "stage3": False}
        # P0：NoProgressAbortError 触发标记——收敛轮跳过 M3 零产出拦截
        # （收敛轮刚注入提示，本轮零产出是预期的，不应被完成门拦截）
        _no_progress_triggered = False
        # 断连强制保存：正常路径保存成功后置 True；流被 GeneratorExit/CancelledError
        # 打断时 finally 兜底强制保存（2026-08-11 15:26 断连丢"写 skill"指令的修复）
        _saved = False
        try:
            while True:
                async for _ev in _drain_astream(_graph_input):
                    yield _ev
                _after_files = snapshot_report_files(settings.report_root, user_id, session_id)
                _new_files = _after_files - _before_files

                # ─── B② 收尾兜底：把沙箱里漏掉的产物拉回宿主 ───
                # 位置放在 M3 完成门**之前**：兜底救回来的产物应当让本轮正常放行，而不是
                # 先弹"未产出交付物"再补救（那是自相矛盾的 UX，正是本次要修的病）。
                # 单次门 _salvage_done：完成门重试轮不重复扫（差集已取过，重扫只是多花
                # 一次沙箱往返且无新信息）。
                #
                # 触发条件两个来源（2026-09-29 A+B 扩展）：
                #   ① 原条件（词表判交付 + 宿主零产出）；② 本轮 execute 命中虚拟路径护栏。
                # ②为什么必要：`_is_deliverable` 是**关键词启发式**，会漏判（实测
                # 60c64c4f：英文 "appointment PPT" 未命中强信号 "pptx" → 判 False →
                # 基线没取、兜底全程没生效）。而"模型试图往 /reports 写"这件事本身就是
                # **交付意图的强信号**，比词表可靠；此刻沙箱里极可能已经躺着它写出来的
                # 产物（先写 /home/user 再 cp /reports 的典型形态）。
                # A 是高频拦截层、不是完备保证（变量拼接/base64/通配符可绕），
                # B 不依赖命令解析、只比沙箱前后差集 ⇒ A 被绕过也不丢产物。
                # 🔭 治本方案 C（把宿主报告卷 bind-mount 进沙箱，通道不对称彻底消失）
                # 本次未做，理由与触发条件见 src/core/execution_guard.py 模块头「后续演进」。
                _exec_violation = execution_guard.peek_execute_path_violation(
                    user_id, session_id
                )
                if (
                    not _salvage_done
                    and _before_sandbox_files is not None
                    and (
                        (
                            not _new_files
                            and not _generated_files
                            and _is_deliverable
                        )
                        or _exec_violation is not None
                    )
                ):
                    _salvage_done = True
                    # 条件成立才 consume：条件不满足（如基线不可用）时把信号留给本轮
                    # 后续循环/下一轮，避免"吃掉信号却什么都没做"。
                    _exec_violation = execution_guard.consume_execute_path_violation(
                        user_id, session_id
                    )
                    if _exec_violation is not None:
                        logger.warning(
                            "[FILE_GEN] 命中 execute 越界写信号 → 强制收尾兜底: "
                            "user=%s, session=%s, cmd=%s",
                            user_id, session_id,
                            str(_exec_violation.get("command_head", ""))[:160],
                        )
                    _salvaged = await _salvage_sandbox_artifacts(
                        sandbox, user_id, session_id, _before_sandbox_files
                    )
                    for _sf in _salvaged:
                        if _sf["file_path"] in _emitted_files:
                            continue
                        _emitted_files.add(_sf["file_path"])
                        _files_in_window += 1  # 新交付物计数（非空转信号，与 B① 一致）
                        _generated_files.append(_sf)
                        yield f"data: {json.dumps({'type': 'file_generated', **_sf}, ensure_ascii=False)}\n\n"
                    if _salvaged:
                        _after_files = snapshot_report_files(
                            settings.report_root, user_id, session_id
                        )
                        _new_files = _after_files - _before_files
                # ─── 空响应降级优先于 M3 零产出拦截（2026-09-16）───
                # 模型层一个字都没产出时，「/reports 为空」只是它的**后果**，
                # 不能按"漏了 download_from_sandbox / write_file"归因（de18ad37 实锤）。
                # 命中即上报真实原因并结束本轮——此时再多跑一轮完成门重试也无意义
                #（上游产出为空，重试同样拿不到交付物；auto_continue 开启时同理，
                #  因为根因是单次输出预算，不是"模型忘了下一步"）。
                _fallback = pop_fallback_signal(thread_id)
                if _fallback is not None and _fallback.get("quick_mode"):
                    # 关思考兜底**成功**：本轮有正文产出，绝不能走下面的完成门拦截
                    # （那会把一条正常回复误判成「模型未产出」）。只需告知用户这条是
                    # 「快速模式」产物 —— 关思考的产出与正常回复外观完全一致，不留痕
                    # 用户会当完整答案用（隐性失败比空白气泡更危险）。
                    # 注意：**不设** `_terminal_fallback` —— 那不是「降级收尾」，
                    # 设了会污染落库标记与断连路径的判断。
                    _qm_attempt = int(_fallback.get("attempt", 1) or 1)
                    _qm_reason, _qm_hint = quick_mode_notice(_qm_attempt)
                    logger.warning(
                        "[Guard] 本轮为关思考兜底产出（快速模式，第 %d 次重试）"
                        "→ 提示用户: user=%s, session=%s",
                        _qm_attempt, user_id, session_id,
                    )
                    yield f"data: {json.dumps({
                        'type': 'quick_mode_notice',
                        'code': 'thinking_disabled_fallback',
                        'title': '本轮为快速模式产出',
                        'reason': _qm_reason,
                        'hint': _qm_hint,
                        'terminated': False,
                    }, ensure_ascii=False)}\n\n"
                    _fallback = None
                if _fallback is not None:
                    _terminal_fallback = _fallback
                    _ft_reason, _ft_hint = fallback_notice(
                        str(_fallback.get("branch", "")),
                        int(_fallback.get("retries", 0) or 0),
                    )
                    logger.warning(
                        "[M3] 空响应降级收尾 → 跳过零产出拦截并直报模型层原因: "
                        "user=%s, session=%s, branch=%s, finish_reason=%s, reasoning_len=%d",
                        user_id, session_id, _fallback.get("branch"),
                        _fallback.get("finish_reason"), int(_fallback.get("reasoning_len", 0) or 0),
                    )
                    yield f"data: {json.dumps({
                        'type': 'completion_blocked',
                        'code': 'model_output_truncated',
                        'title': '模型本轮未产出内容',
                        'reason': _ft_reason,
                        'hint': _ft_hint,
                        'terminated': False,
                    }, ensure_ascii=False)}\n\n"
                    break
                if _no_progress_triggered:
                    # 收敛轮：跳过零产出拦截，直接继续下一轮（_graph_input 已换成收敛提示）
                    _no_progress_triggered = False
                    _before_files = _after_files  # 重新基线
                    continue
                if _generated_files or _new_files:
                    # 有本轮产出 → 再校验 skill 工作流关键产物（跑过 stage1/stage3 时）。
                    # 缺失即"流程没走完"（如 stage1 产物没拉回 reports），不放行，
                    # 注入收敛提示继续——不信任模型自陈"做完了"。
                    _missing = _missing_skill_artifacts(
                        settings.report_root, user_id, session_id,
                        _skill_state["stage1"], _skill_state["stage3"],
                    )
                    if not _missing:
                        break  # 产物齐，放行
                    if (
                        _completion_retries >= settings.sandbox_completion_gate_max_retries
                        or not settings.sandbox_completion_gate_auto_continue
                    ):
                        _completion_blocked = True
                        logger.warning(
                            "[M3] skill 产物缺失且重试耗尽: user=%s, session=%s, missing=%s",
                            user_id, session_id, _missing,
                        )
                        yield f"data: {json.dumps({
                            'type': 'completion_blocked',
                            'code': 'skill_artifact_missing',
                            'title': 'skill 工作流产物缺失',
                            'reason': f'skill 工作流产物缺失: {_missing}',
                            'hint': '请检查是否遗漏 download_from_sandbox 拉回产物',
                            'terminated': True,
                        }, ensure_ascii=False)}\n\n"
                        break
                    _completion_retries += 1
                    logger.warning(
                        "[M3] skill 产物缺失，注入收敛继续（%d/%d）: missing=%s",
                        _completion_retries, settings.sandbox_completion_gate_max_retries, _missing,
                    )
                    _graph_input = {"messages": [HumanMessage(
                        f"系统校验：本轮 skill 工作流关键产物缺失：{'、'.join(_missing)}。"
                        f"请立即用 download_from_sandbox 将对应文件拉回 /reports/{user_id}/{session_id}/，"
                        "然后继续完成。禁止自写替代代码或跳过流程步骤。"
                    )]}
                    _before_files = _after_files  # 重新基线
                    continue
                if not settings.sandbox_completion_gate_enabled or not _is_deliverable:
                    # 完成门关闭（回滚开关），或本轮为非文件交付任务（内容型任务如
                    # 散文/问答，交付物即对话回复本身）→ 零产出是正常结果，直接放行。
                    break
                if (
                    _completion_retries >= settings.sandbox_completion_gate_max_retries
                    or not settings.sandbox_completion_gate_auto_continue
                ):
                    _completion_blocked = True
                    logger.warning(
                        "[M3] 完成门拦截（本轮零产出）: user=%s, session=%s, generated=%d, new_files=%d, retries=%d",
                        user_id, session_id, len(_generated_files), len(_new_files), _completion_retries,
                    )
                    # C② 反幻觉（2026-09-29）：模型常在**未做任何验证**的情况下于正文声称
                    # "已保存/可直接下载"（GY35377/60c64c4f 实测：连续四轮零工具调用纯复述，
                    # 同屏还挂着"本轮未产出交付物"横幅，界面自相矛盾）。
                    # 零产出是磁盘事实，故这里**明确否定**该类表述——与本门"只描述事实、
                    # 不做无据推测"的原则不冲突：否定句同样是事实断言（文件确实不存在），
                    # 不是对"模型漏了哪一步"的猜测。
                    # 措辞随核查范围变化：B② 沙箱兜底跑过才有资格说"沙箱也没有"。
                    _verified_scope = (
                        "宿主报告目录与沙箱均已核查，均无本轮新交付物"
                        if _salvage_done
                        else "宿主报告目录无本轮新交付物"
                    )
                    yield f"data: {json.dumps({
                        'type': 'completion_blocked',
                        'code': 'no_artifact_in_reports',
                        'title': '本轮未产出交付物',
                        'reason': f'本轮任务未产出任何交付物（{_verified_scope}）；'
                                  '若上文中出现“已保存/已生成/可直接下载”一类说法，该说法不成立，'
                                  '请勿据此操作（文件不存在）',
                        # 措辞只描述事实 + 条件式建议：本门只看得到磁盘差集，看不到
                        # 模型是否漏了哪一步，原文"请检查是否遗漏 ..."是无据推测
                        #（2026-09-16 会话 de18ad37 因"输出"×"表格"顺带词误命中而错报）。
                        # 模型层真没产出的情况已由 terminal_response 降级信号在更早处分流。
                        'hint': '若任务本应产出文件，请检查是否遗漏 download_from_sandbox / write_file 步骤',
                        'terminated': _completion_retries >= settings.sandbox_completion_gate_max_retries,
                    }, ensure_ascii=False)}\n\n"
                    break
                _completion_retries += 1
                logger.warning(
                    "[M3] 零产出，注入 SystemMessage 自动继续（第 %d/%d 次）: user=%s, session=%s",
                    _completion_retries, settings.sandbox_completion_gate_max_retries, user_id, session_id,
                )
                _graph_input = {"messages": [SystemMessage(
                    f"系统检测：/reports/{user_id}/{session_id}/ 当前无本轮新文件，本轮任务未产出任何交付物。"
                    "请检查是否遗漏 download_from_sandbox / write_file 步骤，并继续完成。"
                )]}
                _before_files = _after_files  # 重新基线
            _saved = True  # 循环正常结束（未被取消/打断），finally 不再兜底保存
        finally:
            # 断连/取消/异常路径：流被中断，正常保存逻辑未执行，强制保存 checkpoint 状态
            if not _saved:
                try:
                    logger.warning(
                        "[DIAG] SSE 流中断（断连/取消），强制保存当前会话: user=%s, session=%s",
                        user_id, session_id,
                    )
                    # 断连路径也要取走降级信号（若守卫已登记但本轮没走到消费点）：
                    # 否则它会以 thread_id 为键留在登记表里被下一轮消费，造成误报。
                    if _terminal_fallback is None:
                        _terminal_fallback = pop_fallback_signal(thread_id)
                    # 断连路径：turn_ended 兜底取 now（持久化时刻作为 turn 终点）
                    interrupted_turn_end = _turn_ended_at_ms if _turn_ended_at_ms is not None else int(time.time() * 1000)
                    await _persist_session(
                        agent, thread_id, user_id, session_id, store, body,
                        generated_files=_generated_files,
                        disk_files=_new_files,
                        completion_blocked=_completion_blocked,
                        terminal_fallback=_terminal_fallback,
                        reasoning_started_at_ms=_reasoning_started_at_ms,
                        reasoning_ended_at_ms=_reasoning_ended_at_ms,
                        turn_started_at_ms=_turn_started_at_ms,
                        turn_ended_at_ms=interrupted_turn_end,
                        reason="interrupted",
                    )
                except Exception as e:
                    logger.error(
                        "断连强制保存失败（关键错误，本轮消息可能丢失）: %s",
                        e, exc_info=True,
                    )

        # ─── SSE 流结束，先保存消息到会话历史，再发 [DONE] ───
        # 防止前端 [DONE] 后立即编辑导致竞态（消息尚未落盘 → from_index 越界）
        # turn_ended 兜底：while 循环有 4 个 break 出口都没主动设 turn_ended，
        # 在这里统一设值为 now（持久化时刻），这样无论 break / 异常早退
        # / 断连走外层 finally 都至少能算到"持久化时刻"。
        # 偏差是 _persist_session 函数执行耗时（读 checkpoint + 写 store，
        # 几十到几百毫秒，可忽略）。
        if _turn_ended_at_ms is None:
            _turn_ended_at_ms = int(time.time() * 1000)
        await _persist_session(
            agent, thread_id, user_id, session_id, store, body,
            generated_files=_generated_files,
            disk_files=_new_files,
            completion_blocked=_completion_blocked,
            terminal_fallback=_terminal_fallback,
            reasoning_started_at_ms=_reasoning_started_at_ms,
            reasoning_ended_at_ms=_reasoning_ended_at_ms,
            turn_started_at_ms=_turn_started_at_ms,
            turn_ended_at_ms=_turn_ended_at_ms,
            reason="normal",
        )
        yield "data: [DONE]\n\n"

    async def event_stream():
        """外层包装：登记在跑轮次 + 把 LangGraph span 顶成真根。

        acquire/release 与 beat 都在这一层，_event_stream_inner 函数体一行不动：
        - release 放在 finally：正常结束、异常、以及客户端断连（内层 :1557 的
          强制持久化 finally 先执行完）都会走到——**release 一定发生在"写台账"
          之后**，否则删除请求可能趁持久化过程中插进来。
        - beat 挂在每个 chunk 上：token/工具事件密集时刷新活跃时间；空转期间由
          内层的心跳哨兵（::interval 秒一个 chunk）继续刷新。陈旧判定（
          turn_registry.STALE_MS）兜底进程被杀等 release 未执行的路径。
        - 空 Context（2026-09-29 重做，原理见 _otel_context_api 上方注释块）：
          只切断 OTel span 继承，**不新建 span、不写任何 name/input/output** ——
          LangChain 插桩建的 `LangGraph` span 因此 parent=None 成为真根，Langfuse
          trace 级三列自然回退到它自己的值（与 9.2/9.3 基线逐字一致），Phoenix 的
          rootSpan 也一起回来。全程 best-effort：观测依赖缺失、attach/detach 失败
          都只记 debug 日志，绝不影响 SSE 下发。
        """
        turn_registry.acquire(thread_id)
        _ctx_token = None
        if _otel_context_api is not None and _OtelContext is not None:
            try:
                # 空 Context ⇒ get_current_span() 是 INVALID_SPAN，父子链路在此断开
                _ctx_token = _otel_context_api.attach(_OtelContext())
            except Exception:
                logger.debug("[TRACING] 切断 OTel 父 span 失败（已忽略）", exc_info=True)
        try:
            async for _chunk in _event_stream_inner():
                turn_registry.beat(thread_id)
                yield _chunk
        finally:
            # detach 是非阻塞的纯 contextvar 复位，放 release 之前，
            # 保证"在跑轮次"台账的释放不被任何观测侧动作拖住。
            if _ctx_token is not None:
                try:
                    _otel_context_api.detach(_ctx_token)
                except Exception:
                    logger.debug("[TRACING] 恢复 OTel Context 失败（已忽略）", exc_info=True)
            turn_registry.release(thread_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 禁用 nginx/openresty 缓冲，保证 SSE 实时逐字节下发（配合心跳保活）
            "X-Accel-Buffering": "no",
        },
    )

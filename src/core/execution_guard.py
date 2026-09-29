"""execute 通道虚拟路径护栏：拒绝文案 + 轮次信号（A+B，2026-09-29）。

## 为什么要在 backend 层拦（而不是只靠提示词）

`/reports/{uid}/{sid}/` 与 `/uploads/{uid}/{sid}/` 是**虚拟文件系统路径**：
- `write_file` 命中路由 → `FilesystemBackend(report_root/uid/sid)` **直写宿主卷**；
- 而 `execute`（shell）**不参与路径路由**（deepagents `CompositeBackend.execute`
  原文注释 "execution is not path-routable — it always delegates to the default
  backend"）→ 落到沙箱（CubeSandbox MicroVM），而 `create_sandbox` **没有挂载任何
  宿主目录** ⇒ 沙箱里的 `/reports/...` 是模型自己 `mkdir -p` 出来的空目录，
  与宿主报告卷毫无关系，5min TTL 一到随 MicroVM 一起消失。

实测事故链（GY35377/60c64c4f「项目团队任命书.pptx」）：
1. `execute: python3 generate_ppt.py` → 产物落在**沙箱内** `/home/user/`；
2. `cp /home/user/x.pptx /reports/...` → **exit 1**：`No such file or directory`；
3. 模型把这句话读成"目录没建好" → `mkdir -p /reports/... && cp ...` → exit 0
   （**沙箱内**成功，回显里一个 `/reports/` 字样都没有）；
4. 模型据此宣称"已保存、可直接下载"，而宿主报告卷**全程零产出**。

注意第 3 步：**是环境把模型引导向了错误动作**。提示词只能降低概率（长上下文/
多轮后衰减，且它在模型决策之后不再起作用），拦不住"mkdir 后重试成功"这条路径。
故在唯一必经点（`ValidatedCompositeBackend.execute/aexecute`）把它升级为
**确定性拒绝 + 正确通道指引**。

## A 与 B 的分工

- **A｜前置拒绝**：命令文本引用 `/reports` 或 `/uploads` → `raise ValueError`。
  deepagents 工具层 `except ValueError` → `ToolMessage(status="error")`（官方预留
  通道，模型能明确读到；反例见下"为什么不用 exit_code"）。
- **B｜后置救济**：A 命中时**只举报不做 I/O**（本模块的 `note_*`），由 API 层
  （chat.py）在收尾时消费该信号、触发沙箱产物回收（`_salvage_sandbox_artifacts`）
  把已经躺在沙箱里的东西救回宿主并 emit `file_generated`。
  **B 才是"确定性"的来源**：A 被绕过（见下滑网面）也不丢产物。

## 诚实记录：A 的滑网面（不是完备保证）

变量拼接、base64 解码后执行、通配符（`/re*ts`）、`cd / && cd reports`、
以及进程内直接 open() 写宿主其他路径，都绕得过字面量匹配。
本护栏的定位是**高频拦截层 + 把误导性环境错误改成明确规则违反**，
完备性由 B（不依赖命令解析的差集回收）承担。

## 为什么拒绝不能用 `ExecuteResponse(exit_code=1)`

`ExecuteResponse` 无 error 字段（deepagents `backends/protocol.py`），且 execute
工具层**恒定**回 `status="success"`（`middleware/filesystem.py`：只有 exit_code
被拼进正文 `[Command failed with exit code N]`）→ 会被 `is_error=False` 的调用方
当成成功，反而进入"文件识别"路径。而 `ValueError` 是工具层显式捕获并转
`status="error"` 的唯一预留通道。

## 调用方约定（生命周期）

- backend：`note_execute_path_violation(user_id, session_id, command)`（只举报）；
- chat.py：开轮 `clear_execute_path_violation(...)` 清残留（防上一轮断连遗留被
  下一轮误当自己的信号，与 `pop_fallback_signal(thread_id)` 同款铁律），
  收尾 `consume_execute_path_violation(...)` 取走（唯一消费者，pop 语义）。

## 后续演进：C｜治本方案（本次**未做**，记录在此备立项）

A+B 是应用层的"拦截 + 救济"，都建立在同一个前提上：**沙箱没有挂载宿主报告卷**。
只要这个前提还在，沙箱内的 `/reports`、`/uploads` 就永远是与宿主无关的空目录，
模型也永远可能把它们当成真目录（提示词只是降低概率）。

**C 的做法**：把宿主 `/data/reports/{uid}/{sid}`（可写）与
`/data/uploads/{uid}/{sid}`（只读）**bind-mount 进沙箱**。通道不对称随之消失：
shell 怎么写都落在正确位置，A/B 退化为冗余保险，模型也不再需要理解"虚拟路径"
这个概念。这是唯一能同时消掉"用错通道"和"滑网面"的方案。

**本次不做的理由（都是硬约束，不是省事）**：

1. **不在应用层可控范围内**：`create_sandbox`（`src/infra/sandbox.py`）当前没有任何
   mount 参数，挂载能力要由 Cube 模板/宿主侧基础设施提供 —— 属跨层改动，
   且必须与 CubeSandbox（MicroVM）的隔离模型一起评审，无法作为后端补丁交付。
2. **写入语义发生质变**：沙箱内**任意进程**（含模型通过 pip 装的第三方库、
   被解析的 PDF/脚本）都能直写宿主报告卷。这会把"沙箱逃逸面"从"控制面"
   扩到"数据面"，需要重新评估配额、审计、清理与恶意文件投放风险。
3. **权限模型需 Cube 侧支持**：`/uploads` 只读 + `/reports` 可写 + 多租户路径隔离，
   要靠只读 bind / 子路径白名单实现，当前模板未暴露这类能力。
4. **与生命周期的交互未验证**：沙箱 5min 空闲 TTL 回收、快照恢复时，
   挂载点内**残留的宿主文件**如何处理（是否会被当成沙箱产物、是否参与快照）
   需要实测，不能靠推断。

**触发立项的条件**（满足任一即应重估优先级）：
- A 的滑网面在生产被实际观察到（模型用变量拼接/base64/通配符绕过字面量护栏）；
- 出现"必须让沙箱内第三方工具直接产出交付物"的需求（如大型二进制生成、
  多文件工程打包），此时逐文件 `download_from_sandbox` 的往返成本变得不可接受。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


#: 虚拟路径白名单守卫的轮次信号表。key = "user_id:session_id"
#: （backend 只持有 user/session 上下文，拿不到 thread_id；与 Langfuse 的
#:  sessionId 同风格 `GY35377:60c64c4f`）。
#: OrderedDict + 窗口上限：无界 dict 会在长跑进程里慢性泄漏（同
#: `terminal_response._FALLBACK_SIGNALS` 的处理）。
_VIOLATIONS: OrderedDict[str, dict[str, Any]] = OrderedDict()
_VIOLATION_WINDOW = 500
_violation_lock = threading.Lock()


def _key(user_id: str | None, session_id: str | None) -> str:
    return "%s:%s" % (user_id or "", session_id or "")


def note_execute_path_violation(
    user_id: str | None,
    session_id: str | None,
    command: str | None = None,
) -> None:
    """登记"本轮 execute 引用了虚拟路径"（只举报，不做任何 I/O 或沙箱往返）。

    为什么 backend 只举报、不在原地救济：`execute`/`aexecute` 是**工具调用热路径**，
    在同步版本里做沙箱列举/下载会占住事件循环（同 `_pull_report_from_sandbox`
    docstring 记载的教训）；而回收动作（含路径穿越防御、白名单、上限、大小排序）
    在 API 层已有成熟实现，重复一份必然漂移。此处只留最小事实：
    "这个会话这一轮发生过越界写尝试"。

    同一轮内多次命中只保留最后一次命令摘要（信号是布尔语义，不需要计数）。
    """
    key = _key(user_id, session_id)
    with _violation_lock:
        _VIOLATIONS[key] = {
            "user_id": user_id or "",
            "session_id": session_id or "",
            # 只留摘要：这是日志/排障用，不是判据；避免把整条命令（可能很长、
            # 可能含用户数据）在进程内存里留存
            "command_head": (command or "")[:300],
        }
        _VIOLATIONS.move_to_end(key)
        while len(_VIOLATIONS) > _VIOLATION_WINDOW:
            _VIOLATIONS.popitem(last=False)


def peek_execute_path_violation(
    user_id: str | None, session_id: str | None
) -> dict[str, Any] | None:
    """只看不取（用于"要不要跑兜底"的条件判断，消费点仍用 consume）。"""
    with _violation_lock:
        hit = _VIOLATIONS.get(_key(user_id, session_id))
        return dict(hit) if hit else None


def consume_execute_path_violation(
    user_id: str | None, session_id: str | None
) -> dict[str, Any] | None:
    """取走信号（pop 语义，唯一消费者 = chat.py 收尾的兜底回收）。

    取走而非读取：留着会被**下一轮**当成自己的信号，让一轮正常回复白白多扫一次
    沙箱（同 `pop_fallback_signal` 的生命周期铁律）。
    """
    with _violation_lock:
        return _VIOLATIONS.pop(_key(user_id, session_id), None)


def clear_execute_path_violation(user_id: str | None, session_id: str | None) -> None:
    """开轮清残留：上一轮断连时登记的信号不会自己消失，必须显式清。"""
    with _violation_lock:
        _VIOLATIONS.pop(_key(user_id, session_id), None)


def virtual_path_write_hint(command: str | None, report_prefix: str) -> str:
    """A 的拒绝文案：把"环境错误"升级为"规则违反 + 正确通道 + 禁宣称"。

    返回文案会被塞进 `ToolMessage(status="error")` 的 content，所以它同时是
    **给模型的操作指令**，不是给用户的报错——必须包含"下一步怎么做"。

    `report_prefix` 由 backend 传入（形如 `/reports/{uid}/{sid}/`）；backend 未拿到
    用户上下文时是占位形式 `/reports/<user_id>/<session_id>/`，仍可指路。
    """
    return (
        "拒绝执行：命令引用了虚拟路径 '/reports' 或 '/uploads'，这两条路径"
        "**在沙箱内并不存在**（沙箱没有挂载宿主报告卷/上传卷）。命令未执行。\n"
        "你看到的 'No such file or directory' 不是'目录没建好'，而是**通道用错了**："
        "在沙箱里 mkdir 出来的 /reports 只是 MicroVM 内的空目录，5 分钟后随沙箱"
        "回收一起消失，用户永远下载不到。\n"
        "请改用正确通道：\n"
        f"1) 交付物（报告/表格/PPT/图片等）→ write_file(file_path='{report_prefix}<文件名>')，"
        "它直写宿主报告卷，用户立即可下载；\n"
        "2) shell/脚本产出的文件 → 先让脚本写到沙箱内 '/home/user/<文件名>'，"
        "再 download_from_sandbox(path='/home/user/<文件名>', "
        f"remote_path='{report_prefix}<文件名>') 拉回宿主；\n"
        "3) 读取用户上传的输入文件 → 不要用 shell 读 /uploads/...，"
        "用 read_file，或二进制文档走 decrypt_and_upload_to_sandbox。\n"
        "⚠️ 在拿到 download_from_sandbox / write_file 的成功回执之前，"
        "禁止对用户声称'文件已保存/可直接下载'。"
    )

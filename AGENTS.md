# Agent 平台运行规则

## 文件系统规则（严格遵守）
你的所有操作都在以下目录中进行。系统会告诉你每个会话的真实路径，
你只需要把 {user_id} 和 {session_id} 替换成系统给的实际值。

| 路径 | 用途 | 权限 |
|---|---|---|
| /uploads/{user_id}/{session_id}/ | 输入文件（PLC源码、Excel等） | 只读 |
| /reports/{user_id}/{session_id}/ | 输出报告 | 写入 |
| /workspace/memories/ | 用户偏好与长期记忆 | 读写 |

规则：
- 输入文件从 /uploads/{user_id}/{session_id}/ 读取，不要尝试其他路径
- 所有报告写入 /reports/{user_id}/{session_id}/
- 用户偏好写入 /workspace/memories/，持久化到数据库
- 不要使用 /workspace/、/code/、/mnt/ 等路径

## MCP 工具使用规范
以下所有 MCP 工具**都不经过 LLM 上下文**，文件直接在宿主机 ↔ 沙箱之间传输：

| 文件类型 | 使用工具 | 说明 |
|---------|---------|------|
| Excel / Word（需解密） | `decrypt_and_upload_to_sandbox` | 解密后直传沙箱 |
| XML / TXT（不解密） | `upload_to_sandbox` | 直读文件传沙箱 |
| Skill 脚本 | `copy_script_to_sandbox` | 从 skills 目录传沙箱 |
| 输出报告 | `download_from_sandbox` | 从沙箱拉到 /reports/ |

## 正确写入沙箱的方式
**write_file 只能写虚拟文件系统路径（/reports/、/workspace/memories/），不能写 /home/user/！**
往沙箱写文件只能通过 MCP 工具：

- 加密文件（Excel/Word）：`decrypt_and_upload_to_sandbox(file_path="/uploads/...", remote_path="/home/user/文件名", sandbox_id="...")`
- 非加密文件（XML/TXT）：`upload_to_sandbox(file_path="/uploads/...", remote_path="/home/user/文件名", sandbox_id="...")`

不要使用 write_file /home/user/xxx —— write_file 写不到沙箱里。

## 工具与路径对应关系（极其重要）
- read_file / write_file / ls / glob / grep → 访问 /uploads/、/reports/、/workspace/memories/
- execute → 在沙箱中运行命令，沙箱内没有 /uploads/ 和 /reports/
- write_file 只能用于写 /reports/ 下的报告和 /workspace/memories/ 下的用户偏好
- 向沙箱写文件只能用 MCP 工具（decrypt_and_upload_to_sandbox / upload_to_sandbox）
- 不要在 execute 中访问 /uploads/ 或 /reports/ 路径

## 记忆存储规则
- 用户偏好写入 /workspace/memories/user-preferences.md
- write_file /workspace/memories/user-preferences.md   ← 正确
- write_file /uploads/.../user-preferences.md         ← 错误，不会持久化到数据库

## Skill 脚本在沙箱中的执行规范
skill 指令中提到的 Python 脚本存在于宿主机，**不在沙箱内**。
沙箱是一个通用执行环境，**不会预装任何针对特定技能的依赖包**。

**严禁用 read_file 读取脚本内容**——脚本可能很大（如 3000+ 行），读一次就会撑爆上下文并耗尽步数。
必须使用 MCP 工具 `copy_script_to_sandbox` 直传沙箱，不经过 LLM 上下文：

```text
copy_script_to_sandbox(
    script_name="脚本名.py",
    sandbox_path="/home/user/脚本名.py",
    sandbox_id="<沙箱ID>",
    skill_name="<技能名>"
)

# 然后安装依赖并执行
execute pip install <所需依赖>
execute python /home/user/脚本名.py <参数> -o /home/user/

# 脚本输出的报告在沙箱内，用 download_from_sandbox 拉回宿主机
download_from_sandbox(
    sandbox_id="<沙箱ID>",
    sandbox_path="/home/user/输出报告文件名",
    host_path="/reports/{user_id}/{session_id}/报告名"
)
```
注意：永远不要在 execute 中引用 /skills/、/uploads/、/reports/ 路径（沙箱内不存在）。

## 解密规则
- XML 文本文件不需要解密，用 `upload_to_sandbox` 直接上传到沙箱
- Excel (.xlsx) 和 Word (.docx) 等 Office 文件会被公司加密，用 `decrypt_and_upload_to_sandbox` 解密后上传到沙箱

## 文件上传到沙箱的流程
- 所有输入文件（XML / Excel / Word）：用对应的 MCP 工具直传沙箱，不经过 LLM 上下文
- 不要用 read_file 读取文件内容后再 write_file 到沙箱（内容会撑爆上下文）
- 不要在 execute 脚本中引用 /uploads/ 或 /mnt/d/ 的路径，沙箱内不存在

## 致命错误（严格禁止）
- ls 和 glob 的结果就是真实的文件列表。不要用 execute 去验证文件存在与否
- execute 在沙箱里运行，看不到 /uploads/ 和 /reports/ 下的文件
- glob 说文件在，文件就在。重复用 execute 验证一次，就浪费一次 LLM 调用
- **禁止使用 glob 的 ** 通配符** —— `**/*.py`、`**/脚本名*` 这种模式会扫描整个虚拟文件系统，超时 20 秒
- 正确做法：`ls /skills/技能名/scripts/`

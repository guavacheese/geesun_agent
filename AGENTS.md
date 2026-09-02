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
| 任意文件（含加密） | `upload_to_sandbox` | **统一入口**：自动检测 `%TSD-Header` 加密头，命中即内部切换为解密上传（等价 `decrypt_and_upload_to_sandbox`，明文不落盘）；非加密文件直传沙箱 |
| Skill 脚本 | `copy_script_to_sandbox` | 从 skills 目录传沙箱 |
| 输出报告 | `download_from_sandbox` | 从沙箱拉到 /reports/ |

> `decrypt_and_upload_to_sandbox` 仍保留可用，但模型**无需再手动区分**加密/非加密——一律用 `upload_to_sandbox` 即可，工具会自行兜底解密。

## 正确写入沙箱的方式
**write_file 只能写虚拟文件系统路径（/reports/、/workspace/memories/），不能写 /home/user/！**
往沙箱写文件只能通过 MCP 工具：

- 任意文件（加密/非加密统一）：`upload_to_sandbox(file_path="/uploads/...", remote_path="/home/user/文件名", sandbox_id="...")`
  - 工具会自动检测 DLP 加密头（`%TSD-Header`），命中即内部解密上传，无需手动选工具
  - （`decrypt_and_upload_to_sandbox` 仍可用，行为等价，但不再需要区分调用）

不要使用 write_file /home/user/xxx —— write_file 写不到沙箱里。

## write_file 路径规则（严格）

**⚠️ 严重警告：文件只能写到 `/reports/{user_id}/{session_id}/`，写错位置会导致文件无法预览和下载。**

- `/uploads/` 是**用户上传的输入文件**目录，**只读**，不可写入
- `/reports/` 是**Agent 生成的输出文件**目录，**可写**，所有给用户下载的文件必须写到这里
- 写错到 `/uploads/` 的文件虽然会存在宿主机磁盘上，但**不会触发 `file_generated` 事件**，导致前端不显示文件卡片，用户无法预览和下载

**判断准则（每次 write_file 前必须确认）：**
1. 文件是给用户预览/下载的 → **必须**写 `/reports/{user_id}/{session_id}/`
2. 文件是用户偏好 → 写 `/workspace/memories/`
3. 其他路径（`/uploads/`、`/home/user/`、`/tmp/` 等）→ **不允许**

## write_file 与 download_from_sandbox 的生命周期关系（极其重要）
**write_file 写到 /reports/ 的文件，已经在宿主机上，不需要再 download。**

write_file 通过 filesystem middleware 直写宿主机磁盘，不经沙箱。
因此：
- **如果你用 write_file 创建了 /reports/... 下的文件，该文件已经在宿主机的报告目录中，立即可用。**
- **不要对 write_file 刚写好的文件再调 download_from_sandbox**——那会试图从沙箱下载同一个文件，不仅多余，而且在沙箱未创建时必定失败。
- **download_from_sandbox 的唯一适用场景**：在沙箱内用 `execute` 运行脚本，脚本在沙箱文件系统 `/home/user/` 下生成了输出文件，需要拉回宿主机。
- 判断准则：文件路径是 `/home/user/` → 需要 download；文件路径是 `/reports/` 且刚用 write_file 创建 → 不需要 download。

## 工具与路径对应关系（极其重要）
- read_file / write_file / ls / glob / grep → 访问 /uploads/、/reports/、/workspace/memories/
- execute → 在沙箱中运行命令，沙箱内没有 /uploads/ 和 /reports/
- write_file 只能用于写 /reports/ 下的报告和 /workspace/memories/ 下的用户偏好
- 向沙箱写文件只能用 MCP 工具：`upload_to_sandbox`（统一入口，自动解密 DLP 加密文件）或 `decrypt_and_upload_to_sandbox`（等价，显式解密）
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
)   # 自动搜索 __system__ → __agent__ → __user_*__，无需指定来源

# 然后安装依赖并执行
# 安装依赖（沙箱下载包速度一般，设置 timeout=600 最多等10分钟）
execute pip install <所需依赖> timeout=600
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
- **`upload_to_sandbox` 已内置 DLP 兜底**：自动检测 `%TSD-Header` 加密头，命中即内部切换为解密上传（与 `decrypt_and_upload_to_sandbox` 等价，明文不落盘）。因此**一律用 `upload_to_sandbox` 即可，无需判断文件是否加密、也无需手动换工具**。
- `decrypt_and_upload_to_sandbox` 仍保留可用，行为完全等价，仅在需要显式表达"我要解密"语义时使用。
- **判断文件是否加密：不靠扩展名，靠文件头**。公司 DLP 加密软件会在文件开头写入 `%TSD-Header` 魔数——**txt/py 等文本类文件也可能被加密**。不确定时：
  1. 先尝试 `read_file`——如果被系统拒绝（二进制拦截提示），说明是加密/二进制文件
  2. 或直接走 `upload_to_sandbox`（它内部已自动检测加密头并兜底解密，非加密文件同样直传）
- **禁止用 read_excel 直接读取 /uploads/ 下的加密 Office 文件**：`/uploads/` 是虚拟路径，只在虚拟文件系统（read_file/ls/glob/grep）和 MCP 传输工具里有效；read_excel 底层用真实文件系统 open()，宿主机与沙箱均无 `/uploads` 目录，必然报 No such file；且文件为密文，路径通了也读不出内容
- 加密文件的正确读取流程：`upload_to_sandbox(file_path="/uploads/...", remote_path="/home/user/文件名", sandbox_id="...")` → 工具自动解密 → 沙箱 `/home/user/` 下用 execute / read 处理
- 同一目录下的文件名可用 `ls /uploads/{user_id}/{session_id}/` 确认（虚拟文件系统可列出），但**不要**用 execute 在沙箱里验证 /uploads（沙箱内不存在）
- **PDF 解析必须用标准库 API，禁止手动遍历 PDF 内部对象**：
  1. `fitz`（PyMuPDF）：`doc = fitz.open(path)` → `doc[页码].get_text()` 提取文本
  2. 或 `pdfplumber`：`with pdfplumber.open(path) as pdf` → `page.extract_text()`
  3. **禁止**自己写脚本遍历 PDF 的 CMap 字符映射/对象流——那会把二进制映射数据当文本输出，表现为"乱码"（2026-08-13 实测：AI 手动遍历 CMap 得到 65309 条乱码段；同文件用 fitz.get_text() 解析完全正常）
  4. 若 `get_text()` 返回空/乱码，先检查文件头：`%TSD-Header` 说明仍是密文（未解密），重新 `upload_to_sandbox`（会自动解密）；`%PDF-` 才是明文
- **沙箱内 pip 装包默认走内网 devpi 源**（沙箱创建时已配置
  `http://192.168.10.136:3141/root/pypi/+simple/`），直接 `pip install <包名>` 即可；
  **不要改 pip 源/加 --trusted-host**——外网 pypi https 被公司上网认证网关
  MITM 重签证书，公共 CA 不认，现场改源必然失败（2026-08-14 实测）
- pymupdf/pdfplumber 等常用库已预装在镜像内，通常无需再装；装其他包（如 openpyxl）直接 pip install

## 沙箱内文件写入（v3.1）
- **write_file 支持直接写沙箱路径**：`/home/user/xxx` 和 `/tmp/xxx` 已放行，会经 e2b 上传通道写入沙箱（仅 UTF-8 文本）
- 写脚本/中间文件直接用 `write_file /home/user/脚本.py`，或 `execute` 里 shell 创建，二选一即可
- `/root/`、`/mnt/`、`/code/`、`/var/` 是沙箱内系统级/挂载路径，禁止写入
- **沙箱内文件不算交付物**：要给用户看的报告必须 `download_from_sandbox` 到 `/reports/{user_id}/{session_id}/`

## Skill 创建（v3.1）
- Skill 分三层：`/skills/__system__/`（预装，只读）、`/skills/__agent__/`（agent 自创，**可写**）、`/skills/__user_{user_id}__/`（用户上传，归 /api/v1/skill/upload 管，AI 只读）
- **创建/更新自创 skill**：`write_file /skills/__agent__/<skill_name>/SKILL.md`（YAML frontmatter 需含 name + description，name 与目录名一致；格式不合法该 skill 不会被加载）
- **用户共享 skill**：通过 `/api/v1/skill/upload` 接口（前端"技能"面板上传），不要直接 write_file 用户目录
- 不要写 `/skills/__system__/`（系统预装只读）

## Skill 优先使用规则（强制，违反即白烧步数）
- **接到任务先查 skill**：`ls /skills/__system__/`、`ls /skills/__agent__/`、
  `ls /skills/__user_*__/`，确认是否存在覆盖当前任务能力的 skill
  （PDF 比对/PLC 审查等高频场景都有现成 skill）。
- **有匹配 skill 必须用**：按该 skill 的 SKILL.md 流程执行
  （copy_script_to_sandbox 传脚本 → execute 运行），**禁止**绕开 skill
  自己现场写同等能力的算法脚本。
- **禁止"造轮子"**：不得以"脚本有 bug 我重写一个更好的"为由另起炉灶——
  已有 skill 时，任何现场编写的同功能脚本都是重复劳动。
- **唯一例外**：确认全部 skill 目录（__system__/__agent__/__user_*__）
  均无覆盖任务能力的 skill 后，才允许写一次性脚本；写完直接执行，
  不要陷入"写脚本→报错→改脚本→重跑"循环，连续 2 次运行失败即停手
  汇报问题，禁止无限调试。

## 文件上传到沙箱的流程
- 所有输入文件（XML / Excel / Word）：用对应的 MCP 工具直传沙箱，不经过 LLM 上下文
- 不要用 read_file 读取文件内容后再 write_file 到沙箱（内容会撑爆上下文）
- 不要在 execute 脚本中引用 /uploads/ 或 /mnt/d/ 的路径，沙箱内不存在

## 致命错误（严格禁止）
- ls 和 glob 的结果就是真实的文件列表。不要用 execute 去验证文件存在与否
- execute 在沙箱里运行，看不到 /uploads/ 和 /reports/ 下的文件
- glob 说文件在，文件就在。重复用 execute 验证一次，就浪费一次 LLM 调用
- **禁止使用 glob 的 ** 通配符** —— `**/*.py`、`**/脚本名*` 这种模式会扫描整个虚拟文件系统，超时 20 秒
- 正确做法：`ls /skills/__system__/技能名/scripts/`

## 沙箱环境与完成门（系统兜底，勿对抗）
- **环境快照由系统自动注入**：每条用户消息会附带 `【沙箱环境】` 段（已装工具、磁盘空间、rustup toolchain），直接使用即可。**禁止**自行 `which`/`df`/重装工具/下载 toolchain——重装必然超时或磁盘不足，属于已知弯路
- **完成门系统校验**：任务结束时系统会检查 `/reports/{user_id}/{session_id}/` 是否有本轮新文件。零产出会被拦截并打回提示，**必须**把交付物（报告/代码/结果文件）落到 reports 目录才算任务完成
- 提示词只是引导，不负责兜底；以上行为由服务端强制校验，违反只会浪费你自己的步数

## 已知部署坑与运行提示（防复发，供诊断参考）
- **"model 调用超过 600s（vLLM 无响应或过慢）" ≠ vLLM 宕机**：根因是 vLLM 曾带 `--enforce-eager`/`--tokenizer-mode slow`（禁用 CUDA graph）致 decode 仅 12-14 tok/s，长生成跑满客户端 600s 预算。已去 flag 根治（decode ~190 tok/s，约 15×）。遇此类超时先看 vLLM 引擎日志（`journalctl -u vllm-qwen3.5.service`）的 `Avg generation throughput` / `request_aborted` 再下结论，不要断言"模型挂了"。
- **会话"刷新后消息丢失/空白"**：根因是 Postgres store/saver 单连接并发碰撞（`another command is already in progress`）导致读取被吞/断连兜底写失败。已改连接池根治（store `pool_config`、saver 手建 `AsyncConnectionPool`）。再遇此现象先查 `server.log` 该错误与 Postgres `messages` 表数据是否在，勿直接判定数据丢失。
- **Phoenix 19.x 按标准 `project.name` 资源属性对 trace 分组**：缺该属性时所有 trace 落 `default` 项目；旧的 `openinference.project.name` 在 19.x 已被忽略（设了也不分组）。`src/core/tracing.py` 的 TracerProvider Resource **已双设** `project.name` + `openinference.project.name`，值取自 `config.py` 的 `OTEL_PROJECT_NAME`（prod `.env` 设 `Geesun-Agent-prod`）。若 Phoenix UI 只看到 `default` 而看不到业务项目，先查该环境变量与 tracing.py 是否两属性都在，不要去 Phoenix 管理台手建项目（项目会随首条 trace 自动出现）。
- **swarm 镜像部署：源码改动必须回构建机 rebuild+push 才生效**：geesun-agent / geesun-mcp / geesun-agent-web 均跑预构建镜像（Harbor `geesun_ai`，tag 由 `.env` 的 `GEESUN_AGENT_TAG` 等控制），生产机只 `start_stack.sh --no-build` 拉镜像刷新。改了 `src/` 后若仅在生产机改文件**不会生效**——必须在构建机跑 `deploy/build-push.sh`（或 `docker build -t 172.16.220.74:8333/geesun_ai/geesun-agent:<新tag>` 后 `docker push`），再上生产机改 tag + `--no-build --with=phoenix,langfuse,mcp,web` 重启。镜像 tag 只认正斜杠 `/`，反斜杠 `\` 会报 `invalid reference format`。
- **可观测栈统一随主栈自托管（2026-09-02 起）**：Phoenix（`--with=phoenix`）与 Langfuse（`--with=langfuse`）均已并入本 swarm stack，网络统一 `appnet2`，UI 分别发布 `6006` / `3000`、Minio S3 发布 `9092`，后端 postgres/redis/clickhouse 仅 appnet2 内不占主机端口。旧「复用现网共享实例」方案已废弃。**迁移前须先停用旧共享 Langfuse 栈**（占 3000/3030/5432/6379/8123/9000/9091/9092，无人自愈），否则与我方栈抢 3000/9092。Langfuse 自托管后 `LANGFUSE_PUBLIC_KEY/SECRET_KEY` 须对应**本实例**项目（用 `LANGFUSE_INIT_*` 环境变量首次启动时自动建项目并产出 key），不能填旧共享实例的 key。
- **附加 compose 网络名铁律**：所有 `docker-compose.*.yml` 附加文件（mcp / web / phoenix / langfuse）均引用主栈同名的 overlay 网络 `appnet2`，**不得**写 `appnet`（历史上 langfuse 文件曾写 `appnet` 导致并入后与主栈不通）。合并只看 `start_stack.sh --with=` 传哪些文件 + 对应 `*_BASE_URL` 端点。

# 日志配置必须在任何其他导入之前就绪，否则 logger.warning 会丢失时间戳
from src.core.logging import *  # noqa: F401,F403

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    base_url: str
    openai_api_key: str
    model_name: str
    extra_models: str = "[]"  # 额外模型，JSON 数组：[{"model_name":"...","base_url":"...","api_key":"..."}]
    agent_workspace: str

    upload_root: str = "/data/myapp/uploads"
    report_root: str = "/data/myapp/reports"
    mcp_token: str = "YOUR_TOKEN"
    # MCP 服务地址（streamable-http）。默认 dev 同机裸跑用 localhost:8000；
    # 生产 docker-compose 下 mcp 容器化进同一 appnet，改为服务名 geesun-mcp:8000。
    mcp_server_url: str = "http://localhost:8000/mcp"

    # Arize Phoenix 追踪（从 .env 或环境变量读取）
    phoenix_collector_endpoint: str = ""

    # Langfuse 追踪（OpenTelemetry HTTP ingest）
    langfuse_secret_key: str = ""
    langfuse_public_key: str = ""
    langfuse_base_url: str = ""

    # OpenTelemetry 项目名（dev / prod 区分，便于在 Phoenix / Langfuse 中隔离 trace）
    otel_project_name: str = "Geesun-Agent"

    # 允许的前端跨域源（逗号分隔）；生产部署填 Web 实际域名 / IP
    cors_allow_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    cube_template_id: str = ""
    cube_api_url: str = ""
    cube_api_key: str = "e2b_0000000000000000000000000000000000000000"

    # ─── 沙箱护栏（设计文档 M1：环境快照注入 + 磁盘前置校验）───
    # 覆盖默认探测命令白名单（JSON 数组），空 = 用 sandbox.py 内置默认
    sandbox_probe_commands: str = ""
    # 磁盘可用空间阈值（MB）：低于 warn 注入警告提示模型避开编译类任务；
    # 低于 hard 直接在 chat 入口拒绝启动任务（HTTP 503）
    sandbox_disk_warn_mb: int = 200
    sandbox_disk_hard_mb: int = 50
    # 环境快照缓存 TTL（秒）：同一 thread_id 的沙箱在 TTL 内复用快照，避免每轮 chat 重跑探测
    sandbox_probe_ttl_sec: int = 60
    # 沙箱空闲回收 TTL（秒）：CubeSandbox v0.6.0 起 cube-lifecycle-manager 按此值回收空闲沙箱；
    # 每次 execute 会自动续期（POST /sandboxes/:id/timeout），-1 = 永不过期。
    # 若任务存在两轮工具调用间隙 > 此值的场景（如模型长思考），调大该值。
    # 300 → 3600：AI 单步生成（大脚本/长文本）可达数分钟，300s 会在思考期间被回收
    # （2026-08-13 实测 CubeMaster 130404 sandbox id not found：沙箱闲置 5 分钟过期，
    #  execute 前 refresh_timeout 救不回已死的沙箱）
    sandbox_idle_timeout_sec: int = 3600
    # ─── M3 完成门（设计文档）：零产出不放行结束 ───
    # 总开关（M1/M2/M3 回滚开关，False = 全部关闭护栏）
    sandbox_guardrails_enabled: bool = True
    # 完成门检测：False = 不做零产出校验（回到旧行为）
    sandbox_completion_gate_enabled: bool = True
    # 自动继续：零产出时注入 SystemMessage 让模型再跑一轮。
    # ⚠ 依赖 deepagents astream 继续模式（spike_checkpoint_resume.py 验证），
    #   spike 通过前保持默认 False（此时完成门为纯检测 + blocked 事件，即 v1 行为）；
    #   验证通过后在 .env 设 SANDOX_COMPLETION_GATE_AUTO_CONTINUE=true 开启 v2。
    sandbox_completion_gate_auto_continue: bool = False
    # 最多打回次数，超限标记任务失败终止
    sandbox_completion_gate_max_retries: int = 2
    # 工具连续失败阈值：单轮内连续失败（error/超时）达到该次数即提前终止，
    # 避免模型在错误循环里空转直到烧满 recursion_limit（100 步，约几分钟白等）
    sandbox_tool_failure_threshold: int = 5
    # ─── P0 无进展循环检测（2026-08-19 新增）───
    # 工具"成功但无进展"死循环兜底：同一工具调用意图（工具名+参数指纹）
    # 连续出现 N 次且期间零新交付物 → 判定循环，注入收敛 SystemMessage。
    # 与 sandbox_tool_failure_threshold 互补：那个管"失败循环"，这个管"成功空转"。
    no_progress_repeat_threshold: int = 5
    # 收敛注入最多次数：超限后不再注入，直接放弃终止（防无限收敛轮）
    no_progress_max_injections: int = 1
    # 无进展窗口判定：重复 N 次内是否有新交付物（file_generated / reports 新文件）
    no_progress_window_files: int = 3
    # ─── model 调用总时长超时（2026-08-19 新增）───
    # openai SDK 的 timeout 是"字节间隔超时"（httpx read timeout），vLLM 慢速流式时
    # 永不触发（2026-08-19 实测 16.8 万 token prefill 挂 20 分钟无超时）。
    # 此处为"总时长"超时：单次 model 调用（含 prefill+decode 全流）超过即中止，
    # 抛错 → SSE error → M3 兜底，防永久挂起。
    model_call_timeout_sec: int = 600
    # ─── model 单次调用输出上限（2026-08-24 决策：thinking 保留，上限给足）───
    # Qwen3 保留 thinking（复杂任务需要深度思考），max_tokens 取 200000，
    # 接近 max_model_len=262144（~256k）上限、留 ~62k 余量。
    # 注意：vLLM 约束 prompt + max_tokens ≤ max_model_len——输入接近 262k 时
    # 200k 输出会 400，但 Summarization 在 20 万 tokens 触发压缩（keep=10 条），
    # 实际输入远小于上限；单次调用失控仍由 model_call_timeout_sec(600s) 兜底。
    model_max_tokens: int = 200000
    # ─── vLLM 上下文总上限（prompt + max_tokens 不得超过）───
    # 探活 /v1/models 得 max_model_len=262144；动态 max_tokens 用它做减法，
    # 防止"输入 62k + 输出 200k = 262145 > 262144"被 vLLM 400 拒载（2026-08-24 实测）。
    model_max_len: int = 262144
    # ─── 动态 max_tokens 安全边距（tokens）───
    # 防 prompt 估算低估触发 vLLM 400（2026-08-25 实测估算 125238 vs 实际 129335、
    # 低估 4097，margin=4096 差 1 token 又被拒）。16384 留足余量（含 tools schema 等未计入部分）。
    model_max_tokens_margin: int = 16384
    # ─── Summarization 压缩触发阈值（tokens）───
    # 上下文达此值即把历史压缩为摘要（keep 10 条 + 资源清单注入，前端消息表不受影响）。
    # 2026-08-25 从 200000 降到 100000：fe27a95a 反复失败重跑历史膨胀到 12.9 万 tokens
    # 仍未触发（阈值偏高 + get_num_tokens 对 Qwen 估算不稳定），prefill 慢且挤压输出空间。
    summarization_trigger_tokens: int = 100000
    # ─── 加密文件识别（v3.1 护栏）───
    # 判断"是否加密"不靠扩展名，靠文件头魔数（公司 DLP 加密软件特征头）
    dlp_header_signatures: tuple[str, ...] = ("%TSD-Header",)
    # 提示层用：这些扩展名"通常加密"，但最终以文件头为准（txt/py 也可能被加密）
    dlp_encrypted_ext_hints: tuple[str, ...] = (
        ".pdf", ".xlsx", ".xls", ".docx", ".doc", ".txt", ".py",
    )
    # read_file 硬拦截的二进制扩展名：直接读取会撑爆上下文或触发模型 API 501
    # （Qwen 不支持 file part）。命中即拒绝并提示走 decrypt_and_upload_to_sandbox 链路
    # 注意：图片类（png/jpg/gif 等）不在拦截名单——Qwen 支持 image_url part（2026-08-12 实测），
    # 图片由 file_to_image middleware 转成 image_url 后可直接视觉理解，无需拦截
    read_file_binary_exts: tuple[str, ...] = (
        ".pdf", ".xlsx", ".xls", ".docx", ".doc",
        ".zip", ".7z", ".rar",
    )

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "agent_mem"
    postgres_user: str = "postgres"
    postgres_password: str = ""

    # LDAP / AD 配置
    ldap_server: str = "ldap://192.168.1.241:389"
    ldap_base_dn: str = "DC=geesun,DC=li"
    ldap_bind_user: str = "geesunai"
    ldap_bind_password: str = ""
    ldap_domain_format: str = "%s@geesun.li"  # 用于构造 UPN：username@geesun.li
    ldap_admin_group_dn: str = "CN=geesun-admins,CN=Users,DC=geesun,DC=li"

    # JWT 配置
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24 * 7

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

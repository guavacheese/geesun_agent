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
    # 注意（2026-09-03）：生产 compose 默认注入 http://alloy:4317——alloy 是
    # trace/metrics 统一 OTLP 入口（grpc 同端口按 OTLP service path 分流），
    # 不再直连 Phoenix；本字段名保留 phoenix 但实际承担"OTLP gRPC 统一端点"。
    phoenix_collector_endpoint: str = ""

    # ─── OTel metrics（2026-09-03 新增，修复 genai_*/http_server_* 全 0）───
    # 此前 setup_tracing() 只注册 TracerProvider、从未创建 MeterProvider → metrics API
    # 走 no-op，进程不发任何 OTLP metrics 请求（alloy/Prometheus 空等）。此处补管道：
    # otel_metrics_enabled=False 可整体关闭（回归纯 trace）；
    # otel_metrics_endpoint 留空 = 复用 phoenix_collector_endpoint（推荐，同一 alloy 入口）。
    otel_metrics_enabled: bool = True
    otel_metrics_endpoint: str = ""

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
    # ⚠ 依赖 deepagents astream 继续模式（M3-v2 spike 已验证 astream 继续 + 消息注入可行），
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
    # ─── model 调用灾难性总时长超时（兜底中的兜底，2026-08-28 由 600 上调至 1800）───
    # openai SDK 的 timeout 是"字节间隔超时"（httpx read timeout），vLLM 慢速流式时
    # 永不触发（2026-08-19 实测 16.8 万 token prefill 挂 20 分钟无超时）；
    # 真正防"静默卡死"靠下方的 model_stream_chunk_timeout_sec（流式块间隔空闲超时）。
    # 本值仅作灾难性墙钟兜底：单次 model 调用（含 prefill+decode 全流）超过即中止，
    # 抛错 → SSE error → M3 兜底。原为 600s 会误杀 200k token 报告（实测 ~8min 生成），
    # 对齐 deer-flow / deepseek-harness「只在静默时杀、不按墙钟杀长生成」的思路上调到 1800。
    model_call_timeout_sec: int = 1800
    # ─── 流式块间隔空闲超时（2026-08-28 新增，真正的长任务护盾）───
    # 注入 ChatOpenAI stream_chunk_timeout：两次解析出的流式 chunk 之间超过该值即判死、
    # 抛错中止（只杀"引擎吐字停了"的静默流，不杀"正在正常长生成"的流）。
    # 对齐 deer-flow / deepseek-harness 的 run_streaming 空闲超时护长任务，解决 600s 墙钟
    # 误杀 200k 报告的问题。0/None 可关（依赖上方 model_call_timeout_sec 灾难性兜底）。
    model_stream_chunk_timeout_sec: int = 240
    # ─── model 单次调用输出上限 ───
    # 2026-08-27 修正：从 200000 降到 65536。
    # 原 200000 把 input 安全空间压到仅 45960（262144-200000-16384），
    # 中文 context 稍长即触发 vLLM 400（server.log 实测 input 62145 + 200000 = 262145 > 262144）。
    # 降到 65536 后 input 安全空间放大到 ~180k（262144-65536-16384），
    # 即便 token 计数低估中文 ~3x 也触碰不到 400 线；报告类任务 64k 输出足够。
    model_max_tokens: int = 65536
    # ─── vLLM 上下文总上限（prompt + max_tokens 不得超过）───
    # 默认值 262144 仅作 fallback；真实上限由两路获得（按优先级）：
    # ① model.py resolve_max_len() 启动探活 /v1/models 的 max_model_len 字段
    #   （2026-08-28 实测确认 vLLM 返回该字段，值为 262144），按 (base_url,model_name) 缓存；
    # ② 模型调用 400 时从错误 message 解析 "maximum context length is N tokens"（API 返回，100% 可靠）。
    # 注：原注释"探活 /v1/models 得 max_model_len=262144"此前未实现，已于 9c32f08 补齐为
    #   probe_model_max_len，2026-08-28 重构成 resolve_max_len（多模型缓存 + fallback 小表）。
    model_max_len: int = 262144
    # ─── 动态 max_tokens 安全边距（tokens）───
    # 16384 留足余量（含 tools schema 等未计入部分 + 计数误差缓冲）。
    model_max_tokens_margin: int = 16384
    # ─── Summarization 压缩触发阈值（tokens，仅作无 profile 时的下限保护）───
    # 2026-08-28 起触发改用 fraction（0.8 × model.profile["max_input_tokens"]），不再推导
    # effective_trigger；本值仅当模型 profile 缺失（fraction 退化）时作为下限保护。
    summarization_trigger_tokens: int = 20000
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

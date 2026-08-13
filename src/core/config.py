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

    # Arize Phoenix 追踪（从 .env 或环境变量读取）
    phoenix_collector_endpoint: str = ""

    # Langfuse 追踪（OpenTelemetry HTTP ingest）
    langfuse_secret_key: str = ""
    langfuse_public_key: str = ""
    langfuse_base_url: str = ""

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

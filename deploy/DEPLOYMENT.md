# geesun 生产部署 — 设计细节 / 变量清单 / 踩坑与排障

> 本文件是 `deploy/` 目录的部署参考，配合根 `README.md` 的 `## Deployment` 使用：
> - **端口矩阵、部署步骤、外部依赖**见 `README.md` 的 `## Deployment`（本文件不重复）；
> - 本文件额外收录：①设计细节（网络 / 卷前缀 / `limits` 生效差异，README 已交叉引用）②完整环境变量清单 ③实锤过的部署坑与修复（防复发）。
>
> 职责分离：**`AGENTS.md`（仓库根）= agent 沙箱运行时 obey 规则**（文件系统 / MCP 工具 / Skill 优先 / write_file 路径）；**本文件 = 部署 / 排障诊断参考**。两者不混。

---

## 1. 设计细节：网络 / 卷前缀 / limits 生效差异

### 1.1 网络：单一 overlay `appnet2`，服务名互访

- 主栈 `docker-compose.yml` 在 `networks:` 块定义 `appnet2`（`driver: overlay`）。各附加 compose（mcp / web / phoenix / langfuse）只写 `networks: [appnet2]` 引用它，**不重新定义**。
- 经 `start_stack.sh --with=...` 合并后，整栈是**同一个 swarm stack**（默认名 `geesun`），所以 `appnet2` 在所有被合并的 compose 间是同一张网络。swarm 下该网络实际名为 **`geesun_appnet2`**（`<STACK_NAME>_` 前缀，见 §1.2）。
- 容器间寻址用 **compose 里声明的短服务名**（Docker 嵌入式 DNS），例如：
  - agent → `http://geesun-mcp:8000/mcp`（`.env` 的 `mcp_server_url`）
  - agent / alloy → `http://alloy:4317`（`.env` 的 `PHOENIX_COLLECTOR_ENDPOINT`）
  - alloy → `phoenix:4317`（`.env` 的 `PHOENIX_OTLP_ENDPOINT`）
  - agent → `http://10.10.10.67:3000`（`.env` 的 `LANGFUSE_BASE_URL`，走主机 LAN IP，非服务名）
  - langfuse-web/worker → `postgres:5432` / `clickhouse:8123` / `redis:6379` / `minio:9000`
  - 主栈 → `agent-postgres:5432`（`.env` 的 `AGENT_PG_HOST`）
  - ⚠️ **不要用 stack 前缀名**（如 `geesun_geesun-mcp`）做 DNS——那是调度/任务名，不是网络 DNS 名，互相访问会解析失败。

### 1.2 卷前缀与持久化

- **命名卷**：swarm 会把 compose 里声明的命名卷加 `<STACK_NAME>_` 前缀。默认 `STACK_NAME=geesun`，故：

  | compose 里声明 | swarm 实际卷名 |
  | --- | --- |
  | `agent_pg_data` | `geesun_agent_pg_data` |
  | `caddy_data` / `caddy_config` | `geesun_caddy_data` / `geesun_caddy_config` |
  | `loki_data` | `geesun_loki_data` |
  | `grafana_data` | `geesun_grafana_data` |
  | `alloy_data` | `geesun_alloy_data` |
  | `prometheus_data` | `geesun_prometheus_data` |
  | `phoenix_pg_data` | `geesun_phoenix_pg_data` |
  | `langfuse_postgres_data` | `geesun_langfuse_postgres_data` |
  | `langfuse_clickhouse_data` / `langfuse_clickhouse_logs` | `geesun_langfuse_clickhouse_data` / `..._logs` |
  | `langfuse_minio_data` | `geesun_langfuse_minio_data` |
  | `langfuse_redis_data` | `geesun_langfuse_redis_data` |

- **持久化语义**：`docker stack rm geesun`（即 `stop_stack.sh`）会删服务与网络，但**保留命名卷**；重新 `start_stack.sh` 会复用同名卷、数据不丢。要彻底清空某卷须手动 `docker volume rm geesun_<name>`。
- **bind mount（宿主机路径）**：`${AGENT_DATA_ROOT}/...`、`${CA_MOUNT_SRC}`、`./Caddyfile`、`./alloy.config.alloy`、`./loki-config.yaml`、`./prometheus.yml`、`./grafana/provisioning` 等**不**加前缀，直接引用宿主机路径；单节点 swarm 下相对路径以 `docker stack deploy -c` 所在目录（即 `deploy/`）为基准解析（`../certs/combined-ca.pem` → 仓库根 `certs/`，与 `.env.example` 的 `CA_MOUNT_SRC` 一致）。这些路径/文件必须由 `setup-combined-ca.sh`、`init-host.sh` 等预先在节点上生成，否则 stack 起不来。

### 1.3 `limits` 生效差异（与 `docker compose up` 的关键区别）

- 本仓库所有 compose 用 **`deploy.resources.limits.{cpus,memory}`** 设限额（swarm 专用键）。**仅 `docker stack deploy`（swarm）会生效**；`docker compose up`（非 swarm）**完全忽略 `deploy.resources.limits`**，且这些 compose 没有配套写非 swarm 的 `mem_limit` / `cpus` 键，于是：
  - 若误用 `docker compose up` 拉起，**所有服务无 CPU/内存上限** → 失控容器可能撑爆节点内存（尤其 `langfuse-web`/`langfuse-worker` 需 2g、`clickhouse` 需 4g 的场景）。
  - **生产只走 `start_stack.sh`（`docker stack deploy`）**，不用 `docker compose up`。这是硬性约束。
- swarm 下 `memory` 限额是硬上限：容器超用会被 cgroup 杀（表现为 OOMKill 或进程 V8 `SIGABRT` exit 134，见 §3 的 Langfuse 内存坑）。`cpus` 是 CFS 配额硬上限。
- 当前各服务限额（核对自各 compose，2026-09-02）：

  | 服务 | cpus | memory | 文件 |
  | --- | --- | --- | --- |
  | geesun-agent | 2.0 | 2g | docker-compose.yml |
  | agent-postgres | 1.0 | 1g | docker-compose.yml |
  | caddy | 0.5 | 256m | docker-compose.yml |
  | loki | 1.0 | 1g | docker-compose.yml |
  | alloy | 0.5 | 512m | docker-compose.yml |
  | prometheus | 0.5 | 512m | docker-compose.yml |
  | grafana | 0.5 | 512m | docker-compose.yml |
  | geesun-mcp | 1.0 | 512m | docker-compose.mcp.yml |
  | geesun-agent-web | 1.0 | 512m | docker-compose.web.yml |
  | phoenix | 1.0 | 1g | docker-compose.phoenix.yml |
  | phoenix-db | 1.0 | 1g | docker-compose.phoenix.yml |
  | langfuse-web | 1.0 | **2g** | docker-compose.langfuse.yml |
  | langfuse-worker | 1.0 | **2g** | docker-compose.langfuse.yml |
  | clickhouse | 2.0 | 4g | docker-compose.langfuse.yml |
  | minio | 0.5 | 512m | docker-compose.langfuse.yml |
  | redis | 0.5 | 512m | docker-compose.langfuse.yml |
  | postgres（langfuse） | 1.0 | 1g | docker-compose.langfuse.yml |

### 1.4 端口发布模式（swarm `mode: ingress` vs `mode: host`）

- `mode: ingress`（默认，mesh 路由）：端口在**每个节点**经 IPVS VIP 发布。本栈用于 `caddy:80`、`grafana:3100`、`prometheus:19091`、`alloy:12345`、`phoenix:6006`、`minio:9092`（默认 ingress）。
- `mode: host`：docker-proxy 直绑节点网卡、不经 mesh。本栈仅 `langfuse-web:3000` 用（见 §3 的「主机端口 RST」坑——崩溃循环期间 ingress VIP 残留失效后端，改 host 绕开）。
- **不发布主机**：`geesun-agent:8009`、`geesun-mcp:8000`、`geesun-agent-web:3000`、`phoenix:4317/9090`、`langfuse-worker:3030`、`postgres/redis/clickhouse`（langfuse 后端）——仅 `appnet2` 内可达。
- 端口冲突是部署最常见失败：统一命令 `./start_stack.sh --with=phoenix,langfuse,mcp,web` 会占用主机 `80/3100/6006/12345/19091/3000/9092`；启用 Langfuse 自托管**前必须先停用旧共享 Langfuse 栈**释放 `3000/9092`。

### 1.5 env 注入与 `docker stack deploy` 不读 `.env` 的坑

- `docker stack deploy` **不会**像 `docker compose` 那样自动读取同目录 `.env` 文件，它只从**当前 shell 环境变量**展开 `${VAR}`。所以 `start_stack.sh` 第 47 行 `set -a; source "$ENV_FILE"; set +a` 先把 `deploy/.env` 注入 shell，再调 `docker stack deploy`——否则 `${REGISTRY_GEESUN}` 等全空、镜像名变 `/pgvector:...` → `invalid reference format`。
- compose 内服务侧用 `env_file: [.env]`（geesun-agent / geesun-mcp / alloy）把全部变量透传给容器；其余服务在 `environment:` 块逐个 `${VAR}` 引用。`.env` 含密钥，**不入库**（`chmod 600`）。
- `NEXT_PUBLIC_API_BASE` 是 **build-time** 变量：经 `build-push.sh` 的 `--build-arg` 内联进 web 镜像 JS 产物，运行时改 `.env` 无效（改前端地址须重建 web 镜像并递增 `WEB_TAG`）。

### 1.6 镜像 tag 与拉取策略

- 自有应用（geesun-agent / geesun-mcp-server / geesun-agent-web）来自 Harbor `geesun_ai`（`REGISTRY_GEESUN`），第三方通用镜像（pgvector / caddy / loki / alloy / prometheus / grafana / phoenix / langfuse / clickhouse / minio / redis / postgres）来自 `dockerhub`（`REGISTRY_HUB`），均由 `build-push.sh` 构建/同步。
- `start_stack.sh` 带 `--resolve-image=always`（固定 tag 重新发布时强制拉新镜像）+ `--with-registry-auth`（私有 Harbor 必需，依赖本机已 `docker login`）。改了源码须回构建机 `build-push.sh` 重推并**递增 tag**，否则 `--resolve-image=always` 拉到旧镜像。镜像 tag 只认正斜杠 `/`，反斜杠 `\` 报 `invalid reference format`。
- `depends_on` 在 swarm 仅支持**列表**写法（无 `condition: service_healthy`）；agent/mcp/web 连不上依赖时靠自身重试 + `restart_policy: any` 自愈，compose 文件里已有相应 `healthcheck` 供人工排查。

---

## 2. 完整环境变量清单（`deploy/.env`，对照 `.env.example`）

> 全部变量集中在 `deploy/.env`（单一可信源），各 compose 经 `env_file` / `environment` 引用。`.env.example` 内联注释含每项**取舍与生成命令**（如 `openssl rand -hex 32`），本表只列变量、默认值/示例与消费方。带 `REPLACE_ME` / `CHANGE_ME` 的必须替换；留空项按需填（如 `LANGFUSE_INIT_*` 留空则 UI 手动建）。

### 2.1 geesun_agent 基础（对接远端 vLLM / CubeSandbox）

| 变量 | 默认 / 示例 | 消费方 | 说明 |
| --- | --- | --- | --- |
| `base_url` | `http://172.16.66.13:8003/v1` | config.py → model.py | vLLM OpenAI 兼容端点（**8003 是 vLLM，非 E2B**，见 README 注） |
| `openai_api_key` | `sk-REPLACE_ME` | config.py | vLLM key |
| `model_name` | `Qwen3.6-35B-A3B` | config.py | 模型名 |
| `agent_workspace` | `/data/agent` | config.py + compose 挂载 | agent 工作根（容器路径） |
| `upload_root` | `/data/uploads` | config.py + compose 挂载 | 上传根 |
| `report_root` | `/data/reports` | config.py + compose 挂载 | 报告根 |
| `mcp_token` | `REPLACE_ME` | config.py（MCP 鉴权） | MCP 调用令牌 |

### 2.2 MCP / CubeSandbox / egress CA

| 变量 | 默认 / 示例 | 消费方 | 说明 |
| --- | --- | --- | --- |
| `mcp_server_url` | `http://geesun-mcp:8000/mcp` | config.py（MCP client） | 覆盖默认 localhost，走 appnet2 服务名 |
| `MCP_TAG` | `1.0.0` | docker-compose.mcp.yml | MCP 镜像 tag |
| `DECRYPT_API_URL` | `http://REPLACE_ME_DECRYPT_HOST:PORT/decrypt` | geesun-mcp 运行时 | DLP 解密网关（compose 外，待替换） |
| `E2B_API_URL` | `http://172.16.66.13:6000` | config.py / sandbox | CubeSandbox E2B 控制面（**6000 不是 8003**） |
| `E2B_API_KEY` | `REPLACE_ME` | config.py / sandbox | E2B key |
| `SSL_CERT_FILE` | `/etc/ssl/certs/combined-ca.pem` | agent / mcp 容器 env | 必须用 combined-ca.pem（mkcert+系统根），见 README §0 |
| `CA_MOUNT_SRC` | `../certs/combined-ca.pem` | compose 卷挂载源 | 宿主机 CA 路径，由 `setup-combined-ca.sh` 生成 |

### 2.3 agent 可调优 / 覆盖项（config.py 大小写不敏感）

| 变量 | 默认 / 示例 | 消费方 | 说明 |
| --- | --- | --- | --- |
| `extra_models` | `[]` | config.py | 额外模型列表 |
| `model_max_tokens` | `65536` | config.py | 模型单次输出上限 |
| `model_call_timeout_sec` | `1800` | config.py | 模型调用超时（秒） |
| `model_max_len` | `262144` | config.py | 模型上下文长度 |
| `model_max_tokens_margin` | `16384` | config.py | 输出上限余量 |
| `summarization_trigger_tokens` | `20000` | config.py（SummarizationMiddleware） | 触发摘要的 token 阈值 |
| `cube_template_id` | （空） | config.py | CubeSandbox 模板 ID |
| `cube_api_url` | `http://172.16.66.13:6000` | config.py | 同 E2B_API_URL（Cube 控制面） |
| `cube_api_key` | `e2b_...` | config.py | Cube key |
| `ldap_server` | `ldap://192.168.1.241:389` | config.py（auth） | LDAP/AD 地址 |
| `ldap_base_dn` | `DC=geesun,DC=li` | config.py | 基 DN |
| `ldap_bind_user` | `geesunai` | config.py | 绑定用户 |
| `ldap_bind_password` | `REPLACE_ME` | config.py | 绑定密码 |
| `ldap_domain_format` | `%s@geesun.li` | config.py | 域格式 |
| `ldap_admin_group_dn` | `CN=geesun-admins,...` | config.py | 管理员组 |
| `jwt_secret` | `REPLACE_ME_openssl_rand_hex32` | config.py | JWT 签名密钥 |
| `jwt_algorithm` | `HS256` | config.py | JWT 算法 |
| `jwt_expire_hours` | `168` | config.py | JWT 有效期 |
| `sandbox_probe_commands` | （空） | config.py（护栏） | 沙箱探针命令 |
| `sandbox_disk_warn_mb` | `200` | config.py | 磁盘告警阈值 |
| `sandbox_disk_hard_mb` | `50` | config.py | 磁盘硬限 |
| `sandbox_probe_ttl_sec` | `60` | config.py | 探针 TTL |
| `sandbox_idle_timeout_sec` | `3600` | config.py | 沙箱空闲超时 |
| `sandbox_guardrails_enabled` | `true` | config.py | 沙箱护栏总开关 |
| `sandbox_completion_gate_enabled` | `true` | config.py | 完成门开关 |
| `sandbox_completion_gate_auto_continue` | `false` | config.py | 完成门自动续 |
| `sandbox_completion_gate_max_retries` | `2` | config.py | 完成门最大重试 |
| `sandbox_tool_failure_threshold` | `5` | config.py | 工具失败阈值 |
| `no_progress_repeat_threshold` | `5` | config.py | 无进展重复阈值 |
| `no_progress_max_injections` | `1` | config.py | 无进展最大注入 |
| `no_progress_window_files` | `3` | config.py | 无进展窗口文件数 |

### 2.4 日志 / agent 库 / 数据根

| 变量 | 默认 / 示例 | 消费方 | 说明 |
| --- | --- | --- | --- |
| `LOG_LEVEL` | `INFO` | compose → 容器 logging | 日志级别 |
| `LOG_FORMAT` | `json` | compose → 容器 logging | json 供 Loki 按字段索引 |
| `AGENT_PG_HOST` | `agent-postgres` | config.py + compose | agent 库主机（服务名） |
| `AGENT_PG_PORT` | `5432` | config.py | agent 库端口 |
| `AGENT_PG_USER` | `geesun` | config.py + compose | agent 库用户 |
| `AGENT_PG_DB` | `agent_mem` | config.py + compose | agent 库名 |
| `AGENT_PG_PASSWORD` | `REPLACE_ME_STRONG` | config.py + compose | agent 库密码 |
| `AGENT_DATA_ROOT` | `/opt/geesun/data` | compose 挂载源 | 宿主机数据根（agent/uploads/reports/skills） |

### 2.5 追踪 collector（Phoenix / Langfuse）

| 变量 | 默认 / 示例 | 消费方 | 说明 |
| --- | --- | --- | --- |
| `PHOENIX_COLLECTOR_ENDPOINT` | `http://alloy:4317` | src/core/tracing.py（经 Alloy） | agent OTLP 出口（appnet2 内） |
| `PHOENIX_OTLP_ENDPOINT` | `phoenix:4317` | alloy.config.alloy | Alloy → Phoenix 转发目标（服务名） |
| `LANGFUSE_BASE_URL` | `http://10.10.10.67:3000` | src/core/tracing.py | agent → Langfuse OTLP HTTP ingest |
| `OTEL_PROJECT_NAME` | `Geesun-Agent-prod` | config.py → tracing.py Resource | Phoenix 分组 project.name |
| `CORS_ALLOW_ORIGINS` | `http://10.10.10.67` | config.py（CORS） | 跨域源 |

### 2.6 镜像仓库 / tag

| 变量 | 默认 / 示例 | 消费方 | 说明 |
| --- | --- | --- | --- |
| `REGISTRY_GEESUN` | `172.16.220.74:8333/geesun_ai` | 各 compose image | 自有应用镜像仓库 |
| `REGISTRY_HUB` | `172.16.220.74:8333/dockerhub` | 各 compose image | 第三方镜像仓库 |
| `GEESUN_AGENT_TAG` | `1.0.0` | docker-compose.yml | agent 镜像 tag |
| `ALLOY_TAG` | `v1.19.2` | docker-compose.yml | Alloy 镜像 tag |
| `PROMETHEUS_TAG` | `3.0` | docker-compose.yml | Prometheus 镜像 tag |
| `WEB_TAG` | `1.0.0` | docker-compose.web.yml | 前端镜像 tag |
| `NEXT_PUBLIC_API_BASE` | `http://10.10.10.67/` | build-push.sh（build-time） | 前端 API 地址（内联进 JS，**运行期改无效**） |
| `LANGFUSE_TAG` | `3.224.3` | docker-compose.langfuse.yml | Langfuse 镜像 tag（pin 具体补丁版，禁浮动 `:3`） |
| `POSTGRES_VERSION` | `17` | docker-compose.langfuse.yml | Langfuse 后端 postgres 版本 |

### 2.7 Phoenix（随 `--with=phoenix` 并入）

| 变量 | 默认 / 示例 | 消费方 | 说明 |
| --- | --- | --- | --- |
| `PHOENIX_DB_USER` | `phoenix` | docker-compose.phoenix.yml | Phoenix 库用户 |
| `PHOENIX_DB_PASSWORD` | `REPLACE_ME_STRONG` | docker-compose.phoenix.yml | Phoenix 库密码 |
| `PHOENIX_DB_NAME` | `phoenix` | docker-compose.phoenix.yml | Phoenix 库名 |
| `PHOENIX_SQL_DATABASE_URL` | `postgresql://phoenix:...@phoenix-db:5432/phoenix` | docker-compose.phoenix.yml | Phoenix 连接串（由上面三项拼出） |

### 2.8 Langfuse（随 `--with=langfuse` 并入）

| 变量 | 默认 / 示例 | 消费方 | 说明 |
| --- | --- | --- | --- |
| `NEXTAUTH_URL` | `http://10.10.10.67:3000` | langfuse-web | Web 回调地址 |
| `NEXTAUTH_SECRET` | `REPLACE_ME_openssl_rand_hex32` | langfuse-web | 会话密钥（`openssl rand -hex 32`） |
| `SALT` | `REPLACE_ME_openssl_rand_hex16` | web/worker | 加解密盐 |
| `ENCRYPTION_KEY` | `REPLACE_ME_openssl_rand_hex32` | web/worker | 加解密密钥 |
| `TELEMETRY_ENABLED` | `false` | web/worker | 匿名遥测（生产关） |
| `LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES` | `false` | web/worker | 实验特性开关 |
| `DATABASE_URL` | `postgresql://postgres:...@postgres:5432/postgres` | web/worker | 主库连接串（密码须= `POSTGRES_PASSWORD`） |
| `POSTGRES_USER` | `postgres` | postgres | 后端库用户 |
| `POSTGRES_PASSWORD` | `REPLACE_ME_openssl_rand_hex12` | postgres + DATABASE_URL | 后端库密码（与 DATABASE_URL 一致） |
| `POSTGRES_DB` | `postgres` | postgres | 后端库名 |
| `CLICKHOUSE_MIGRATION_URL` | `clickhouse://clickhouse:9000` | worker | Clickhouse 迁移地址 |
| `CLICKHOUSE_URL` | `http://clickhouse:8123` | web/worker | Clickhouse HTTP |
| `CLICKHOUSE_USER` | `clickhouse` | web/worker + clickhouse | Clickhouse 用户 |
| `CLICKHOUSE_PASSWORD` | `REPLACE_ME_openssl_rand_hex12` | web/worker + clickhouse | Clickhouse 密码 |
| `CLICKHOUSE_CLUSTER_ENABLED` | `false` | web/worker | 集群开关 |
| `REDIS_HOST` | `redis` | web/worker | Redis 主机（服务名） |
| `REDIS_PORT` | `6379` | web/worker | Redis 端口 |
| `REDIS_AUTH` | `REPLACE_ME_openssl_rand_hex12` | web/worker + redis | Redis 密码（redis 用 `--requirepass`） |
| `REDIS_TLS_ENABLED` | `false` | web/worker | Redis TLS |
| `LANGFUSE_BULLMQ_SKIP_REDIS_VERSION_CHECK` | `false` | web/worker | 跳过 BullMQ Redis 版本校验 |
| `MINIO_ROOT_USER` | `minio` | minio | MinIO 根用户 |
| `MINIO_ROOT_PASSWORD` | `REPLACE_ME_openssl_rand_hex12` | minio | MinIO 根密码 |
| `LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY` | （=MINIO_ROOT_PASSWORD） | web/worker | 事件桶 S3 key（**须=MINIO_ROOT_PASSWORD**） |
| `LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY` | （=MINIO_ROOT_PASSWORD） | web/worker | 媒体桶 S3 key（**须=MINIO_ROOT_PASSWORD**） |
| `LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY` | （=MINIO_ROOT_PASSWORD） | web/worker | 批量导出桶 S3 key（**须=MINIO_ROOT_PASSWORD**） |
| `LANGFUSE_USE_AZURE_BLOB` | `false` | web/worker | 事件桶走 Azure Blob（自托管保持 false） |
| `LANGFUSE_S3_EVENT_UPLOAD_*` | bucket=`langfuse`, endpoint=`http://minio:9000`, prefix=`events/` | web/worker | 事件桶配置 |
| `LANGFUSE_S3_MEDIA_UPLOAD_*` | bucket=`langfuse`, endpoint=`http://minio:9000`, prefix=`media/`, `EXTERNAL_ENDPOINT=http://10.10.10.67:9092` | web/worker | 媒体桶配置（浏览器经主机 9092 取） |
| `LANGFUSE_S3_BATCH_EXPORT_*` | bucket=`langfuse`, endpoint=`http://minio:9000`, prefix=`exports/`, `ENABLED=false` | web/worker | 批量导出桶（默认关） |
| `LANGFUSE_INIT_ORG_ID` / `_ORG_NAME` | （空） | web/worker | 首启组织 ID/名（留空 UI 手建） |
| `LANGFUSE_INIT_PROJECT_ID` / `_NAME` | （空） | web/worker | 首启项目 ID/名（留空 UI 手建） |
| `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` | `pk-lf-init-CHANGE_ME` | web/worker | 首启项目 key（**须与下方 `LANGFUSE_PUBLIC_KEY` 完全一致**） |
| `LANGFUSE_INIT_PROJECT_SECRET_KEY` | `sk-lf-init-CHANGE_ME` | web/worker | 首启项目 secret（**须与下方 `LANGFUSE_SECRET_KEY` 完全一致**） |
| `LANGFUSE_INIT_USER_EMAIL` / `_NAME` / `_PASSWORD` | （空） | web/worker | 首启用户（留空 UI 手建） |
| `LANGFUSE_PUBLIC_KEY` | `pk-lf-init-CHANGE_ME` | src/core/tracing.py（agent 上报） | 项目 API key（自托管须=INIT key） |
| `LANGFUSE_SECRET_KEY` | `sk-lf-init-CHANGE_ME` | src/core/tracing.py | 项目 API secret（自托管须=INIT key） |

### 2.9 Grafana / 其他

| 变量 | 默认 / 示例 | 消费方 | 说明 |
| --- | --- | --- | --- |
| `GRAFANA_PASSWORD` | `REPLACE_ME` | docker-compose.yml（grafana） | Grafana admin 密码（发布到本机 `3100`） |

> **未列出项的取舍**：`.env.example` 内联注释已逐条说明每个变量的取值来源、是否必填、生成命令（如 `openssl rand -hex 32`）。新增变量时同步更新 `.env.example` 注释，勿只改 compose。

---

## 3. 已知部署坑与运行提示（防复发，供诊断参考）

- **"model 调用超过 600s（vLLM 无响应或过慢）" ≠ vLLM 宕机**：根因是 vLLM 曾带 `--enforce-eager`/`--tokenizer-mode slow`（禁用 CUDA graph）致 decode 仅 12-14 tok/s，长生成跑满客户端 600s 预算。已去 flag 根治（decode ~190 tok/s，约 15×）。遇此类超时先看 vLLM 引擎日志（`journalctl -u vllm-qwen3.5.service`）的 `Avg generation throughput` / `request_aborted` 再下结论，不要断言"模型挂了"。
- **会话"刷新后消息丢失/空白"**：根因是 Postgres store/saver 单连接并发碰撞（`another command is already in progress`）导致读取被吞/断连兜底写失败。已改连接池根治（store `pool_config`、saver 手建 `AsyncConnectionPool`）。再遇此现象先查 `server.log` 该错误与 Postgres `messages` 表数据是否在，勿直接判定数据丢失。
- **Phoenix 19.x 按标准 `project.name` 资源属性对 trace 分组**：缺该属性时所有 trace 落 `default` 项目；旧的 `openinference.project.name` 在 19.x 已被忽略（设了也不分组）。`src/core/tracing.py` 的 TracerProvider Resource **已双设** `project.name` + `openinference.project.name`，值取自 `config.py` 的 `OTEL_PROJECT_NAME`（prod `.env` 设 `Geesun-Agent-prod`）。若 Phoenix UI 只看到 `default` 而看不到业务项目，先查该环境变量与 tracing.py 是否两属性都在，不要去 Phoenix 管理台手建项目（项目会随首条 trace 自动出现）。
- **swarm 镜像部署：源码改动必须回构建机 rebuild+push 才生效**：geesun-agent / geesun-mcp / geesun-agent-web 均跑预构建镜像（Harbor `geesun_ai`，tag 由 `.env` 的 `GEESUN_AGENT_TAG` 等控制），生产机只 `start_stack.sh --no-build` 拉镜像刷新。改了 `src/` 后若仅在生产机改文件**不会生效**——必须在构建机跑 `deploy/build-push.sh`（或 `docker build -t 172.16.220.74:8333/geesun_ai/geesun-agent:<新tag>` 后 `docker push`），再上生产机改 tag + `--no-build --with=phoenix,langfuse,mcp,web` 重启。镜像 tag 只认正斜杠 `/`，反斜杠 `\` 会报 `invalid reference format`。
- **可观测栈统一随主栈自托管（2026-09-02 起）**：Phoenix（`--with=phoenix`）与 Langfuse（`--with=langfuse`）均已并入本 swarm stack，网络统一 `appnet2`，UI 分别发布 `6006` / `3000`、Minio S3 发布 `9092`，后端 postgres/redis/clickhouse 仅 appnet2 内不占主机端口。旧「复用现网共享实例」方案已废弃。**迁移前须先停用旧共享 Langfuse 栈**（占 3000/3030/5432/6379/8123/9000/9091/9092，无人自愈），否则与我方栈抢 3000/9092。Langfuse 自托管后 `LANGFUSE_PUBLIC_KEY/SECRET_KEY` 须对应**本实例**项目（用 `LANGFUSE_INIT_*` 环境变量首次启动时自动建项目并产出 key），不能填旧共享实例的 key。
- **附加 compose 网络名铁律**：所有 `docker-compose.*.yml` 附加文件（mcp / web / phoenix / langfuse）均引用主栈同名的 overlay 网络 `appnet2`，**不得**写 `appnet`（历史上 langfuse 文件曾写 `appnet` 导致并入后与主栈不通）。合并只看 `start_stack.sh --with=` 传哪些文件 + 对应 `*_BASE_URL` 端点。
- **Langfuse-web / langfuse-worker 内存 limits.memory 必须 ≥ 2g（2026-09-02 实测）**：1g 限额下 Next.js 16 在 prisma + clickhouse migration + `next-server` 启动阶段 V8 堆超 1g 触发 `SIGABRT`（exit 134）崩溃循环，swarm `restart_policy: any` 反复拉起永不停（容器内仅占 ~44% 内存、无 heap OOM 文本、宿主机有大量空闲，故非宿主机 OOM，而是 cgroup 限额截断）。**修复**：worker 与 web 的 `deploy.resources.limits.memory` 提到 `2g`（当前 compose 已设）。若再遇 langfuse-web 崩溃循环且日志无 OOM 字样，先调内存限额而非去看应用代码。
- **swarm 发布端口须让进程绑 0.0.0.0（Next.js 只绑 overlay IP 的特例，2026-09-02 实测）**：`langfuse-web:3000` 默认 `mode: host` 发布后，主机 `10.10.10.67:3000` / `127.0.0.1:3000` 仍 `Connection reset by peer`（000）；但容器内 `curl localhost:3000` 与同 overlay 内 agent 访问 `langfuse-web:3000` 均 200。根因：Next.js 16 进程只监听 overlay IP（`10.0.1.x:3000`），而 swarm `mode: host` 经 docker-proxy 把主机 3000 转发到容器 gwbridge IP（`172.21.0.15`），该地址无监听 → RST（`iptables DNAT dpt:3000 to:172.21.0.15` + `/proc/net/tcp` 仅 `10.0.1.x:0BB8` 实锤）。**修复**：langfuse-web 加环境变量 `HOSTNAME: "0.0.0.0"` 使其绑全网卡（容器 `/proc/net/tcp` 变 `00000000:0BB8`），主机端口即 200。注意：`minio:9092` 用默认 ingress 发布即正常（403 响应），无需此处理——仅 Next.js 这类「只绑首个网卡 IP」的进程需要。

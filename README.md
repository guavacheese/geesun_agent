##

## Installation

### Init project

```
uv init geesun_agent
```

### Install dependencies

```
uv add langchain langchain-openai openai deepagents

## install sandbox--choose one

uv add langchain-modal
uv add langchain-runloop

## sandbox
uv add langchain-daytona
uv add "langsmith[sandbox]"

## mcp
uv add langchain-mcp-adapters

## long-term memory
uv add langgraph-checkpoint-sqlite aiosqlite
uv add langgraph-checkpoint-postgres
uv add "psycopg[binary]" 
uv add grandalf
uv add fastapi "uvicorn[standard]" pydantic-settings

# LDAP
uv add ldap3 pyjwt

uv sync
```


## Install CubeSandbox locally
```
cd /mnt/d/workspace/geesun_agent
source .venv/bin/activate
uv pip install -e ../langchain-cubesandbox
```

## Install cubesandbox certificat
```
export SSL_CERT_FILE=/home/dhp/projects/cube-cert/cube-ca.pem

```

## Deployment

geesun_agent 生产部署目录位于 `deploy/`，采用 **Docker Swarm stack** 方式发布（与 `flyctrl_deploy` 同源思路，但补齐了 Swarm 必带参数与前置检查）。

对外仅暴露 **Caddy :80**（前端主入口 + `/api/*` 反向代理）；数据库 / Loki / Grafana 等只发布到本机回环或容器内网，不暴露到主机公网。

设计细节（网络/卷前缀、limits 生效、与 `docker compose up` 的差异、可观测栈路线 A/B 取舍）见 [`deploy/DEPLOYMENT.md`](./deploy/DEPLOYMENT.md)。

### 文件（deploy/）

| 文件 | 说明 |
| --- | --- |
| `docker-compose.yml` | **主栈**：`geesun-agent` + `agent-postgres`（pgvector）+ `caddy` + `loki` + `alloy` + `prometheus` + `grafana`；含 overlay 网络 `appnet` 与命名卷 |
| `docker-compose.mcp.yml` | 附加：`geesun-mcp`（MCP 服务，路线 A 默认并入） |
| `docker-compose.web.yml` | 附加：`geesun-agent-web`（Next.js 前端，并入后由 Caddy :80 同域服务） |
| `docker-compose.phoenix.yml` | 附加（路线 B 自托管兜底）：`phoenix` + `phoenix-db` |
| `docker-compose.langfuse.yml` | 附加（路线 B 自托管兜底）：`langfuse-web` / `langfuse-worker` + `clickhouse` / `minio` / `redis` / `postgres` |
| `start_stack.sh` | **部署入口**：前置检查（docker/swarm/`.env`）→ 调 `build-push.sh` 打包推送 → `docker stack deploy`（带 `--with-registry-auth --resolve-image=always --prune`） |
| `stop_stack.sh` | 停止并移除 stack（`docker stack rm geesun`，**保留命名卷**） |
| `service_stack.sh` | 查看 stack 服务状态（`docker stack services geesun`） |
| `build-push.sh` | 构建自有镜像（geesun-agent / geesun-mcp-server / geesun-agent-web）并同步第三方镜像进 Harbor |
| `.env.example` | **全部环境变量安全模板**（可入库）；复制为 `.env` 后填密钥 |
| `.env` | 真实连接配置（**不入库**，务必 `chmod 600`） |
| `Caddyfile` / `alloy.config.alloy` / `loki-config.yaml` / `prometheus.yml` / `grafana/` | 各服务运行时配置（bind mount 进容器） |
| `setup-cube-dns.sh` / `init-host.sh` | 主机侧辅助：`*.cube.app` DNS 解析、主机初始化 |
| `backup.sh` | 数据卷备份 |
| `certs/` | CubeSandbox egress MITM CA（供 agent / mcp 容器信任 sandbox 出网 TLS 拦截） |

> **多文件合并**：主栈默认只含 `docker-compose.yml`；MCP / 前端 / 可观测栈通过 `start_stack.sh --with=mcp,web` 以 `-c` 叠加，共享 overlay 网络 `appnet`（容器间用服务名互访，stack 前缀 `geesun_`）。

### 部署步骤

#### 0. 前置（目标机 / 构建机一次性）

- 目标机加入 Swarm（单节点）：`docker swarm init`
- Harbor（`172.16.220.74:8333`）为 HTTP，需加入 Docker「不安全仓库」：
  - Linux：`/etc/docker/daemon.json` 增加 `{"insecure-registries":["172.16.220.74:8333"]}` 后 `systemctl restart docker`
  - Docker Desktop：Settings → Docker Engine → 加入上述 JSON → Apply & Restart
- 目标机登录 Harbor（私有仓库拉取镜像必需）：`docker login 172.16.220.74:8333`
- 预建 agent 工作目录（防容器重建丢数据）：`mkdir -p /opt/geesun/data/{agent,uploads,reports}`（路径对应 `.env` 的 `AGENT_DATA_ROOT`）
- Harbor 提前建好两个项目：`geesun_ai`（自有应用）、`dockerhub`（第三方中央仓库）

#### 1. 构建并推送镜像（在有源码的构建机执行）

```bash
cd deploy
# 构建上下文需含 geesun_agent / geesun_mcp_server / geesun_agent_web / langchain-cubesandbox（同级目录）
HARBOR_USER=xxx HARBOR_PASSWORD=yyy bash build-push.sh
```

脚本会把 `geesun-agent` / `geesun-mcp-server` / `geesun-agent-web` 推送至 `geesun_ai`，并把 pgvector / caddy / loki / alloy / prometheus / grafana / phoenix / langfuse / clickhouse / minio / redis / postgres 等同步进 `dockerhub`。

> 生产内网机若无法直连公网拉取第三方镜像，请在能出网的机器跑本脚本后，再到内网机 `docker pull`。

#### 2. 填写环境变量（目标机）

```bash
cd deploy
cp .env.example .env && vi .env   # 填全部密钥（REGISTRY_GEESUN / REGISTRY_HUB / Harbor 凭证 / 各 *PASSWORD / GRAFANA_PASSWORD 等）
chmod 600 .env
```

#### 3. 发布到 Swarm

```bash
cd deploy
./start_stack.sh                          # 默认仅主栈（先 build-push 再发布）
./start_stack.sh --no-build               # 跳过打包，仅用已推送镜像重新部署（改配置时只重启）
./start_stack.sh --with=mcp,web           # 并入 MCP / 前端
./start_stack.sh --with=phoenix,langfuse  # 路线 B：自托管可观测栈兜底
STACK_NAME=geesun ./start_stack.sh         # 显式指定 stack 名（默认 geesun）
```

- `--with-registry-auth`：依赖本机已 `docker login` Harbor，否则私有镜像拉取失败
- `--resolve-image=always`：固定 tag 重新发布时强制拉新镜像
- `--prune`：compose 中删掉的服务会被真正清理

#### 4. 校验

```bash
cd deploy
./service_stack.sh                                  # 列出全部服务（副本数 / 镜像 / 端口）
docker service logs -f geesun_geesun-agent          # 看某服务日志
docker service logs -f geesun_geesun-mcp            # MCP 服务日志
curl -f http://127.0.0.1:8009/docs                  # agent 健康检查
```

前端访问 `http://<服务器IP>/`（Caddy :80 同域；`/api/*` 由 Caddy 转发后端）。

#### 5. 停止 / 升级

```bash
cd deploy
./stop_stack.sh             # 移除 stack，命名卷（geesun_*）保留，数据不丢
# 改完 .env 或镜像后重新：./start_stack.sh --no-build --with=mcp,web
```

> 彻底清理卷（⚠️ 不可恢复）：`docker volume ls | grep geesun_` 后手动 `docker volume rm`。

### 环境变量说明（deploy/.env）

复制自 `.env.example`，所有 `REPLACE_ME*` 项务必用 `openssl rand -hex 32`（密钥/盐）或 `openssl rand -hex 12`（密码）生成强随机值。

#### 应用基础（geesun-agent）

| 变量 | 说明 |
| --- | --- |
| `base_url` | 远端 vLLM OpenAI 兼容地址，如 `http://172.16.66.13:8003/v1` |
| `openai_api_key` | vLLM API Key |
| `model_name` | 模型名，如 `Qwen3.6-35B-A3B` |
| `agent_workspace` | 容器内 agent 工作目录（默认 `/data/agent`） |
| `upload_root` | 容器内上传根（默认 `/data/uploads`） |
| `report_root` | 容器内报告根（默认 `/data/reports`） |
| `mcp_token` | MCP 调用鉴权 token |

#### MCP（geesun-mcp）

| 变量 | 说明 |
| --- | --- |
| `mcp_server_url` | agent 经服务名访问 MCP 的地址，默认 `http://geesun-mcp:8000/mcp`（覆盖 `config.py` 的 localhost 默认值） |
| `MCP_TAG` | MCP 镜像 tag（Harbor `geesun_ai` 项目） |
| `DECRYPT_API_URL` | DLP 解密网关地址（compose 外，MCP 内部 httpx POST 加密文件） |
| `E2B_API_URL` | CubeSandbox(E2B) 代理地址（实测与 vLLM 同主机同端口，按路径区分服务，非笔误） |
| `E2B_API_KEY` | CubeSandbox API Key |
| `SSL_CERT_FILE` | 容器内信任的 CubeSandbox egress CA 路径（无 MITM 可删） |
| `CA_MOUNT_SRC` | CA 证书在宿主机的路径（相对 `deploy/`，bind mount 进容器） |

#### 日志 / 数据库 / 数据根

| 变量 | 说明 |
| --- | --- |
| `LOG_LEVEL` | 日志级别（默认 `INFO`） |
| `LOG_FORMAT` | `json`（供 Loki 按字段索引）/ `text`（本地看） |
| `AGENT_PG_HOST` / `AGENT_PG_PORT` / `AGENT_PG_USER` / `AGENT_PG_DB` / `AGENT_PG_PASSWORD` | agent 专属 Postgres（服务名 `agent-postgres`），`agent_mem` 库 |
| `AGENT_DATA_ROOT` | 宿主机挂载根（默认 `/opt/geesun/data`），防容器重建丢数据 |

#### 追踪 collector（可观测栈）

| 变量 | 说明 |
| --- | --- |
| `PHOENIX_COLLECTOR_ENDPOINT` | agent OTLP 发往 Alloy，默认 `http://alloy:4317` |
| `PHOENIX_OTLP_ENDPOINT` | Alloy → Phoenix traces 转发目标（路线 A=LAN IP `10.10.10.67:4317`；路线 B=`phoenix:4317`） |
| `LANGFUSE_BASE_URL` | Langfuse 地址（路线 A=LAN IP；路线 B=`http://langfuse:3000`） |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 你的 Langfuse 项目 API Keys（回填后重启 agent 生效） |
| `OTEL_PROJECT_NAME` | OpenTelemetry 项目名（默认 `Geesun-Agent-prod`） |
| `CORS_ALLOW_ORIGINS` | 前端跨域源（Caddy 同域一般无跨域；按实际域名/IP 调整） |

#### 镜像仓库（Harbor，HTTP）

| 变量 | 说明 |
| --- | --- |
| `REGISTRY_GEESUN` | 自有应用仓库，如 `172.16.220.74:8333/geesun_ai` |
| `REGISTRY_HUB` | 第三方中央仓库，如 `172.16.220.74:8333/dockerhub` |
| `GEESUN_AGENT_TAG` | geesun-agent 镜像 tag（默认 `1.0.0`） |
| `ALLOY_TAG` | Alloy 镜像 tag（默认 `v1.19.2`） |
| `PROMETHEUS_TAG` | Prometheus 镜像 tag（默认 `3.0`，需 2.48+ 支持 remote-write-receiver） |
| `WEB_TAG` | 前端镜像 tag（默认 `1.0.0`；`NEXT_PUBLIC_API_BASE` 为 build-time 注入，运行时改无效） |
| `LANGFUSE_TAG` | Langfuse 镜像 tag（pin `3.224.3`；4.0.0 暂无可拉取镜像） |
| `POSTGRES_VERSION` | pgvector 基础 Postgres 大版本（默认 `17`） |

#### Phoenix（路线 B 自托管时填写）

| 变量 | 说明 |
| --- | --- |
| `PHOENIX_DB_USER` / `PHOENIX_DB_PASSWORD` / `PHOENIX_DB_NAME` | Phoenix 库凭据 |
| `PHOENIX_SQL_DATABASE_URL` | 由上面三项拼出（phoenix 与 phoenix-db 共用） |

#### Langfuse（路线 B 自托管时填写；prod 复用现网共享实例可忽略）

| 变量 | 说明 |
| --- | --- |
| `NEXTAUTH_URL` / `NEXTAUTH_SECRET` | Web 回调地址与会话密钥（`NEXTAUTH_SECRET` 须 `openssl rand -hex 32`） |
| `SALT` / `ENCRYPTION_KEY` | 加解密盐/密钥（各 `openssl rand -hex 16` / `hex 32`） |
| `DATABASE_URL` | 主库连接串（密码须与 `POSTGRES_PASSWORD` 一致） |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Langfuse 后端 postgres 自身凭据 |
| `CLICKHOUSE_*` | Clickhouse 连接（url / user / password / cluster） |
| `REDIS_*` | Redis 连接（host / port / auth） |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | MinIO 根凭据；**官方要求三个 `LANGFUSE_S3_*_SECRET_ACCESS_KEY` 必须等于它** |
| `LANGFUSE_S3_*` | 事件/媒体/批量导出三个桶的 S3 配置（endpoint 默认 `http://minio:9000`） |
| `LANGFUSE_INIT_*` | 首次启动初始化组织/项目/用户（留空则 UI 手动创建） |

#### Grafana

| 变量 | 说明 |
| --- | --- |
| `GRAFANA_PASSWORD` | Grafana admin 密码（发布到本机 `127.0.0.1:3100`） |

> 完整变量清单与未列出项的取舍见 `.env.example` 内联注释与 [`deploy/DEPLOYMENT.md`](./deploy/DEPLOYMENT.md)。

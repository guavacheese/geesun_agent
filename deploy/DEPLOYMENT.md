# geesun_agent 生产部署规划

> 范围：geesun_agent 后端 + 依赖（Postgres/pgvector）+ 可观测性（Phoenix / Langfuse 并行）+ 日志集中（Loki / Promtail / Grafana）+ 反向代理（Caddy）。
> 目标形态：单宿主 `docker compose` 硬化部署，全部镜像托管在内部 Harbor，物理机仅暴露 Caddy 入口。
> 本文档为规划稿，落地项见文末「待办清单」。

---

## 0. 架构总览

| 组件 | 镜像（Harbor） | 容器端口 | 对外 | 数据持久化 | 关键依赖 |
|---|---|---|---|---|---|
| geesun-agent | `geesun_ai/geesun-agent:<tag>` | 8009 | 经 Caddy | `/data/agent` `/data/uploads` `/data/reports`（host 绑定） | vLLM :8003、CubeSandbox、geesun_mcp_server :8000、Phoenix/Langfuse |
| agent-postgres | `geesun_ai/pgvector:0.8.0-pg17` | 5432 | 否 | named volume `agent_pg_data` | — |
| caddy | `geesun_ai/caddy:2.8-alpine` | 80 / 6006 / 3000 | 是 | `caddy_data` `caddy_config` | geesun-agent / phoenix / langfuse-web |
| loki | `geesun_ai/loki:3.2.0` | 3100 | 否（Grafana 接） | `loki_data` | — |
| promtail | `geesun_ai/promtail:3.2.0` | — | 否 | — | docker.sock |
| grafana | `geesun_ai/grafana:11.3.0` | 3000 | 127.0.0.1:3100 | `grafana_data` | loki |
| phoenix | `geesun_ai/phoenix:19.1.0` | 6006 / 4317 | 经 Caddy | — | phoenix-db |
| phoenix-db | `geesun_ai/postgres:16.14` | 5432 | 否 | `phoenix_pg_data` | — |
| langfuse-web / worker | `geesun_ai/langfuse:3.224.3` | 3000 | 经 Caddy | — | langfuse 后端栈 |
| langfuse 后端 | `postgres:17` / `clickhouse-server:25.12` / `redis:7` / `minio:chainguard` | 各自 | 否（minio S3 9092） | 多个 named volume | — |

**外部物理机（不在 compose 内，由 geesun-agent 跨网络访问）：**
- **vLLM**：`172.16.66.13:8003`（MoE 35B，`base_url=http://172.16.66.13:8003/v1`）
- **CubeSandbox**：`172.16.66.13`（cube-proxy / cube-egress MITM，sandbox 执行环境）
- **geesun_mcp_server**：`:8000`（独立进程，提供 MCP 工具）
- **Harbor**：`172.16.220.74:8333`（HTTP，项目 `geesun_ai`）

---

## 1. 镜像打包（每个工程）

### 1.1 geesun-agent（自研，需构建）
- **Dockerfile** 位于仓库根，基于 `ghcr.io/astral-sh/uv:python3.13-bookworm`，非 root 运行。
- **构建上下文必须为 `/d/workspace`**（与 `langchain-cubesandbox` 同级），否则 `pyproject.toml` 中的 editable 源 `../langchain-cubesandbox` 在镜像内无法解析为 `/langchain-cubesandbox`。
- 构建命令（由 `deploy/build-push.sh` 封装，不要手写）：
  ```sh
  cd /d/workspace
  docker build -f geesun_agent/Dockerfile -t 172.16.220.74:8333/geesun_ai/geesun-agent:1.0.0 .
  ```

### 1.2 第三方组件（无需构建，直接取公网镜像）
Phoenix / Langfuse / Postgres / pgvector / Caddy / Loki / Promtail / Grafana / Clickhouse / Redis / Minio 均为上游官方镜像，`build-push.sh` 的 `sync()` 会 `pull → tag → push` 进 Harbor，**不需要 Dockerfile**。

---

## 2. 镜像上传（Harbor）

### 2.1 Harbor 接入前提（一次性）
Harbor 为 **HTTP**（`172.16.220.74:8333`），Docker 默认走 HTTPS，必须把该地址加入**不安全仓库**：

- **Linux（生产机）**：编辑 `/etc/docker/daemon.json`
  ```json
  { "insecure-registries": ["172.16.220.74:8333"] }
  ```
  然后 `systemctl restart docker`。
- **Docker Desktop**：Settings → Docker Engine → 加入上述 JSON → Apply & Restart。

未配置会报 `http: server gave HTTP response to HTTPS client`。

### 2.2 上传流程
在**能访问公网**的构建机上执行（内网生产机通常无出网，只从 Harbor 拉）：
```sh
HARBOR_USER=<你的Harbor账号> HARBOR_PASSWORD=<密码> \
  bash deploy/build-push.sh 1.0.0
```
脚本动作：
1. `docker login 172.16.220.74:8333`
2. 构建并 push `geesun-agent:<tag>`
3. `sync()` 拉取并将 11 个第三方镜像推入 `geesun_ai` 项目（公网拉不到时回退用本地已有镜像）

### 2.3 镜像清单（源 → Harbor 目标）

| 源镜像 | Harbor 目标 |
|---|---|
| `pgvector/pgvector:0.8.0-pg17` | `geesun_ai/pgvector:0.8.0-pg17` |
| `caddy:2.8-alpine` | `geesun_ai/caddy:2.8-alpine` |
| `grafana/loki:3.2.0` | `geesun_ai/loki:3.2.0` |
| `grafana/promtail:3.2.0` | `geesun_ai/promtail:3.2.0` |
| `grafana/grafana:11.3.0` | `geesun_ai/grafana:11.3.0` |
| `arizephoenix/phoenix:19.1.0` | `geesun_ai/phoenix:19.1.0` |
| `postgres:16.14` | `geesun_ai/postgres:16.14` |
| `langfuse/langfuse:3.224.3` | `geesun_ai/langfuse:3.224.3` |
| `clickhouse/clickhouse-server:25.12` | `geesun_ai/clickhouse-server:25.12` |
| `cgr.dev/chainguard/minio` | `geesun_ai/minio:chainguard` |
| `redis:7` | `geesun_ai/redis:7` |
| `postgres:17` | `geesun_ai/postgres:17` |

---

## 3. 镜像版本管理

### 3.1 命名规范（镜像不区分环境，配置区分环境）
**核心原则：构建一次、跨环境晋升同一镜像。** 环境差异（dev 的 `LOG_LEVEL=DEBUG`、prod 的 `otel_project_name=Geesun-Agent-prod`）全部由 `.env` 注入，镜像保持环境无关——绝不把 dev / prod 烤进 tag，否则 prod 跑的镜像你其实没在 dev 验证过。

- **geesun-agent（生产）**：不可变 `<语义版本>-<gitsha>`（如 `1.0.0-a1b2c3d`），`GEESUN_AGENT_TAG` 在 `.env` 指定；**禁用 `latest`**（不可复现）。
- **geesun-agent（开发）**：可用浮动 `dev-<branch>` 便于本地丢，但永不进生产。
- **环境隔离（可选）**：若需物理隔离，Harbor 开 `geesun_ai-dev` / `geesun_ai` 两个项目；当前统一在 `geesun_ai`，用 tag 后缀 + `.env` 区分即可。
- **第三方**：已固定具体版本（loki 3.2.0、grafana 11.3.0、phoenix 19.1.0、pgvector 0.8.0-pg17、langfuse 3.224.3 等）。
- **Langfuse 版本说明**：已 pin 到 `3.224.3`（当前可 `docker pull` 的最新稳定版）。`.env` 的 `LANGFUSE_TAG` 控制，`build-push.sh` 已同步 `langfuse:3.224.3`。
  > 注：本地 `langfuse` 源码仓库为 **v4.0.0**（你在开发的分支），但官方自托管**可拉取镜像**稳定线仍是 `:3`，`3.224.3` 是该线最新具体版本，与源码 `4.0.0` 并非同一号。若未来要从源码构建 `4.0.0` 镜像，需单独 `docker build` 并改 `LANGFUSE_TAG`，届时同步 `build-push.sh`。

### 3.2 Harbor 保留策略
在 Harbor 项目 `geesun_ai` 配置 **Tag Retention**：
- `geesun-agent`：保留最近 10 个版本 + 带 `latest`/`release-*` 标签的永久保留。
- 第三方：保留最近 3 个版本，避免无限增长。

### 3.3 升级流程
1. 改代码 → 本地验证 → 打新 tag（如 `1.1.0`）。
2. `bash deploy/build-push.sh 1.1.0` 推送。
3. 生产机 `export GEESUN_AGENT_TAG=1.1.0` 或改 `.env` → `docker compose pull geesun-agent && docker compose up -d geesun-agent`。
4. 回滚：把 tag 指回旧版本重跑上一步。

---

## 4. 网络规划

### 4.1 Docker 内部网络
- 所有服务加入自定义 bridge 网络 **`appnet`**，容器间用**服务名**互访（如 agent 配 `phoenix_collector_endpoint=http://phoenix:4317`、`langfuse_base_url=http://langfuse-web:3000`）。
- 三个 compose 用 `-f` 合并时，各自的 `networks: [appnet]` 会加入同一网络（名称固定为 `appnet`）。

### 4.2 对外暴露（最小化）
- 只暴露 **Caddy**：`80`（主入口）/ `6006`（Phoenix）/ `3000`（Langfuse）。
- 数据库 / Redis / Clickhouse / Minio(除 S3 9092) **不映射主机端口**，仅经 `appnet` 内部访问；如需排障临时暴露，绑 `127.0.0.1`（已配置）。
- Minio S3 API 映射主机 `9092`（避开 Phoenix 的 9090 冲突）；其 `EXTERNAL_ENDPOINT` 必须指向**主机 LAN IP**（`http://10.10.10.67:9092`），容器视角不能用 `localhost`。

### 4.3 访问外部物理机（vLLM / CubeSandbox / MCP）
- 容器通过宿主机的 NAT（MASQUERADE）访问外部 IP `172.16.66.13`，因此 agent 内配置 `base_url=http://172.16.66.13:8003/v1` 可直接连通（前提是宿主机能路由到该网段）。
- 若用 Docker Desktop 或想解耦硬编码 IP，可加 `extra_hosts: ["host.docker.internal:host-gateway"]` 并用 `host.docker.internal` 指代宿主机；但指向**另一台物理机**时直接用其 LAN IP 更稳。
- **CubeSandbox egress MITM**：CubeSandbox 走 `cube-egress` 做 TLS 拦截，提供 `rootCA.pem`。agent 容器若需信任该 CA，需：
  - 挂载 CA 到容器（如 `/etc/ssl/certs/cube-root-ca.crt`）；
  - 设 `REQUESTS_CA_BUNDLE=/etc/ssl/certs/cube-root-ca.crt`（或在 compose `environment` 注入）；
  - 否则 sandbox 内出网请求会因证书不受信失败。

### 4.4 TLS
- Caddy 做 TLS 终止，使用内部 CA（mkcert 链）签发 `10.10.10.67` 证书；内网服务之间明文。证书存于 `caddy_data` volume。

### 4.5 端口冲突速查
| 端口 | 用途 | 处理 |
|---|---|---|
| 9090 | Phoenix prometheus / Langfuse 默认 | Phoenix 的 9090 **不映射**；Langfuse Minio 改 9092 |
| 5432 | postgres ×3（agent/phoenix/langfuse） | 各自独立容器，仅内部；不冲突 |
| 3000 | Langfuse / Grafana 内部 | 对外只给 Langfuse（Grafana 走 127.0.0.1:3100） |

---

## 5. 数据与挂载（防丢）

### 5.1 有状态服务 → named volume（推荐）
当前 compose 已为所有有状态组件使用 named volume，生命周期独立于容器，重建/升级容器数据不丢：

`agent_pg_data` `phoenix_pg_data` `langfuse_postgres_data` `langfuse_clickhouse_data` `langfuse_clickhouse_logs` `langfuse_minio_data` `langfuse_redis_data` `loki_data` `grafana_data` `caddy_data` `caddy_config`

> named volume 物理存放于宿主 `/var/lib/docker/volumes/`。**named volume 不等于「免备份」**——磁盘损坏/误删仍会丢。见 5.3 备份。

### 5.2 geesun-agent 的工作目录已挂宿主机
`docker-compose.yml` 的 `geesun-agent` 已通过 `${AGENT_DATA_ROOT}` 挂载 `agent_workspace` / `upload_root` / `report_root`（`.env` 默认 `/opt/geesun/data`，容器内 `/data/{agent,uploads,reports}`）。容器重建后已生成的报告/上传文件不丢：
```yaml
    volumes:
      - ${AGENT_DATA_ROOT}/agent:/data/agent
      - ${AGENT_DATA_ROOT}/uploads:/data/uploads
      - ${AGENT_DATA_ROOT}/reports:/data/reports
```
目标机需预先 `mkdir -p /opt/geesun/data/{agent,uploads,reports}`（建议并入 `init-host.sh`，见 §9 #4）。

### 5.3 备份策略（真·防丢）
已由 `deploy/backup.sh` 落地（待办 #5 ✅），每日 cron 执行：
- **Postgres（agent_mem / phoenix / langfuse）**：`pg_dump` 三个库 → `/opt/geesun/backups/<时间戳>/*.sql.gz`，默认保留 7 天（`RETENTION_DAYS` 可调）。
- **Minio（Langfuse 对象）**：用 `minio:chainguard` 镜像内 `mc` 把 `langfuse` 桶 `mirror` 到备份目录。
- **Clickhouse（Langfuse）**：未自动备份（trace 数据可重建，接受「重建后重新摄入」）；如需可后续加 `clickhouse-backup`。
- **Loki**：日志可重建，优先保 retention，不必备份。
- **agent 报告/上传**：随 5.2 的 host 绑定目录，纳入宿主机的文件级备份（rsync / 存储快照）。

cron 示例（每天 03:07）：
```sh
7 3 * * *  cd /opt/geesun/geesun_agent/deploy && BACKUP_ROOT=/opt/geesun/backups bash backup.sh >> /opt/geesun/backups/cron.log 2>&1
```

### 5.4 危险操作红线
- **禁止** `docker compose down -v`（`-v` 删 named volume，数据全毁）。
- 升级前先按 5.3 做逻辑备份。
- 迁移主机：named volume 用 `docker volume inspect` 定位实际目录，`rsync` 到新机对应路径，而非重新建空卷。

---

## 6. 应用日志管理

### 6.1 原则
- **应用只打 stdout/stderr（JSON）**，不写容器内文件（已删除 `start.sh` 的 `tee server.log` 反模式）。
- **大小滚动** = Docker `json-file` 驱动的 `max-size` / `max-file`（各服务已配：agent 50m×5，其余 20m×3）。
- **日期留存 + 检索 + 告警** = Loki `retention_period: 720h`（30 天）+ Grafana。这正是 log4j 日期滚动的平台等价物，且带查询能力。

### 6.2 采集链路
`geesun-agent(stdout JSON)` → Docker `json-file`(本地轮转) → Promtail(`docker_sd` 发现 appnet 容器、提取 JSON 字段) → Loki(30d 留存) → Grafana(检索/面板/告警)。

### 6.3 配置要点
- `LOG_LEVEL`（默认 `INFO`）、`LOG_FORMAT=json|text` 由环境变量控制（`logging.py` 已实现）。
- `loki-config.yaml` 已设 `retention_period: 720h` + `compactor` 启用删除；按需调整。

### 6.4 uvicorn.access 已统一为 JSON
业务日志已是 JSON；uvicorn 的 access 日志通过 Dockerfile CMD 注入 `--log-config logging.uvicorn.json`，把 `uvicorn.access` 也指向同一个 JSON formatter，HTTP 访问日志现在同为结构化 JSON，可被 Loki 按 `level` / `logger` / `msg` 字段索引。

### 6.5 与「链路追踪」区分
- **应用日志**（stdout JSON，Loki 管）：error / request / business 流水。
- **链路追踪**（Phoenix / Langfuse 管）：LLM 调用的 span / token / 延迟，存各自数据库，**不管业务日志**。
- 二者是独立管道，排查时互补：日志看「发生了什么」，trace 看「LLM 链路长什么样」。

---

## 7. 一步一步部署流程

**阶段 A — 构建机（能出网）**
1. 安装 Docker + compose plugin。
2. 配置 Harbor 不安全仓库（见 2.1）。
3. `HARBOR_USER/PASSWORD bash deploy/build-push.sh <tag>` 把全量镜像推入 `geesun_ai`。

**阶段 B — 生产机（仅内网，从 Harbor 拉）**
1. 安装 Docker + compose plugin，配置 Harbor 不安全仓库（2.1）。
2. `bash deploy/init-host.sh`（自动建 `/opt/geesun/data/{agent,uploads,reports}` 与备份目录，并把属主改成容器 UID 1001，避免 Docker 自动以 root 建目录导致容器无写权限；见 §9 #4 ✅）。
3. 拷贝 `deploy/` 目录到生产机（或 git 拉取）。
4. `cd deploy && cp .env.example .env && vi .env`：填全部 `CHANGEME` 密钥（`openssl rand` 生成）、确认 `REGISTRY` / `GEESUN_AGENT_TAG` / 外部 IP（vLLM、CubeSandbox、Harbor、LAN IP）。
5. （可选）挂载 CubeSandbox `rootCA.pem` 到 agent 并设 `REQUESTS_CA_BUNDLE`（4.3）。
6. 合并拉取镜像：
   ```sh
   docker compose -f docker-compose.yml -f docker-compose.phoenix.yml -f docker-compose.langfuse.yml pull
   ```
7. 启动（依赖与 healthcheck 会控制顺序）：
   ```sh
   docker compose -f docker-compose.yml -f docker-compose.phoenix.yml -f docker-compose.langfuse.yml up -d
   ```
8. 校验：
   - `docker compose ps` 全 healthy；
   - `http://10.10.10.67/` → agent 文档页（Caddy）；
   - `http://10.10.10.67:6006` → Phoenix；
   - `http://10.10.10.67:3000` → Langfuse，注册首个账号；
   - `127.0.0.1:3100` → Grafana，看 Loki 数据源有日志。

**阶段 C — 接线**
9. Langfuse 内 `Settings → API Keys` 拿 public/secret key，回填 `.env` 的 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 并 `up -d geesun-agent` 重启 agent 生效。
10. 验证 agent 一次真实对话：Phoenix 出 span、Langfuse 出 trace、Grafana 出 JSON 日志。

---

## 8. 注意事项 / 已知坑

- **Harbor HTTP**：务必配 `insecure-registries`，否则 login/pull 失败。
- **构建上下文**：`geesun-agent` 必须在 `/d/workspace` 下构建，否则 `langchain-cubesandbox` 解析失败。
- **9090 端口冲突**：Phoenix 的 9090 不映射；Langfuse Minio 改 9092 + EXTERNAL 指向 LAN IP。
- **三个 Postgres**：agent_mem / phoenix / langfuse 各自独立，资源占用偏高；后续若想省可合并实例（按安全域权衡）。
- **密钥**：`.env` 必须 `chmod 600` 且**绝不进 git**；生产可进一步改用 compose `secrets:` 挂载。
- **DB 不暴露**：仅 `127.0.0.1` 供排障；对外只走 Caddy。
- **资源限制**：各服务已加 `deploy.resources.limits`，防止 vLLM/agent 互相挤占（compose 非 swarm 下 `limits` 仅提示，真正限需用 `--compatibility` 或 cgroup v2 配置，按需确认）。
- **Langfuse 初始化**：官方 clickhouse migration 步骤务必在 `up` 前核对；首次启动 worker 会跑迁移，需等 healthy。
- **不要 `-v` 删卷**：见 5.4。
- **外部依赖可达性**：部署后先 `docker exec geesun-agent curl -s http://172.16.66.13:8003/v1/models` 验证 vLLM 连通，再验 CubeSandbox / MCP。

---

## 9. 待办清单（与本文档配套的实现项）

| # | 事项 | 文件 | 状态 |
|---|---|---|---|
| 1 | geesun-agent 挂载 `/data/{agent,uploads,reports}` 到宿主机 | `docker-compose.yml`（`${AGENT_DATA_ROOT}` 驱动） | ✅ 已落地 |
| 2 | Langfuse 镜像 pin 到具体补丁版本（3.224.3） | `docker-compose.langfuse.yml`（`${LANGFUSE_TAG}`）+ `build-push.sh` + `.env.example` | ✅ 已落地 |
| 3 | uvicorn.access 注入 JSON formatter（`--log-config`） | `logging.uvicorn.json` + Dockerfile CMD | ✅ 已落地 |
| 4 | 生产机 `/opt/geesun/data` 目录预创建脚本 | 独立 `init-host.sh`（含属主修正） | ✅ 已落地 |
| 5 | 每日备份脚本（pg_dump / minio mirror） | 新增 `backup.sh` + cron 示例 | ✅ 已落地 |
| 6 | Harbor `geesun_ai` 项目 Tag Retention 规则 | Harbor 控制台人工配置 | ⬜ 待配置（步骤见 §11） |
| 7 | 三项目配置全收口到 `.env`（Phoenix 入 `.env` + compose 去硬编码；Langfuse 补齐所有变量且去除不安全默认值，改为 `${VAR}` 强契约） | `.env.example` + `docker-compose.phoenix.yml` + `docker-compose.langfuse.yml` | ✅ 已落地 |

> 实现项 #1–#5、#7 已落地并提交；仅 #6（Harbor Retention）需在控制台人工配置，步骤见 §11。

---

## 10. 环境策略：dev / prod 区分（路线 A，已确认）

**决策：路线 A** —— 单宿主机 `10.10.10.67`，只跑**一套** Phoenix + 一套 Langfuse，dev/prod 用「项目名 / 项目 key」区分；prod 的 geesun_agent 也部署在同一台。理由：Phoenix/Langfuse 是「可观测性」不是「业务数据」，共享后端可接受；业务数据（agent_mem）与 agent 应用本身已是独立容器/卷，隔离到位。

### 10.1 Phoenix：靠 `openinference.project.name` 分项目
- 已做成可配项（`config.py` 的 `otel_project_name`，`.env` 的 `OTEL_PROJECT_NAME`）。
- dev agent → `OTEL_PROJECT_NAME=Geesun-Agent-dev`；prod agent → `OTEL_PROJECT_NAME=Geesun-Agent-prod`。
- 同一个 Phoenix 实例，UI 里是两个独立 Project，可分别筛选。
- 注：后端 PG 仍共享（dev/prod span 同库，仅 project 标签不同）。

### 10.2 Langfuse：靠 Organization → Project + 独立 key 分项目
- 同一个 Langfuse 实例内建两个 Project：`dev` 与 `prod`，各自有独立 `public_key` / `secret_key`。
- dev agent `.env` 填 dev project 的 key；prod agent `.env` 填 prod project 的 key；二者的 `LANGFUSE_BASE_URL` 指向同一实例。
- 注：后端 PG/Clickhouse/Minio 共享，逻辑隔离靠 key。

### 10.3 端口冲突（路线 A 的前置动作，必须处理）
- **现状**：dev 直接在 10.10.10.67 上占用 `:6006`（Phoenix）与 `:3000`（Langfuse）。
- **prod compose 里 caddy 也要绑 `80 / 6006 / 3000`**（见 §4.2）。若 dev 仍在跑，`up` 会因端口占用失败。
- **处理（二选一，推荐 a）**：
  - **a. 下线 dev 的 Phoenix/Langfuse 占用**：停止 dev 的 `docker run` 进程，释放 6006/3000，让 prod 的 caddy 接管。dev 仍可经同一 Phoenix/Langfuse 实例的 dev/prod 项目继续用。
  - **b. dev 挪端口 / 挪机器**：保留 dev 实例但改绑其他端口（如 dev Phoenix 16006、dev Langfuse 13000），prod 维持 6006/3000。

### 10.4 agent 环境变量按环境切换
prod 的 `geesun_agent/deploy/.env` 至少区分：
```sh
OTEL_PROJECT_NAME=Geesun-Agent-prod
LANGFUSE_BASE_URL=http://10.10.10.67:3000
LANGFUSE_PUBLIC_KEY=pk-lf-prod-xxxx
LANGFUSE_SECRET_KEY=sk-lf-prod-xxxx
PHOENIX_COLLECTOR_ENDPOINT=http://phoenix:4317
```

---

## 11. Harbor Tag Retention 手动配置（控制台，步骤 #6）

Harbor 是 HTTP（`172.16.220.74:8333`），用浏览器访问并登录后，给 `geesun_ai` 项目设保留策略（脚本无法替你点，故手动）：

1. 打开 `http://172.16.220.74:8333` → 登录（Harbor 管理员或 `geesun_ai` 项目 Maintainter 账号）。
2. 左侧 **Projects** → 点 `geesun_ai` → 顶部 **Configuration** 标签 → **Tag Retention** 子页（旧版在 **Policies → Tag Retention**）。
3. 点 **Add Rule**（或 **NEW RULE**），按两条分别建：
   - **规则 A（自研镜像）**：
     - Matched repositories：`geesun_ai/geesun-agent`
     - Retain：keep most recently pushed **10** tags
     - 额外：勾选 "with labels" 并填 `latest,release-*`（这些标签永久保留，不被清理）
   - **规则 B（第三方镜像）**：
     - Matched repositories：`geesun_ai/pgvector`, `geesun_ai/caddy`, `geesun_ai/loki`, `geesun_ai/promtail`, `geesun_ai/grafana`, `geesun_ai/phoenix`, `geesun_ai/postgres`, `geesun_ai/langfuse`, `geesun_ai/clickhouse-server`, `geesun_ai/minio`, `geesun_ai/redis`
     - Retain：keep most recently pushed **3** tags
4. **Save** → 可点 **Simulate Run** 预览哪些 tag 会被删，确认无误后 **Run Now** 立即执行一次，之后按调度周期自动跑（默认每日）。
5. 验证：过一天后回看，确认旧 tag 已被清理、磁盘不再无限增长。

> 若 Harbor 版本 UI 不同（如 2.8+ 把 Retention 放在 **Policies** 下），以控制台实际布局为准；核心是「按 repository 设保留份数 + 保护 release/latest 标签」。

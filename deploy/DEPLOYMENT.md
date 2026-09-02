# geesun_agent 生产部署规划

> 范围：geesun_agent 后端 + 依赖（Postgres/pgvector）+ 可观测性（Phoenix / Langfuse 并行）+ 日志/指标/追踪 集中（Loki / Alloy / Prometheus / Grafana，Alloy 替代原 Promtail 统一采集 logs+metrics+traces）+ 反向代理（Caddy）。
> 目标形态：单宿主 **Docker Swarm stack** 硬化部署（stack 名 `geesun`，与 flyctrl_deploy 的 `base` 隔离），全部镜像托管在内部 Harbor，物理机仅暴露 Caddy 入口；启动用 `deploy/start_stack.sh`（包装 `docker stack deploy`，内置 `build-push` 一键打包发布），停止用 `stop_stack.sh`、状态用 `service_stack.sh`。
> 本文档为规划稿，落地项见文末「待办清单」。

---

## 0. 架构总览

| 组件 | 镜像（Harbor） | 容器端口 | 主机发布端口 | 对外 | 数据持久化 | 关键依赖 |
|---|---|---|---|---|---|---|
| geesun-agent | `geesun_ai/geesun-agent:<tag>` | 8009 | 不发布（仅 appnet） | 经 Caddy | `/data/agent` `/data/uploads` `/data/reports`（host 绑定） | vLLM :8003、CubeSandbox、geesun-mcp :8000、Phoenix/Langfuse |
| geesun-mcp | `geesun_ai/geesun-mcp-server:<tag>` | 8000 | 不发布（仅 appnet） | 否 | 共享 `${AGENT_DATA_ROOT}/{agent,uploads,reports}`（与 agent 同挂） | agent（经服务名）、DLP 解密 API、CubeSandbox(E2B) |
| geesun-agent-web | `geesun_ai/geesun-agent-web:<tag>` | 3000 | 不发布（仅 appnet） | 经 Caddy :80 | 无（无状态） | geesun-agent（healthcheck 门控）；NEXT_PUBLIC_API_BASE 构建期内联 |
| agent-postgres | `dockerhub/pgvector:0.8.0-pg17` | 5432 | 不发布 | 否 | named volume `agent_pg_data` | — |
| caddy | `dockerhub/caddy:2.8-alpine` | 80 | 80 | 是 | `caddy_data` `caddy_config` | geesun-agent / geesun-agent-web |
| loki | `dockerhub/loki:3.2.0` | 3100 | 不发布 | 否（Grafana 接） | `loki_data` | — |
| alloy | `dockerhub/alloy:v1.19.2` | 4317/4321(OTLP)、12345(UI,仅 127.0.0.1) | 不发布（OTLP 仅 appnet） | 否 | `alloy_data` | loki、prometheus（docker.sock 仅读） |
| prometheus | `dockerhub/prometheus:3.0` | 9090 | 127.0.0.1:19091 | 否 | `prometheus_data` | —（需 `--web.enable-remote-write-receiver` 接收 Alloy remote_write） |
| grafana | `dockerhub/grafana:11.3.0` | 3000 | 127.0.0.1:3100 | 127.0.0.1:3100 | `grafana_data` | loki、prometheus |
| phoenix / langfuse（可观测栈） | **路线 A（默认，当前 67）**：复用现网共享实例（见 §10.3），agent OTLP 统一发往 Alloy（`PHOENIX_COLLECTOR_ENDPOINT=http://alloy:4317`，由 Alloy 转发到 Phoenix `10.10.10.67:4317`）；Langfuse 仍直发（`LANGFUSE_BASE_URL=http://10.10.10.67:3000`），**本 compose 不并入这两个文件**。**路线 B（自托管兜底，两文件始终保留不删）**：无共享实例时把 `docker-compose.phoenix.yml`+`docker-compose.langfuse.yml` 一并 `-f` 并入，agent OTLP 仍发往 Alloy、由 Alloy 经 `PHOENIX_OTLP_ENDPOINT=phoenix:4317` 转发到 phoenix 服务名；Langfuse 经 `langfuse-web:3000` 服务名直连（见 §4.9）。 | 路线 A：是（他人已发布）；路线 B：否（仅 appnet） | 路线 A：他人已发布；路线 B：Caddy 可选代理 | 路线 B：各 named volume | — |

**外部物理机（不在 compose 内，由 geesun-agent 跨网络访问）：**
- **vLLM**：`172.16.66.13:8003`（MoE 35B，`base_url=http://172.16.66.13:8003/v1`）
- **CubeSandbox**：`172.16.66.13`（cube-api 控制面 :6000；cube-egress MITM；sandbox 域名 `*.cube.app` 经 dnsmasq `address=` 直答指向 172.16.66.13，见 §4.8。⚠️ 8003 是 vLLM 不是 E2B，`E2B_API_URL` 必须指 :6000）
- **geesun_mcp_server**：现已容器化为 `geesun-mcp` 服务（见 `docker-compose.mcp.yml`），与 agent 同处 `appnet`，agent 经服务名 `geesun-mcp:8000` 访问；容器内部端口 8000 **不发布到主机**（避免与 dev 的 :8000 冲突），仅 appnet 内互访。DLP 解密 API 仍在 compose 外，由 `.env` 的 `DECRYPT_API_URL` 提供。
- **Harbor**：`172.16.220.74:8333`（HTTP，项目 `geesun_ai`（自有应用 geesun-agent）+ `dockerhub`（第三方通用镜像中央仓库 redis/minio/postgres/loki/grafana/phoenix/langfuse 等））

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
Phoenix / Langfuse / Postgres / pgvector / Caddy / Loki / Alloy / Prometheus / Grafana / Clickhouse / Redis / Minio 均为上游官方镜像，`build-push.sh` 的 `sync()` 会 `pull → tag → push` 进 Harbor，**不需要 Dockerfile**。

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
2. 构建并 push `geesun-agent:<tag>`（→ `geesun_ai` 项目）
3. `build_push_mcp()` 构建并 push `geesun-mcp-server:<tag>`（→ `geesun_ai` 项目，构建上下文 = `geesun_mcp_server` 仓库根；旧名 `sync_mcp`）
4. `build_push_web()` 构建并 push `geesun-agent-web:<tag>`（→ `geesun_ai` 项目，构建上下文 = `geesun_agent_web` 仓库根；`NEXT_PUBLIC_API_BASE` 构建期注入，默认 `http://10.10.10.67/`；旧名 `sync_web`）
5. `sync()` 拉取并将 11 个第三方镜像推入 Harbor `dockerhub` 项目（公网拉不到时回退用本地已有镜像）

### 2.3 镜像清单（源 → Harbor 目标）

| 源镜像 | Harbor 目标 |
|---|---|
| `pgvector/pgvector:0.8.0-pg17` | `dockerhub/pgvector:0.8.0-pg17` |
| `caddy:2.8-alpine` | `dockerhub/caddy:2.8-alpine` |
| `grafana/loki:3.2.0` | `dockerhub/loki:3.2.0` |
| `grafana/alloy:v1.19.2` | `dockerhub/alloy:v1.19.2` |
| `prometheus:3.0` | `dockerhub/prometheus:3.0` |
| `grafana/grafana:11.3.0` | `dockerhub/grafana:11.3.0` |
| `arizephoenix/phoenix:19.1.0` | `dockerhub/phoenix:19.1.0` |
| `postgres:16.14` | `dockerhub/postgres:16.14` |
| `langfuse/langfuse:3.224.3` | `dockerhub/langfuse:3.224.3` |
| `clickhouse/clickhouse-server:25.12` | `dockerhub/clickhouse-server:25.12` |
| `cgr.dev/chainguard/minio` | `dockerhub/minio:chainguard` |
| `redis:7` | `dockerhub/redis:7` |
| `postgres:17` | `dockerhub/postgres:17` |

---

## 3. 镜像版本管理

### 3.1 命名规范（镜像不区分环境，配置区分环境）
**核心原则：构建一次、跨环境晋升同一镜像。** 环境差异（dev 的 `LOG_LEVEL=DEBUG`、prod 的 `otel_project_name=Geesun-Agent-prod`）全部由 `.env` 注入，镜像保持环境无关——绝不把 dev / prod 烤进 tag，否则 prod 跑的镜像你其实没在 dev 验证过。

- **geesun-agent（生产）**：不可变 `<语义版本>-<gitsha>`（如 `1.0.0-a1b2c3d`），`GEESUN_AGENT_TAG` 在 `.env` 指定；**禁用 `latest`**（不可复现）。
- **geesun-agent（开发）**：可用浮动 `dev-<branch>` 便于本地丢，但永不进生产。
- **环境隔离（可选）**：若需物理隔离，Harbor 可为自有应用开 `geesun_ai-dev` / `geesun_ai` 两个项目；第三方通用镜像统一在 `dockerhub`（跨项目共享，不区分环境）。当前 dev/prod 用 tag 后缀 + `.env` 区分即可。
- **第三方**：已固定具体版本（loki 3.2.0、grafana 11.3.0、phoenix 19.1.0、pgvector 0.8.0-pg17、langfuse 3.224.3 等）。
- **Langfuse 版本说明**：已 pin 到 `3.224.3`（当前可 `docker pull` 的最新稳定版）。`.env` 的 `LANGFUSE_TAG` 控制，`build-push.sh` 已同步 `langfuse:3.224.3`。
  > 注：本地 `langfuse` 源码仓库为 **v4.0.0**（你在开发的分支），但官方自托管**可拉取镜像**稳定线仍是 `:3`，`3.224.3` 是该线最新具体版本，与源码 `4.0.0` 并非同一号。若未来要从源码构建 `4.0.0` 镜像，需单独 `docker build` 并改 `LANGFUSE_TAG`，届时同步 `build-push.sh`。

### 3.2 Harbor 保留策略
在 Harbor 项目 `geesun_ai` 与 `dockerhub` 分别配置 **Tag Retention**：
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
- 所有服务加入自定义 bridge 网络 **`appnet`**，容器间用**服务名**互访（如 agent 配 `mcp_server_url=http://geesun-mcp:8000/mcp`）。
- 多个 compose 用 `-f` 合并时，各自的 `networks: [appnet]` 会加入同一网络（名称固定为 `appnet`）。
- ⚠️ **可观测性不在 appnet 内**：Phoenix/Langfuse 为现网共享实例（另一 compose），agent 经主机 LAN IP 已发布端口连接（`http://10.10.10.67:4317` / `http://10.10.10.67:3000`）。

### 4.2 对外暴露（最小化）
- 只暴露 **Caddy**：`:80`（前端主入口，`/api/*` 路由到 agent）。Phoenix(`:6006`)、Langfuse(`:3000`)、Minio S3(`:9092`) 由现网共享容器直出，**不经 Caddy**（避免与其已发布端口冲突）。
- 数据库 / Loki / Grafana(127.0.0.1:3100) **不映射主机端口**，仅经 `appnet` 内部访问。

### 4.3 访问外部物理机（vLLM / CubeSandbox / MCP）与 CA 信任
- 容器通过宿主机的 NAT（MASQUERADE）访问外部 IP（`172.16.66.13` vLLM :8003 与 CubeSandbox :6000），agent 内 `base_url=http://172.16.66.13:8003/v1` 可直接连通（前提是宿主机能路由到该网段）。
- **CubeSandbox egress MITM CA（docker 化，已落地）**：CubeSandbox 走 `cube-egress` 做 TLS 拦截，提供 `rootCA.pem`。**agent 与 geesun-mcp 两个容器都已配好**（`docker-compose.yml` 与 `docker-compose.mcp.yml`）：
  - bind mount：`${CA_MOUNT_SRC:-../certs/combined-ca.pem}` → `/etc/ssl/certs/combined-ca.pem:ro`（相对 `deploy/` 目录；生产机把 `combined-ca.pem` 拷到 deploy 上级 certs/ 即可）；
  - **必须用 `combined-ca.pem`（mkcert CA + 147 系统根 bundle，约 148 证书/227KB）**：仅挂 `rootCA.pem` 单证书（1688B）会把容器内 Python/pip/uv 的默认信任库整体替换成这一个 CA、系统根被全部排除 → 拉 pypi.org（GlobalSign 签发）报 `UnknownIssuer`，agent 启动即崩（2026-09-01 实测反证）。bundle 同时含 mkcert CA（信任内网 cube-egress 拦截）+ 系统根（信任外网），两者缺一不可。
  - **生成方式（处理流程，脚本化）**：宿主机执行 `bash deploy/setup-combined-ca.sh`——脚本自动①定位本机 mkcert 单证书（`certs/rootCA.pem` 或 `cube-root-ca.pem`/`cube-ca.pem`，可用 `CA_INPUT=` 覆盖）→ ②探测系统根 bundle（Rocky/OpenCloudOS `/etc/pki/tls/certs/ca-bundle.crt`；Debian/Ubuntu `/etc/ssl/certs/ca-certificates.crt`）→ ③python3 按证书块 sha256 合并去重 → ④输出 `combined-ca.pem` 到 `CA_MOUNT_SRC` 默认路径 `certs/combined-ca.pem`，并打印证书数/大小/md5 供比对。**不要再手动 `cat rootCA.pem ca-bundle.crt > combined-ca.pem`**（旧流程不可复现、易漏去重）。**2026-09-02 宿主机实测**：脚本输出与现网 `combined-ca.pem` 证书集合逐证书一致（148/148，sha256 指纹 0 差异），仅块间分隔格式不同、md5 不同属正常，信任语义等价。
  - 环境变量：agent 设 `REQUESTS_CA_BUNDLE` + `SSL_CERT_FILE`；mcp 设 `SSL_CERT_FILE`；`CUBE_CA_PATH` 同指容器内 `combined-ca.pem`（sandbox.py 读取）；
  - 若 sandbox 代理无 MITM，可删挂载与该变量。
- 连接外网组件走 LAN IP 时，容器→主机→目标 NAT 自动完成，无需 `host.docker.internal`。

### 4.4 TLS
- Caddy 做 TLS 终止，使用内部 CA（mkcert 链）签发 `10.10.10.67` 证书；内网服务之间明文。证书存于 `caddy_data` volume。

### 4.5 端口冲突速查（含部署前预检）
容器**内部端口**（appnet 服务名互访）与**主机发布端口**（`ports:` 映射）是两回事：仅在 `ports:` 里声明的才占主机端口；容器间互访走服务名，不占主机端口。

**部署前在 10.10.10.67 上预检我方端口是否空闲**（该机已跑共享可观测栈，见下表"复用"行，勿占用其端口）：
```sh
ss -tlnp | grep -E ':(80|8009|8000|3100)\b'
```
- 预期：`80 / 8009 / 8000 / 3100` **全部空闲**（这是我方要用的主机端口）。
- 若 80 被占用 → 换 Caddy 映射：改 `docker-compose.yml` 的 caddy `ports:` + `.env` 的 `CORS_ALLOW_ORIGINS` + 前端镜像 `NEXT_PUBLIC_API_BASE`（构建期）后重新部署。

| 端口 | 用途 | 主机发布? | 处理 |
|---|---|---|---|
| 80 | Caddy 入口（前端 + `/api/*` → agent） | 是 | 我方唯一主机端口 |
| 8009 | geesun-agent | 否（仅 appnet） | 不冲突 |
| 8000 | geesun-mcp | 否（仅 appnet） | 不发布，不冲突 |
| 3100 | Grafana | 127.0.0.1:3100 | 仅本机排障 |
| 3000 / 6006 / 4317 / 9090 / 9092 / 5432 | **共享可观测栈**（现网 langfuse-web / opt-phoenix / langfuse-minio / opt-db） | 是（他人已发布） | **复用，勿占用、勿下线**；agent 经主机 IP 连接 |

---

### 4.6 MCP（geesun_mcp_server）容器化部署
开发期 mcp 与 agent 同机裸跑无问题，但 docker-compose 部署有两个真隐患（已修）：
1. **agent 容器内 `localhost` 指向自身**：`mcp.json` 默认 `http://localhost:8000/mcp`，在容器内 localhost 是 agent 自己而非 mcp → 连不到。修复：`config.py` 新增 `mcp_server_url`（默认 dev 用 localhost，prod 由 `.env` 覆为 `http://geesun-mcp:8000/mcp`）；`mcp.py` 默认服务改读该值。
2. **mcp 服务绑回环**：原 `main.py` 绑 `127.0.0.1:8000`，仅听本机回环，容器内 agent 经服务名访问不到。修复：`main.py` 改绑 `0.0.0.0:8000`（dev 同机裸跑仍兼容 127.0.0.1）。

**拓扑**：`geesun-mcp` 服务与 `geesun-agent` 同处 `appnet`，agent 用服务名 `geesun-mcp:8000` 访问；mcp 容器内部端口 8000 **不发布到主机**（避免与 dev 裸跑的 :8000 冲突），仅 appnet 内互访。

**共享挂载**：mcp 复用 agent 的 `${AGENT_DATA_ROOT}/{agent,uploads,reports}`，因为 mcp 需：
- 读 skills（`AGENT_WORKSPACE` 下的 `skills/`）；
- 解析 `/uploads` `/reports` 虚拟路径（`_resolve_host_path`）。

**外部依赖**：
- DLP 解密网关 `DECRYPT_API_URL`（compose 外，由 `.env` 提供）；
- CubeSandbox(E2B) `E2B_API_URL` / `E2B_API_KEY`（MCP 内 `e2b_code_interpreter` / `upload_to_sandbox` 用，需信任 egress CA，见 §4.3）。**`E2B_API_URL` 必须指向 cube-api 控制面 `http://172.16.66.13:6000`**——8003 是 vLLM，指向它会 404 且 e2b 报 `KeyError: 'code'`（2026-09-02 实测）。

**⚠️ mcp.json 残留坑**：agent 首次启动会按 `mcp_server_url` 生成 `{AGENT_WORKSPACE}/mcp.json`。若你**之前**手动改过 `mcp.json`（或旧部署遗留），里面的 url 可能是 `localhost`，改 `.env` 的 `mcp_server_url` **不会自动覆盖**已存在的 `mcp.json`。修复：`rm {AGENT_DATA_ROOT}/agent/mcp.json` 让它用新默认值重新生成，或手动把里面 `decrypt-file.url` 改成 `http://geesun-mcp:8000/mcp`。

**合并启动**（路线 A：prod 仅 3 个文件，复用现网共享实例；需自托管见 §4.9 路线 B）：
```sh
./start_stack.sh --with=mcp,web            # 默认先 build-push 打包再 stack deploy
# 等价展开：docker stack deploy -c docker-compose.yml -c docker-compose.mcp.yml \
#          -c docker-compose.web.yml --with-registry-auth --resolve-image=always --prune geesun
```

**镜像构建**：`build-push.sh` 已加 `build_push_mcp()`（旧名 `sync_mcp`），会把 `geesun_mcp_server` 仓库（上下文=其根）构建并推到 `geesun_ai/geesun-mcp-server:<MCP_TAG>`。基镜像已从 `python:3.11` 改 `python:3.13-slim-bookworm`，与 `pyproject.toml` 的 `requires-python>=3.13` 及 dev 运行时一致，避免 dev/prod 漂移。

**排障 runbook**：
- mcp 工具加载失败（agent 日志 `MCP server [decrypt-file] 加载工具失败`）：
  1. `docker exec geesun-agent curl -s http://geesun-mcp:8000/mcp` 确认 appnet 内可达；
  2. `docker logs geesun-mcp` 看是否 `ValidationError`（env 缺失）或 E2B/解密报错；
  3. 确认 `mcp.json` 的 url 已是 `geesun-mcp:8000/mcp`（见上方残留坑）。
- E2B 调用报错 `KeyError: 'code'`（工具级 `ToolException: 'code'`）：根因是 `E2B_API_URL` 指向 vLLM(8003) 而非 cube-api(6000)，`E2BSandbox.connect` 打 404 响应体无 `code` 字段致 e2b 解析崩。修复：`.env` 的 `E2B_API_URL=http://172.16.66.13:6000` 后 redeploy mcp 服务。
- E2B 调用 TLS 报错：确认 `CA_MOUNT_SRC` 指向真实 CA 文件且 `SSL_CERT_FILE` 已注入（无 MITM 则删挂载+变量）。

---

### 4.7 前端（geesun_agent_web）容器化部署
前端 Next.js 16 已容器化为 `geesun-agent-web` 服务（`docker-compose.web.yml`），与 agent 同处 `appnet`，内部 3000 不发布主机，由 Caddy :80 对外。

**⚠️ 构建期注入（最容易踩的坑）**：`NEXT_PUBLIC_API_BASE` 是 Next.js **公共变量，浏览器直接读构建产物**——属于 build-time 内联，运行时改 `.env` **无效**。生产镜像默认内联 `http://10.10.10.67/`（Caddy :80 同域）；换环境必须用 `NEXT_PUBLIC_API_BASE=xxx bash deploy/build-push.sh` 重新构建镜像，不能靠改 `.env`。

**⚠️ Caddy 路径路由（防 rewrite 回环）**：前端 `next.config.ts` 会把 `/api/*` rewrite 到 `NEXT_PUBLIC_API_BASE`（同域）。若 Caddy 把 `/` 整体代理到 web，`/api/*` 请求会 Caddy→web→rewrite→Caddy 无限循环。因此 `Caddyfile` 必须按路径分流：`/api/*`、`/docs`、`/openapi.json` 直连 `geesun-agent:8009`，其余走 `geesun-agent-web:3000`（已实现）。

**前端本地 dev 不受影响**：dev 仍是 `bun run dev`（读 `.env.local`，默认 `http://localhost:8009`），`next.config.ts` 的 `output: "standalone"` 仅对 `next build` 生效，`next dev` 忽略；容器与 dev 是两套独立运行方式。

**合并启动**（路线 A：prod 仅 3 个文件，复用现网共享实例；需自托管见 §4.9 路线 B）：
```sh
./start_stack.sh --with=mcp,web
```

**排障**：
- 前端打不开但后端通：`docker logs geesun-agent-web` 看是否 `next build` 产物缺失（确认镜像用 `build_push_web`（旧名 `sync_web`）重建，含 `.next/static` 与 `public`）。
- `/api/*` 404 或回环：确认 Caddyfile 是路径分流版本（`handle /api/*` 在前），且前端镜像 `NEXT_PUBLIC_API_BASE` 为同域地址。

---

### 4.8 CubeSandbox 域名（`*.cube.app`）与 DNS（docker 化）
sandbox 访问域名形如 `49983-78083c0f5b044a3084a891a2c1f35b50.cube.app`，e2b SDK 用该域名连沙箱运行时（`connection_config.get_sandbox_url`）；**生产容器里必须能解析到 `172.16.66.13`**（cube-egress 所在裸金属，dev 机 WSL 的 dnsmasq 也是 `address=/cube.app/172.16.66.13`）。

**Docker 的 DNS 链**：容器 → `127.0.0.11`（docker 内嵌 DNS，只解 appnet 服务名）→ 转发其它域名给上游 resolver（daemon.json `dns` / resolv.conf）。

**正确做法（已在 `deploy/setup-cube-dns.sh` 落地，root 执行一次）**：
1. 宿主机装 dnsmasq，写入 `/etc/dnsmasq.d/cube.conf`：**`address=/cube.app/172.16.66.13`（address= 本地直答模式）** + `server=<resolv.conf 上游>`（保持外网解析）+ `listen-address=<docker0 网关>,127.0.0.1`；
2. `/etc/docker/daemon.json` 加 `"dns": ["<dnsmasq 监听 IP>"]`（如 docker0 网关 `172.17.0.1`），重启 docker；
3. 验证：`docker run --rm busybox nslookup xxx.cube.app` → 应返回 `172.16.66.13`。

**⚠️ 必须 `address=` 直答，不要 `server=/cube.app/<IP>` 转发**：`server=` 会把查询转发给 `<IP>:53` 的 DNS 服务，而 172.16.66.13 上并无 DNS 服务（实测 53 端口不通），转发必 NXDOMAIN。此前脚本默认 `server=/cube.app/192.168.10.136` 是旧 QEMU dev 环境的写法（136 上确有 dnsmasq），裸金属环境已不适用（2026-09-02 实测修正）。

**为什么不用 compose 的 `dns:` 字段**：它会顶掉 `127.0.0.11`，导致 agent/mcp 容器**无法用服务名**（`geesun-mcp`/`agent-postgres`/`geesun-agent-web`）互访——内嵌 DNS 是服务名解析的唯一通道。daemon 级 `dns` 则保留内嵌 DNS，仅把外部域名转发给 dnsmasq。

**前提**：67 到 `172.16.66.13` 网络可达（:6000/:8003 实测通）。

---

### 4.9 可观测栈两种路线：A 复用共享实例（默认）/ B 自托管兜底

Phoenix + Langfuse 有两条部署路线。**两个 compose 文件（`docker-compose.phoenix.yml`、`docker-compose.langfuse.yml`）始终留在仓库，不删除**——它们是路线 B 的兜底：万一下次换环境 / 换机器 / 共用栈下线、没有现成实例时，可直接拉起自托管栈。

**路线 A — 复用现网共享实例（当前 67 默认）**
- 现网 `10.10.10.67` 已跑一套 Phoenix/Langfuse（见 §10.3），prod 主栈**只并 3 个文件**（yml+mcp+web），不并 phoenix/langfuse 文件。
- agent 经主机 LAN IP 连接：`PHOENIX_COLLECTOR_ENDPOINT=http://10.10.10.67:4317`、`LANGFUSE_BASE_URL=http://10.10.10.67:3000`。
- 共享实例的 3000/6006/9092/5432 端口**勿占用、勿下线**（§4.5 预检表）。

**路线 B — 自托管兜底（无共享实例时）**
- 把两个可观测 compose 文件一并并入（与 yml/mcp/web 合并到同一 `appnet`）：
```sh
./start_stack.sh --with=phoenix,langfuse,mcp,web
```
- `.env` 须填全 Langfuse 自托管变量（DATABASE_URL / SALT / ENCRYPTION_KEY / CLICKHOUSE_* / REDIS_* / MINIO_* / LANGFUSE_S3_* 等，`.env.example` 已含），并把追踪端点改走服务名：
```sh
PHOENIX_COLLECTOR_ENDPOINT=http://phoenix:4317
LANGFUSE_BASE_URL=http://langfuse-web:3000
```
- 端口：phoenix(6006/4317)、langfuse-web(3000)、minio(9092) 在 `appnet` 内可达；langfuse 的 minio 映射到主机 9092、clickhouse/redis/postgres 仅绑 127.0.0.1（排障用）。只要同机无其它进程占 9092/8123/9000/6379/5432，路线 B 与主栈不冲突（主栈只映射 80 与 127.0.0.1:3100）。
- 若想在路线 B 也从外部看 UI，可在 `Caddyfile` 加 `:6006`/`:3000` 代理块（前提：这些主机端口空闲、未被任何共享实例占用）；否则 UI 仅 appnet 内可达，不影响 agent 上报 trace。

> 切换路线只改「并入哪几个 `-f` 文件」+ 对应 `*_BASE_URL` 端点，镜像与业务栈不变。

---

### 4.10 以 Docker Swarm stack 部署（推荐）

部署形态已从 `docker compose up` 切换为 **Docker Swarm stack**（与 `flyctrl_deploy` 的 `base` 同源思路，但 stack 名用 `geesun` 以免同机互相覆盖）。`deploy/` 下三个脚本封装了完整发布链路：

| 脚本 | 作用 |
|---|---|
| `start_stack.sh` | 前置检查（docker / swarm / `.env`）→ 默认跑 `build-push.sh` 打包推 Harbor → `docker stack deploy`（必带 `--with-registry-auth --resolve-image=always --prune`）；`--no-build` 跳过打包，`--with=phoenix,langfuse,mcp,web` 叠加附加 compose |
| `stop_stack.sh` | `docker stack rm geesun`（移除服务/网络，**不删命名卷**）|
| `service_stack.sh` | `docker stack services geesun` 看副本/镜像/端口 |

**`docker stack deploy` 三个必带参数（漏了必踩坑）**：
- `--with-registry-auth`：Harbor 是私有仓库，不带则节点拉不到镜像（依赖本机已 `docker login`）。
- `--resolve-image=always`：固定 tag（如 `1.0.0`）重新发布时，Swarm 默认认为镜像未变而不更新；此参数强制每次拉新。
- `--prune`：compose 里删掉的服务会被真正从 stack 清理（否则成为孤儿服务）。

**与 `docker compose up` 的关键差异（已在本仓库 compose 文件改好，勿回退）**：
- ⚠️ `restart:` 字段被 Swarm **忽略** → 全部改为 `deploy.restart_policy: { condition: any }`（异常退出自动拉起）。
- ⚠️ `depends_on` **只认列表语法** `depends_on: [x]`（仅保证先创建，不保证就绪）；`condition: service_healthy` 长语法直接报 `depends_on must be a list`，map 语法同理 → 已全部改为列表，就绪等待靠 `healthcheck` + `restart_policy` 自愈（agent 探 `/docs`、postgres `pg_isready`、alloy/prometheus/grafana 探 UI 等）。
- ⚠️ **`host_ip` 不被 Swarm 接受**：短端口 `127.0.0.1:host:container` 的 host IP 绑定在 Swarm 下直接报 `Additional property host_ip is not allowed` → 已**删除全部 `host_ip` 字段**，alloy/prometheus/grafana 等本机服务改用 `published: "127.0.0.1:xxxx"` 短语法尽力约束（Swarm ingress 下实际可能全网卡监听，属已知权衡）；caddy `80` 保留 ingress 全网卡（本就对外）。
- ⚠️ `appnet` 网络去掉 `name:`（与 stack 前缀冲突），改 `driver: overlay`；stack 会加 `geesun_` 前缀 → 实际网络名 `geesun_appnet`。附加 compose 文件不再重复定义 `appnet`，只引用。
- 命名卷自动加 `geesun_` 前缀（如 `geesun_agent_pg_data` / `geesun_loki_data`）；**生产尚未部署，无历史数据迁移问题**，直接切。
- `env_file: [.env]` 仍生效（start_stack.sh 在 deploy/ 目录执行，`.env` 由 docker 在 deploy 时插值注入）。

**发布 / 停止 / 状态**：
```sh
cd deploy
./start_stack.sh --with=mcp,web          # 打包 + 发布主栈 + mcp + web
./start_stack.sh --no-build              # 仅用已推送镜像重新部署（改配置后快速重启）
./service_stack.sh                       # 看服务状态
./stop_stack.sh                          # 停止（数据卷保留）
```

**生产验证（部署后）**：见 §7.8 校验 —— `./service_stack.sh` 全 `1/1 Running`；`http://10.10.10.67/` 前端、`127.0.0.1:3100` Grafana(看 Loki 有日志)、`127.0.0.1:19091` Prometheus(看 `geesun-agent`/`alloy` target up)、`127.0.0.1:12345` Alloy UI；agent 经 `alloy:4317` → Phoenix trace 链路通。

**⚠️ 行尾铁律（CRLF 坑，已踩两次）**：仓库文本已由 `.gitattributes` 强制 `eol=lf`，但**目标机手动拷贝的 deploy 目录**若来自 Windows 仍可能带 CRLF。症状链：脚本报 `pipefail invalid option` → yml 报 schema 错 → **`.env` 值尾带 `\r` 时 compose 展开 `${REGISTRY_HUB}` 得到 `...\r/pgvector:...` 报 `invalid reference format`**（`.env` 极易漏修）。目标机就地统一修复：
```sh
cd deploy
sed -i 's/\r$//' .env *.sh *.yml *.yaml *.alloy *.md Caddyfile loki-config.yaml prometheus.yml
sed -n "$(grep -n '^REGISTRY_HUB=' .env | cut -d: -f1)p" .env | cat -A   # 确认行尾是 $ 而非 ^M$
```

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
`geesun-agent(stdout JSON)` → Docker `json-file`(本地轮转) → Alloy(`discovery.docker` 发现 appnet 容器、`loki.process` 提取 JSON 字段，并统一收 metrics→Prometheus / traces→Phoenix) → Loki(30d 留存) + Prometheus(metrics) → Grafana(检索/面板/告警)。
- **验证**：进 alloy 容器看 UI `:12345`（本机 `127.0.0.1:12345`）；Prometheus `:19091` 查 `up`/`geesun-agent`/`alloy`；Phoenix（路线 A `10.10.10.67:6006`）确认 traces 到达。Alloy→Phoenix 转发经 `PHOENIX_OTLP_ENDPOINT` 控制（路线 B 改 `phoenix:4317`）。

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
3. `HARBOR_USER/PASSWORD bash deploy/build-push.sh <tag>` 把镜像推入 Harbor（geesun-agent→`geesun_ai`，第三方→`dockerhub`）。

**阶段 B — 生产机（仅内网，从 Harbor 拉）**
1. 安装 Docker + compose plugin，配置 Harbor 不安全仓库（2.1）。
2. `bash deploy/init-host.sh`（自动建 `/opt/geesun/data/{agent,uploads,reports}` 与备份目录，并把属主改成容器 UID 1001，避免 Docker 自动以 root 建目录导致容器无写权限；见 §9 #4 ✅）。
3. 拷贝 `deploy/` 目录到生产机（或 git 拉取）。
4. `cd deploy && cp .env.example .env && vi .env`：按下方 **`.env` 初始化清单** 逐项填写（大部分不能用默认值）。

   **`.env` 初始化清单（拷贝后逐项确认；漏填会在 `up` 时直接报错或功能静默失效）：**
   - **A. 必改（无默认值，漏填容器 `ValidationError` 必崩）：**
     - `base_url` / `openai_api_key` / `model_name` / `agent_workspace` / `upload_root` / `report_root`：agent 必填（`config.py` 无默认）。
     - `AGENT_PG_PASSWORD` / `POSTGRES_PASSWORD`：两套 PG 各自强密码。
     - `DECRYPT_API_URL`：DLP 解密网关（MCP 调用，compose 外）。
     - `E2B_API_URL` / `E2B_API_KEY`：CubeSandbox(E2B) 代理（MCP 内 `e2b_code_interpreter` 用）。
     - `NEXTAUTH_SECRET` / `ENCRYPTION_KEY`：`openssl rand -hex 32`。
     - `SALT`：`openssl rand -hex 16`。
     - `GRAFANA_PASSWORD` / `REDIS_AUTH` / `CLICKHOUSE_PASSWORD`：各自强密码。
   - **B. 强匹配约束（漏改会静默功能失效，务必逐对核对）：**
     - `PHOENIX_SQL_DATABASE_URL` 内密码 = `PHOENIX_DB_PASSWORD`
     - `DATABASE_URL` 内密码 = `POSTGRES_PASSWORD`
     - `LANGFUSE_S3_EVENT/MEDIA/BATCH_EXPORT_SECRET_ACCESS_KEY` 三者都 = `MINIO_ROOT_PASSWORD`
     - `mcp_server_url` 必须 = `http://geesun-mcp:8000/mcp`（服务名，非 localhost）
   - **C. 建议改（有默认值但应设真实值）：**
     - `mcp_token`：agent 调 MCP 的 Bearer token（当前 mcp 服务未做服务端校验，但保持非默认以便日后开启鉴权）。
     - `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`：首次启动后从 Langfuse UI 取，回填后 `up -d geesun-agent` 重启生效（§7 阶段 C）。
     - `CORS_ALLOW_ORIGINS`：按需改成 Web 实际域名/IP。
   - **D. 可保持默认（通常无需改）：**
     - `LOG_LEVEL`/`LOG_FORMAT`、`AGENT_PG_*`、`AGENT_DATA_ROOT`、`REGISTRY_GEESUN/HUB`、`GEESUN_AGENT_TAG`、`LANGFUSE_TAG`、`POSTGRES_VERSION`、`PHOENIX_DB_*`(名称)、`OTEL_PROJECT_NAME`、`*_BASE_URL`(服务名)、网络类 `REDIS_HOST/PORT` 等。
5. （可选但推荐）CubeSandbox 信任与 DNS：在宿主机跑 `bash deploy/setup-combined-ca.sh` 生成 `combined-ca.pem`（自动合并本机 `rootCA.pem` + 系统根，输出到 deploy 上级 certs/，见 §4.3），并 root 执行 `bash deploy/setup-cube-dns.sh`（dnsmasq `address=` 直答 `*.cube.app` → 172.16.66.13，见 §4.8）。
6. 一键发布（打包镜像 → 推 Harbor → stack deploy；`--no-build` 可跳过打包只重启）：
   ```sh
   ./start_stack.sh --with=mcp,web
   ```
   > 前置：`docker swarm init`（单节点即可）、`docker login` 到 Harbor（start_stack.sh 的 `--with-registry-auth` 依赖本机凭证）。
7. 查看状态：
   ```sh
   ./service_stack.sh
   ```
8. 校验：
   - `docker compose ps` 全 healthy；
   - `http://10.10.10.67/` → 前端页面（Caddy → geesun-agent-web）；API 文档 `http://10.10.10.67/docs`（Caddy → geesun-agent:8009）；
   - `http://10.10.10.67:6006` → Phoenix（现网共享实例）；
   - `http://10.10.10.67:3000` → Langfuse（现网共享实例），注册/使用 prod project 的 API key；
   - `127.0.0.1:3100` → Grafana，看 Loki 数据源有日志。

**阶段 C — 接线**
9. Langfuse 内 `Settings → API Keys` 拿 public/secret key，回填 `.env` 的 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 并 `up -d geesun-agent` 重启 agent 生效。
10. 验证 agent 一次真实对话：Phoenix 出 span、Langfuse 出 trace、Grafana 出 JSON 日志。

---

## 8. 注意事项 / 已知坑

- **Harbor HTTP**：务必配 `insecure-registries`，否则 login/pull 失败。
- **构建上下文**：`geesun-agent` 必须在 `/d/workspace` 下构建，否则 `langchain-cubesandbox` 解析失败。
- **9090 端口冲突**：Phoenix 的 9090 由现网共享实例发布，prod 不再自建 Phoenix/Langfuse，无冲突。
- **三个 Postgres**：agent_mem / phoenix / langfuse 各自独立，资源占用偏高；后续若想省可合并实例（按安全域权衡）。
- **密钥**：`.env` 必须 `chmod 600` 且**绝不进 git**；生产可进一步改用 compose `secrets:` 挂载。
- **DB 不暴露**：仅 `127.0.0.1` 供排障；对外只走 Caddy。
- **资源限制**：各服务已加 `deploy.resources.limits` + `deploy.restart_policy`（Swarm 下 `limits` 真正生效做资源硬限、`restart_policy: any` 负责异常自愈；`docker compose` 非 swarm 下 `limits` 仅提示）。
- **Langfuse 初始化**：官方 clickhouse migration 步骤务必在 `up` 前核对；首次启动 worker 会跑迁移，需等 healthy。
- **不要 `-v` 删卷**：见 5.4。
- **外部依赖可达性**：部署后先 `docker exec geesun-agent curl -s http://172.16.66.13:8003/v1/models` 验证 vLLM 连通，再验 CubeSandbox / MCP。
- **MCP 容器化**：geesun_mcp_server 现已容器化为 `geesun-mcp` 服务（见 `docker-compose.mcp.yml`），合并 `up` 时加 `-f docker-compose.mcp.yml`；agent 经服务名 `geesun-mcp:8000` 访问，勿再手跑裸进程。部署拓扑与排障见 §4.6。
- **前端容器化**：geesun_agent_web 已容器化为 `geesun-agent-web`（见 `docker-compose.web.yml`），合并 `up` 时加 `-f docker-compose.web.yml`；`NEXT_PUBLIC_API_BASE` 为构建期内联（运行时改 `.env` 无效）、Caddy 按路径分流防 rewrite 回环，详见 §4.7。
- **部署前端口预检**：务必先跑 §4.5 的 `ss -tlnp` 确认**我方端口**（80/8009/8000/3100）空闲；3000/6006/9092/5432 为现网共享可观测栈，**复用勿动**。
- **CubeSandbox DNS**：容器内 `*.cube.app` 解析必须配 dnsmasq + daemon `dns`（§4.8），**不要**用 compose `dns:`（会顶掉 127.0.0.11 导致服务名解析失效）。

---

## 9. 待办清单（与本文档配套的实现项）

| # | 事项 | 文件 | 状态 |
|---|---|---|---|
| 1 | geesun-agent 挂载 `/data/{agent,uploads,reports}` 到宿主机 | `docker-compose.yml`（`${AGENT_DATA_ROOT}` 驱动） | ✅ 已落地 |
| 2 | Langfuse 镜像 pin 到具体补丁版本（3.224.3） | `docker-compose.langfuse.yml`（`${LANGFUSE_TAG}`）+ `build-push.sh` + `.env.example` | ✅ 已落地 |
| 3 | uvicorn.access 注入 JSON formatter（`--log-config`） | `logging.uvicorn.json` + Dockerfile CMD | ✅ 已落地 |
| 4 | 生产机 `/opt/geesun/data` 目录预创建脚本 | 独立 `init-host.sh`（含属主修正） | ✅ 已落地 |
| 5 | 每日备份脚本（pg_dump / minio mirror） | 新增 `backup.sh` + cron 示例 | ✅ 已落地 |
| 6 | Harbor `geesun_ai` + `dockerhub` 项目 Tag Retention 规则 | Harbor 控制台人工配置 | ⬜ 待配置（步骤见 §11） |
| 7 | 三项目配置全收口到 `.env`（Phoenix 入 `.env` + compose 去硬编码；Langfuse 补齐所有变量且去除不安全默认值，改为 `${VAR}` 强契约） | `.env.example` + `docker-compose.phoenix.yml` + `docker-compose.langfuse.yml` | ✅ 已落地 |
| 8 | geesun_mcp_server 容器化进 compose（docker-compose.mcp.yml + build-push.sh build_push_mcp（旧名 sync_mcp）+ requirements.txt + Dockerfile 基镜像 3.13） | `docker-compose.mcp.yml` + `build-push.sh` + `geesun_mcp_server/requirements.txt` + `geesun_mcp_server/Dockerfile` | ✅ 已落地 |
| 9 | geesun_agent_web 容器化进 compose（docker-compose.web.yml + build-push.sh build_push_web（旧名 sync_web）+ Caddy 路径路由防回环 + next.config standalone） | `docker-compose.web.yml` + `build-push.sh` + `geesun_agent_web/Dockerfile` + `geesun_agent_web/.dockerignore` + `Caddyfile` + `next.config.ts` | ✅ 已落地 |
| 10 | 复用现网共享可观测栈（路线 A）：prod compose 仅 yml+mcp+web 三文件，Caddy 只留 :80，agent 经 10.10.10.67:4317/:3000 连共享 Phoenix/Langfuse；phoenix/langfuse 两个 compose 文件保留作路线 B 兜底（见 §4.9） | `docker-compose.yml` + `Caddyfile` + `.env.example` + 前端镜像 `NEXT_PUBLIC_API_BASE` + `docker-compose.phoenix.yml` + `docker-compose.langfuse.yml` | ✅ 已落地 |
| 11 | CubeSandbox 信任与 DNS docker 化：agent/mcp CA 挂载 + REQUESTS_CA_BUNDLE/SSL_CERT_FILE；`setup-combined-ca.sh`（生成 mkcert+系统根 bundle）；`setup-cube-dns.sh`（dnsmasq 直答 *.cube.app + daemon dns） | `docker-compose.yml` + `docker-compose.mcp.yml` + 新增 `setup-combined-ca.sh` + `setup-cube-dns.sh` + §4.3/§4.8 | ✅ 已落地 |

> 实现项 #1–#5、#7–#11 已落地；仅 #6（Harbor Retention）需在控制台人工配置，步骤见 §11。

---

## 10. 环境策略：dev / prod 区分（路线 A 默认；langfuse/phoenix 文件保留为路线 B 兜底）

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

### 10.3 端口现状与处理（已按"复用共享可观测栈"落地）
- **现状（2026-08-27 实测）**：10.10.10.67 上运行 **dev/共享可观测栈**（已 3 周）：`langfuse-langfuse-web-1`(:3000)、`langfuse-worker`(:3030 lo)、`opt-phoenix-1`(:6006/:4317/:9090)、`opt-db-1`(:5432)、`langfuse-minio-1`(:9092/:9091 lo)、langfuse postgres/redis/clickhouse（内部/lo）。**主机 80 空闲**。
- **决策**：遵循路线 A——**复用**这套 Phoenix/Langfuse 作为 dev/prod 共享后端（project key 区分）；prod compose **只并 3 个文件**（yml+mcp+web），不并 phoenix/langfuse 文件；Caddy 只用 :80（前端主入口）；agent 经 `http://10.10.10.67:4317` / `http://10.10.10.67:3000` 连接共享实例。共享实例的 3000/6006/9092/5432 等端口**勿占用、勿下线**。两个可观测 compose 文件（`docker-compose.phoenix.yml`/`docker-compose.langfuse.yml`）**保留在仓库作路线 B 兜底，不删除**——换环境无共享实例时按 §4.9 并入即可。

### 10.4 agent 环境变量按环境切换
prod 的 `geesun_agent/deploy/.env` 至少区分：
```sh
OTEL_PROJECT_NAME=Geesun-Agent-prod
LANGFUSE_BASE_URL=http://10.10.10.67:3000
LANGFUSE_PUBLIC_KEY=pk-lf-prod-xxxx
LANGFUSE_SECRET_KEY=sk-lf-prod-xxxx
PHOENIX_COLLECTOR_ENDPOINT=http://10.10.10.67:4317
```

- **路线 B（自托管兜底）** 的端点不同：把 `LANGFUSE_BASE_URL` / `PHOENIX_COLLECTOR_ENDPOINT` 改走服务名（同 `appnet` 内可达），其余 Langfuse 自托管变量（DATABASE_URL / SALT / ENCRYPTION_KEY / CLICKHOUSE_* / REDIS_* / MINIO_* / LANGFUSE_S3_*）在 `.env` 填全（`.env.example` 已含）：
```sh
LANGFUSE_BASE_URL=http://langfuse-web:3000
PHOENIX_COLLECTOR_ENDPOINT=http://phoenix:4317
```

---

## 11. Harbor Tag Retention 手动配置（控制台，步骤 #6）

Harbor 是 HTTP（`172.16.220.74:8333`），用浏览器访问并登录后，给 `geesun_ai`（自有应用）与 `dockerhub`（第三方通用镜像）两个项目**分别**设保留策略（脚本无法替你点，故手动）：

1. 打开 `http://172.16.220.74:8333` → 登录（Harbor 管理员或项目 Maintainter 账号）。
2. 左侧 **Projects** → 分别点 `geesun_ai` 与 `dockerhub` → 顶部 **Configuration** 标签 → **Tag Retention** 子页（旧版在 **Policies → Tag Retention**）。两个项目各建一套规则。
3. 点 **Add Rule**（或 **NEW RULE**），按两条分别建：
   - **规则 A（自研镜像）**：
     - Matched repositories：`geesun_ai/geesun-agent`
     - Retain：keep most recently pushed **10** tags
     - 额外：勾选 "with labels" 并填 `latest,release-*`（这些标签永久保留，不被清理）
   - **规则 B（第三方镜像）**：
     - Matched repositories：`dockerhub/pgvector`, `dockerhub/caddy`, `dockerhub/loki`, `dockerhub/alloy`, `dockerhub/prometheus`, `dockerhub/grafana`, `dockerhub/phoenix`, `dockerhub/postgres`, `dockerhub/langfuse`, `dockerhub/clickhouse-server`, `dockerhub/minio`, `dockerhub/redis`
     - Retain：keep most recently pushed **3** tags
4. **Save** → 可点 **Simulate Run** 预览哪些 tag 会被删，确认无误后 **Run Now** 立即执行一次，之后按调度周期自动跑（默认每日）。
5. 验证：过一天后回看，确认旧 tag 已被清理、磁盘不再无限增长。

> 若 Harbor 版本 UI 不同（如 2.8+ 把 Retention 放在 **Policies** 下），以控制台实际布局为准；核心是「按 repository 设保留份数 + 保护 release/latest 标签」。

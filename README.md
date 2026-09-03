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

## 各仓 .env 处理（本地开发）

四个仓库的环境变量文件全部**不入库**（各自 `.gitignore` 已忽略，且 `.dockerignore` 保证不进镜像），仓库只保留脱敏模板 `.env.example`。新环境一律 `cp 模板 → 填真实值`。

| 仓库 | 本地文件 | 模板 | 加载机制 | 用途 |
| --- | --- | --- | --- | --- |
| `geesun_agent` | `./.env`（根目录） | `./.env.example` | `src/core/config.py` 的 `Settings`（pydantic-settings，`env_file=[.env]`，大小写不敏感） | 后端本地开发 |
| `geesun_mcp_server` | `./.env` | `./.env.example`（7 变量） | `main.py:14` `load_dotenv()` 自动加载 | MCP 服务本地开发 |
| `geesun_agent_web` | `./.env.local` | `./.env.example`（`NEXT_PUBLIC_API_BASE`） | Next.js 原生（`next dev` 自动读 `.env.local`） | 前端本地开发 |
| `langchain-cubesandbox` | `./.env.test` | `./.env.test.example` | `tests/integration_tests/test_sandbox.py:13-14` 显式 `load_dotenv(".env.test")` | 仅集成测试 |

### 处理步骤

**1. geesun_agent（后端）**

```bash
cd geesun_agent
cp .env.example .env      # 模板字段对应 src/core/config.py 的 Settings（大小写不敏感）
vi .env                   # 填 base_url / openai_api_key / model_name 等
uv sync --frozen
uv run uvicorn src.server:app --host 0.0.0.0 --port 8009
```

> 生产部署用 `deploy/.env.example → deploy/.env`（180 行全量模板，含密钥与镜像 tag），由 compose `env_file:[.env]` 注入容器——与根目录 `.env`（57 行，只管本地 `uv run`）是**两种运行模式**，互不替代。

**2. geesun_mcp_server（MCP 服务，独立仓）**

```bash
cd geesun_mcp_server
cp .env.example .env      # DECRYPT_API_URL / E2B_API_URL / E2B_API_KEY / SSL_CERT_FILE / AGENT_WORKSPACE / UPLOAD_ROOT / REPORT_ROOT
vi .env
uv sync --frozen
uv run python main.py
```

`main.py:14` 的 `load_dotenv()` 自动加载仓库根 `.env`，无需 export；`override=False`，shell 已 export 的同名变量以 shell 为准（容器部署用 env 注入，本地用 `.env`）。

**3. geesun_agent_web（前端，独立仓）**

```bash
cd geesun_agent_web
cp .env.example .env.local   # NEXT_PUBLIC_API_BASE，本地默认 http://localhost:8009
bun install
bun run dev
```

> 生产部署**不走 `.env.local`**：`NEXT_PUBLIC_API_BASE` 是 build-time 变量，由 `deploy/build-push.sh` 以 `--build-arg` 注入镜像，改值必须重新构建（见 `geesun_agent_web/Dockerfile` 头注释）。

**4. langchain-cubesandbox（沙箱库，仅集成测试）**

```bash
cd langchain-cubesandbox
cp .env.test.example .env.test   # CUBE_TEMPLATE_ID(必填) / CUBE_API_URL / CUBE_API_KEY / CUBE_SSL_CERT
vi .env.test
uv run pytest tests/integration_tests/test_sandbox.py
```

### 安全规则（四仓一致）

- 所有 `.env` 文件均已入各自 `.gitignore`（`.env*` 通配），`git add` 前确认不会误提交；
- 模板 `.env.example` / `.env.test.example` 用白名单入库（`!.env.example` / `!.env.test.example`），只含键名与注释，不含真实值；
- 各仓 `.dockerignore` 均已排除 `.env` 与虚拟环境（`**/.venv`、`**/.env.test*`），密钥不会烘焙进镜像；
- 历史版本若误提交过密钥，**轮换密钥**即可（内网 key 轮换成本低，不建议重写 git 历史）。

## Deployment

geesun_agent 生产部署目录位于 `deploy/`，采用 **Docker Swarm stack** 方式发布（与 `flyctrl_deploy` 同源思路，但补齐了 Swarm 必带参数与前置检查）。

对外仅暴露 **Caddy :80**（前端主入口 + `/api/*` 反向代理）；数据库 / Loki / Grafana 等只发布到本机回环或容器内网，不暴露到主机公网。

设计细节（网络/卷前缀、limits 生效、与 `docker compose up` 的差异、可观测栈随栈自托管）见 [`deploy/DEPLOYMENT.md`](./deploy/DEPLOYMENT.md)。

### 本地开发 vs 生产：env 双模式（先读这个再往下）

**生产只有一个配置入口：`deploy/.env`（180 行全量模板），同时服务「构建」与「运行」两个阶段；四个源码仓根目录的 `.env` / `.env.local` / `.env.test` 仅供本地开发，生产不参与**（各自 `.dockerignore` 已全部排除，镜像内不存在）。

```
deploy/.env.example ──cp──> deploy/.env（填全部密钥）
        │
        ├─ 构建阶段：build-push.sh:27-32 `set -a; . "$_DEPLOY_DIR/.env"; set +a` 读入
        │    ├─ agent 镜像：uv sync --frozen 装依赖（构建期无需 env）
        │    └─ web 镜像：--build-arg NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE:-http://10.10.10.67/}
        │       （缺省 10.10.10.67；改前端地址见下方「场景 C」）
        │
        └─ 运行阶段：docker stack deploy 时 compose 原生读取 deploy/.env
             ├─ geesun-agent：env_file:[.env] 全量透传 → config.py Settings 读取
             ├─ geesun-mcp：env_file:[.env] → os.getenv（镜像内 load_dotenv 无 .env 文件 = no-op，兼容）
             └─ geesun-agent-web：运行期无需 env（API 地址已内联进 JS 构建产物）
```

| 易混点 | 本地 | 生产 |
| --- | --- | --- |
| agent 配置 | 根 `.env`（57 行）→ `uv run uvicorn` | `deploy/.env`（180 行）→ compose `env_file:[.env]` 注入容器 |
| web API 地址 | `.env.local` 的 `NEXT_PUBLIC_API_BASE`，`next dev` 每次启动生效 | 构建期 `--build-arg` 内联进 JS 产物，**运行期改 env 无效** |
| mcp / cubesandbox | mcp 根 `.env`；cubesandbox `.env.test` 仅本地集成测试用 | 全部走 `deploy/.env`；cubesandbox 生产不参与 |

> **场景 C（改前端 API 地址）**：`deploy/.env` 加一行 `NEXT_PUBLIC_API_BASE=http://新地址/` → 重跑 `bash build-push.sh`（重建 web 镜像，`WEB_TAG` 需同步递增或覆盖）→ 重新发布。仅改配置（非前端地址）则 `./start_stack.sh --no-build --with=mcp,web` 即可，无需重构建。

### 文件（deploy/）

| 文件 | 说明 |
| --- | --- |
| `docker-compose.yml` | **主栈**：`geesun-agent` + `agent-postgres`（pgvector）+ `caddy` + `loki` + `alloy` + `prometheus` + `grafana`；含 overlay 网络 `appnet2` 与命名卷（端口占用见「端口一览」） |
| `docker-compose.mcp.yml` | 附加：`geesun-mcp`（MCP 服务，默认并入） |
| `docker-compose.web.yml` | 附加：`geesun-agent-web`（Next.js 前端，并入后由 Caddy :80 同域服务） |
| `docker-compose.phoenix.yml` | 附加（`--with=phoenix` 并入）：`phoenix` + `phoenix-db`；Alloy 经服务名 `phoenix:4317` 转发 traces，UI 发布 `6006`。原「复用现网共享实例」方案已废弃——该实例无 restart 策略、停止后 traces 全丢（2026-09-02） |
| `docker-compose.langfuse.yml` | 附加（`--with=langfuse` 并入）：`langfuse-web` / `langfuse-worker` + `clickhouse` / `minio` / `redis` / `postgres`；与 Phoenix 同形态随栈自托管，UI+OTLP 发布 `3000`、minio S3 发布 `9092` |
| `start_stack.sh` | **部署入口**：前置检查（docker/swarm/`.env`）→ 调 `build-push.sh` 打包推送 → `docker stack deploy`（带 `--with-registry-auth --resolve-image=always --prune`） |
| `stop_stack.sh` | 停止并移除 stack（`docker stack rm geesun`，**保留命名卷**） |
| `service_stack.sh` | 查看 stack 服务状态（`docker stack services geesun`） |
| `build-push.sh` | 构建自有镜像（geesun-agent / geesun-mcp-server / geesun-agent-web）并同步第三方镜像进 Harbor |
| `.env.example` | **全部环境变量安全模板**（可入库）；复制为 `.env` 后填密钥 |
| `.env` | 真实连接配置（**不入库**，务必 `chmod 600`） |
| `Caddyfile` / `alloy.config.alloy` / `loki-config.yaml` / `prometheus.yml` / `grafana/` | 各服务运行时配置（bind mount 进容器） |
| `setup-cube-dns.sh` / `setup-combined-ca.sh` / `init-host.sh` | 主机侧辅助：`*.cube.app` DNS 解析、生成 combined-ca.pem（mkcert+系统根 bundle）、主机初始化 |
| `backup.sh` | 数据卷备份 |
| `certs/` | CubeSandbox egress MITM CA（供 agent / mcp 容器信任 sandbox 出网 TLS 拦截）。**`combined-ca.pem` 由 `deploy/setup-combined-ca.sh` 生成**（自动合并你上传的 `rootCA.pem` 单证书 + 系统根 bundle），不要手动 cat；目录不入库 |

> **多文件合并**：主栈默认只含 `docker-compose.yml`；MCP / 前端 / 可观测栈通过 `start_stack.sh --with=mcp,web,phoenix,langfuse` 以 `-c` 叠加，共享 overlay 网络 **`appnet2`**（容器间用服务名互访，stack 前缀 `geesun_`）。
>
> 各附加 compose（mcp / web / phoenix / langfuse）现已统一使用 `appnet2`，可直接 `--with=` 并入，无需再改网络名。

### 端口一览（部署前必读）

端口分三类：**发布到宿主机**（部署前必须确认空闲，否则 `docker stack deploy` 冲突起不来）、**仅 overlay 网络内**（用服务名互访，不占宿主机端口）、**compose 之外的外部依赖**（需网络可达）。下表容器内端口 = 服务在容器里监听的端口，宿主机暴露 = 映射到主机的端口。

#### A. 应用与网关

| 服务（stack 名） | 容器内端口 | 宿主机暴露 | 作用 / 访问方式 |
| --- | --- | --- | --- |
| `geesun_caddy` | 80 | **80 → 80**（ingress，全网卡） | 唯一主入口：`/api/*`、`/docs`、`/openapi.json` → `geesun-agent:8009`；其余 → `geesun-agent-web:3000` |
| `geesun_geesun-agent` | 8009 | 不发布 | FastAPI（`/docs` 健康检查用）；仅经 Caddy 或 appnet2 内访问 |
| `geesun_geesun-mcp` | 8000 | 不发布 | MCP（streamable-http）；agent 经 `.env` 的 `mcp_server_url=http://geesun-mcp:8000/mcp` 访问。**刻意不发布**：现网已有 dev 占用 :8000 |
| `geesun_geesun-agent-web` | 3000 | 不发布 | Next.js 前端；经 Caddy :80 同域（无 CORS）。**必须保持不发布**：:3000 已由本栈 `langfuse-web` 发布（UI+OTLP） |

#### B. 数据库与缓存

| 服务 | 容器内端口 | 宿主机暴露 | 说明 |
| --- | --- | --- | --- |
| `geesun_agent-postgres`（pgvector 0.8.0-pg17） | 5432 | 不发布 | agent 库 `agent_mem`；appnet2 内 `agent-postgres:5432` |
| `geesun_phoenix-db`（`--with=phoenix` 并入） | 5432 | 不发布 | Phoenix 元数据库，卷 `phoenix_pg_data` |
| `postgres`（`--with=langfuse` 并入） | 5432 | 不发布 | Langfuse 后端库；仅 appnet2 内 `postgres:5432`，不占主机端口（旧共享实例的 :5432 冲突已消除） |
| `redis`（`--with=langfuse` 并入） | 6379 | 不发布 | Langfuse BullMQ；仅 appnet2 内，不占主机端口 |
| `clickhouse`（`--with=langfuse` 并入） | 8123（HTTP）/ 9000（native） | 不发布 | Langfuse 事件存储；仅 appnet2 内，不占主机端口 |

#### C. 可观测（日志 / 指标 / 追踪）

| 服务 | 容器内端口 | 宿主机暴露 | 说明 |
| --- | --- | --- | --- |
| `geesun_loki` | 3100 | 不发布 | 仅 Alloy 在 appnet2 内写 `http://loki:3100` |
| `geesun_alloy` | 12345（UI/自监控）· 4317（OTLP gRPC）· 4321（OTLP HTTP） | **12345 → 12345** | agent 追踪入口 `http://alloy:4317`（`.env` 的 `PHOENIX_COLLECTOR_ENDPOINT`）；4317/4321 仅内网不发布 |
| `geesun_prometheus` | 9090 | **19091 → 9090** | 开 `--web.enable-remote-write-receiver` 收 Alloy remote_write；Grafana 数据源走 `prometheus:9090` |
| `geesun_grafana` | 3000 | **3100 → 3000** | 日志检索 UI（admin / `.env` 的 `GRAFANA_PASSWORD`）。⚠️ compose 现为全网卡发布，而 `.env` 注释称 `127.0.0.1:3100`——**两者不一致**；若只允本机访问，把 compose 改成 `127.0.0.1:3100:3000` |
| `geesun_phoenix`（默认 `--with=phoenix` 并入） | 6006（HTTP UI）· 4317（OTLP gRPC）· 9090（metrics，默认未启用） | **6006 → 6006** | UI：`http://10.10.10.67:6006`；Alloy 经服务名 `phoenix:4317` 转发 traces（4317/9090 不发布主机）。**已不依赖现网共享实例**（原 `opt-phoenix-1` 无 restart 策略、2026-09-01 停止后 traces 全丢，2026-09-02 改为栈内自托管） |
| `geesun_langfuse-web`（`--with=langfuse` 并入） | 3000 | **3000 → 3000** | UI + OTLP ingest：`http://10.10.10.67:3000`；agent 经 `.env` 的 `LANGFUSE_BASE_URL` 上报 trace。**已不依赖现网共享实例**（旧共享 Langfuse 须停用释放 :3000，否则端口冲突） |
| `geesun_langfuse-worker`（`--with=langfuse` 并入） | 3030 | 不发布 | 仅容器间消费队列 |

#### D. 对象存储（`--with=langfuse` 并入）

| 服务 | 容器内端口 | 宿主机暴露 | 说明 |
| --- | --- | --- | --- |
| `geesun_minio`（`--with=langfuse` 并入） | 9000（S3 API）· 9001（Console） | **9092 → 9000**（Console :9001 不发布） | 媒体外部端点 `.env` 的 `LANGFUSE_S3_*_EXTERNAL_ENDPOINT=http://10.10.10.67:9092`；旧共享 minio 须停用释放 :9092 |

#### E. compose 之外的外部依赖（需网络可达，不在本 stack 内）

| 依赖 | 地址:端口 | 用途 | 配置位置 |
| --- | --- | --- | --- |
| vLLM（OpenAI 兼容） | `172.16.66.13:8003` | 模型推理 `/v1/*` | `.env` 的 `base_url` |
| CubeSandbox cube-api（E2B 控制面） | `172.16.66.13:6000` | 沙箱 create / connect / kill | `.env` 的 `E2B_API_URL`、`cube_api_url` |
| LDAP / AD | `192.168.1.241:389` | 登录认证 | `.env` 的 `ldap_server` |
| Harbor 镜像仓库（HTTP） | `172.16.220.74:8333` | 拉取镜像，需配 insecure-registries | `.env` 的 `REGISTRY_GEESUN` / `REGISTRY_HUB` |
| dnsmasq（`*.cube.app` 直答） | 本机 `127.0.0.1:53` + `172.17.0.1:53` | 容器解析沙箱域名 → 172.16.66.13 | `deploy/setup-cube-dns.sh` |
| DLP 解密网关 | `.env` 的 `DECRYPT_API_URL`（占位待替换） | MCP 解密加密文件 | `.env` |

> **8003 vs 6000 别搞混**：两者同在 172.16.66.13，但 8003 是 vLLM（OpenAI `/v1/*`），**6000 才是 CubeSandbox E2B 控制面**。`E2B_API_URL` 误写成 8003 → `E2BSandbox.connect` 打 vLLM 拿 404 → e2b 解析报 `KeyError: 'code'`（2026-09-02 实锤）。验证：`curl http://172.16.66.13:6000/sandboxes` 应返回沙箱列表。

#### F. 部署前端口自检

```bash
# 1) 主栈会占用的宿主机端口（默认 --with=phoenix,langfuse,mcp,web）——除 6006/3000/9092 外必须空闲，
#    6006=Phoenix UI、3000=Langfuse UI+OTLP、9092=Langfuse minio S3（均随 --with= 并入，2026-09-02 起）
for p in 80 3100 6006 12345 19091 3000 9092; do
  printf "%-6s %s\n" "$p" "$(ss -lntupH 2>/dev/null | grep -q ":$p " && echo 'OCCUPIED' || echo 'free')"
done

# 2) Langfuse 后端（postgres/redis/clickhouse/minio-console）仅 appnet2 内、不发布主机端口，
#    无需预检；仅 3000/9092 已并入上方主栈端口列表。⚠️ 启用前须停用旧共享 Langfuse 实例释放 3000/9092。

# 3) 外部依赖连通性
curl -s -o /dev/null -w "vLLM  :%{http_code}\n" http://172.16.66.13:8003/v1/models
curl -s -o /dev/null -w "cube  :%{http_code}\n" http://172.16.66.13:6000/sandboxes
curl -s -o /dev/null -w "harbor:%{http_code}\n" http://172.16.220.74:8333/v2/
```

**现网 10.10.10.67 实测占用快照（2026-09-02，Phoenix 纳入 stack 后）**：`22`(sshd) · `53`(dnsmasq) · `80`(caddy) · `323`(chronyd) · `631`(cupsd) · `2377`/`7946`/`4789`(swarm) · `3000`(共享 Langfuse) · `3030`(langfuse-worker, 127.0.0.1) · `3100`(grafana) · `5432`(opt-db + langfuse-postgres) · `6006`(**Phoenix UI，栈内**) · `6379`(共享 redis, 127.0.0.1) · `8123`/`9000`(共享 clickhouse, 127.0.0.1) · `9091`/`9092`(共享 minio) · `12345`(alloy) · `19091`(prometheus)。

> ⚠️ **由此得出的部署结论**：统一命令 `./start_stack.sh --with=phoenix,langfuse,mcp,web` 会发布主机 80/3100/6006/12345/19091/**3000/9092**；其余 Langfuse 后端（5432/6379/8123/9000）仅 appnet2 内、不占主机。**迁移前提**：启用 Langfuse 自托管前，必须先停用现网旧共享 Langfuse 实例（释放 3000/9092，否则端口冲突起不来）；Phoenix 与 Langfuse 现已均随栈自托管。

### 部署步骤

#### 0. 前置（目标机 / 构建机一次性）

- 目标机加入 Swarm（单节点）：`docker swarm init`
- Harbor（`172.16.220.74:8333`）为 HTTP，需加入 Docker「不安全仓库」：
  - Linux：`/etc/docker/daemon.json` 增加 `{"insecure-registries":["172.16.220.74:8333"]}` 后 `systemctl restart docker`
  - Docker Desktop：Settings → Docker Engine → 加入上述 JSON → Apply & Restart
- 目标机登录 Harbor（私有仓库拉取镜像必需）：`docker login 172.16.220.74:8333`
- 预建 agent 工作目录（防容器重建丢数据）：`mkdir -p /opt/geesun/data/{agent,uploads,reports}`（路径对应 `.env` 的 `AGENT_DATA_ROOT`）
- **生成 CA 合并 bundle（为什么不是直接用上传的 `rootCA.pem`，见下方说明）**：
  ```bash
  # 把上传的 rootCA.pem 放到仓库 certs/（或任一路径），然后在宿主机执行：
  bash deploy/setup-combined-ca.sh          # 输出 certs/combined-ca.pem（mkcert+系统根，约 148 证书/227KB）
  # 生产机如需自定义路径：CA_INPUT=/opt/x/rootCA.pem CA_OUTPUT=/opt/x/combined-ca.pem bash deploy/setup-combined-ca.sh
  ```
  > **为什么 .env.example 里挂的是 `combined-ca.pem` 而不是你上传的 `rootCA.pem`**：`rootCA.pem` 只是 CubeSandbox egress MITM 的 mkcert 单证书（约 1.7KB）。容器内 `SSL_CERT_FILE` 指向它后，Python/pip/uv 的默认信任库被**整体替换**成这一个 CA → 系统根被全部排除 → agent 启动 `uv sync` 拉 pypi.org（GlobalSign 签发）报 `UnknownIssuer` 直接崩。`combined-ca.pem` = mkcert CA（信任内网拦截证书）+ 系统根（信任外网 pypi 等），两者缺一不可（2026-09-01 实测反证）。
- **配置 `*.cube.app` 域名解析（sandbox 运行时访问依赖）**：e2b SDK 用 `<port>-<sandbox_id>.cube.app` 域名连沙箱，容器内必须解析到 `172.16.66.13`（cube-egress 所在裸金属）：
  ```bash
  sudo bash deploy/setup-cube-dns.sh   # 宿主机 root 执行一次：dnsmasq address= 直答 + daemon.json dns 指向 dnsmasq
  # 验证：docker run --rm busybox nslookup xxx.cube.app 应返回 172.16.66.13
  ```
  > 必须 `address=` 本地直答模式；不要用 `server=/cube.app/<IP>` 转发——172.16.66.13:53 无 DNS 服务，转发必 NXDOMAIN（2026-09-02 实测修正）。
- Harbor 建好 `geesun_ai` 项目（**生产镜像统一仓库**：自有应用与第三方镜像同仓，`REGISTRY_GEESUN` / `REGISTRY_HUB` 同值，2026-09-02 起）；`dockerhub` proxy 项目可选，仅作工作站手动拉 `library/*` 官方镜像的加速通道

#### 1. 构建并推送镜像（在有源码的构建机执行）

```bash
cd deploy
# 构建上下文需含 geesun_agent / geesun_mcp_server / geesun_agent_web / langchain-cubesandbox（同级目录）
HARBOR_USER=xxx HARBOR_PASSWORD=yyy bash build-push.sh
```

脚本会把 `geesun-agent` / `geesun-mcp-server` / `geesun-agent-web` 推送至 `geesun_ai`（`REGISTRY_GEESUN`），并把 pgvector / caddy / loki / alloy / prometheus / grafana / phoenix / langfuse / clickhouse / minio / redis / postgres 等同步进 `REGISTRY_HUB` 所指项目——**生产单仓：与 `geesun_ai` 同值**（第三方镜像不再进 `dockerhub` proxy 项目）。

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
./start_stack.sh --with=phoenix,langfuse  # 可观测栈随栈自托管（与 agent/mcp/web 统一启动）
STACK_NAME=geesun ./start_stack.sh         # 显式指定 stack 名（默认 geesun）
```

按场景选命令（参数/变量语义、影响范围与推演详见 [`deploy/DEPLOYMENT.md`](./deploy/DEPLOYMENT.md) §1.6）：

| 场景 | 操作 | 影响范围 |
| --- | --- | --- |
| **首次部署**（从零拉起） | 目标机先 `docker swarm init` + `docker login <Harbor>`；构建机 `./build-push.sh` 全量打包推送；目标机 `./start_stack.sh`（默认只发主栈；要带子栈就补全 `--with=...`） | 全新拉起 |
| **加挂子栈**（首次引入 mcp/web/phoenix/langfuse） | `./start_stack.sh --with=<完整复刻>`——`--with` 每次必须与上次完全一致，漏项会触发 `--prune` 清掉对应服务 | 新增服务 |
| **只改了 `.env`**（如 langfuse PK/SK、DB 密码） | `./start_stack.sh --no-build --with=<与上次完全一致>`（几十秒完成）。`.env` 是 deploy 那一刻固化的快照，`docker service update --force` 不会重读 `.env`，重跑 deploy 是唯一生效入口 | 仅引用了变更 env 的服务滚动重启，其余零中断 |
| **改了源码**（agent/mcp/web 任一） | 回构建机 `./build-push.sh`（**先递增 `*_TAG`**，否则同 tag 拉到的还是旧镜像）→ 目标机 `.env` 改对应 tag → `./start_stack.sh --no-build --with=<复刻>` | 单服务滚动重启 |
| **停掉某服务**（如不用 langfuse） | 从 compose 集合移除：`./start_stack.sh --no-build --with=<剩余项>`——`--prune` 删该服务 task；命名卷/数据保留，将来要恢复把该项传回 `--with` 重新拉起即可 | 对应服务被清理 |

> **改源码（场景④）实操**：三仓 ↔ `*_TAG` 变量映射、「构建机/目标机 `.env` 必须同值」提醒与五步流程见 [`DEPLOYMENT.md`](./deploy/DEPLOYMENT.md) §1.6.2 场景④ 展开；只想热更单个服务、不重跑 `start_stack.sh` 时用 `docker service update --image <镜像:新tag> geesun_<服务名>`——**必须显式 `--image`，只 `--force` 换不掉代码**（Swarm 固化 digest），详见 DEPLOYMENT §1.6.4。

- `--with-registry-auth`：依赖本机已 `docker login` Harbor，否则私有镜像拉取失败
- `--resolve-image=always`：固定 tag 重新发布时强制拉新镜像
- `--prune`：compose 中删掉的服务会被真正清理（**只删服务，命名卷保留**）
- `STACK_NAME` 默认 `geesun`，**一旦定了别改**——改名等于另起一套 stack，旧服务全部孤立

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

#### 应用可调优 / 覆盖项（geesun-agent 高级）

容器经 `env_file:[.env]` 透传整个 `.env`，`config.py` 的 `Settings` 大小写不敏感匹配字段名；以下项 Docker 部署此前默认走代码值，生产需覆盖时在此填。完整清单与默认值见 `deploy/.env.example` 同节注释。

| 变量 | 说明 |
| --- | --- |
| `extra_models` | 预装额外模型列表（默认 `[]`，`config.py:11`） |
| `model_max_tokens` | 单次模型输出上限（默认 `65536`，`config.py:99`） |
| `model_call_timeout_sec` | 模型调用墙钟兜底超时（默认 `1800`，`config.py:86`） |
| `model_max_len` | vLLM 上下文上限（默认 `262144`，`config.py:107`） |
| `model_max_tokens_margin` | 动态 `max_tokens` 边距（默认 `16384`，`config.py:110`） |
| `summarization_trigger_tokens` | 上下文压缩触发阈值（默认 `20000`，`config.py:114`） |
| `cube_template_id` / `cube_api_url` / `cube_api_key` | CubeSandbox 沙箱模板/代理地址/Key（默认空，走代码默认值，`config.py:35-37`） |
| `ldap_server` / `ldap_base_dn` / `ldap_bind_user` / `ldap_bind_password` / `ldap_domain_format` / `ldap_admin_group_dn` | AD/LDAP 登录（默认空则关闭 LDAP 登录，`config.py:138-143`） |
| `jwt_secret` / `jwt_algorithm` / `jwt_expire_hours` | 登录 JWT 密钥/算法/有效期（默认 `HS256` / `168` 小时；生产务必覆盖 `jwt_secret`，`config.py:146-148`） |
| `sandbox_*`（十余项） | 沙箱护栏：`sandbox_disk_warn_mb`/`sandbox_disk_hard_mb`（磁盘阈值）、`sandbox_idle_timeout_sec`（空闲回收）、`sandbox_completion_gate_*`（完成门）、`no_progress_*`（无进展熔断）、`sandbox_probe_commands`/`sandbox_probe_ttl_sec`（探针）；默认见 `config.py:41-78` |

#### MCP（geesun-mcp）

| 变量 | 说明 |
| --- | --- |
| `mcp_server_url` | agent 经服务名访问 MCP 的地址，默认 `http://geesun-mcp:8000/mcp`（覆盖 `config.py` 的 localhost 默认值） |
| `MCP_TAG` | MCP 镜像 tag（Harbor `geesun_ai` 项目） |
| `DECRYPT_API_URL` | DLP 解密网关地址（compose 外，MCP 内部 httpx POST 加密文件） |
| `E2B_API_URL` | CubeSandbox(E2B) 代理地址（实测与 vLLM 同主机同端口，按路径区分服务，非笔误） |
| `E2B_API_KEY` | CubeSandbox API Key |
| `SSL_CERT_FILE` | 容器内信任的 CubeSandbox egress CA 路径（必须 `combined-ca.pem` bundle，不能只挂 rootCA.pem 单证书——会排除系统根致 pypi UnknownIssuer；无 MITM 可删） |
| `CA_MOUNT_SRC` | CA 证书在宿主机的路径（相对 `deploy/`，bind mount 进容器；文件由 `setup-combined-ca.sh` 生成） |

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
| `PHOENIX_OTLP_ENDPOINT` | Alloy → Phoenix traces 转发目标，栈内 `phoenix:4317`（仅 appnet2 内，不发布主机） |
| `LANGFUSE_BASE_URL` | Langfuse 地址，随栈自托管为 `http://10.10.10.67:3000`（langfuse-web 已发布 :3000） |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 你的 Langfuse 项目 API Keys（回填后重启 agent 生效） |
| `OTEL_PROJECT_NAME` | OpenTelemetry 项目名（默认 `Geesun-Agent-prod`） |
| `CORS_ALLOW_ORIGINS` | 前端跨域源（Caddy 同域一般无跨域；按实际域名/IP 调整） |

#### 镜像仓库（Harbor，HTTP）

| 变量 | 说明 |
| --- | --- |
| `REGISTRY_GEESUN` | 自有应用仓库，`172.16.220.74:8333/geesun_ai` |
| `REGISTRY_HUB` | 第三方镜像仓库——**生产单仓：与 `REGISTRY_GEESUN` 同值 `geesun_ai`**（第三方镜像也放 geesun_ai；变量名保留，将来拆分只改此值） |
| `GEESUN_AGENT_TAG` | geesun-agent 镜像 tag（默认 `1.0.0`） |
| `ALLOY_TAG` | Alloy 镜像 tag（默认 `v1.19.2`） |
| `PROMETHEUS_TAG` | Prometheus 镜像 tag（默认 `3.0`，需 2.48+ 支持 remote-write-receiver） |
| `WEB_TAG` | 前端镜像 tag（默认 `1.0.0`；`NEXT_PUBLIC_API_BASE` 为 build-time 注入，运行时改无效） |
| `LANGFUSE_TAG` | Langfuse 镜像 tag（pin `3.224.3`；4.0.0 暂无可拉取镜像） |
| `POSTGRES_VERSION` | pgvector 基础 Postgres 大版本（默认 `17`） |

#### Phoenix（随 `--with=phoenix` 并入时填写）

| 变量 | 说明 |
| --- | --- |
| `PHOENIX_DB_USER` / `PHOENIX_DB_PASSWORD` / `PHOENIX_DB_NAME` | Phoenix 库凭据 |
| `PHOENIX_SQL_DATABASE_URL` | 由上面三项拼出（phoenix 与 phoenix-db 共用） |

#### Langfuse（随 `--with=langfuse` 并入时填写）

| 变量 | 说明 |
| --- | --- |
| `NEXTAUTH_URL` / `NEXTAUTH_SECRET` | Web 回调地址与会话密钥（`NEXTAUTH_SECRET` 须 `openssl rand -hex 32`） |
| `SALT` / `ENCRYPTION_KEY` | 加解密盐/密钥（各 `openssl rand -hex 16` / `hex 32`） |
| `DATABASE_URL` | 主库连接串（密码须与 `POSTGRES_PASSWORD` 一致） |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Langfuse 后端 postgres 自身凭据 |
| `CLICKHOUSE_*` | Clickhouse 连接（url / user / password / cluster） |
| `REDIS_*` | Redis 连接（host / port / auth） |
| `LANGFUSE_BULLMQ_SKIP_REDIS_VERSION_CHECK` | 跳过 BullMQ 对 Redis 版本校验（默认 `false`；内网 Redis 版本非官方检测范围时设 `true`） |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | MinIO 根凭据；**官方要求三个 `LANGFUSE_S3_*_SECRET_ACCESS_KEY` 必须等于它** |
| `LANGFUSE_S3_*` | 事件/媒体/批量导出三个桶的 S3 配置（endpoint 默认 `http://minio:9000`） |
| `LANGFUSE_USE_AZURE_BLOB` | S3 事件桶是否走 Azure Blob（默认 `false`；MinIO 自托管保持 `false`） |
| `LANGFUSE_INIT_*` | 首次启动初始化组织/项目/用户（留空则 UI 手动创建） |
| `TELEMETRY_ENABLED` | Langfuse 匿名遥测开关（默认 `false`，生产建议关） |
| `LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES` | 启用实验特性（默认 `false`） |

#### Grafana

| 变量 | 说明 |
| --- | --- |
| `GRAFANA_PASSWORD` | Grafana admin 密码（发布到本机 `127.0.0.1:3100`） |

> 完整变量清单与未列出项的取舍见 `.env.example` 内联注释与 [`deploy/DEPLOYMENT.md`](./deploy/DEPLOYMENT.md)。

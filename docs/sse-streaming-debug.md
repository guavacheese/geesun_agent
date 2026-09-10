# SSE 流式失效诊断手册

> 沉淀日期：2026-09-10
> 触发案例：geesun_agent_web 前端"后端逐字 yield、前端一次性渲染"（commit `1948e61` / `6748c3e`）
> 适用范围：任何 `后端流式 → 代理 → 浏览器` 的 SSE / LLM 流式对话链路

---

## 0. 一句话结论

**先隔离层，再改代码。** 流式链路有 4 个可能失效的层，改错层等于白改。

```
①浏览器/前端消费  →  ②前端框架 dev 代理  →  ③反向代理  →  ④后端生成
   getReader/batch      Next.js rewrites      nginx/Caddy     yield/flush
```

后端 log 逐字但前端一次性 ⇒ **④ 无罪**，问题在 ①~③。
反过来，`curl -N` 直连后端也一次性 ⇒ 问题才在 ④。

---

## 1. 症状归因表

| 观察到的现象 | 指向的层 |
|---|---|
| 后端 log 逐字 yield，前端一次性 | ②③ 代理层（**最高频**） |
| `curl -N` 直连后端分段到达 | ④ 无罪，锁定 ②③ |
| `curl -N` 直连后端也一次性 | ④ 后端：yield 被缓存 / gzip / 生成器先 gather 再 yield |
| **工具调用、reasoning、token 全部一次性** | 数据流根本没到浏览器 → 代理层 |
| 只有正文一次性、工具调用正常 | ① 渲染层（markdown 全量重解析阻塞主线程） |
| 流式正常但"思考完才出正文" | 非本手册范围：查 reasoning/content 分流逻辑 |

> 最后一行来自实战：2026-09-10 用户报告"所有结果，包括工具调用都是最后一次性"——
> 这个补充信息直接排除了渲染层，把范围压缩到代理层。

---

## 2. 三步 curl 隔离法（决定性证据）

```bash
# ① 直连后端
curl -N -X POST http://127.0.0.1:8009/api/v1/chat \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"user_id":"u","session_id":"s","message":"写500字"}'

# ② 经前端 dev server 代理
curl -N -X POST http://localhost:3000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"user_id":"u","session_id":"s","message":"写500字"}'

# ③ 前端绕过代理直连（改代码或浏览器 console）
# fetch('http://127.0.0.1:8009/api/v1/chat', ...)
```

**判读矩阵**：

| ① | ② | 结论 |
|---|---|---|
| 分段 | 一次性 | **② 代理层问题**（框架 dev proxy） |
| 分段 | 分段 | 但浏览器仍一次性 → **① 前端消费层** |
| 一次性 | 一次性 | **④ 后端** |

> ⚠️ **`-N` 不可省**。缺了它 curl 自己会缓冲输出，会得到**假阴性**（明明流式却看起来一次性）。

---

## 3. 按层定位与修复

### ② 前端框架 dev 代理（Next.js 尤甚）

**症状**：`next.config.ts` 里有 `rewrites()` 且 source 覆盖 SSE 端点路径。

**归因精度要求**（避免过度推断，这是被用户纠正过的点）：

- ❌ 不要说"node-http-proxy 默认把整个 response buffer 完再发"——它本身支持流式 pipe，这说法过头
- ❌ 不要说"小响应走 fast path、大响应走 buffer path"——无官方依据
- ✅ 准确表述：**Next.js rewrites 的代理链路不是为 SSE/长连接设计的透明 streaming proxy，
  不同版本、dev server、运行模式都可能让 streaming 行为异常**（next.js #49239、#51879 等回归）

**修复**（前端单侧，后端零改动）：只让 SSE 绕开代理，普通 REST 仍走 rewrites（保留零 CORS）。

`geesun_agent_web/lib/sse-client.ts`：

```ts
export function resolveChatUrl(base?: string): string {
  // 生产判据必须前置！见第 5 节"生产回归陷阱"
  if (process.env.NODE_ENV === "production") {
    return "/api/v1/chat";              // 同域 Caddy 反代
  }
  const apiBase = base !== undefined ? base : process.env.NEXT_PUBLIC_API_BASE;
  const normalized = (apiBase || "http://localhost:8009")
    .replace(/\/+$/, "").replace(/\/v1$/, "");
  return `${normalized}/api/v1/chat`;   // 开发态直连后端，绕开 rewrites
}
```

**配套检查**：
- CORS 白名单是否含前端 origin（后端 `src/core/config.py:48` 的 `cors_allow_origins`）
- 认证方式：`Authorization: Bearer`（JWT/localStorage）→ 不需 `credentials: 'include'`；
  cookie 方案则需 `credentials: 'include'` + `allow_credentials=True` + 白名单回显 origin
- **`NEXT_PUBLIC_*` 是启动时内联的 → 改完必须重启 dev server**

**替代修复**（不推荐）：`app/api/v1/chat/route.ts` Route Handler 用 `fetch` + `ReadableStream`
主动 pipe 后端响应。比绕开代理多一层代码，收益不明显。

### ③ 反向代理

| 代理 | 默认行为 | 修复 |
|---|---|---|
| **Caddy v2** | `reverse_proxy` **默认流式** | 无需配置（推荐让 SSE 走 Caddy） |
| **nginx / openresty** | `proxy_buffering on`（默认缓冲） | `proxy_buffering off;` 或后端发 `X-Accel-Buffering: no` |
| **Apache** | 需 `flushpackets=on` | 按模块配 |

> ⚠️ **`X-Accel-Buffering: no` 只对 nginx/openresty 有效**，控制不了 Next.js 的 Node proxy。
> 后端加了这个 header 仍不流式 → 说明问题不在 nginx 层，别在这浪费时间。
> （`src/api/endpoints/chat.py:1441` 保留该 header 是对的，对生产 Caddy 链路无害。）

### ④ 后端生成

- **gzip/压缩中间件**：挂了 `GZipMiddleware` 会压缩缓冲 SSE 响应。
  （geesun_agent 当前无 gzip 中间件，`src/server.py:121` 只有 CORSMiddleware，已排除。）
- **yield 缓存**：生成器内先 `await gather(...)` 收全部再 yield = 非流式。
- **心跳保活**：长思考（LLM 不吐 token 超 30~60s）会被代理 `proxy_read_timeout` 掐断。
  正确写法——**`asyncio.wait` 竞速，绝不用 `asyncio.wait_for`**：

```python
# ✅ token 先到立即下发（sleep 被取消，零延迟）；满 interval 无字节才发心跳
async def _astream_with_heartbeat(stream, interval=15.0):
    it = stream.__aiter__()
    while True:
        nxt = asyncio.ensure_future(it.__anext__())
        done, _ = await asyncio.wait({nxt, asyncio.ensure_future(asyncio.sleep(interval))},
                                     return_when=asyncio.FIRST_COMPLETED)
        if nxt in done:
            try:
                yield await nxt
            except StopAsyncIteration:
                return
        else:
            yield ("__heartbeat__", None)   # 消费方转成 SSE 注释行 ": ping\n\n"
```

> ❌ **绝不能用 `asyncio.wait_for` 包 `stream.__anext__()`** —— 超时 cancel 会破坏
> 异步生成器状态。（`src/api/endpoints/chat.py` 的 `_astream_with_heartbeat` 是正确实现。）

### ① 前端消费层

- `fetch` + `res.body.getReader()` 逐段读（正确）；`await res.text()` / `res.json()` = 非流式（错误）
- 每个 SSE 事件应**即时** `setState`，不能 debounce/throttle/batch 整轮
- **markdown 渲染阻塞**：每次 token 全量重解析历史消息 → 主线程爆炸 → 视觉上"不逐字"。
  修法：给 Markdown 渲染组件包 `memo`，流式期降级纯文本（见 commit `9e6c47d`）

---

## 4. 推荐架构（生产已正确，勿回退）

```
开发                                生产
Browser                             Browser
 ├─ 页面  → Next.js :3000            └─ Caddy
 └─ SSE  → 后端   :8009                ├─ /api/* → geesun-agent:8009   ← SSE 直连
                                       └─ /*     → Next.js :3000
```

**永远不要让 Next.js rewrites 承担 SSE 代理。**

生产配置见 `deploy/Caddyfile` 的 `handle /api/*` —— 已直连后端，Caddy v2 `reverse_proxy`
默认流式（不像 nginx 需关 `proxy_buffering`）。

**参考实现佐证**（AI streaming 项目均直连后端，不把 Next.js 当 SSE 代理）：
- deepseek-harness：Vite + React 直连后端
- deer-flow：Vite + React 直连后端

---

## 5. ⚠️ 生产回归陷阱（本次差点踩中，务必记住）

**场景**：`geesun_agent_web/Dockerfile` 原第 17 行是

```dockerfile
ARG NEXT_PUBLIC_API_BASE=http://localhost:8009    # ← 危险的默认值
```

而 `deploy/build-push.sh:132-140` 的**生产路径在未设该变量时不传 `--build-arg`**
（注释原文："生产**无需注入此变量**，交给 build 默认 localhost 兜底"）。

**为什么改造前无害**：旧客户端代码**硬编码**相对路径 `/api/v1/chat`，该变量只被
`next.config.ts` 的 `rewrites()` 使用，而 rewrites **仅 dev 生效**。

**为什么改造后会炸**：一旦客户端代码也读该变量（`resolveChatUrl`），生产构建时
Dockerfile 默认值 `http://localhost:8009` 会被 Next.js **内联进浏览器产物** →
浏览器访问 `agent.geesun.com` 时 fetch `http://localhost:8009/api/v1/chat` →
**直连用户自己机器的 8009** → 全量请求失败。

**双重修复**：
1. `lib/sse-client.ts`：**生产判据前置**（`NODE_ENV === "production"` 直接短路返回相对路径，
   完全不读 `NEXT_PUBLIC_API_BASE`）
2. `Dockerfile`：`ARG NEXT_PUBLIC_API_BASE=`（默认值改空，消除误导；
   `next.config.ts` 的 rewrites 自带 `|| "http://localhost:8009"` 兜底，dev 不受影响）

**教训**：给"仅构建期/仅服务端使用"的环境变量新增**客户端读取方**时，必须回头审查
该变量在**生产构建链**里的实际取值——不能假设它只在开发态存在。

---

## 6. 回归防护

修完必须写 spike，**从源码提取真实函数体而非复刻**（复刻版发现不了源码被改坏）。

`geesun_agent_web/scripts/spike-sse-url.cjs` 的做法：

```js
const src = fs.readFileSync("lib/sse-client.ts", "utf8");
const start = src.indexOf("export function resolveChatUrl");
// 大括号配对找函数结尾 → 剥离 TS 类型标注 → eval → 跑用例表
```

用例表带 `NODE_ENV` 列以覆盖 dev/prod 双分支。**必须包含的回归红线用例**：

| 用例 | 期望 |
|---|---|
| **生产 + base=`http://localhost:8009`（Dockerfile 默认值）** | `/api/v1/chat`（回归红线） |
| 生产 + base=生产域名 | `/api/v1/chat`（无视误注入） |
| 生产 + base=undefined | `/api/v1/chat` |
| 开发 + base=`http://localhost:8009` | `http://localhost:8009/api/v1/chat` |
| 开发 + base 已含 `/v1` | 防双 `/v1` |
| 开发 + base 缺失 | 兜底 `http://localhost:8009/api/v1/chat` |

跑法：`node scripts/spike-sse-url.cjs`（当前 13 例全 PASS）

---

## 7. 附：本次案例的完整证据链

| 层 | 检查项 | 结论 |
|---|---|---|
| ④ 后端 | `chat.py` 的 `event_stream()` / `_drain_astream()` / `_astream_with_heartbeat()` | 逐事件 yield 无缓存 ✓ 无罪 |
| ④ 后端 | gzip 中间件 | `src/server.py:121` 只有 CORS，无 gzip ✓ 无罪 |
| ① 前端 | `sse-client.ts` 的 `getReader()` 逐行解析 | 无 batch ✓ 无罪 |
| ① 前端 | `ChatArea.tsx` 每事件即时 `setMessages` | 无节流 ✓ 无罪 |
| ③ 代理 | `X-Accel-Buffering: no`（`chat.py:1441`） | 对 Next.js proxy 无效，保留无害 |
| ② 代理 | `next.config.ts` 的 `rewrites()` 全量代理 `/api/*` | **← 根因** |

**修复 commit**：
- `1948e61` — SSE 直连后端绕开 Next.js dev rewrites
- `6748c3e` — resolveChatUrl 开发态兜底 + spike（11 例）
- `d4f34d8` — **生产回归修复**：生产判据前置 + 运行时 hostname 兜底 + Dockerfile
  ARG 默认值改空；spike 扩到 18 例（含不可删的回归红线）
- `451bb24` — 本文档
- `29f65fc` — （相关）ReasoningChatOpenAI 补非流式钩子，见
  `llm-reasoning-field-passthrough.md`

"""FastAPI 应用入口 — 日志配置由 src/core/logging.py 统一管理。"""

from src.core.logging import *  # noqa: F401,F403 — 日志最早就绪

# ──────────────────────────────────────────────
# Arize Phoenix tracing 初始化（必须在任何 LangChain 导入之前）
# ──────────────────────────────────────────────
# setup_tracing() 必须在 from .api.router import api_router 之前执行，
# 因为 router → endpoints → services/agent 会触发 deepagents + langchain 的导入。
# OpenInference 的 auto_instrument 需要在模块加载时 patch 进去。
from src.core.tracing import setup_tracing

setup_tracing()
# ──────────────────────────────────────────────

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .api.router import api_router
from .infra.database import _build_dsn, ReconnectingAsyncPostgresStore, ReconnectingAsyncPostgresSaver
from .infra import trash


# ──────────────────────────────────────────────
# 禁用 langgraph 1.0.x 的调试 print（必须在 router 导入之后执行——此时 langgraph 已加载）
# 现象：langgraph 把每个 step 的完整 state 用 print() 打到 stdout（日志里满屏
# [values]/[updates] 快照、无时间戳前缀）。大会话时一次 print 几百 KB，stdout
# 为非阻塞管道时抛 BlockingIOError 导致 SSE 流崩溃（2026-08-11 实测 913cfdb2
# 会话 174 条消息在 astream 内 _output 的 print 处崩）。
# 方案：patch langgraph.pregel.main._output，把 print_mode 参数置空——
# yield 行为完全不变，仅跳过 print(完整 state)。
# ──────────────────────────────────────────────
def _disable_langgraph_print() -> None:
    import logging  # 局部导入：本函数在模块底部 import logging 之前执行

    try:
        import langgraph.pregel.main as _lg_main

        _ORIG_OUTPUT = _lg_main._output

        def _silenced_output(stream_mode, print_mode, *args, **kwargs):
            # print_mode 置空：保留原 yield 逻辑，跳过调试 print
            yield from _ORIG_OUTPUT(stream_mode, (), *args, **kwargs)

        _lg_main._output = _silenced_output
        logging.warning("[DIAG] langgraph 调试 print 已禁用（print_mode 置空）")
    except Exception as e:
        logging.warning("禁用 langgraph print 失败（不影响运行）: %s", e)


_disable_langgraph_print()


from src.core.config import settings
import asyncio
import os
import logging


def _load_skills() -> list[str]:
    """启动时一次性加载所有技能目录，避免每次请求扫描磁盘。"""
    skills_root = f"{settings.agent_workspace}/skills"
    logging.warning(f"[DIAG] _load_skills: skills_root={skills_root}")
    if not os.path.isdir(skills_root):
        logging.warning(f"[DIAG] _load_skills: DIR NOT FOUND: {skills_root}")
        return []
    dirs = os.listdir(skills_root)
    logging.warning(f"[DIAG] _load_skills: dirs={dirs}")
    found = [f"{skills_root}/{d}" for d in dirs if os.path.isdir(f"{skills_root}/{d}")]
    logging.warning(f"[DIAG] _load_skills: found skills={found}")
    return found


async def _trash_sweep_loop(interval_seconds: int) -> None:
    """周期清扫 <root>/.trash 下的超期条目（lifespan 启动，退出时 cancel）。

    - 走 asyncio.to_thread：rmtree 是阻塞 IO，大目录删除不能卡事件循环。
    - 异常只告警不抛出：一次清扫失败不该杀掉循环（下一轮自然重试）。
    - TTL 判定在 trash.sweep_trash 内部用目录名时间戳完成，与 mtime 无关。
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            for root in (settings.report_root, settings.upload_root):
                res = await asyncio.to_thread(
                    trash.sweep_trash, root, settings.trash_ttl_seconds
                )
                if res["removed"]:
                    logging.warning("[trash] 周期清扫 %s: %s", root, res)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logging.warning("[trash] 周期清扫异常（下轮重试）: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = _build_dsn()

    # 预加载 skills（启动时设置系统 + Agent 自创路径，用户路径在 chat.py 中按请求追加）
    app.state.skills = ["/skills/__system__/", "/skills/__agent__/"]
    logging.warning(f"[DIAG] lifespan: skills loaded = {app.state.skills}")

    # 使用自动重连的 store 包装器替换原始 AsyncPostgresStore
    # 普通 async with 生命周期无法在连接断开后重建，包装器在
    # aget/aput 抛出 psycopg.OperationalError 时自动重连并重试
    store = ReconnectingAsyncPostgresStore(dsn)
    await store.setup()

    # 种子 AGENTS.md 到 store（供 memory= 参数通过 StoreBackend 读取）
    # 总是覆盖：AGENTS.md 是系统级规则文件（用户不直接改 store），保证最新版本生效
    agents_md_key = "/AGENTS.md"
    with open("AGENTS.md", "r", encoding="utf-8") as f:
        content = f.read()
    stored = await store.aget(("__agent__",), agents_md_key)
    if stored is None or stored.value.get("content") != content:
        await store.aput(
            ("__agent__",),
            agents_md_key,
            {"content": content, "encoding": "utf-8"},
        )
        logging.warning("[DIAG] AGENTS.md seeded to store (updated=%s)", stored is not None)

    app.state.store = store

    checkpointer = ReconnectingAsyncPostgresSaver(dsn)
    await checkpointer.setup()
    app.state.checkpointer = checkpointer

    from src.core.mcp import get_mcp_tools

    await get_mcp_tools()

    # ─── .trash 清扫（2026-09-14）───
    # 删除会话失败的路径已把文件还原（trash.move_back），正常不残留；这里兜底
    # 两种残留：① 事务已提交但 remove_trash 失败；② 进程在提交后、清理前崩溃。
    # 残留只是不可见的 <root>/.trash/<sid>_<ms> 目录，无数据风险。
    # 启动即清一次历史残留（阻塞 IO 走 to_thread，别卡事件循环）：
    for _root in (settings.report_root, settings.upload_root):
        try:
            _swept = await asyncio.to_thread(
                trash.sweep_trash, _root, settings.trash_ttl_seconds
            )
            if _swept["scanned"]:
                logging.warning("[trash] 启动清扫 %s: %s", _root, _swept)
        except Exception as e:  # noqa: BLE001 — 清扫失败不该阻止服务启动
            logging.warning("[trash] 启动清扫失败 %s: %s", _root, e)
    _trash_sweeper = asyncio.create_task(
        _trash_sweep_loop(settings.trash_sweep_interval_seconds)
    )

    yield  # ← 服务运行期间停在这里

    # 服务退出时手动关闭连接池
    _trash_sweeper.cancel()
    await store.aclose()
    await checkpointer.aclose()
    logging.warning("Store + Checkpointer 连接池已关闭，服务退出完成")


app = FastAPI(title="Geesun Agent", lifespan=lifespan)

# CORS：允许的前端源由 settings.cors_allow_origins 控制（逗号分隔，支持生产域名）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """容器 healthcheck 专用探活端点。

    为什么单独建这个端点（2026-09-10 之前 healthcheck 探的是 /docs）：
    /docs 是 FastAPI 的 Swagger UI 路由，会被 FastAPIInstrumentor 建 HTTP server
    span —— 而 healthcheck 每 15s 探一次 → 每天 5760 次 × 3 span = 17,280 span
    灌进 Phoenix/Langfuse。实测 Phoenix spans 表 108,209 条里 HTTP 形态占 98.3%、
    其中 `GET /docs` 占 91.8%，真正有价值的 LLM trace 只剩 1.1%。

    /healthz 同时列在 src/core/tracing.py 的 _HTTP_EXCLUDED_URLS 中
    （excluded_urls 在 ASGI middleware 入口直接 return），双保险：既不建 span，
    也不计入 http_server_* 指标。

    刻意不查数据库：原 /docs 探活同样不查库，此处保持等价语义 —— 探活的职责是
    "进程是否活着"，DB 依赖由服务启动时的 lifespan 负责（连不上就起不来、由
    restart_policy 兜底）。
    """
    return {"status": "ok"}

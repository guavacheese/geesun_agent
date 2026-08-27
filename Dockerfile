# 构建上下文必须为 /d/workspace（与 langchain-cubesandbox 同级），
# 以便 pyproject 中的 editable 源 "../langchain-cubesandbox" 在镜像内解析为 /langchain-cubesandbox。
# 镜像统一推送到 Harbor（http://172.16.220.74:8333/geesun_ai），由 deploy/build-push.sh 完成：
#   bash deploy/build-push.sh            # 构建并 push 到 172.16.220.74:8333/geesun_ai/geesun-agent:<tag>
FROM ghcr.io/astral-sh/uv:python3.13-bookworm

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# 本地 editable 依赖必须位于 /app 的 ../langchain-cubesandbox
COPY langchain-cubesandbox /langchain-cubesandbox

# 先装依赖（利用层缓存），再拷源码
COPY geesun_agent/pyproject.toml geesun_agent/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY geesun_agent /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 非 root 运行
RUN useradd --create-home --uid 1001 appuser \
    && chown -R appuser:appuser /app /langchain-cubesandbox
USER appuser

EXPOSE 8009

# 日志由 Docker json-file 驱动轮转 + Alloy 采集（logs→Loki），此处只打 stdout（JSON）
# --log-config 强制 uvicorn / uvicorn.access 也走统一 JSON 格式器（见 logging.uvicorn.json）
CMD ["uv", "run", "uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8009", "--log-config", "/app/logging.uvicorn.json"]

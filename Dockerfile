# 构建上下文必须为 /d/workspace（与 langchain-cubesandbox 同级），
# 以便 pyproject 中的 editable 源 "../langchain-cubesandbox" 在镜像内解析为 /langchain-cubesandbox。
# 镜像统一推送到 Harbor（http://172.16.220.74:8333/geesun_ai），由 deploy/build-push.sh 完成：
#   bash deploy/build-push.sh            # 构建并 push 到 172.16.220.74:8333/geesun_ai/geesun-agent:<tag>
FROM ghcr.io/astral-sh/uv:python3.13-bookworm

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_CACHE_DIR=/tmp/uv-cache

WORKDIR /app

# 非 root 运行。用户先建，后续 COPY 一律用 --chown 直接落盘为 1001，
# 不再用 `chown -R` 收尾：那次递归会让 BuildKit 为整棵树（17651 文件 / 396MB）
# 生成一份 354MB 的重复层，实测单层 1149.5s，占整次构建 98.5%（2026-10-17 实测）。
RUN useradd --create-home --uid 1001 appuser \
    && chown appuser:appuser /app

# 本地 editable 依赖必须位于 /app 的 ../langchain-cubesandbox
COPY --chown=appuser:appuser langchain-cubesandbox /langchain-cubesandbox

# 先装依赖（利用层缓存），再拷源码
COPY --chown=appuser:appuser geesun_agent/pyproject.toml geesun_agent/uv.lock ./
# 以 appuser 身份装包：产物天然属于 1001，无需事后 chown
USER appuser
RUN --mount=type=cache,target=/tmp/uv-cache,uid=1001 \
    uv sync --frozen --no-dev --no-install-project

COPY --chown=appuser:appuser geesun_agent /app
RUN --mount=type=cache,target=/tmp/uv-cache,uid=1001 \
    uv sync --frozen --no-dev

# BuildKit 会把 cache mount 的挂载点目录以 root:root 写进镜像层（缓存内容本身不落层）。
# 运行时 `uv run` 要在该目录建 CACHEDIR.TAG，appuser 写不进去 ->
# "Failed to initialize cache at /tmp/uv-cache: Permission denied"。
# 故删掉这个空目录，让运行时以 appuser 自行创建。
USER root
RUN rm -rf /tmp/uv-cache
USER appuser

EXPOSE 8009

# 日志由 Docker json-file 驱动轮转 + Alloy 采集（logs→Loki），此处只打 stdout（JSON）
# --log-config 强制 uvicorn / uvicorn.access 也走统一 JSON 格式器（见 logging.uvicorn.json）
CMD ["uv", "run", "uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8009", "--log-config", "/app/logging.uvicorn.json"]

"""统一日志模块 — 纯 stdlib，零项目依赖。

作为整个项目最早的导入模块，在任何业务代码执行前完成日志配置。
用法：在任意入口文件或模块的第一行导入：
    from src.core.logging import *

生产 / 容器化约定：
- 日志统一写 stdout/stderr（12-factor），Docker json-file 驱动负责大小轮转，
  Loki/Alloy 负责集中留存与检索（logs→Loki，metrics→Prometheus，traces→Phoenix）；应用内部不写轮转文件。
- 通过环境变量控制：
    LOG_LEVEL  日志级别，默认 INFO（排查时可设 DEBUG）
    LOG_FORMAT text | json，默认 json（容器 / 集中日志场景推荐 json）
"""

import datetime
import json
import logging
import os
import sys

__all__ = []  # 禁止导出任何名称，只执行 side-effect

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_LOG_FORMAT = os.environ.get("LOG_FORMAT", "json").lower()
_UTC8 = datetime.timezone(datetime.timedelta(hours=8))


class _UvicornNameFilter(logging.Filter):
    """把 uvicorn.error / uvicorn.access 统一显示为 uvicorn。"""

    def filter(self, record):
        if record.name.startswith("uvicorn."):
            record.name = "uvicorn"
        return True


class _UTC8Formatter(logging.Formatter):
    """强制 UTC+8 时区的文本格式器，不受系统时区配置影响。"""

    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created, tz=_UTC8)
        return dt.strftime(datefmt) if datefmt else dt.isoformat()


class _JSONFormatter(logging.Formatter):
    """零依赖 JSON 格式器：一行一事件，便于 Loki / ELK 按字段索引。"""

    def format(self, record):
        dt = datetime.datetime.fromtimestamp(record.created, tz=_UTC8)
        payload = {
            "ts": dt.isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "thread": record.thread,
            "pid": record.process,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# 供 uvicorn --log-config 引用的公有名（dictConfig 需要可导入的 dotted path）
JsonFormatter = _JSONFormatter
UvicornNameFilter = _UvicornNameFilter


def _build_formatter():
    if _LOG_FORMAT == "json":
        return _JSONFormatter()
    return _UTC8Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S CST",
    )


_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(_build_formatter())
_handler.addFilter(_UvicornNameFilter())
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    handlers=[_handler],
)

# 统一 uvicorn 日志格式与级别（否则 uvicorn.access 的 INFO 会被 root WARNING 挡掉）
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _logger = logging.getLogger(_name)
    _logger.handlers.clear()
    _logger.addHandler(_handler)
    _logger.propagate = False
    _logger.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))

# Python warnings 也走日志（如 InsecureKeyLengthWarning）
logging.captureWarnings(True)

"""统一日志模块 — 纯 stdlib，零项目依赖。

作为整个项目最早的导入模块，在任何业务代码执行前完成日志配置。
用法：在任意入口文件或模块的第一行导入：
    from src.core.logging import *
"""

import datetime
import logging

__all__ = []  # 禁止导出任何名称，只执行 side-effect


class _UvicornNameFilter(logging.Filter):
    """把 uvicorn.error / uvicorn.access 统一显示为 uvicorn。"""
    def filter(self, record):
        if record.name.startswith("uvicorn."):
            record.name = "uvicorn"
        return True


class _UTC8Formatter(logging.Formatter):
    """强制 UTC+8 时区的日志格式器，不受系统时区配置影响。"""

    def formatTime(self, record, datefmt=None):
        tz = datetime.timezone(datetime.timedelta(hours=8))
        dt = datetime.datetime.fromtimestamp(record.created, tz=tz)
        return dt.strftime(datefmt) if datefmt else dt.isoformat()


_handler = logging.StreamHandler()
_handler.setFormatter(_UTC8Formatter(
    fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S CST",
))
_handler.addFilter(_UvicornNameFilter())
logging.basicConfig(level=logging.WARNING, handlers=[_handler])

# 统一 uvicorn 日志格式
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _logger = logging.getLogger(_name)
    _logger.handlers.clear()
    _logger.addHandler(_handler)
    _logger.propagate = False

# Python warnings 也走日志（如 InsecureKeyLengthWarning）
logging.captureWarnings(True)

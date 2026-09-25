# -*- coding: utf-8 -*-
"""核心模块统一日志: RotatingFileHandler + tracker_id 请求唯一性标记
- tracker_id 基于 contextvars: 线程/协程隔离, 同一任务链路内所有日志共享同一标记,
  支持从 任务提交→LLM请求→工具执行→交付 的全链路追踪。
- 日志文件: ~/.haisnap/logs/haisnap.log (5MB x 3 轮转), 级别由
  HAISNAP_LOG_LEVEL 控制(默认 INFO), HAISNAP_LOG_CONSOLE=on 可同时输出控制台。
"""
import contextvars
import logging
import os
import uuid
from logging.handlers import RotatingFileHandler
from .config import GLOBAL_DIR

LOG_DIR = os.path.join(GLOBAL_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")

_tracker = contextvars.ContextVar("haisnap_tracker_id", default="-")
_configured = False


def new_tracker(prefix="req"):
    """生成并绑定新的 tracker_id(任务/请求开始时调用), 返回标记值"""
    tid = f"{prefix}-{uuid.uuid4().hex[:12]}"
    _tracker.set(tid)
    return tid


def get_tracker():
    return _tracker.get()


def set_tracker(tid):
    """跨线程传递 tracker_id(如 Web worker 线程继承请求标记)"""
    _tracker.set(tid or "-")


class TrackerFilter(logging.Filter):
    """把当前上下文的 tracker_id 注入每条日志记录"""

    def filter(self, record):
        record.tracker_id = _tracker.get()
        return True


def _configure_root():
    import sys as _sys
    global _configured
    if _configured:
        return
    _configured = True
    root = logging.getLogger("haisnap")
    level = getattr(logging, os.environ.get("HAISNAP_LOG_LEVEL", "INFO").upper(),
                    logging.INFO)
    root.setLevel(level)
    root.propagate = False   # 不向 python root logger 冒泡, 避免污染宿主应用
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | [%(tracker_id)s] | %(message)s")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(TrackerFilter())
        root.addHandler(fh)
    except OSError as e:
        print(f"[haisnap-logger] 日志文件初始化失败(降级为无文件日志): "
              f"{type(e).__name__}: {e}", file=_sys.stderr)
    if os.environ.get("HAISNAP_LOG_CONSOLE", "").lower() in ("on", "1", "true"):
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.addFilter(TrackerFilter())
        root.addHandler(ch)


def get_logger(name="core"):
    """获取带 tracker_id 的命名子日志器: get_logger('agent') -> haisnap.agent"""
    _configure_root()
    return logging.getLogger(f"haisnap.{name}")

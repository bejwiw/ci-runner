# -*- coding: utf-8 -*-
"""
ghbox 统一日志系统

四级输出：控制台 + 内存环形缓冲（可查询）+ 文件（自动轮转）+ 请求日志（异步）
结构化字段：时间 / 级别 / 模块 / JOB_ID
进程日志：持久化进程独立日志文件
资源监控：CPU/内存/磁盘
"""
import os
import time
import threading
import logging
import subprocess
from logging.handlers import RotatingFileHandler

import config

# ==================== 配置 ====================
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
MAX_RING_LINES = int(os.environ.get("LOG_RING_LINES", "5000"))
LOG_FILE = os.path.join(os.path.expanduser("~"), "ghbox.log")
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP = 5
JOB_ID = os.environ.get("GHBOX_JOB_ID", "")

# 内存环形缓冲
_ring = []
_ring_lock = threading.Lock()
_stats = {"error": 0, "warning": 0, "info": 0, "request": 0}
_stats_lock = threading.Lock()

_loggers = {}
_loggers_lock = threading.Lock()


class RingBufferHandler(logging.Handler):
    def emit(self, record):
        try:
            entry = {
                "time": time.time(),
                "level": record.levelname,
                "module": getattr(record, "module", ""),
                "job": getattr(record, "job", JOB_ID),
                "msg": self.format(record),
            }
            with _ring_lock:
                _ring.append(entry)
                if len(_ring) > MAX_RING_LINES:
                    del _ring[:len(_ring) - MAX_RING_LINES]
            with stats_lock:
                if record.levelno >= logging.ERROR:
                    _stats["error"] += 1
                elif record.levelno >= logging.WARNING:
                    _stats["warning"] += 1
                else:
                    _stats["info"] += 1
        except Exception:
            pass


class ContextFilter(logging.Filter):
    def filter(self, record):
        name = getattr(record, "name", "")
        record.module = name.split(".")[-1] if name else ""
        record.job = JOB_ID
        return True


def setup_logger(name="ghbox"):
    """获取/创建统一 logger"""
    with _loggers_lock:
        if name in _loggers:
            return _loggers[name]
        logger = logging.getLogger(name)
        if not logger.handlers:
            logger.setLevel(LOG_LEVEL)
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(module)s: %(message)s",
                "%Y-%m-%d %H:%M:%S")
            logger.addHandler(logging.StreamHandler())
            rb = RingBufferHandler()
            rb.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(rb)
            try:
                fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_FILE_MAX_BYTES,
                                        backupCount=LOG_FILE_BACKUP, encoding="utf-8")
                fh.setFormatter(fmt)
                logger.addHandler(fh)
            except Exception:
                pass
            logger.addFilter(ContextFilter())
        _loggers[name] = logger
        return logger


def get_logs(limit=500, level=None, module=None, keyword=None):
    levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
    min_lv = levels.get(level, 0)
    with _ring_lock:
        entries = list(_ring)
    result = []
    for e in entries:
        if min_lv and levels.get(e.get("level", ""), 20) < min_lv:
            continue
        if module and module.lower() not in e.get("module", "").lower():
            continue
        if keyword and keyword.lower() not in e.get("msg", "").lower():
            continue
        result.append(e)
    return result[-limit:]


def get_stats():
    with stats_lock:
        return dict(_stats)


# ==================== 请求日志（异步） ====================
import queue as _queue
_req_queue = _queue.Queue(maxsize=5000)
_req_started = False


def request_logger(app):
    global _req_started
    from flask import request

    if not _req_started:
        _req_started = True
        threading.Thread(target=_req_writer, daemon=True).start()

    @app.before_request
    def _start():
        request.environ["_req_start"] = time.time()

    @app.after_request
    def _end(response):
        dur = (time.time() - request.environ.get("_req_start", time.time())) * 1000
        try:
            ip = request.headers.get("CF-Connecting-IP", "") or request.remote_addr or ""
            parts = ip.split(".")
            if len(parts) == 4:
                ip = ".".join(parts[:3]) + ".x"
            _req_queue.put_nowait(
                (request.method, request.path, response.status_code, dur, ip))
        except _queue.Full:
            pass
        with stats_lock:
            _stats["request"] += 1
        return response
    return app


def _req_writer():
    lg = setup_logger("api")
    while True:
        try:
            item = _req_queue.get()
            if item is None:
                break
            method, path, status, dur, ip = item
            lg.info("%s %s -> %d (%.0fms) ip=%s", method, path, status, dur, ip)
        except Exception:
            time.sleep(0.1)


# ==================== 进程日志 ====================
def process_logger(name):
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    path = os.path.join(config.LOGS_DIR, f"{name}.log")
    lg = logging.getLogger(f"proc.{name}")
    if lg.handlers:
        return lg, path
    lg.setLevel(logging.DEBUG)
    try:
        fh = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        lg.addHandler(fh)
    except Exception:
        pass
    return lg, path


def read_process_log(name, limit=200):
    path = os.path.join(config.LOGS_DIR, f"{name}.log")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-limit:]
    except Exception:
        return []


# ==================== 资源监控 ====================
def get_resource_stats():
    result = {}
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
        result["mem_total_kb"] = mem.get("MemTotal", 0)
        result["mem_avail_kb"] = mem.get("MemAvailable", mem.get("MemFree", 0))
    except Exception:
        pass
    try:
        r = subprocess.run(["df", "-k", os.path.expanduser("~")],
                           capture_output=True, text=True, timeout=10)
        lines = r.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            result["disk_total_kb"] = int(parts[1])
            result["disk_used_kb"] = int(parts[2])
            result["disk_use_pct"] = float(parts[4].rstrip("%"))
    except Exception:
        pass
    return result

# -*- coding: utf-8 -*-
"""
日志系统测试

测试：setup_logger、get_logs、get_stats、clear_logs、process_logger
"""
import os
import sys
import logging

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import log


def test_setup_logger():
    """setup_logger 返回有效 logger"""
    lg = log.setup_logger("test_mod")
    assert lg is not None
    assert lg.level == logging.INFO


def test_setup_logger_reuse():
    """同名 logger 复用（不重复添加 handler）"""
    lg1 = log.setup_logger("test_reuse")
    lg2 = log.setup_logger("test_reuse")
    assert lg1 is lg2


def test_log_and_get_logs():
    """写日志后能查到"""
    log.clear_logs()
    lg = log.setup_logger("test_logs")
    lg.info("test message 1")
    lg.warning("test warning 1")
    lg.error("test error 1")
    logs = log.get_logs(limit=100)
    assert len(logs) >= 3
    msgs = [e["msg"] for e in logs]
    assert any("test message 1" in m for m in msgs)
    assert any("test warning 1" in m for m in msgs)
    assert any("test error 1" in m for m in msgs)


def test_get_logs_filter_level():
    """按级别过滤日志"""
    log.clear_logs()
    lg = log.setup_logger("test_filter")
    lg.info("info msg")
    lg.error("error msg")
    errors = log.get_logs(limit=100, level="ERROR")
    assert len(errors) >= 1
    assert all(e["level"] == "ERROR" for e in errors)


def test_get_logs_filter_keyword():
    """按关键词过滤日志"""
    log.clear_logs()
    lg = log.setup_logger("test_kw")
    lg.info("hello world")
    lg.info("foo bar")
    results = log.get_logs(limit=100, keyword="hello")
    assert len(results) >= 1
    assert any("hello" in e["msg"] for e in results)


def test_get_stats():
    """统计计数"""
    log.clear_logs()
    lg = log.setup_logger("test_stats")
    lg.info("i1")
    lg.warning("w1")
    lg.error("e1")
    stats = log.get_stats()
    assert stats["info"] >= 1
    assert stats["warning"] >= 1
    assert stats["error"] >= 1


def test_clear_logs():
    """清空日志和统计"""
    lg = log.setup_logger("test_clear")
    lg.info("before clear")
    log.clear_logs()
    stats = log.get_stats()
    assert stats["info"] == 0
    assert stats["error"] == 0
    assert len(log.get_logs()) == 0


def test_process_logger(monkeypatch, tmp_path):
    """进程日志写入和读取"""
    monkeypatch.setattr("log.config.LOGS_DIR", str(tmp_path / "logs"))
    lg, path = log.process_logger("test_proc")
    assert os.path.exists(path)
    lg.info("proc log line")
    monkeypatch.setattr("log.config.LOGS_DIR", str(tmp_path / "logs"))
    lines = log.read_process_log("test_proc", limit=10)
    assert len(lines) >= 1
    assert any("proc log line" in l for l in lines)

# -*- coding: utf-8 -*-
"""
工具函数测试

测试：is_alive、dir_size_mb、run_cmd、_parse_body
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core.utils import is_alive, dir_size_mb, run_cmd, elapsed_since, _parse_body


def test_is_alive_invalid_pid():
    """非法 PID 返回 False"""
    assert is_alive("not-a-number") is False


def test_is_alive_nonexistent():
    """不存在的 PID 返回 False"""
    assert is_alive(999999999) is False


def test_dir_size_nonexistent():
    """不存在的目录返回 0"""
    assert dir_size_mb("/nonexistent/path") == 0.0


def test_run_cmd_echo():
    """执行 echo 命令"""
    code, stdout, stderr = run_cmd("echo hello")
    assert code == 0
    assert "hello" in stdout


def test_run_cmd_fail():
    """执行失败命令"""
    code, stdout, stderr = run_cmd("false")
    assert code != 0


def test_run_cmd_timeout():
    """命令超时"""
    code, stdout, stderr = run_cmd("sleep 10", timeout=1)
    assert code == -1
    assert "timeout" in stderr


def test_parse_body_json():
    """JSON 解析"""
    result = _parse_body(b'{"key": "value"}')
    assert result == {"key": "value"}


def test_parse_body_text():
    """非 JSON 返回字符串"""
    assert _parse_body(b"plain text") == "plain text"


def test_parse_body_empty():
    """空输入返回 None"""
    assert _parse_body(b"") is None
    assert _parse_body(None) is None


def test_elapsed_since():
    """时间差计算"""
    import time
    t0 = time.time() - 5
    elapsed = elapsed_since(t0)
    assert elapsed >= 5

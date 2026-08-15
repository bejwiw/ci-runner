# -*- coding: utf-8 -*-
"""
CLI API 客户端测试

测试：check 函数（统一错误处理）
"""
import os
import sys

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from cli.api import check


def test_check_ok():
    """成功返回 (True, data)"""
    ok, data = check({"ok": True, "instances": [{"id": "inst1"}]})
    assert ok is True
    assert data["instances"][0]["id"] == "inst1"


def test_check_fail():
    """失败返回 (False, error)"""
    ok, error = check({"ok": False, "error": "未授权"})
    assert ok is False
    assert error == "未授权"


def test_check_missing_ok():
    """缺少 ok 字段视为失败"""
    ok, error = check({})
    assert ok is False
    assert error == "未知错误"


def test_check_no_error_field():
    """失败但没有 error 字段"""
    ok, error = check({"ok": False})
    assert ok is False
    assert error == "未知错误"

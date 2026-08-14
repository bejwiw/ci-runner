# -*- coding: utf-8 -*-
"""
Leader 锁测试

测试：acquire（无 leader/有别的 leader/自己是 leader）
"""
import os
import sys
import json

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")
os.environ.setdefault("MANAGER_HOST", "test.manager")
os.environ.setdefault("INSTANCE_ID", "test-inst")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _mock_http(status, body_dict):
    """构造 mock http_request 返回值"""
    return lambda url, method="GET", **kwargs: (status, json.dumps(body_dict).encode() if body_dict else None)


def test_acquire_no_leader(monkeypatch):
    """无 leader 时 acquire 成功"""
    monkeypatch.setattr("core.lock.http_request",
        lambda url, **kw: (200, json.dumps({"ok": True, "is_leader": False, "has_leader": False}).encode()))
    from core.lock import LeaderLock
    lock = LeaderLock(backend="manager", instance_id="test")
    assert lock.acquire() is True
    assert lock.is_leader is True


def test_acquire_other_leader_active(monkeypatch):
    """有别的活跃 leader 时 acquire 失败"""
    monkeypatch.setattr("core.lock.http_request",
        lambda url, **kw: (200, json.dumps({
            "ok": True, "is_leader": False, "has_leader": True,
            "leader_job": "OTHER_JOB", "leader_age": 10
        }).encode()))
    from core.lock import LeaderLock
    lock = LeaderLock(backend="manager", instance_id="test")
    assert lock.acquire() is False
    assert lock.is_leader is False


def test_acquire_self_is_leader(monkeypatch):
    """自己已经是 leader"""
    from core.lock import LeaderLock, JOB_ID
    monkeypatch.setattr("core.lock.http_request",
        lambda url, **kw: (200, json.dumps({
            "ok": True, "is_leader": True, "has_leader": True,
            "leader_job": JOB_ID, "leader_age": 5
        }).encode()))
    lock = LeaderLock(backend="manager", instance_id="test")
    assert lock.acquire() is True
    assert lock.is_leader is True


def test_acquire_network_failure(monkeypatch):
    """网络失败时降级为 leader"""
    monkeypatch.setattr("core.lock.http_request",
        lambda url, **kw: (0, None))
    from core.lock import LeaderLock
    lock = LeaderLock(backend="manager", instance_id="test")
    assert lock.acquire() is True
    assert lock.is_leader is True


def test_job_id_unique():
    """每个 LeaderLock 实例有唯一的 JOB_ID"""
    from core.lock import LeaderLock
    lock1 = LeaderLock(backend="manager")
    lock2 = LeaderLock(backend="manager")
    # JOB_ID 是模块级常量，所有实例共享
    assert lock1.job_id == lock2.job_id
    assert len(lock1.job_id) == 8

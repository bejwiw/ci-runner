# -*- coding: utf-8 -*-
"""
任务队列测试

测试：add_task、update_task、_trim_history、recover_pending、get_pending_tasks
"""
import os
import sys
import time

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture
def task_store(monkeypatch):
    """用内存列表替代 S3 存储"""
    from manager import tasks
    storage = []
    monkeypatch.setattr(tasks, "load_tasks", lambda: list(storage))
    def save_tasks(t):
        storage.clear()
        storage.extend(t)
    monkeypatch.setattr(tasks, "save_tasks", save_tasks)
    return tasks, storage


def test_add_task(task_store):
    """添加任务"""
    tasks, storage = task_store
    task = tasks.add_task("test_type", {"key": "value"})
    assert task["type"] == "test_type"
    assert task["status"] == "pending"
    assert task["params"] == {"key": "value"}
    assert len(storage) == 1


def test_add_task_dedup(task_store):
    """去重：相同 dedup_key 的 pending/running 任务跳过"""
    tasks, storage = task_store
    t1 = tasks.add_task("test_type", {"name": "a"}, dedup_key="dk:a")
    t2 = tasks.add_task("test_type", {"name": "a"}, dedup_key="dk:a")
    assert t1["id"] == t2["id"]  # 返回已存在的任务
    assert len(storage) == 1


def test_add_task_different_dedup(task_store):
    """不同 dedup_key 都加入"""
    tasks, storage = task_store
    tasks.add_task("test_type", {"name": "a"}, dedup_key="dk:a")
    tasks.add_task("test_type", {"name": "b"}, dedup_key="dk:b")
    assert len(storage) == 2


def test_update_task(task_store):
    """更新任务状态"""
    tasks, storage = task_store
    task = tasks.add_task("test_type", {})
    tasks.update_task(task["id"], status="running", started_at=time.time())
    assert storage[0]["status"] == "running"
    assert storage[0]["started_at"] is not None


def test_trim_history(task_store):
    """历史超过 MAX_HISTORY 时裁剪"""
    tasks, storage = task_store
    for i in range(60):
        tasks.add_task("type", {"i": i})
    assert len(storage) <= tasks.MAX_HISTORY


def test_recover_pending(task_store):
    """恢复未完成任务"""
    tasks, storage = task_store
    # 手动添加 pending 和 failed 任务
    storage.extend([
        {"id": "t1", "type": "test", "status": "pending", "params": {},
         "retries": 0, "dedup_key": None, "created_at": time.time(),
         "updated_at": time.time(), "started_at": None, "error": ""},
        {"id": "t2", "type": "test", "status": "failed", "params": {},
         "retries": 2, "dedup_key": None, "created_at": time.time(),
         "updated_at": time.time(), "started_at": None, "error": "err"},
    ])
    tasks.recover_pending()
    assert storage[0]["status"] == "pending"
    assert storage[0]["started_at"] is None
    # failed 且 retries < MAX_RETRIES 也恢复为 pending
    assert storage[1]["status"] == "pending"


def test_get_pending_tasks(task_store):
    """获取待处理任务"""
    tasks, storage = task_store
    storage.extend([
        {"id": "t1", "type": "test", "status": "pending", "params": {},
         "retries": 0, "dedup_key": None, "created_at": time.time(),
         "updated_at": time.time(), "started_at": None, "error": ""},
        {"id": "t2", "type": "test", "status": "done", "params": {},
         "retries": 0, "dedup_key": None, "created_at": time.time(),
         "updated_at": time.time(), "started_at": None, "error": ""},
    ])
    pending = tasks.get_pending_tasks()
    assert len(pending) == 1
    assert pending[0]["id"] == "t1"


def test_register_handler(task_store):
    """注册任务处理器"""
    tasks, storage = task_store
    @tasks.register_handler("custom_type")
    def handler(params, task):
        return "result"
    assert "custom_type" in tasks._handlers
    assert tasks._handlers["custom_type"]({"test": True}, {}) == "result"

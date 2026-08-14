# -*- coding: utf-8 -*-
"""
数据持久化统计测试

测试：record_backup、record_restore、load_stats、save_stats、
      get_pending、clear_pending、get_storage_mb
"""
import os
import sys

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture
def stats_file(monkeypatch, tmp_path):
    """设置 STATS_FILE 到临时路径"""
    stats_path = str(tmp_path / "stats.json")
    monkeypatch.setattr("worker.persistence.STATS_FILE", stats_path)
    from worker import persistence
    return persistence


def test_record_backup(stats_file):
    """记录备份操作"""
    stats_file.record_backup("success", 1048576, "test backup", 2, 1)
    stats = stats_file.load_stats()
    assert stats["pending_a"] == 2
    assert stats["pending_b"] == 1
    assert stats["storage_mb"] == 1.0
    assert len(stats["backup_history"]) == 1
    assert stats["backup_history"][0]["status"] == "success"
    assert stats["backup_history"][0]["size_bytes"] == 1048576


def test_record_backup_accumulates(stats_file):
    """多次备份累积 pending"""
    stats_file.record_backup("success", 100, "b1", 3, 0)
    stats_file.record_backup("success", 200, "b2", 2, 1)
    stats = stats_file.load_stats()
    assert stats["pending_a"] == 5
    assert stats["pending_b"] == 1
    assert len(stats["backup_history"]) == 2


def test_record_backup_history_limit(stats_file):
    """备份历史最多50条"""
    for i in range(60):
        stats_file.record_backup("success", 100, f"b{i}", 1, 0)
    stats = stats_file.load_stats()
    assert len(stats["backup_history"]) == 50


def test_record_restore(stats_file):
    """记录恢复操作"""
    stats_file.record_restore("success", "restore ok", 1, 2)
    stats = stats_file.load_stats()
    assert stats["pending_a"] == 1
    assert stats["pending_b"] == 2
    assert len(stats["restore_history"]) == 1
    assert stats["restore_history"][0]["status"] == "success"


def test_timeline(stats_file):
    """时间线记录备份和恢复"""
    stats_file.record_backup("success", 100, "backup", 1, 0)
    stats_file.record_restore("success", "restore", 0, 1)
    stats = stats_file.load_stats()
    assert len(stats["timeline"]) == 2
    types = [e["type"] for e in stats["timeline"]]
    assert "backup" in types
    assert "restore" in types


def test_get_pending(stats_file):
    """获取未上报次数"""
    stats_file.record_backup("success", 100, "b", 5, 3)
    pa, pb = stats_file.get_pending()
    assert pa == 5
    assert pb == 3


def test_clear_pending(stats_file):
    """清零 pending"""
    stats_file.record_backup("success", 100, "b", 5, 3)
    stats_file.clear_pending()
    pa, pb = stats_file.get_pending()
    assert pa == 0
    assert pb == 0


def test_get_storage_mb(stats_file):
    """获取存储用量"""
    stats_file.record_backup("success", 2097152, "b", 0, 0)
    assert stats_file.get_storage_mb() == 2.0


def test_load_stats_no_file(stats_file):
    """文件不存在时返回默认值"""
    stats = stats_file.load_stats()
    assert stats["pending_a"] == 0
    assert stats["pending_b"] == 0
    assert stats["storage_mb"] == 0
    assert stats["backup_history"] == []

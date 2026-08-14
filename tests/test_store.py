# -*- coding: utf-8 -*-
"""
实例清单管理测试

测试：save_instances merge 逻辑、空数据保护、数量骤减保护
"""
import os
import sys
import json
import time

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


class MockS3Pool:
    """模拟 S3Pool 用于 store 测试"""
    def __init__(self, instances=None):
        self._data = {
            "meta/instances.json": json.dumps(instances or []).encode() if instances else None
        }
    def is_ready(self):
        return True
    def get(self, key):
        return self._data.get(key)
    def put(self, key, data):
        self._data[key] = data
        return True


@pytest.fixture
def setup_store(monkeypatch):
    """设置 store 模块的 S3 池 + mock Releases"""
    from manager import store
    # 清空缓存
    store._cache.clear()
    store._cache_time.clear()
    # mock Releases（避免网络请求）
    monkeypatch.setattr("manager.store.releases.save_json_enc", lambda *a, **kw: True)
    monkeypatch.setattr("manager.store.releases.load_json_enc", lambda *a, **kw: [])
    monkeypatch.setattr("manager.store.releases.save_json_protected", lambda *a, **kw: True)
    return store


def test_merge_preserves_other_instances(monkeypatch, setup_store):
    """merge 保留不在当前列表中的非 closed 实例"""
    store = setup_store
    # S3 中已有 inst1 和 inst2
    s3_instances = [
        {"id": "inst1", "closed": False, "status": "running"},
        {"id": "inst2", "closed": False, "status": "running"},
    ]
    mock_pool = MockS3Pool(s3_instances)
    store.set_s3pool(mock_pool)
    # 当前只有 inst1（模拟并发场景：另一个线程在保存时只拿到了 inst1）
    new_instances = [{"id": "inst1", "closed": False, "status": "running"}]
    result = store.save_instances(new_instances)
    assert result is True
    # inst2 应该被 merge 回来
    assert any(i["id"] == "inst2" for i in new_instances)


def test_merge_skips_closed_instances(monkeypatch, setup_store):
    """merge 跳过已 closed 的实例"""
    store = setup_store
    s3_instances = [
        {"id": "inst1", "closed": False, "status": "running"},
        {"id": "inst2", "closed": True, "status": "closed"},
    ]
    mock_pool = MockS3Pool(s3_instances)
    store.set_s3pool(mock_pool)
    new_instances = [{"id": "inst1", "closed": False, "status": "running"}]
    store.save_instances(new_instances)
    # inst2 是 closed，不应该被 merge
    assert not any(i["id"] == "inst2" for i in new_instances)


def test_save_empty_instances_rejected(setup_store):
    """空列表不允许覆盖"""
    store = setup_store
    store.set_s3pool(MockS3Pool([{"id": "inst1", "closed": False}]))
    result = store.save_instances([])
    assert result is False


def test_quantity_drop_protection(setup_store):
    """数量骤减保护"""
    store = setup_store
    # S3 中有 4 个已关闭实例（不会被 merge 回来）
    s3_instances = [{"id": f"inst{i}", "closed": True} for i in range(4)]
    store.set_s3pool(MockS3Pool(s3_instances))
    store._cache.clear()
    store._cache_time.clear()
    # 当前只有 1 个（< 4/2 = 2），应该被拒绝
    new_instances = [{"id": "inst_new", "closed": False}]
    result = store.save_instances(new_instances)
    assert result is False


def test_get_or_create_instance(setup_store):
    """实例不存在时从配置恢复创建"""
    store = setup_store
    store.set_s3pool(MockS3Pool([]))
    cfg = {"hostname": "inst9.kekeke.cc.cd", "account": "acc1"}
    inst = store.get_or_create_instance("inst9", cfg)
    assert inst["id"] == "inst9"
    assert inst["hostname"] == "inst9.kekeke.cc.cd"
    assert inst["status"] == "running"
    assert inst["closed"] is False


def test_next_inst_id(setup_store):
    """生成下一个实例 ID"""
    store = setup_store
    store.set_s3pool(MockS3Pool([
        {"id": "inst1", "closed": False},
        {"id": "inst3", "closed": False},
    ]))
    # 清空缓存确保从 S3 读
    store._cache.clear()
    store._cache_time.clear()
    next_id = store.next_inst_id()
    assert next_id == "inst4"


def test_close_instance(setup_store):
    """标记实例为已关闭"""
    store = setup_store
    store.set_s3pool(MockS3Pool([{"id": "inst1", "closed": False}]))
    store._cache.clear()
    store._cache_time.clear()
    assert store.close_instance("inst1") is True
    instances = store.load_instances()
    inst = next(i for i in instances if i["id"] == "inst1")
    assert inst["closed"] is True
    assert inst["status"] == "closed"

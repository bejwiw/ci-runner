# -*- coding: utf-8 -*-
"""
实例清单管理测试（纯内存模式）

测试：save/load 内存一致性、空数据拒绝、自愈、更新、关闭、worker stats 纯内存
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
    """模拟 S3Pool"""
    def __init__(self, instances=None, accounts=None):
        self._data = {}
        if instances is not None:
            self._data["meta/instances.json"] = json.dumps(instances).encode()
        if accounts is not None:
            self._data["meta/accounts.json"] = json.dumps(accounts).encode()
    def is_ready(self):
        return True
    def get(self, key):
        return self._data.get(key)
    def put(self, key, data):
        self._data[key] = data
        return True
    def put_meta_json(self, key, obj):
        self._data[key] = json.dumps(obj).encode()
        return True
    def get_meta_json(self, key, default=None):
        raw = self._data.get(key)
        if raw is None:
            return default
        return json.loads(raw.decode())
    def delete(self, key):
        self._data.pop(key, None)


@pytest.fixture
def fresh_store(monkeypatch):
    """每个测试用全新的 store 内存状态"""
    from manager import store
    # 重置内存
    store._instances.clear()
    store._accounts.clear()
    store._tasks.clear()
    store._worker_stats.clear()
    store._instance_configs.clear()
    store._loaded = False
    # mock Releases（避免网络）
    monkeypatch.setattr("manager.store.releases.save_json_enc", lambda *a, **kw: True)
    monkeypatch.setattr("manager.store.releases.load_json_enc", lambda *a, **kw: [])
    monkeypatch.setattr("manager.store.releases.save_json_protected", lambda *a, **kw: True)
    return store


def test_save_and_load_instances(fresh_store):
    """save 写内存，load 读内存"""
    store = fresh_store
    store.set_s3pool(MockS3Pool())
    insts = [{"id": "inst1", "closed": False}, {"id": "inst2", "closed": False}]
    store.save_instances(insts)
    loaded = store.load_instances()
    assert len(loaded) == 2
    assert loaded[0]["id"] == "inst1"


def test_save_empty_rejected(fresh_store):
    """空列表不允许覆盖"""
    store = fresh_store
    store.set_s3pool(MockS3Pool())
    result = store.save_instances([])
    assert result is False


def test_get_or_create_instance(fresh_store):
    """实例不存在时从配置恢复创建"""
    store = fresh_store
    store.set_s3pool(MockS3Pool())
    store._loaded = True  # 跳过 load_all
    cfg = {"hostname": "inst9.kekeke.cc.cd", "account": "acc1"}
    inst = store.get_or_create_instance("inst9", cfg)
    assert inst["id"] == "inst9"
    assert inst["hostname"] == "inst9.kekeke.cc.cd"
    assert inst["status"] == "running"
    assert inst["closed"] is False
    # 内存里有这个实例了
    assert any(i["id"] == "inst9" for i in store._instances)


def test_update_instance(fresh_store):
    """更新实例字段"""
    store = fresh_store
    store.set_s3pool(MockS3Pool())
    store._loaded = True
    store._instances = [{"id": "inst1", "closed": False, "status": "running"}]
    updated = store.update_instance("inst1", status="restarting")
    assert updated["status"] == "restarting"
    assert updated["last_seen"] > 0
    # 内存也更新了
    assert store._instances[0]["status"] == "restarting"


def test_close_instance(fresh_store):
    """关闭实例：从活跃清单移除（存储净化）+ 进墓碑"""
    store = fresh_store
    store.set_s3pool(MockS3Pool())
    store._loaded = True
    store._instances = [{"id": "inst1", "closed": False, "status": "running"}]
    assert store.close_instance("inst1") is True
    # 实例从活跃清单移除
    assert len(store._instances) == 0
    # 进墓碑，拒绝自愈复活
    assert store.is_closed("inst1") is True


def test_closed_ids_persist_and_block_self_heal(fresh_store):
    """墓碑持久化 + 拒绝自愈复活"""
    store = fresh_store
    pool = MockS3Pool()
    store.set_s3pool(pool)
    store._loaded = True
    store._instances = [{"id": "inst1", "closed": False, "status": "running"},
                        {"id": "inst2", "closed": False, "status": "running"}]
    assert store.close_instance("inst1") is True
    # 墓碑应该被持久化到S3
    assert pool.get("meta/closed_ids.json") is not None
    closed = pool.get("meta/closed_ids.json")
    assert "inst1" in closed.decode()

    # 模拟manager重启：重新加载
    store._loaded = False
    store._instances = []
    store._closed_ids.clear()
    store.load_all()
    # 墓碑应该被恢复
    assert store.is_closed("inst1") is True

    # get_or_create_instance 应该拒绝已关闭的实例
    result = store.get_or_create_instance("inst1", {"hostname": "inst1.kekeke.cc.cd"})
    assert result is None

    # 正常实例不受影响
    result = store.get_or_create_instance("inst2", {"hostname": "inst2.kekeke.cc.cd"})
    assert result is not None


def test_next_inst_id(fresh_store):
    """生成下一个实例 ID"""
    store = fresh_store
    store.set_s3pool(MockS3Pool([{"id": "inst1", "closed": False},
                                  {"id": "inst3", "closed": False}]))
    store._loaded = True
    store._instances = [{"id": "inst1"}, {"id": "inst3"}]
    assert store.next_inst_id() == "inst4"


def test_worker_stats_pure_memory(fresh_store):
    """worker stats 纯内存，不读 S3"""
    store = fresh_store
    store._loaded = True
    stats = {"a_count_total": 100, "b_count_total": 50, "storage_mb": 10.5}
    store.save_worker_stats("inst1", stats)
    loaded = store.get_worker_stats("inst1")
    assert loaded["a_count_total"] == 100
    assert loaded["storage_mb"] == 10.5
    # 不存在的实例返回默认值
    default = store.get_worker_stats("nonexistent")
    assert default["a_count_total"] == 0


def test_instance_config_cached(fresh_store):
    """实例配置读一次后缓存内存"""
    store = fresh_store
    mock = MockS3Pool()
    store.set_s3pool(mock)
    store._loaded = True
    # 第一次读从 S3
    mock._data["meta/inst-config/inst1.json"] = json.dumps({"inst_id": "inst1", "hostname": "inst1.test"}).encode()
    cfg = store.load_instance_config("inst1")
    assert cfg["inst_id"] == "inst1"
    # 删掉 S3 数据，再读应该从内存缓存
    mock._data.clear()
    cfg2 = store.load_instance_config("inst1")
    assert cfg2["inst_id"] == "inst1"


def test_load_all_from_s3(fresh_store):
    """load_all 从 S3 加载"""
    store = fresh_store
    store.set_s3pool(MockS3Pool(
        instances=[{"id": "inst1", "closed": False}],
        accounts=[{"name": "acc1", "token": "xxx"}]
    ))
    store.load_all()
    assert len(store._instances) == 1
    assert store._instances[0]["id"] == "inst1"
    assert len(store._accounts) == 1
    assert store._accounts[0]["name"] == "acc1"
    assert store._loaded is True

# -*- coding: utf-8 -*-
"""
purge_instance_data 清理逻辑测试

验证关闭实例时能彻底清理 S3 + Releases 数据：
- 单文件
- 分片（从manifest获取分片列表）
- manifest 本身
"""
import os
import sys
import json

os.environ.setdefault("EXEC_TOKEN", "test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


class FakeS3Pool:
    """模拟 S3Pool，记录删除的key"""
    def __init__(self, stored=None):
        self.stored = stored or {}  # key -> bytes
        self.deleted = []

    def is_ready(self):
        return True

    def get(self, key):
        return self.stored.get(key)

    def delete(self, key):
        self.deleted.append(key)
        self.stored.pop(key, None)
        return True


@pytest.fixture
def setup_store(monkeypatch):
    """设置 store 模块的 _s3pool 和 releases"""
    import manager.store as store
    deleted_assets = []
    download_calls = []

    def fake_delete_asset(name, token=None, repo=None):
        deleted_assets.append(name)
        return True

    def fake_download_asset(name, token=None, repo=None):
        download_calls.append(name)
        # 返回 None 表示没有 manifest（默认单文件场景）
        return None

    monkeypatch.setattr("core.releases.delete_asset", fake_delete_asset)
    monkeypatch.setattr("core.releases.download_asset", fake_download_asset)
    monkeypatch.setattr("manager.store.releases.delete_asset", fake_delete_asset)
    monkeypatch.setattr("manager.store.releases.download_asset", fake_download_asset)

    return store, deleted_assets, download_calls


def test_purge_single_files(setup_store):
    """清理单文件版本（db / files / proc）"""
    store, deleted_assets, _ = setup_store
    fake_pool = FakeS3Pool(stored={
        "inst-data/inst1/db": b"data",
        "inst-files/inst1/files.tar.gz": b"files",
        "inst-proc/inst1/proc.tar.gz": b"proc",
    })
    import manager.store as store_mod
    store_mod._s3pool = fake_pool

    store.purge_instance_data("inst1")

    # S3 单文件应该被删除
    assert "inst-data/inst1/db" in fake_pool.deleted
    assert "inst-files/inst1/files.tar.gz" in fake_pool.deleted
    assert "inst-proc/inst1/proc.tar.gz" in fake_pool.deleted
    # Releases 资产应该被删除
    assert "inst-inst1.db.enc" in deleted_assets
    assert "inst-inst1.files.tar.gz.enc" in deleted_assets
    assert "inst-inst1.processes.tar.gz.enc" in deleted_assets
    assert "inst-inst1.json.enc" in deleted_assets


def test_purge_chunked_files_from_manifest(setup_store):
    """从manifest获取分片列表，逐个删除分片"""
    store, deleted_assets, _ = setup_store
    manifest = json.dumps({
        "chunks": 5,
        "chunk_size": 10485760,
        "total_size": 52428800,
        "locations": [
            {"chunk": 0, "account": 1},
            {"chunk": 1, "account": 2},
            {"chunk": 2, "account": 3},
            {"chunk": 3, "account": 4},
            {"chunk": 4, "account": 5},
        ],
    }).encode()
    fake_pool = FakeS3Pool(stored={
        "inst-files/inst1/files.tar.gz.manifest": manifest,
        "inst-files/inst1/files.tar.gz.chunk0": b"0",
        "inst-files/inst1/files.tar.gz.chunk1": b"1",
        "inst-files/inst1/files.tar.gz.chunk2": b"2",
        "inst-files/inst1/files.tar.gz.chunk3": b"3",
        "inst-files/inst1/files.tar.gz.chunk4": b"4",
    })
    import manager.store as store_mod
    store_mod._s3pool = fake_pool

    store.purge_instance_data("inst1")

    # 每个分片都应该被删除
    for i in range(5):
        assert f"inst-files/inst1/files.tar.gz.chunk{i}" in fake_pool.deleted
    # manifest 本身应该被删除
    assert "inst-files/inst1/files.tar.gz.manifest" in fake_pool.deleted


def test_purge_chunked_fallback_num_chunks(setup_store):
    """manifest locations 不完整时，用 num_chunks 兜底删除"""
    store, deleted_assets, _ = setup_store
    manifest = json.dumps({
        "chunks": 8,
        "chunk_size": 10485760,
        "total_size": 83886080,
        "locations": [  # 只记录了3个位置，实际有8个分片
            {"chunk": 0, "account": 1},
            {"chunk": 1, "account": 2},
            {"chunk": 2, "account": 3},
        ],
    }).encode()
    fake_pool = FakeS3Pool(stored={
        "inst-files/inst1/files.tar.gz.manifest": manifest,
    })
    import manager.store as store_mod
    store_mod._s3pool = fake_pool

    store.purge_instance_data("inst1")

    # 应该尝试删除 0-7 共8个分片（locations 3个 + num_chunks 兜底）
    for i in range(8):
        assert f"inst-files/inst1/files.tar.gz.chunk{i}" in fake_pool.deleted


def test_purge_no_s3pool(setup_store):
    """没有 s3pool 时，只清理 Releases，不报错"""
    store, deleted_assets, _ = setup_store
    import manager.store as store_mod
    store_mod._s3pool = None

    # 不应该抛异常
    store.purge_instance_data("inst1")

    # Releases 资产应该被删除
    assert "inst-inst1.db.enc" in deleted_assets


def test_purge_releases_chunked(setup_store):
    """Releases 分片也清理（从 manifest 获取分片数）"""
    store, deleted_assets, download_calls = setup_store

    # 让 download_asset 返回分片 manifest
    manifest = json.dumps({"parts": 6}).encode()

    def fake_download(name, token=None, repo=None):
        if name.endswith(".manifest"):
            return manifest
        return None

    import manager.store as store_mod
    store_mod._s3pool = None
    store_mod.releases.download_asset = fake_download

    store.purge_instance_data("inst1")

    # 分片 part0-part5 应该被删除
    for i in range(6):
        assert f"inst-inst1.files.tar.gz.enc.part{i}" in deleted_assets
    # manifest 本身应该被删除
    assert "inst-inst1.files.tar.gz.enc.manifest" in deleted_assets

# -*- coding: utf-8 -*-
"""backup_files 分支测试（v2 异步架构）

覆盖：
- <50MB / >=50MB 两个大小分支
- S3 成功 → Releases 异步入队
- S3 失败 → Releases 同步兜底成功/失败
"""
import os
import sys

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


class FakeS3Pool:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def is_ready(self):
        return True

    def put_file(self, key, path):
        self.calls.append(("put_file", key))
        return self.ok

    def put(self, key, data):
        self.calls.append(("put", key))
        return self.ok


class FakeQueue:
    def __init__(self, accept=True):
        self.accept = accept
        self.tasks = []

    def enqueue(self, kind, base, path=None, data=None, inst_id=None, repo=None, token=None):
        self.tasks.append({"kind": kind, "base": base, "path": path, "inst_id": inst_id})
        return self.accept


@pytest.fixture
def setup_backup(monkeypatch, tmp_path):
    """mock 备份依赖"""
    import worker.persistence as persistence
    import worker.upload_queue as uq

    fake_tmp = str(tmp_path / "backup.tar.gz")
    with open(fake_tmp, "wb") as f:
        f.write(b"x" * 1024)

    monkeypatch.setattr(persistence, "backup_files_to_disk", lambda: fake_tmp)
    monkeypatch.setattr(persistence, "_s3pool", FakeS3Pool())

    # mock 队列
    fake_q = FakeQueue()
    monkeypatch.setattr(uq, "QUEUE_DIR", str(tmp_path / ".queue"))
    monkeypatch.setattr(uq.ReleasesUploadQueue, "enqueue", FakeQueue.enqueue)
    monkeypatch.setattr(persistence, "upload_queue", uq)
    monkeypatch.setattr(uq, "get_queue", lambda: fake_q)
    return persistence, fake_q, fake_tmp


def test_backup_files_small_file(setup_backup):
    """S3 成功 → 异步入队，返回 (size, 1)"""
    persistence, fake_q, _ = setup_backup
    result = persistence.backup_files(None)
    assert result is not None
    size, parts = result
    assert size == 1024
    assert parts == 1
    assert len(fake_q.tasks) == 1
    assert fake_q.tasks[0]["kind"] == "files"


def test_backup_files_large_file(setup_backup, monkeypatch, tmp_path):
    """>=50MB: S3 成功 → 同样异步入队（不内存读）"""
    import worker.persistence as persistence
    import worker.upload_queue as uq

    fake_tmp = str(tmp_path / "large.tar.gz")
    with open(fake_tmp, "wb") as f:
        f.write(b"x" * (50 * 1024 * 1024 + 1024))

    monkeypatch.setattr(persistence, "backup_files_to_disk", lambda: fake_tmp)
    monkeypatch.setattr(persistence, "_s3pool", FakeS3Pool())

    fake_q = FakeQueue()
    monkeypatch.setattr(uq, "get_queue", lambda: fake_q)
    monkeypatch.setattr(persistence, "upload_queue", uq)

    result = persistence.backup_files(None)
    assert result is not None
    size, parts = result
    assert size == 50 * 1024 * 1024 + 1024
    assert parts == 1
    assert len(fake_q.tasks) == 1


def test_backup_files_s3_fail_sync_fallback_ok(setup_backup, monkeypatch, tmp_path):
    """S3 失败 → Releases 同步兜底成功 → (size, 1)"""
    import worker.persistence as persistence
    monkeypatch.setattr(persistence, "_s3pool", FakeS3Pool(ok=False))

    fake_tmp = setup_backup[2]
    calls = {}

    def fake_upload_v2(base, data_or_path, token=None, repo=None, ts=None):
        calls["base"] = base
        return {"ok": True, "status": 201, "name": base, "size": 1}

    monkeypatch.setattr(persistence.releases, "upload_asset_v2", fake_upload_v2)

    result = persistence.backup_files(None)
    assert result is not None
    size, parts = result
    assert parts == 1
    assert "files" in calls.get("base", "")


def test_backup_files_s3_fail_sync_fallback_fail(setup_backup, monkeypatch):
    """S3 失败 + Releases 兜底也失败 → (size, 0)"""
    import worker.persistence as persistence
    monkeypatch.setattr(persistence, "_s3pool", FakeS3Pool(ok=False))

    def fake_upload_v2(base, data_or_path, token=None, repo=None, ts=None):
        return {"ok": False, "status": 500}

    monkeypatch.setattr(persistence.releases, "upload_asset_v2", fake_upload_v2)

    result = persistence.backup_files(None)
    assert result is not None
    size, parts = result
    assert parts == 0


def test_backup_database_s3_ok_async(setup_backup, monkeypatch, tmp_path):
    """数据库 S3 成功 → 异步入队"""
    import worker.persistence as persistence
    import worker.upload_queue as uq
    import config as gconfig

    # 假 db 文件
    db = str(tmp_path / "demo.db")
    with open(db, "wb") as f:
        f.write(b"dbdata" * 100)
    old_db = gconfig.DB_FILE
    monkeypatch.setattr(gconfig, "DB_FILE", db)
    monkeypatch.setattr(persistence.config, "DB_FILE", db)

    fake_q = FakeQueue()
    monkeypatch.setattr(uq, "get_queue", lambda: fake_q)
    monkeypatch.setattr(persistence, "upload_queue", uq)
    monkeypatch.setattr(persistence, "_s3pool", FakeS3Pool(ok=True))

    result = persistence.backup_database(None)
    assert result is not None
    assert len(fake_q.tasks) == 1
    assert fake_q.tasks[0]["kind"] == "db"
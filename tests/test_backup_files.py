# -*- coding: utf-8 -*-
"""backup_files 分支测试

覆盖 <50MB（走Releases双写）和 >=50MB（跳过Releases）两个分支，
防止 parts 未定义等回归。
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


@pytest.fixture
def setup_backup(monkeypatch, tmp_path):
    """mock 备份依赖"""
    import worker.persistence as persistence

    # 假文件
    fake_tmp = str(tmp_path / "backup.tar.gz")
    with open(fake_tmp, "wb") as f:
        f.write(b"x" * 1024)

    monkeypatch.setattr(persistence, "backup_files_to_disk", lambda: fake_tmp)
    monkeypatch.setattr(persistence, "_s3pool", FakeS3Pool())

    upload_calls = {}

    def fake_upload_chunked(name, data, token=None, repo=None):
        upload_calls["name"] = name
        return len(data), 1  # (size, ok_assets)

    monkeypatch.setattr(persistence.releases, "upload_chunked", fake_upload_chunked)
    return persistence, upload_calls


def test_backup_files_small_file(setup_backup):
    """<50MB: 走 Releases 双写分支，不报错，返回 (size, parts)"""
    persistence, upload_calls = setup_backup
    result = persistence.backup_files(None)
    assert result is not None
    size, parts = result
    assert size == 1024
    assert parts == 1
    assert upload_calls.get("name") == persistence.config.ASSET_FILES


def test_backup_files_large_file(setup_backup, monkeypatch, tmp_path):
    """>=50MB: 跳过 Releases，返回 (size, 1)，不报错"""
    import worker.persistence as persistence

    # 构造 50MB+ 假文件
    fake_tmp = str(tmp_path / "large.tar.gz")
    with open(fake_tmp, "wb") as f:
        f.write(b"x" * (50 * 1024 * 1024 + 1024))

    monkeypatch.setattr(persistence, "backup_files_to_disk", lambda: fake_tmp)
    monkeypatch.setattr(persistence, "_s3pool", FakeS3Pool())

    failed = {"called": False}

    def fake_upload_chunked(name, data, token=None, repo=None):
        failed["called"] = True  # 不应被调用
        return len(data), 1

    monkeypatch.setattr(persistence.releases, "upload_chunked", fake_upload_chunked)

    result = persistence.backup_files(None)
    assert result is not None
    size, parts = result
    assert size == 50 * 1024 * 1024 + 1024
    assert parts == 1
    assert failed["called"] is False  # 大文件不调 Releases


def test_backup_files_s3_fail_still_returns(setup_backup, monkeypatch):
    """S3 失败但仍返回结果（不抛异常）"""
    import worker.persistence as persistence
    monkeypatch.setattr(persistence, "_s3pool", FakeS3Pool(ok=False))
    result = persistence.backup_files(None)
    assert result is not None
    size, parts = result
    assert parts == 1
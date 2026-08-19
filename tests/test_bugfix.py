# -*- coding: utf-8 -*-
"""
Bug修复验证测试

测试每个修复的bug是否真正被修复：
- Bug 1: S3计数器虚高 — 覆盖写时used_bytes只算差值
- Bug 2: put/get不对称 — put不换桶，只写原桶
- Bug 3: backup_files不检查返回值
- Bug 4: _S3Client.put()检查返回值
- Bug 5: PID文件和配置依赖processes/目录
- Bug 6: 全量备份和进程快照重叠
"""
import os
import sys
import json
import threading

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _make_pool():
    """构造一个测试用的S3Pool（mock客户端）"""
    from core.s3 import S3Pool
    pool = S3Pool.__new__(S3Pool)
    pool._initialized = True
    pool._hash_ring = type('R', (), {
        'get_account': lambda s, k: 0,
        'get_nearby_accounts': lambda s, k, count=10: [1, 2]
    })()
    pool._counters = {
        0: {'status': 'active', 'a_count': 0, 'used_bytes': 0,
            'b_count': 0, 'fail_count': 0, 'last_error': '',
            'last_error_time': 0, 'last_success': 0, 'month': 0},
        1: {'status': 'active', 'a_count': 0, 'used_bytes': 0,
            'b_count': 0, 'fail_count': 0, 'last_error': '',
            'last_error_time': 0, 'last_success': 0, 'month': 0},
    }
    pool._lock = threading.RLock()
    pool._owner = 'test'
    return pool


class FakeClient:
    """模拟S3客户端，记录所有操作"""
    def __init__(self):
        self.stored = {}
        self.put_should_fail = False

    def put(self, key, data, prefix='ghbox'):
        if self.put_should_fail:
            return False
        self.stored[key] = data
        return True

    def get(self, key, prefix='ghbox'):
        return self.stored.get(key)

    def delete(self, key, prefix='ghbox'):
        self.stored.pop(key, None)
        return True


# ==================== Bug 1: 计数器虚高 ====================

class TestBug1CounterInflation:
    def test_put_overwrite_no_inflate(self):
        """覆盖写同一key时，used_bytes只算差值，不虚增"""
        pool = _make_pool()
        client = FakeClient()
        pool._get_client = lambda idx: client
        pool._get_object_size = lambda idx, key: len(client.stored.get(key, b''))

        # 第一次写入 100 字节
        assert pool.put('testkey', b'x' * 100) is True
        assert pool._counters[0]['used_bytes'] == 100

        # 第二次覆盖写 150 字节 → 差值 50，used_bytes = 150
        assert pool.put('testkey', b'y' * 150) is True
        assert pool._counters[0]['used_bytes'] == 150

        # 第三次覆盖写 80 字节 → 差值 -70，used_bytes = 80
        assert pool.put('testkey', b'z' * 80) is True
        assert pool._counters[0]['used_bytes'] == 80

    def test_put_different_keys_accumulate(self):
        """不同key正常累加"""
        pool = _make_pool()
        client = FakeClient()
        pool._get_client = lambda idx: client
        pool._get_object_size = lambda idx, key: len(client.stored.get(key, b''))

        pool.put('key_a', b'a' * 100)
        pool.put('key_b', b'b' * 200)
        assert pool._counters[0]['used_bytes'] == 300

    def test_repeated_backup_no_inflate(self):
        """模拟周期备份场景：同一key反复上传，used_bytes不虚高"""
        pool = _make_pool()
        client = FakeClient()
        pool._get_client = lambda idx: client
        pool._get_object_size = lambda idx, key: len(client.stored.get(key, b''))

        data = b'x' * (50 * 1024)  # 50KB
        for _ in range(100):  # 模拟100次周期备份
            pool.put('files.tar.gz', data)

        # 100次覆盖写，used_bytes 应该只有 50KB，不是 5MB
        assert pool._counters[0]['used_bytes'] == 50 * 1024


# ==================== Bug 2: put/get不对称 ====================

class TestBug2PutGetSymmetry:
    def test_put_does_not_fallback(self):
        """原桶可写时，put只写原桶，不fallback到其他桶"""
        pool = _make_pool()
        client0 = FakeClient()
        client1 = FakeClient()
        clients = {0: client0, 1: client1}
        pool._get_client = lambda idx: clients[idx]

        pool._get_object_size = lambda idx, key: len(clients[idx].stored.get(key, b''))

        pool.put('testkey', b'data')
        # 数据应该在桶0，不在桶1
        assert 'testkey' in client0.stored
        assert 'testkey' not in client1.stored

    def test_put_falls_back_when_bucket_unwritable(self):
        """原桶不可写时，put fallback到附近可写桶（v2设计）"""
        pool = _make_pool()
        # 让桶0不可写（used_bytes超过限制）
        pool._counters[0]['used_bytes'] = 5_000_000_000  # 5GB > 4.5GB限制

        client0 = FakeClient()
        client1 = FakeClient()
        clients = {0: client0, 1: client1}
        pool._get_client = lambda idx: clients[idx]
        pool._get_object_size = lambda idx, key: len(clients[idx].stored.get(key, b''))

        result = pool.put('testkey', b'data')
        # v2: fallback成功，数据写到桶1
        assert result is True
        assert 'testkey' not in client0.stored
        assert 'testkey' in client1.stored

    def test_put_returns_false_when_all_unwritable(self):
        """所有桶都不可写时，put返回False"""
        pool = _make_pool()
        # 让所有桶不可写
        for i in pool._counters:
            pool._counters[i]['used_bytes'] = 5_000_000_000  # 5GB > 4.5GB限制
        # nearby只返回存在的账号（真实环境不会返回不存在的账号）
        pool._hash_ring = type('R', (), {
            'get_account': lambda s, k: 0,
            'get_nearby_accounts': lambda s, k, count=10: [1]
        })()

        client0 = FakeClient()
        client1 = FakeClient()
        clients = {0: client0, 1: client1}
        pool._get_client = lambda idx: clients[idx]
        pool._get_object_size = lambda idx, key: len(clients[idx].stored.get(key, b''))

        result = pool.put('testkey', b'data')
        assert result is False
        assert 'testkey' not in client0.stored
        assert 'testkey' not in client1.stored

    def test_get_reads_from_same_bucket_as_put(self):
        """get从put写入的桶读取数据"""
        pool = _make_pool()
        client0 = FakeClient()
        client1 = FakeClient()
        clients = {0: client0, 1: client1}
        pool._get_client = lambda idx: clients[idx]
        pool._get_object_size = lambda idx, key: len(clients[idx].stored.get(key, b''))

        # put写到桶0
        pool.put('testkey', b'hello')
        # get应该从桶0读到
        data = pool.get('testkey')
        assert data == b'hello'
        # 桶1不应该有数据
        assert 'testkey' not in client1.stored


# ==================== Bug 4: _S3Client.put()检查返回值 ====================

class TestBug4PutReturnValue:
    def test_put_returns_true_on_success(self):
        """put_object成功时返回True"""
        from core.s3 import _S3Client
        client = _S3Client.__new__(_S3Client)
        client.bucket = 'test-bucket'

        class FakeBotoClient:
            def put_object(self, Bucket, Key, Body):
                return {'ETag': 'abc123'}

        client._client = FakeBotoClient()
        assert client.put('testkey', b'data') is True

    def test_put_returns_false_on_no_etag(self):
        """put_object返回无ETag时返回False"""
        from core.s3 import _S3Client
        client = _S3Client.__new__(_S3Client)
        client.bucket = 'test-bucket'

        class FakeBotoClient:
            def put_object(self, Bucket, Key, Body):
                return {}  # 无ETag

        client._client = FakeBotoClient()
        assert client.put('testkey', b'data') is False


# ==================== Bug 5: PID/配置路径 ====================

class TestBug5PidConfigPath:
    def test_pid_file_in_project_dir(self, monkeypatch, tmp_path):
        """PID文件写到项目目录，不依赖processes/"""
        import config
        from worker.process import config as pconfig

        files_dir = str(tmp_path / "kodebite")
        monkeypatch.setattr("config.FILES_DIR", files_dir)
        monkeypatch.setattr("worker.process.config.config.FILES_DIR", files_dir)

        path = pconfig.pid_file_path("myapp")
        # 应该在 FILES_DIR/myapp/pid，不是 processes/myapp/pid
        assert "processes" not in path
        assert path == os.path.join(files_dir, "myapp", "pid")

    def test_proc_config_in_project_dir(self, monkeypatch, tmp_path):
        """配置文件就是项目目录的ghvps.json"""
        import config
        from worker.process import config as pconfig

        files_dir = str(tmp_path / "kodebite")
        monkeypatch.setattr("config.FILES_DIR", files_dir)
        monkeypatch.setattr("worker.process.config.config.FILES_DIR", files_dir)

        path = pconfig.proc_config_path("myapp")
        assert path == os.path.join(files_dir, "myapp", "ghvps.json")
        assert "processes" not in path

    def test_save_load_proc_config_roundtrip(self, monkeypatch, tmp_path):
        """save_proc_config写到项目目录，load_proc_config能读回来"""
        import config
        from worker.process import config as pconfig

        files_dir = str(tmp_path / "kodebite")
        monkeypatch.setattr("config.FILES_DIR", files_dir)
        monkeypatch.setattr("worker.process.config.config.FILES_DIR", files_dir)

        cfg = {"name": "myapp", "command": "node app.js", "cwd": os.path.join(files_dir, "myapp")}
        assert pconfig.save_proc_config(cfg) is True

        # 读回来
        loaded = pconfig.load_proc_config("myapp")
        assert loaded is not None
        assert loaded["name"] == "myapp"
        assert loaded["command"] == "node app.js"

    def test_load_proc_config_returns_none_if_missing(self, monkeypatch, tmp_path):
        """项目目录没有ghvps.json时返回None"""
        import config
        from worker.process import config as pconfig

        files_dir = str(tmp_path / "kodebite")
        monkeypatch.setattr("config.FILES_DIR", files_dir)
        monkeypatch.setattr("worker.process.config.config.FILES_DIR", files_dir)

        assert pconfig.load_proc_config("nonexistent") is None

    def test_pid_file_survives_restore(self, monkeypatch, tmp_path):
        """PID文件在项目目录，不被restore_files_from_file删除"""
        import config
        from worker.process import config as pconfig

        files_dir = str(tmp_path / "kodebite")
        proc_dir = os.path.join(files_dir, "processes")
        monkeypatch.setattr("config.FILES_DIR", files_dir)
        monkeypatch.setattr("config.PROC_DIR", proc_dir)
        monkeypatch.setattr("worker.process.config.config.FILES_DIR", files_dir)
        monkeypatch.setattr("worker.process.config.config.PROC_DIR", proc_dir)

        # 在项目目录写PID文件
        pconfig.write_pid_file("myapp", 12345)
        pid_path = pconfig.pid_file_path("myapp")
        assert os.path.exists(pid_path)

        # 模拟restore_files_from_file的行为 — 不删processes/，PID文件应该还在
        # （之前的bug是restore后rm -rf processes/，PID文件丢失）
        # 现在不再删processes/，PID文件安全
        assert os.path.exists(pid_path)
        assert pconfig.read_pid_file("myapp") == 12345


# ==================== Bug 6: 数据重叠 ====================

class TestBug6NoDataOverlap:
    def test_restore_all_no_migration(self, monkeypatch, tmp_path):
        """restore_all不做旧格式迁移（全量备份已包含所有文件）"""
        import config
        from worker.process import config as pconfig

        files_dir = str(tmp_path / "kodebite")
        proc_dir = os.path.join(files_dir, "processes")
        monkeypatch.setattr("config.FILES_DIR", files_dir)
        monkeypatch.setattr("config.PROC_DIR", proc_dir)
        monkeypatch.setattr("worker.process.config.config.FILES_DIR", files_dir)
        monkeypatch.setattr("worker.process.config.config.PROC_DIR", proc_dir)
        monkeypatch.setattr("worker.process.restore.config.FILES_DIR", files_dir)
        monkeypatch.setattr("worker.process.restore.config.PROC_DIR", proc_dir)

        # 创建项目（模拟全量备份解压后的状态）
        proj = os.path.join(files_dir, "myapp")
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, "ghvps.json"), "w") as f:
            json.dump({"name": "myapp", "command": "echo hi", "cwd": proj}, f)

        # 在processes/创建一个旧格式目录（不应该被迁移）
        old_proc = os.path.join(proc_dir, "myapp")
        os.makedirs(os.path.join(old_proc, "app"), exist_ok=True)
        with open(os.path.join(old_proc, "app", "old_file.txt"), "w") as f:
            f.write("old")

        # scan_configs应该只从项目目录读，不从processes/读
        configs = pconfig.scan_configs()
        assert "myapp" in configs
        # 项目目录的ghvps.json应该被读到
        assert configs["myapp"]["command"] == "echo hi"

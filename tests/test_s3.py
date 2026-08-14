# -*- coding: utf-8 -*-
"""
S3 工具函数测试

测试：_parse_accounts、_dynamic_concurrency、常量值
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core.s3 import (
    _parse_accounts, _dynamic_concurrency,
    A_LIMIT, B_LIMIT, STORAGE_LIMIT,
    S3_PREFIX, LARGE_FILE_THRESHOLD, CHUNK_SIZE,
    CONC_SMALL, CONC_MEDIUM, CONC_LARGE,
    SMALL_FILE_BOUNDARY, MEDIUM_FILE_BOUNDARY,
)


def test_parse_accounts():
    """解析 AWS profiles 格式"""
    text = """[profile acc1]
aws_access_key_id = AKIA123
aws_secret_access_key = SECRET123
# bucket: bucket1

[profile acc2]
aws_access_key_id = AKIA456
aws_secret_access_key = SECRET456
# bucket: bucket2
"""
    accounts = _parse_accounts(text)
    assert len(accounts) == 2
    assert accounts[0]["access_key"] == "AKIA123"
    assert accounts[0]["secret_key"] == "SECRET123"
    assert accounts[0]["bucket"] == "bucket1"
    assert accounts[1]["access_key"] == "AKIA456"
    assert accounts[1]["bucket"] == "bucket2"


def test_parse_accounts_empty():
    """空文本返回空列表"""
    assert _parse_accounts("") == []
    assert _parse_accounts("# comment only\n# another") == []


def test_parse_accounts_partial():
    """缺少 secret_key 的账号仍被包含（只要有 access_key）"""
    text = """[profile acc1]
aws_access_key_id = AKIA123

[profile acc2]
aws_access_key_id = AKIA456
aws_secret_access_key = SECRET456
"""
    accounts = _parse_accounts(text)
    assert len(accounts) == 2
    assert accounts[0]["access_key"] == "AKIA123"
    assert "secret_key" not in accounts[0]
    assert accounts[1]["secret_key"] == "SECRET456"


def test_dynamic_concurrency():
    """动态并发数"""
    assert _dynamic_concurrency(1024) == CONC_SMALL          # < 50MB
    assert _dynamic_concurrency(SMALL_FILE_BOUNDARY) == CONC_MEDIUM   # 边界
    assert _dynamic_concurrency(SMALL_FILE_BOUNDARY + 1) == CONC_MEDIUM
    assert _dynamic_concurrency(MEDIUM_FILE_BOUNDARY) == CONC_LARGE   # 边界
    assert _dynamic_concurrency(MEDIUM_FILE_BOUNDARY + 1) == CONC_LARGE


def test_constants():
    """常量值校验"""
    assert S3_PREFIX == "ghbox"
    assert A_LIMIT == 9000
    assert B_LIMIT == 90000
    assert STORAGE_LIMIT == 4_500_000_000
    assert LARGE_FILE_THRESHOLD == 50 * 1024 * 1024
    assert CHUNK_SIZE == 10 * 1024 * 1024
    assert CONC_SMALL < CONC_MEDIUM < CONC_LARGE


def test_put_file_deletes_old_manifest(monkeypatch, tmp_path):
    """put_file 上传小文件后删除旧 manifest（防止 get_to_file 读旧版本）"""
    from core.s3 import S3Pool
    pool = S3Pool.__new__(S3Pool)
    pool._initialized = True
    pool._hash_ring = type('R', (), {
        'get_account': lambda s, k: 0,
        'get_nearby_accounts': lambda s, k, count=10: [1, 2]
    })()
    pool._counters = {0: {'status': 'active', 'a_count': 0, 'used_bytes': 0, 'b_count': 0, 'fail_count': 0},
                      1: {'status': 'active', 'a_count': 0, 'used_bytes': 0, 'b_count': 0, 'fail_count': 0},
                      2: {'status': 'active', 'a_count': 0, 'used_bytes': 0, 'b_count': 0, 'fail_count': 0}}
    pool._clients = {}
    pool._bootstrap = {'access_key': 'a', 'secret_key': 's', 'bucket': 'b'}
    pool._accounts = []
    pool._lock = __import__('threading').RLock()
    pool._owner = 'test'
    deleted = []
    class FakeClient:
        def put(self, key, data, prefix='ghbox'):
            return True
        def get(self, key, prefix='ghbox'):
            return None
        def delete(self, key, prefix='ghbox'):
            deleted.append(key)
            return True
    pool._get_client = lambda idx: FakeClient()
    # 创建小文件
    f = tmp_path / 'small.txt'
    f.write_text('hello')
    pool.put_file('inst-files/test/files.tar.gz', str(f))
    # 应该删除 manifest
    assert 'inst-files/test/files.tar.gz.manifest' in deleted

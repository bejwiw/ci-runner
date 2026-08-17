# -*- coding: utf-8 -*-
"""
一致性哈希环测试

测试：构建、分配一致性、空环、附近账号、增减账号影响范围
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core.hashring import HashRing


def test_build():
    """构建哈希环"""
    ring = HashRing(virtual_nodes=150)
    ring.build(10)
    assert ring.size == 1500  # 10 accounts * 150 vnodes


def test_build_zero():
    """0 个账号构建空环"""
    ring = HashRing()
    ring.build(0)
    assert ring.size == 0
    assert ring.get_account("key") is None


def test_get_account_consistency():
    """同一个 key 多次查询返回同一个账号"""
    ring = HashRing(virtual_nodes=150)
    ring.build(10)
    a1 = ring.get_account("test-key-1")
    a2 = ring.get_account("test-key-1")
    assert a1 == a2
    assert 1 <= a1 <= 10


def test_get_account_different_keys():
    """不同 key 可能分配到不同账号（概率性，至少不全是同一个）"""
    ring = HashRing(virtual_nodes=150)
    ring.build(20)
    accounts = set()
    for i in range(50):
        accounts.add(ring.get_account(f"key-{i}"))
    # 50个key至少分布到2个以上账号
    assert len(accounts) >= 2


def test_nearby_no_duplicates():
    """附近账号不重复"""
    ring = HashRing(virtual_nodes=150)
    ring.build(20)
    nearby = ring.get_nearby_accounts("some-key", count=5)
    assert len(nearby) == 5
    assert len(set(nearby)) == 5


def test_nearby_max_accounts():
    """count 超过账号数时返回全部"""
    ring = HashRing(virtual_nodes=150)
    ring.build(3)
    nearby = ring.get_nearby_accounts("key", count=10)
    assert len(nearby) <= 3


def test_add_account_impact():
    """增减账号只影响约 1/N 的数据"""
    ring1 = HashRing(virtual_nodes=150)
    ring1.build(10)
    ring2 = HashRing(virtual_nodes=150)
    ring2.build(11)

    moved = 0
    total = 200
    for i in range(total):
        key = f"key-{i}"
        if ring1.get_account(key) != ring2.get_account(key):
            moved += 1
    # 约 1/11 ≈ 9% 受影响，允许 3-20%
    ratio = moved / total
    assert 0.03 <= ratio <= 0.20, f"受影响比例 {ratio:.1%} 超出预期"


def test_get_nearby_excluding():
    """get_nearby_excluding 跳过排除账号，返回不同账号"""
    from core.hashring import HashRing
    ring = HashRing(virtual_nodes=10)
    ring.build(5)
    # 排除账号2，结果不应包含2
    result = ring.get_nearby_excluding("some-key", exclude=[2], count=3)
    assert len(result) == 3
    assert 2 not in result
    assert len(set(result)) == 3  # 不重复


def test_get_nearby_excluding_all_excluded():
    """排除全部账号时返回空"""
    from core.hashring import HashRing
    ring = HashRing(virtual_nodes=10)
    ring.build(3)
    result = ring.get_nearby_excluding("key", exclude=[1, 2, 3], count=5)
    assert result == []

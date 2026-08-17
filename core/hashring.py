# -*- coding: utf-8 -*-
"""
一致性哈希环

每个账号创建 virtual_nodes 个虚拟节点，均匀分布在 0~2^128 的环上。
key 哈希后顺时针找到的第一个虚拟节点即为归属账号。

增减账号时只有约 1/N 的数据受影响（而非取余哈希的几乎全部）。
"""
import hashlib
import bisect

DEFAULT_VIRTUAL_NODES = 150


class HashRing:
    """一致性哈希环"""

    def __init__(self, virtual_nodes=DEFAULT_VIRTUAL_NODES):
        self.virtual_nodes = virtual_nodes
        self._ring = []       # [(hash, account_idx), ...] 有序列表
        self._hashes = []     # [hash, ...] 用于 bisect 二分查找
        self._num_accounts = 0

    def build(self, num_accounts):
        """构建哈希环：每个账号撒 virtual_nodes 个虚拟节点"""
        self._ring = []
        self._num_accounts = num_accounts
        if num_accounts <= 0:
            self._hashes = []
            return
        for i in range(1, num_accounts + 1):
            for v in range(self.virtual_nodes):
                h = int(hashlib.md5(f"acct-{i}-vn-{v}".encode()).hexdigest(), 16)
                self._ring.append((h, i))
        self._ring.sort()
        self._hashes = [h for h, _ in self._ring]

    @property
    def size(self):
        return len(self._ring)

    def get_account(self, key):
        """key 顺时针找到的第一个账号，返回 account_idx 或 None"""
        if not self._ring:
            return None
        h = int(hashlib.md5(key.encode()).hexdigest(), 16)
        idx = bisect.bisect_right(self._hashes, h)
        if idx >= len(self._ring):
            idx = 0
        return self._ring[idx][1]

    def get_nearby_accounts(self, key, count=10):
        """key 顺时针方向的 count 个不同账号（遍历用）"""
        if not self._ring:
            return []
        h = int(hashlib.md5(key.encode()).hexdigest(), 16)
        start = bisect.bisect_right(self._hashes, h)
        result, seen = [], set()
        for i in range(len(self._ring)):
            pos = (start + i) % len(self._ring)
            acct = self._ring[pos][1]
            if acct not in seen:
                seen.add(acct)
                result.append(acct)
                if len(result) >= count:
                    break
        return result

    def get_nearby_excluding(self, key, exclude, count=10):
        """顺时针方向跳过 exclude 中的账号，返回 count 个不同账号"""
        if not self._ring:
            return []
        h = int(hashlib.md5(key.encode()).hexdigest(), 16)
        start = bisect.bisect_right(self._hashes, h)
        result, seen = [], set(exclude)
        for i in range(len(self._ring)):
            pos = (start + i) % len(self._ring)
            acct = self._ring[pos][1]
            if acct not in seen:
                seen.add(acct)
                result.append(acct)
                if len(result) >= count:
                    break
        return result

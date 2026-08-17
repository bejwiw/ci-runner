# -*- coding: utf-8 -*-
"""manager 元数据 Releases 节流测试"""
import os
import sys
import time

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


class TestReleasesThrottle:
    def test_throttle_blocks_high_freq(self, monkeypatch):
        """300s 内的重复保存被节流（只标记脏）"""
        import manager.store as store
        calls = {"n": 0}

        def fake_save(name, obj, **kw):
            calls["n"] += 1

        monkeypatch.setattr(store.releases, "save_json_enc", fake_save)
        monkeypatch.setattr(store, "RELEASES_FLUSH_INTERVAL", 300)
        store._last_releases_save[0] = 0.0
        store._releases_dirty[0] = False

        # 第一次：立即保存
        assert store._releases_save_throttled("instances.json.enc", [1]) is True
        assert calls["n"] == 1

        # 第二次（<300s）：节流跳过，标记脏
        assert store._releases_save_throttled("instances.json.enc", [1, 2]) is False
        assert calls["n"] == 1
        assert store._releases_dirty[0] is True

        # force 强制保存
        assert store._releases_save_throttled("instances.json.enc", [1, 2, 3],
                                              force=True) is True
        assert calls["n"] == 2

    def test_flush_dirty(self, monkeypatch):
        """flush_releases_dirty 补刷脏数据并清标记"""
        import manager.store as store
        calls = {"n": 0}

        def fake_save(name, obj, **kw):
            calls["n"] += 1

        monkeypatch.setattr(store.releases, "save_json_enc", fake_save)
        store._last_releases_save[0] = time.time()  # 刚保存过
        store._releases_dirty[0] = True
        store._instances = [{"id": "inst-x"}]

        assert store.flush_releases_dirty() is True
        assert calls["n"] == 1
        assert store._releases_dirty[0] is False

    def test_flush_not_dirty(self, monkeypatch):
        """无脏标记时不刷"""
        import manager.store as store
        calls = {"n": 0}

        def fake_save(name, obj, **kw):
            calls["n"] += 1

        monkeypatch.setattr(store.releases, "save_json_enc", fake_save)
        store._releases_dirty[0] = False
        assert store.flush_releases_dirty() is False
        assert calls["n"] == 0
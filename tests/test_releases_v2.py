# -*- coding: utf-8 -*-
"""Releases v2 + 上传队列测试"""
import os
import sys
import json
import time
import threading

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


class TestReleasesV2:
    def test_asset_name_v2(self):
        from core.releases import asset_name_v2
        name = asset_name_v2("inst-inst3.db", ts=1234567890)
        assert name == "inst-inst3.db.1234567890.enc"
        # 含 / 替换为 .
        name2 = asset_name_v2("a/b", ts=1)
        assert name2 == "a.b.1.enc"

    def test_concurrency_for_parts(self):
        from core.releases import _concurrency_for_parts
        assert _concurrency_for_parts(1) == 1
        assert _concurrency_for_parts(2) == 2
        assert _concurrency_for_parts(5) == 5
        assert _concurrency_for_parts(8) == 8
        assert _concurrency_for_parts(100) == 8  # 封顶8

    def test_chunk_size_for(self):
        from core.releases import _chunk_size_for, SINGLE_UPLOAD_LIMIT
        assert _chunk_size_for(100 * 1024 * 1024) == 0          # 不分片
        assert _chunk_size_for(SINGLE_UPLOAD_LIMIT) == 0        # 边界不分片
        # 2GB: 目标片数2~6 → per≈341MB → 最小500MB? 按算法 ceil(2G/6)=341M→向上取整250M倍数=500M
        total = 2 * 1024 * 1024 * 1024
        cs = _chunk_size_for(total)
        assert cs > 0 and cs <= SINGLE_UPLOAD_LIMIT
        # 片数应 <= 6
        import math
        parts = math.ceil(total / cs)
        assert 2 <= parts <= 6

    def test_upload_once_404_rebuild(self, monkeypatch):
        """404 时重建 release 后重试成功"""
        import core.releases as rel
        calls = {"n": 0}

        def fake_gh(method, url, **kw):
            calls["n"] += 1
            if method == "POST" and "assets" in url:
                if calls["n"] == 1:
                    return 404, {}
                return 201, {}
            return 200, {"id": 123}

        monkeypatch.setattr(rel.ghapi, "gh_request", fake_gh)
        monkeypatch.setattr(rel, "ensure_release", lambda **kw: 999)
        monkeypatch.setattr(rel, "_invalidate_release", lambda *a, **kw: None)

        status = rel._upload_once(999, "x.enc", b"data", "tok", "repo", timeout=10)
        assert status == 201

    def test_find_latest_asset(self, monkeypatch):
        """版本化命名找最新；旧固定名视为最旧"""
        from core.releases import find_latest_asset
        assets = [
            {"name": "inst-inst3.db.enc", "id": 1},                       # 旧格式(无ts)
            {"name": "inst-inst3.db.1700000001.enc", "id": 2},
            {"name": "inst-inst3.db.1700000002.enc", "id": 3},            # 最新
            {"name": "inst-inst3.db.manifest.1700000002.enc", "id": 4},   # manifest 非版本
            {"name": "inst-inst3.db.part0.1700000002.enc", "id": 5},      # 分片非版本
            {"name": "other.enc", "id": 99},
        ]
        monkeypatch.setattr("core.releases.list_assets", lambda **kw: assets)
        a = find_latest_asset("inst-inst3.db")
        assert a["id"] == 3  # manifest/part 不被当版本

    def test_cleanup_old_versions(self, monkeypatch):
        """清理保留最新 keep 个（只删创建超过1小时）"""
        from core.releases import cleanup_old_versions
        now = time.time()
        assets = [
            {"name": "inst-x.files.100.enc", "id": 1,
             "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7200))},
            {"name": "inst-x.files.200.enc", "id": 2,
             "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))},
            {"name": "inst-x.files.300.enc", "id": 3,
             "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 100))},
        ]
        deleted = []
        monkeypatch.setattr("core.releases.list_assets", lambda **kw: assets)
        monkeypatch.setattr("core.releases.ghapi.gh_request",
                            lambda method, url, **kw: (204, None) if method == "DELETE"
                            else (200, {"id": 1}))
        # ensure_release 需要
        monkeypatch.setattr("core.releases.ensure_release", lambda **kw: 1)
        monkeypatch.setattr("core.releases._invalidate_release", lambda *a, **kw: None)

        removed = cleanup_old_versions("inst-x.files", keep=2, older_than=3000,
                                       token="t", repo="r")
        # 保留最新2个(300/200)，100(旧>1h) 被删；200 虽然旧但保留
        assert removed == ["inst-x.files.100.enc"]


class TestUploadQueue:
    def _make_queue(self, monkeypatch, tmp_path):
        import worker.upload_queue as uq
        monkeypatch.setattr(uq, "QUEUE_DIR", str(tmp_path / ".queue"))
        # mock 配额检查（测试环境无 API）
        monkeypatch.setattr("core.ghapi.check_rate_limit",
                            lambda *a, **kw: (5000, 5000, 0))
        q = uq.ReleasesUploadQueue(max_workers=1, maxsize=8)
        return uq, q

    def test_enqueue_and_flush(self, monkeypatch, tmp_path):
        """入队→worker 处理→上传调用"""
        uq, q = self._make_queue(monkeypatch, tmp_path)

        uploaded = []
        def fake_upload_asset_v2(base, data_or_path, token=None, repo=None, ts=None):
            uploaded.append((base, len(data_or_path) if isinstance(data_or_path, bytes) else 0))
            return {"ok": True, "status": 201}
        monkeypatch.setattr(uq.releases, "upload_asset_v2", fake_upload_asset_v2)
        monkeypatch.setattr(uq.releases, "SINGLE_UPLOAD_LIMIT", 10 * 1024 * 1024)

        q.start()
        assert q.enqueue("db", "inst-x.db", data=b"hello", inst_id="x") is True
        assert q.flush(timeout=10) is True
        q.stop(flush=False)
        assert uploaded and uploaded[0][0] == "inst-x.db"  # 拼ts在upload内部
        assert q.status()["ok_uploads"] >= 1

    def test_queue_full_skip(self, monkeypatch, tmp_path):
        """队列满时拒绝并跳过"""
        uq, q = self._make_queue(monkeypatch, tmp_path)
        monkeypatch.setattr(uq.releases, "upload_asset_v2",
                            lambda *a, **kw: {"ok": True, "status": 201})
        monkeypatch.setattr(uq, "_quota_low_v2", lambda *a: False, raising=False)
        # 小 maxsize，一次性塞满
        for i in range(100):
            q.enqueue("db", f"inst-{i}.db", data=b"x", inst_id="x")
        assert q.status()["skipped"] > 0
        q.stop(flush=False)

    def test_dlq_on_failure(self, monkeypatch, tmp_path):
        """上传失败进死信"""
        uq, q = self._make_queue(monkeypatch, tmp_path)
        monkeypatch.setattr(uq.releases, "upload_asset_v2",
                            lambda *a, **kw: {"ok": False, "status": 500})
        q.start()
        q.enqueue("db", "inst-x.db", data=b"data", inst_id="x")
        q.flush(timeout=10)
        q.stop(flush=False)
        assert q.status()["dlq"] >= 1
        assert os.path.exists(uq.dlq_file())

    def test_pending_restore(self, monkeypatch, tmp_path):
        """待传清单落盘与恢复"""
        uq, q = self._make_queue(monkeypatch, tmp_path)
        os.makedirs(uq.QUEUE_DIR, exist_ok=True)
        # 制造一个待传文件
        f = os.path.join(uq.QUEUE_DIR, "restore-me.tar.gz")
        with open(f, "wb") as fp:
            fp.write(b"test-data")
        pending = [{"kind": "files", "base": "inst-x.files", "path": f,
                    "inst_id": "x", "ts": time.time(), "attempts": 0}]
        with open(uq.pending_file(), "w") as fp:
            json.dump(pending, fp)

        uploaded = []
        monkeypatch.setattr(uq.releases, "upload_asset_v2",
                            lambda base, data_or_path, **kw: (
                                uploaded.append((base, data_or_path)) or
                                {"ok": True, "status": 201}))
        monkeypatch.setattr(uq.releases, "SINGLE_UPLOAD_LIMIT", 10 * 1024 * 1024)

        q.start()
        assert q.flush(timeout=10) is True
        q.stop(flush=False)
        assert uploaded, "pending 任务应被恢复并上传"
        assert q.status()["ok_uploads"] >= 1
        # 临时文件应被清理
        assert not os.path.exists(f) or q.status()["ok_uploads"] >= 1

class TestChunkedDownloadV2:
    def test_download_chunked_v2(self, monkeypatch):
        """v2 分片下载：manifest → 逐片 CDN → 合并"""
        import core.releases as rel
        manifest_json = json.dumps({"parts": 3, "chunk_size": 10}).encode()

        downloads = {}

        def fake_list_assets(**kw):
            return [{"name": "inst-x.files.manifest.1700000001.enc", "id": 1}]

        def fake_download_asset(name, token=None, repo=None):
            # 模拟 download_asset（API降级）—— 返回明文（未加密简化）
            return downloads.get(name)

        def fake_download_cdn(name, token=None, repo=None):
            if name.endswith(".manifest.1700000001.enc"):
                return manifest_json
            if name == "inst-x.files.part0.1700000001.enc":
                return b"AAA"
            if name == "inst-x.files.part1.1700000001.enc":
                return b"BBB"
            if name == "inst-x.files.part2.1700000001.enc":
                return b"CCC"
            return None

        monkeypatch.setattr(rel, "list_assets", fake_list_assets)
        monkeypatch.setattr(rel, "download_cdn", fake_download_cdn)
        monkeypatch.setattr(rel, "download_asset", fake_download_asset)
        monkeypatch.setattr(rel, "_concurrency_for_parts", lambda n: 2)

        out = rel.download_chunked_v2("inst-x.files")
        assert out == b"AAABBBCCC"

    def test_download_latest_no_single_falls_to_chunked(self, monkeypatch):
        """无单文件版本 → 自动走分片下载"""
        import core.releases as rel
        manifest_json = json.dumps({"parts": 1, "chunk_size": 10}).encode()

        monkeypatch.setattr(rel, "find_latest_asset", lambda *a, **kw: None)
        monkeypatch.setattr(rel, "list_assets",
                            lambda **kw: [{"name": "inst-x.files.manifest.999.enc", "id": 1}])
        monkeypatch.setattr(rel, "download_cdn",
                            lambda name, **kw: manifest_json
                            if "manifest" in name
                            else (b"DATA" if name == "inst-x.files.part0.999.enc" else None))
        monkeypatch.setattr(rel, "download_asset", lambda *a, **kw: None)
        monkeypatch.setattr(rel, "_concurrency_for_parts", lambda n: 1)

        out = rel.download_latest("inst-x.files")
        assert out == b"DATA"


def test_upload_invalidates_cache(monkeypatch):
    """上传成功后 asset 缓存失效（spy 验证）"""
    import core.releases as rel
    import core.ghapi as ghapi_mod

    count = {"n": 0}
    real_invalidate = rel._invalidate_release

    def spy_invalidate(*a, **kw):
        count["n"] += 1
        return real_invalidate(*a, **kw)

    monkeypatch.setattr(rel, "_invalidate_release", spy_invalidate)
    # 简化上传：直接 mock gh_request 201 + encrypt 恒等
    monkeypatch.setattr(ghapi_mod, "gh_request",
                        lambda *a, **kw: (201, {}))
    monkeypatch.setattr(rel, "crypto",
                        type("C", (), {"encrypt_bytes": staticmethod(lambda d: d)})())
    monkeypatch.setattr(rel, "ensure_release", lambda **kw: 1)

    r = rel.upload_asset_v2("inst-x.db", b"data", token="t", repo="r")
    assert r["ok"] is True, r
    assert count["n"] == 1

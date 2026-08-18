# -*- coding: utf-8 -*-
"""
Bug修复验证测试（第二批，2026-08-18审计）

Bug 1（S3初始化顺序）放弃修复（影响小且引入时序问题）
Bug 2-7 已修复
"""
import os
import sys
import inspect

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


class TestBug2NoDuplicateUpload:
    def test_put_file_called_once(self, monkeypatch, tmp_path):
        from worker import persistence
        files_dir = str(tmp_path / "kodebite")
        monkeypatch.setattr("config.FILES_DIR", files_dir)
        monkeypatch.setattr("worker.persistence.config.FILES_DIR", files_dir)
        os.makedirs(files_dir, exist_ok=True)
        call_count = [0]
        class FakeS3Pool:
            def is_ready(self): return True
            def put_file(self, key, path): call_count[0] += 1; return True
        persistence._s3pool = FakeS3Pool()
        tmp_file = os.path.join(files_dir, "backup.tar.gz")
        with open(tmp_file, "wb") as f: f.write(b"fake")
        monkeypatch.setattr(persistence, "backup_files_to_disk", lambda: tmp_file)
        class FakeQueue:
            def enqueue(self, *a, **kw): return True
        monkeypatch.setattr("worker.persistence.upload_queue.get_queue", lambda: FakeQueue())
        monkeypatch.setattr("worker.persistence.upload_queue.QUEUE_DIR", files_dir)
        class FakeCfg:
            instance_id = "test"
            asset_files = "inst-test.files.tar.gz.enc"
        persistence.backup_files(FakeCfg())
        assert call_count[0] == 1

    def test_no_duplicate_in_source(self):
        from worker import persistence
        src = inspect.getsource(persistence.backup_files)
        assert src.count("_s3pool.put_file(") == 1


class TestBug3NoExclude:
    def test_no_exclude_in_upload_chunk(self):
        from core import s3
        src = inspect.getsource(s3.S3Pool._put_file_chunked)
        assert "exclude=[" not in src


class TestBug4TerminalEncoding:
    def test_ws_input_utf8(self):
        from worker import app as wapp
        src = inspect.getsource(wapp.ws_input)
        assert "utf-8" in src
        assert "latin-1" not in src

    def test_pty_reader_buffer(self):
        from worker import app as wapp
        src = inspect.getsource(wapp._pty_reader)
        assert "buf" in src
        assert "utf-8" in src
        assert "latin-1" not in src

    def test_pty_reader_flush_on_exit(self):
        from worker import app as wapp
        src = inspect.getsource(wapp._pty_reader)
        assert "if buf:" in src

    def test_utf8_split(self):
        buf = b"\xe4\xbd\xa0\xe4\xbd"
        for i in range(1, min(5, len(buf) + 1)):
            try:
                text = buf[:-i].decode("utf-8")
                buf2 = buf[-i:]
                break
            except UnicodeDecodeError:
                continue
        else:
            text, buf2 = buf.decode("utf-8", errors="replace"), b""
        assert text == "你"
        assert buf2 == b"\xe4\xbd"


class TestBug5ZstdDetection:
    def test_has_zstd(self):
        from worker import persistence
        src = inspect.getsource(persistence.restore_files_from_bytes)
        assert "\\x28" in src
        assert "--zstd" in src


class TestBug6AutoUpdate:
    def test_dispatch_status_check(self):
        from manager import app
        src = inspect.getsource(app._auto_update)
        assert "d_status" in src
        assert "not in (200, 204)" in src

    def test_exit_after_check(self):
        from manager import app
        src = inspect.getsource(app._auto_update)
        assert src.find("d_status not in") < src.find("os._exit(0)")


class TestBug7NoImportHack:
    def test_no_import_logging(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "log.py")
        with open(path) as f:
            src = f.read()
        assert '__import__("logging")' not in src

    def test_classes(self):
        import log, logging
        assert logging.Formatter in log._BJFormatter.__mro__
        assert logging.Handler in log.RingBufferHandler.__mro__
        assert logging.Filter in log.ContextFilter.__mro__

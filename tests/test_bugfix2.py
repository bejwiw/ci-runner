# -*- coding: utf-8 -*-
"""
Bug修复验证测试（第二批，2026-08-18审计）

Bug 1（S3初始化顺序）放弃修复（影响小且引入时序问题）
Bug 2-7 已修复

注意：测试通过读源码文件验证，不导入 worker.app/manager.app
（避免在 test.yml 环境中依赖 flask）
"""
import os
import sys

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel_path):
    """读取源码文件（不导入模块，避免 flask 依赖）"""
    with open(os.path.join(_BASE, rel_path)) as f:
        return f.read()


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
        src = _src("worker/persistence.py")
        # 在 backup_files 函数体内只出现一次
        idx = src.find("def backup_files(")
        assert idx >= 0
        func_body = src[idx:]
        assert func_body.count("_s3pool.put_file(") == 1


class TestBug3NoExclude:
    def test_no_exclude_in_upload_chunk(self):
        src = _src("core/s3.py")
        idx = src.find("def _put_file_chunked")
        assert idx >= 0
        func_body = src[idx:src.find("def ", idx + 10)]
        assert "exclude=[" not in func_body


class TestBug4TerminalEncoding:
    def test_ws_input_utf8(self):
        src = _src("worker/app.py")
        assert "encode(\"utf-8\")" in src or "encode('utf-8')" in src
        assert "encode(\"latin-1\")" not in src
        assert "encode('latin-1')" not in src

    def test_pty_reader_buffer(self):
        src = _src("worker/app.py")
        idx = src.find("def _pty_reader")
        assert idx >= 0
        func_body = src[idx:src.find("def ", idx + 10)]
        assert "buf" in func_body
        assert "utf-8" in func_body or "utf_8" in func_body
        assert "latin-1" not in func_body

    def test_pty_reader_flush_on_exit(self):
        src = _src("worker/app.py")
        idx = src.find("def _pty_reader")
        func_body = src[idx:src.find("def ", idx + 10)]
        assert "if buf:" in func_body

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
        src = _src("worker/persistence.py")
        idx = src.find("def restore_files_from_bytes")
        assert idx >= 0
        func_body = src[idx:src.find("def ", idx + 10)]
        assert "\\x28" in func_body
        assert "--zstd" in func_body


class TestBug6AutoUpdate:
    def test_dispatch_status_check(self):
        src = _src("manager/app.py")
        idx = src.find("def _auto_update")
        assert idx >= 0
        func_body = src[idx:]
        assert "d_status" in func_body
        assert "not in (200, 204)" in func_body

    def test_exit_after_check(self):
        src = _src("manager/app.py")
        idx = src.find("def _auto_update")
        func_body = src[idx:]
        assert func_body.find("d_status not in") < func_body.find("os._exit(0)")


class TestBug7NoImportHack:
    def test_no_import_logging(self):
        src = _src("log.py")
        assert '__import__("logging")' not in src
        assert "__import__('logging')" not in src

    def test_classes(self):
        import log, logging
        assert logging.Formatter in log._BJFormatter.__mro__
        assert logging.Handler in log.RingBufferHandler.__mro__
        assert logging.Filter in log.ContextFilter.__mro__

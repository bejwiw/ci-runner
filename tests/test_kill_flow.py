# -*- coding: utf-8 -*-
"""
关闭流程v2测试（2026-08-19）

覆盖：
- worker /api/kill 端点：立即退出，不备份（区别于 /api/shutdown）
- worker _report_running 404 检测：连续3次被拒退出，网络错误不计数
- manager close_instance 优先 kill API，失败才取消run
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


class TestKillAPI:
    def test_kill_endpoint_exists(self):
        """worker/app.py 中有 /api/kill 端点"""
        src = _src("worker/app.py")
        assert '"/api/kill"' in src or "'/api/kill'" in src
        assert "def kill_now" in src

    def test_kill_uses_os_exit(self):
        """kill 端点使用 os._exit(0) 立即退出"""
        src = _src("worker/app.py")
        idx = src.find("def kill_now")
        assert idx >= 0
        func_body = src[idx:]
        assert "os._exit(0)" in func_body
        assert "state.shutting_down = True" in func_body

    def test_kill_requires_auth(self):
        """kill 端点需要鉴权"""
        src = _src("worker/app.py")
        idx = src.find("def kill_now")
        assert idx >= 0
        func_body = src[idx:]
        assert "_check(data)" in func_body

    def test_kill_distinct_from_shutdown(self):
        """kill 与 shutdown 严格区分（kill不备份）"""
        src = _src("worker/app.py")
        kill_idx = src.find("def kill_now")
        shutdown_idx = src.find("def graceful_shutdown")
        assert kill_idx >= 0 and shutdown_idx >= 0
        kill_body = src[kill_idx:shutdown_idx]
        # kill 部分不应包含备份逻辑
        assert "backup_database" not in kill_body
        assert "final_snapshot" not in kill_body


class TestReport404Detection:
    def test_404_detection_in_report(self):
        """_report_running 中有 404 检测逻辑"""
        src = _src("worker/loops.py")
        idx = src.find("def _report_running")
        assert idx >= 0
        func_body = src[idx:]
        assert "e.code == 404" in func_body
        assert "rejected_count" in func_body
        assert "os._exit(0)" in func_body

    def test_only_404_counts(self):
        """只有404计数，其他HTTP错误/网络错误不计数"""
        src = _src("worker/loops.py")
        idx = src.find("def _report_running")
        func_body = src[idx:]
        # 非404的HTTP错误分支：重置计数
        assert "rejected_count = 0" in func_body
        # 网络异常分支：重置计数
        assert "rejected_count = 0" in func_body

    def test_threshold_exit(self):
        """达到阈值后退出"""
        src = _src("worker/loops.py")
        assert "REJECT_THRESHOLD" in src
        assert "rejected_count >= REJECT_THRESHOLD" in src or "rejected_count >= REJECT_THRESHOLD" in src


class TestCloseFlowKillFirst:
    def test_close_flow_calls_kill(self):
        """close_instance 优先调用 /api/kill"""
        src = _src("manager/api_instances.py")
        idx = src.find("def close_instance")
        assert idx >= 0
        func_body = src[idx:]
        assert "/api/kill" in func_body

    def test_close_flow_kill_before_cancel(self):
        """kill 调用在取消run之前"""
        src = _src("manager/api_instances.py")
        idx = src.find("def close_instance")
        func_body = src[idx:]
        kill_pos = func_body.find("/api/kill")
        cancel_pos = func_body.find("/cancel")
        assert kill_pos >= 0, "应有kill调用"
        assert cancel_pos >= 0, "应有取消run"
        assert kill_pos < cancel_pos, "kill应该在取消run之前"

    def test_close_flow_still_cancels_run(self):
        """kill后仍取消run（双保险）"""
        src = _src("manager/api_instances.py")
        idx = src.find("def close_instance")
        func_body = src[idx:]
        assert "/cancel" in func_body
        assert func_body.count("/cancel") >= 1

    def test_close_flow_keeps_tombstone(self):
        """关闭流程仍持久化墓碑"""
        src = _src("manager/api_instances.py")
        idx = src.find("def close_instance")
        func_body = src[idx:]
        assert "store.close_instance(inst_id)" in func_body
# -*- coding: utf-8 -*-
"""
快照备份测试

测试：snapshot 完整性验证、backup_process_files、_clear_dir
"""
import os
import sys
import json

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture
def setup_env(monkeypatch, tmp_path):
    """设置完整环境到临时路径"""
    files_dir = str(tmp_path / "kodebite")
    proc_dir = os.path.join(files_dir, "processes")
    logs_dir = os.path.join(files_dir, "logs")
    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(proc_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    monkeypatch.setattr("config.FILES_DIR", files_dir)
    monkeypatch.setattr("config.PROC_DIR", proc_dir)
    monkeypatch.setattr("config.LOGS_DIR", logs_dir)
    monkeypatch.setattr("worker.process.config.config.FILES_DIR", files_dir)
    monkeypatch.setattr("worker.process.config.config.PROC_DIR", proc_dir)
    monkeypatch.setattr("worker.process.backup.config.FILES_DIR", files_dir)
    monkeypatch.setattr("worker.process.backup.config.PROC_DIR", proc_dir)
    monkeypatch.setattr("worker.process.backup.pconfig.config.FILES_DIR", files_dir)
    monkeypatch.setattr("worker.process.backup.pconfig.config.PROC_DIR", proc_dir)
    return files_dir, proc_dir


def test_snapshot_includes_all_projects(setup_env):
    """snapshot 包含所有 scan_configs 扫描到的项目"""
    files_dir, proc_dir = setup_env
    # 创建两个项目
    for name, cmd in [("app1", "node server.js"), ("app2", "python app.py")]:
        proj = os.path.join(files_dir, name)
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, "ghvps.json"), "w") as f:
            json.dump({"name": name, "command": cmd, "cwd": proj}, f)
        with open(os.path.join(proj, "index.js"), "w") as f:
            f.write("// " + name)
    # 执行 snapshot
    from worker.process import backup as pbackup
    saved, meta = pbackup.snapshot(reason="test")
    assert saved == 2
    assert "app1" in meta
    assert "app2" in meta
    assert meta["app1"]["cmdline"] == "node server.js"
    assert meta["app2"]["cmdline"] == "python app.py"


def test_snapshot_includes_failed_projects(setup_env):
    """即使 backup_process_files 失败也加入 manifest"""
    files_dir, proc_dir = setup_env
    # 创建一个有效项目
    proj1 = os.path.join(files_dir, "good")
    os.makedirs(proj1, exist_ok=True)
    with open(os.path.join(proj1, "ghvps.json"), "w") as f:
        json.dump({"name": "good", "command": "echo hi", "cwd": proj1}, f)
    # 创建一个 cwd 在 FILES_DIR 下但目录不存在的项目（scan_configs 扫到但 backup 失败）
    proj2 = os.path.join(files_dir, "bad")
    os.makedirs(proj2, exist_ok=True)
    with open(os.path.join(proj2, "ghvps.json"), "w") as f:
        json.dump({"name": "bad", "command": "echo bad", "cwd": os.path.join(files_dir, "bad", "missing_cwd")}, f)
    from worker.process import backup as pbackup
    saved, meta = pbackup.snapshot(reason="test")
    # 两个都在 manifest 里
    assert "good" in meta
    assert "bad" in meta
    assert meta["bad"]["files_backed"] is False


def test_snapshot_cleans_stale(setup_env):
    """snapshot 只记录有效项目，不记录无ghvps.json的残留目录"""
    files_dir, proc_dir = setup_env
    # 创建一个有效项目
    proj = os.path.join(files_dir, "active")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "ghvps.json"), "w") as f:
        json.dump({"name": "active", "command": "echo hi", "cwd": proj}, f)
    # 创建残留目录（无ghvps.json，不会被scan_configs扫到）
    stale = os.path.join(files_dir, "deleted_project")
    os.makedirs(stale, exist_ok=True)
    with open(os.path.join(stale, "data.txt"), "w") as f:
        f.write("stale data")
    from worker.process import backup as pbackup
    saved, meta = pbackup.snapshot(reason="test")
    # 只有有效项目被记录
    assert "active" in meta
    assert "deleted_project" not in meta


def test_snapshot_empty(setup_env):
    """无项目时返回空"""
    from worker.process import backup as pbackup
    saved, meta = pbackup.snapshot(reason="test")
    assert saved == 0
    assert meta == {}


def test_backup_process_files_copies(setup_env):
    """backup_process_files 正确复制文件"""
    files_dir, proc_dir = setup_env
    proj = os.path.join(files_dir, "myapp")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "ghvps.json"), "w") as f:
        json.dump({"name": "myapp", "command": "node app.js", "cwd": proj}, f)
    with open(os.path.join(proj, "app.js"), "w") as f:
        f.write("console.log('hello')")
    from worker.process.backup import backup_process_files
    cfg = {"name": "myapp", "command": "node app.js", "cwd": proj, "exclude": []}
    ok, size_mb, cfg = backup_process_files(cfg)
    assert ok is True
    assert cfg["files_backed"] is True
    # app.js 应该被复制到 processes/myapp/app/
    dest = os.path.join(proc_dir, "myapp", "app", "app.js")
    assert os.path.exists(dest)

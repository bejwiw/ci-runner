# -*- coding: utf-8 -*-
"""
进程配置测试

测试：scan_configs、is_bash_session、pid_file 读写、save/load_proc_config
"""
import os
import sys
import json

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture
def setup_files_dir(monkeypatch, tmp_path):
    """设置 FILES_DIR 到临时路径"""
    files_dir = str(tmp_path / "kodebite")
    os.makedirs(files_dir, exist_ok=True)
    monkeypatch.setattr("config.FILES_DIR", files_dir)
    monkeypatch.setattr("config.PROC_DIR", os.path.join(files_dir, "processes"))
    monkeypatch.setattr("worker.process.config.config.FILES_DIR", files_dir)
    from worker.process import config as pconfig
    return pconfig, files_dir


def test_scan_configs_finds_ghvps(setup_files_dir):
    """扫描到有效的 ghvps.json"""
    pconfig, files_dir = setup_files_dir
    proj = os.path.join(files_dir, "myapp")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "ghvps.json"), "w") as f:
        json.dump({"name": "myapp", "command": "node app.js", "cwd": proj}, f)
    configs = pconfig.scan_configs()
    assert "myapp" in configs
    assert configs["myapp"]["command"] == "node app.js"


def test_scan_configs_skips_dirs(setup_files_dir):
    """跳过 processes/logs/mcp-server 等目录"""
    pconfig, files_dir = setup_files_dir
    # 创建应跳过的目录
    for skip_dir in ["processes", "logs", "mcp-server", "mcp-files", "sysconfig"]:
        d = os.path.join(files_dir, skip_dir)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "ghvps.json"), "w") as f:
            json.dump({"name": skip_dir, "command": "test"}, f)
    configs = pconfig.scan_configs()
    for skip_dir in ["processes", "logs", "mcp-server", "mcp-files", "sysconfig"]:
        assert skip_dir not in configs


def test_scan_configs_empty_command(setup_files_dir):
    """command 为空时跳过"""
    pconfig, files_dir = setup_files_dir
    proj = os.path.join(files_dir, "nocommand")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "ghvps.json"), "w") as f:
        json.dump({"name": "nocommand", "command": "", "cwd": proj}, f)
    configs = pconfig.scan_configs()
    assert "nocommand" not in configs


def test_scan_configs_no_name_uses_dirname(setup_files_dir):
    """name 为空时用目录名"""
    pconfig, files_dir = setup_files_dir
    proj = os.path.join(files_dir, "noname")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "ghvps.json"), "w") as f:
        json.dump({"name": "", "command": "echo hi", "cwd": proj}, f)
    configs = pconfig.scan_configs()
    assert "noname" in configs


def test_scan_configs_empty(setup_files_dir):
    """空目录返回空配置"""
    pconfig, files_dir = setup_files_dir
    configs = pconfig.scan_configs()
    assert configs == {}


def test_is_bash_session():
    """判断 bash 终端会话"""
    from worker.process.config import is_bash_session
    assert is_bash_session({"command": "bash --norc --noprofile"}) is True
    assert is_bash_session({"command": "bash"}) is True
    assert is_bash_session({"command": "bash run.sh"}) is False
    assert is_bash_session({"command": "node app.js"}) is False
    assert is_bash_session({"command": ""}) is False
    assert is_bash_session({}) is False


def test_pid_file_write_read(setup_files_dir):
    """PID 文件写入和读取"""
    pconfig, files_dir = setup_files_dir
    pconfig.write_pid_file("testproc", 12345)
    pid = pconfig.read_pid_file("testproc")
    assert pid == 12345


def test_pid_file_read_nonexistent(setup_files_dir):
    """读取不存在的 PID 文件返回 None"""
    pconfig, files_dir = setup_files_dir
    assert pconfig.read_pid_file("nonexistent") is None


def test_pid_file_delete(setup_files_dir):
    """删除 PID 文件"""
    pconfig, files_dir = setup_files_dir
    pconfig.write_pid_file("testproc", 12345)
    assert pconfig.read_pid_file("testproc") == 12345
    pconfig.delete_pid_file("testproc")
    assert pconfig.read_pid_file("testproc") is None


def test_save_load_proc_config(setup_files_dir):
    """保存和读取进程配置"""
    pconfig, files_dir = setup_files_dir
    cfg = {"name": "testproc", "command": "node app.js", "cwd": "/tmp/test"}
    pconfig.save_proc_config(cfg)
    loaded = pconfig.load_proc_config("testproc")
    assert loaded is not None
    assert loaded["name"] == "testproc"
    assert loaded["command"] == "node app.js"

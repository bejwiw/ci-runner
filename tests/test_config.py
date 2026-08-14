# -*- coding: utf-8 -*-
"""
配置校验测试

测试：合法配置通过、非法 DEMO_KEY/PORT/INTERVAL 退出
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib
import pytest


def _reload_config():
    """重新加载 config 模块（读取最新环境变量）"""
    import config
    importlib.reload(config)
    return config


def test_validate_ok():
    """合法配置通过校验"""
    os.environ["EXEC_TOKEN"] = "test-token"
    os.environ["DEMO_KEY"] = "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a"
    os.environ["PORT"] = "8080"
    os.environ["BACKUP_INTERVAL"] = "180"
    os.environ["HEARTBEAT_INTERVAL"] = "60"
    os.environ["HEARTBEAT_TIMEOUT"] = "90"
    os.environ["INSTANCE_ROLE"] = "worker"
    os.environ["MANAGER_HOST"] = "ghvps2.kekeke.cc.cd"
    cfg = _reload_config()
    assert cfg.validate_config() is True


def test_validate_bad_demo_key():
    """非法 DEMO_KEY 应退出"""
    os.environ["DEMO_KEY"] = "not-hex!!"
    os.environ["EXEC_TOKEN"] = "test"
    os.environ["PORT"] = "8080"
    os.environ["BACKUP_INTERVAL"] = "180"
    os.environ["HEARTBEAT_INTERVAL"] = "60"
    os.environ["HEARTBEAT_TIMEOUT"] = "90"
    cfg = _reload_config()
    with pytest.raises(SystemExit):
        cfg.validate_config()


def test_validate_short_demo_key():
    """DEMO_KEY 长度不对应退出"""
    os.environ["DEMO_KEY"] = "abcd"  # 2字节，不是32字节
    os.environ["EXEC_TOKEN"] = "test"
    os.environ["PORT"] = "8080"
    os.environ["BACKUP_INTERVAL"] = "180"
    os.environ["HEARTBEAT_INTERVAL"] = "60"
    os.environ["HEARTBEAT_TIMEOUT"] = "90"
    cfg = _reload_config()
    with pytest.raises(SystemExit):
        cfg.validate_config()


def test_validate_bad_port():
    """非法 PORT 应退出"""
    os.environ["DEMO_KEY"] = "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a"
    os.environ["EXEC_TOKEN"] = "test"
    os.environ["PORT"] = "99999"
    os.environ["BACKUP_INTERVAL"] = "180"
    os.environ["HEARTBEAT_INTERVAL"] = "60"
    os.environ["HEARTBEAT_TIMEOUT"] = "90"
    cfg = _reload_config()
    with pytest.raises(SystemExit):
        cfg.validate_config()


def test_validate_backup_interval_too_small():
    """BACKUP_INTERVAL < 30 应退出"""
    os.environ["DEMO_KEY"] = "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a"
    os.environ["EXEC_TOKEN"] = "test"
    os.environ["PORT"] = "8080"
    os.environ["BACKUP_INTERVAL"] = "10"
    os.environ["HEARTBEAT_INTERVAL"] = "60"
    os.environ["HEARTBEAT_TIMEOUT"] = "90"
    cfg = _reload_config()
    with pytest.raises(SystemExit):
        cfg.validate_config()


def test_validate_no_exec_token():
    """无 EXEC_TOKEN 应退出"""
    os.environ["EXEC_TOKEN"] = ""
    os.environ["DEMO_KEY"] = "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a"
    os.environ["PORT"] = "8080"
    os.environ["BACKUP_INTERVAL"] = "180"
    os.environ["HEARTBEAT_INTERVAL"] = "60"
    os.environ["HEARTBEAT_TIMEOUT"] = "90"
    cfg = _reload_config()
    with pytest.raises(SystemExit):
        cfg.validate_config()

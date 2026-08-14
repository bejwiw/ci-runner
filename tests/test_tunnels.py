# -*- coding: utf-8 -*-
"""
隧道持久化管理测试

测试：token 解析、config.yml 生成、credentials 路径更新
"""
import os
import sys
import json
import base64

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from worker.process import tunnels


def test_parse_token():
    """解析 cloudflared token"""
    token_data = {"a": "acc123", "t": "tnl456", "s": "sct789"}
    token = base64.b64encode(json.dumps(token_data).encode()).decode()
    account, tunnel_id, secret = tunnels._parse_token(token)
    assert account == "acc123"
    assert tunnel_id == "tnl456"
    assert secret == "sct789"


def test_parse_token_invalid():
    """非法 token 返回空字符串"""
    a, t, s = tunnels._parse_token("not-a-valid-token")
    assert a == ""
    assert t == ""
    assert s == ""


def test_parse_token_empty():
    """空 token 返回空"""
    a, t, s = tunnels._parse_token("")
    assert a == "" and t == "" and s == ""


def test_generate_config_yml(tmp_path):
    """生成 config.yml 含 ingress 规则"""
    creds_path = str(tmp_path / "creds.json")
    config_path = tunnels._generate_config_yml(
        "tunnel123", creds_path,
        [{"hostname": "example.com", "service": "http://localhost:8080"}],
        str(tmp_path))
    content = open(config_path).read()
    assert "tunnel: tunnel123" in content
    assert creds_path in content
    assert "hostname: example.com" in content
    assert "service: http://localhost:8080" in content
    # 自动补 catch-all
    assert "http_status:404" in content


def test_generate_config_yml_catchall(tmp_path):
    """已有 catch-all 时不重复添加"""
    config_path = tunnels._generate_config_yml(
        "tid", "",
        [{"hostname": "a.com", "service": "http://localhost:80"},
         {"service": "http_status:503"}],
        str(tmp_path))
    content = open(config_path).read()
    assert "http_status:503" in content
    # 不应额外添加 catch-all
    assert content.count("http_status:404") == 0


def test_update_config_yml_credentials(tmp_path):
    """更新 config.yml 里的 credentials-file 路径"""
    config_path = str(tmp_path / "config.yml")
    with open(config_path, "w") as f:
        f.write("tunnel: tid\ncredentials-file: /old/path.json\ningress:\n  - service: http_status:404\n")
    tunnels._update_config_yml_credentials(config_path, "/new/path.json")
    content = open(config_path).read()
    assert "/new/path.json" in content
    assert "/old/path.json" not in content


def test_generate_config_yml_no_tunnel_id(tmp_path):
    """无 tunnel_id 时不写 tunnel 行"""
    config_path = tunnels._generate_config_yml(
        "", "/creds.json",
        [{"hostname": "test.com", "service": "http://localhost:3000"}],
        str(tmp_path))
    content = open(config_path).read()
    assert "tunnel:" not in content
    assert "credentials-file: /creds.json" in content

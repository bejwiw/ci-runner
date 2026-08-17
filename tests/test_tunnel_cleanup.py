# -*- coding: utf-8 -*-
"""
隧道孤儿清理逻辑测试

验证 _token_extract_tunnel_id 正确提取 tunnel_id，
以及清理时只用唯一标识（tunnel_id）匹配，不误杀其他隧道。
"""
import os
import sys
import json
import base64

os.environ.setdefault("EXEC_TOKEN", "test")
os.environ.setdefault("DEMO_KEY", "0a7deb9b0978e05d5a1ffe405ece28fa2158360c54ff2cf5847b3e8392e3069a")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _make_token(tunnel_id):
    """构造 CF 格式 token：base64({a: account, t: tunnel_id, s: secret})"""
    payload = json.dumps({
        "a": "09486a5b2a376338e6511d3c0c093d8c",  # 同一 account
        "t": tunnel_id,
        "s": "some-secret-value",
    }).encode()
    return base64.b64encode(payload).decode()


def test_token_extract_tunnel_id():
    """从 token 解码出 tunnel_id"""
    from worker.process.tunnels import _token_extract_tunnel_id
    token = _make_token("cb9e4a18-74fd-4f91-b95b-46b0d258b970")
    assert _token_extract_tunnel_id(token) == "cb9e4a18-74fd-4f91-b95b-46b0d258b970"


def test_token_extract_empty():
    """空 token 返回空"""
    from worker.process.tunnels import _token_extract_tunnel_id
    assert _token_extract_tunnel_id("") == ""
    assert _token_extract_tunnel_id(None) == ""


def test_token_extract_invalid():
    """无效 token 返回空，不抛异常"""
    from worker.process.tunnels import _token_extract_tunnel_id
    assert _token_extract_tunnel_id("not-a-token!!!") == ""
    assert _token_extract_tunnel_id("abc") == ""


def test_kill_orphan_matches_only_own_tunnel(monkeypatch, tmp_path):
    """清理时只杀匹配 tunnel_id 的进程，不误杀其他隧道

    模拟：两个 cloudflared 进程（A=inst3主隧道, B=ddg-search），
    清理 ddg-search 时只杀 B，不能杀 A。
    """
    from worker.process import tunnels
    tid_a = "84c0a427-b23a-45b9-9176-b4071bc78fc0"
    tid_b = "cb9e4a18-74fd-4f91-b95b-46b0d258b970"
    token_a = _make_token(tid_a)
    token_b = _make_token(tid_b)

    killed = []

    # 模拟 /proc 里的进程
    fake_procs = {
        "1001": f"cloudflared tunnel --no-autoupdate run --token {token_a}",
        "1002": f"cloudflared tunnel --no-autoupdate run --token {token_b}",
        "1003": "python3 app.py",  # 非 cloudflared
    }

    def fake_listdir(path):
        return list(fake_procs.keys())

    def fake_open(path, *a, **kw):
        pid = path.split("/")[2]
        import io
        return io.StringIO("")

    real_open = open
    def fake_read_cmdline(path, mode="rb"):
        pid = path.split("/")[2]
        import io
        return io.BytesIO(fake_procs[pid].encode())

    def fake_kill(pid, sig):
        killed.append(pid)

    monkeypatch.setattr(os, "listdir", fake_listdir)
    monkeypatch.setattr(tunnels.os, "listdir", fake_listdir)
    # 替换 open 读取 cmdline 的逻辑
    import builtins
    orig_open = builtins.open
    def patched_open(path, *args, **kwargs):
        if "/proc/" in path and "cmdline" in path:
            return fake_read_cmdline(path, *args[1:], **kwargs)
        return orig_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", patched_open)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(tunnels.os, "kill", fake_kill)

    # 清理 ddg-search 的隧道（tunnel_id = tid_b）
    tunnels._kill_orphan_tunnels("ddg-search", {
        "token": token_b,
        "tunnel_id": tid_b,
    })

    # 只杀 pid 1002（B），不能杀 1001（A 主隧道）
    assert killed == [1002]


def test_kill_orphan_uses_token_tunnel_id(monkeypatch):
    """没有显式 tunnel_id 时，从 token 解码提取"""
    from worker.process import tunnels
    tid_b = "cb9e4a18-74fd-4f91-b95b-46b0d258b970"
    token_b = _make_token(tid_b)

    killed = []
    fake_procs = {
        "2001": f"cloudflared run --token {_make_token('84c0a427-b23a-45b9-9176-b4071bc78fc0')}",
        "2002": f"cloudflared run --token {token_b}",
    }

    def fake_listdir(path):
        return list(fake_procs.keys())

    def fake_read_cmdline(path, mode="rb"):
        pid = path.split("/")[2]
        import io
        return io.BytesIO(fake_procs[pid].encode())

    import builtins
    orig_open = builtins.open
    def patched_open(path, *args, **kwargs):
        if "/proc/" in path and "cmdline" in path:
            return fake_read_cmdline(path, *args[1:], **kwargs)
        return orig_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", patched_open)
    monkeypatch.setattr(os, "listdir", fake_listdir)
    monkeypatch.setattr(tunnels.os, "listdir", fake_listdir)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(tunnels.os, "kill", lambda pid, sig: killed.append(pid))

    # 只给 token，不显式给 tunnel_id（完整token匹配，唯一不误杀）
    tunnels._kill_orphan_tunnels("ddg-search", {"token": token_b})

    assert killed == [2002]
# -*- coding: utf-8 -*-
"""HTTP API 客户端（带 token，统一错误处理）"""
import json
import urllib.request
import urllib.error

from cli import config


def _api(method, url, data=None, timeout=60, token=None):
    h = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token or config.TOKEN}",
        "User-Agent": "Mozilla/5.0 (Linux; Android) ghbox-cli",
    }
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, method=method, headers=h, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get(path, timeout=60):
    return _api("GET", config.mgr(path), timeout=timeout)


def post(path, data=None, timeout=90):
    return _api("POST", config.mgr(path), data=data, timeout=timeout)


def delete(path, timeout=60):
    return _api("DELETE", config.mgr(path), timeout=timeout)


def get_url(url, timeout=60):
    return _api("GET", url, timeout=timeout)


def post_url(url, data=None, timeout=60):
    return _api("POST", url, data=data, timeout=timeout)


def post_inst(hostname, path, data=None, timeout=60):
    """直接请求实例 API"""
    url = config.inst_url(hostname) + path
    return _api("POST", url, data=data, timeout=timeout)


def get_inst(hostname, path, timeout=60):
    url = config.inst_url(hostname) + path
    return _api("GET", url, timeout=timeout)

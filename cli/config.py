# -*- coding: utf-8 -*-
"""CLI 配置：token / manager 地址 / 会话持久化"""
import os
import uuid

MANAGER = os.environ.get("GHBOX_MANAGER", "https://ghvps2.kekeke.cc.cd")
TOKEN = os.environ.get("EXEC_TOKEN", "")
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "kekeke.cc.cd")

SESSION_FILE = os.path.expanduser("~/.ghbox_session")


def load_session():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE) as f:
                k = f.read().strip()
                if k:
                    return k
        except Exception:
            pass
    k = uuid.uuid4().hex
    try:
        with open(SESSION_FILE, "w") as f:
            f.write(k)
    except Exception:
        pass
    return k


def set_manager(url):
    global MANAGER
    MANAGER = url.rstrip("/")


def set_token(token):
    global TOKEN
    TOKEN = token


def mgr(path):
    return MANAGER.rstrip("/") + path


def inst_url(hostname):
    return f"https://{hostname}"

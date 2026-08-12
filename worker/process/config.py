# -*- coding: utf-8 -*-
"""
进程配置读写

build_config: 如果ghvps.json中的cwd与进程实际cwd不一致，
自动修复为进程实际cwd并回写ghvps.json。
"""
import os
import json
import time

import config
import log

logger = log.setup_logger("proc.config")

DEFAULT_EXCLUDE = config.PROC_BACKUP_EXCLUDE


def is_bash_session(cfg):
    """判断是否是bash终端会话（应该排除，不是服务进程）"""
    cmd = (cfg or {}).get("command", "")
    if not cmd or not cmd.startswith("bash"):
        return False
    parts = cmd.split()
    has_script = any(not p.startswith("-") for p in parts[1:])
    return not has_script


def proc_dir():
    return config.PROC_DIR


def proc_config_path(name):
    return os.path.join(config.PROC_DIR, name, "ghvps.json")


def manifest_path():
    return os.path.join(config.PROC_DIR, "manifest.json")


# 排除的敏感/易变环境变量
_SKIP_ENV = (
    "PATH", "HOME", "USER", "SHELL", "PWD", "_", "SHLVL", "LANG", "LC_ALL",
    "TERM", "OLDPWD", "GITHUB_", "RUNNER_", "ACTIONS_", "CI", "GH_TOKEN",
    "DEMO_KEY", "EXEC_TOKEN", "TUNNEL_TOKEN", "CF_", "AWS_", "AZURE_",
    "S3_", "DECRYPT_KEY", "MANAGER_HOST", "BASE_DOMAIN",
    "INSTANCE_ROLE", "INSTANCE_ID", "REPO", "MAIN_REPO",
    "CURRENT_SHA", "MCP_PORT", "MCP_PREFIX", "FILES_DIR",
    "PORT", "BACKUP_INTERVAL", "PRE_WAKE_SECONDS",
    "HEARTBEAT_INTERVAL", "HEARTBEAT_TIMEOUT",
    "PROC_SCAN_INTERVAL", "DISK_", "SESSION_TTL",
    "GHBOX_JOB_ID", "LOG_LEVEL", "S3_ACCOUNTS_FILE",
)
_PREFIX_ENV = tuple(e for e in _SKIP_ENV if e.endswith("_"))
_EXACT_ENV = tuple(e for e in _SKIP_ENV if not e.endswith("_"))


def read_env(pid):
    env = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            data = f.read().split(b"\x00")
        for item in data:
            if b"=" not in item:
                continue
            k, _, v = item.partition(b"=")
            k = k.decode(errors="replace")
            v = v.decode(errors="replace")
            if not k:
                continue
            if k in _EXACT_ENV:
                continue
            if any(k.startswith(p) for p in _PREFIX_ENV):
                continue
            env[k] = v
    except Exception:
        pass
    return env


def build_config(info):
    """从进程信息构建配置。如果ghvps.json的cwd过时，自动修复。"""
    cwd = info.cwd
    cfg = None
    ghvps_path = None
    if cwd and os.path.isdir(cwd):
        ghvps_path = os.path.join(cwd, "ghvps.json")
        if os.path.exists(ghvps_path):
            try:
                with open(ghvps_path) as f:
                    cfg = json.load(f)
            except Exception:
                cfg = None
    if cfg is None:
        # 没有ghvps.json，自动生成
        cfg = {
            "name": info.name,
            "command": info.cmdline_str(),
            "cwd": cwd or os.path.expanduser("~"),
            "env": read_env(info.pid),
            "install": [],
            "exclude": list(DEFAULT_EXCLUDE),
            "auto_restart": True,
            "restart_delay": 3,
        }
    else:
        # 有ghvps.json，补全缺失字段
        cfg.setdefault("name", info.name)
        cfg.setdefault("command", info.cmdline_str())
        cfg.setdefault("env", read_env(info.pid))
        cfg.setdefault("install", [])
        cfg.setdefault("exclude", list(DEFAULT_EXCLUDE))
        cfg.setdefault("auto_restart", True)
        cfg.setdefault("restart_delay", 3)

        # 核心：cwd自动修复
        # 如果ghvps.json中的cwd和进程实际cwd不一致，
        # 说明目录迁移了（如/home/runner/files→/home/kodebite），
        # 自动修复ghvps.json中的cwd为进程实际cwd
        old_cwd = cfg.get("cwd", "")
        if cwd and old_cwd != cwd:
            cfg["cwd"] = cwd
            # 回写修复后的ghvps.json到进程cwd下
            if ghvps_path:
                try:
                    with open(ghvps_path, "w") as f:
                        json.dump(cfg, f, indent=2, ensure_ascii=False)
                    logger.info(f"[config] {cfg.get('name')} cwd自动修复: {old_cwd} -> {cwd}")
                except Exception as e:
                    logger.warning(f"[config] {cfg.get('name')} ghvps.json回写失败: {e}")

    cfg["source_pid"] = info.pid
    cfg["saved_at"] = time.time()
    return cfg


def save_proc_config(cfg):
    """保存配置到processes/<name>/ghvps.json"""
    try:
        d = os.path.join(proc_dir(), cfg["name"])
        os.makedirs(d, exist_ok=True)
        with open(proc_config_path(cfg["name"]), "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"保存配置失败 {cfg.get('name')}: {e}")
        return False


def load_proc_config(name):
    """从processes/<name>/ghvps.json读取配置"""
    path = proc_config_path(name)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_manifest(processes, reason="periodic"):
    """保存进程清单"""
    manifest = {
        "version": 2,
        "saved_at": time.time(),
        "reason": reason,
        "processes": processes,
    }
    try:
        with open(manifest_path(), "w") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"保存清单失败: {e}")
        return False


def load_manifest():
    """读取进程清单"""
    path = manifest_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f).get("processes", {}) or {}
    except Exception:
        return {}

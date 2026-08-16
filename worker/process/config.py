# -*- coding: utf-8 -*-
"""
进程配置读写（重构版）

核心改变：不再依赖scanner扫描/proc，改为扫描ghvps.json配置文件。
scan_configs()是唯一的进程发现入口。
"""
import os
import json
import time

import config
import log

logger = log.setup_logger("proc.config")

DEFAULT_EXCLUDE = config.PROC_BACKUP_EXCLUDE

# 不扫描这些目录
SKIP_DIRS = {"processes", "logs", "mcp-server", "mcp-files", "sysconfig", "demo.db", ".backup_tmp"}


def proc_dir():
    return config.PROC_DIR


def proc_config_path(name):
    return os.path.join(config.PROC_DIR, name, "ghvps.json")


def manifest_path():
    return os.path.join(config.PROC_DIR, "manifest.json")


def is_bash_session(cfg):
    """判断是否是bash终端会话（应该跳过）"""
    cmd = (cfg or {}).get("command", "")
    if not cmd or not cmd.startswith("bash"):
        return False
    parts = cmd.split()
    has_script = any(not p.startswith("-") for p in parts[1:])
    return not has_script


def pid_file_path(name):
    """PID文件路径"""
    return os.path.join(config.PROC_DIR, name, "pid")


def write_pid_file(name, pid):
    """写PID文件"""
    try:
        d = os.path.dirname(pid_file_path(name))
        os.makedirs(d, exist_ok=True)
        with open(pid_file_path(name), "w") as f:
            f.write(str(pid))
    except Exception as e:
        logger.warning(f"{name}: 写PID文件失败: {e}")


def read_pid_file(name):
    """读PID文件"""
    try:
        with open(pid_file_path(name)) as f:
            return int(f.read().strip())
    except Exception:
        return None


def delete_pid_file(name):
    """删PID文件"""
    try:
        os.remove(pid_file_path(name))
    except Exception as e:
        logger.debug(f"读取失败: {e}")


def scan_configs():
    """扫描FILES_DIR下所有ghvps.json，返回 {name: cfg}

    这是唯一的进程发现入口。不扫描/proc。
    """
    configs = {}
    base = config.FILES_DIR
    if not os.path.isdir(base):
        return configs
    try:
        entries = os.listdir(base)
    except Exception as e:
        logger.error(f"扫描 {base} 失败: {e}")
        return configs
    for entry in entries:
        if entry in SKIP_DIRS:
            continue
        ghvps_path = os.path.join(base, entry, "ghvps.json")
        if not os.path.isfile(ghvps_path):
            continue
        try:
            with open(ghvps_path) as f:
                cfg = json.load(f)
        except Exception as e:
            logger.warning(f"{entry}: ghvps.json读取失败: {e}")
            continue
        # name验证
        name = cfg.get("name", "").strip()
        if not name:
            name = entry
            cfg["name"] = name
        # command验证
        if not cfg.get("command", "").strip():
            logger.warning(f"{name}: command为空，跳过")
            continue
        # bash终端会话跳过
        if is_bash_session(cfg):
            logger.info(f"{name}: bash终端会话，跳过")
            continue
        # cwd验证：不在FILES_DIR下则跳过
        cwd = cfg.get("cwd", "").strip()
        if not cwd:
            cwd = os.path.join(base, entry)
            cfg["cwd"] = cwd
        if not cwd.startswith(config.FILES_DIR):
            logger.warning(f"{name}: cwd={cwd} 不在{config.FILES_DIR}下，跳过")
            continue
        # name去重
        if name in configs:
            logger.warning(f"{name}: 重复配置，跳过")
            continue
        cfg.setdefault("install", [])
        cfg.setdefault("exclude", list(DEFAULT_EXCLUDE))
        cfg.setdefault("auto_restart", True)
        cfg.setdefault("restart_delay", 3)
        cfg.setdefault("env", {})
        cfg.setdefault("tunnels", [])
        cfg["source_pid"] = 0
        cfg["saved_at"] = time.time()
        configs[name] = cfg
    return configs


def save_proc_config(cfg):
    """保存配置到processes/<name>/ghvps.json（备份用）"""
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
    """从processes/<name>/ghvps.json读取配置（备份版本）"""
    path = proc_config_path(name)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"写PID失败: {e}")
    return None


def save_manifest(processes, reason="periodic"):
    """保存清单（保留兼容，但新架构不依赖manifest）"""
    manifest = {
        "version": 3,
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
    """读取清单（保留兼容）"""
    path = manifest_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f).get("processes", {}) or {}
    except Exception:
        return {}

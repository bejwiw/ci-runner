# -*- coding: utf-8 -*-
"""
进程恢复/启动/停止/重启

修复旧项目 bug：install_deps 超时从 600 秒降到 180 秒。
"""
import os
import time
import signal
import subprocess

import config
import log
from core import utils
from worker.process import config as pconfig

logger = log.setup_logger("proc.restore")


def restore_files(cfg):
    name = cfg.get("name", "proc")
    if not cfg.get("files_backed", True):
        return True
    src = os.path.join(pconfig.proc_dir(), name, "app")
    cwd = cfg.get("cwd") or ""
    if not src or not os.path.isdir(src) or not cwd:
        return True
    # 修正cwd：如果不是FILES_DIR下的路径，迁移到FILES_DIR下
    if not cwd.startswith(config.FILES_DIR):
        old_basename = os.path.basename(cwd.rstrip("/")) if cwd else name
        cwd = os.path.join(config.FILES_DIR, old_basename)
        cfg["cwd"] = cwd
        pconfig.save_proc_config(cfg)
        logger.info(f"[restore] {name} cwd 迁移到 {cwd}")
    os.makedirs(cwd, exist_ok=True)
    count = utils.copy_tree(src, cwd, set())
    logger.info(f"[restore] {name} 恢复了 {count} 个文件")
    return True


def install_deps(cfg):
    """执行依赖安装（超时 180 秒）"""
    cwd = cfg.get("cwd") or os.path.expanduser("~")
    for cmd in cfg.get("install") or []:
        logger.info(f"[restore] {cfg['name']} 安装: {cmd}")
        code, out, err = utils.run_cmd(cmd, timeout=180, cwd=cwd)
        if code != 0:
            raise RuntimeError(f"安装失败: {err[:200]}")


def start_process(name, cfg=None):
    cfg = cfg or pconfig.load_proc_config(name)
    if not cfg:
        return False, None
    command = cfg.get("command") or ""
    if not command:
        return False, None
    cwd = cfg.get("cwd") or os.path.expanduser("~")
    env = dict(os.environ)
    for k, v in (cfg.get("env") or {}).items():
        env[k] = v
    try:
        _, logpath = log.process_logger(name)
        logf = open(logpath, "ab")
    except Exception:
        logf = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            command, shell=True, stdout=logf, stderr=subprocess.STDOUT,
            cwd=cwd, env=env, start_new_session=True, executable="/bin/bash")
        logger.info(f"[start] {name} (pid={proc.pid})")
        time.sleep(2)
        if proc.poll() is not None:
            logger.error(f"[start] {name} 立即退出 (exit={proc.returncode})")
            return False, None
        return True, proc.pid
    except Exception as e:
        logger.error(f"[start] {name} 失败: {e}")
        return False, None


def stop_process(name, cfg=None, pid=None):
    cfg = cfg or pconfig.load_proc_config(name)
    if not cfg:
        return False, "无配置"
    if not pid:
        pid = cfg.get("source_pid")
    if not pid or not utils.is_alive(pid):
        return False, "进程未运行"
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(1.5)
        if utils.is_alive(pid):
            os.kill(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    logger.info(f"[stop] {name} (pid={pid})")
    return True, "已停止"


def restore_one(name, cfg=None):
    """恢复并启动单个进程（含重试）"""
    cfg = cfg or pconfig.load_proc_config(name)
    if not cfg:
        return False, None
    for attempt in range(config.PROC_MAX_RETRY + 1):
        try:
            restore_files(cfg)
            install_deps(cfg)
            ok, pid = start_process(name, cfg)
            if ok:
                return True, pid
            if attempt < config.PROC_MAX_RETRY:
                delay = config.PROC_RETRY_DELAY[min(attempt, len(config.PROC_RETRY_DELAY) - 1)]
                logger.warning(f"[restore] {name} 第{attempt+1}次失败，{delay}s后重试")
                time.sleep(delay)
        except Exception as e:
            if attempt < config.PROC_MAX_RETRY:
                delay = config.PROC_RETRY_DELAY[min(attempt, len(config.PROC_RETRY_DELAY) - 1)]
                logger.warning(f"[restore] {name} 第{attempt+1}次失败: {e}，{delay}s后重试")
                time.sleep(delay)
            else:
                logger.error(f"[restore] {name} 最终失败: {e}")
    return False, None


def restore_all():
    """恢复并启动所有持久化进程"""
    procs = pconfig.load_manifest()
    if not procs:
        return 0, 0
    restored, failed = 0, 0
    for name in procs:
        ok, _ = restore_one(name)
        if ok:
            restored += 1
        else:
            failed += 1
    logger.info(f"[restore] {restored} 成功, {failed} 失败")
    return restored, failed

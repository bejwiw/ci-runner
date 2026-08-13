# -*- coding: utf-8 -*-
"""
进程恢复/启动/停止（重构版）

核心改变：
- restore_all()从scan_configs()读取，不依赖manifest
- install_deps()用sudo -n执行
- start_process()写PID文件
- stop_process()从PID文件读PID，删PID文件
- restore_files()不再迁移cwd，cwd不在FILES_DIR下则跳过
"""
import os
import json
import time
import signal
import subprocess

import config
import log
from core import utils
from worker.process import config as pconfig

logger = log.setup_logger("proc.restore")


def restore_files(cfg):
    """从processes/<name>/app/复制文件回cwd"""
    name = cfg.get("name", "proc")
    if not cfg.get("files_backed", True):
        return True
    src = os.path.join(pconfig.proc_dir(), name, "app")
    cwd = cfg.get("cwd") or ""
    if not cwd or not os.path.isdir(src):
        return True
    # cwd不在FILES_DIR下则跳过（不修复，不报错）
    if not cwd.startswith(config.FILES_DIR):
        logger.warning(f"[restore] {name}: cwd={cwd} 不在{config.FILES_DIR}下，跳过文件恢复")
        return True
    os.makedirs(cwd, exist_ok=True)
    count = utils.copy_tree(src, cwd, set())
    logger.info(f"[restore] {name} 恢复了 {count} 个文件")
    return True


def install_deps(cfg):
    """执行依赖安装（直接执行，不用sudo。遇到权限错误自动sudo chown后重试）"""
    cwd = cfg.get("cwd") or os.path.expanduser("~")
    name = cfg.get("name", "proc")
    for cmd in cfg.get("install") or []:
        logger.info(f"[restore] {name} 安装: {cmd}")
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=180, cwd=cwd, executable="/bin/bash")
        if r.returncode != 0:
            _stderr = r.stderr or ""
            _stdout = r.stdout or ""
            # 遇到权限错误，sudo chown修改目录权限后重试
            if "EACCES" in _stderr or "EACCES" in _stdout or "permission denied" in _stderr.lower():
                logger.warning(f"[restore] {name} 权限错误，自动修复权限后重试")
                subprocess.run(f"sudo chown -R runner:runner {cwd}", shell=True, timeout=30)
                r = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True,
                    timeout=180, cwd=cwd, executable="/bin/bash")
            if r.returncode != 0:
                _full_err = f"returncode={r.returncode}\nstderr={r.stderr or ''}\nstdout={r.stdout or ''}"
                logger.error(f"[restore] {name} 安装失败: {_full_err[:500]}")
                raise RuntimeError(f"安装失败(returncode={r.returncode}): {r.stderr[:300]}")


def start_process(name, cfg=None):
    """启动进程，写PID文件"""
    cfg = cfg or pconfig.load_proc_config(name)
    if not cfg:
        return False, None
    command = cfg.get("command") or ""
    if not command:
        return False, None
    cwd = cfg.get("cwd") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        os.makedirs(cwd, exist_ok=True)
    # env合并：os.environ + cfg["env"]
    env = {**os.environ, **(cfg.get("env") or {})}
    # 日志
    log_file = cfg.get("log_file") or ""
    if not log_file:
        log_file = os.path.join(config.LOGS_DIR, f"{name}.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    try:
        logf = open(log_file, "ab")
    except Exception:
        logf = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            command, shell=True, stdout=logf, stderr=subprocess.STDOUT,
            cwd=cwd, env=env, start_new_session=True, executable="/bin/bash")
        # 写PID文件
        pconfig.write_pid_file(name, proc.pid)
        logger.info(f"[start] {name} (pid={proc.pid})")
        time.sleep(2)
        if proc.poll() is not None:
            logger.error(f"[start] {name} 立即退出 (exit={proc.returncode})")
            pconfig.delete_pid_file(name)
            return False, None
        return True, proc.pid
    except Exception as e:
        logger.error(f"[start] {name} 失败: {e}")
        return False, None


def stop_process(name, cfg=None, pid=None):
    """停止进程，从PID文件读PID，删PID文件"""
    if not pid:
        pid = pconfig.read_pid_file(name)
    if not pid or not utils.is_alive(pid):
        pconfig.delete_pid_file(name)
        return False, "进程未运行"
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(1.5)
        if utils.is_alive(pid):
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    pconfig.delete_pid_file(name)
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
    """从scan_configs()读取所有ghvps.json，恢复进程"""
    configs = pconfig.scan_configs()
    if not configs:
        return 0, 0
    restored, failed = 0, 0
    for name, cfg in configs.items():
        ok, _ = restore_one(name, cfg)
        if ok:
            restored += 1
        else:
            failed += 1
    logger.info(f"[restore] {restored} 成功, {failed} 失败")
    return restored, failed

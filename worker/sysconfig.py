# -*- coding: utf-8 -*-
"""
系统配置备份恢复（systemd / cron / hosts）

- 备份 /etc/systemd/system/ 下的自定义服务
- 备份 crontab
- 恢复时自动 enable + start
"""
import os
import time
import shutil
import subprocess

import config
import log

logger = log.setup_logger("sysconfig")


def backup_system_config():
    """备份系统配置到持久化目录"""
    os.makedirs(config.SYSCONFIG_DIR, exist_ok=True)
    # systemd
    svc_dir = os.path.join(config.SYSCONFIG_DIR, "systemd")
    os.makedirs(svc_dir, exist_ok=True)
    sys_svc = "/etc/systemd/system"
    if os.path.isdir(sys_svc):
        for f in os.listdir(sys_svc):
            if f.endswith(".service"):
                src = os.path.join(sys_svc, f)
                dst = os.path.join(svc_dir, f)
                try:
                    shutil.copy2(src, dst)
                except Exception as e:
                    logger.debug(f"文件操作失败: {e}")
    # crontab
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            with open(os.path.join(config.SYSCONFIG_DIR, "crontab.txt"), "w") as f:
                f.write(r.stdout)
    except Exception as e:
        logger.debug(f"操作失败: {e}")
    # hosts
    try:
        shutil.copy2("/etc/hosts", os.path.join(config.SYSCONFIG_DIR, "hosts"))
    except Exception as e:
        logger.debug(f"操作失败: {e}")
    logger.info("系统配置已备份")


def restore_system_config():
    """恢复系统配置"""
    if not os.path.isdir(config.SYSCONFIG_DIR):
        return
    # systemd
    svc_dir = os.path.join(config.SYSCONFIG_DIR, "systemd")
    if os.path.isdir(svc_dir):
        for f in os.listdir(svc_dir):
            if f.endswith(".service"):
                src = os.path.join(svc_dir, f)
                dst = os.path.join("/etc/systemd/system", f)
                try:
                    subprocess.run(["sudo", "cp", src, dst], timeout=10)
                    subprocess.run(["sudo", "systemctl", "daemon-reload"], timeout=10)
                    subprocess.run(["sudo", "systemctl", "enable", f], timeout=10)
                    subprocess.run(["sudo", "systemctl", "start", f], timeout=10)
                    logger.info(f"恢复服务: {f}")
                except Exception as e:
                    logger.warning(f"恢复 {f} 失败: {e}")
    # crontab
    crontab_path = os.path.join(config.SYSCONFIG_DIR, "crontab.txt")
    if os.path.exists(crontab_path):
        try:
            subprocess.run(["crontab", crontab_path], timeout=10)
            logger.info("crontab 已恢复")
        except Exception as e:
            logger.warning(f"crontab 恢复失败: {e}")
    # hosts
    hosts_path = os.path.join(config.SYSCONFIG_DIR, "hosts")
    if os.path.exists(hosts_path):
        try:
            subprocess.run(["sudo", "cp", hosts_path, "/etc/hosts"], timeout=10)
        except Exception as e:
            logger.debug(f"操作失败: {e}")
    logger.info("系统配置恢复完成")

# -*- coding: utf-8 -*-
"""
进程快照元数据记录

snapshot() 从 scan_configs() 读取进程列表，记录元数据（PID/命令/cwd/大小）。
文件备份由 persistence.backup_files() 全量备份负责。
"""
import os
import time

import config
import log
from core import utils
from worker.process import config as pconfig

logger = log.setup_logger("proc.backup")


def snapshot(reason="periodic"):
    """扫描ghvps.json，记录所有进程快照元数据

    只记录元数据（PID/命令/cwd/大小），不复制文件。
    文件备份由 persistence.backup_files() 全量备份负责。
    """
    configs = pconfig.scan_configs()
    if not configs:
        return 0, {}
    saved = 0
    processes_meta = {}
    for name, cfg in configs.items():
        pid = pconfig.read_pid_file(name)
        cwd = cfg.get("cwd") or ""
        size_mb = 0
        files_backed = False
        if cwd and os.path.isdir(cwd):
            size_mb = utils.dir_size_mb(cwd)
            if size_mb <= config.PROC_MAX_BACKUP_MB:
                files_backed = True
                saved += 1
            else:
                logger.warning(f"{name} 过大({size_mb:.1f}MB)，跳过文件备份")
        processes_meta[name] = {
            "name": name,
            "pid": pid,
            "cmdline": cfg.get("command", ""),
            "cwd": cwd,
            "size_mb": round(size_mb, 2),
            "files_backed": files_backed,
            "saved_at": cfg.get("saved_at"),
        }
    pconfig.save_manifest(processes_meta, reason=reason)
    logger.info(f"{saved}/{len(configs)} 个进程快照已记录（{reason}）")
    return saved, processes_meta



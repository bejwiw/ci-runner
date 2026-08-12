# -*- coding: utf-8 -*-
"""
进程文件快照与备份

- 复制进程 cwd 下项目文件到 processes/<name>/app/
- 原子替换（先复制到 tmp，完成后替换）
- 排除可重建目录
- 打包/解包 processes 目录
"""
import os
import io
import shutil
import tarfile

import config
import log
from core import utils
from worker.process import config as pconfig

logger = log.setup_logger("proc.backup")


def _clear_dir(path):
    if not os.path.isdir(path):
        return
    for item in os.listdir(path):
        p = os.path.join(path, item)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except Exception:
                pass


def backup_process_files(cfg):
    """备份单个进程项目文件（原子操作）"""
    cwd = cfg.get("cwd") or ""
    name = cfg.get("name", "proc")
    if not cwd or not os.path.isdir(cwd):
        cfg["files_backed"] = False
        return False, 0, cfg

    dest = os.path.join(pconfig.proc_dir(), name, "app")
    tmp_dest = os.path.join(pconfig.proc_dir(), name, "app.tmp")
    os.makedirs(tmp_dest, exist_ok=True)
    _clear_dir(tmp_dest)

    exclude = set(cfg.get("exclude") or pconfig.DEFAULT_EXCLUDE)
    exclude.add(os.path.basename(pconfig.proc_dir().rstrip("/")))
    if "processes" in cwd.split(os.sep):
        exclude.add("processes")

    count = 0
    skipped = 0
    for root, dirs, files in os.walk(cwd):
        rel = os.path.relpath(root, cwd)
        dirs[:] = [d for d in dirs if d not in exclude and rel not in exclude]
        for f in files:
            if rel in exclude:
                continue
            s = os.path.join(root, f)
            d = os.path.join(tmp_dest, rel, f)
            try:
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)
                count += 1
            except Exception:
                skipped += 1

    size_mb = utils.dir_size_mb(tmp_dest)
    if size_mb > config.PROC_MAX_BACKUP_MB:
        logger.warning(f"[backup] {name} 过大({size_mb:.1f}MB)，跳过")
        shutil.rmtree(tmp_dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
        cfg["files_backed"] = False
    else:
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        try:
            os.rename(tmp_dest, dest)
        except OSError:
            os.makedirs(dest, exist_ok=True)
            _clear_dir(dest)
            utils.copy_tree(tmp_dest, dest, set())
            shutil.rmtree(tmp_dest, ignore_errors=True)
        cfg["files_backed"] = True
        cfg["files_count"] = count
        cfg["size_mb"] = round(size_mb, 2)
        logger.info(f"[backup] {name}: {count} 文件, {size_mb:.1f}MB")

    from worker.process import tunnels
    tunnels.copy_tunnel_files(cfg, name, pconfig.proc_dir())
    pconfig.save_proc_config(cfg)
    return True, size_mb, cfg


def pack_processes_tar():
    """打包 processes 目录为 gz 字节流"""
    if not os.path.isdir(pconfig.proc_dir()):
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(pconfig.proc_dir(), arcname="processes")
    return buf.getvalue()


def unpack_processes_tar(data):
    """解包到 files 目录（保留权限）"""
    if not data:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            try:
                tar.extractall(path=config.FILES_DIR, filter="tar")
            except TypeError:
                tar.extractall(path=config.FILES_DIR)
        logger.info("[backup] 进程快照解包完成")
        return True
    except Exception as e:
        logger.error(f"[backup] 解包失败: {e}")
        return False


def snapshot(reason="periodic"):
    """扫描并备份所有用户进程"""
    from worker.process import scanner
    procs = scanner.scan_user_processes()
    if not procs:
        return 0, {}
    saved = 0
    processes_meta = {}
    seen_cwds = set()
    for info in procs:
        if info.cwd in seen_cwds:
            continue
        seen_cwds.add(info.cwd)
        try:
            cfg = pconfig.build_config(info)
            ok, size_mb, cfg = backup_process_files(cfg)
            if ok:
                saved += 1
                name = cfg.get("name", info.name)
                processes_meta[name] = {
                    "name": name,
                    "pid": info.pid,
                    "cmdline": info.cmdline_str(),
                    "cwd": info.cwd,
                    "size_mb": round(size_mb, 2),
                    "files_backed": cfg.get("files_backed", True),
                    "saved_at": cfg.get("saved_at"),
                }
        except Exception as e:
            logger.error(f"[snapshot] 备份 {info.name} 失败: {e}")
    pconfig.save_manifest(processes_meta, reason=reason)
    logger.info(f"[snapshot] {saved} 个进程持久化（{reason}）")
    return saved, processes_meta


def pack_processes_to_disk():
    """打包processes目录到磁盘文件"""
    if not os.path.isdir(pconfig.proc_dir()):
        return None
    tmp = os.path.join(os.path.expanduser("~"), ".backup_tmp", "proc_backup.tar.gz")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(pconfig.proc_dir(), arcname="processes")
    return tmp


def unpack_processes_from_file(file_path):
    """从磁盘文件解包"""
    try:
        with tarfile.open(file_path, "r:gz") as tar:
            try:
                tar.extractall(path=config.FILES_DIR, filter="tar")
            except TypeError:
                tar.extractall(path=config.FILES_DIR)
        logger.info("[backup] 进程快照解包完成(磁盘)")
        return True
    except Exception as e:
        logger.error(f"[backup] 解包失败: {e}")
        return False

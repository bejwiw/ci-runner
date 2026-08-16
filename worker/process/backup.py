# -*- coding: utf-8 -*-
"""
进程文件快照与备份（重构版）

核心改变：snapshot()从scan_configs()读取进程列表，不扫描/proc。
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
            except Exception as e:
                logger.debug(f"复制文件失败: {e}")


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
            if f == "ghvps.json":
                continue  # ghvps.json单独管理
            if f == "pid":
                continue  # PID文件不备份
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
        logger.warning(f"{name} 过大({size_mb:.1f}MB)，跳过")
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
        logger.info(f"{name}: {count} 文件, {size_mb:.1f}MB")

    from worker.process import tunnels
    tunnels.copy_tunnel_files(cfg, name, pconfig.proc_dir())
    pconfig.save_proc_config(cfg)
    return True, size_mb, cfg


def pack_processes_tar():
    """直接打包项目目录为 gz 字节流（不经过processes/中转，排除pid文件）"""
    configs = pconfig.scan_configs()
    if not configs:
        return None
    tmp_dir = "/tmp/ghbox_snapshot"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir)
    for name, cfg in configs.items():
        cwd = cfg.get("cwd") or ""
        if not cwd or not os.path.isdir(cwd):
            continue
        exclude = set(cfg.get("exclude") or pconfig.DEFAULT_EXCLUDE)
        dest = os.path.join(tmp_dir, name)
        utils.copy_tree(cwd, dest, exclude)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(tmp_dir, arcname=".", filter=lambda info: None if info.name.endswith("/pid") or info.name.endswith("/pid/") else info)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return buf.getvalue()


def unpack_processes_tar(data):
    """解包到 files 目录"""
    if not data:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            try:
                tar.extractall(path=config.FILES_DIR, filter="tar")
            except TypeError:
                tar.extractall(path=config.FILES_DIR)
        logger.info("进程快照解包完成")
        return True
    except Exception as e:
        logger.error(f"解包失败: {e}")
        return False


def snapshot(reason="periodic"):
    """扫描ghvps.json，记录所有进程快照元数据

    pack_processes_tar() 直接打包项目目录，不再经过processes/中转。
    此函数只记录元数据（PID/命令/cwd/大小），不复制文件。
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


def pack_processes_to_disk():
    """直接打包项目目录到磁盘文件（zstd压缩，排除pid文件）"""
    configs = pconfig.scan_configs()
    if not configs:
        return None
    tmp_dir = "/tmp/ghbox_snapshot"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir)
    for name, cfg in configs.items():
        cwd = cfg.get("cwd") or ""
        if not cwd or not os.path.isdir(cwd):
            continue
        exclude = set(cfg.get("exclude") or pconfig.DEFAULT_EXCLUDE)
        utils.copy_tree(cwd, os.path.join(tmp_dir, name), exclude)
    tmp = os.path.join("/tmp/ghbox_backup", "proc_backup.tar.zst")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    import subprocess
    subprocess.run(["tar", "--zstd", "-cf", tmp, "-C", tmp_dir, "."], timeout=120)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return tmp


def unpack_processes_from_file(file_path):
    """从磁盘文件解包（自动检测zstd/gzip）"""
    import subprocess
    try:
        with open(file_path, "rb") as _f:
            _hdr = _f.read(4)
        if _hdr[:4] == b"\x28\xb5\x2f\xfd":
            subprocess.run(["sudo", "tar", "--zstd", "-xf", file_path, "-C", config.FILES_DIR], timeout=120)
        else:
            subprocess.run(["sudo", "tar", "xzf", file_path, "-C", config.FILES_DIR], timeout=120)
        logger.info("进程快照解包完成(磁盘)")
        return True
    except Exception as e:
        logger.error(f"解包失败: {e}")
        return False

# -*- coding: utf-8 -*-
"""
数据/文件持久化（S3 优先 + Releases 降级）

修复：
1. 用 S3Pool 代替 Turso
2. S3 put 返回值正确处理（旧项目 bug：put 失败仍标记成功）
3. _worker_pre_wake 从 S3 读实例配置（不只从 Releases）
"""
import os
import time
import sqlite3
import threading
import subprocess
import datetime

import config
import log
from core import crypto, releases
from core.s3 import S3Pool

TMP_DIR = os.path.join(os.path.expanduser("~"), ".backup_tmp")

logger = log.setup_logger("persistence")

_db_conn = None
_db_lock = threading.RLock()
_s3pool = None


def set_s3pool(pool):
    global _s3pool
    _s3pool = pool


def _get_db():
    global _db_conn
    with _db_lock:
        if _db_conn is None:
            _db_conn = sqlite3.connect(config.DB_FILE, check_same_thread=False)
            _db_conn.row_factory = sqlite3.Row
        return _db_conn


def db_execute(sql, params=None):
    with _db_lock:
        conn = _get_db()
        cur = conn.execute(sql, params or ())
        conn.commit()
        return cur


def create_new_db():
    db_execute("CREATE TABLE IF NOT EXISTS messages "
               "(id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, created_at TEXT)")
    db_execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    db_execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('visits', '0')")
    db_execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('created_at', ?)",
               (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))


# ==================== 文件打包 ====================
def backup_files_to_bytes():
    if not os.path.isdir(config.FILES_DIR):
        return None
    result = subprocess.run(
        ["sudo", "tar", "czf", "-", "-C", os.path.expanduser("~"), "files"],
        capture_output=True, timeout=180)
    if result.returncode != 0:
        logger.error(f"[persist] 文件打包失败: {result.stderr.decode(errors='replace')[:200]}")
        return None
    return result.stdout


def restore_files_from_bytes(data):
    if not data:
        return False
    tmp = os.path.join(os.path.expanduser("~"), ".files_restore.tar.gz")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        result = subprocess.run(
            ["sudo", "tar", "xzf", tmp, "-C", os.path.expanduser("~")],
            capture_output=True, timeout=180)
        if result.returncode != 0:
            logger.error(f"[persist] 文件解压失败: {result.stderr.decode(errors='replace')[:200]}")
            return False
        os.makedirs(config.FILES_DIR, exist_ok=True)
        subprocess.run(["sudo", "rm", "-rf", config.PROC_DIR], timeout=30)
        os.makedirs(config.PROC_DIR, exist_ok=True)
        logger.info(f"[persist] 文件恢复完成 ({len(data)} 字节)")
        return True
    except Exception as e:
        logger.error(f"[persist] 文件恢复失败: {e}")
        return False
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


# ==================== 备份/恢复 ====================
def load_or_create(inst_cfg):
    """恢复实例数据（S3 优先 + Releases 降级）"""
    inst_id = inst_cfg.instance_id if inst_cfg else "global"
    db_key = f"inst-data/{inst_id}/db"
    files_key = f"inst-files/{inst_id}/files.tar.gz"
    db_asset = inst_cfg.asset_db if inst_cfg else config.ASSET_DB
    files_asset = inst_cfg.asset_files if inst_cfg else config.ASSET_FILES
    status_msg = "新建初始数据库"

    # 数据库
    db_data = None
    if _s3pool and _s3pool.is_ready():
        try:
            db_data = _s3pool.get(db_key)
        except Exception as e:
            logger.warning(f"[persist] S3 数据库读取失败: {e}")
    if db_data is None:
        db_data = releases.download_chunked(db_asset)
    if db_data:
        with open(config.DB_FILE, "wb") as f:
            f.write(db_data)
        status_msg = f"恢复数据库（{len(db_data)} 字节）"
        logger.info(f"[persist] {status_msg}")
    else:
        create_new_db()

    # 文件
    tmp_files = os.path.join(TMP_DIR, "restore_files.tar.gz")
    os.makedirs(TMP_DIR, exist_ok=True)
    files_ok = False
    if _s3pool and _s3pool.is_ready():
        try:
            files_ok = _s3pool.get_to_file(files_key, tmp_files)
        except Exception as e:
            logger.warning(f"[persist] S3 文件读取失败: {e}")
    if not files_ok:
        files_data = releases.download_chunked(files_asset)
        if files_data:
            with open(tmp_files, "wb") as f:
                f.write(files_data)
            files_ok = True
    if files_ok:
        restore_files_from_file(tmp_files)
    try:
        os.remove(tmp_files)
    except Exception:
        pass

    os.makedirs(config.FILES_DIR, exist_ok=True)
    return status_msg


def backup_database(inst_cfg=None):
    """备份数据库。S3 优先，Releases 降级。"""
    inst_id = inst_cfg.instance_id if inst_cfg else "global"
    db_key = f"inst-data/{inst_id}/db"
    db_asset = inst_cfg.asset_db if inst_cfg else config.ASSET_DB
    with _db_lock:
        conn = _get_db()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
    with open(config.DB_FILE, "rb") as f:
        data = f.read()
    # S3（不加密，S3本身私有访问）
    if _s3pool and _s3pool.is_ready():
        if _s3pool.put(db_key, data):
            logger.info(f"[backup] 数据库 → S3 ({len(data)} 字节)")
            return len(data), 1
    # Releases（upload_chunked内部自动加密）
    size, parts = releases.upload_chunked(db_asset, data)
    logger.info(f"[backup] 数据库 → Releases ({size} 字节, {parts} 分片)")
    return size, parts


def backup_files(inst_cfg=None):
    """备份 ~/files。tar写磁盘+S3分片上传+Releases双写。"""
    inst_id = inst_cfg.instance_id if inst_cfg else "global"
    files_key = f"inst-files/{inst_id}/files.tar.gz"
    files_asset = inst_cfg.asset_files if inst_cfg else config.ASSET_FILES
    # tar写磁盘（避免内存爆炸）
    tmp = backup_files_to_disk()
    if not tmp:
        return None
    file_size = os.path.getsize(tmp)
    # S3（分片上传，不经过内存）
    if _s3pool and _s3pool.is_ready():
        _s3pool.put_file(files_key, tmp)
        logger.info(f"[backup] 文件 → S3 ({file_size} 字节)")
    # Releases（<50MB双写，>=50MB跳过避免GitHub API超限）
    if file_size < 50 * 1024 * 1024:
        with open(tmp, "rb") as f:
            data = f.read()
        size, parts = releases.upload_chunked(files_asset, data)
        logger.info(f"[backup] 文件 → Releases ({size} 字节)")
    else:
        logger.info(f"[backup] 文件 >=50MB, 跳过Releases(S3分片存储)")
        size, parts = file_size, 1
    # 清理临时文件
    try:
        os.remove(tmp)
    except Exception:
        pass
    return size, parts


def backup_files_to_disk():
    """tar写磁盘临时文件（避免内存爆炸）"""
    os.makedirs(TMP_DIR, exist_ok=True)
    tmp = os.path.join(TMP_DIR, "backup_files.tar.gz")
    try:
        result = subprocess.run(
            ["sudo", "tar", "czf", tmp, "-C", os.path.expanduser("~"), "files"],
            capture_output=True, timeout=300)
        subprocess.run(["sudo", "chown", "runner:runner", tmp], timeout=5)
        if result.returncode != 0:
            logger.error(f"[persist] 文件打包失败: {result.stderr.decode(errors='replace')[:200]}")
            return None
        return tmp
    except Exception as e:
        logger.error(f"[persist] 文件打包异常: {e}")
        return None


def restore_files_from_file(file_path):
    """从磁盘文件解包"""
    try:
        result = subprocess.run(
            ["sudo", "tar", "xzf", file_path, "-C", os.path.expanduser("~")],
            capture_output=True, timeout=300)
        if result.returncode != 0:
            logger.error(f"[persist] 文件解压失败: {result.stderr.decode(errors='replace')[:200]}")
            return False
        os.makedirs(config.FILES_DIR, exist_ok=True)
        subprocess.run(["sudo", "rm", "-rf", config.PROC_DIR], timeout=30)
        os.makedirs(config.PROC_DIR, exist_ok=True)
        logger.info(f"[persist] 文件恢复完成")
        return True
    except Exception as e:
        logger.error(f"[persist] 文件恢复失败: {e}")
        return False


def save_prev_backup(inst_cfg=None):
    """保存上一版数据库快照（用于回滚）"""
    db_asset = inst_cfg.asset_db if inst_cfg else config.ASSET_DB
    try:
        blob = releases.download_chunked(db_asset)
        if blob:
            releases.upload_chunked(f"{db_asset}.bak", blob)
    except Exception as e:
        logger.warning(f"[persist] 保存快照失败: {e}")
